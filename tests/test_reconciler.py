"""Reconciliation scoring — the deterministic core of reconcile_institution."""
from reconciler import normalize_name, score_name, score_geo, reconcile

# A tiny in-memory fixture; reconcile() takes institutions as a param, so these
# tests need no snapshot, no network, and no cache ZIPs (CI-safe).
FIXTURE = [
    {"name": "Mountain America Federal Credit Union", "city": "Sandy", "state": "UT",
     "source": "ncua", "charter_number": "24692", "cert": "", "rssdid": "0"},
    {"name": "JPMorgan Chase Bank, National Association", "city": "Columbus", "state": "Ohio",
     "source": "fdic", "cert": "628", "rssdid": "852218", "charter_number": ""},
]


def test_normalize_expands_abbreviations():
    n = normalize_name("Mtn America FCU")
    assert "mountain" in n
    assert "federal credit union" in n
    assert "fcu" not in n  # abbreviation was expanded, not left as-is


def test_normalize_lowercases_and_strips_punctuation():
    n = normalize_name("First National Bank, Inc.")
    assert n == n.lower()
    assert "," not in n and "." not in n


def test_normalize_expands_contiguous_abbreviations():
    # Abbreviations without embedded punctuation expand cleanly.
    assert normalize_name("Sandy CU") == "sandy credit union"
    # NOTE: dotted forms (N.A.) are split by punctuation-stripping before expansion,
    # so they do NOT fully expand — pinning current scorer behavior, not asserting ideal.
    assert "national association" not in normalize_name("Bank N.A.")


def test_score_name_identical_is_near_one():
    a = normalize_name("Mountain America Credit Union")
    assert score_name(a, a) > 0.99


def test_score_name_unrelated_is_low():
    q = normalize_name("Mountain America Credit Union")
    c = normalize_name("JPMorgan Chase Bank")
    assert score_name(q, c) < 0.5


def test_score_geo_city_adds_to_state():
    cand = {"state": "UT", "city": "Sandy", "source": "ncua"}
    state_only, _ = score_geo("", "UT", cand)
    both, _ = score_geo("Sandy", "UT", cand)
    assert state_only > 0          # state agreement scores
    assert both > state_only       # a matching city adds more


def test_score_geo_normalizes_full_state_name():
    # FDIC stores "Utah", query gives "UT" — must still agree.
    full_name = score_geo("Sandy", "UT", {"state": "Utah", "city": "Sandy", "source": "fdic"})[0]
    abbrev = score_geo("Sandy", "UT", {"state": "UT", "city": "Sandy", "source": "ncua"})[0]
    assert full_name == abbrev


def test_reconcile_empty_institutions_returns_empty_list():
    assert reconcile("anything", institutions=[]) == []
    assert reconcile("anything", institutions=None) == []


def test_reconcile_fuzzy_ranks_correct_candidate_first():
    res = reconcile("Mtn America FCU", query_city="Sandy", query_state="UT", institutions=FIXTURE)
    assert res, "expected at least one candidate"
    assert "Mountain America" in res[0]["name"]
    assert res[0]["confidence"] > 0.8


def test_reconcile_exact_cert_overrides_to_one():
    # Even with a wrong name, an exact cert match forces confidence to 1.0.
    res = reconcile("totally wrong name", query_cert="628", institutions=FIXTURE)
    jpm = [r for r in res if r["name"].startswith("JPMorgan")]
    assert jpm and jpm[0]["confidence"] == 1.0
