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
from business_classifier import enrich_institutions, inst_key, load_coverage
from data_loader import build_snapshot, get_all_institutions


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-render audit-flagged sites and measure flip rate")
    ap.add_argument("--limit", type=int, default=60, help="max sites to render (deposit-ranked)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--categories", nargs="*", default=sorted(FLIPPABLE),
                    help=f"which flag categories to render (default: {sorted(FLIPPABLE)})")
    ap.add_argument("--force", action="store_true",
                    help="re-render even sites already JS-rendered (default: only never-rendered)")
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
    already_js = sum(1 for i in subset if (cache.get(inst_key(i)) or {}).get("js"))
    rendered_today = [i for i in subset if not (cache.get(inst_key(i)) or {}).get("js")] \
        if not args.force else subset
    print(f"  of these, {already_js} were already JS-rendered in a prior run "
          f"(re-render only with --force); {len(subset) - already_js} never rendered.\n")

    try:
        from js_loader import build_js_coverage
        summary = asyncio.run(build_js_coverage(
            subset, limit=args.limit, concurrency=args.concurrency,
            only_missing=not args.force, only_candidates=False))
    except ImportError:
        print("Playwright not installed — this is the OPTIONAL JS tier.\n"
              "  pip install -r requirements-js.txt && python -m playwright install chromium",
              file=sys.stderr)
        sys.exit(2)

    # "Resolved" = no longer trips its audit flag after re-enriching from the fresh cache.
    # (Tracking flag resolution, not just "any signal" — a recall_miss site that already had
    # a login only counts as fixed when serves_business actually flips.)
    enrich_institutions(rendered_today)
    resolved = [i for i in rendered_today if not (want & set(classify_flags(i)))]
    n = len(rendered_today)
    print(f"\n=== JS re-render result ===")
    print(f"  rendered:        {summary.get('rendered', 0)}")
    print(f"  resolved (flag cleared): {len(resolved)}/{n}  "
          f"({100*len(resolved)//max(n,1)}% of rendered were JS/bot-wall errors JS could fix)")
    print(f"  still flagged:           {n - len(resolved)}/{n}  "
          f"(hard bot walls, wrong corporate URL, or homepage genuinely lacks the signal)")
    if resolved:
        print("\n  recovered:")
        for i in resolved[:40]:
            sig = ("business" if i.get("serves_business") else "") + \
                  (" login" if i.get("has_business_login") else "") + \
                  (f" [{i.get('service_provider')}]" if i.get("service_provider") else "")
            print(f"    {dep(i):>11,}  {i.get('name','')[:38]:38} →{sig}")
    print("\nThe cache is updated in place; rebuild the snapshot/release to apply.")


if __name__ == "__main__":
    main()
