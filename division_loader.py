"""
division_loader.py

Per-division business-coverage scrape. Each distinctly-branded division a charter
operates (from `trade_name_urls`) is a separate point of entry for an open-finance
aggregator, so each gets its own coverage scan — does THIS division advertise
business/SMB accounts, does it have a business login portal, what provider powers it.

Reuses business_classifier.scrape_one (plain-HTTP, no headless browser) and mirrors
the delta-driven, checkpointed pattern of scrape_business_coverage. Heavy — run as an
occasional job; results cache to cache/division_coverage.json and are merged into the
snapshot by enrich_divisions() on every build.
"""
import asyncio
import json
import os
from datetime import date
from pathlib import Path

import httpx

import business_classifier as bc
from data_loader import log

CACHE_DIR = Path(__file__).parent / "cache"
DIVISION_COVERAGE_FILE = CACHE_DIR / "division_coverage.json"


_GENERIC = {"bank", "trust", "national", "state", "first", "community", "company",
            "the", "of", "and", "financial", "federal", "savings", "co", "na", "nta"}


def _domain_stem(url: str) -> str:
    h = (url or "").lower().split("://")[-1].lstrip("/").split("/")[0]
    if h.startswith("www."):
        h = h[4:]
    parts = h.split(".")
    return parts[0] if len(parts) >= 2 else h


