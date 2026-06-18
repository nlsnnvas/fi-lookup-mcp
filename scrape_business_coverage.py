#!/usr/bin/env python
"""
scrape_business_coverage.py
Cached enrichment runner: scrape institution home URLs for business / SMB
account support and store the results in cache/business_coverage.json.

Heavy (one fetch per institution) — run periodically, not on every refresh.
Resumable: by default only scrapes institutions not already cached, so you can
chip away in capped batches.

    python scrape_business_coverage.py --limit 200      # top 200 uncached (by size)
    python scrape_business_coverage.py --limit 0        # everything uncached
    python scrape_business_coverage.py --rescan         # re-scan all (ignore cache)
    python scrape_business_coverage.py --concurrency 30

Not the MCP server — printing to stdout here is fine.
"""

import argparse
import asyncio
import json

from data_loader import build_snapshot, get_all_institutions
from business_classifier import build_business_coverage


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape business/SMB account coverage")
    ap.add_argument("--limit", type=int, default=200,
                    help="max new institutions to scrape (0 = all uncached). Default 200.")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--rescan", action="store_true",
                    help="re-scan institutions already in the cache")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args()

    asyncio.run(build_snapshot())
    summary = asyncio.run(build_business_coverage(
        get_all_institutions(),
        limit=args.limit,
        concurrency=args.concurrency,
        only_missing=not args.rescan,
        timeout=args.timeout,
    ))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
