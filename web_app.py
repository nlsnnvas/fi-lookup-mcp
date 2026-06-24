"""
web_app.py
Local web dashboard ("FI Explorer") for the fi-lookup dataset.

A dependency-free Starlette app (Starlette + uvicorn both ship with FastMCP).
It builds the snapshot once on startup and reuses server.list_institutions for
live search / filter / sort, plus CSV/JSON export. Read-only, localhost only.

Run:
    python web_app.py            # serves http://127.0.0.1:8765
    python web_app.py --port 9000
"""

import os
import sys
import time
import base64
import csv
import hmac
import json
import tempfile
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, FileResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

import server
from data_loader import build_snapshot, get_all_institutions, get_data_as_of


# ---------------------------------------------------------------------------
# Share-safe configuration (all opt-in via env; defaults = open, localhost-only)
# ---------------------------------------------------------------------------
AUTH_USER = os.environ.get("FI_AUTH_USER", "")
AUTH_PASS = os.environ.get("FI_AUTH_PASS", "")
RATE_LIMIT_PER_MIN = int(os.environ.get("FI_RATE_LIMIT_PER_MIN", "240"))
DISABLE_PORTAL_CHECKS = os.environ.get("FI_DISABLE_PORTAL_CHECKS", "").lower() in ("1", "true", "yes")
# Hard ceiling on the outbound portal-check fan-out, regardless of query param.
PORTAL_CHECK_HARD_CAP = int(os.environ.get("FI_MAX_PORTAL_CHECKS", "60"))

_rate_buckets: dict = {}  # client-ip -> [minute_window, count]


