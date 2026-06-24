"""
data_loader.py
Handles FDIC BankFind API calls, NCUA ZIP ingestion, and FFIEC NIC enrichment.
All data is public regulatory data only.
"""

from nic_loader import load_nic_data
import httpx
import json
import csv
import sys
import re
import hashlib
import zipfile
import io
from datetime import datetime
from pathlib import Path


CACHE_DIR = Path(__file__).parent / "cache"
NCUA_CACHE_FILE = CACHE_DIR / "ncua_institutions.json"
FDIC_CACHE_FILE = CACHE_DIR / "fdic_institutions.json"
MANIFEST_FILE = CACHE_DIR / "source_manifest.json"

FDIC_BASE_URL = "https://api.fdic.gov/banks"
NCUA_DOWNLOAD_BASE = "https://ncua.gov/files/publications/analysis"


def _quarter_end_date(year: int, month: int) -> str:
    """Quarter-end date as YYYY-MM-DD for a quarter-end month (3/6/9/12)."""
    last_day = {3: 31, 6: 30, 9: 30, 12: 31}.get(month, 28)
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def recent_quarter_tags(n: int = 6) -> list[str]:
    """Return the last n quarter-end tags 'YYYY-MM' (newest first, none in the future)."""
    today = datetime.today()
    pairs = []
    for yr in (today.year, today.year - 1, today.year - 2):
        for m in (12, 9, 6, 3):
            if yr < today.year or m <= today.month:
                pairs.append((yr, m))
    pairs.sort(reverse=True)
    return [f"{yr:04d}-{m:02d}" for yr, m in pairs[:n]]


def log(msg: str):
    """Print to stderr so we don't pollute the MCP stdio JSON channel."""
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# FFIEC NIC Active Attributes
# ---------------------------------------------------------------------------

def load_ffiec_lookup() -> dict:
    """
    Parse the FFIEC NIC Active Attributes ZIP from cache.
    Returns a dict with three indexes for fast lookup:
      - by_rssd:   rssd_id (str) -> attributes dict
      - by_cert:   fdic_cert (str) -> attributes dict
      - by_ncua:   ncua_charter (str) -> attributes dict
    """
    zip_files = list(CACHE_DIR.glob("CSV_ATTRIBUTES_ACTIVE*.zip"))
    if not zip_files:
        log("WARNING: No FFIEC attributes ZIP found in cache/. Routing numbers will be unavailable.")
        return {"by_rssd": {}, "by_cert": {}, "by_ncua": {}}

    zip_path = sorted(zip_files)[-1]
    log(f"Loading FFIEC attributes from {zip_path.name}...")

    with zipfile.ZipFile(zip_path) as z:
        csv_name = next(n for n in z.namelist() if n.endswith(".CSV") or n.endswith(".csv"))
        text = z.read(csv_name).decode("utf-8", errors="replace")

    lines = text.splitlines()
    if lines[0].startswith("#"):
        lines[0] = lines[0][1:]

    reader = csv.DictReader(lines)
    by_rssd = {}
    by_cert = {}
    by_ncua = {}

    for row in reader:
        aba  = row.get("ID_ABA_PRIM", "").strip()
        url  = row.get("URL", "").strip()
        rssd = row.get("ID_RSSD", "").strip()
        cert = row.get("ID_FDIC_CERT", "").strip()
        ncua = row.get("ID_NCUA", "").strip()

        entry = {
            "aba_routing": aba if aba not in ("0", "", "0 ") else "",
            "web_address": url if url not in ("0", "", "0 ") else "",
            "rssdid": rssd,
        }

        if rssd and rssd != "0":
            by_rssd[rssd] = entry
        if cert and cert not in ("0", ""):
            by_cert[cert] = entry
        if ncua and ncua not in ("0", ""):
            by_ncua[ncua] = entry

    log(f"FFIEC lookup built: {len(by_rssd)} by RSSD, {len(by_cert)} by FDIC cert, {len(by_ncua)} by NCUA charter.")
    return {"by_rssd": by_rssd, "by_cert": by_cert, "by_ncua": by_ncua}


