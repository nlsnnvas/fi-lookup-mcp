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

    with zipfile.ZipFile(zip_path) as z:
        foicu_text  = z.read("FOICU.txt").decode("utf-8", errors="replace").splitlines()
        fs220a_text = z.read("FS220A.txt").decode("utf-8", errors="replace").splitlines()
        fs220d_text = z.read("FS220D.txt").decode("utf-8", errors="replace").splitlines()

        foicu = {row["CU_NUMBER"]: row for row in csv.DictReader(foicu_text)}

        deposit_counts = {}
        for row in csv.DictReader(fs220a_text):
            deposit_counts[row.get("CU_NUMBER", "")] = row.get("ACCT_460", "")

        web_addresses = {}
        for row in csv.DictReader(fs220d_text):
            cu_num = row.get("CU_NUMBER", "")
            url = row.get("Acct_891", "").strip()
            if url and url not in ("0", ""):
                web_addresses[cu_num] = url

    records = []
    for cu_num, row in foicu.items():
        active_val = row.get("ACTIVE", row.get("Quarter_Flag", "1")).strip()
        if active_val not in ("0", "1", "TRUE", "True", "true", "Y", ""):
            continue

        records.append({
            "source":           "ncua",
            "charter_number":   cu_num.strip(),
            "name":             row.get("CU_NAME", "").strip(),
            "city":             row.get("CITY", "").strip(),
            "state":            row.get("STATE", "").strip(),
            "rssdid":           row.get("RSSD", "").strip(),
            "total_assets":     "",
            "deposit_accounts": deposit_counts.get(cu_num, ""),
            "web_address":      web_addresses.get(cu_num, ""),
            "charter_type":     row.get("CU_TYPE", "").strip(),
            "cert":             "",
            "inst_category":    "",
            "aba_routing":      "",
            "data_as_of":       ncua_as_of,
            "predecessors":     [],
            "successors":       [],
            "parent_rssd":      None,
            "subsidiaries":     [],
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

async def fetch_fdic_institutions(limit: int = 10000) -> list[dict]:
    """Pull active bank records from FDIC BankFind API."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        params = {
            "filters":    "ACTIVE:1",
            "fields":     "CERT,NAME,CITY,STNAME,FED_RSSD,ASSET,INSTCAT,SPECGRP,WEBADDR",
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


async def fetch_fdic_deposit_counts() -> tuple[dict, str]:
    """
    Fetch deposit account counts from the MOST RECENT FDIC financials quarter.
    The report date is auto-discovered (not hardcoded) so each refresh pulls the
    newest data FDIC has published.
    Returns (counts keyed by cert str -> deposit_count str, report_date YYYYMMDD).
    """
    counts = {}
    limit  = 10000
    offset = 0

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        repdte = await fetch_latest_fdic_repdte(client)
        if not repdte:
            log("WARNING: could not determine latest FDIC report date — skipping deposit counts.")
            return counts, ""
        log(f"Fetching FDIC deposit counts for most recent quarter (REPDTE={repdte})...")
        while True:
            params = {
                "fields":     "CERT,DEPLGB,DEPSMB,REPDTE",
                "filters":    f"REPDTE:{repdte}",
                "limit":      limit,
                "offset":     offset,
                "output":     "json",
                "sort_by":    "CERT",
                "sort_order": "ASC",
            }
            resp = await client.get(f"{FDIC_BASE_URL}/financials", params=params)
            resp.raise_for_status()
            raw   = resp.json()
            batch = raw.get("data", [])
            if not batch:
                break
            for row in batch:
                d      = row["data"]
                cert   = str(d.get("CERT", ""))
                deplgb = d.get("DEPLGB") or 0
                depsmb = d.get("DEPSMB") or 0
                try:
                    counts[cert] = str(int(deplgb) + int(depsmb))
                except (ValueError, TypeError):
                    counts[cert] = ""
            if len(batch) < limit:
                break
            offset += limit

    log(f"Fetched deposit counts for {len(counts)} FDIC institutions (as of {repdte}).")
    return counts, repdte


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
        fdic_deposit_counts, fdic_as_of = await fetch_fdic_deposit_counts()
    else:
        fdic_deposit_counts, fdic_as_of = {}, ""
        log("FDIC cache already normalized — skipping deposit count fetch.")

    # Normalize FDIC records
    fdic_normalized = []
    for r in fdic_raw:
        if r.get("source") == "fdic":
            fdic_normalized.append(r)
            continue
        deposit_accounts = fdic_deposit_counts.get(str(r.get("CERT", "")), "")
        inst = {
            "source":           "fdic",
            "cert":             str(r.get("CERT", "")),
            "name":             r.get("NAME", ""),
            "city":             r.get("CITY", ""),
            "state":            r.get("STNAME", ""),
            "rssdid":           str(r.get("FED_RSSD", "") or ""),
            "total_assets":     str(r.get("ASSET", "")),
            "deposit_accounts": deposit_accounts,
            "web_address":      r.get("WEBADDR", "") or "",
            "charter_number":   "",
            "inst_category":    str(r.get("INSTCAT", "")),
            "aba_routing":      "",
            "data_as_of":       f"{fdic_as_of[:4]}-{fdic_as_of[4:6]}-{fdic_as_of[6:8]}" if len(fdic_as_of) == 8 else "",
            "predecessors":     [],
            "successors":       [],
            "parent_rssd":      None,
            "subsidiaries":     [],
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