def _alnum(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def pair_names(urls: list, names: list) -> dict:
    """Best-effort map division URL -> FDIC trade name. The two FDIC lists aren't
    index-aligned, so match each name to a URL by domain similarity (each name used
    once). Conservative — leaves a URL nameless rather than guess wrong."""
    from rapidfuzz import fuzz
    pool, out = list(names), {}
    for url in urls:
        stem = _alnum(_domain_stem(url))
        if not stem:
            continue
        best, score = None, 0
        for n in pool:
            na = _alnum(n)
            if stem in na or na in stem:                       # one contains the other
                s = 100
            else:
                # distinctive (non-generic) word of the name embedded in the domain stem
                words = [w for w in (_alnum(x) for x in n.lower().split()) if w not in _GENERIC and len(w) >= 4]
                s = max([90 for w in words if w in stem] or [fuzz.partial_ratio(stem, na)])
            if s > score:
                best, score = n, s
        if best and score >= 85:
            out[url] = best
            pool.remove(best)
    return out


import re

_BANKISH = re.compile(r"\b(bank|credit union|financial|trust|savings|federal|fcu)\b", re.I)
# The brand is the segment that ENDS with a bank-type word ("...Country Club Bank"),
# not a tagline that merely contains "financial" ("...trusted financial partner").
_BANK_SUFFIX = re.compile(r"\b(bank|trust|credit union|bancorp|bancshares|savings|n\.?a\.?|fcu)\.?$", re.I)
_SEP = re.compile(r"\s+[|·•—–:]\s+|\s+-\s+|\s*[|·•]\s*")
_BOILER = re.compile(
    r"\b(welcome to|home\s*page|homepage|online banking|personal banking|business banking|"
    r"online & mobile banking|mobile banking|internet banking|personal|business|consumer|"
    r"online|mobile|digital|home|login|log in|sign in|official site|website|site)\b", re.I)


def clean_name(title: str) -> str:
    """Extract a brand name from a scraped page title (best-effort)."""
    t = (title or "").strip()
    if not t or _DEAD_TITLE.search(t):            # challenge / error / stub / parked pages
        return ""
    segs = [s.strip() for s in _SEP.split(t) if s.strip()]
    # prefer the (shortest) segment ending in a bank-type word; else any bank-ish
    # segment; else the first segment.
    suffix = sorted((s for s in segs if _BANK_SUFFIX.search(s)), key=len)
    cand = suffix[0] if suffix else next((s for s in segs if _BANKISH.search(s)), segs[0] if segs else t)
    cand = _BOILER.sub("", cand)
    cand = re.sub(r"\s{2,}", " ", cand).strip(" -|·•—–:,&")
    # reject results that are really just the domain/URL (some sites title = hostname)
    if not cand or re.search(r"https?://|www\.|\.(com|org|net|bank|us|co)\b", cand, re.I):
        return ""
    return cand[:60]


def derive_name(url: str) -> str:
    """Last-resort name from the domain stem (e.g. gnty.com -> 'Gnty')."""
    stem = _domain_stem(url)
    return (stem[:1].upper() + stem[1:]) if stem else ""


def _host(u: str) -> str:
    from urllib.parse import urlparse
    try:
        return (urlparse(u if u.startswith(("http://", "https://")) else "https://" + u).hostname or "").lower()
    except ValueError:
        return ""


def _reg(h: str) -> str:
    p = (h or "").split(".")
    return ".".join(p[-2:]) if len(p) >= 2 else (h or "")


_LOGIN_KW = re.compile(r"(secure|login|logon|signin|sign-?in|sso|webauth|onlinebank|ebank|olui)", re.I)
_LOGIN_FINAL_PATH = re.compile(r"/(auth|login|logon|signin|sign-in)\b", re.I)
# Error/stub/parked page titles — a consumed or dead domain that still returns 200
# (e.g. UMB's absorbed HTLF divisions serve an "Invalid URL" stub).
_DEAD_TITLE = re.compile(
    r"(invalid url|bad request|forbidden|\b40[34]\b|not found|account suspended|"
    r"domain (is )?(for sale|parked|expired)|this domain|under construction|coming soon|"
    r"just a moment|site can'?t be reached|\berror\b|\bdns\b|page not found)", re.I)


def _login_host(host: str) -> bool:
    parts = (host or "").split(".")
    subs = parts[:-2] if len(parts) >= 2 else []
    return any(_LOGIN_KW.search(s) for s in subs)


def _norm_host(h: str) -> str:
    h = (h or "").lower()
    return h[4:] if h.startswith("www.") else h


def is_real_division(url: str, entry: dict, parent_home: str) -> bool:
    """A trade-name URL is a real, distinct division HOME only if it isn't a login
    portal, an error/stub page, a duplicate of the parent's own URL, or a redirect
    that bounces to the parent's home. Uses the scrape's final URL (pages_checked[0])."""
    if _login_host(_host(url)):                        # e.g. securelogin.synchronybank.com
        return False
    if entry and _DEAD_TITLE.search(entry.get("title") or ""):
        return False                                   # 200 stub of a consumed/dead domain
    parent_host, parent_reg = _norm_host(_host(parent_home)), _reg(_host(parent_home))
    if parent_host and _norm_host(_host(url)) == parent_host:
        return False                                   # same URL as the parent (duplicate record)
    final = (entry.get("pages_checked") or [url])[0] if entry else url
    fh = _host(final)
    if _login_host(fh) or _LOGIN_FINAL_PATH.search(final):
        return False                                   # e.g. jpmorgan.chase.com -> secure.chase.com/auth
    if parent_reg and _reg(fh) == parent_reg and _reg(_host(url)) != parent_reg:
        return False                                   # e.g. wf.com -> wellsfargo.com (the parent's home)
    return True


def _key(url: str) -> str:
    """Normalize a URL to a stable cache key (drop scheme / www / trailing slash)."""
    u = (url or "").strip().lower()
    for p in ("https://", "http://"):
        if u.startswith(p):
            u = u[len(p):]
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def load_division_coverage() -> dict:
    if not DIVISION_COVERAGE_FILE.exists():
        return {}
    with open(DIVISION_COVERAGE_FILE) as f:
        return json.load(f)


def save_division_coverage(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = DIVISION_COVERAGE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, DIVISION_COVERAGE_FILE)


def _all_division_urls(institutions: list[dict]) -> list[tuple]:
    """Unique (url, owning-institution name) pairs across all trade_name_urls."""
    seen, out = set(), []
    for inst in institutions:
        for url in (inst.get("trade_name_urls") or []):
            k = _key(url)
            if k and k not in seen:
                seen.add(k)
                out.append((url, inst.get("name", "")))
    return out


async def build_division_coverage(institutions: list[dict], concurrency: int = 12,
                                  timeout: float = 12.0, only_missing: bool = True,
                                  checkpoint_every: int = 200, limit: int = 0) -> dict:
    """Scrape each distinct division URL and cache its coverage (keyed by normalized URL)."""
    cache = load_division_coverage()
    today = date.today().isoformat()
    pool = [(u, owner) for (u, owner) in _all_division_urls(institutions)
            if not (only_missing and cache.get(_key(u), {}).get("v") == bc.SCHEMA_V)]
    if limit and limit > 0:
        pool = pool[:limit]

    log(f"[divisions] scraping {len(pool)} division URLs (concurrency={concurrency})...")
    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": "Mozilla/5.0 (fi-lookup-mcp division-coverage scan)"}
    results = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(timeout),
                                 headers=headers, verify=False) as client:
        async def one(url, owner):
            syn = {"web_address": url, "name": owner, "source": "fdic", "cert": ""}
            r = await bc.scrape_one(client, sem, syn, today)
            r["division_url"] = url
            r["owner"] = owner
            return r

        tasks = [asyncio.ensure_future(one(u, o)) for (u, o) in pool]
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    r = await fut
                except Exception as e:
                    log(f"[divisions] one scrape errored ({type(e).__name__}); skipped.")
                    continue
                cache[_key(r["division_url"])] = r
                results += 1
                if checkpoint_every and results % checkpoint_every == 0:
                    save_division_coverage(cache)
                    log(f"[divisions] checkpoint: {results}/{len(pool)} scanned.")
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            save_division_coverage(cache)
    log(f"[divisions] done: {results} scanned, {len(cache)} in cache.")
    return {"scanned_this_run": results, "total_in_cache": len(cache)}


