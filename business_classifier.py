"""
business_classifier.py
Determine business-account coverage by scraping each institution's home URL.

Regulatory data (FDIC LNCI / NCUA member-business loans) only tells you an
institution *lends* to businesses — not whether it *offers business deposit
accounts* to customers. This module gets the consumer-facing answer by fetching
the home URL (and one level of likely "Business" / "Small Business" pages) and
looking for advertised business- and SMB-account signals.

Heavy (one fetch per institution) — run as a cached, periodic enrichment, NOT
inline on every snapshot build. Results are cached in cache/business_coverage.json
keyed by a stable institution id, with the matched evidence and a checked_at date
so each flag is auditable.

Never prints to stdout (MCP stdio safety) — diagnostics go to stderr via log().
"""

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx


CACHE_DIR = Path(__file__).parent / "cache"
COVERAGE_FILE = CACHE_DIR / "business_coverage.json"

# Bump when the scrape result shape changes so cached entries get re-scanned.
# v3: capture provider_hints (HTML asset hosts + 'powered by') for white-label
# digital-banking platform fingerprinting (Q2, Alkami, Banno on the bank's domain).
SCHEMA_V = 3


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Keyword signals (matched against whitespace-normalized page text, so
# "business-checking" / "/business/" / "Business Banking" all collapse to a
# matchable "business checking" / "business" / "business banking").
# ---------------------------------------------------------------------------

# SMB is the stronger, more specific claim — checked first and implies business.
SMB_PHRASES = [
    "small business", "small business banking", "small business checking",
    "small business savings", "small business loan", "small business loans",
    "small business administration", "sba loan", "sba loans", "sba lending",
    "sba preferred lender", "sba 7a", "startup business", "for small businesses",
]

BUSINESS_PHRASES = [
    "business checking", "business savings", "business account", "business accounts",
    "business banking", "business loan", "business loans", "business line of credit",
    "business credit card", "business debit card", "business money market",
    "business cd", "business services", "business solutions", "for your business",
    "for businesses", "business owners", "commercial banking", "commercial checking",
    "commercial account", "commercial loan", "commercial lending", "treasury management",
    "merchant services", "cash management", "payroll services",
]

# Anchor hrefs/labels worth following one level deep for better recall.
LINK_KEYWORDS = ("business", "small business", "commercial", "sba", "merchant", "treasury")

# ---------------------------------------------------------------------------
# Login-portal detection (open-finance / data-aggregator signal): a separate
# BUSINESS login URL, distinct from the personal one, means an institution has
# multiple authenticated entry points an aggregator must be able to connect to.
# ---------------------------------------------------------------------------
LOGIN_HINTS = (
    "login", "log in", "log-in", "logon", "log on", "sign in", "signin", "sign-in",
    "online banking", "internet banking", "digital banking", "account access",
    "access your account", "ebanking", "e-banking", "olb",
)
LOGIN_BUSINESS_HINTS = ("business", "commercial", "treasury", "merchant", "corporate", "biz")
LOGIN_PERSONAL_HINTS = ("personal", "retail", "consumer", "individual", "member")

_LINK_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_NONWORD_RE = re.compile(r"[^a-z0-9]+")


def _normalize(html: str) -> str:
    """Strip scripts/styles, drop tags, lowercase, collapse non-alnum to spaces."""
    text = _SCRIPT_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    return _NONWORD_RE.sub(" ", text.lower())


def classify_text(html: str) -> tuple[list[str], list[str]]:
    """Return (business_evidence, smb_evidence) phrases found on the page."""
    norm = _normalize(html)
    smb = [p for p in SMB_PHRASES if p in norm]
    biz = [p for p in BUSINESS_PHRASES if p in norm]
    return sorted(set(biz)), sorted(set(smb))


def _full_url(url: str) -> str:
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def _page_title(html: str) -> str:
    """Best brand-name signal on a page: og:site_name, else the <title>."""
    m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', html or "", re.I)
    if m:
        return m.group(1).strip()[:120]
    m = re.search(r"<title[^>]*>([^<]+)</title>", html or "", re.I)
    return m.group(1).strip()[:120] if m else ""


def _discover_business_links(html: str, base: str, cap: int = 2) -> list[str]:
    """Find up to `cap` same-host links whose href/label hints at business pages."""
    base_host = (urlparse(base).hostname or "").lower()
    out, seen = [], set()
    for href, label in _LINK_RE.findall(html):
        hay = (href + " " + _TAG_RE.sub(" ", label)).lower()
        if not any(k in hay for k in LINK_KEYWORDS):
            continue
        target = urljoin(base, href).split("#")[0]
        p = urlparse(target)
        if p.scheme not in ("http", "https"):
            continue
        host = (p.hostname or "").lower()
        if base_host and host and base_host not in host and host not in base_host:
            continue  # stay on the institution's own domain
        if target in seen or target == base:
            continue
        seen.add(target)
        out.append(target)
        if len(out) >= cap:
            break
    return out


def _discover_logins(html: str, base: str) -> list[dict]:
    """
    Find login/sign-in links and classify each as business / personal / generic.

    Login portals frequently live on a different host (an online-banking vendor
    like q2online, netteller, telepc), so — unlike business *content* links — we
    do NOT restrict to the institution's own domain here.
    """
    found = {}  # url -> {"kind", "label"}
    for href, label in _LINK_RE.findall(html):
        lab = _TAG_RE.sub(" ", label).strip()
        hay = (href + " " + lab).lower()
        if not any(k in hay for k in LOGIN_HINTS):
            continue
        target = urljoin(base, href.strip()).split("#")[0]
        try:
            p = urlparse(target)
        except ValueError:
            continue
        if p.scheme not in ("http", "https"):
            continue
        if any(k in hay for k in LOGIN_BUSINESS_HINTS):
            kind = "business"
        elif any(k in hay for k in LOGIN_PERSONAL_HINTS):
            kind = "personal"
        else:
            kind = "generic"
        # Prefer a more specific classification if the same URL is seen twice.
        if target not in found or (found[target]["kind"] == "generic" and kind != "generic"):
            found[target] = {"url": target, "kind": kind, "label": lab[:60]}
    return list(found.values())


def _summarize_logins(logins: list[dict]) -> dict:
    """Roll login links up into the aggregator-facing signal."""
    biz = [l for l in logins if l["kind"] == "business"]
    personal = [l for l in logins if l["kind"] == "personal"]
    generic = [l for l in logins if l["kind"] == "generic"]
    biz_urls = {l["url"] for l in biz}
    other_urls = {l["url"] for l in personal + generic}
    return {
        "login_portals": logins[:10],
        "has_business_login": bool(biz),
        # A business login at a URL distinct from the personal/generic one =
        # a genuinely separate entry point for aggregation.
        "distinct_business_login": bool(biz_urls - other_urls),
        "business_login_url": (biz[0]["url"] if biz else ""),
        "personal_login_url": ((personal or generic)[0]["url"] if (personal or generic) else ""),
    }


# ---------------------------------------------------------------------------
# Digital-banking / OAuth service-provider fingerprinting.
# A 1:many platform (Jack Henry/Banno, Fiserv, FIS, Q2, Alkami, CU Answers, …)
# hosts the consumer login, so the login host is a strong vendor fingerprint.
# An aggregator connects once to the provider's OAuth/FDX endpoint to reach every
# institution on that platform. Derived from already-cached login URLs — no scrape.
# Registered login domain -> provider label.
# ---------------------------------------------------------------------------
PROVIDER_DOMAINS = {
    # Fiserv
    "secureinternetbank.com": "Fiserv", "myvirtualbranch.com": "Fiserv",
    "onlineaccess1.com": "Fiserv", "fiserv.com": "Fiserv", "fiservapps.com": "Fiserv",
    "financial-net.com": "Fiserv", "onlinebank.com": "Fiserv",
    # FIS
    "fisglobal.com": "FIS", "fundsxpress.com": "FIS",
    # Jack Henry
    "banno.com": "Jack Henry (Banno)", "gobanno.com": "Jack Henry (Banno)",
    "netteller.com": "Jack Henry (NetTeller)", "profitstars.com": "Jack Henry",
    "jackhenry.com": "Jack Henry", "jhabanking.com": "Jack Henry", "goolb.com": "Jack Henry",
    # CU Answers
    "itsme247.com": "CU Answers (It's Me 247)",
    # CSI
    "myebanking.net": "CSI", "csiweb.com": "CSI",
    # Mobicint
    "mobicint.net": "Mobicint", "mobicint.com": "Mobicint",
    # Apiture
    "apiture.com": "Apiture",
    # Alkami
    "alkami.com": "Alkami", "alkamitech.com": "Alkami",
    # Q2
    "q2online.com": "Q2", "q2ebanking.com": "Q2",
    # NCR Voyix / Digital Insight
    "digitalinsight.com": "NCR (Digital Insight)", "dibill.com": "NCR (Digital Insight)",
    # CU Answers business banking (companion to It's Me 247)
    "bizlink247.com": "CU Answers (BizLink 247)",
    # Others (researched from cached login hosts)
    "narmi.com": "Narmi", "bottomline.com": "Bottomline", "jwaala.com": "Jwaala",
    "lumindigital.com": "Lumin Digital", "tyfone.com": "Tyfone", "homecu.net": "HomeCU",
    "ufsdata.com": "UFS (Navanta)", "amimembernet.com": "AMI (Member.Net)",
    "realtimehomebanking.com": "RealTime Home Banking",
}

# HTML/asset fingerprints (Phase 2): white-labeled platforms (Q2, Alkami, Banno…)
# serve the login on the bank's OWN domain, so the login URL can't reveal them —
# but their JS/CDN assets and "powered by" footers do. Matched as substrings
# against captured asset hosts + page-text markers in `provider_hints`.
HTML_PROVIDER_PATTERNS = [
    # Digital-banking front ends
    ("q2.com", "Q2"), ("q2online", "Q2"), ("q2ebanking", "Q2"), ("q2cdn", "Q2"),
    ("alkami", "Alkami"),
    ("banno", "Jack Henry (Banno)"), ("jackhenry", "Jack Henry"), ("jhadigital", "Jack Henry"),
    ("symitar", "Jack Henry (Symitar)"), ("episys", "Jack Henry (Episys)"),
    ("lumindigital", "Lumin Digital"),
    ("digitalinsight", "NCR (Digital Insight)"), ("dibill", "NCR (Digital Insight)"),
    ("d3banking", "NCR (D3)"),
    ("narmi", "Narmi"),
    ("corillian", "Fiserv"), ("fiserv", "Fiserv"), ("fisglobal", "FIS"),
    ("apiture", "Apiture"), ("tyfone", "Tyfone"), ("mahalobanking", "Mahalo Banking"),
    ("nymbus", "Nymbus"), ("cu-anytime", "CU Answers"), ("itsme247", "CU Answers"),
    ("bizlink247", "CU Answers"),
    # Cores / platforms with digital front ends
    ("temenos", "Temenos"), ("kony", "Temenos (Kony)"), ("backbase", "Backbase"),
    ("malauzai", "Finastra (Malauzai)"), ("finastra", "Finastra"),
    ("bankjoy", "Bankjoy"), ("alogent", "Alogent"), ("jwaala", "Alogent (Jwaala)"),
    ("accessoftek", "Access Softek"), ("access-softek", "Access Softek"),
    ("sharetec", "Sharetec"), ("datacenterinc", "DCI"), ("dcihub", "DCI"),
    ("cocc.com", "COCC"), ("corelation", "Corelation"),
    ("csiweb", "CSI"), ("nupoint", "CSI (NuPoint)"),
    ("ufstech", "UFS (Navanta)"), ("navanta", "UFS (Navanta)"),
    ("mybankingservices", "Fiserv"),
    # Hosted multi-tenant online-banking login domains. These show up as asset/login
    # markers in provider_hints when a bank's online banking is hosted on the vendor's
    # domain rather than its own. Researched & confirmed as banking-platform hosts
    # (not lending/CMS/card-only) via cert-transparency + vendor evidence.
    ("financial-net.com", "Fiserv"), ("secureinternetbank.com", "Fiserv"),
    ("onlineaccess1.com", "Fiserv"), ("onlinebank.com", "Fiserv"),
    ("mobicint", "Mobicint"),
    # NOTE: deliberately EXCLUDED from HTML matching — MeridianLink, Blend, MANTL,
    # Kasasa, Bottomline, Terafina. These are embedded loan/account-opening/rewards
    # widgets, not the digital-banking platform; their asset appearing on a homepage
    # does NOT mean they run the institution's online banking. (Login-host matches in
    # PROVIDER_DOMAINS remain reliable and are kept.)
]

_POWERED_RE = re.compile(r"powered by ([a-z0-9 .&'-]{2,30})", re.I)


def _html_provider_markers(html: str, base: str) -> list[str]:
    """External asset registered-domains + 'powered by X' snippets from a page."""
    base_dom = _reg_domain(_safe_hostname(base))
    out = set()
    for ref in re.findall(r'(?:src|href)=["\']([^"\']+)["\']', html, re.I):
        u = ref.strip()
        if u.startswith("//"):
            u = "https:" + u
        elif not u.startswith(("http://", "https://")):
            continue  # relative asset — no host to fingerprint
        d = _reg_domain(_safe_hostname(u).lower())
        if d and d != base_dom:
            out.add(d)
    for m in _POWERED_RE.findall(html):
        out.add("poweredby:" + m.strip().lower())
    return sorted(out)[:25]


# ---------------------------------------------------------------------------
# Likely aggregation connection method (open-finance / Mastercard-Finicity view).
# A provider that exposes an FDX/OAuth data API to aggregators can be connected
# token-based (no credentials) → no screen-scraping "tower script" needed.
# Everything else defaults to credential-only (the universal screen-scrape
# fallback) → that's where a credential/tower connection IS required.
#
# HEURISTIC — validate against your own aggregator connectivity data; edit freely.
# Anything not in this set is treated as credential-only.
# ---------------------------------------------------------------------------
API_CAPABLE_PROVIDERS = {
    "Fiserv", "FIS",
    "Jack Henry", "Jack Henry (Banno)", "Jack Henry (NetTeller)",
    "Jack Henry (Symitar)", "Jack Henry (Episys)",
    "Q2", "Alkami", "Apiture", "Narmi", "Lumin Digital", "Bottomline",
    "Backbase", "Temenos", "Temenos (Kony)", "Finastra", "Finastra (Malauzai)",
    "NCR (Digital Insight)", "NCR (D3)", "NCR (Terafina)", "Nymbus",
    "Bankjoy", "Mahalo Banking", "Alogent", "Alogent (Jwaala)", "Tyfone",
}


# Public OAuth/data-exchange rails by digital-banking provider, from PUBLIC sources
# only (FDX membership + publicly-announced Akoya / Plaid Core Exchange (PCX) /
# Fiserv AllData integrations). Keyed by provider base name (variant products share
# rails). HEURISTIC — no proprietary data; edit as the ecosystem changes.
PROVIDER_OAUTH_NETWORKS = {
    "Fiserv": ["FDX", "Fiserv AllData", "Akoya"],
    "FIS": ["FDX", "Akoya"],
    "Jack Henry": ["FDX", "Akoya", "PCX"],
    "Q2": ["FDX"],
    "Alkami": ["FDX"],
    "Apiture": ["FDX"],
    "Narmi": ["FDX"],
    "Lumin Digital": ["FDX"],
    "Bottomline": ["FDX"],
    "Backbase": ["FDX"],
    "Temenos": ["FDX"],
    "Finastra": ["FDX"],
    "NCR": ["FDX"],
    "Nymbus": ["FDX"],
    "Bankjoy": ["FDX"],
    "Mahalo Banking": ["FDX"],
    "Alogent": ["FDX"],
    "Tyfone": ["FDX"],
}


def oauth_networks_for(provider: str) -> list:
    """Public OAuth rails for a provider (variant products share the base's rails)."""
    if not provider:
        return []
    base = provider.split(" (")[0].strip()
    return PROVIDER_OAUTH_NETWORKS.get(provider) or PROVIDER_OAUTH_NETWORKS.get(base, [])


def likely_connection_method(inst: dict) -> tuple[str, str]:
    """Return (method, basis): 'api_oauth' | 'credential' | 'unknown'."""
    prov = inst.get("service_provider", "")
    if prov:
        if prov in API_CAPABLE_PROVIDERS:
            return "api_oauth", f"{prov} exposes an FDX/OAuth aggregator API"
        return "credential", f"{prov} — credential-only (no known aggregator API)"
    status = inst.get("business_coverage_status", "")
    if status in ("", "not_scanned"):
        return "unknown", "website not yet scanned"
    if status == "unreachable":
        return "unknown", "website unreachable — provider undetermined"
    if inst.get("has_business_login") or inst.get("personal_login_url"):
        return "credential", "no API-capable provider identified — login-form scrape"
    return "unknown", "no login portal detected"


def _safe_hostname(url: str) -> str:
    """urlparse(...).hostname but never raises on malformed input."""
    try:
        return (urlparse(url).hostname or "")
    except ValueError:
        return ""


def _reg_domain(host: str) -> str:
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "")