def ffiec_enrich(inst: dict, lookup: dict) -> dict:
    """Add ABA routing number and web address to an institution record."""
    entry = None

    rssd = inst.get("rssdid", "").strip()
    if rssd and rssd not in ("0", ""):
        entry = lookup["by_rssd"].get(rssd)

    if entry is None and inst["source"] == "fdic":
        entry = lookup["by_cert"].get(inst.get("cert", "").strip())

    if entry is None and inst["source"] == "ncua":
        entry = lookup["by_ncua"].get(inst.get("charter_number", "").strip())

    if entry:
        inst["aba_routing"] = entry.get("aba_routing", "")
        if not inst.get("web_address"):
            inst["web_address"] = entry.get("web_address", "")

    return inst


# ---------------------------------------------------------------------------
# NCUA ZIP ingestion
# ---------------------------------------------------------------------------

async def ensure_latest_ncua_zip() -> Path | None:
    """
    Make sure the most recent NCUA quarterly call-report ZIP is present in cache/.

    Probes the NCUA download site newest-quarter-first. Cheap on repeat runs: if
    the newest available quarter's ZIP is already cached, no download happens (one
    HEAD at most). Falls back to the newest local ZIP if the network is unavailable.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            for tag in recent_quarter_tags():
                local = CACHE_DIR / f"call-report-data-{tag}.zip"
                if local.exists() and local.stat().st_size > 0:
                    log(f"NCUA latest quarter {tag} already cached — no download.")
                    return local
                url = f"{NCUA_DOWNLOAD_BASE}/call-report-data-{tag}.zip"
                try:
                    head = await client.head(url)
                except Exception:
                    continue
                if head.status_code != 200:
                    continue  # quarter not published yet — try the previous one
                log(f"Downloading NCUA call report data for {tag}...")
                resp = await client.get(url)
                resp.raise_for_status()
                tmp = local.with_suffix(".zip.tmp")
                tmp.write_bytes(resp.content)
                tmp.rename(local)
                log(f"Saved {local.name} ({len(resp.content) // 1_000_000} MB).")
                return local
    except Exception as e:
        log(f"WARNING: NCUA auto-download failed ({e}); falling back to local ZIP.")

    zips = sorted(CACHE_DIR.glob("call-report-data*.zip"))
    if zips:
        log(f"Using newest local NCUA ZIP: {zips[-1].name}")
        return zips[-1]
    return None


async def fetch_ncua_institutions() -> list[dict]:
    """
    Read the latest NCUA quarterly ZIP (auto-downloaded into cache/ if needed).
    Joins FOICU.txt + FS220A.txt (deposit count) + FS220D.txt (web address).
    """
    zip_path = await ensure_latest_ncua_zip()
    if not zip_path:
        log("ERROR: No NCUA ZIP file found in cache/ and auto-download failed.")
        return []

    # Derive the reporting date from the filename (call-report-data-YYYY-MM.zip).
    m = re.search(r"call-report-data-(\d{4})-(\d{2})", zip_path.name)
    ncua_as_of = _quarter_end_date(int(m.group(1)), int(m.group(2))) if m else ""

    log(f"Reading NCUA data from {zip_path.name} (as of {ncua_as_of or 'unknown'})...")

    def _read(z, name):
        try:
            return z.read(name).decode("utf-8", errors="replace").splitlines()
        except KeyError:
            log(f"NCUA: {name} not in ZIP — related fields will be empty.")
            return []

    with zipfile.ZipFile(zip_path) as z:
        foicu_text  = _read(z, "FOICU.txt")
        fs220a_text = _read(z, "FS220A.txt")
        fs220d_text = _read(z, "FS220D.txt")
        fs220_text  = _read(z, "FS220.txt")    # Acct_400A: net member business loan balance
        fs220l_text = _read(z, "FS220L.txt")   # Acct_400A1: commercial loans to members; Acct_691B1: SBA count
        tradenames_text = _read(z, "TradeNames.txt")  # CU "doing business as" brands (names only — no URLs)

        foicu = {row["CU_NUMBER"]: row for row in csv.DictReader(foicu_text)}

        trade_names_by_cu = {}
        for row in csv.DictReader(tradenames_text):
            cu = row.get("CU_NUMBER", "")
            tn = (row.get("TradeName", "") or "").strip()
            if cu and tn:
                trade_names_by_cu.setdefault(cu, []).append(tn)

        deposit_counts = {}
        for row in csv.DictReader(fs220a_text):
            deposit_counts[row.get("CU_NUMBER", "")] = row.get("ACCT_460", "")

        web_addresses = {}
        for row in csv.DictReader(fs220d_text):
            cu_num = row.get("CU_NUMBER", "")
            url = row.get("Acct_891", "").strip()
            if url and url not in ("0", ""):
                web_addresses[cu_num] = url

        # Business-lending signal: net member business loans (FS220) and
        # commercial loans to members + SBA loans outstanding (FS220L).
        mbl_balance = {}
        for row in csv.DictReader(fs220_text):
            mbl_balance[row.get("CU_NUMBER", "")] = _to_int(row.get("ACCT_400A", ""))
        commercial_bal, sba_count = {}, {}
        for row in csv.DictReader(fs220l_text):
            cu = row.get("CU_NUMBER", "")
            commercial_bal[cu] = _to_int(row.get("ACCT_400A1", ""))
            sba_count[cu]      = _to_int(row.get("ACCT_691B1", ""))

    records = []
    for cu_num, row in foicu.items():
        active_val = row.get("ACTIVE", row.get("Quarter_Flag", "1")).strip()
        if active_val not in ("0", "1", "TRUE", "True", "true", "Y", ""):
            continue

        # Member business loans are statutorily small-business-oriented for credit
        # unions, so business lending and small-business lending coincide; SBA loans
        # outstanding are an extra small-business signal.
        commercial = max(commercial_bal.get(cu_num, 0), mbl_balance.get(cu_num, 0))
        does_business = commercial > 0 or sba_count.get(cu_num, 0) > 0

        records.append({
            "source":                 "ncua",
            "charter_number":         cu_num.strip(),
            "name":                   row.get("CU_NAME", "").strip(),
            "city":                   row.get("CITY", "").strip(),
            "state":                  row.get("STATE", "").strip(),
            "rssdid":                 row.get("RSSD", "").strip(),
            "total_assets":           "",
            "deposit_accounts":       deposit_counts.get(cu_num, ""),
            "web_address":            web_addresses.get(cu_num, ""),
            "charter_type":           row.get("CU_TYPE", "").strip(),
            "cert":                   "",
            "inst_category":          "",
            "aba_routing":            "",
            "data_as_of":             ncua_as_of,
            "business_lending":       "yes" if does_business else "no",
            "commercial_loans_000":   commercial // 1000,  # NCUA reports whole $ → $000 to match FDIC
            "trade_names":            _clean_trade_names(trade_names_by_cu.get(cu_num, []), row.get("CU_NAME", "")),
            "trade_name_urls":        [],  # NCUA publishes trade names without URLs
            "predecessors":           [],
            "successors":             [],
            "parent_rssd":            None,
            "subsidiaries":           [],
        })

    log(f"Loaded {len(records)} NCUA credit unions from ZIP.")
    return records


def save_ncua_cache(records: list[dict]):
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = NCUA_CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(records, f)
    tmp.rename(NCUA_CACHE_FILE)
    log(f"Saved {len(records)} NCUA records to cache.")


def load_ncua_cache() -> list[dict]:
    if not NCUA_CACHE_FILE.exists():
        return []
    with open(NCUA_CACHE_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# FDIC API
# ---------------------------------------------------------------------------

# FDIC trade-name fields. TE01N528..TE10N528 hold trade-name *URLs* (cap 10);
# TE01N529..TE06N529 hold trade *names* (cap 6). Two independent lists — NOT
# index-aligned, and the counts differ (Glacier: 10 URLs / 0 names; Zions: 9 / 6).
# These are the distinctly-branded division entry points (e.g. Zions Bancorporation
# -> amegybank.com, calbanktrust.com, ...) that a single charter operates under.
_FDIC_TRADE_URL_FIELDS = [f"TE{i:02d}N528" for i in range(1, 11)]
_FDIC_TRADE_NAME_FIELDS = [f"TE{i:02d}N529" for i in range(1, 7)]


def _fdic_trade_names(r: dict) -> tuple[list, list]:
    """Extract (trade_name_urls, trade_names) from a raw FDIC institution row."""
    urls = [s for f in _FDIC_TRADE_URL_FIELDS if (v := r.get(f)) and (s := str(v).strip())]
    names = [s for f in _FDIC_TRADE_NAME_FIELDS if (v := r.get(f)) and (s := str(v).strip())]
    return urls, names


# Overflow for banks whose division count exceeds FDIC's 10-URL trade-name cap.
# Keyed by FDIC cert. Only a tiny set of banks are truncated (find them with
# division_count == 10). Hand-verified from public sources (10-Ks, division pages);
# URLs confirmed live. Merged (union) with FDIC's trade_name_urls so capped banks
# still expose every distinctly-branded subsidiary an end user would connect to.
DIVISION_OVERFLOW = {
    # Glacier Bank (cert 30788) — 18 divisions per FY2025 10-K; FDIC lists 10.
    # These are the 7 missing brands + gofirstbank.com (FDIC's firstbankofwyoming.com
    # is stale/dead; this is the live URL for the same division).
    "30788": [
        "www.1stbmt.com", "www.fcbutah.com", "www.altabank.com",
        "www.collegiatepeaksbank.com", "www.foothillsbank.com",
        "www.heritagebanknevada.com", "www.gnty.com", "www.gofirstbank.com",
    ],
}


def _merge_division_overflow(cert: str, urls: list) -> list:
    """Union FDIC trade-name URLs with any curated overflow for a capped bank."""
    extra = DIVISION_OVERFLOW.get(str(cert), [])
    if not extra:
        return urls
    seen = {u.lower().removeprefix("www.").rstrip("/") for u in urls}
    return urls + [e for e in extra if e.lower().removeprefix("www.").rstrip("/") not in seen]


def _clean_trade_names(names: list, legal_name: str) -> list:
    """Dedupe trade names (case-insensitive) and drop any identical to the legal name."""
    seen, out = set(), []
    ln = (legal_name or "").strip().lower()
    for n in names:
        s = n.strip()
        k = s.lower()
        if s and k != ln and k not in seen:
            seen.add(k)
            out.append(s)
    return out


async def fetch_fdic_institutions(limit: int = 10000) -> list[dict]:
    """Pull active bank records from FDIC BankFind API."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        params = {
            "filters":    "ACTIVE:1",
            "fields":     "CERT,NAME,CITY,STNAME,FED_RSSD,ASSET,INSTCAT,SPECGRP,WEBADDR,"
                          + ",".join(_FDIC_TRADE_URL_FIELDS + _FDIC_TRADE_NAME_FIELDS),
            "limit":      limit,
            "offset":     0,
            "output":     "json",
            "sort_by":    "ASSET",
            "sort_order": "DESC",
        }
        resp = await client.get(f"{FDIC_BASE_URL}/institutions", params=params)
        resp.raise_for_status()
        raw = resp.json()
        records = [row["data"] for row in raw.get("data", [])]
        log(f"Fetched {len(records)} FDIC bank records.")
        return records


