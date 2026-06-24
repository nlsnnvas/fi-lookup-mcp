#!/usr/bin/env python
"""
find_url_candidates.py

Surface institutions whose regulatory `web_address` is *likely* the corporate /
holding-company site rather than the consumer-banking brand (e.g. jpmorganchase.com
instead of chase.com) — which skews the website business-coverage scrape.

There is no credential-free source of "legal entity -> consumer brand domain," so
this is a **ranked review aid**, not an auto-fix: it flags the highest-impact
suspects so they can be added to `CONSUMER_DOMAIN_OVERRIDES` in business_classifier.py
by hand. Two heuristics, ranked by deposit-account size (impact):

  1. unreachable  — a large bank whose site didn't respond is a prime suspect
                    (the corporate site often blocks scrapers, as Chase does).
  2. zero-signal  — reachable, but no business / SMB / login / provider detected
                    despite a large deposit base (a big bank with nothing found).

    python find_url_candidates.py            # top 40 to stdout
    python find_url_candidates.py --top 100 --csv cache/url_candidates.csv
"""
import argparse
import asyncio
import csv
import sys

from data_loader import build_snapshot, get_all_institutions


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def candidate_reason(inst: dict) -> str:
    status = inst.get("business_coverage_status", "")
    if status == "unreachable":
        return "unreachable"
    if status == "scanned":
        no_biz = inst.get("serves_business") is not True
        no_smb = inst.get("serves_smb") is not True
        no_login = not inst.get("has_business_login")
        no_prov = not inst.get("service_provider")
        if no_biz and no_smb and no_login and no_prov:
            return "zero-signal"
    return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Rank likely corporate-URL institutions for review")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--csv", default="")
    args = ap.parse_args()

    asyncio.run(build_snapshot())
    insts = get_all_institutions()
    if not insts:
        print("No institutions loaded.", file=sys.stderr)
        sys.exit(1)

    rows = []
    for i in insts:
        reason = candidate_reason(i)
        if not reason:
            continue
        rows.append({
            "name": i.get("name", ""),
            "type": "CU" if i.get("source") == "ncua" else "Bank",
            "state": i.get("state", ""),
            "deposit_accounts": _int(i.get("deposit_accounts")),
            "web_address": i.get("web_address", ""),
            "reason": reason,
        })
    rows.sort(key=lambda r: r["deposit_accounts"], reverse=True)

    n_unreach = sum(1 for r in rows if r["reason"] == "unreachable")
    n_zero = sum(1 for r in rows if r["reason"] == "zero-signal")
    print(f"{len(rows)} candidates ({n_unreach} unreachable, {n_zero} zero-signal). "
          f"Top {min(args.top, len(rows))} by deposit accounts:\n")
    print(f"{'deposits':>10}  {'reason':12}  {'state':5}  {'web_address':32}  name")
    for r in rows[:args.top]:
        print(f"{r['deposit_accounts']:>10,}  {r['reason']:12}  {r['state'][:5]:5}  "
              f"{r['web_address'][:32]:32}  {r['name']}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
