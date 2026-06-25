#!/usr/bin/env python
"""
score_coverage.py  —  Tier 3: score the web scraper against a hand-labeled gold set.

Tiers 1-2 tell you WHERE the scraper is wrong for free (cross-signal contradictions,
JS flip rate). This tells you HOW OFTEN, defensibly: it matches each row of
tests/gold_business_coverage.csv to the live snapshot and computes precision / recall /
F1 for `serves_business` (and `has_business_login`), split by reachable vs unreachable —
because an unreachable site is an honest "unknown", not a wrong answer, and shouldn't be
scored as one. Every miss is printed so the gold set and the scraper both stay honest.

Not part of the hermetic pytest suite (it needs the built snapshot). Run after a refresh:

    python score_coverage.py
    python score_coverage.py --gold tests/gold_business_coverage.csv --list
"""
import argparse
import asyncio
import csv
import sys

from data_loader import build_snapshot, get_all_institutions

GOLD_DEFAULT = "tests/gold_business_coverage.csv"


def _dep(i):
    try:
        return int(i.get("deposit_accounts") or 0)
    except (TypeError, ValueError):
        return 0


def _load_gold(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(l for l in f if not l.startswith("#"))]
    return rows


def _match(insts: list[dict], q: str) -> dict | None:
    hits = [i for i in insts if q.lower() in i.get("name", "").lower()]
    hits.sort(key=_dep, reverse=True)
    return hits[0] if hits else None


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return prec, rec, f1


def _score_field(gold, insts, gold_key, pred_key, show):
    """Confusion matrix for one yes/no field over reachable (scanned) gold rows."""
    tp = fp = fn = tn = 0
    skipped_unknown = skipped_unreachable = 0
    misses = []
    for g in gold:
        truth = g.get(gold_key, "").strip().lower()
        if truth not in ("yes", "no"):
            skipped_unknown += 1
            continue
        inst = _match(insts, g["name_query"])
        if not inst:
            misses.append((g["name_query"], "NOT FOUND in snapshot"))
            continue
        if inst.get("business_coverage_status") != "scanned":
            skipped_unreachable += 1
            continue
        pred = inst.get(pred_key)            # True / False / None
        t = truth == "yes"
        if pred is True and t:
            tp += 1
        elif pred is True and not t:
            fp += 1
            misses.append((inst["name"][:40], f"{pred_key}=yes but gold=no"))
        elif (pred in (False, None)) and t:
            fn += 1
            misses.append((inst["name"][:40], f"{pred_key}=no but gold=yes"))
        else:
            tn += 1
    prec, rec, f1 = _prf(tp, fp, fn)
    n = tp + fp + fn + tn
    acc = (tp + tn) / n if n else 0.0
    print(f"\n{pred_key}  (scored on {n} reachable gold rows; "
          f"{skipped_unreachable} unreachable, {skipped_unknown} unlabeled skipped)")
    print(f"  TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    print(f"  precision={prec:.2f}  recall={rec:.2f}  F1={f1:.2f}  accuracy={acc:.2f}")
    if show and misses:
        print("  misses:")
        for name, why in misses:
            print(f"    - {name}: {why}")
    return f1


def main() -> None:
    ap = argparse.ArgumentParser(description="Score the scraper against the gold set")
    ap.add_argument("--gold", default=GOLD_DEFAULT)
    ap.add_argument("--list", action="store_true", help="print every miss")
    ap.add_argument("--min-f1", type=float, default=0.0, help="exit 1 if serves_business F1 < this")
    args = ap.parse_args()

    gold = _load_gold(args.gold)
    asyncio.run(build_snapshot())
    insts = get_all_institutions()
    if not insts:
        print("No institutions loaded.", file=sys.stderr)
        sys.exit(1)

    # Coverage of the gold set (how many are even scannable vs blocked).
    statuses = {}
    for g in gold:
        inst = _match(insts, g["name_query"])
        st = inst.get("business_coverage_status", "NOT FOUND") if inst else "NOT FOUND"
        statuses[st] = statuses.get(st, 0) + 1
    print(f"Gold set: {len(gold)} institutions. Snapshot status: " +
          ", ".join(f"{n} {s}" for s, n in sorted(statuses.items(), key=lambda x: -x[1])))

    biz_f1 = _score_field(gold, insts, "gold_business", "serves_business", args.list)
    _score_field(gold, insts, "gold_login", "has_business_login", args.list)

    sys.exit(1 if (args.min_f1 and biz_f1 < args.min_f1) else 0)


if __name__ == "__main__":
    main()
