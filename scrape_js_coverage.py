#!/usr/bin/env python
"""
scrape_js_coverage.py — OPTIONAL heavy JS-render tier.

Renders the highest-value blocked / JavaScript-rendered institution sites with
headless Chromium (Playwright) and re-runs the normal classification, updating
cache/business_coverage.json. Scoped to a deposit-ranked subset of unreachable /
zero-signal banks — not the whole dataset.

Prerequisite (kept out of core requirements):
    pip install -r requirements-js.txt && python -m playwright install chromium

FAIL-SAFE / resumable: checkpoints every --checkpoint renders, flushes on
interrupt/error, and skips already JS-scanned entries on re-run.

    python scrape_js_coverage.py                 # top 150 by deposits
    python scrape_js_coverage.py --limit 50      # smaller batch
    python scrape_js_coverage.py --limit 0       # all candidates (slow!)
"""
import argparse
import asyncio
import sys

from data_loader import build_snapshot, get_all_institutions
from js_loader import build_js_coverage


def main() -> None:
    ap = argparse.ArgumentParser(description="Render high-value JS/blocked sites (resumable)")
    ap.add_argument("--limit", type=int, default=150, help="max sites this run (0 = all candidates)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--checkpoint", type=int, default=25)
    ap.add_argument("--all", action="store_true", help="re-render even already JS-scanned sites")
    args = ap.parse_args()

    async def run():
        await build_snapshot()
        insts = get_all_institutions()
        if not insts:
            print("No institutions loaded.", file=sys.stderr)
            sys.exit(1)
        summary = await build_js_coverage(
            insts, limit=args.limit, concurrency=args.concurrency,
            only_missing=not args.all, checkpoint_every=args.checkpoint,
        )
        print(summary)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Interrupted — progress checkpointed; re-run to resume.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
