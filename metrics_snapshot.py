#!/usr/bin/env python
"""
metrics_snapshot.py  —  continuous accuracy/coverage monitoring for the non-deterministic fields.

The website/inferred signals (serves_business, has_business_login, service_provider, …) drift
as sites change, bot walls move, and re-scrapes land. The Tier 1-3 validators measure quality at
a point in time; this turns them into a TREND: it computes one metrics record per run, appends it
to cache/accuracy_history.jsonl, and prints the delta vs the previous run with threshold alerts.

What it records each run:
  coverage  — scanned / unreachable / not_scanned counts, unreachable_rate, median scrape age
  audit     — Tier-1 cross-signal contradiction counts (recall_miss, precision_suspect, …)
  gold      — Tier-3 precision/recall/F1 for website_business, business_banking, business_login
  providers — distinct service_providers + the top UNMAPPED provider_hints (new-pattern worklist)
  bb_basis  — business_banking yes split by lending-data vs website
  churn     — fields that flipped since the last run (serves_business/login/provider/reachable)

Append-only history; safe to run on every refresh. Reads only the snapshot + caches (no network).

    python metrics_snapshot.py                 # compute, append, print delta + alerts
    python metrics_snapshot.py --no-write       # dry run (don't append or update churn baseline)
    python metrics_snapshot.py --history PATH --gold PATH
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime

import business_classifier as bc
import server
from audit_coverage import CATEGORIES, classify_flags
from business_classifier import HTML_PROVIDER_PATTERNS, PROVIDER_DOMAINS, classify_provider, inst_key, load_coverage
from data_loader import build_snapshot, get_all_institutions, get_data_as_of
from score_coverage import _load_gold, _match, _prf

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
HISTORY_DEFAULT = os.path.join(CACHE_DIR, "accuracy_history.jsonl")
SIGNATURE_FILE = os.path.join(CACHE_DIR, "coverage_signature.json")
GOLD_DEFAULT = "tests/gold_business_coverage.csv"

# Alert thresholds (vs the previous run).
F1_DROP = 0.05            # gold F1 falling this much
UNREACH_RISE = 0.02       # unreachable_rate rising this much (2pp)
CHURN_FRAC = 0.05         # fraction of signals flipping in one run


def _days_since(d: str, today: datetime) -> int | None:
    try:
        return (today - datetime.strptime(d, "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


def _confuse(gold, insts, gold_key, rec_field) -> dict:
    """Confusion matrix for one record field vs a gold yes/no column (unknown not scored)."""
    tp = fp = fn = tn = 0
    for g in gold:
        truth = g.get(gold_key, "").strip().lower()
        if truth not in ("yes", "no"):
            continue
        inst = _match(insts, g["name_query"])
        if not inst:
            continue
        pred = server._full_record(inst).get(rec_field)
        if pred not in ("yes", "no"):
            continue
        t, p = truth == "yes", pred == "yes"
        tp += p and t
        fp += p and not t
        fn += (not p) and t
        tn += (not p) and not t
    prec, rec, f1 = _prf(tp, fp, fn)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3)}


def _signature(cache: dict) -> dict:
    """Compact per-institution scrape signature, for run-to-run churn detection."""
    sig = {}
    for k, e in cache.items():
        if not isinstance(e, dict):
            continue
        sig[k] = f"{e.get('serves_business')}|{e.get('serves_smb')}|{e.get('has_business_login')}|" \
                 f"{classify_provider(e)}|{e.get('reachable')}"
    return sig


# Generic third-party hosts (analytics, ads, social, CDNs, fonts, tag managers, chat) that
# appear on nearly every site and are NOT banking platforms — excluded so the unmapped-hint
# worklist surfaces real white-label candidates, not Google Tag Manager.
_GENERIC_HINT_HOSTS = {
    "googletagmanager.com", "google-analytics.com", "googleapis.com", "gstatic.com", "google.com",
    "doubleclick.net", "googlesyndication.com", "googleadservices.com", "g.doubleclick.net",
    "facebook.com", "facebook.net", "fb.com", "instagram.com", "twitter.com", "x.com", "t.co",
    "linkedin.com", "licdn.com", "youtube.com", "ytimg.com", "vimeo.com", "pinterest.com", "tiktok.com",
    "cloudflare.com", "cloudflareinsights.com", "cloudfront.net", "akamaized.net", "akamai.net",
    "jsdelivr.net", "jquery.com", "bootstrapcdn.com", "fontawesome.com", "typekit.net", "fonts.com",
    "hotjar.com", "hs-scripts.com", "hubspot.com", "hubapi.com", "segment.com", "newrelic.com",
    "recaptcha.net", "gstatic.cn", "bing.com", "clarity.ms", "adobedtm.com", "demdex.net",
    "addthis.com", "sharethis.com", "olark.com", "livechatinc.com", "zopim.com", "intercom.io",
    "cookielaw.org", "onetrust.com", "trustarc.com", "usercentrics.eu", "wp.com", "gravatar.com",
    # app stores, web standards, CMS, regulator badges every bank links (not platforms)
    "apple.com", "microsoft.com", "android.com", "play.google.com", "gmpg.org", "w3.org",
    "schema.org", "wordpress.org", "wordpress.com", "wix.com", "squarespace.com", "godaddy.com",
    "adobe.com", "adobedtm.com", "audioeye.com", "siteimprove.com", "accessibe.com",  # analytics/a11y overlays
}


def _unmapped_hints(cache: dict, top: int = 12) -> list:
    """Most frequent provider_hints on entries with NO resolved provider, EXCLUDING generic
    analytics/social/CDN hosts — a candidate-new-pattern worklist (and an over-match guard)."""
    known = {n for n, _ in HTML_PROVIDER_PATTERNS}
    counts = Counter()
    for e in cache.values():
        if not isinstance(e, dict) or classify_provider(e):
            continue
        for h in (e.get("provider_hints") or []):
            if h.startswith("poweredby:"):
                counts[h] += 1
                continue
            reg = bc._reg_domain(h)
            if reg in _GENERIC_HINT_HOSTS or reg.endswith(".gov") or reg in PROVIDER_DOMAINS \
                    or any(n in h for n in known):
                continue
            counts[h] += 1
    return counts.most_common(top)


def collect(insts: list, cache: dict, today: datetime, gold_path: str = GOLD_DEFAULT) -> dict:
    recs = [server._full_record(i) for i in insts]

    status = Counter(i.get("business_coverage_status", "not_scanned") for i in insts)
    attempted = status["scanned"] + status["unreachable"]
    ages = [d for d in (_days_since(i.get("business_coverage_checked_at", ""), today) for i in insts)
            if d is not None]
    coverage = {
        "scanned": status["scanned"], "unreachable": status["unreachable"],
        "not_scanned": status["not_scanned"],
        "unreachable_rate": round(status["unreachable"] / attempted, 4) if attempted else 0.0,
        "median_scrape_age_days": int(statistics.median(ages)) if ages else None,
    }

    audit = Counter()
    for i in insts:
        for f in classify_flags(i):
            audit[f] += 1

    gold = _load_gold(gold_path)
    gold_scores = {
        "website_business": _confuse(gold, insts, "gold_business", "website_business"),
        "business_banking": _confuse(gold, insts, "gold_business", "business_banking"),
        "business_login": _confuse(gold, insts, "gold_login", "business_login_portal"),
    }

    providers = Counter(r["service_provider"] for r in recs if r["service_provider"])
    bb = [r for r in recs if r["business_banking"] == "yes"]
    return {
        "ts": today.strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_as_of": get_data_as_of(),
        "rows": len(insts),
        "coverage": coverage,
        "audit": {c: audit[c] for c in CATEGORIES},
        "gold": gold_scores,
        "providers": {"distinct": len(providers), "top": providers.most_common(8),
                      "top_unmapped_hints": _unmapped_hints(cache)},
        "bb_basis": {
            "yes_total": len(bb),
            "yes_by_lending": sum(1 for r in bb if r["business_basis"].startswith("regulatory")),
            "yes_by_website": sum(1 for r in bb if r["business_basis"].startswith("website")),
        },
    }


def _churn(cache: dict, write: bool) -> dict:
    """Compare scrape signatures to the last run; count flips. Updates the baseline if write."""
    new = _signature(cache)
    prev = {}
    if os.path.exists(SIGNATURE_FILE):
        try:
            with open(SIGNATURE_FILE) as f:
                prev = json.load(f)
        except (OSError, json.JSONDecodeError):
            prev = {}
    changed = [k for k in new.keys() & prev.keys() if new[k] != prev[k]]
    out = {"tracked": len(new), "added": len(new.keys() - prev.keys()),
           "removed": len(prev.keys() - new.keys()), "changed": len(changed),
           "changed_frac": round(len(changed) / len(prev), 4) if prev else 0.0,
           "sample": [{"key": k, "from": prev[k], "to": new[k]} for k in changed[:8]],
           "baseline_existed": bool(prev)}
    if write:
        tmp = SIGNATURE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(new, f)
        os.replace(tmp, SIGNATURE_FILE)
    return out


def _last_history(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    last = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    try:
        return json.loads(last) if last else None
    except json.JSONDecodeError:
        return None


def _delta_report(cur: dict, prev: dict | None) -> tuple[list, list]:
    """Human-readable deltas + threshold alerts vs the previous run."""
    lines, alerts = [], []

    def arrow(now, was, places=4):
        if was is None:
            return f"{now}"
        d = round(now - was, places)
        return f"{now}  ({'+' if d >= 0 else ''}{d} vs last)"

    p = prev or {}
    pc, cc = p.get("coverage", {}), cur["coverage"]
    lines.append(f"  coverage: {cc['scanned']} scanned, {cc['unreachable']} unreachable, "
                 f"{cc['not_scanned']} not_scanned")
    lines.append(f"  unreachable_rate: {arrow(cc['unreachable_rate'], pc.get('unreachable_rate'))}")
    lines.append(f"  median_scrape_age_days: {cc['median_scrape_age_days']}")
    for fld in ("website_business", "business_banking", "business_login"):
        cf = cur["gold"][fld]["f1"]
        pf = p.get("gold", {}).get(fld, {}).get("f1")
        lines.append(f"  gold {fld} F1: {arrow(cf, pf, 3)}  "
                     f"(P{cur['gold'][fld]['precision']}/R{cur['gold'][fld]['recall']})")
        if pf is not None and cf < pf - F1_DROP:
            alerts.append(f"gold {fld} F1 dropped {round(pf - cf, 3)} ({pf} → {cf})")
    pa, ca = p.get("audit", {}), cur["audit"]
    lines.append("  audit: " + ", ".join(f"{c}={ca[c]}" + (f"({'+' if ca[c]-pa.get(c,ca[c])>=0 else ''}{ca[c]-pa[c]})"
                 if c in pa else "") for c in CATEGORIES))
    ch = cur["churn"]
    lines.append(f"  churn: {ch['changed']} signals flipped "
                 f"({ch['changed_frac']*100:.1f}%), +{ch['added']} new, -{ch['removed']} gone"
                 + ("" if ch["baseline_existed"] else "  [first run — baseline set]"))
    bb = cur["bb_basis"]
    lines.append(f"  business_banking=yes: {bb['yes_total']} "
                 f"({bb['yes_by_lending']} by lending, {bb['yes_by_website']} by website)")
    uh = cur["providers"]["top_unmapped_hints"]
    if uh:
        lines.append(f"  top unmapped provider hints: " + ", ".join(f"{h}×{n}" for h, n in uh[:5]))

    if pc.get("unreachable_rate") is not None and cc["unreachable_rate"] > pc["unreachable_rate"] + UNREACH_RISE:
        alerts.append(f"unreachable_rate rose {round(cc['unreachable_rate']-pc['unreachable_rate'],4)} "
                      f"({pc['unreachable_rate']} → {cc['unreachable_rate']})")
    if ch["baseline_existed"] and ch["changed_frac"] > CHURN_FRAC:
        alerts.append(f"churn spike: {ch['changed_frac']*100:.1f}% of scrape signals flipped this run")
    return lines, alerts


def emit(history_path: str = HISTORY_DEFAULT, write: bool = True,
         gold_path: str = GOLD_DEFAULT) -> tuple[dict, str]:
    """Build the snapshot, compute metrics + churn, append history, return (metrics, report_text)."""
    asyncio.run(build_snapshot())
    insts = get_all_institutions()
    cache = load_coverage()
    today = datetime.now()
    metrics = collect(insts, cache, today, gold_path)
    metrics["churn"] = _churn(cache, write)

    prev = _last_history(history_path)
    lines, alerts = _delta_report(metrics, prev)
    report = "Accuracy/coverage snapshot @ " + metrics["ts"] + "\n" + "\n".join(lines)
    if alerts:
        report += "\n\n  ⚠ ALERTS:\n" + "\n".join(f"    - {a}" for a in alerts)
    else:
        report += "\n\n  ✓ no threshold alerts"

    if write:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(history_path, "a") as f:
            f.write(json.dumps(metrics) + "\n")
    return metrics, report


def main() -> None:
    ap = argparse.ArgumentParser(description="Append an accuracy/coverage metrics snapshot and report the delta")
    ap.add_argument("--history", default=HISTORY_DEFAULT)
    ap.add_argument("--gold", default=GOLD_DEFAULT)
    ap.add_argument("--no-write", action="store_true", help="dry run: don't append history or update churn baseline")
    args = ap.parse_args()
    _, report = emit(history_path=args.history, write=not args.no_write, gold_path=args.gold)
    print(report)


if __name__ == "__main__":
    main()
