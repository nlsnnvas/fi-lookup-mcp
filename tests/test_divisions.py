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
                               "trade_names": ["Brand A"]})
    assert rec["division_count"] == 2
    assert rec["trade_name_urls"] == "www.a.com, www.b.com"
    assert rec["trade_names"] == "Brand A"
