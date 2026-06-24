"""Trade-name / division ingestion: FDIC TEnnN528/N529 + NCUA cleanup + surfacing."""
import server
from data_loader import DIVISION_OVERFLOW, _clean_trade_names, _fdic_trade_names, _merge_division_overflow


def test_fdic_trade_names_extracts_urls_and_names():
    raw = {
        "TE01N528": "www.amegybank.com", "TE02N528": "www.calbanktrust.com",
        "TE07N528": "www.zionsbank.com",                    # non-contiguous slot
        "TE01N529": "Amegy Bank of Texas", "TE02N529": "California Bank & Trust",
    }
    urls, names = _fdic_trade_names(raw)
    assert urls == ["www.amegybank.com", "www.calbanktrust.com", "www.zionsbank.com"]
    assert names == ["Amegy Bank of Texas", "California Bank & Trust"]


def test_fdic_trade_names_empty():
    assert _fdic_trade_names({}) == ([], [])


def test_fdic_trade_names_filters_null_sentinels():
    # FDIC stores literal "NULL"/"N/A" placeholders (e.g. TD Bank) — must not become divisions
    raw = {f"TE{i:02d}N528": "NULL" for i in range(1, 11)}
    raw.update({f"TE{i:02d}N529": "N/A" for i in range(1, 7)})
    raw["TE01N528"] = "www.realbrand.com"
    urls, names = _fdic_trade_names(raw)
    assert urls == ["www.realbrand.com"]
    assert names == []


def test_clean_trade_names_dedupes_and_drops_legal_name():
    # drops the entry identical to the legal name; dedupes case-insensitively
    assert _clean_trade_names(["Broadview", "broadview", "NOFFCU", "BROADVIEW"], "Broadview") == ["NOFFCU"]


def test_clean_trade_names_keeps_distinct_brands():
    assert _clean_trade_names(["Falls Landing", "CU Marketing Group LLC"], "SERVICE FIRST") == \
        ["Falls Landing", "CU Marketing Group LLC"]


def test_division_overflow_merges_for_capped_bank():
    fdic10 = ["www.valleybankglacier.com", "www.fsbmsla.com", "www.firstbankofwyoming.com"]
    merged = _merge_division_overflow("30788", fdic10)            # Glacier
    assert len(merged) == len(fdic10) + len(DIVISION_OVERFLOW["30788"])
    assert "www.altabank.com" in merged and "www.gofirstbank.com" in merged


def test_division_overflow_noop_and_dedup():
    assert _merge_division_overflow("99999", ["www.x.com"]) == ["www.x.com"]   # uncapped bank
    merged = _merge_division_overflow("30788", ["www.altabank.com"])           # already present
    assert merged.count("www.altabank.com") == 1


def test_division_fields_surface_in_full_record():
    rec = server._full_record({"source": "fdic",
                               "trade_name_urls": ["www.a.com", "www.b.com"],
                               "trade_names": ["Brand A"],
                               "divisions": [{"serves_business": True}, {"serves_business": False}]})
    assert rec["division_count"] == 2
    assert rec["trade_name_urls"] == "www.a.com, www.b.com"
    assert rec["trade_names"] == "Brand A"
    assert rec["divisions_serving_business"] == 1


def test_is_real_division_filters_login_and_parent_redirects():
    from division_loader import is_real_division
    # redirects to a login/auth portal -> not a home URL
    assert not is_real_division("www.jpmorgan.chase.com",
                                {"pages_checked": ["https://secure.chase.com/web/auth/?treatment=jpo"]},
                                "jpmorganchase.com")
    # bounces to the parent's own home domain -> not a distinct division
    assert not is_real_division("www.wf.com", {"pages_checked": ["https://www.wellsfargo.com/"]}, "wellsfargo.com")
    # a normal division home -> kept
    assert is_real_division("www.amegybank.com", {"pages_checked": ["https://www.amegybank.com/"]},
                            "jpmorganchase.com")
    # 200 stub of a consumed/dead domain (UMB's absorbed brands titled "Invalid URL")
    assert not is_real_division("www.premiervalleybank.com",
                                {"pages_checked": ["https://www.premiervalleybank.com"],
                                 "title": "Invalid URL", "reachable": True}, "umb.com")
    assert not is_real_division("www.x.com", {"title": "DNS resolution error", "reachable": True}, "p.com")
    # no scrape data -> kept (can't tell, don't drop)
    assert is_real_division("www.newbrand.com", {}, "parent.com")


def test_clean_name_and_derive():
    from division_loader import clean_name, derive_name
    assert clean_name("Zions Bank Personal Home Page") == "Zions Bank"
    # prefer the segment ending in a bank word, not a tagline containing "financial"
    assert clean_name("Kansas City's trusted financial partner for 70 years | Country Club Bank") == "Country Club Bank"
    assert clean_name("Altabank | We Got You") == "Altabank"
    assert clean_name("Experienced, Local Partners - The Commerce Bank") == "The Commerce Bank"
    assert clean_name("Just a moment...") == ""          # Cloudflare challenge rejected
    assert clean_name("") == ""
    assert derive_name("www.gnty.com") == "Gnty"         # guaranteed last-resort name


def test_pair_names_matches_clear_brands_only():
    from division_loader import pair_names
    p = pair_names(["www.amegybank.com", "www.calbanktrust.com", "www.nsbank.com"],
                   ["Amegy Bank of Texas", "California Bank & Trust"])
    assert p["www.amegybank.com"] == "Amegy Bank of Texas"
    assert p["www.calbanktrust.com"] == "California Bank & Trust"
    assert "www.nsbank.com" not in p              # acronym domain -> left unnamed, not guessed
    # an unrelated name must not pair
    assert pair_names(["www.altabank.com"], ["Heritage Bank of Nevada"]) == {}


def test_enrich_divisions_attaches_per_division_coverage(monkeypatch):
    import division_loader as dl
    inst = {"source": "fdic", "trade_name_urls": ["www.a.com", "www.b.com"]}
    fake = {"a.com": {"serves_business": True, "serves_smb": False, "has_business_login": True,
                      "reachable": True, "business_login_url": "https://a.com/biz"}}
    monkeypatch.setattr(dl, "load_division_coverage", lambda: fake)
    dl.enrich_divisions([inst])
    assert len(inst["divisions"]) == 2
    a = next(d for d in inst["divisions"] if d["url"] == "www.a.com")
    assert a["serves_business"] is True and a["has_business_login"] is True
    b = next(d for d in inst["divisions"] if d["url"] == "www.b.com")
    assert b["serves_business"] is None  # not in cache -> unknown, not False
