#!/usr/bin/env python
"""
scrape_division_coverage.py

Occasional (heavy) job: scrape each distinctly-branded division URL (trade_name_urls)
for its own business/SMB/login/provider coverage. Results cache to
cache/division_coverage.json and merge into the snapshot via enrich_divisions().

FAIL-SAFE / resumable by design:
  - checkpoints the cache to disk every --checkpoint scrapes (default 50),
  - always flushes whatever completed on interrupt/error (finally block),
  - --only-missing (default) skips URLs already in the cache, so re-running after
    a crash, Ctrl-C, sleep, or network blip RESUMES where it left off — no lost work.

    python scrape_division_coverage.py                 # full run / resume
    python scrape_division_coverage.py --limit 50      # cap this run (still resumable)
    python scrape_division_coverage.py --all           # re-scan everything (ignore cache)
"""
import argparse
import asyncio
import sys

from data_loader import build_snapshot, get_all_institutions
from division_loader import build_division_coverage


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape per-division business coverage (resumable)")
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--checkpoint", type=int, default=50, help="flush cache every N scrapes")
    ap.add_argument("--limit", type=int, default=0, help="cap division URLs this run (0 = all)")
    ap.add_argument("--all", action="store_true", help="re-scan even cached URLs")
    args = ap.parse_args()

    async def run():
        await build_snapshot()
        insts = get_all_institutions()
        if not insts:
            print("No institutions loaded.", file=sys.stderr)
            sys.exit(1)
        if args.limit:
            # Prioritize the largest institutions' divisions when capping a run.
            insts = sorted(insts, key=lambda i: len(i.get("trade_name_urls") or []), reverse=True)
        summary = await build_division_coverage(
            insts, concurrency=args.concurrency, timeout=args.timeout,
            only_missing=not args.all, checkpoint_every=args.checkpoint, limit=args.limit,
        )
        print(summary)

    # The finally block in build_division_coverage flushes on any exit, but guard the
    # top level too so a Ctrl-C still exits cleanly with progress saved.
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Interrupted — progress was checkpointed; re-run to resume.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
