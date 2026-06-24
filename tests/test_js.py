"""Optional JS tier — pure logic only (no browser, so this runs in CI).
   business_classifier.scrape_one is reused unchanged via the _Resp/PlaywrightClient
   adapter; here we just pin candidate selection + the response shim shape."""
import js_loader as jl


def test_candidate_selection():
    assert jl._is_candidate({"business_coverage_status": "unreachable"}) is True
    assert jl._is_candidate({"business_coverage_status": "scanned", "serves_business": False,
                             "serves_smb": False, "has_business_login": False,
                             "service_provider": ""}) is True
    # already has a signal -> not worth a browser render
    assert jl._is_candidate({"business_coverage_status": "scanned", "serves_business": True}) is False
    assert jl._is_candidate({"business_coverage_status": "not_scanned"}) is False


def test_resp_shim_matches_httpx_surface():
    r = jl._Resp("<html>", "https://x.com/final", 200)
    assert r.text == "<html>" and str(r.url) == "https://x.com/final" and r.status_code == 200


def test_dep_parsing():
    assert jl._dep({"deposit_accounts": "1000"}) == 1000
    assert jl._dep({"deposit_accounts": None}) == 0
    assert jl._dep({}) == 0
