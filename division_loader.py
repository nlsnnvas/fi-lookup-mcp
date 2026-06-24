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


def enrich_divisions(institutions: list[dict]) -> int:
    """Attach a `divisions` list (per-division coverage) to records with trade-name URLs."""
    cache = load_division_coverage()
    n = 0
    for inst in institutions:
        urls = inst.get("trade_name_urls") or []
        if not urls:
            inst["divisions"] = []
            continue
        divs = []
        for url in urls:
            e = cache.get(_key(url)) or {}
            divs.append({
                "url": url,
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
