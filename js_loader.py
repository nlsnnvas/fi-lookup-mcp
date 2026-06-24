"""
js_loader.py — OPTIONAL Playwright JS-render tier.

For the high-value sites plain-HTTP scraping can't read — JavaScript-rendered
content, or bot walls a real browser gets through — render them with headless
Chromium and run the SAME classification as the normal scraper. Playwright is
wrapped in an httpx-response-compatible adapter, so business_classifier.scrape_one
works unchanged: only the *fetch* is swapped, every extractor is reused.

HEAVY + OPTIONAL. Needs `pip install -r requirements-js.txt && python -m playwright
install chromium`. Scoped to a small, deposit-ranked high-value subset (unreachable
or zero-signal banks) — NOT the whole dataset. Writes into cache/business_coverage.json
(the same cache the normal scraper + enrich_institutions use); entries it renders are
tagged `js: true`. Resumable: checkpointed, and re-running skips already JS-scanned
entries (so a crash/Ctrl-C/sleep loses nothing).
"""
import asyncio
from datetime import date

import business_classifier as bc
from business_classifier import SCHEMA_V, inst_key, load_coverage, save_coverage, scrape_one
from data_loader import log

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class _Resp:
    """Minimal httpx-response shim: scrape_one only touches .text / .url / .status_code."""
    def __init__(self, text, url, status_code):
        self.text, self.url, self.status_code = text, url, status_code


class PlaywrightClient:
    """An object with `async get(url)` like httpx.AsyncClient, backed by a page render."""
    def __init__(self, context, nav_timeout=20000, settle_ms=3500):
        self._ctx, self._nav, self._settle = context, nav_timeout, settle_ms

    async def get(self, url):
        page = await self._ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=self._nav)
            try:                                   # let client-side JS settle
                await page.wait_for_load_state("networkidle", timeout=self._settle)
            except Exception:
                pass
            return _Resp(await page.content(), page.url, resp.status if resp else 0)
        finally:
            await page.close()


def _dep(i):
    try:
        return int(i.get("deposit_accounts") or 0)
    except (TypeError, ValueError):
        return 0


def _is_candidate(i: dict) -> bool:
    """High-value JS targets: unreachable, or scanned with zero business signal."""
    st = i.get("business_coverage_status", "")
    if st == "unreachable":
        return True
    if st == "scanned":
        return (i.get("serves_business") is not True and i.get("serves_smb") is not True
                and not i.get("has_business_login") and not i.get("service_provider"))
    return False


async def build_js_coverage(institutions: list[dict], limit: int = 150, concurrency: int = 3,
                            only_missing: bool = True, checkpoint_every: int = 25) -> dict:
    """Render the highest-value blocked/JS sites and update business_coverage.json."""
    cache = load_coverage()
    today = date.today().isoformat()
    pool = [i for i in institutions if (i.get("web_address") or "").strip() and _is_candidate(i)]
    if only_missing:
        pool = [i for i in pool if not cache.get(inst_key(i), {}).get("js")]
    pool.sort(key=_dep, reverse=True)              # biggest institutions first
    if limit and limit > 0:
        pool = pool[:limit]

    log(f"[js] rendering {len(pool)} high-value sites with headless Chromium "
        f"(concurrency={concurrency})...")
    from playwright.async_api import async_playwright   # lazy: optional dependency

    page_sem = asyncio.Semaphore(concurrency)          # bounds total concurrent renders
    done = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=_UA)
        client = PlaywrightClient(ctx)

        async def _one(inst):
            try:
                r = await scrape_one(client, page_sem, inst, today)
                r["js"] = True                         # tag as browser-rendered
                return r
            except Exception as e:
                log(f"[js] {inst.get('name','?')}: {type(e).__name__}; skipped")
                return None

        tasks = [asyncio.ensure_future(_one(i)) for i in pool]
        try:
            for fut in asyncio.as_completed(tasks):
                r = await fut
                if not r:
                    continue
                cache[r["key"]] = r
                done += 1
                if checkpoint_every and done % checkpoint_every == 0:
                    save_coverage(cache)
                    log(f"[js] checkpoint: {done}/{len(pool)} rendered, cache saved.")
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            save_coverage(cache)
            await browser.close()

    recovered = sum(1 for r in (cache.get(inst_key(i)) for i in pool)
                    if r and r.get("js") and (r.get("serves_business") or r.get("has_business_login")
                                              or bc.classify_provider(r)))
    log(f"[js] done: {done} rendered, {recovered} now show a business/login/provider signal.")
    return {"rendered": done, "recovered_signal": recovered}
