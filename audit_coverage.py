#!/usr/bin/env python
"""
audit_coverage.py  —  Tier 1 web-scraper accuracy audit (free, no manual labeling).

The website business signal (serves_business / has_business_login, from
business_classifier) is best-effort scraping and goes wrong in predictable ways:
JS-rendered homepages read as "no", bot walls read as "unreachable", a corporate/
global `web_address` gets scraped instead of the consumer site, and keyword noise
reads as a false "yes". We don't need hand labels to catch most of these — we already
hold a DETERMINISTIC, regulator-sourced ground truth for business activity:
`business_lending` (FDIC C&I + commercial-RE / NCUA member-business loans) and
`sba_lender`. Where the website signal contradicts the lending data, the website
signal is almost certainly wrong. This script flags those contradictions, ranked by
deposit size (impact), so the consequential ones surface first.

Categories:
  recall_miss      lends to business, but the site says serves_business = no
                   -> JS/keyword miss or wrong URL  (Fifth Third, Regions, Banco Popular, Santander)
  precision_suspect site says serves_business = yes, but no commercial/MBL lending and not SBA
                   -> keyword false-positive  (USAA)
  login_contradiction has_business_login = yes, but serves_business = no  (internal inconsistency)
  login_url_suspect business_login_url is a same-host marketing page, not an auth portal  (BECU)
  coverage_gap     site unreachable, but it lends to business / is large
                   -> bot wall; a prime target for the JS-render tier  (KeyBank, BMO, Golden 1)

    python audit_coverage.py                 # ranked summary
    python audit_coverage.py --list          # also print the top flagged institutions
    python audit_coverage.py --csv out.csv   # write every flagged row
    python audit_coverage.py --flip-candidates cache/flip_candidates.json   # feed Tier 2
    python audit_coverage.py --fail-over 250 # exit non-zero if > N flagged (CI gate)
"""
import argparse
import asyncio
import csv
import json
import sys

from business_classifier import _is_auth_portal, _reg_domain, _safe_hostname
from data_loader import build_snapshot, get_all_institutions


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _lends_to_business(i: dict) -> bool:
    return i.get("business_lending") == "yes" or i.get("sba_lender") is True


def classify_flags(i: dict) -> list[str]:
    """Every contradiction category this institution trips (usually 0 or 1)."""
    flags = []
    status = i.get("business_coverage_status", "")
    biz = i.get("serves_business")          # True / False / None(unknown)
    lends = _lends_to_business(i)

    if status == "scanned" and biz is False and lends:
        flags.append("recall_miss")
    if status == "scanned" and biz is True and not lends:
        flags.append("precision_suspect")
    if status == "scanned" and i.get("has_business_login") and biz is False:
        flags.append("login_contradiction")
    blu = i.get("business_login_url") or ""
    if status == "scanned" and i.get("has_business_login") and blu:
        home = i.get("web_address") or ""
        same_host = _reg_domain(_safe_hostname("https://" + blu.split("//")[-1])) == \
            _reg_domain(_safe_hostname("https://" + home.split("//")[-1]))
        if same_host and not _is_auth_portal(blu, home):
            flags.append("login_url_suspect")
    if status == "unreachable" and (lends or _int(i.get("deposit_accounts")) >= 50_000):
        flags.append("coverage_gap")
    return flags


CATEGORIES = ["recall_miss", "precision_suspect", "login_contradiction",
              "login_url_suspect", "coverage_gap"]
# Categories a headless-Chromium re-render (Tier 2) could plausibly flip.
FLIPPABLE = {"recall_miss", "coverage_gap"}


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit web-scraper business signal vs deterministic lending data")
    ap.add_argument("--list", action="store_true", help="print the top flagged institutions per category")
    ap.add_argument("--top", type=int, default=12, help="rows per category with --list")
    ap.add_argument("--csv", default="", help="write every flagged row to this CSV")
    ap.add_argument("--flip-candidates", default="", help="write JS-tier candidate keys to this JSON")
    ap.add_argument("--fail-over", type=int, default=0, help="exit 1 if total flagged exceeds N (0 = never)")
    args = ap.parse_args()

    asyncio.run(build_snapshot())
    insts = get_all_institutions()
    if not insts:
        print("No institutions loaded.", file=sys.stderr)
        sys.exit(1)

    rows = []
    for i in insts:
        for f in classify_flags(i):
            rows.append({
                "category": f,
                "name": i.get("name", ""),
                "type": "CU" if i.get("source") == "ncua" else "Bank",
                "state": i.get("state", ""),
                "deposit_accounts": _int(i.get("deposit_accounts")),
                "web_address": i.get("web_address", ""),
                "serves_business": i.get("serves_business"),
                "business_lending": i.get("business_lending", ""),
                "sba_lender": i.get("sba_lender"),
                "business_login_url": i.get("business_login_url", "") or "",
                "status": i.get("business_coverage_status", ""),
            })
    rows.sort(key=lambda r: r["deposit_accounts"], reverse=True)

    scanned = sum(1 for i in insts if i.get("business_coverage_status") == "scanned")
    print(f"Audited {len(insts):,} institutions ({scanned:,} scanned) against deterministic lending data.\n")
    print(f"  {'category':20} {'flagged':>8}   what it means")
    blurb = {
        "recall_miss": "lends to business but site says NO (JS/URL miss)",
        "precision_suspect": "site says YES but no commercial/MBL/SBA lending",
        "login_contradiction": "business login found but business = no",
        "login_url_suspect": "business_login_url is a marketing page, not a portal",
        "coverage_gap": "unreachable + lends/large (JS-tier target)",
    }
    for c in CATEGORIES:
        n = sum(1 for r in rows if r["category"] == c)
        print(f"  {c:20} {n:>8}   {blurb[c]}")
    print(f"\n  {'TOTAL flagged':20} {len(rows):>8}")

    if args.list:
        for c in CATEGORIES:
            crows = [r for r in rows if r["category"] == c][: args.top]
            if not crows:
                continue
            print(f"\n── {c}  (top {len(crows)} by deposits) ──")
            for r in crows:
                extra = (r["business_login_url"][:48] if c == "login_url_suspect"
                         else f"lending={r['business_lending']} sba={r['sba_lender']}")
                print(f"  {r['deposit_accounts']:>11,}  {r['type']:4} {r['state'][:2]:2}  "
                      f"{r['name'][:34]:34}  {extra}")

    if args.csv and rows:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {len(rows)} flagged rows to {args.csv}")

    if args.flip_candidates:
        flip = [{"name": r["name"], "web_address": r["web_address"], "category": r["category"],
                 "deposit_accounts": r["deposit_accounts"]}
                for r in rows if r["category"] in FLIPPABLE]
        with open(args.flip_candidates, "w", encoding="utf-8") as f:
            json.dump(flip, f, indent=2)
        print(f"Wrote {len(flip)} JS-tier flip candidates to {args.flip_candidates}")

    sys.exit(1 if (args.fail_over and len(rows) > args.fail_over) else 0)


if __name__ == "__main__":
    main()