def _entry_login_hosts(entry: dict) -> list[str]:
    hosts = []
    for k in ("business_login_url", "personal_login_url"):
        if entry.get(k):
            hosts.append(_safe_hostname(entry[k]))
    for l in (entry.get("login_portals") or []):
        if l.get("url"):
            hosts.append(_safe_hostname(l["url"]))
    return hosts


def classify_provider(entry: dict) -> str:
    """
    Best-guess digital-banking service provider:
      1. login host → PROVIDER_DOMAINS (vendor-hosted login — strongest signal)
      2. HTML asset/'powered by' hints → HTML_PROVIDER_PATTERNS (white-labeled login)
    """
    if not entry:
        return ""
    for h in _entry_login_hosts(entry):
        prov = PROVIDER_DOMAINS.get(_reg_domain(h))
        if prov:
            return prov
    for hint in (entry.get("provider_hints") or []):
        for needle, prov in HTML_PROVIDER_PATTERNS:
            if needle in hint:
                return prov
    return ""


def inst_key(inst: dict) -> str:
    """Stable cache key for an institution."""
    rssd = (inst.get("rssdid") or "").strip()
    if rssd and rssd != "0":
        return f"rssd:{rssd}"
    if inst.get("source") == "fdic" and inst.get("cert"):
        return f"cert:{inst['cert']}"
    if inst.get("source") == "ncua" and inst.get("charter_number"):
        return f"ncua:{inst['charter_number']}"
    return f"url:{(inst.get('web_address') or '').strip().lower()}"


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