# Curated division ADDITIONS (cert -> [{name, url}]) for real, distinctly-branded
# divisions FDIC doesn't list. Added verbatim (bypassing the login/redirect/reachable
# filters), since they're explicitly wanted even when the brand's URL is a portal.
DIVISION_ADDITIONS = {
    "3511": [{"name": "Wells Fargo Vantage", "url": "https://vantage.wellsfargo.com"}],  # ex-Commercial Electronic Office
}


def enrich_divisions(institutions: list[dict]) -> int:
    """Attach a `divisions` list (per-division coverage) to records with trade-name URLs."""
    cache = load_division_coverage()
    n = 0
    for inst in institutions:
        additions = DIVISION_ADDITIONS.get(inst.get("cert", ""), [])
        add_name = {a["url"]: a["name"] for a in additions}
        urls = inst.get("trade_name_urls") or []
        if not urls and not additions:
            inst["divisions"] = []
            continue
        # Drop URLs that (after redirect) are login portals, bounce to the parent's own
        # home, or are unreachable (dead/consumed) — not real division HOME pages. Keep
        # trade_name_urls in sync so division_count matches.
        parent_home = inst.get("web_address", "")
        urls = [u for u in urls
                if is_real_division(u, cache.get(_key(u)) or {}, parent_home)
                and (cache.get(_key(u)) or {}).get("reachable") is not False]
        for a in additions:                            # curated, always included
            if a["url"] not in urls:
                urls.append(a["url"])
        seen = set()                                   # dedupe any URLs that normalize the same
        urls = [u for u in urls if _key(u) not in seen and not seen.add(_key(u))]
        inst["trade_name_urls"] = urls
        if not urls:
            inst["divisions"] = []
            continue
        name_by_url = pair_names([u for u in urls if u not in add_name], inst.get("trade_names") or [])
        divs = []
        for url in urls:
            e = cache.get(_key(url)) or {}
            # Tiered name (every division gets one): curated -> FDIC trade name -> scraped
            # page title -> domain-derived. name_source flags the quality tier.
            if url in add_name:
                nm, src = add_name[url], "curated"
            elif name_by_url.get(url):
                nm, src = name_by_url[url], "fdic"
            elif (site := clean_name(e.get("title", ""))):
                nm, src = site, "site"
            else:
                nm, src = derive_name(url), "derived"
            divs.append({
                "url": url,
                "name": nm,
                "name_source": src,
                "reachable": e.get("reachable"),
                "serves_business": e.get("serves_business"),
                "serves_smb": e.get("serves_smb"),
                "has_business_login": e.get("has_business_login"),
                "business_login_url": e.get("business_login_url", ""),
                "service_provider": bc.classify_provider(e) if e else "",
            })
        inst["divisions"] = divs
        n += 1
    return n