async def fetch_latest_fdic_repdte(client) -> str:
    """Discover the most recent reporting date (REPDTE, YYYYMMDD) in FDIC financials."""
    params = {
        "fields":     "REPDTE",
        "sort_by":    "REPDTE",
        "sort_order": "DESC",
        "limit":      1,
        "output":     "json",
    }
    resp = await client.get(f"{FDIC_BASE_URL}/financials", params=params)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return str(data[0]["data"]["REPDTE"]) if data else ""


def _to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


async def fetch_fdic_financials() -> tuple[dict, dict, str]:
    """
    Fetch per-bank financials from the MOST RECENT FDIC quarter (auto-discovered):
      - deposit account counts (DEPLGB + DEPSMB)
      - business-lending signal: LNCI (commercial & industrial) + LNCOMRE (commercial RE)
      - small-business loans: SZLNCI + SZLNRES (RC-C Part II, loans with original
        amounts <= $1M; this schedule is filed in the June Call Report)

    Returns (deposit_counts, business_by_cert, report_date YYYYMMDD), where
    business_by_cert[cert] = {"business_lending": "yes|no",
                              "commercial_loans_000": int}.
    """
    counts, business = {}, {}
    limit = 10000

    async def _paginate(client, params):
        offset = 0
        while True:
            resp = await client.get(f"{FDIC_BASE_URL}/financials",
                                    params={**params, "limit": limit, "offset": offset,
                                            "output": "json", "sort_by": "CERT", "sort_order": "ASC"})
            resp.raise_for_status()
            batch = resp.json().get("data", [])
            for row in batch:
                yield row["data"]
            if len(batch) < limit:
                break
            offset += limit

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        repdte = await fetch_latest_fdic_repdte(client)
        if not repdte:
            log("WARNING: could not determine latest FDIC report date — skipping financials.")
            return counts, business, ""

        log(f"Fetching FDIC financials (deposits + business lending) for REPDTE={repdte}...")
        async for d in _paginate(client, {"fields": "CERT,DEPLGB,DEPSMB,LNCI,LNCOMRE", "filters": f"REPDTE:{repdte}"}):
            cert = str(d.get("CERT", ""))
            counts[cert] = str(_to_int(d.get("DEPLGB")) + _to_int(d.get("DEPSMB")))

            commercial = _to_int(d.get("LNCI")) + _to_int(d.get("LNCOMRE"))
            biz = "yes" if commercial > 0 else "no"
            # Bank small-business lending is NOT derivable from the public financials
            # API (the "SZ*" fields are securitized loans, not small-business loans).
            # Left "unknown" here; populated separately from SBA 7(a)/504 lender data.
            business[cert] = {
                "business_lending":       biz,
                "commercial_loans_000":   commercial,
            }

    log(f"Fetched financials for {len(counts)} FDIC institutions (as of {repdte}).")
    return counts, business, repdte


