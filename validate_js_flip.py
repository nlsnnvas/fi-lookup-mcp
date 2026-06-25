#!/usr/bin/env python
"""
validate_js_flip.py  —  Tier 2: re-render the audit-flagged sites and measure the flip rate.

audit_coverage.py flags institutions whose website business signal contradicts their
lending data — chiefly `recall_miss` (lends to business, site says no) and `coverage_gap`
(unreachable but lends/large). The leading cause is JS-rendered or bot-walled homepages
that plain-HTTP scraping can't read. This harness renders exactly that flagged set with
headless Chromium (the optional js_loader tier) and reports how many flip from no-signal
to a real business / login / provider signal. That flip rate IS the JS-induced error
estimate — and the render also repairs the cache (entries tagged js:true).

Heavy + optional: needs `pip install -r requirements-js.txt && python -m playwright
install chromium`. Scoped to the flagged subset and deposit-ranked, so it's a small run.

    python validate_js_flip.py                 # top 60 flagged sites
    python validate_js_flip.py --limit 150
    python validate_js_flip.py --categories recall_miss   # only one category
"""
import argparse
import asyncio
import sys

import business_classifier as bc
from audit_coverage import FLIPPABLE, classify_flags
from business_classifier import inst_key, load_coverage
from data_loader import build_snapshot, get_all_institutions


def _signal(entry: dict) -> bool:
    """Does a coverage entry carry any business/login/provider signal?"""
    return bool(entry and (entry.get("serves_business") or entry.get("serves_smb")
                           or entry.get("has_business_login") or bc.classify_provider(entry)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-render audit-flagged sites and measure flip rate")
    ap.add_argument("--limit", type=int, default=60, help="max sites to render (deposit-ranked)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--categories", nargs="*", default=sorted(FLIPPABLE),
                    help=f"which flag categories to render (default: {sorted(FLIPPABLE)})")
    args = ap.parse_args()
    want = set(args.categories)

    asyncio.run(build_snapshot())
    insts = get_all_institutions()
    flagged = [i for i in insts if want & set(classify_flags(i))]

    def dep(i):
        try:
            return int(i.get("deposit_accounts") or 0)
        except (TypeError, ValueError):
            return 0
    flagged.sort(key=dep, reverse=True)
    subset = flagged[: args.limit]
    print(f"{len(flagged)} institutions match {sorted(want)}; rendering top {len(subset)} by deposits.\n")
    if not subset:
        return

    cache = load_coverage()
    before = {inst_key(i): _signal(cache.get(inst_key(i))) for i in subset}

    try:
        from js_loader import build_js_coverage
        summary = asyncio.run(build_js_coverage(subset, limit=args.limit, concurrency=args.concurrency))
    except ImportError:
        print("Playwright not installed — this is the OPTIONAL JS tier.\n"
              "  pip install -r requirements-js.txt && python -m playwright install chromium",
              file=sys.stderr)
        sys.exit(2)

    cache = load_coverage()
    flips = []
    for i in subset:
        k = inst_key(i)
        if not before.get(k) and _signal(cache.get(k)):
            flips.append(i)

    n = len(subset)
    print(f"\n=== JS re-render result ===")
    print(f"  rendered:        {summary.get('rendered', 0)}")
    print(f"  flipped to signal: {len(flips)}/{n}  ({100*len(flips)//max(n,1)}% of flagged were JS/bot-wall errors)")
    if flips:
        print("\n  recovered (now show a business/login/provider signal):")
        for i in flips[:40]:
            e = cache.get(inst_key(i)) or {}
            sig = ("business" if e.get("serves_business") else "") + \
                  (" login" if e.get("has_business_login") else "") + \
                  (f" [{bc.classify_provider(e)}]" if bc.classify_provider(e) else "")
            print(f"    {dep(i):>11,}  {i.get('name','')[:38]:38} →{sig}")
    print("\nThe cache is updated in place; rebuild the snapshot/release to apply.")


if __name__ == "__main__":
    main()
