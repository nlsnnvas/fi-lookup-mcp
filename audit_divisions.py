#!/usr/bin/env python
"""
audit_divisions.py

Stress-test EVERY division (and its redirect target) against the quality rules, so
junk in FDIC's trade-name fields can't slip through silently. Flags, per division:
  - social   : a social-media / non-bank domain (facebook, x, linkedin, ...)
  - dup_parent: same URL as the parent's own home (a duplicate record)
  - login    : the URL, or its redirect target, is a login/auth portal
  - redirect_parent: redirects to the parent's own home domain
  - error    : the scraped page is an error/stub/parked page
  - unreachable: the scraper couldn't reach it (curated additions are exempt)

These are exactly what enrich_divisions filters, so a clean run (all zero, bar the
curated exceptions) confirms the filters are holding. Re-run after any data refresh.

    python audit_divisions.py            # summary
    python audit_divisions.py --list     # also print every flagged URL
"""
import argparse
import asyncio
import sys
from urllib.parse import urlparse

from data_loader import build_snapshot, get_all_institutions, _SOCIAL_DOMAINS, _reg_domain
import division_loader as dl
from division_loader import DIVISION_ADDITIONS


def _host(u: str) -> str:
    try:
        return (urlparse(u if u.startswith("http") else "https://" + u).hostname or "").lower()
    except ValueError:
        return ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit all divisions for junk URLs")
    ap.add_argument("--list", action="store_true", help="print each flagged URL")
    args = ap.parse_args()

    asyncio.run(build_snapshot())
    insts = get_all_institutions()
    cache = dl.load_division_coverage()
    curated = {u for adds in DIVISION_ADDITIONS.values() for u in (a["url"] for a in adds)}

    divs = [(i, d) for i in insts for d in (i.get("divisions") or [])]
    flags = {k: [] for k in ("social", "dup_parent", "login", "redirect_parent", "error", "unreachable")}
    for i, d in divs:
        u = d["url"]
        if u in curated:
            continue
        e = cache.get(dl._key(u)) or {}
        final = (e.get("pages_checked") or [u])[0]
        fh, pweb = _host(final), i.get("web_address", "")
        preg, phost = dl._reg(_host(pweb)), dl._norm_host(_host(pweb))
        if _reg_domain(_host(u)) in _SOCIAL_DOMAINS:
            flags["social"].append(u)
        if phost and dl._norm_host(_host(u)) == phost:
            flags["dup_parent"].append(u)
        if dl._login_host(_host(u)) or dl._login_host(fh):
            flags["login"].append(u)
        if preg and dl._reg(fh) == preg and dl._reg(_host(u)) != preg:
            flags["redirect_parent"].append(u)
        if dl._DEAD_TITLE.search(e.get("title") or ""):
            flags["error"].append(u)
        if d.get("reachable") is False:
            flags["unreachable"].append(u)

    n_parents = sum(1 for i in insts if i.get("divisions"))
    print(f"Audited {len(divs)} divisions across {n_parents} parents "
          f"(excluding {len(curated)} curated):\n")
    total = 0
    for k, v in flags.items():
        total += len(v)
        print(f"  {k:16}: {len(v)}" + ("  " + ", ".join(v[:5]) if (args.list and v) else ""))
    print("\n" + ("CLEAN ✓ — no junk URLs slipped through." if total == 0
                  else f"{total} flagged — tighten the filters in data_loader/division_loader."))
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
