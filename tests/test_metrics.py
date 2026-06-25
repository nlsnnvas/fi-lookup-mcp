"""metrics_snapshot pure helpers: scrape signature, scrape age, unmapped-hint filtering."""
from datetime import datetime

import metrics_snapshot as m


def test_days_since():
    today = datetime(2026, 6, 25)
    assert m._days_since("2026-06-19", today) == 6
    assert m._days_since("", today) is None
    assert m._days_since("not-a-date", today) is None


def test_signature_captures_signals_and_skips_nondict():
    cache = {
        "cert:1": {"serves_business": True, "serves_smb": False, "has_business_login": True,
                   "reachable": True},
        "cert:2": "junk",            # non-dict entries are ignored
    }
    sig = m._signature(cache)
    assert "cert:2" not in sig
    assert sig["cert:1"].startswith("True|False|True|")     # provider appended after
    assert sig["cert:1"].endswith("|True")                  # reachable last


def test_unmapped_hints_filters_generic_and_resolved():
    cache = {
        # resolved provider (q2.com matches an HTML pattern) -> excluded entirely
        "a": {"provider_hints": ["q2.com", "newvendor.io"]},
        # unresolved: generic + regulator hosts dropped, real vendor kept
        "b": {"provider_hints": ["googletagmanager.com", "fdic.gov", "coolfintech.com"]},
        "c": {"provider_hints": ["coolfintech.com"]},
        "d": "not-a-dict",
    }
    hints = dict(m._unmapped_hints(cache))
    assert "coolfintech.com" in hints and hints["coolfintech.com"] == 2
    assert "googletagmanager.com" not in hints     # generic analytics filtered
    assert "fdic.gov" not in hints                 # .gov filtered
    assert "q2.com" not in hints                   # entry had a resolved provider
    assert "newvendor.io" not in hints             # ...so its hints aren't counted