# Curated corporate-domain -> consumer-banking-domain overrides. Some regulatory
# `web_address` values are the holding-company / corporate site (e.g. Chase files
# jpmorganchase.com) rather than the consumer brand where business/SMB accounts and
# login portals actually live. We scrape the consumer domain for signal detection
# while preserving the original `web_address` on the record. There is no credential-
# free source mapping legal entity -> consumer brand, so this is hand-curated; run
# find_url_candidates.py to surface high-impact suspects, then add verified ones here.
# Keyed by registered domain of the regulatory web_address.
CONSUMER_DOMAIN_OVERRIDES = {
    "jpmorganchase.com": "https://www.chase.com",
    # Santander files the GLOBAL group site (santander.com, Banco Santander S.A.);
    # US retail / business banking lives on santanderbank.com.
    "santander.com": "https://www.santanderbank.com",
    # NOTE reviewed (find_url_candidates.py global-or-holding) but deliberately NOT
    # overridden — verified each would not help or would regress:
    #   td.com/us/en/personal-banking already scrapes as the US consumer site (biz+login+FIS);
    #   us.hsbc.com is JS-rendered and reads as no-business, WORSE than HSBC's about page;
    #   bmo.com / pnc.com / key.com are the correct consumer domains, just bot-walled
    #   (an override can't fix a bot wall). Santander was the only genuine win.
}


