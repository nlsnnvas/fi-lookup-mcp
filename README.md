# fi-lookup-mcp

A personal portfolio project demonstrating a **tool-use and reconciliation pattern** over public regulatory data, implemented as a local MCP (Model Context Protocol) server connected to Claude Desktop.

Built by Nelson Anievas. Public data only — no proprietary or employer systems involved.

---

## What It Does

This server exposes four tools that allow an AI agent to resolve messy, real-world financial institution records against canonical regulatory identifiers from FDIC, NCUA, and FFIEC public datasets.

The centerpiece is `reconcile_institution`: given a dirty external record (e.g. `"Mtn America FCU, Sandy UT"`), it returns ranked candidate matches with confidence scores and human-readable match reasons.

## Local Data Setup

This project uses local data snapshots from three sources. The `cache/` directory is **not committed to Git** — you must populate it manually before first run.

### Required downloads

| File | Source | Instructions |
|------|--------|--------------|
| `cache/ncua_data.zip` | [NCUA Credit Union Data](https://www.ncua.gov/analysis/credit-union-corporate-call-report-data/call-report-data-for-download) | Download the most recent quarterly ZIP |
| `cache/ffiec_data.zip` | [FFIEC NIC Data](https://www.ffiec.gov/nicpubweb/content/NICXMLFILESINFORMATION.aspx) | Download the Active institutions ZIP |

FDIC data is fetched live from the [FDIC BankFind API](https://banks.data.fdic.gov/docs/) — no manual download needed.

After placing the ZIPs in `cache/`, run the server once and it will build the local JSON snapshots automatically. Or use the `refresh_cache` MCP tool from within Claude Desktop.

### Demo

**Prompt to Claude Desktop:**
> "I have a vendor row that says 'Mtn America FCU, Sandy UT' — what is it, and give me its FDIC cert if it has one."

**What happens under the hood:**
1. `reconcile_institution` scores ~8,600 institutions and returns Mountain America Credit Union (NCUA #24692, Sandy UT) at 0.984 confidence
2. `get_institution_profile` returns the full regulatory profile including ABA routing, deposit account count, web address, and charter type
3. `crosswalk_identifiers` explains that no FDIC cert exists because credit unions are NCUA-regulated

---

## Tools

### `search_institutions`
Free-text name search across all FDIC banks and NCUA credit unions. Supports filtering by institution type and state. Returns ranked candidates with fuzzy match scores.

### `get_institution_profile`
Full regulatory profile lookup by any identifier — FDIC cert, NCUA charter number, or RSSD ID. Returns all available metadata including regulator, charter type, ABA routing number, deposit account count, and web address.

### `reconcile_institution`
The centerpiece tool. Takes a messy external record (name, optional city/state/identifier) and returns ranked candidate matches, each with:
- A confidence score (0-1)
- Human-readable match reasons (e.g. "strong name match", "state match (UT)", "city match (SANDY)")
- Full identifier set for the matched institution

Scoring blends:
- **Name similarity** (0.6 weight): token-set ratio + Jaro-Winkler, with abbreviation expansion (FCU to federal credit union, Mtn to mountain, N.A. to national association)
- **Geographic agreement** (0.4 weight): state match (0.6) + city match (0.4)
- **Exact identifier override**: if a cert, charter, or RSSD is provided and matches, confidence is set to 1.0

### `crosswalk_identifiers`
Translates between FDIC cert, NCUA charter number, and RSSD ID. Explains regulatory boundaries (e.g. why a credit union has no FDIC cert).

---

## Data Sources

All data is public regulatory data. No licensed or proprietary sources.

| Source | Data | Refresh |
|--------|------|---------|
| FDIC BankFind API | ~4,300 active banks: name, location, cert, RSSD, web address | API call |
| FDIC Financials API | Deposit account counts from most recent quarter | API call |
| NCUA Quarterly ZIP | ~4,300 active credit unions; deposit counts from FS220A; web addresses from FS220D | Manual download |
| FFIEC NIC Active Attributes | ABA primary routing numbers joined via RSSD/cert/charter | Manual download |

**Total universe: ~8,600 institutions**

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
         +-- reconcile_institution  -->  reconciler.py
         +-- crosswalk_identifiers
                    |
                    v
             data_loader.py
                    |
                    +-- cache/fdic_institutions.json
                    +-- cache/ncua_institutions.json
                    +-- cache/call-report-data-*.zip
                    +-- cache/CSV_ATTRIBUTES_ACTIVE.zip

Key design decisions:
- **Local cache first**: runs fully offline after initial build
- **Atomic cache writes**: prevents corruption on interrupted writes
- **Stderr-only logging**: never pollutes the MCP stdio JSON channel
- **Abbreviation-aware normalization**: improves recall on dirty external records

---

## Why This Pattern Matters

Financial institution data is notoriously messy. The reconciliation pattern here is directly applicable to:
- Matching vendor/counterparty records to a canonical institution master
- Resolving BIN/issuer data to regulatory identifiers
- Enriching internal datasets with public regulatory metadata
- Onboarding automation that maps free-text institution names to stable IDs

This project re-expresses a reconciliation pattern from production AI agent work, using only public data.

---

## Stack

- Python 3.11
- FastMCP 3.4.2
- rapidfuzz (fuzzy string matching)
- httpx (async HTTP)
- Claude Desktop (MCP host)

---

## Setup

### Prerequisites
- Python 3.10+
- Claude Desktop

### Install

    git clone <repo>
    cd fi-lookup-mcp
    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

### Download manual data files

Two files require a one-time manual browser download:

1. **NCUA quarterly ZIP** from ncua.gov/analysis/credit-union-corporate-call-report-data/quarterly-data — save to cache/
2. **FFIEC NIC Active Attributes** from ffiec.gov/npw/FinancialReport/DataDownload — save to cache/

### Build the data snapshot

    python -c "import asyncio; from data_loader import build_snapshot; asyncio.run(build_snapshot())"

### Connect to Claude Desktop

    fastmcp install claude-desktop server.py --name "fi-lookup"

Then restart Claude Desktop.

---

## Framing Note

This is a **tool-use and reconciliation pattern** — not RAG. The model calls structured tools that execute deterministic scoring logic against a pre-built regulatory snapshot and return ranked, explainable results.
