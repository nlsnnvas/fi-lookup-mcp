# Security Policy

## Reporting a vulnerability

Please report security issues by opening a private security advisory on the GitHub
repository, or by contacting the maintainer directly. Do not file a public issue for
an unfixed vulnerability.

## Security posture

`fi-lookup-mcp` is a **local, deterministic MCP tool server** over **public** US
financial-institution regulatory data. Its threat surface is intentionally small.

### Data

- **Classification: PUBLIC.** All data is public US regulatory data — FDIC BankFind,
  NCUA call reports, FFIEC NIC, SBA 7(a)/504 FOIA. It is institution-level *business*
  data, not consumer data.
- **No PII.** No personal data is collected, stored, or processed.
- **No secrets.** Every data source is **credential-free** — no account, API key, or
  login is required for any source. The server holds and needs no credentials.
- **At rest:** local JSON/ZIP caches under `cache/` (gitignored, not committed). Cache
  writes are **atomic** (`.tmp` file + `os.rename`) to prevent corruption on interruption.

### Network

- **Outbound-only, all public, no auth.** Egress endpoints: `api.fdic.gov`,
  `ncua.gov`, `data.sba.gov`, FFIEC NIC bulk ZIPs (downloaded manually), and best-effort
  scraping of bank homepages for the optional business/division enrichment.
- **Runtime is offline.** A warm start reads local caches and makes **zero** network
  calls. Egress happens only during an explicit data build / refresh / scrape job.
- **No inbound listeners** except the optional local **FI Explorer** dashboard, which
  binds `127.0.0.1`. If exposed to a team, it ships opt-in basic-auth + rate-limiting.
- **No stealth/evasion in scraping** — plain HTTPS with an honest User-Agent. Bot-walled
  sites are recorded as `unreachable`, never bypassed.

### Telemetry

The application sends **no telemetry**. `opentelemetry-api` is pulled in transitively by
FastMCP, but no OpenTelemetry SDK or exporter is installed, so the API is a no-op and
cannot export anything.

### Dependencies, SBOM & supply-chain scanning

- Python 3.11.9 (pinned). Runtime/test/optional dependencies are pinned in
  `requirements*.txt`. A full inventory with licenses is in [`SBOM.md`](SBOM.md); a
  machine-readable CycloneDX 1.6 SBOM is in [`SBOM.cdx.json`](SBOM.cdx.json).
- All dependencies are permissively licensed (MIT / BSD / Apache-2.0 / ISC / PSF /
  Unlicense; certifi is MPL-2.0 for its CA bundle). **No GPL/LGPL/AGPL copyleft.**
- **SCA + secret scanning run in CI** (the `security` job in `.github/workflows/ci.yml`):
  `pip-audit` fails the build on any known dependency CVE, and `detect-secrets` fails on
  any committed secret. Reproduce locally with `tools/security_scan.sh` (uses `uvx`, no
  venv changes). Current status: 0 known vulnerabilities, 0 secrets.

### Threat model

See [`THREAT-MODEL.md`](THREAT-MODEL.md) for trust boundaries, a STRIDE summary, and the
AI-specific (host↔MCP) risk notes.

### Code-safety conventions (test-guarded)

- **Never writes to stdout** — the MCP stdio channel carries JSON; stray stdout would
  corrupt it. All diagnostics go to stderr. Guarded by a hermetic test.
- **Tolerates an empty snapshot** — tools return an error dict rather than throwing while
  the snapshot is still building. Guarded by a hermetic test.
- A hermetic `pytest` suite runs in CI on every push (`.github/workflows/ci.yml`).

## Supported versions

This is an actively developed project; security fixes target the `main` branch.
