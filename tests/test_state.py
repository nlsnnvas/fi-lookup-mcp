"""State standardization — server._canonical_state maps any form to the USPS code."""
from server import _canonical_state


def test_full_name_to_code():
    assert _canonical_state("Utah") == "UT"
    assert _canonical_state("New York") == "NY"
    assert _canonical_state("west virginia") == "WV"  # case-insensitive


def test_two_letter_code_passthrough():
    assert _canonical_state("UT") == "UT"
    assert _canonical_state("ut") == "UT"


def test_territories_and_dc():
    assert _canonical_state("Puerto Rico") == "PR"
    assert _canonical_state("District Of Columbia") == "DC"
    assert _canonical_state("U.S. Virgin Islands") == "VI"
    assert _canonical_state("Guam") == "GU"


def test_blank_is_empty():
    assert _canonical_state("") == ""
    assert _canonical_state("   ") == ""
    assert _canonical_state(None) == ""


def test_unknown_passes_through_unmangled():
    # Unexpected values stay visible rather than being coerced.
    assert _canonical_state("Atlantis") == "Atlantis"
