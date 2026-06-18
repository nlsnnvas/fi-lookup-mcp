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
import zipfile
import io
from pathlib import Path


CACHE_DIR = Path(__file__).parent / "cache"
NCUA_CACHE_FILE = CACHE_DIR / "ncua_institutions.json"
FDIC_CACHE_FILE = CACHE_DIR / "fdic_institutions.json"

FDIC_BASE_URL = "https://api.fdic.gov/banks"


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

async def fetch_ncua_institutions() -> list[dict]:
    """
    Read NCUA quarterly ZIP from local cache folder.
    Joins FOICU.txt + FS220A.txt (deposit count) + FS220D.txt (web address).
    """
    zip_files = list(CACHE_DIR.glob("call-report-data*.zip"))
    if not zip_files:
        log("ERROR: No NCUA ZIP file found in cache/.")
        return []

    zip_path = sorted(zip_files)[-1]
    log(f"Reading NCUA data from {zip_path.name}...")

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


async def fetch_fdic_deposit_counts() -> dict:
    """
    Fetch deposit account counts from FDIC financials endpoint.
    Returns dict keyed by cert (str) -> deposit_count (str).
    """
    log("Fetching FDIC deposit counts from financials endpoint...")
    counts = {}
    limit  = 10000
    offset = 0

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        while True:
            params = {
                "fields":     "CERT,DEPLGB,DEPSMB,REPDTE",
                "filters":    "REPDTE:20241231",
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

    log(f"Fetched deposit counts for {len(counts)} FDIC institutions.")
    return counts


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


def get_all_institutions() -> list[dict]:
    """Return the in-memory snapshot. Call build_snapshot() first."""
    return _INSTITUTIONS


def get_nic_names() -> dict:
    """Return the NIC name lookup dict. Keyed by RSSD ID string."""
    return _NIC_NAMES


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
        fdic_deposit_counts = await fetch_fdic_deposit_counts()
    else:
        fdic_deposit_counts = {}
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

    _INSTITUTIONS = all_institutions
    log(f"Snapshot ready: {len(fdic_normalized)} banks + {len(ncua_enriched)} CUs = {len(_INSTITUTIONS)} total.")
    return _INSTITUTIONS