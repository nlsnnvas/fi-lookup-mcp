"""
data_loader.py
Handles FDIC BankFind API calls, NCUA ZIP ingestion, and FFIEC NIC enrichment.
All data is public regulatory data only.
"""

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
    # Header line starts with # — strip it
    if lines[0].startswith("#"):
        lines[0] = lines[0][1:]

    reader = csv.DictReader(lines)
    by_rssd = {}
    by_cert = {}
    by_ncua = {}

    for row in reader:
        aba = row.get("ID_ABA_PRIM", "").strip()
        url = row.get("URL", "").strip()
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

    # Try RSSD first, then source-specific identifier
    rssd = inst.get("rssdid", "").strip()
    if rssd and rssd not in ("0", ""):
        entry = lookup["by_rssd"].get(rssd)

    if entry is None and inst["source"] == "fdic":
        entry = lookup["by_cert"].get(inst.get("cert", "").strip())

    if entry is None and inst["source"] == "ncua":
        entry = lookup["by_ncua"].get(inst.get("charter_number", "").strip())

    if entry:
        inst["aba_routing"] = entry.get("aba_routing", "")
        # Only overwrite web_address if we don't already have one
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
        names = z.namelist()

        # --- FOICU: core institution fields ---
        foicu_text = z.read("FOICU.txt").decode("utf-8", errors="replace").splitlines()
        foicu_reader = csv.DictReader(foicu_text)
        foicu = {row["CU_NUMBER"]: row for row in foicu_reader}

        # --- FS220A: deposit account count (Acct_460) ---
        fs220a_text = z.read("FS220A.txt").decode("utf-8", errors="replace").splitlines()
        fs220a_reader = csv.DictReader(fs220a_text)
        deposit_counts = {}
        for row in fs220a_reader:
            cu_num = row.get("CU_NUMBER", "")
            deposit_counts[cu_num] = row.get("ACCT_460", "")

        # --- FS220D: web address (Acct_891) ---
        fs220d_text = z.read("FS220D.txt").decode("utf-8", errors="replace").splitlines()
        fs220d_reader = csv.DictReader(fs220d_text)
        web_addresses = {}
        for row in fs220d_reader:
            cu_num = row.get("CU_NUMBER", "")
            url = row.get("Acct_891", "").strip()
            if url and url not in ("0", ""):
                web_addresses[cu_num] = url

    records = []
    for cu_num, row in foicu.items():
        active_val = row.get("ACTIVE", row.get("Quarter_Flag", "1")).strip()
        # Quarter_Flag=0 means active in FOICU
        if active_val not in ("0", "1", "TRUE", "True", "true", "Y", ""):
            continue

        records.append({
            "source": "ncua",
            "charter_number": cu_num.strip(),
            "name": row.get("CU_NAME", "").strip(),
            "city": row.get("CITY", "").strip(),
            "state": row.get("STATE", "").strip(),
            "rssdid": row.get("RSSD", "").strip(),
            "total_assets": "",
            "deposit_accounts": deposit_counts.get(cu_num, ""),
            "web_address": web_addresses.get(cu_num, ""),
            "charter_type": row.get("CU_TYPE", "").strip(),
            "cert": "",
            "inst_category": "",
            "aba_routing": "",  # filled by FFIEC enrichment
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
            "filters": "ACTIVE:1",
            "fields": "CERT,NAME,CITY,STNAME,FED_RSSD,ASSET,INSTCAT,SPECGRP,WEBADDR",
            "limit": limit,
            "offset": 0,
            "output": "json",
            "sort_by": "ASSET",
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
    Uses most recent quarter (REPDTE descending).
    """
    log("Fetching FDIC deposit counts from financials endpoint...")
    counts = {}
    limit = 10000
    offset = 0

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        while True:
            params = {
                "fields": "CERT,DEPLGB,DEPSMB,REPDTE",
                "filters": "REPDTE:20241231",
                "limit": limit,
                "offset": offset,
                "output": "json",
                "sort_by": "CERT",
                "sort_order": "ASC",
            }
            resp = await client.get(f"{FDIC_BASE_URL}/financials", params=params)
            resp.raise_for_status()
            raw = resp.json()
            batch = raw.get("data", [])
            if not batch:
                break
            for row in batch:
                d = row["data"]
                cert = str(d.get("CERT", ""))
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


async def build_snapshot(force_refresh: bool = False):
    """Build the in-memory institution list from cache (fetch if missing)."""
    global _INSTITUTIONS

    fdic_raw = load_fdic_cache()
    ncua_records = load_ncua_cache()

    if not fdic_raw or force_refresh:
        log("FDIC cache empty — fetching from API...")
        fdic_raw = await fetch_fdic_institutions()
        save_fdic_cache(fdic_raw)
        # Clear so normalization runs below
        fdic_raw = load_fdic_cache()

    if not ncua_records or force_refresh:
        log("NCUA cache empty — reading from ZIP...")
        ncua_records = await fetch_ncua_institutions()

    # Load FFIEC lookup and FDIC deposit counts for enrichment
    ffiec = load_ffiec_lookup()
    fdic_deposit_counts = await fetch_fdic_deposit_counts()

    # Normalize FDIC records (skip if already normalized)
    fdic_normalized = []
    for r in fdic_raw:
        if r.get("source") == "fdic":
            fdic_normalized.append(r)
            continue
        deposit_accounts = fdic_deposit_counts.get(str(r.get("CERT", "")), "")

        inst = {
            "source": "fdic",
            "cert": str(r.get("CERT", "")),
            "name": r.get("NAME", ""),
            "city": r.get("CITY", ""),
            "state": r.get("STNAME", ""),
            "rssdid": str(r.get("FED_RSSD", "") or ""),
            "total_assets": str(r.get("ASSET", "")),
            "deposit_accounts": deposit_accounts,
            "web_address": r.get("WEBADDR", "") or "",
            "charter_number": "",
            "inst_category": str(r.get("INSTCAT", "")),
            "aba_routing": "",
        }
        inst = ffiec_enrich(inst, ffiec)
        fdic_normalized.append(inst)

    # Enrich NCUA records
    ncua_enriched = []
    for inst in ncua_records:
        inst = ffiec_enrich(inst, ffiec)
        ncua_enriched.append(inst)

    # Save normalized+enriched caches
    save_fdic_cache(fdic_normalized)
    save_ncua_cache(ncua_enriched)

    _INSTITUTIONS = fdic_normalized + ncua_enriched
    log(f"Snapshot ready: {len(fdic_normalized)} banks + {len(ncua_enriched)} CUs = {len(_INSTITUTIONS)} total.")
    return _INSTITUTIONS


def get_all_institutions() -> list[dict]:
    """Return the in-memory snapshot. Call build_snapshot() first."""
    return _INSTITUTIONS
