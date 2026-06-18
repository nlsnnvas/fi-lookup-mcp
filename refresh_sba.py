#!/usr/bin/env python
"""
refresh_sba.py
Rebuild the SBA small-business-lender index from the SBA 7(a) + 504 FOIA datasets.

Heavy (downloads large CSVs) — run occasionally (e.g. quarterly, when SBA posts a
new 'as of' file) or whenever you want to refresh small-business coverage. Writes
cache/sba_lenders.json, which build_snapshot() then reads cheaply on every refresh.

    python refresh_sba.py
"""

import asyncio
import json
from datetime import datetime

from data_loader import build_snapshot, get_all_institutions
from sba_loader import build_sba_lenders


async def main():
    # Need the institution list (for the 504 name match); warm-load is enough.
    if not get_all_institutions():
        await build_snapshot()
    index = await build_sba_lenders(get_all_institutions())
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = {
        "as_of": index.get("as_of"),
        "recent_fys": index.get("recent_fys"),
        "sba_7a_fdic_lenders": len(index.get("sba_7a_fdic_certs", {})),
        "sba_7a_ncua_lenders": len(index.get("sba_7a_ncua_charters", {})),
        "sba_504_fdic_matched": len(index.get("sba_504_fdic_certs", [])),
        "sba_504_ncua_matched": len(index.get("sba_504_ncua_charters", [])),
    }
    print(f"[{stamp}] refresh_sba -> {json.dumps(summary)}", flush=True)
    # Rebuild the snapshot so records pick up the new SBA flags immediately.
    await build_snapshot(force_refresh=True)


if __name__ == "__main__":
    asyncio.run(main())
