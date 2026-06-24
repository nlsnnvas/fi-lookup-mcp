# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local **MCP (Model Context Protocol) server** built with FastMCP that exposes 11 tools over public US financial-institution regulatory data (FDIC, NCUA, FFIEC NIC). It resolves messy external records to canonical institutions, traces merger/acquisition lineage, and serves a regulatory change feed. This is a tool-use/reconciliation pattern — deterministic scoring and lookups against a pre-built snapshot — **not RAG**.

There is a small hermetic **pytest** suite in `tests/` (pure-function + convention guards — no snapshot, network, or ZIPs required; run `python -m pytest`); test deps are in `requirements-dev.txt` and CI runs them via `.github/workflows/ci.yml`. No linter or build step is configured. Python 3.11 (`.python-version` pins 3.11.9), runtime deps in `requirements.txt`, venv in `.venv/`.

## Commands

```bash
# Activate environment
source .venv/bin/activate

# Build / rebuild the data snapshot (fetches FDIC live, reads ZIPs, runs NIC enrichment).
# Required before first run. Takes 2-3 min on cold start.
python -c "import asyncio; from data_loader import build_snapshot; asyncio.run(build_snapshot())"

# Run the MCP server directly (stdio transport; lifespan builds the snapshot on startup)
python server.py

# Install into Claude Desktop
fastmcp install claude-desktop server.py --name "fi-lookup"
```

To exercise a single tool without Claude Desktop, import it from `server.py` and `await` it inside an `asyncio.run`, after calling `build_snapshot()` to populate the in-memory snapshot.

## Architecture

```
server.py        — FastMCP app + 11 @mcp.tool() definitions. lifespan() calls build_snapshot() on startup.
data_loader.py   — FDIC API fetch, NCUA ZIP ingestion, FFIEC + SBA + website enrichment, the unified snapshot.
nic_loader.py    — Parses FFIEC NIC bulk ZIPs (transformations, relationships, active+closed name lookup).
reconciler.py    — Name normalization + confidence scoring for reconcile_institution.
sba_loader.py    — Builds the SBA 7(a)/504 small-business-lender index (cache/sba_lenders.json).
business_classifier.py — Scrapes home URLs for advertised business/SMB accounts + business login portals.
web_app.py       — Starlette local web dashboard (FI Explorer) over the snapshot + tools. No new deps.
division_loader.py — per-division coverage: scrapes each `trade_name_urls` entry (reuses business_classifier.scrape_one) → `cache/division_coverage.json`; `enrich_divisions` attaches a `divisions` list (per-division business/SMB/login/provider) to each record. Built by scrape_division_coverage.py — checkpointed + resumable (re-run skips cached URLs).
find_url_candidates.py — ranks likely corporate-URL institutions (large unreachable / zero-signal) to review for CONSUMER_DOMAIN_OVERRIDES.
refresh_sba.py / scrape_business_coverage.py — occasional (heavy) batch enrichers; build the caches the
                   above two modules read cheaply on every snapshot build.
cache/           — Local data snapshots, source ZIPs, and enrichment caches. NOT committed (see .gitignore).
```

### The snapshot is module-global state

`data_loader.py` holds two module-level globals, `_INSTITUTIONS` (list of institution dicts) and `_NIC_NAMES` (RSSD→name lookup including defunct institutions), populated by `build_snapshot()` and read everywhere via `get_all_institutions()` and `get_nic_names()`. Tools must tolerate the snapshot being empty (server still starting) and return an error dict rather than throwing.

### Cold start vs. warm start

`build_snapshot()` reads the JSON caches (`cache/fdic_institutions.json`, `cache/ncua_institutions.json`) if present and skips all network calls — that's a warm start. A cold start (or `force_refresh=True`, triggered by the `refresh_cache` tool) re-fetches FDIC from the BankFind API and re-reads the local ZIPs. **NIC enrichment runs and is written into the JSON cache at save time**, so predecessor/successor/parent/subsidiary fields load instantly on warm starts. The one thing always recomputed regardless of warm/cold is `_NIC_NAMES`, because name resolution needs it and it isn't persisted in the institution records.

The `needs_normalization` check distinguishes raw FDIC API rows (uppercase keys like `CERT`, `NAME`) from already-normalized cached records (`source == "fdic"`); deposit-count fetching is skipped when the cache is already normalized.

### The institution record shape

Every institution is a flat dict with a `source` of `"fdic"` or `"ncua"`. Both sources are normalized to the same keys: `name`, `city`, `state`, `rssdid`, `deposit_accounts`, `web_address`, `aba_routing`, plus `cert` (FDIC) or `charter_number` (NCUA), and the NIC fields `predecessors`, `successors`, `parent_rssd`, `subsidiaries`. RSSD ID is the join key across all FFIEC/NIC data and is stored as a string throughout.

