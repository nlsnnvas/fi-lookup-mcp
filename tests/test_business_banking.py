"""business_banking composite: trust deterministic lending data over the homepage scrape."""
import server


def test_lender_overrides_negative_website_scrape():
    # Fifth Third class: C&I lender whose JS homepage scraped as no-business -> still yes.
    rec = server._full_record({"source": "fdic", "business_lending": "yes", "serves_business": False})
    assert rec["business_banking"] == "yes"
    assert "lending data" in rec["business_basis"]


def test_sba_lender_alone_is_yes():
    rec = server._full_record({"source": "fdic", "business_lending": "no",
                               "sba_lender": True, "serves_business": None})
    assert rec["business_banking"] == "yes"


def test_falls_back_to_website_when_not_a_lender():
    rec = server._full_record({"source": "fdic", "business_lending": "no", "serves_business": True})
    assert rec["business_banking"] == "yes"
    assert "website" in rec["business_basis"]


def test_no_only_when_not_lender_and_site_says_no():
    rec = server._full_record({"source": "fdic", "business_lending": "no", "serves_business": False})
    assert rec["business_banking"] == "no"


def test_unknown_when_not_lender_and_site_unreachable():
    rec = server._full_record({"source": "fdic", "business_lending": "no", "serves_business": None})
    assert rec["business_banking"] == "unknown"


def test_never_downgrades_a_website_yes():
    # lending data can't disprove deposit accounts -> a website 'yes' is never flipped to 'no'
    rec = server._full_record({"source": "fdic", "business_lending": "unknown", "serves_business": True})
    assert rec["business_banking"] == "yes"


def test_business_banking_is_a_listable_field():
    assert "business_banking" in server._LIST_FIELDS
    assert "business_basis" in server._LIST_FIELDS