# ---------------------------------------------------------------------------
# Login-portal precision (audit aid). A real authentication portal is vendor-hosted,
# sits on a login-looking subdomain, or has an auth token in its path — NOT a same-host
# marketing page that merely mentions "online banking" (e.g. BECU's business-banking
# info page got mis-flagged as a business *login*). Used by audit_coverage.py to flag
# `business_login_url`s worth a manual look; deliberately NOT auto-applied to the cached
# signal, since the brand may still have a real portal elsewhere.
# ---------------------------------------------------------------------------
_AUTH_PATH_TOKENS = ("login", "log-in", "logon", "signin", "sign-in", "sso", "webauth",
                     "oauth", "auth", "olb", "oao", "enroll", "onlineserv", "tmconnect")
_LOGIN_SUBDOMAIN_TOKENS = ("secure", "login", "logon", "signin", "online", "digital",
                           "ebank", "banking", "access", "treasury", "connect", "portal",
                           "auth", "sso", "business", "biz", "commercial", "cash")


def _is_auth_portal(login_url: str, home_url: str = "") -> bool:
    """Heuristic: does `login_url` look like a real auth portal vs a marketing page?"""
    if not login_url:
        return False
    host = _safe_hostname(_full_url(login_url)).lower()
    if not host:
        return False
    reg = _reg_domain(host)
    if reg in PROVIDER_DOMAINS:                                  # vendor-hosted portal
        return True
    home_reg = _reg_domain(_safe_hostname(_full_url(home_url)).lower()) if home_url else ""
    sub = host[: -len(reg)].rstrip(".") if reg and host.endswith(reg) else ""
    labels = [l for l in sub.split(".") if l and l != "www"]
    if (reg != home_reg or labels) and any(tok in lab for lab in labels for tok in _LOGIN_SUBDOMAIN_TOKENS):
        return True                                             # login-y subdomain (businessaccess.popular.com)
    try:
        segs = [s for s in urlparse(_full_url(login_url)).path.lower().split("/") if s]
    except ValueError:
        segs = []
    return any(tok == s or (len(tok) > 3 and tok in s) for s in segs for tok in _AUTH_PATH_TOKENS)


