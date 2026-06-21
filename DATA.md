# Data: provenance, storage, and licensing

This project is **public data only**. No proprietary, licensed, or institution-internal
data is embedded in the repository. This doc explains where each kind of data lives and how
it's licensed — read it before committing data or publishing a release.

## The three-tier data model

| Tier | What | Where it lives | In git? |
|------|------|----------------|---------|
| **1. Curated knowledge + code** | Loaders, reconciler, MCP tools, and the hand-maintained maps (`PROVIDER_DOMAINS`, `HTML_PROVIDER_PATTERNS`, `PROVIDER_OAUTH_NETWORKS`, `API_CAPABLE_PROVIDERS`) | the repo | ✅ yes |
| **2. Raw source data** | FDIC/NCUA/FFIEC/SBA snapshots + manually/auto-downloaded ZIPs/CSVs, and the scrape cache | `cache/` | ❌ no (gitignored) — regenerated from public sources |
| **3. Derived enrichment snapshot** | The unified institution table with all flags/signals | `releases/` → published as **dated GitHub Release** artifacts | ❌ no (gitignored) — attached to Releases |

Personal config (the assistant's memory, local `.env` with `FI_AUTH_*`) lives outside the repo
entirely and is never committed.

## Sources & licensing

All sources are public. Government datasets are U.S. public domain; the rest are factual
data derived from public websites.

| Source | Data | License / terms |
|--------|------|-----------------|
| FDIC BankFind / Financials API | Bank identity, deposits, commercial lending | U.S. public domain |
| NCUA Call Reports (quarterly ZIP) | Credit union identity, deposits, member-business loans | U.S. public domain |
| FFIEC NIC (bulk ZIPs) | RSSD identifiers, names, transformations, relationships | U.S. public domain |
| SBA 7(a)/504 FOIA datasets | Small-business lenders (by cert/charter/name) | U.S. public domain (FOIA) |
| Institution websites (scraped) | Advertised business/SMB accounts, login portals, provider fingerprints | Factual data derived from public web pages |

**Code license:** [MIT](LICENSE).
**Data license for published snapshots:** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) —
free to use with attribution to this project and the underlying public sources above.

## What is and isn't published in a release

`build_release.py` exports the **derived view only** (`server._full_record`): institution
metadata + business-coverage flags + provider / connection-method / OAuth-rail signals, as
**CSV + SQLite (+ Parquet if `pandas`/`pyarrow` are installed)**, plus a manifest with row
counts and `data_as_of` dates.

It deliberately **excludes the raw scrape artifacts** — `business_evidence` phrases,
`provider_hints`, and the full captured `login_portals` URL lists. Those stay local in
`cache/business_coverage.json`; anyone can regenerate them with `scrape_business_coverage.py`.
This keeps published artifacts to factual, aggregate flags rather than a raw dump of 8k+ sites.

## Important caveats for consumers

- **Staleness:** every record carries `data_as_of`; releases are dated. A snapshot is a
  point in time — re-run the build for current data.
- **Heuristics:** `service_provider`, `oauth_networks`, and `likely_connection_method` are
  *inferred* from public fingerprints and curated maps — directional, not authoritative.
  Lending ≠ deposit accounts; website signals are advertised, not guaranteed; JS-only login
  widgets read as `unknown`.
- **No proprietary joins:** any signal requiring internal/aggregator data is intentionally
  out of scope. Do that downstream against your own data — don't bake it into this repo.

## Regenerating the data

```bash
python -c "import asyncio; from data_loader import build_snapshot; asyncio.run(build_snapshot())"
python refresh_sba.py                 # SBA lender index (quarterly)
python scrape_business_coverage.py    # website coverage (delta-driven)
python build_release.py               # export a dated release snapshot
```
