"""Digital-banking provider classification, including the false-positive guards."""
import business_classifier as bc


def test_login_host_resolves_provider():
    # A vendor-hosted login URL is the strongest signal (PROVIDER_DOMAINS).
    entry = {"login_portals": [{"url": "https://web5.secureinternetbank.com/PBI_PBI1151/login/"}]}
    assert bc.classify_provider(entry) == "Fiserv"


def test_html_marker_resolves_white_label_platform():
    # White-label platforms (Q2/Alkami/Banno) serve login on the bank's own domain;
    # only their HTML asset markers reveal them.
    assert bc.classify_provider({"provider_hints": ["q2cdn.com"]}) == "Q2"
    assert bc.classify_provider({"provider_hints": ["alkami"]}) == "Alkami"


def test_meridianlink_class_widgets_are_excluded_from_html_matching():
    # REGRESSION (the MeridianLink fix): embedded loan/account-opening/rewards widgets
    # appearing as a homepage asset must NOT be tagged as the banking platform.
    for widget in ["meridianlink.com", "blend.com", "mantl.com",
                   "kasasa.com", "bottomline.com", "terafina.com"]:
        assert bc.classify_provider({"provider_hints": [widget]}) == "", widget


def test_fiserv_hosted_domains_resolve_via_html_markers():
    # The researched batch: confirmed multi-tenant banking hosts.
    for dom in ["financial-net.com", "secureinternetbank.com",
                "onlineaccess1.com", "onlinebank.com"]:
        assert bc.classify_provider({"provider_hints": [dom]}) == "Fiserv", dom
    assert bc.classify_provider({"provider_hints": ["mobicint.net"]}) == "Mobicint"


def test_generic_assets_resolve_nothing():
    assert bc.classify_provider({}) == ""
    assert bc.classify_provider({"provider_hints": ["facebook.com", "googleapis.com", "fontawesome.com"]}) == ""