def _consumer_scrape_url(orig: str) -> str:
    """Return the consumer-banking URL to scrape for `orig`, or `orig` unchanged."""
    dom = _reg_domain(_safe_hostname(_full_url(orig)))
    return CONSUMER_DOMAIN_OVERRIDES.get(dom, orig)


async def scrape_one(client, sem, inst: dict, today: str) -> dict:
    """Fetch one institution's site and classify business/SMB coverage."""
    url = (inst.get("web_address") or "").strip()
    scrape_url = _consumer_scrape_url(url) if url else url
    base_result = {
        "key": inst_key(inst),
        "name": inst.get("name", ""),
        "url": url,
        "scraped_url": scrape_url if scrape_url != url else "",
        "checked_at": today,
        "v": SCHEMA_V,
        "serves_business": False,
        "serves_smb": False,
        "business_evidence": [],
        "smb_evidence": [],
        "has_business_login": False,
        "distinct_business_login": False,
        "business_login_url": "",
        "personal_login_url": "",
        "login_portals": [],
        "provider_hints": [],
        "pages_checked": [],
        "title": "",
        "reachable": False,
        "http_status": None,
        "note": "",
    }
    if not url:
        base_result["note"] = "no web address on record"
        return base_result

    async with sem:
        try:
            resp = await client.get(_full_url(scrape_url))
        except Exception as e:
            base_result["note"] = f"unreachable ({type(e).__name__})"
            return base_result

    base_result["http_status"] = resp.status_code
    html = resp.text or ""
    base_result["title"] = _page_title(html)
    if resp.status_code >= 400:
        # 4xx/5xx (incl. Cloudflare 5xx like 524 timeouts) — an error page, not a real
        # home. Don't classify off it and don't call it reachable.
        base_result["note"] = f"http {resp.status_code}"
        return base_result
    base_result["reachable"] = True
    final = str(resp.url)
    biz, smb = classify_text(html)
    biz_ev, smb_ev = set(biz), set(smb)
    logins = {l["url"]: l for l in _discover_logins(html, final)}
    pages = [final]

    # One level deep: follow up to 2 business/SMB-looking links for recall.
    for link in _discover_business_links(html, final, cap=2):
        async with sem:
            try:
                r2 = await client.get(link)
            except Exception:
                continue
        b2, s2 = classify_text(r2.text or "")
        biz_ev |= set(b2)
        smb_ev |= set(s2)
        for l in _discover_logins(r2.text or "", str(r2.url)):
            logins.setdefault(l["url"], l)
        pages.append(str(r2.url))

    if smb_ev:
        biz_ev |= {"small business"}  # SMB support implies business support
    login_summary = _summarize_logins(list(logins.values()))

    # Provider fingerprinting. If a login host is already a known vendor domain,
    # the URL alone identifies the provider — skip the extra fetch. Otherwise this
    # is a white-label candidate: fetch the primary login page (where the vendor's
    # JS/CDN assets load) and capture host/'powered by' hints.
    hints = set(_html_provider_markers(html, final))
    url_known = any(PROVIDER_DOMAINS.get(_reg_domain(_safe_hostname(l["url"]))) for l in logins.values())
    if not url_known:
        login_url = login_summary.get("personal_login_url") or login_summary.get("business_login_url")
        if login_url:
            async with sem:
                try:
                    rl = await client.get(login_url)
                    hints |= set(_html_provider_markers(rl.text or "", str(rl.url)))
                    pages.append(str(rl.url))
                except Exception:
                    pass

    base_result.update(
        serves_business=bool(biz_ev),
        serves_smb=bool(smb_ev),
        business_evidence=sorted(biz_ev),
        smb_evidence=sorted(smb_ev),
        pages_checked=pages,
        provider_hints=sorted(hints)[:25],
        **login_summary,
    )
    if not html.strip():
        base_result["note"] = "empty/JS-rendered page — may be unclassifiable"
    return base_result


