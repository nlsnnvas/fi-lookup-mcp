"""
sba_loader.py
Builds an SBA small-business-lender index from the SBA 7(a) and 504 FOIA loan
datasets, so institutions can be flagged as active small-business lenders.

- 7(a) FOIA carries `bankfdicnumber` / `bankncuanumber` → joined to our FDIC/NCUA
  records DIRECTLY by identifier (authoritative, no fuzzy matching).
- 504 FOIA carries only `thirdpartylender_name` (no identifier) → matched by
  normalized name + state against the institution list (precise, lower recall).

Heavy (downloads large CSVs) — run occasionally via refresh_sba.py, cached to
cache/sba_lenders.json. build_snapshot() reads the cache cheaply.
"""

import sys
import csv
import json
import os
import re
import tempfile
import httpx
from pathlib import Path

from reconciler import normalize_name

CACHE_DIR = Path(__file__).parent / "cache"
SBA_LENDERS_FILE = CACHE_DIR / "sba_lenders.json"

SBA_CKAN_PACKAGE = "https://data.sba.gov/api/3/action/package_show"
SBA_PACKAGE_ID = "7-a-504-foia"

# Approval fiscal years counted as "recent active" SBA lending.
RECENT_FYS = {"2023", "2024", "2025", "2026"}

# Raise the csv field-size limit — some FOIA rows have very large free-text fields.
csv.field_size_limit(10 * 1024 * 1024)

_STATE_FULL_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


def _state_abbr(state: str) -> str:
    s = (state or "").strip()
    if len(s) == 2:
        return s.upper()
    return _STATE_FULL_TO_ABBR.get(s.lower(), s.upper())


async def _resolve_sba_urls(client) -> tuple[str, str]:
    """Resolve current 7(a) FY2020-Present and 504 FY2010-Present CSV URLs from CKAN."""
    resp = await client.get(SBA_CKAN_PACKAGE, params={"id": SBA_PACKAGE_ID})
    resp.raise_for_status()
    url_7a = url_504 = ""
    for res in resp.json()["result"]["resources"]:
        name = (res.get("name") or "")
        if "7(a)" in name and "FY2020-Present" in name:
            url_7a = res["url"]
        elif "504" in name and "FY2010-Present" in name:
            url_504 = res["url"]
    return url_7a, url_504


async def _stream_to_tmp(client, url: str) -> str:
    """Stream a (large) CSV to a temp file in cache/, return its path."""
    CACHE_DIR.mkdir(exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".csv", dir=str(CACHE_DIR))
    os.close(fd)
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        with open(path, "wb") as f:
            async for chunk in resp.aiter_bytes(1 << 20):
                f.write(chunk)
    return path


def _rows(path: str):
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        yield from csv.DictReader(f)


async def build_sba_lenders(institutions: list[dict], recent_fys: set = RECENT_FYS) -> dict:
    """
    Download the SBA 7(a) and 504 FOIA datasets and build the lender index.
    Returns the index dict and writes it to cache/sba_lenders.json.
    """
    index = {
        "as_of": "", "recent_fys": sorted(recent_fys),
        "sba_7a_fdic_certs": {}, "sba_7a_ncua_charters": {},
        "sba_504_fdic_certs": [], "sba_504_ncua_charters": [],
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0), follow_redirects=True) as client:
        url_7a, url_504 = await _resolve_sba_urls(client)
        if not url_7a:
            log("SBA: could not resolve 7(a) dataset URL — aborting.")
            return index
        m = re.search(r"asof-(\d{2})(\d{2})(\d{2})", url_7a)
        if m:
            index["as_of"] = f"20{m.group(1)}-{m.group(2)}-{m.group(3)}"

        # ---- 7(a): identifier join ----
        log(f"SBA: downloading 7(a) dataset… ({url_7a.rsplit('/',1)[-1]})")
        path_7a = await _stream_to_tmp(client, url_7a)
        certs, charters = {}, {}
        n = 0
        try:
            for row in _rows(path_7a):
                n += 1
                if (row.get("approvalfy") or "").strip() not in recent_fys:
                    continue
                c = (row.get("bankfdicnumber") or "").strip()
                u = (row.get("bankncuanumber") or "").strip()
                if c and c not in ("0", "NULL"):
                    certs[c] = certs.get(c, 0) + 1
                if u and u not in ("0", "NULL"):
                    charters[u] = charters.get(u, 0) + 1
        finally:
            os.remove(path_7a)
        index["sba_7a_fdic_certs"] = certs
        index["sba_7a_ncua_charters"] = charters
        log(f"SBA 7(a): {n:,} rows; {len(certs):,} FDIC-cert + {len(charters):,} NCUA-charter lenders (FY {sorted(recent_fys)}).")

        # ---- 504: normalized-name + state match (no identifier in the data) ----
        name_idx = {}  # (normalized_name, state_abbr) -> institution
        for inst in institutions:
            key = (normalize_name(inst.get("name", "")), _state_abbr(inst.get("state", "")))
            name_idx.setdefault(key, inst)

        if url_504:
            log(f"SBA: downloading 504 dataset… ({url_504.rsplit('/',1)[-1]})")
            path_504 = await _stream_to_tmp(client, url_504)
            seen_lenders = set()
            matched_certs, matched_charters = set(), set()
            n504 = 0
            try:
                for row in _rows(path_504):
                    n504 += 1
                    if (row.get("approvalfy") or "").strip() not in recent_fys:
                        continue
                    name = (row.get("thirdpartylender_name") or "").strip()
                    if not name:
                        continue
                    key = (normalize_name(name), _state_abbr(row.get("thirdpartylender_state", "")))
                    if key in seen_lenders:
                        continue
                    seen_lenders.add(key)
                    inst = name_idx.get(key)
                    if inst:
                        if inst["source"] == "fdic" and inst.get("cert"):
                            matched_certs.add(inst["cert"])
                        elif inst["source"] == "ncua" and inst.get("charter_number"):
                            matched_charters.add(inst["charter_number"])
            finally:
                os.remove(path_504)
            index["sba_504_fdic_certs"] = sorted(matched_certs)
            index["sba_504_ncua_charters"] = sorted(matched_charters)
            log(f"SBA 504: {n504:,} rows; {len(seen_lenders):,} distinct third-party lenders → "
                f"{len(matched_certs):,} FDIC + {len(matched_charters):,} NCUA matched by name.")

    CACHE_DIR.mkdir(exist_ok=True)
    tmp = SBA_LENDERS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2)
    tmp.rename(SBA_LENDERS_FILE)
    log(f"SBA: lender index saved to {SBA_LENDERS_FILE.name} (as of {index['as_of'] or 'unknown'}).")
    return index


def load_sba_lenders() -> dict:
    """Cheap read of the cached SBA lender index (empty dict if not built yet)."""
    if not SBA_LENDERS_FILE.exists():
        return {}
    try:
        with open(SBA_LENDERS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def sba_lender_sets(index: dict) -> tuple[set, set]:
    """Return (set of SBA-lender FDIC certs, set of SBA-lender NCUA charters)."""
    if not index:
        return set(), set()
    certs = set(index.get("sba_7a_fdic_certs", {})) | set(index.get("sba_504_fdic_certs", []))
    charters = set(index.get("sba_7a_ncua_charters", {})) | set(index.get("sba_504_ncua_charters", []))
    return certs, charters