**Trade names / divisions:** a single charter often runs several distinctly-branded banks/divisions, each with its own home/login URL (Zions Bancorporation → Zions Bank, Amegy, CB&T, …) — separate points of entry for open-finance aggregators, invisible to the base record. Captured **credential-free from the regulators**: FDIC fields `TE01N528..TE10N528` (trade-name *URLs*, cap 10) and `TE01N529..TE06N529` (trade *names*, cap 6) — two independent, non-index-aligned lists with different counts (`data_loader._fdic_trade_names`); NCUA `TradeNames.txt` (names only, no URLs; `_clean_trade_names` dedupes + drops the legal name). Stored as `trade_name_urls` / `trade_names`; surfaced as `division_count` + a `has_divisions` filter. The 10-URL cap truncates big multi-brand banks (Glacier has 18 divisions → FDIC lists 10). `DIVISION_OVERFLOW` (keyed by cert) is a tiny hand-verified map that's unioned with the FDIC URLs so capped banks still expose every subsidiary; find truncated banks via `division_count == 10`. In practice only Glacier (cert 30788) is truly over the cap — most "at-cap" banks are single-brand or share one login across branch brands.

### Critical conventions

- **Never print to stdout.** The MCP stdio channel carries JSON; any stray stdout corrupts it. `data_loader.log()` writes to stderr; `nic_loader` uses the `logging` module. Keep all diagnostics off stdout.
- **State format mismatch:** *raw* records still differ — FDIC stores full state names ("Utah"), NCUA stores 2-letter codes ("UT"). `reconciler.score_geo` and the `list_institutions` state filter normalize both directions, so when filtering on the raw records handle both forms. **Output is standardized:** `server._canonical_state()` maps any form (incl. territories) to the 2-letter USPS code, and `_full_record()` applies it — so every emitted record's `state` is the 2-letter code (use `_canonical_state` for any new aggregation rather than reading raw `state`).
- **Atomic cache writes:** caches are written to a `.tmp` file then `os.rename`d to prevent corruption on interruption. Preserve this in any new cache writer.
- **NIC transformation direction is inverted by design:** in a transformation record, this institution's `predecessors` come from events where it is the *successor* (`as_successor`), and its `successors` from events where it is the *predecessor* (`as_predecessor`). See `data_loader.build_snapshot` and `nic_loader.parse_transformations`.

### Data freshness & conditional refresh

FDIC and NCUA self-update to the latest published quarter: FDIC's report date is **auto-discovered** (a `sort_by=REPDTE DESC, limit=1` query in `fetch_latest_fdic_repdte` — do not hardcode it), and the newest NCUA quarterly ZIP is **auto-downloaded** by `ensure_latest_ncua_zip` (newest-quarter-first probe, conditional on local cache). Every record carries a `data_as_of` date; `get_data_as_of()` returns the per-source dates and `_DATA_AS_OF` is repopulated on each build (read from records, so it works on warm starts too).

`refresh_if_changed()` is the cost-effective refresh: `current_source_signature()` fingerprints all sources (ZIP content hashes + latest FDIC REPDTE + latest NCUA tag) against `cache/source_manifest.json`, and only calls `build_snapshot(force_refresh=True)` when something actually advanced — otherwise it returns `changed: False` without reprocessing (no warm build on the no-op path). `refresh_cache()` still always rebuilds. `scheduled_refresh.py` wraps `refresh_if_changed()` for a monthly launchd job (`~/Library/LaunchAgents/com.fi-lookup.monthly-refresh.plist`). FFIEC is **not** auto-fetched — its bulk download is 403-gated to scripts, so its ZIPs are dropped into `cache/` manually and the hash guard rebuilds when they change.

### Business-coverage enrichment (lending, SBA, website, login portals)

Every record carries business-coverage fields from three layers, applied in `build_snapshot` from cheap caches (the heavy builds are separate occasional jobs):

