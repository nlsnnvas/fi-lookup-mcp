# fi-lookup-mcp

A personal portfolio project demonstrating a **tool-use, reconciliation, and lineage-tracing pattern** over public regulatory data, implemented as a local MCP (Model Context Protocol) server connected to Claude Desktop.

Built by Nelson Anievas. Public data only — no proprietary or employer systems involved.

---

## What It Does

This server exposes 9 tools that allow an AI agent to resolve, enrich, and track the history of US financial institution records using canonical regulatory identifiers from FDIC, NCUA, and FFIEC public datasets.

The server handles three distinct patterns:

- **Reconciliation**: given a dirty external record (e.g. `"Mtn America FCU, Sandy UT"`), return ranked candidate matches with confidence scores and match reasons
- **Lineage tracing**: given an RSSD ID, return the full merger, acquisition, rebrand, and consolidation history — predecessors, successors, parent company, and subsidiaries — with real names resolved across 223,750 active and historical institutions
- **Change feed**: return all transformation events (mergers, failures, rebrands, splits) within a configurable lookback window, filterable by institution type, event type, and state — for dataset maintenance and regulatory monitoring

---

## Tools

### `search_institutions`
Free-text name search across all FDIC banks and NCUA credit unions. Supports filtering by institution type and state. Returns ranked candidates with fuzzy match scores.

### `get_institution_profile`
Full regulatory profile lookup by any identifier — FDIC cert, NCUA charter number, or RSSD ID. Returns all available metadata including regulator, charter type, ABA routing number, deposit account count, and web address.

### `reconcile_institution`
The centerpiece reconciliation tool. Takes a messy external record (name, optional city/state/identifier) and returns ranked candidate matches, each with a confidence score (0–1) and human-readable match reasons.

Scoring blends:
- **Name similarity** (0.6 weight): token-set ratio + Jaro-Winkler, with abbreviation expansion (FCU → federal credit union, Mtn → mountain, N.A. → national association)
- **Geographic agreement** (0.4 weight): state match (0.6) + city match (0.4)
- **Exact identifier override**: if a cert, charter, or RSSD is provided and matches, confidence is set to 1.0

### `crosswalk_identifiers`
Translates between FDIC cert, NCUA charter number, and RSSD ID. Explains regulatory boundaries (e.g. why a credit union has no FDIC cert).

### `get_institution_history`
Returns the full merger, acquisition, and rebrand lineage for any institution by RSSD ID. Resolves real names for both active and defunct predecessor/successor institutions using a 223,750-record historical name lookup built from FFIEC NIC active and closed attributes files. Includes parent company and subsidiary relationships.

Example output for JPMorgan Chase (RSSD 852218): 52 predecessors including Washington Mutual (FDIC-assisted, 2008), Bank One (merger, 2004), and Bear Stearns entities — all with resolved names and dates.

### `get_recent_changes`
A configurable regulatory change feed built from FFIEC NIC Transformations data. Returns mergers, failures, rebrands, splits, and other structural events within a lookback window. Useful for identifying institutions that have changed status and may need dataset updates.

Parameters:
- `days`: lookback window (default 365, max 3650)
- `institution_type`: `"bank"`, `"cu"`, or `"all"`
- `event_type`: `"merger"`, `"failure"`, `"split"`, `"rebrand"`, or `"all"`
- `state`: optional 2-letter state filter

### `get_top_institutions`
Returns the top N institutions ranked by deposit account count, with individual and cumulative market share percentages. Supports filtering by institution type.

### `export_institutions`
Exports the full institution dataset to a CSV file with configurable filters, sorting, and market share calculations.

### `refresh_cache`
Rebuilds the local data snapshot from scratch — re-fetches FDIC data from the BankFind API and re-reads all local ZIPs. Runs the full NIC enrichment pipeline on refresh.

---

## Data Sources

All data is public regulatory data. No licensed or proprietary sources.

| Source | Data | Refresh |
|--------|------|---------|
| FDIC BankFind API | ~4,274 active banks: name, location, cert, RSSD, web address | API call |
| FDIC Financials API | Deposit account counts from most recent quarter | API call |
| NCUA Quarterly ZIP | ~4,336 active credit unions; deposit counts from FS220A; web addresses from FS220D | Manual download |
| FFIEC NIC Active Attributes | ABA primary routing numbers; joined via RSSD/cert/charter | Manual download |
| FFIEC NIC Closed Attributes | Historical institution names for 161,950 defunct entities | Manual download |
| FFIEC NIC Transformations | 59,071 merger/acquisition/rebrand/failure events | Manual download |
| FFIEC NIC Relationships | Parent/subsidiary/branch ownership structure | Manual download |

**Total universe: 8,610 active institutions + 223,750 name-resolved historical records**

---

## Architecture
Claude Desktop

|

|  MCP stdio transport

v

server.py  (FastMCP 3.4.2)

|

+-- search_institutions

+-- get_institution_profile

+-- reconcile_institution     -->  reconciler.py

+-- crosswalk_identifiers

+-- get_institution_history   -->  nic_names lookup (223,750 records)

+-- get_recent_changes        -->  CSV_TRANSFORMATIONS.zip