def save_fdic_cache(records: list[dict]):
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = FDIC_CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(records, f)
    tmp.rename(FDIC_CACHE_FILE)
    log(f"Saved {len(records)} FDIC records to cache.")


def load_fdic_cache() -> list[dict]:
    if not FDIC_CACHE_FILE.exists():
        return []
    with open(FDIC_CACHE_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Unified in-memory snapshot
# ---------------------------------------------------------------------------

_INSTITUTIONS: list[dict] = []
_NIC_NAMES: dict = {}
_DATA_AS_OF: dict = {"fdic": "", "ncua": "", "ffiec": ""}


def get_all_institutions() -> list[dict]:
    """Return the in-memory snapshot. Call build_snapshot() first."""
    return _INSTITUTIONS


def get_nic_names() -> dict:
    """Return the NIC name lookup dict. Keyed by RSSD ID string."""
    return _NIC_NAMES


def get_data_as_of() -> dict:
    """Reporting dates of the current snapshot, per source (YYYY-MM-DD)."""
    return _DATA_AS_OF


async def build_snapshot(force_refresh: bool = False):
    """Build the in-memory institution list from cache (fetch if missing)."""
    global _INSTITUTIONS, _NIC_NAMES

    fdic_raw     = load_fdic_cache()
    ncua_records = load_ncua_cache()

    if not fdic_raw or force_refresh:
        log("FDIC cache empty — fetching from API...")
        fdic_raw = await fetch_fdic_institutions()
        save_fdic_cache(fdic_raw)
        fdic_raw = load_fdic_cache()

    if not ncua_records or force_refresh:
        log("NCUA cache empty — reading from ZIP...")
        ncua_records = await fetch_ncua_institutions()

    ffiec = load_ffiec_lookup()

    needs_normalization = any(r.get("source") != "fdic" for r in fdic_raw)
    if force_refresh or needs_normalization:
        fdic_deposit_counts, fdic_business, fdic_as_of = await fetch_fdic_financials()
    else:
        fdic_deposit_counts, fdic_business, fdic_as_of = {}, {}, ""
        log("FDIC cache already normalized — skipping financials fetch.")

    # Normalize FDIC records
    fdic_normalized = []
    for r in fdic_raw:
        if r.get("source") == "fdic":
            fdic_normalized.append(r)
            continue
        cert = str(r.get("CERT", ""))
        deposit_accounts = fdic_deposit_counts.get(cert, "")
        biz = fdic_business.get(cert, {})
        trade_name_urls, trade_names = _fdic_trade_names(r)
        trade_name_urls = _merge_division_overflow(cert, trade_name_urls)
        inst = {
            "source":                 "fdic",
            "cert":                   cert,
            "name":                   r.get("NAME", ""),
            "city":                   r.get("CITY", ""),
            "state":                  r.get("STNAME", ""),
            "rssdid":                 str(r.get("FED_RSSD", "") or ""),
            "total_assets":           str(r.get("ASSET", "")),
            "deposit_accounts":       deposit_accounts,
            "web_address":            r.get("WEBADDR", "") or "",
            "charter_number":         "",
            "inst_category":          str(r.get("INSTCAT", "")),
            "aba_routing":            "",
            "data_as_of":             f"{fdic_as_of[:4]}-{fdic_as_of[4:6]}-{fdic_as_of[6:8]}" if len(fdic_as_of) == 8 else "",
            "business_lending":       biz.get("business_lending", "unknown"),
            "commercial_loans_000":   biz.get("commercial_loans_000", 0),
            "trade_names":            trade_names,
            "trade_name_urls":        trade_name_urls,
            "predecessors":           [],
            "successors":             [],
            "parent_rssd":            None,
            "subsidiaries":           [],
        }
        inst = ffiec_enrich(inst, ffiec)
        fdic_normalized.append(inst)

    # Enrich NCUA records
    ncua_enriched = []
    for inst in ncua_records:
        inst = ffiec_enrich(inst, ffiec)
        ncua_enriched.append(inst)

    all_institutions = fdic_normalized + ncua_enriched

    # ── NIC Transformations & Relationships ──────────────────────────────────
    log("[NIC] Loading FFIEC NIC transformation and relationship data...")
    nic_transformations, nic_relationships, nic_names = load_nic_data(CACHE_DIR)

    # Always load NIC names into memory (needed for name resolution even on warm start)
    _NIC_NAMES = nic_names

    if nic_transformations or nic_relationships:
        enriched = 0
        for inst in all_institutions:
            rssd = inst.get("rssdid", "").strip()
            if not rssd or rssd == "0":
                continue

            trans_data = nic_transformations.get(rssd, {})
            inst["predecessors"] = trans_data.get("as_successor", [])
            inst["successors"]   = trans_data.get("as_predecessor", [])

            rel_data = nic_relationships.get(rssd, {})
            inst["parent_rssd"]  = rel_data.get("parent_rssd")
            inst["subsidiaries"] = rel_data.get("subsidiaries", [])

            if trans_data or rel_data:
                enriched += 1

        log(f"[NIC] Enriched {enriched:,} institutions with NIC history/relationship data.")
    else:
        log("[NIC] No NIC data loaded — history fields will be empty.")
    # ── End NIC block ────────────────────────────────────────────────────────

    # ── SBA small-business lender flags (from cache/sba_lenders.json) ─────────
    # The heavy SBA download is a separate occasional job (refresh_sba.py); here
    # we just apply the cached index, flagging each institution that appears as an
    # SBA 7(a)/504 lender.
    from sba_loader import load_sba_lenders, sba_lender_sets
    sba_index = load_sba_lenders()
    sba_certs, sba_charters = sba_lender_sets(sba_index)
    sba_hits = 0
    for inst in all_institutions:
        is_sba = (
            (inst["source"] == "fdic" and inst.get("cert", "") in sba_certs) or
            (inst["source"] == "ncua" and inst.get("charter_number", "") in sba_charters)
        )
        inst["sba_lender"] = is_sba
        if is_sba:
            sba_hits += 1
    if sba_index:
        log(f"[SBA] Flagged {sba_hits:,} institutions as SBA 7(a)/504 lenders (as of {sba_index.get('as_of','?')}).")
    else:
        log("[SBA] No SBA lender index cached — run refresh_sba.py to build it.")
    # ── End SBA block ────────────────────────────────────────────────────────

    # ── Website business-coverage flags (from cache/business_coverage.json) ──
    # Distinct from lending: whether the institution ADVERTISES business / small-
    # business accounts on its site. Populated occasionally by scrape_business_coverage.py.
    from business_classifier import enrich_institutions as _enrich_web
    web_n = _enrich_web(all_institutions)
    if web_n:
        log(f"[web] Applied advertised business-coverage flags to {web_n:,} institutions.")
    # Per-division coverage (cache built occasionally by scrape_division_coverage.py).
    from division_loader import enrich_divisions as _enrich_divs
    div_n = _enrich_divs(all_institutions)
    if div_n:
        log(f"[divisions] Attached per-division coverage to {div_n:,} institutions.")
    # ── End website block ────────────────────────────────────────────────────

    # Save AFTER NIC enrichment so JSON cache includes history fields
    save_fdic_cache([i for i in all_institutions if i["source"] == "fdic"])
    save_ncua_cache([i for i in all_institutions if i["source"] == "ncua"])

    # Record reporting dates per source (read from records so this works on warm
    # starts too, where data_as_of comes straight from the cached JSON).
    ffiec_zips = sorted(CACHE_DIR.glob("CSV_ATTRIBUTES_ACTIVE*.zip"))
    ffiec_as_of = ""
    if ffiec_zips:
        ffiec_as_of = datetime.fromtimestamp(ffiec_zips[-1].stat().st_mtime).strftime("%Y-%m-%d")
    _DATA_AS_OF["fdic"]  = next((i.get("data_as_of", "") for i in all_institutions
                                 if i["source"] == "fdic" and i.get("data_as_of")), "")
    _DATA_AS_OF["ncua"]  = next((i.get("data_as_of", "") for i in all_institutions
                                 if i["source"] == "ncua" and i.get("data_as_of")), "")
    _DATA_AS_OF["ffiec"] = ffiec_as_of

    _INSTITUTIONS = all_institutions
    log(f"Snapshot ready: {len(fdic_normalized)} banks + {len(ncua_enriched)} CUs = "
        f"{len(_INSTITUTIONS)} total. As-of — FDIC:{_DATA_AS_OF['fdic']} "
        f"NCUA:{_DATA_AS_OF['ncua']} FFIEC:{_DATA_AS_OF['ffiec']}.")
    return _INSTITUTIONS


# ---------------------------------------------------------------------------
# Change-detection guard + conditional refresh
# ---------------------------------------------------------------------------
# The expensive part of a refresh is re-parsing the ZIPs and re-running NIC
# enrichment across all institutions — identical work whether the source changed
# or not. refresh_if_changed() does cheap fingerprinting first and only rebuilds
# when something genuinely advanced. Intended for scheduled (e.g. monthly) runs.

def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _latest_available_ncua_tag(client) -> str:
    """Newest NCUA quarter tag that exists (cached locally or published upstream)."""
    for tag in recent_quarter_tags():
        local = CACHE_DIR / f"call-report-data-{tag}.zip"
        if local.exists() and local.stat().st_size > 0:
            return tag
        try:
            head = await client.head(f"{NCUA_DOWNLOAD_BASE}/call-report-data-{tag}.zip")
        except Exception:
            continue
        if head.status_code == 200:
            return tag
    return ""


async def current_source_signature() -> dict:
    """
    Cheap fingerprint of all data sources used to decide whether a rebuild is needed:
      - zip_hashes:         content hash of every ZIP in cache/ (catches FFIEC/NCUA edits)
      - fdic_latest_repdte: newest FDIC reporting quarter available (one tiny API call)
      - ncua_latest_tag:    newest NCUA quarter available (HEAD probe)
    """
    zip_hashes = {}
    for p in sorted(CACHE_DIR.glob("*.zip")):
        try:
            zip_hashes[p.name] = _file_sha256(p)
        except OSError:
            pass

    fdic_repdte, ncua_tag = "", ""
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            try:
                fdic_repdte = await fetch_latest_fdic_repdte(client)
            except Exception:
                pass
            try:
                ncua_tag = await _latest_available_ncua_tag(client)
            except Exception:
                pass
    except Exception:
        pass

    return {"zip_hashes": zip_hashes, "fdic_latest_repdte": fdic_repdte, "ncua_latest_tag": ncua_tag}


def load_manifest() -> dict:
    if not MANIFEST_FILE.exists():
        return {}
    try:
        with open(MANIFEST_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_manifest(sig: dict):
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = MANIFEST_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(sig, f, indent=2)
    tmp.rename(MANIFEST_FILE)


def _as_of_from_cache() -> dict:
    """Read per-source as-of dates straight from the JSON caches (no enrichment)."""
    out = {"fdic": "", "ncua": "", "ffiec": ""}
    try:
        out["fdic"] = next((r.get("data_as_of", "") for r in load_fdic_cache() if r.get("data_as_of")), "")
    except Exception:
        pass
    try:
        out["ncua"] = next((r.get("data_as_of", "") for r in load_ncua_cache() if r.get("data_as_of")), "")
    except Exception:
        pass
    ffiec_zips = sorted(CACHE_DIR.glob("CSV_ATTRIBUTES_ACTIVE*.zip"))
    if ffiec_zips:
        out["ffiec"] = datetime.fromtimestamp(ffiec_zips[-1].stat().st_mtime).strftime("%Y-%m-%d")
    return out


async def refresh_if_changed() -> dict:
    """
    Rebuild the snapshot ONLY when an upstream source actually changed (FFIEC ZIP
    content, or a newly published FDIC/NCUA quarter). Otherwise skip the expensive
    enrichment pass entirely — including the warm load, since a no-op run (e.g. the
    monthly scheduler) just needs to confirm nothing changed and exit. Returns a
    small summary dict.
    """
    sig          = await current_source_signature()
    prev         = load_manifest()
    caches_exist = FDIC_CACHE_FILE.exists() and NCUA_CACHE_FILE.exists()

    if prev == sig and caches_exist:
        log("[refresh] Sources unchanged — skipped rebuild (no reprocessing).")
        return {"changed": False, "reason": "all sources unchanged",
                "data_as_of": _as_of_from_cache()}

    log("[refresh] Source change detected — rebuilding snapshot.")
    await build_snapshot(force_refresh=True)
    # Recompute AFTER the build: force_refresh may have downloaded a new NCUA ZIP.
    save_manifest(await current_source_signature())
    return {"changed": True, "reason": ("first run / no manifest" if not prev else "source change detected"),
            "data_as_of": dict(_DATA_AS_OF)}