def load_coverage() -> dict:
    if not COVERAGE_FILE.exists():
        return {}
    try:
        with open(COVERAGE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_coverage(data: dict):
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = COVERAGE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(COVERAGE_FILE)


async def build_business_coverage(
    institutions: list[dict],
    limit: int = 0,
    concurrency: int = 20,
    only_missing: bool = True,
    timeout: float = 10.0,
    checkpoint_every: int = 200,
) -> dict:
    """
    Scrape institutions' home URLs and cache business/SMB coverage.

    Args:
        institutions: records from get_all_institutions().
        limit: max NEW institutions to scrape this run (0 = no cap). Largest
               (by deposit_accounts) are prioritized so partial runs cover the
               most consequential institutions first.
        concurrency: parallel fetches.
        only_missing: skip institutions already in the cache.
        timeout: per-request timeout (seconds).
        checkpoint_every: flush the cache to disk every N completed scrapes
               (0 = save only at the end). Makes a long run crash-resilient —
               combined with only_missing, a re-run resumes where it left off.

    Returns:
        Summary dict; results merged into cache/business_coverage.json.
    """
    cache = load_coverage()
    today = datetime.today().strftime("%Y-%m-%d")

    def dep(i):
        try:
            return int(i.get("deposit_accounts") or 0)
        except (TypeError, ValueError):
            return 0

    def _norm_url(u: str) -> str:
        return (u or "").strip().lower().rstrip("/")

    def _needs_scan(i: dict) -> bool:
        # Delta-driven: scan if never scanned, or the institution's URL changed
        # since it was last scanned (products on a NEW site must be re-read).
        entry = cache.get(inst_key(i))
        if entry is None:
            return True
        if entry.get("v") != SCHEMA_V:        # result shape upgraded — re-scan
            return True
        # A consumer-domain override added/changed shifts the scrape target even when
        # web_address is unchanged — re-scan those (and only those).
        cached_target = entry.get("scraped_url") or entry.get("url")
        if _norm_url(cached_target) != _norm_url(_consumer_scrape_url(i.get("web_address") or "")):
            return True
        return _norm_url(entry.get("url")) != _norm_url(i.get("web_address"))

    pool = [i for i in institutions if (i.get("web_address") or "").strip()]
    if only_missing:
        pool = [i for i in pool if _needs_scan(i)]
    pool.sort(key=dep, reverse=True)
    if limit and limit > 0:
        pool = pool[:limit]

    log(f"[business] scraping {len(pool)} institution home URLs "
        f"(concurrency={concurrency}, checkpoint every {checkpoint_every or 'never'})...")
    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": "Mozilla/5.0 (fi-lookup-mcp business-coverage scan)"}
    results = []
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=httpx.Timeout(timeout), headers=headers, verify=False
    ) as client:
        tasks = [asyncio.ensure_future(scrape_one(client, sem, i, today)) for i in pool]
        try:
            # Consume as each scrape finishes; checkpoint the cache periodically so
            # an interrupted run keeps its progress (re-run resumes via only_missing).
            for fut in asyncio.as_completed(tasks):
                try:
                    r = await fut
                except Exception as e:
                    log(f"[business] one scrape errored ({type(e).__name__}); skipped.")
                    continue
                cache[r["key"]] = r
                results.append(r)
                if checkpoint_every and len(results) % checkpoint_every == 0:
                    save_coverage(cache)
                    log(f"[business] checkpoint: {len(results)}/{len(pool)} scanned, cache saved.")
        finally:
            # Always persist whatever completed, even on cancellation/error.
            for t in tasks:
                if not t.done():
                    t.cancel()
            save_coverage(cache)

    scanned = len(results)
    summary = {
        "scanned_this_run": scanned,
        "total_in_cache": len(cache),
        "serves_business": sum(1 for r in results if r["serves_business"]),
        "serves_smb": sum(1 for r in results if r["serves_smb"]),
        "unreachable": sum(1 for r in results if not r["reachable"]),
        "no_url": sum(1 for r in results if not r["url"]),
        "coverage_file": str(COVERAGE_FILE),
    }
    log(f"[business] done: {summary}")
    return summary