+-- get_top_institutions

+-- export_institutions

+-- refresh_cache

|

v

data_loader.py            nic_loader.py

|                        |

+-- cache/fdic_institutions.json     (NIC-enriched)

+-- cache/ncua_institutions.json     (NIC-enriched)

+-- cache/call-report-data-*.zip

+-- cache/CSV_ATTRIBUTES_ACTIVE.zip

+-- cache/CSV_ATTRIBUTES_CLOSED.zip

+-- cache/CSV_TRANSFORMATIONS.zip

+-- cache/CSV_RELATIONSHIPS.zip

Key design decisions:
- **Local cache first**: runs fully offline after initial build; warm start skips live API calls
- **NIC enrichment at save time**: predecessor/successor/parent/subsidiary fields are written into the JSON cache so subsequent warm starts load enriched data instantly
- **Atomic cache writes**: `.tmp` rename pattern prevents corruption on interrupted writes
- **Stderr-only logging**: never pollutes the MCP stdio JSON channel
- **Abbreviation-aware normalization**: improves recall on dirty external records

---

## Local Data Setup

The `cache/` directory is **not committed to Git** — populate it manually before first run.

### Required downloads

| File | Source |
|------|--------|
| `cache/call-report-data-*.zip` | [NCUA Quarterly Call Report Data](https://www.ncua.gov/analysis/credit-union-corporate-call-report-data/call-report-data-for-download) |
| `cache/CSV_ATTRIBUTES_ACTIVE.zip` | [FFIEC NIC Data Download](https://www.ffiec.gov/npw/FinancialReport/DataDownload) — Active Attributes |
| `cache/CSV_ATTRIBUTES_CLOSED.zip` | [FFIEC NIC Data Download](https://www.ffiec.gov/npw/FinancialReport/DataDownload) — Closed Attributes |
| `cache/CSV_TRANSFORMATIONS.zip` | [FFIEC NIC Data Download](https://www.ffiec.gov/npw/FinancialReport/DataDownload) — Transformations |
| `cache/CSV_RELATIONSHIPS.zip` | [FFIEC NIC Data Download](https://www.ffiec.gov/npw/FinancialReport/DataDownload) — Relationships |

FDIC data is fetched live from the [FDIC BankFind API](https://banks.data.fdic.gov/docs/) — no manual download needed.

---

## Setup

### Prerequisites
- Python 3.11+
- Claude Desktop

### Install

```bash
git clone https://github.com/nlsnnvas/fi-lookup-mcp.git
cd fi-lookup-mcp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Download manual data files

Download the five ZIPs listed in the table above and place them in `cache/`. FFIEC files are available at [ffiec.gov/npw/FinancialReport/DataDownload](https://www.ffiec.gov/npw/FinancialReport/DataDownload).

### Build the data snapshot

```bash
python -c "import asyncio; from data_loader import build_snapshot; asyncio.run(build_snapshot())"
```

This fetches FDIC data live, reads all local ZIPs, runs NIC enrichment, and writes the JSON cache. Expect 2–3 minutes on first run.

### Connect to Claude Desktop

```bash
fastmcp install claude-desktop server.py --name "fi-lookup"
```

Then restart Claude Desktop.

---

## Example Interactions

**Reconciliation:**
> "I have a vendor row that says 'Mtn America FCU, Sandy UT' — what is it?"

`reconcile_institution` scores ~8,610 institutions and returns Mountain America Credit Union (NCUA #24692) at 0.984 confidence, with ABA routing, deposit account count, and charter type.

**Lineage tracing:**
> "What is the full acquisition history of Bank of America?"

`get_institution_history` returns 117 predecessor institutions going back to 1960, including the 1998 BankAmerica merger, the 2008 Countrywide acquisition, and the 2009 Merrill Lynch absorption — all with resolved names and dates.

**Change feed:**
> "What bank failures and mergers happened in the last 90 days?"

`get_recent_changes` returns 108 events grouped by type: 1 FDIC-assisted failure, 107 mergers — including Meadows Bank absorbed by AMERICA FIRST Credit Union and two bank-to-credit-union conversions.

---

## Why This Pattern Matters

Financial institution data is notoriously messy. The patterns here are directly applicable to:

- Matching vendor/counterparty records to a canonical institution master
- Tracing merger lineage for compliance, KYC, or data governance
- Building regulatory change feeds for dataset maintenance automation
- Enriching internal datasets with public regulatory metadata
- Onboarding automation that maps free-text institution names to stable IDs

This project re-expresses reconciliation and lineage patterns from production AI agent work, using only public data.

---

## Stack

- Python 3.11
- FastMCP 3.4.2
- rapidfuzz (fuzzy string matching)
- httpx (async HTTP)
- Claude Desktop (MCP host)

---

## Framing Note

This is a **tool-use, reconciliation, and lineage-tracing pattern** — not RAG. The model calls structured tools that execute deterministic scoring and lookup logic against a pre-built regulatory snapshot and return ranked, explainable results. The NIC enrichment pipeline runs at startup and writes enriched data to the JSON cache, so subsequent tool calls are fast in-memory lookups.