def _rate_ok(ip: str) -> bool:
    if RATE_LIMIT_PER_MIN <= 0:
        return True
    win = int(time.time() // 60)
    rec = _rate_buckets.get(ip)
    if not rec or rec[0] != win:
        _rate_buckets[ip] = [win, 1]
        return True
    rec[1] += 1
    return rec[1] <= RATE_LIMIT_PER_MIN


def _auth_ok(header: str) -> bool:
    if not header.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace").partition(":")
    except Exception:
        return False
    # constant-time compare on both fields
    return (hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(pw, AUTH_PASS))


class GuardMiddleware(BaseHTTPMiddleware):
    """Rate-limit every request and (if configured) require HTTP basic auth."""

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        ip = request.client.host if request.client else "?"
        if not _rate_ok(ip):
            return PlainTextResponse("Too many requests — slow down.", status_code=429)
        if AUTH_USER and AUTH_PASS:
            if not _auth_ok(request.headers.get("authorization", "")):
                return Response("Authentication required.", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="FI Explorer"'})
        return await call_next(request)


# ---------------------------------------------------------------------------
# Query-param helpers
# ---------------------------------------------------------------------------

def _bool(q, name: str) -> bool:
    return q.get(name, "false").lower() in ("1", "true", "yes", "on")


def _int(q, name: str, default: int = 0) -> int:
    try:
        return int(q.get(name, default))
    except (TypeError, ValueError):
        return default


def _list_kwargs(q) -> dict:
    """Build list_institutions kwargs from the request query params."""
    return dict(
        search=q.get("search", ""),
        search_fields=q.get("search_fields", "name"),
        institution_type=q.get("institution_type", "all"),
        state=q.get("state", ""),
        min_deposit_accounts=_int(q, "min_deposit_accounts", 0),
        max_deposit_accounts=_int(q, "max_deposit_accounts", 0),
        has_routing=_bool(q, "has_routing"),
        has_rssd=_bool(q, "has_rssd"),
        has_history=_bool(q, "has_history"),
        has_divisions=_bool(q, "has_divisions"),
        business_lending=q.get("business_lending", ""),
        sba_lender=_bool(q, "sba_lender"),
        website_business=q.get("website_business", ""),
        website_small_business=q.get("website_small_business", ""),
        business_login=q.get("business_login", ""),
        service_provider=q.get("service_provider", ""),
        sort_by=q.get("sort_by", "deposit_accounts"),
        sort_order=q.get("sort_order", "desc"),
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

def _coverage_stats(insts):
    """Counts for the business-coverage / open-finance signals."""
    def c(pred):
        return sum(1 for i in insts if pred(i))
    return {
        "business_lending_yes":   c(lambda i: i.get("business_lending") == "yes"),
        "sba_lender":             c(lambda i: i.get("sba_lender")),
        "website_business":       c(lambda i: i.get("serves_business") is True),
        "website_small_business": c(lambda i: i.get("serves_smb") is True),
        "business_login":         c(lambda i: i.get("has_business_login") is True),
        "scanned":                c(lambda i: i.get("business_coverage_status") in ("scanned", "unreachable")),
    }


async def api_meta(request):
    insts = get_all_institutions()
    return JSONResponse({
        "data_as_of": get_data_as_of(),
        "total": len(insts),
        "banks": sum(1 for i in insts if i["source"] == "fdic"),
        "credit_unions": sum(1 for i in insts if i["source"] == "ncua"),
        "coverage": _coverage_stats(insts),
        "fields": server._LIST_FIELDS,
    })


async def api_overview(request):
    q = request.query_params
    top = await server.get_top_institutions(
        top_n=_int(q, "top_n", 15),
        institution_type=q.get("institution_type", "all"),
    )
    insts = get_all_institutions()
    providers = {}
    for i in insts:
        p = i.get("service_provider")
        if p:
            providers[p] = providers.get(p, 0) + 1
    providers = sorted(providers.items(), key=lambda kv: kv[1], reverse=True)
    conn = {}
    for i in insts:
        m = i.get("likely_connection_method") or "unknown"
        conn[m] = conn.get(m, 0) + 1
    states = {}
    for i in insts:
        st = server._canonical_state(i.get("state", ""))
        if st:
            states[st] = states.get(st, 0) + 1
    states = sorted(states.items(), key=lambda kv: kv[1], reverse=True)
    return JSONResponse({
        "data_as_of": get_data_as_of(),
        "total": len(insts),
        "banks": sum(1 for i in insts if i["source"] == "fdic"),
        "credit_unions": sum(1 for i in insts if i["source"] == "ncua"),
        "coverage": _coverage_stats(insts),
        "connection_methods": conn,
        "providers": providers,
        "states": states,
        "top": top,
    })


async def api_institutions(request):
    q = request.query_params
    result = await server.list_institutions(
        **_list_kwargs(q),
        limit=_int(q, "limit", 50),
        offset=_int(q, "offset", 0),
        fields="all",
    )
    return JSONResponse(result)


async def api_export(request):
    q = request.query_params
    fmt = q.get("format", "csv").lower()
    if fmt not in ("csv", "json"):
        return JSONResponse({"error": "format must be csv or json"}, status_code=400)

    tmpdir = tempfile.mkdtemp(prefix="fi_export_")
    path = os.path.join(tmpdir, f"fi_institutions.{fmt}")
    # export_path makes list_institutions write ALL matched rows (ignores paging).
    await server.list_institutions(**_list_kwargs(q), export_path=path, export_format=fmt)

    media = "text/csv" if fmt == "csv" else "application/json"
    return FileResponse(path, media_type=media, filename=f"fi_institutions.{fmt}")


async def api_profile(request):
    rssd = request.query_params.get("rssd", "").strip()
    if not rssd:
        return JSONResponse({"error": "rssd is required"}, status_code=400)
    return JSONResponse(await server.get_institution_history(rssd_id=rssd))


def _yn(v):
    return "yes" if v is True else ("no" if v is False else "unknown")


def _find_inst(q):
    """Locate one institution by cert / charter / rssd."""
    cert, charter, rssd = (q.get("cert", "").strip(), q.get("charter", "").strip(),
                           q.get("rssd", "").strip())
    for i in get_all_institutions():
        if cert and i.get("cert") == cert:
            return i
        if charter and i.get("charter_number") == charter:
            return i
        if rssd and rssd not in ("", "0") and i.get("rssdid") == rssd:
            return i
    return None


async def api_divisions(request):
    """The full per-division list for one institution (URL + business/login/provider)."""
    inst = _find_inst(request.query_params)
    if not inst:
        return JSONResponse({"error": "not found"}, status_code=404)
    out = []
    for d in (inst.get("divisions") or []):
        out.append({
            "url": d.get("url", ""),
            "serves_business": _yn(d.get("serves_business")),
            "serves_smb": _yn(d.get("serves_smb")),
            "has_business_login": _yn(d.get("has_business_login")),
            "business_login_url": d.get("business_login_url", "") or "",
            "service_provider": d.get("service_provider", "") or "",
            "reachable": d.get("reachable"),
        })
    return JSONResponse({"name": inst.get("name", ""), "count": len(out), "divisions": out})


_DIV_FIELDS = ["parent_name", "parent_type", "state", "fdic_cert", "division_url",
               "serves_business", "serves_smb", "has_business_login", "business_login_url",
               "service_provider", "reachable"]


async def api_divisions_export(request):
    """Flat ONE-ROW-PER-DIVISION export across the filtered set (csv|json)."""
    q = request.query_params
    fmt = q.get("format", "csv").lower()
    if fmt not in ("csv", "json"):
        return JSONResponse({"error": "format must be csv or json"}, status_code=400)
    # Respect the active Browse filters by matching on the institution list, then map
    # each matched institution to the raw record (which carries the divisions list).
    matched = await server.list_institutions(**_list_kwargs(q), limit=1_000_000, fields="all")
    by_cert = {i.get("cert"): i for i in get_all_institutions() if i.get("cert")}
    by_charter = {i.get("charter_number"): i for i in get_all_institutions() if i.get("charter_number")}
    rows = []
    for r in matched.get("results", []):
        inst = by_cert.get(r.get("fdic_cert")) or by_charter.get(r.get("ncua_charter"))
        if not inst:
            continue
        for d in (inst.get("divisions") or []):
            rows.append({
                "parent_name": inst.get("name", ""),
                "parent_type": "Credit Union" if inst.get("source") == "ncua" else "Bank / Thrift",
                "state": r.get("state", ""),
                "fdic_cert": inst.get("cert", ""),
                "division_url": d.get("url", ""),
                "serves_business": _yn(d.get("serves_business")),
                "serves_smb": _yn(d.get("serves_smb")),
                "has_business_login": _yn(d.get("has_business_login")),
                "business_login_url": d.get("business_login_url", "") or "",
                "service_provider": d.get("service_provider", "") or "",
                "reachable": d.get("reachable"),
            })

    tmpdir = tempfile.mkdtemp(prefix="fi_div_")
    path = os.path.join(tmpdir, f"fi_divisions.{fmt}")
    if fmt == "json":
        with open(path, "w") as f:
            json.dump({"count": len(rows), "divisions": rows}, f, indent=2)
        media = "application/json"
    else:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_DIV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        media = "text/csv"
    return FileResponse(path, media_type=media, filename=f"fi_divisions.{fmt}")


async def api_changes(request):
    q = request.query_params
    # Portal checks make outbound HTTP to third-party sites — gate them so an
    # exposed instance can't be used to fan out requests. FI_DISABLE_PORTAL_CHECKS
    # forces them off; otherwise the count is hard-capped.
    check_portals = _bool(q, "check_portals") and not DISABLE_PORTAL_CHECKS
    max_checks = min(_int(q, "max_portal_checks", 50), PORTAL_CHECK_HARD_CAP)
    result = await server.get_recent_changes(
        days=_int(q, "days", 365),
        institution_type=q.get("institution_type", "all"),
        event_type=q.get("event_type", "all"),
        state=q.get("state", ""),
        check_portals=check_portals,
        max_portal_checks=max_checks,
    )
    if DISABLE_PORTAL_CHECKS and _bool(q, "check_portals"):
        result["note"] = "Portal verification is disabled on this instance (FI_DISABLE_PORTAL_CHECKS)."
    return JSONResponse(result)


async def healthz(request):
    insts = get_all_institutions()
    return JSONResponse({"ok": bool(insts), "institutions": len(insts), "auth": bool(AUTH_USER and AUTH_PASS)})


async def api_reconcile(request):
    q = request.query_params
    result = await server.reconcile_institution(
        name=q.get("name", ""),
        city=q.get("city", ""),
        state=q.get("state", ""),
        fdic_cert=q.get("fdic_cert", ""),
        ncua_charter=q.get("ncua_charter", ""),
        rssd_id=q.get("rssd_id", ""),
        top_n=_int(q, "top_n", 5),
    )
    return JSONResponse({"results": result})


async def homepage(request):
    return HTMLResponse(INDEX_HTML)


# ---------------------------------------------------------------------------
# Single-file dashboard
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FI Explorer — fi-lookup</title>
<style>
  :root { --bg:#0f1419; --panel:#1a2029; --line:#2a323d; --text:#e6edf3; --muted:#8b98a5; --accent:#4493f8; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--text); }
  header { padding:16px 20px; border-bottom:1px solid var(--line); display:flex; align-items:baseline; gap:16px; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; }
  header .stat { color:var(--muted); font-size:12px; } header .stat b { color:var(--text); }
  nav { display:flex; gap:4px; padding:0 16px; border-bottom:1px solid var(--line); }
  nav button { background:transparent; border:0; border-bottom:2px solid transparent; color:var(--muted); padding:11px 16px; font-size:13px; cursor:pointer; }
  nav button.active { color:var(--text); border-bottom-color:var(--accent); }
  .controls { padding:14px 20px; display:flex; gap:10px 14px; flex-wrap:wrap; align-items:flex-end; border-bottom:1px solid var(--line); }
  .field { display:flex; flex-direction:column; gap:4px; }
  .field label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  input, select { background:var(--panel); color:var(--text); border:1px solid var(--line); border-radius:6px; padding:7px 9px; font-size:13px; }
  input[type=text]{ min-width:180px; } input[type=number]{ width:120px; }
  .checks { display:flex; gap:14px; align-items:center; }
  .checks label { display:flex; gap:5px; align-items:center; font-size:12px; color:var(--text); text-transform:none; letter-spacing:0; }
  button.act { background:var(--accent); color:#fff; border:0; border-radius:6px; padding:8px 14px; font-size:13px; cursor:pointer; }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--text); border-radius:6px; padding:8px 14px; font-size:13px; cursor:pointer; }
  button.act:hover, button.ghost:hover { filter:brightness(1.12); }
  .wrap { padding:0 20px 40px; }
  .panel { display:none; } .panel.active { display:block; }
  .bar { display:flex; justify-content:space-between; align-items:center; padding:12px 0; color:var(--muted); font-size:13px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); white-space:nowrap; vertical-align:top; }
  th { color:var(--muted); font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.04em; cursor:pointer; }
  th:hover { color:var(--text); }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  tr.row:hover { background:var(--panel); cursor:pointer; }
  .pill { padding:1px 7px; border-radius:999px; font-size:11px; }
  .pill.bank { background:#1f6feb33; color:#79c0ff; } .pill.cu { background:#3fb95033; color:#7ee787; }
  .pill.live { background:#3fb95033; color:#7ee787; } .pill.consumed { background:#f8514933; color:#ff7b72; }
  .pill.elsewhere { background:#d2992233; color:#e3b341; } .pill.dead { background:#6e768133; color:#8b98a5; } .pill.none { background:#30363d; color:#8b98a5; }
  .pager { display:flex; gap:8px; align-items:center; }
  .detail { position:fixed; top:0; right:0; width:min(520px,90vw); height:100%; background:var(--panel); border-left:1px solid var(--line); padding:18px; overflow:auto; transform:translateX(100%); transition:transform .18s; z-index:5; }
  .detail.open { transform:none; }
  .detail h2 { font-size:15px; margin:0 0 12px; } .detail .close { position:absolute; top:14px; right:16px; cursor:pointer; color:var(--muted); }
  .kv { display:grid; grid-template-columns:150px 1fr; gap:4px 12px; font-size:13px; margin-bottom:14px; } .kv .k { color:var(--muted); }
  .divtbl { width:100%; border-collapse:collapse; font-size:12px; }
  .divtbl th { text-align:left; color:var(--muted); font-weight:600; text-transform:uppercase; font-size:10px; letter-spacing:.04em; padding:5px 8px; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--panel); }
  .divtbl td { padding:5px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
  .divtbl td:first-child { white-space:normal; word-break:break-all; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin:14px 0; }
  .card h3 { margin:0 0 10px; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
  .summary { background:#1f6feb1a; border:1px solid #1f6feb44; border-radius:8px; padding:12px 14px; margin:14px 0; }
  .chips { display:flex; gap:8px; flex-wrap:wrap; margin:8px 0; }
  .chip { background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:5px 10px; font-size:12px; } .chip b { color:var(--text); }
  .muted { color:var(--muted); } a { color:var(--accent); }
  .hint { color:var(--muted); font-size:12px; margin-top:6px; }
  /* ── Charts (dependency-free inline SVG) ───────────────────────────────── */
  .cgrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:14px; margin:14px 0; }
  .cgrid .card { margin:0; }
  .donut { display:flex; align-items:center; gap:16px; }
  .donut svg { flex:0 0 auto; }
  .donut .legend { display:flex; flex-direction:column; gap:7px; font-size:12px; min-width:0; }
  .lg { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .lg .sw { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:7px; vertical-align:middle; }
  .lg b { color:var(--text); } .lg .pc { color:var(--muted); margin-left:4px; }
  .hbars { display:flex; flex-direction:column; gap:9px; }
  .hb { display:grid; grid-template-columns:128px 1fr 70px; align-items:center; gap:10px; font-size:12px; }
  .hb .hl { color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .hb .ht { background:var(--line); border-radius:3px; height:10px; overflow:hidden; }
  .hb .hf { display:block; height:10px; border-radius:3px; }
  .hb .hv { color:var(--muted); text-align:right; } .hb .hv b { color:var(--text); }
  .hb.clk { cursor:pointer; } .hb.clk:hover .hl { color:var(--accent); }
</style>
</head>
<body>
<header>
  <h1>🏦 FI Explorer</h1>
  <span class="stat" id="stats">loading…</span>
  <span class="stat" id="asof"></span>
</header>

<nav>
  <button data-tab="overview" class="active">Overview</button>
  <button data-tab="browse">Browse</button>
  <button data-tab="profile">Profile &amp; Lineage</button>
  <button data-tab="changes">Recent Changes</button>
  <button data-tab="reconcile">Reconcile</button>
</nav>

<!-- ============ OVERVIEW ============ -->
<section class="panel active" id="tab-overview">
<div class="wrap" id="o_out"><p class="muted">Loading overview…</p></div>
</section>

<!-- ============ BROWSE ============ -->
<section class="panel" id="tab-browse">
<div class="controls">
  <div class="field"><label>Search</label><input id="search" type="text" placeholder="name, city…" /></div>
  <div class="field"><label>Search in</label>
    <select id="search_fields"><option value="name">name</option><option value="name,city">name + city</option><option value="all">all fields</option></select></div>
  <div class="field"><label>Type</label>
    <select id="institution_type"><option value="all">All</option><option value="bank">Banks</option><option value="cu">Credit Unions</option></select></div>
  <div class="field"><label>State</label><input id="state" type="text" placeholder="UT" style="width:70px" /></div>
  <div class="field"><label>Min deposits</label><input id="min_deposit_accounts" type="number" min="0" step="1000" placeholder="0" /></div>
  <div class="field"><label>Business lending</label>
    <select id="business_lending"><option value="">any</option><option value="yes">yes</option><option value="no">no</option><option value="unknown">unknown</option></select></div>
  <div class="field"><label>Business login</label>
    <select id="business_login"><option value="">any</option><option value="yes">yes</option><option value="no">no</option><option value="unknown">unknown</option></select></div>
  <div class="field"><label>Website business</label>
    <select id="website_business"><option value="">any</option><option value="yes">yes</option><option value="no">no</option><option value="unknown">unknown</option></select></div>
  <div class="field"><label>Website small biz</label>
    <select id="website_small_business"><option value="">any</option><option value="yes">yes</option><option value="no">no</option><option value="unknown">unknown</option></select></div>
  <div class="field"><label>Service provider</label><input id="service_provider" type="text" placeholder="Jack Henry…" style="width:130px" /></div>
  <div class="field"><label>Sort by</label><select id="sort_by"></select></div>
  <div class="field"><label>Order</label><select id="sort_order"><option value="desc">desc</option><option value="asc">asc</option></select></div>
  <div class="field"><label>Filters</label>
    <div class="checks">
      <label><input type="checkbox" id="sba_lender"> SBA</label>
      <label><input type="checkbox" id="has_routing"> routing</label>
      <label><input type="checkbox" id="has_history"> lineage</label>
      <label><input type="checkbox" id="has_divisions"> divisions</label>
    </div></div>
  <div class="field"><label>&nbsp;</label><button class="act" id="apply">Search</button></div>
  <div class="field"><label>&nbsp;</label><button class="ghost" id="csv">Export CSV</button></div>
  <div class="field"><label>&nbsp;</label><button class="ghost" id="json">Export JSON</button></div>
  <div class="field"><label>&nbsp;</label><button class="ghost" id="divcsv" title="One row per branded division (entry point) across the filtered set">Export divisions ⎘</button></div>
</div>
<div class="wrap">
  <div class="bar"><span id="count">—</span>
    <span class="pager"><button class="ghost" id="prev">‹ Prev</button><span id="pageinfo">—</span>
      <button class="ghost" id="next">Next ›</button>
      <select id="limit"><option>25</option><option selected>50</option><option>100</option><option>250</option></select></span></div>
  <table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table>
</div>
</section>

<!-- ============ PROFILE & LINEAGE ============ -->
<section class="panel" id="tab-profile">
<div class="controls">
  <div class="field"><label>RSSD ID</label><input id="p_rssd" type="text" placeholder="852218" style="width:140px" /></div>
  <div class="field"><label>&nbsp;</label><button class="act" id="p_go">Look up lineage</button></div>
  <div class="field"><span class="hint">Tip: in Browse, click any row → “View lineage”.</span></div>
</div>
<div class="wrap" id="p_out"><p class="muted">Enter an RSSD ID to see merger/acquisition lineage.</p></div>
</section>

<!-- ============ RECENT CHANGES ============ -->
<section class="panel" id="tab-changes">
<div class="controls">
  <div class="field"><label>Days back</label><input id="c_days" type="number" value="365" min="1" max="3650" /></div>
  <div class="field"><label>Type</label><select id="c_type"><option value="all">All</option><option value="bank">Banks</option><option value="cu">Credit Unions</option></select></div>
  <div class="field"><label>Event</label><select id="c_event"><option value="all">All</option><option value="merger">Merger</option><option value="failure">Failure</option><option value="rebrand">Rebrand</option><option value="split">Split</option></select></div>
  <div class="field"><label>State</label><input id="c_state" type="text" placeholder="UT" style="width:70px" /></div>
  <div class="field"><label>Portal check</label><div class="checks"><label><input type="checkbox" id="c_portals"> verify portals (slower)</label></div></div>
  <div class="field"><label>&nbsp;</label><button class="act" id="c_go">Load changes</button></div>
</div>
<div class="wrap" id="c_out"><p class="muted">Load the regulatory change feed (mergers, failures, rebrands, splits).</p></div>
</section>

<!-- ============ RECONCILE ============ -->
<section class="panel" id="tab-reconcile">
<div class="controls">
  <div class="field"><label>Name</label><input id="r_name" type="text" placeholder="Mtn America FCU" /></div>
  <div class="field"><label>City</label><input id="r_city" type="text" placeholder="Sandy" style="width:140px" /></div>
  <div class="field"><label>State</label><input id="r_state" type="text" placeholder="UT" style="width:70px" /></div>
  <div class="field"><label>FDIC cert</label><input id="r_cert" type="text" style="width:100px" /></div>
  <div class="field"><label>NCUA charter</label><input id="r_charter" type="text" style="width:100px" /></div>
  <div class="field"><label>RSSD</label><input id="r_rssd" type="text" style="width:100px" /></div>
  <div class="field"><label>&nbsp;</label><button class="act" id="r_go">Find matches</button></div>
</div>
<div class="wrap" id="r_out"><p class="muted">Paste a messy institution record to get ranked candidate matches with confidence scores.</p></div>
</section>

<div class="detail" id="detail"><span class="close" id="dclose">✕ close</span><div id="dbody"></div></div>

<footer style="border-top:1px solid var(--line);padding:16px 20px;color:var(--muted);font-size:12px;line-height:1.6">
  <b>Data sources (public only):</b> FDIC BankFind · NCUA Call Reports · FFIEC NIC · SBA 7(a)/504 FOIA · institution websites.
  <b>Business signals:</b> <i>business_lending</i> = commercial loans on the call report (deterministic);
  <i>sba_lender</i> = appears in SBA 7(a)/504 lender data (FOIA);
  <i>website_business</i> / <i>website_small_business</i> = business / small-business accounts advertised on the site (scraped, best-effort);
  <i>business_login</i> = a separate business sign-in URL detected on the website (scraped, best-effort — JS-only login widgets may read as unknown).
  Lending ≠ deposit accounts; treat website signals as advertised, not guaranteed.
</footer>

<script>
const $ = id => document.getElementById(id);
const esc = s => (""+(s??"")).replace(/&/g,"&amp;").replace(/</g,"&lt;");
const fmt = v => { const n = parseInt(v,10); return isNaN(n) ? (v||"") : n.toLocaleString(); };
const typePill = t => `<span class="pill ${t==="Credit Union"?"cu":"bank"}">${t||""}</span>`;

/* ---------- tabs ---------- */
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
  b.classList.add("active"); $("tab-" + b.dataset.tab).classList.add("active");
});
function gotoTab(name){ document.querySelector(`nav button[data-tab="${name}"]`).click(); }

/* ---------- browse ---------- */
const yn = v => v==="yes"?'<span class="pill live">yes</span>':(v==="no"?'<span class="pill none">no</span>':(v==="unknown"?'<span class="muted">—</span>':esc(v)));
const COLS = [
  {k:"name",label:"Name"},{k:"type",label:"Type"},{k:"city",label:"City"},{k:"state",label:"State"},
  {k:"deposit_accounts",label:"Deposit accts",num:true},
  {k:"business_lending",label:"Business",pill:true},
  {k:"sba_lender",label:"SBA",bool:true},{k:"division_count",label:"Divs",num:true},
  {k:"website_business",label:"Web biz",pill:true},{k:"website_small_business",label:"Web SMB",pill:true},
  {k:"business_login_portal",label:"Biz login",pill:true},
  {k:"service_provider",label:"Provider"},{k:"data_as_of",label:"As of"},
];
let offset = 0;
const BCHECK = ["sba_lender","has_routing","has_history","has_divisions"];
function bparams(){
  const p = new URLSearchParams();
  p.set("search",$("search").value.trim()); p.set("search_fields",$("search_fields").value);
  p.set("institution_type",$("institution_type").value); p.set("state",$("state").value.trim());
  p.set("min_deposit_accounts",$("min_deposit_accounts").value||"0");
  ["business_lending","business_login","website_business","website_small_business"].forEach(k=>{ if($(k).value) p.set(k,$(k).value); });
  if($("service_provider").value.trim()) p.set("service_provider",$("service_provider").value.trim());
  p.set("sort_by",$("sort_by").value); p.set("sort_order",$("sort_order").value);
  BCHECK.forEach(k=>{ if($(k).checked) p.set(k,"true"); });
  return p;
}
async function bload(){
  const p = bparams(); p.set("limit",$("limit").value); p.set("offset",offset);
  const d = await (await fetch("/api/institutions?"+p)).json();
  $("head").innerHTML = COLS.map(c=>`<th data-k="${c.k}">${c.label}</th>`).join("");
  document.querySelectorAll("#head th").forEach(th=>th.onclick=()=>{
    const k=th.dataset.k;
    if($("sort_by").value===k) $("sort_order").value=$("sort_order").value==="desc"?"asc":"desc"; else $("sort_by").value=k;
    offset=0; bload();
  });
  const rows = d.results||[];
  $("body").innerHTML = rows.length ? rows.map(row=>
    `<tr class="row" data-r='${encodeURIComponent(JSON.stringify(row))}'>`+COLS.map(c=>
      c.k==="type"?`<td>${typePill(row.type)}</td>`
      :c.bool?`<td>${row[c.k]?'<span class="pill live">yes</span>':'<span class="muted">—</span>'}</td>`
      :c.pill?`<td>${yn(row[c.k])}</td>`
      :(c.num?`<td class="num">${fmt(row[c.k])}</td>`:`<td>${esc(row[c.k])}</td>`)
    ).join("")+`</tr>`).join("")
    : `<tr><td colspan="${COLS.length}" class="muted" style="padding:24px;text-align:center">No institutions match these filters.</td></tr>`;
  document.querySelectorAll("#body tr.row").forEach(tr=>tr.onclick=()=>showDetail(JSON.parse(decodeURIComponent(tr.dataset.r))));
  const total=d.total_matched||0, lim=parseInt($("limit").value,10);
  $("count").textContent=total.toLocaleString()+" matched";
  $("pageinfo").textContent=total?`${offset+1}–${Math.min(offset+lim,total)} of ${total.toLocaleString()}`:"0";
  $("prev").disabled=offset<=0; $("next").disabled=offset+lim>=total;
  syncUrl();
}
function divRows(divs){
  // Clean sub-table: each branded division as a row with its own coverage.
  return divs.map(d=>{
    const dom = (d.url||"").replace(/^https?:\/\//,"").replace(/^www\./,"").replace(/\/$/,"");
    const href = "//"+(d.url||"").replace(/^https?:\/\//,"");
    const login = (d.has_business_login==="yes" && d.business_login_url)
      ? `<a href="${esc(d.business_login_url)}" target="_blank" class="pill live">yes ↗</a>` : yn(d.has_business_login);
    return `<tr><td><a href="${esc(href)}" target="_blank">${esc(dom)}</a></td>`+
      `<td>${yn(d.serves_business)}</td><td>${yn(d.serves_smb)}</td><td>${login}</td>`+
      `<td>${esc(d.service_provider)||'<span class="muted">—</span>'}</td></tr>`;
  }).join("");
}
async function showDetail(row){
  // Pull the noisy division string fields out of the flat dump; show them as a table.
  const HIDE = new Set(["trade_name_urls","trade_names"]);
  const kv = Object.entries(row).filter(([k])=>!HIDE.has(k))
    .map(([k,v])=>`<div class="k">${esc(k)}</div><div>${esc(v)||"—"}</div>`).join("");
  const hasDivs = Number(row.division_count)>0;
  $("dbody").innerHTML = `<h2>${esc(row.name)||"Institution"}</h2>`+
    (row.rssdid?`<button class="ghost" id="d_lineage">View lineage →</button>`:"")+
    `<div class="kv" style="margin-top:14px">${kv}</div>`+
    (row.web_address?`<a href="//${row.web_address.replace(/^https?:\/\//,'')}" target="_blank">${esc(row.web_address)} ↗</a>`:"")+
    (hasDivs?`<div id="d_divs" style="margin-top:18px"><p class="muted">Loading divisions…</p></div>`:"");
  if(row.rssdid) $("d_lineage").onclick=()=>{ $("detail").classList.remove("open"); $("p_rssd").value=row.rssdid; gotoTab("profile"); ploadRssd(row.rssdid); };
  $("detail").classList.add("open");
  if(hasDivs){
    const p=new URLSearchParams({cert:row.fdic_cert||"",charter:row.ncua_charter||"",rssd:row.rssdid||""});
    try{
      const dd=await (await fetch("/api/divisions?"+p)).json();
      const divs=dd.divisions||[];
      const nbiz=divs.filter(d=>d.serves_business==="yes").length;
      const nlogin=divs.filter(d=>d.has_business_login==="yes").length;
      $("d_divs").innerHTML =
        `<h3 style="margin:0 0 4px">Divisions (${divs.length})</h3>`+
        `<p class="hint" style="margin:0 0 8px">Distinctly-branded entry points · ${nbiz} serve business · ${nlogin} have a business login</p>`+
        `<table class="divtbl"><thead><tr><th>Division</th><th>Biz</th><th>SMB</th><th>Biz login</th><th>Provider</th></tr></thead>`+
        `<tbody>${divRows(divs)}</tbody></table>`;
    }catch(e){ $("d_divs").innerHTML='<p class="muted">Could not load divisions.</p>'; }
  }
}
function bexport(f){ const p=bparams(); p.set("format",f); window.location="/api/export?"+p; }

/* ---------- profile & lineage ---------- */
function lineageTable(rows, idLabel){
  if(!rows||!rows.length) return `<p class="muted">None.</p>`;
  return `<table><thead><tr><th>Name</th><th>Event</th><th>Date</th><th>RSSD</th></tr></thead><tbody>`+
    rows.map(r=>`<tr><td>${esc(r.name)}</td><td>${esc(r.event_type||"")}</td><td>${esc(r.event_date||"")}</td><td>${esc(r.rssd_id||r.rssd||"")}</td></tr>`).join("")+
    `</tbody></table>`;
}
async function ploadRssd(rssd){
  $("p_out").innerHTML = `<p class="muted">Loading…</p>`;
  const d = await (await fetch("/api/profile?rssd="+encodeURIComponent(rssd))).json();
  if(d.error){ $("p_out").innerHTML=`<p class="muted">${esc(d.error)}</p>`; return; }
  const ids = [d.fdic_cert?`FDIC cert ${d.fdic_cert}`:null, d.ncua_charter?`NCUA charter ${d.ncua_charter}`:null, `RSSD ${d.rssd_id}`].filter(Boolean).join(" · ");
  $("p_out").innerHTML =
    `<div class="card"><h3>Institution</h3><div style="font-size:16px">${esc(d.name)} ${typePill(d.type)}</div>
      <div class="muted" style="margin-top:6px">${esc([d.city,d.state].filter(Boolean).join(", "))} · ${ids}</div></div>`+
    `<div class="summary">${esc(d.summary||"")}</div>`+
    (d.parent?`<div class="card"><h3>Parent</h3>${esc(d.parent.name)} <span class="muted">(RSSD ${esc(d.parent.rssd_id)})</span></div>`:"")+
    `<div class="card"><h3>Predecessors (absorbed into this) — ${(d.predecessors||[]).length}</h3>${lineageTable(d.predecessors)}</div>`+
    `<div class="card"><h3>Successors (what it became) — ${(d.successors||[]).length}</h3>${lineageTable(d.successors)}</div>`+
    `<div class="card"><h3>Subsidiaries — ${(d.subsidiaries||[]).length}</h3>${lineageTable(d.subsidiaries)}${d.subsidiaries_overflow?`<p class="muted">${esc(d.subsidiaries_overflow)}</p>`:""}</div>`;
}

/* ---------- recent changes ---------- */
const VERDICT = {
  independent_portal_live:["live","independent"], consumed_by_acquirer:["consumed","consumed"],
  redirects_elsewhere:["elsewhere","redirects"], unreachable:["dead","unreachable"],
  no_url_on_record:["none","no URL"], not_checked:["none","—"],
};
function portalPill(ps){ if(!ps) return ""; const v=VERDICT[ps.verdict]||["none",ps.verdict||"—"]; return `<span class="pill ${v[0]}">${v[1]}</span>`; }
function changeTable(rows){
  if(!rows||!rows.length) return "";
  return `<table><thead><tr><th>Date</th><th>Predecessor</th><th>Portal</th><th>→ Successor</th></tr></thead><tbody>`+
    rows.map(r=>`<tr><td>${esc(r.date)}</td><td>${esc(r.predecessor?.name)} <span class="muted">${esc(r.predecessor?.state||"")}</span></td>
      <td>${portalPill(r.predecessor?.portal_status)}</td><td>${esc(r.successor?.name)}</td></tr>`).join("")+`</tbody></table>`;
}
async function cload(){
  $("c_out").innerHTML = `<p class="muted">Loading${$("c_portals").checked?" (verifying portals, ~10s)…":"…"}</p>`;
  const p = new URLSearchParams({days:$("c_days").value, institution_type:$("c_type").value, event_type:$("c_event").value, state:$("c_state").value.trim()});
  if($("c_portals").checked) p.set("check_portals","true"); else p.set("check_portals","false");
  const d = await (await fetch("/api/changes?"+p)).json();
  if(d.error){ $("c_out").innerHTML=`<p class="muted">${esc(d.error)}</p>`; return; }
  const s=d.summary||{}, ps=d.portal_summary;
  let html = `<div class="chips"><span class="chip">total <b>${s.total_events||0}</b></span>
    <span class="chip">mergers <b>${s.mergers||0}</b></span><span class="chip">failures <b>${s.failures||0}</b></span>
    <span class="chip">rebrands <b>${s.rebrands||0}</b></span><span class="chip">splits <b>${s.splits||0}</b></span></div>`;
  if(ps && (ps.independent_portal_live||ps.consumed_by_acquirer||ps.redirects_elsewhere||ps.unreachable))
    html += `<div class="chips"><span class="chip">portals — independent <b>${ps.independent_portal_live}</b></span>
      <span class="chip">consumed <b>${ps.consumed_by_acquirer}</b></span><span class="chip">elsewhere <b>${ps.redirects_elsewhere}</b></span>
      <span class="chip">unreachable <b>${ps.unreachable}</b></span></div>`;
  [["Failures","failures"],["Mergers","mergers"],["Rebrands","rebrands"],["Splits","splits"],["Other","other"]].forEach(([t,k])=>{
    if((d[k]||[]).length) html += `<div class="card"><h3>${t} — ${d[k].length}</h3>${changeTable(d[k])}</div>`;
  });
  $("c_out").innerHTML = html;
}

/* ---------- reconcile ---------- */
async function rload(){
  $("r_out").innerHTML = `<p class="muted">Scoring candidates…</p>`;
  const p = new URLSearchParams({name:$("r_name").value, city:$("r_city").value, state:$("r_state").value,
    fdic_cert:$("r_cert").value, ncua_charter:$("r_charter").value, rssd_id:$("r_rssd").value, top_n:"8"});
  const d = await (await fetch("/api/reconcile?"+p)).json();
  const rows = d.results||[];
  if(!rows.length || rows[0].error || rows[0].message){ $("r_out").innerHTML=`<p class="muted">${esc(rows[0]?.error||rows[0]?.message||"No candidates.")}</p>`; return; }
  $("r_out").innerHTML = rows.map(r=>{
    const conf=Math.round((r.confidence||0)*100);
    const ids=[r.fdic_cert?`FDIC ${r.fdic_cert}`:null, r.ncua_charter?`NCUA ${r.ncua_charter}`:null, r.rssdid?`RSSD ${r.rssdid}`:null].filter(Boolean).join(" · ");
    return `<div class="card"><div style="display:flex;justify-content:space-between;align-items:center">
      <div style="font-size:15px">${esc(r.name)} ${typePill(r.type)}</div>
      <div style="font-size:18px;font-weight:600;color:${conf>=80?'#7ee787':conf>=50?'#e3b341':'#ff7b72'}">${conf}%</div></div>
      <div class="muted" style="margin:6px 0">${esc([r.city,r.state].filter(Boolean).join(", "))} · ${ids}</div>
      <div class="muted">${(r.match_reasons||[]).map(esc).join(" · ")}</div>
      ${r.rssdid?`<button class="ghost" style="margin-top:10px" data-r="${esc(r.rssdid)}">View lineage →</button>`:""}</div>`;
  }).join("");
  document.querySelectorAll("#r_out button[data-r]").forEach(b=>b.onclick=()=>{ $("p_rssd").value=b.dataset.r; gotoTab("profile"); ploadRssd(b.dataset.r); });
}

/* ---------- overview ---------- */
function bar(v,max){ const pct=max?Math.round(v/max*100):0; return `<div style="background:var(--line);border-radius:3px;height:8px;width:120px;display:inline-block;vertical-align:middle"><div style="background:var(--accent);height:8px;border-radius:3px;width:${pct}%"></div></div>`; }
const PALETTE=["#4493f8","#3fb950","#d29922","#a371f7","#db61a2","#39c5cf","#f85149","#8b98a5"];
// Inline-SVG donut. segs:[{label,value,color}]; opts.center / opts.centerSub optional.
function donut(segs,opts){ opts=opts||{};
  segs=segs.filter(s=>s.value>0);
  const total=segs.reduce((s,x)=>s+x.value,0)||1;
  const r=54,sw=22,C=2*Math.PI*r,cx=70,cy=70; let off=0;
  const arcs=segs.map(s=>{ const f=s.value/total,len=f*C,o=-off; off+=len;
    return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${s.color}" stroke-width="${sw}" stroke-dasharray="${len} ${C-len}" stroke-dashoffset="${o}" transform="rotate(-90 ${cx} ${cy})"><title>${esc(s.label)}: ${s.value.toLocaleString()} (${Math.round(f*100)}%)</title></circle>`; }).join("");
  const ctr=opts.center!=null?`<text x="${cx}" y="${cy-1}" text-anchor="middle" fill="var(--text)" font-size="19" font-weight="600">${esc(String(opts.center))}</text><text x="${cx}" y="${cy+15}" text-anchor="middle" fill="var(--muted)" font-size="10">${esc(opts.centerSub||"")}</text>`:"";
  const legend=segs.map(s=>`<div class="lg"><span class="sw" style="background:${s.color}"></span>${esc(s.label)} <b>${s.value.toLocaleString()}</b><span class="pc">${Math.round(s.value/total*100)}%</span></div>`).join("");
  return `<div class="donut"><svg viewBox="0 0 140 140" width="132" height="132">${arcs}${ctr}</svg><div class="legend">${legend}</div></div>`;
}
// Horizontal bars. rows:[{label,value,color,data}]; opts.pctOf scales the % label to a
// denominator; opts.filterKey makes rows with a `data` value click-to-filter that control.
function hbars(rows,opts){ opts=opts||{};
  const max=Math.max(1,...rows.map(r=>r.value));
  return `<div class="hbars">`+rows.map(r=>{ const pct=Math.round(r.value/max*100);
    const pof=opts.pctOf?Math.round(r.value/opts.pctOf*100):null;
    const den=pof!=null?` <span class="muted">${pof}%</span>`:"";
    const clk=(r.data!=null&&opts.filterKey)?` clk" data-filter="${esc(opts.filterKey)}" data-val="${esc(r.data)}`:"";
    const tip=`${r.label}: ${r.value.toLocaleString()}`+(pof!=null?` (${pof}%)`:"");
    return `<div class="hb${clk}" title="${esc(tip)}"><span class="hl">${esc(r.label)}</span><span class="ht"><span class="hf" style="width:${pct}%;background:${r.color||'var(--accent)'}"></span></span><span class="hv"><b>${r.value.toLocaleString()}</b>${den}</span></div>`;
  }).join("")+`</div>`;
}
async function oload(){
  let d; try{ d = await (await fetch("/api/overview?top_n=12")).json(); }
  catch(e){ $("o_out").innerHTML=`<p class="muted">Could not load overview.</p>`; return; }
  const cov=d.coverage||{}, t=d.top||{}, results=t.results||[];
  const maxv = results.length?results[0].deposit_accounts:0;
  const scanned = cov.scanned||0, cm = d.connection_methods||{};
  const compSegs=[
    {label:"Banks", value:d.banks||0, color:"#4493f8"},
    {label:"Credit unions", value:d.credit_unions||0, color:"#a371f7"},
  ];
  const connSegs=[
    {label:"API / OAuth", value:cm.api_oauth||0, color:"#3fb950"},
    {label:"Credential",  value:cm.credential||0, color:"#d29922"},
    {label:"Unknown",     value:cm.unknown||0,    color:"#8b98a5"},
  ];
  const apiPct=Math.round((cm.api_oauth||0)/(d.total||1)*100);
  const covRows=[
    {label:"Business lending",  value:cov.business_lending_yes||0,   color:"#4493f8"},
    {label:"SBA 7(a)/504",      value:cov.sba_lender||0,             color:"#3fb950"},
    {label:"Website business",  value:cov.website_business||0,       color:"#d29922"},
    {label:"Website small biz", value:cov.website_small_business||0, color:"#db61a2"},
    {label:"Business login",    value:cov.business_login||0,         color:"#a371f7"},
  ];
  const provAll=d.providers||[], provRows=provAll.slice(0,12).map(([p,n])=>({label:p,value:n,data:p}));
  const stAll=d.states||[], stRows=stAll.slice(0,12).map(([s,n])=>({label:s,value:n,data:s}));
  $("o_out").innerHTML =
    `<div class="chips" style="margin-top:14px">
      <span class="chip">total <b>${d.total.toLocaleString()}</b></span>
      <span class="chip muted">websites scanned <b>${scanned.toLocaleString()}</b></span>
      <span class="chip muted">as of <b>${esc((d.data_as_of&&d.data_as_of.fdic)||"")}</b></span></div>
    <div class="cgrid">
      <div class="card"><h3>Composition</h3>${donut(compSegs,{center:d.total.toLocaleString(),centerSub:"total"})}</div>
      <div class="card"><h3>Likely connection method</h3>${donut(connSegs,{center:apiPct+"%",centerSub:"API-ready"})}
        <p class="hint">Heuristic, from provider OAuth capability — directional, not authoritative.</p></div>
      <div class="card"><h3>Business coverage <span class="muted">(% of universe)</span></h3>${hbars(covRows,{pctOf:d.total})}</div>
    </div>`+
    (provAll.length?`<div class="card"><h3>Top digital-banking service providers${provAll.length>12?` <span class="muted">(top 12 of ${provAll.length})</span>`:""}</h3>`+
      hbars(provRows,{filterKey:"service_provider"})+
      `<p class="hint">Detected from login-host fingerprints + HTML asset / &quot;powered by&quot; markers (white-label platforms like Q2, Alkami, Banno are included). Click a bar to filter Browse. Institutions without a bar use a self-hosted or JS-rendered login that static scraping can&#39;t fingerprint.</p></div>`:"")+
    (stAll.length?`<div class="card"><h3>Institutions by state${stAll.length>12?` <span class="muted">(top 12 of ${stAll.length})</span>`:""}</h3>`+
      hbars(stRows,{filterKey:"state",pctOf:d.total})+
      `<p class="hint">Headquarters state (2-letter USPS code). Click a bar to filter Browse.</p></div>`:"")+
    `<div class="card"><h3>Top institutions by deposit accounts${t.ranked_by?` · ${(t.top_n_market_share_pct||0)}% of universe`:""}</h3>
      <table><thead><tr><th>#</th><th>Name</th><th>Type</th><th class="num">Deposit accts</th><th>Share</th><th class="num">Mkt %</th></tr></thead><tbody>`+
      results.map(r=>`<tr><td>${r.rank}</td><td>${esc(r.name)}</td><td>${typePill(r.type)}</td>
        <td class="num">${(r.deposit_accounts||0).toLocaleString()}</td><td>${bar(r.deposit_accounts,maxv)}</td>
        <td class="num">${r.market_share_pct||0}%</td></tr>`).join("")+
      `</tbody></table></div>`+
    `<p class="hint">Tip: open <a href="#" id="o_to_login">Browse → Business login = yes</a> to see institutions with a separate business sign-in (multiple aggregation entry points).</p>`;
  const lnk=$("o_to_login"); if(lnk) lnk.onclick=(e)=>{e.preventDefault(); $("business_login").value="yes"; gotoTab("browse"); offset=0; bload();};
  document.querySelectorAll("#o_out .hb[data-filter]").forEach(ch=>ch.onclick=()=>{
    const tgt=$(ch.dataset.filter); if(tgt){ tgt.value=ch.dataset.val; gotoTab("browse"); offset=0; bload(); }
  });
}

/* ---------- shareable URL state (tab + browse filters) ---------- */
let restoring=false;
function syncUrl(){
  if(restoring) return;
  const active=document.querySelector("nav button.active").dataset.tab;
  const p=bparams(); p.set("tab",active);
  history.replaceState(null,"","?"+p.toString());
}
function restoreUrl(){
  const p=new URLSearchParams(location.search);
  if(![...p.keys()].length) return null;
  restoring=true;
  ["search","state","min_deposit_accounts","business_lending","business_login","website_business","website_small_business","service_provider","sort_by","sort_order","search_fields","institution_type"].forEach(k=>{ if(p.has(k)&&$(k)) $(k).value=p.get(k); });
  ["sba_lender","has_routing","has_history","has_divisions"].forEach(k=>{ if($(k)) $(k).checked=p.get(k)==="true"; });
  restoring=false;
  return p.get("tab");
}

/* ---------- init ---------- */
async function init(){
  let m; try{ m = await (await fetch("/api/meta")).json(); }
  catch(e){ document.body.insertAdjacentHTML("afterbegin",'<p style="color:#ff7b72;padding:16px">Server not reachable — is web_app.py running?</p>'); return; }
  $("stats").innerHTML = `<b>${m.total.toLocaleString()}</b> institutions · <b>${m.banks.toLocaleString()}</b> banks · <b>${m.credit_unions.toLocaleString()}</b> credit unions`;
  $("asof").innerHTML = `data as of — FDIC ${m.data_as_of.fdic||"?"} · NCUA ${m.data_as_of.ncua||"?"} · FFIEC ${m.data_as_of.ffiec||"?"}`;
  $("sort_by").innerHTML = m.fields.map(f=>`<option ${f==="deposit_accounts"?"selected":""}>${f}</option>`).join("");
  $("apply").onclick=()=>{offset=0;bload();};
  $("search").addEventListener("keydown",e=>{if(e.key==="Enter"){offset=0;bload();}});
  $("prev").onclick=()=>{offset=Math.max(0,offset-parseInt($("limit").value,10));bload();};
  $("next").onclick=()=>{offset+=parseInt($("limit").value,10);bload();};
  $("limit").onchange=()=>{offset=0;bload();};
  $("csv").onclick=()=>bexport("csv"); $("json").onclick=()=>bexport("json");
  $("divcsv").onclick=()=>{ const p=bparams(); p.set("format","csv"); window.location="/api/divisions/export?"+p; };
  $("dclose").onclick=()=>$("detail").classList.remove("open");
  $("p_go").onclick=()=>ploadRssd($("p_rssd").value.trim());
  $("p_rssd").addEventListener("keydown",e=>{if(e.key==="Enter")ploadRssd($("p_rssd").value.trim());});
  $("c_go").onclick=cload;
  $("r_go").onclick=rload;
  $("r_name").addEventListener("keydown",e=>{if(e.key==="Enter")rload();});

  // Tab clicks lazy-load + update URL
  document.querySelectorAll("nav button").forEach(b=>b.addEventListener("click",()=>{
    const t=b.dataset.tab;
    if(t==="overview") oload(); else if(t==="browse") bload();
    syncUrl();
  }));

  const tab=restoreUrl();
  if(tab && tab!=="overview"){ gotoTab(tab); }   // gotoTab triggers the click handler (loads + syncs)
  else { oload(); }
}
init();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app):
    await build_snapshot()  # warm start — reads cache, no network on warm
    yield


app = Starlette(
    lifespan=lifespan,
    middleware=[Middleware(GuardMiddleware)],
    routes=[
        Route("/", homepage),
        Route("/healthz", healthz),
        Route("/api/meta", api_meta),
        Route("/api/institutions", api_institutions),
        Route("/api/export", api_export),
        Route("/api/profile", api_profile),
        Route("/api/divisions", api_divisions),
        Route("/api/divisions/export", api_divisions_export),
        Route("/api/changes", api_changes),
        Route("/api/reconcile", api_reconcile),
        Route("/api/overview", api_overview),
    ],
)


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="FI Explorer local web dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    auth = "ON" if (AUTH_USER and AUTH_PASS) else "OFF"
    portals = "DISABLED" if DISABLE_PORTAL_CHECKS else f"capped at {PORTAL_CHECK_HARD_CAP}"
    print(f"[fi-explorer] http://{args.host}:{args.port}  | auth: {auth} | "
          f"rate-limit: {RATE_LIMIT_PER_MIN}/min | portal checks: {portals}", file=sys.stderr, flush=True)
    if args.host not in ("127.0.0.1", "localhost") and auth == "OFF":
        print("[fi-explorer] WARNING: bound to a non-localhost interface with NO auth. "
              "Set FI_AUTH_USER / FI_AUTH_PASS before sharing on a network.", file=sys.stderr, flush=True)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
