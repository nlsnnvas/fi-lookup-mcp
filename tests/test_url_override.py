"""Corporate-vs-consumer URL handling: override resolution + unreachable honesty."""
import business_classifier as bc


def test_consumer_override_resolves_corporate_domain():
    # jpmorganchase.com (corporate) -> chase.com (consumer)
    assert bc._consumer_scrape_url("www.jpmorganchase.com") == "https://www.chase.com"
    assert bc._consumer_scrape_url("https://jpmorganchase.com/path") == "https://www.chase.com"


def test_non_overridden_domains_pass_through():
    assert bc._consumer_scrape_url("www.chase.com") == "www.chase.com"
    assert bc._consumer_scrape_url("https://www.smalltownbank.com") == "https://www.smalltownbank.com"
    assert bc._consumer_scrape_url("") == ""


def test_santander_global_override():
    # santander.com (global group) -> santanderbank.com (US retail)
    assert bc._consumer_scrape_url("www.santander.com") == "https://www.santanderbank.com"


def test_is_auth_portal_distinguishes_portals_from_marketing():
    # vendor-hosted login host -> portal
    assert bc._is_auth_portal("https://q2online.com/login", "www.bank.com")
    # login-y subdomain on the bank's own domain -> portal (Banco Popular's real portal)
    assert bc._is_auth_portal("https://businessaccess.popular.com/home/uux.aspx", "www.popular.com")
    assert bc._is_auth_portal("https://securelogin.synchronybank.com/", "www.synchronybank.com")
    # auth token in the path -> portal
    assert bc._is_auth_portal("https://www.bank.com/business/login", "www.bank.com")
    # same-host marketing page that merely mentions "online banking" -> NOT a portal (BECU)
    assert not bc._is_auth_portal(
        "https://www.becu.org/business-banking/online-banking/llc-partnerships-corporations", "www.becu.org")
    assert not bc._is_auth_portal("", "www.bank.com")


def test_unreachable_reports_unknown_not_no(monkeypatch):
    """A site we couldn't reach must read as unknown (None), never 'no' (False)."""
    inst = {"source": "fdic", "cert": "628", "rssdid": "852218", "web_address": "www.x.com"}
    entry = {"reachable": False, "serves_business": False, "serves_smb": False,
             "has_business_login": False, "distinct_business_login": False,
             "business_login_url": "", "personal_login_url": "", "checked_at": "2026-01-01"}
    monkeypatch.setattr(bc, "load_coverage", lambda: {bc.inst_key(inst): entry})
    bc.enrich_institutions([inst])
    assert inst["serves_business"] is None
    assert inst["serves_smb"] is None
    assert inst["has_business_login"] is None
    assert inst["business_coverage_status"] == "unreachable"


def test_reachable_uses_scraped_values(monkeypatch):
    inst = {"source": "fdic", "cert": "999", "rssdid": "111", "web_address": "www.y.com"}
    entry = {"reachable": True, "serves_business": True, "serves_smb": False,
             "has_business_login": True, "distinct_business_login": False,
             "business_login_url": "https://y.com/biz", "personal_login_url": "",
             "checked_at": "2026-01-01", "provider_hints": [], "login_portals": []}
    monkeypatch.setattr(bc, "load_coverage", lambda: {bc.inst_key(inst): entry})
    bc.enrich_institutions([inst])
    assert inst["serves_business"] is True
    assert inst["business_coverage_status"] == "scanned"
