"""
business_classifier.py
Determine business-account coverage by scraping each institution's home URL.

Regulatory data (FDIC LNCI / NCUA member-business loans) only tells you an
institution *lends* to businesses — not whether it *offers business deposit
accounts* to customers. This module gets the consumer-facing answer by fetching
the home URL (and one level of likely "Business" / "Small Business" pages) and
looking for advertised business- and SMB-account signals.

Heavy (one fetch per institution) — run as a cached, periodic enrichment, NOT
inline on every snapshot build. Results are cached in cache/business_coverage.json
keyed by a stable institution id, with the matched evidence and a checked_at date
so each flag is auditable.

Never prints to stdout (MCP stdio safety) — diagnostics go to stderr via log().
"""

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx


CACHE_DIR = Path(__file__).parent / "cache"
COVERAGE_FILE = CACHE_DIR / "business_coverage.json"

# Bump when the scrape result shape changes so cached entries get re-scanned.
SCHEMA_V = 2


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Keyword signals (matched against whitespace-normalized page text, so
# "business-checking" / "/business/" / "Business Banking" all collapse to a
# matchable "business checking" / "business" / "business banking").
# ---------------------------------------------------------------------------

# SMB is the stronger, more specific claim — checked first and implies business.
SMB_PHRASES = [
    "small business", "small business banking", "small business checking",
    "small business savings", "small business loan", "small business loans",
    "small business administration", "sba loan", "sba loans", "sba lending",
    "sba preferred lender", "sba 7a", "startup business", "for small businesses",
]

BUSINESS_PHRASES = [
    "business checking", "business savings", "business account", "business accounts",
    "business banking", "business loan", "business loans", "business line of credit",
    "business credit card", "business debit card", "business money market",
    "business cd", "business services", "business solutions", "for your business",
    "for businesses", "business owners", "commercial banking", "commercial checking",
    "commercial account", "commercial loan", "commercial lending", "treasury management",
    "merchant services", "cash management", "payroll services",
]

# Anchor hrefs/labels worth following one level deep for better recall.
LINK_KEYWORDS = ("business", "small business", "commercial", "sba", "merchant", "treasury")

# ---------------------------------------------------------------------------
# Login-portal detection (open-finance / data-aggregator signal): a separate
# BUSINESS login URL, distinct from the personal one, means an institution has
# multiple authenticated entry points an aggregator must be able to connect to.
# ---------------------------------------------------------------------------
LOGIN_HINTS = (
    "login", "log in", "log-in", "logon", "log on", "sign in", "signin", "sign-in",
    "online banking", "internet banking", "digital banking", "account access",
    "access your account", "ebanking", "e-banking", "olb",
)
LOGIN_BUSINESS_HINTS = ("business", "commercial", "treasury", "merchant", "corporate", "biz")
LOGIN_PERSONAL_HINTS = ("personal", "retail", "consumer", "individual", "member")

_LINK_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_NONWORD_RE = re.compile(r"[^a-z0-9]+")


def _normalize(html: str) -> str:
    """Strip scripts/styles, drop tags, lowercase, collapse non-alnum to spaces."""
    text = _SCRIPT_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    return _NONWORD_RE.sub(" ", text.lower())


def classify_text(html: str) -> tuple[list[str], list[str]]:
    """Return (business_evidence, smb_evidence) phrases found on the page."""
    norm = _normalize(html)
    smb = [p for p in SMB_PHRASES if p in norm]
    biz = [p for p in BUSINESS_PHRASES if p in norm]
    return sorted(set(biz)), sorted(set(smb))


def _full_url(url: str) -> str:
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def _discover_business_links(html: str, base: str, cap: int = 2) -> list[str]:
    """Find up to `cap` same-host links whose href/label hints at business pages."""
    base_host = (urlparse(base).hostname or "").lower()
    out, seen = [], set()
    for href, label in _LINK_RE.findall(html):
        hay = (href + " " + _TAG_RE.sub(" ", label)).lower()
        if not any(k in hay for k in LINK_KEYWORDS):
            continue
        target = urljoin(base, href).split("#")[0]
        p = urlparse(target)
        if p.scheme not in ("http", "https"):
            continue
        host = (p.hostname or "").lower()
        if base_host and host and base_host not in host and host not in base_host:
            continue  # stay on the institution's own domain
        if target in seen or target == base:
            continue
        seen.add(target)
        out.append(target)
        if len(out) >= cap:
            break
    return out


def _discover_logins(html: str, base: str) -> list[dict]:
    """
    Find login/sign-in links and classify each as business / personal / generic.

    Login portals frequently live on a different host (an online-banking vendor
    like q2online, netteller, telepc), so — unlike business *content* links — we
    do NOT restrict to the institution's own domain here.
    """
    found = {}  # url -> {"kind", "label"}
    for href, label in _LINK_RE.findall(html):
        lab = _TAG_RE.sub(" ", label).strip()
        hay = (href + " " + lab).lower()
        if not any(k in hay for k in LOGIN_HINTS):
            continue
        target = urljoin(base, href.strip()).split("#")[0]
        p = urlparse(target)
        if p.scheme not in ("http", "https"):
            continue
        if any(k in hay for k in LOGIN_BUSINESS_HINTS):
            kind = "business"
        elif any(k in hay for k in LOGIN_PERSONAL_HINTS):
            kind = "personal"
        else:
            kind = "generic"
        # Prefer a more specific classification if the same URL is seen twice.
        if target not in found or (found[target]["kind"] == "generic" and kind != "generic"):
            found[target] = {"url": target, "kind": kind, "label": lab[:60]}
    return list(found.values())


def _summarize_logins(logins: list[dict]) -> dict:
    """Roll login links up into the aggregator-facing signal."""
    biz = [l for l in logins if l["kind"] == "business"]
    personal = [l for l in logins if l["kind"] == "personal"]
    generic = [l for l in logins if l["kind"] == "generic"]
    biz_urls = {l["url"] for l in biz}
    other_urls = {l["url"] for l in personal + generic}
    return {
        "login_portals": logins[:10],
        "has_business_login": bool(biz),
        # A business login at a URL distinct from the personal/generic one =
        # a genuinely separate entry point for aggregation.
        "distinct_business_login": bool(biz_urls - other_urls),
        "business_login_url": (biz[0]["url"] if biz else ""),
        "personal_login_url": ((personal or generic)[0]["url"] if (personal or generic) else ""),
    }


def inst_key(inst: dict) -> str:
    """Stable cache key for an institution."""
    rssd = (inst.get("rssdid") or "").strip()
    if rssd and rssd != "0":
        return f"rssd:{rssd}"
    if inst.get("source") == "fdic" and inst.get("cert"):
        return f"cert:{inst['cert']}"
    if inst.get("source") == "ncua" and inst.get("charter_number"):
        return f"ncua:{inst['charter_number']}"
    return f"url:{(inst.get('web_address') or '').strip().lower()}"


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

async def scrape_one(client, sem, inst: dict, today: str) -> dict:
    """Fetch one institution's site and classify business/SMB coverage."""
    url = (inst.get("web_address") or "").strip()
    base_result = {
        "key": inst_key(inst),
        "name": inst.get("name", ""),
        "url": url,
        "checked_at": today,
        "v": SCHEMA_V,
        "serves_business": False,
        "serves_smb": False,
        "business_evidence": [],
        "smb_evidence": [],
        "has_business_login": False,
        "distinct_business_login": False,
        "business_login_url": "",
        "personal_login_url": "",
        "login_portals": [],
        "pages_checked": [],
        "reachable": False,
        "http_status": None,
        "note": "",
    }
    if not url:
        base_result["note"] = "no web address on record"
        return base_result

    async with sem:
        try:
            resp = await client.get(_full_url(url))
        except Exception as e:
            base_result["note"] = f"unreachable ({type(e).__name__})"
            return base_result

    base_result["reachable"] = True
    base_result["http_status"] = resp.status_code
    final = str(resp.url)
    html = resp.text or ""
    biz, smb = classify_text(html)
    biz_ev, smb_ev = set(biz), set(smb)
    logins = {l["url"]: l for l in _discover_logins(html, final)}
    pages = [final]

    # One level deep: follow up to 2 business/SMB-looking links for recall.
    for link in _discover_business_links(html, final, cap=2):
        async with sem:
            try:
                r2 = await client.get(link)
            except Exception:
                continue
        b2, s2 = classify_text(r2.text or "")
        biz_ev |= set(b2)
        smb_ev |= set(s2)
        for l in _discover_logins(r2.text or "", str(r2.url)):
            logins.setdefault(l["url"], l)
        pages.append(str(r2.url))

    if smb_ev:
        biz_ev |= {"small business"}  # SMB support implies business support
    base_result.update(
        serves_business=bool(biz_ev),
        serves_smb=bool(smb_ev),
        business_evidence=sorted(biz_ev),
        smb_evidence=sorted(smb_ev),
        pages_checked=pages,
        **_summarize_logins(list(logins.values())),
    )
    if not html.strip():
        base_result["note"] = "empty/JS-rendered page — may be unclassifiable"
    return base_result


def load_coverage() -> dict:
    if not COVERAGE_FILE.exists():
        return {}
    try:
        with open(COVERAGE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_coverage(data: dict):
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = COVERAGE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(COVERAGE_FILE)


async def build_business_coverage(
    institutions: list[dict],
    limit: int = 0,
    concurrency: int = 20,
    only_missing: bool = True,
    timeout: float = 10.0,
) -> dict:
    """
    Scrape institutions' home URLs and cache business/SMB coverage.

    Args:
        institutions: records from get_all_institutions().
        limit: max NEW institutions to scrape this run (0 = no cap). Largest
               (by deposit_accounts) are prioritized so partial runs cover the
               most consequential institutions first.
        concurrency: parallel fetches.
        only_missing: skip institutions already in the cache.
        timeout: per-request timeout (seconds).

    Returns:
        Summary dict; results merged into cache/business_coverage.json.
    """
    cache = load_coverage()
    today = datetime.today().strftime("%Y-%m-%d")

    def dep(i):
        try:
            return int(i.get("deposit_accounts") or 0)
        except (TypeError, ValueError):
            return 0

    def _norm_url(u: str) -> str:
        return (u or "").strip().lower().rstrip("/")

    def _needs_scan(i: dict) -> bool:
        # Delta-driven: scan if never scanned, or the institution's URL changed
        # since it was last scanned (products on a NEW site must be re-read).
        entry = cache.get(inst_key(i))
        if entry is None:
            return True
        if entry.get("v") != SCHEMA_V:        # result shape upgraded — re-scan
            return True
        return _norm_url(entry.get("url")) != _norm_url(i.get("web_address"))

    pool = [i for i in institutions if (i.get("web_address") or "").strip()]
    if only_missing:
        pool = [i for i in pool if _needs_scan(i)]
    pool.sort(key=dep, reverse=True)
    if limit and limit > 0:
        pool = pool[:limit]

    log(f"[business] scraping {len(pool)} institution home URLs (concurrency={concurrency})...")
    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": "Mozilla/5.0 (fi-lookup-mcp business-coverage scan)"}
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=httpx.Timeout(timeout), headers=headers, verify=False
    ) as client:
        results = await asyncio.gather(*[scrape_one(client, sem, i, today) for i in pool])

    for r in results:
        cache[r["key"]] = r
    save_coverage(cache)

    scanned = len(results)
    summary = {
        "scanned_this_run": scanned,
        "total_in_cache": len(cache),
        "serves_business": sum(1 for r in results if r["serves_business"]),
        "serves_smb": sum(1 for r in results if r["serves_smb"]),
        "unreachable": sum(1 for r in results if not r["reachable"]),
        "no_url": sum(1 for r in results if not r["url"]),
        "coverage_file": str(COVERAGE_FILE),
    }
    log(f"[business] done: {summary}")
    return summary


def enrich_institutions(institutions: list[dict]) -> int:
    """
    Merge cached business-coverage flags into in-memory records. Adds:
      serves_business (bool|None), serves_smb (bool|None),
      business_coverage_checked_at (str), business_coverage_status (str).
    None means not yet scraped. Returns the count enriched.
    """
    cache = load_coverage()
    n = 0
    for inst in institutions:
        entry = cache.get(inst_key(inst))
        if entry is None:
            inst["serves_business"] = None
            inst["serves_smb"] = None
            inst["has_business_login"] = None
            inst["distinct_business_login"] = None
            inst["business_login_url"] = ""
            inst["personal_login_url"] = ""
            inst["business_coverage_checked_at"] = ""
            inst["business_coverage_status"] = "not_scanned"
            continue
        inst["serves_business"] = entry["serves_business"]
        inst["serves_smb"] = entry["serves_smb"]
        inst["has_business_login"] = entry.get("has_business_login")
        inst["distinct_business_login"] = entry.get("distinct_business_login")
        inst["business_login_url"] = entry.get("business_login_url", "")
        inst["personal_login_url"] = entry.get("personal_login_url", "")
        inst["business_coverage_checked_at"] = entry.get("checked_at", "")
        inst["business_coverage_status"] = (
            "scanned" if entry.get("reachable") else "unreachable"
        )
        n += 1
    return n