def enrich_institutions(institutions: list[dict]) -> int:
    """
    Merge cached business-coverage flags into in-memory records. Adds:
      serves_business (bool|None), serves_smb (bool|None),
      business_coverage_checked_at (str), business_coverage_status (str).
    None means not yet scraped. Returns the count enriched.
    """
    cache = load_coverage()
    n = 0
    for inst in institutions:
        entry = cache.get(inst_key(inst))
        if entry is None:
            inst["serves_business"] = None
            inst["serves_smb"] = None
            inst["has_business_login"] = None
            inst["distinct_business_login"] = None
            inst["business_login_url"] = ""
            inst["personal_login_url"] = ""
            inst["service_provider"] = ""
            inst["oauth_networks"] = []
            inst["business_coverage_checked_at"] = ""
            inst["business_coverage_status"] = "not_scanned"
            inst["likely_connection_method"], inst["connection_basis"] = likely_connection_method(inst)
            continue
        if not entry.get("reachable"):
            # Site didn't respond — we genuinely don't know its coverage, so report
            # the website signals as unknown (None) rather than asserting "no".
            inst["serves_business"] = None
            inst["serves_smb"] = None
            inst["has_business_login"] = None
            inst["distinct_business_login"] = None
            inst["business_login_url"] = ""
            inst["personal_login_url"] = ""
            inst["service_provider"] = ""
            inst["oauth_networks"] = []
            inst["business_coverage_checked_at"] = entry.get("checked_at", "")
            inst["business_coverage_status"] = "unreachable"
            inst["likely_connection_method"], inst["connection_basis"] = likely_connection_method(inst)
            n += 1
            continue
        inst["serves_business"] = entry["serves_business"]
        inst["serves_smb"] = entry["serves_smb"]
        inst["has_business_login"] = entry.get("has_business_login")
        inst["distinct_business_login"] = entry.get("distinct_business_login")
        inst["business_login_url"] = entry.get("business_login_url", "")
        inst["personal_login_url"] = entry.get("personal_login_url", "")
        inst["service_provider"] = classify_provider(entry)
        inst["oauth_networks"] = oauth_networks_for(inst["service_provider"])
        inst["business_coverage_checked_at"] = entry.get("checked_at", "")
        inst["business_coverage_status"] = "scanned"
        inst["likely_connection_method"], inst["connection_basis"] = likely_connection_method(inst)
        n += 1
    return n