- **Lending (deterministic, complete):** `business_lending` (yes/no) from FDIC `LNCI`+`LNCOMRE` (commercial & industrial + commercial RE) for banks and NCUA member-business loans (`Acct_400A`/`Acct_400A1`) for credit unions; `commercial_loans_000` is the amount. **Do not** use the FDIC `SZ*` fields for small business — they are *securitized* loans, not small-business loans (a corrected earlier mistake).
- **SBA small business (`sba_loader.py` → `cache/sba_lenders.json`):** `sba_lender=yes` for institutions appearing as 7(a) lenders (joined by `bankfdicnumber`/`bankncuanumber` — authoritative) or 504 third-party lenders (matched by normalized name + state). Built by `refresh_sba.py` (downloads large FOIA CSVs; run quarterly). (There is no separate `small_business_lending` field — for banks it duplicated `sba_lender` and for credit unions it duplicated `business_lending`, so it was removed; filter on `sba_lender` or the website signals instead.)
- **Website (`business_classifier.py` → `cache/business_coverage.json`):** `website_business`/`website_small_business` (advertised on the site) and the open-finance signal `has_business_login`/`distinct_business_login`/`business_login_url` (a separate business sign-in URL = a distinct authenticated entry point for aggregators). Plain-HTTP keyword/anchor scraping, no headless browser. Built by `scrape_business_coverage.py` — **delta-driven**: `only_missing` re-scans only new institutions, URL changes, an override change, or a `SCHEMA_V` bump; checkpoints every N scrapes so a full backfill is crash-resilient and resumable. **Corporate-vs-consumer URL:** some regulatory `web_address` values are the holding-company site (Chase files `jpmorganchase.com`, not `chase.com`), which skews the scrape; `CONSUMER_DOMAIN_OVERRIDES` (keyed by registered domain) makes the scraper hit the consumer site instead while preserving the original `web_address` (the override target is recorded as `scraped_url`). The map is hand-curated — `find_url_candidates.py` ranks high-impact suspects (large unreachable / zero-signal banks) to review and add. No credential-free source maps legal entity → consumer brand, so it stays a curated short list.
- **Provider / open-finance (inferred, `business_classifier.classify_provider`):** `service_provider` (digital-banking platform) is resolved from (1) login-host → `PROVIDER_DOMAINS` (strongest), then (2) HTML asset / "powered by" markers → `HTML_PROVIDER_PATTERNS`. `likely_connection_method` (`api_oauth`/`credential`/`unknown`) comes from `API_CAPABLE_PROVIDERS`, `oauth_networks` from `PROVIDER_OAUTH_NETWORKS` (public FDX/Akoya/PCX rails), `connection_basis` is the reason. **False-positive discipline:** embedded loan/account-opening/rewards widgets (MeridianLink, Blend, MANTL, Kasasa, Bottomline, Terafina) are deliberately EXCLUDED from `HTML_PROVIDER_PATTERNS` — an asset on a homepage ≠ the bank's banking platform; only confirmed multi-tenant *banking* hosts are added. New patterns apply on the next snapshot build with no re-scrape (`classify_provider` re-runs in `enrich_institutions`).

**Honesty conventions:** lending ≠ deposit accounts; website signals are advertised/best-effort (JS-only login widgets read as unknown). `_yn()` in `server.py` maps True/False/None → yes/no/unknown (None = website not yet scanned **or unreachable** — `enrich_institutions` reports website signals as unknown, never "no", when the site didn't respond, so a blocked/failed scrape isn't mistaken for a real negative). These distinctions are surfaced in `list_institutions` fields and the web dashboard footer.

### Reconciliation scoring (`reconciler.py`)

Confidence = 0.6 × name score + 0.4 × geo score, unless an exact identifier (cert/charter/RSSD) is supplied and matches → confidence forced to 1.0. Name score blends `fuzz.token_set_ratio` (0.7) and Jaro-Winkler (0.3) over abbreviation-expanded, punctuation-stripped names (`ABBREV_MAP` handles FCU→federal credit union, Mtn→mountain, N.A.→national association, etc.). Candidates scoring below 0.35 on name are dropped early for speed.

## Local data setup

`cache/` is gitignored. FDIC is fetched live, but five ZIPs must be downloaded manually before a cold build (see README "Local Data Setup" for source URLs):

- `call-report-data-*.zip` — NCUA quarterly (credit unions; deposits from FS220A, web from FS220D)
- `CSV_ATTRIBUTES_ACTIVE.zip` — FFIEC NIC, ABA routing numbers + active names
- `CSV_ATTRIBUTES_CLOSED.zip` — FFIEC NIC, historical names for defunct institutions
- `CSV_TRANSFORMATIONS.zip` — FFIEC NIC, merger/acquisition/failure events
- `CSV_RELATIONSHIPS.zip` — FFIEC NIC, parent/subsidiary structure

Loaders glob for these by name and degrade gracefully (logged warning, empty data) when a file is absent — so a missing ZIP silently disables a feature rather than crashing. When debugging "empty history/routing" issues, check the ZIPs are present first.
