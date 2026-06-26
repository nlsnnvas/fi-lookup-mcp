#!/usr/bin/env python
"""
sync_docs.py — keep the refresh-sensitive numbers in README.md / CLAUDE.md in
step with the live snapshot, so a data refresh never leaves stale figures in the
docs.

What it syncs (the values that move every quarterly refresh):
  - institution counts: active total, banks, credit unions, historical name records
  - gold-set accuracy baseline (F1 / precision / recall)

How:
  - COUNTS are distinctive comma-formatted tokens (e.g. "8,605"); we replace the
    previously-synced token (from docs_stats.json — the source of truth) with the
    live one, everywhere in the target docs. Safe because the tokens are unique.
  - The GOLD baseline lives between HTML-comment markers
    (`<!--SYNC:gold_readme-->…<!--/SYNC:gold_readme-->`); we regenerate the whole
    span from a template, so there is no ambiguity around short decimals like "1.0".

`docs_stats.json` is the canonical record of what the docs currently say. A hermetic
test (tests/test_doc_sync.py) enforces that the docs and docs_stats.json agree, so a
hand-edit that forgets one is caught in CI (no snapshot needed there).

Usage:
  python tools/sync_docs.py            # rewrite docs + docs_stats.json from the live snapshot
  python tools/sync_docs.py --check    # report drift vs the live snapshot; exit 1 if any (no writes)

Run automatically by scheduled_refresh.py after every data refresh.
Not the MCP server — stdout is fine here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from decimal import ROUND_HALF_UP, Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATS_PATH = os.path.join(ROOT, "docs_stats.json")
README = os.path.join(ROOT, "README.md")
CLAUDEMD = os.path.join(ROOT, "CLAUDE.md")


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_f(x: float) -> str:
    """Round half-up to 2 dp, then drop a trailing zero so 1.00→'1.0', 0.885→'0.89'."""
    d = Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = f"{d:.2f}"
    return s[:-1] if s.endswith("0") else s


def _gold_readme(g: dict) -> str:
    return (f"`website_business` F1 ≈ {_fmt_f(g['website_business_f1'])}, "
            f"`business_banking` F1 ≈ {_fmt_f(g['business_banking_f1'])} "
            f"(R {_fmt_f(g['business_banking_recall'])})")


def _gold_claude(g: dict) -> str:
    return (f"serves_business F1≈{_fmt_f(g['website_business_f1'])}, "
            f"has_business_login precision {_fmt_f(g['business_login_precision'])} "
            f"/ recall≈{_fmt_f(g['business_login_recall'])}")


MARKERS = {"gold_readme": (_gold_readme, README), "gold_claude": (_gold_claude, CLAUDEMD)}


def compute_live() -> dict:
    """Build the snapshot and derive the canonical figures from it."""
    from data_loader import build_snapshot, get_all_institutions, get_nic_names, get_data_as_of
    from score_coverage import _load_gold
    from metrics_snapshot import _confuse, GOLD_DEFAULT

    asyncio.run(build_snapshot())
    insts = get_all_institutions()
    gold = _load_gold(os.path.join(ROOT, GOLD_DEFAULT))
    wb = _confuse(gold, insts, "gold_business", "website_business")
    bb = _confuse(gold, insts, "gold_business", "business_banking")
    bl = _confuse(gold, insts, "gold_login", "business_login_portal")
    dates = {k.lower(): v for k, v in (get_data_as_of() or {}).items()}
    return {
        "active_total": len(insts),
        "banks": sum(1 for r in insts if r.get("source") == "fdic"),
        "credit_unions": sum(1 for r in insts if r.get("source") == "ncua"),
        "historical": len(get_nic_names()),
        "as_of": {k: dates.get(k) for k in ("fdic", "ncua", "ffiec")},
        "gold": {
            "website_business_f1": wb["f1"],
            "business_banking_f1": bb["f1"],
            "business_banking_recall": bb["recall"],
            "business_login_precision": bl["precision"],
            "business_login_recall": bl["recall"],
        },
    }


def _load_stats() -> dict:
    with open(STATS_PATH) as f:
        return json.load(f)


def _apply(old: dict, live: dict) -> list[str]:
    """Rewrite docs in place from `live`, using `old` (docs_stats) for count tokens. Returns change log."""
    changes: list[str] = []
    count_fields = ["active_total", "banks", "credit_unions", "historical"]

    readme = open(README, encoding="utf-8").read()
    for fld in count_fields:
        if old[fld] != live[fld]:
            readme = readme.replace(_fmt_int(old[fld]), _fmt_int(live[fld]))
            changes.append(f"README {fld}: {_fmt_int(old[fld])} → {_fmt_int(live[fld])}")
    open(README, "w", encoding="utf-8").write(readme)

    for key, (tmpl, path) in MARKERS.items():
        text = open(path, encoding="utf-8").read()
        want = tmpl(live["gold"])
        # Inner span must not contain another comment marker — guards against a stray
        # `<!--SYNC:key-->` literal in prose hijacking the match.
        pat = re.compile(rf"(<!--SYNC:{key}-->)(?:(?!<!--).)*?(<!--/SYNC:{key}-->)", re.S)
        if not pat.search(text):
            changes.append(f"WARNING: marker SYNC:{key} not found in {os.path.basename(path)}")
            continue
        new = pat.sub(lambda m: m.group(1) + want + m.group(2), text)
        if new != text:
            open(path, "w", encoding="utf-8").write(new)
            changes.append(f"{os.path.basename(path)} SYNC:{key} → {want}")
    return changes


def _write_stats(live: dict) -> None:
    cur = _load_stats()
    cur.update(live)
    with open(STATS_PATH, "w") as f:
        json.dump(cur, f, indent=2)
        f.write("\n")


def _drift(old: dict, live: dict) -> list[str]:
    out = []
    for fld in ("active_total", "banks", "credit_unions", "historical"):
        if old[fld] != live[fld]:
            out.append(f"{fld}: docs say {old[fld]}, live is {live[fld]}")
    for k in ("fdic", "ncua", "ffiec"):
        if old.get("as_of", {}).get(k) != live["as_of"].get(k):
            out.append(f"as_of.{k}: docs say {old.get('as_of', {}).get(k)}, live is {live['as_of'].get(k)}")
    for k, v in live["gold"].items():
        if _fmt_f(old["gold"][k]) != _fmt_f(v):
            out.append(f"gold.{k}: docs say {_fmt_f(old['gold'][k])}, live is {_fmt_f(v)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync refresh-sensitive figures in the docs.")
    ap.add_argument("--check", action="store_true", help="report drift vs live snapshot; exit 1 if any (no writes)")
    args = ap.parse_args()

    old = _load_stats()
    live = compute_live()

    if args.check:
        drift = _drift(old, live)
        if drift:
            print("Doc figures are STALE vs the live snapshot — run `python tools/sync_docs.py`:")
            for d in drift:
                print(f"  - {d}")
            return 1
        print("Docs are in sync with the live snapshot.")
        return 0

    changes = _apply(old, live)
    _write_stats(live)
    if changes:
        print("Synced docs to the live snapshot:")
        for c in changes:
            print(f"  - {c}")
    else:
        print("Docs already in sync — no changes.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    sys.exit(main())
