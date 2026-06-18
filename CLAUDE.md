# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local **MCP (Model Context Protocol) server** built with FastMCP that exposes 10 tools over public US financial-institution regulatory data (FDIC, NCUA, FFIEC NIC). It resolves messy external records to canonical institutions, traces merger/acquisition lineage, and serves a regulatory change feed. This is a tool-use/reconciliation pattern — deterministic scoring and lookups against a pre-built snapshot — **not RAG**.

There is no test suite, linter, or build step configured. Python 3.11 (`.python-version` pins 3.11.9), dependencies in `requirements.txt`, venv in `.venv/`.

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
server.py        — FastMCP app + 10 @mcp.tool() definitions. lifespan() calls build_snapshot() on startup.
data_loader.py   — FDIC API fetch, NCUA ZIP ingestion, FFIEC enrichment, the unified in-memory snapshot.
nic_loader.py    — Parses FFIEC NIC bulk ZIPs (transformations, relationships, active+closed name lookup).
reconciler.py    — Name normalization + confidence scoring for reconcile_institution.
cache/           — Local data snapshots + manually-downloaded source ZIPs. NOT committed (see .gitignore).
```

### The snapshot is module-global state

`data_loader.py` holds two module-level globals, `_INSTITUTIONS` (list of institution dicts) and `_NIC_NAMES` (RSSD→name lookup including defunct institutions), populated by `build_snapshot()` and read everywhere via `get_all_institutions()` and `get_nic_names()`. Tools must tolerate the snapshot being empty (server still starting) and return an error dict rather than throwing.

### Cold start vs. warm start

`build_snapshot()` reads the JSON caches (`cache/fdic_institutions.json`, `cache/ncua_institutions.json`) if present and skips all network calls — that's a warm start. A cold start (or `force_refresh=True`, triggered by the `refresh_cache` tool) re-fetches FDIC from the BankFind API and re-reads the local ZIPs. **NIC enrichment runs and is written into the JSON cache at save time**, so predecessor/successor/parent/subsidiary fields load instantly on warm starts. The one thing always recomputed regardless of warm/cold is `_NIC_NAMES`, because name resolution needs it and it isn't persisted in the institution records.

The `needs_normalization` check distinguishes raw FDIC API rows (uppercase keys like `CERT`, `NAME`) from already-normalized cached records (`source == "fdic"`); deposit-count fetching is skipped when the cache is already normalized.

### The institution record shape

Every institution is a flat dict with a `source` of `"fdic"` or `"ncua"`. Both sources are normalized to the same keys: `name`, `city`, `state`, `rssdid`, `deposit_accounts`, `web_address`, `aba_routing`, plus `cert` (FDIC) or `charter_number` (NCUA), and the NIC fields `predecessors`, `successors`, `parent_rssd`, `subsidiaries`. RSSD ID is the join key across all FFIEC/NIC data and is stored as a string throughout.

### Critical conventions

- **Never print to stdout.** The MCP stdio channel carries JSON; any stray stdout corrupts it. `data_loader.log()` writes to stderr; `nic_loader` uses the `logging` module. Keep all diagnostics off stdout.
- **State format mismatch:** FDIC uses full state names ("Utah"), NCUA uses 2-letter codes ("UT"). Several tools and `reconciler.score_geo` normalize both directions — when adding state filtering, handle both forms (there are repeated `state_full_map` / `STATE_FULL` dicts; match the existing pattern).
- **Atomic cache writes:** caches are written to a `.tmp` file then `os.rename`d to prevent corruption on interruption. Preserve this in any new cache writer.
- **NIC transformation direction is inverted by design:** in a transformation record, this institution's `predecessors` come from events where it is the *successor* (`as_successor`), and its `successors` from events where it is the *predecessor* (`as_predecessor`). See `data_loader.build_snapshot` and `nic_loader.parse_transformations`.

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
