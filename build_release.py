#!/usr/bin/env python
"""
build_release.py
Export the derived enrichment snapshot to portable, publishable artifacts:
  - CSV     (accessible; stdlib)
  - SQLite  (queryable;  stdlib)
  - Parquet (analytical; only if pandas/pyarrow are installed)

It exports the CURATED / DERIVED view only — server._full_record() per institution:
institution metadata + business-coverage flags + provider / connection / OAuth-rail
signals. It deliberately OMITS the raw scrape artifacts (evidence phrases,
provider_hints, captured login-portal URL lists), which stay local in
cache/business_coverage.json. Public data only — see DATA.md.

These artifacts are meant to be attached to a dated GitHub Release, NOT committed
to git (releases/ is gitignored).

    python build_release.py                 # -> releases/fi-lookup-<today>.{csv,sqlite,parquet}
    python build_release.py --stamp 2026Q1  # custom label
"""

import argparse
import asyncio
import csv
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from data_loader import build_snapshot, get_all_institutions, get_data_as_of
import server


def main() -> None:
    ap = argparse.ArgumentParser(description="Export the enrichment snapshot for release")
    ap.add_argument("--outdir", default="releases")
    ap.add_argument("--stamp", default=date.today().isoformat(),
                    help="filename label (default: today's date)")
    args = ap.parse_args()

    asyncio.run(build_snapshot())
    insts = get_all_institutions()
    if not insts:
        print("No institutions loaded — build the snapshot first.", file=sys.stderr)
        sys.exit(1)

    fields = server._LIST_FIELDS
    records = [server._full_record(i) for i in insts]

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = outdir / f"fi-lookup-{args.stamp}"

    # ── CSV ──────────────────────────────────────────────────────────────────
    csv_path = base.with_suffix(".csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)

    # ── SQLite ───────────────────────────────────────────────────────────────
    db_path = base.with_suffix(".sqlite")
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    cols_ddl = ", ".join('"' + c + '" TEXT' for c in fields)
    con.execute(f"CREATE TABLE institutions ({cols_ddl})")
    con.executemany(
        f'INSERT INTO institutions VALUES ({",".join("?" * len(fields))})',
        [[("" if r.get(c) is None else str(r.get(c))) for c in fields] for r in records],
    )
    con.execute("CREATE INDEX idx_provider ON institutions(service_provider)")
    con.execute("CREATE INDEX idx_state ON institutions(state)")
    con.commit()
    con.close()

    # ── Parquet (optional) ───────────────────────────────────────────────────
    parquet_status = "skipped (install pandas + pyarrow to enable)"
    try:
        import pandas as pd
        pd.DataFrame(records, columns=fields).to_parquet(base.with_suffix(".parquet"), index=False)
        parquet_status = str(base.with_suffix(".parquet"))
    except Exception as e:
        parquet_status = f"skipped ({type(e).__name__})"

    # ── Manifest (provenance + counts) ───────────────────────────────────────
    manifest = {
        "stamp": args.stamp,
        "rows": len(records),
        "fields": fields,
        "data_as_of": get_data_as_of(),
        "note": "Derived enrichment snapshot. Public data only. Raw scrape artifacts excluded. See DATA.md.",
    }
    with open(base.with_suffix(".manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    print(json.dumps({
        "rows": len(records),
        "data_as_of": get_data_as_of(),
        "csv": str(csv_path),
        "sqlite": str(db_path),
        "parquet": parquet_status,
        "manifest": str(base.with_suffix(".manifest.json")),
    }, indent=2))


if __name__ == "__main__":
    main()
