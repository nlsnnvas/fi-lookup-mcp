# Software Bill of Materials (SBOM)

Dependency + license inventory for **fi-lookup-mcp**, generated from the pinned
requirements and the installed package metadata in the project virtualenv.

- **Python:** 3.11.9 (pinned via `.python-version`)
- **Dependency manifests:** `requirements.txt` (runtime), `requirements-dev.txt` (test),
  `requirements-js.txt` (optional headless-Chromium tier)
- **Human-readable (this file):** regenerate with `python tools/gen_sbom.py` (reads
  `importlib.metadata`, no network)
- **Machine-readable:** [`SBOM.cdx.json`](SBOM.cdx.json) — CycloneDX 1.6, the full
  installed dependency closure (83 components) for ingestion by an SCA/vuln scanner.
  Regenerate with `tools/security_scan.sh` (or `uvx --from cyclonedx-bom cyclonedx-py
  environment .venv -o SBOM.cdx.json`).

## Vulnerability & secret scan (SCA)

- **`pip-audit`** (OSV/PyPI advisory DB) over the pinned runtime + dev set: **0 known
  vulnerabilities** as of the last run. (One finding was remediated — `pydantic-settings`
  2.14.1 → 2.14.2, GHSA-4xgf-cpjx-pc3j; a moderate, local-only path-traversal in a secrets
  source this project does not use. See `intake/05-security-evidence.md`.)
- **`detect-secrets`** over tracked source: **0 secrets** — consistent with the
  credential-free, no-secrets design.
- Both run in CI (the `security` job in `.github/workflows/ci.yml`) and locally via
  `tools/security_scan.sh`.

## License summary

All runtime, dev, and optional dependencies are under **permissive** licenses
(MIT, BSD-2/3-Clause, Apache-2.0, ISC, PSF-2.0, Unlicense). The only weak-copyleft
component is **certifi** (MPL-2.0), which ships an *unmodified* Mozilla CA-certificate
bundle — file-level copyleft that imposes no obligation on this project's source.

**No GPL / LGPL / AGPL or other strong-copyleft dependency is present.**

| License | Count (runtime) |
|---------|-----------------|
| MIT | 38 |
| BSD (2/3-Clause) | 14 |
| Apache-2.0 | 11 |
| Apache-2.0 OR BSD (dual) | 2 (cryptography, packaging) |
| ISC | 2 |
| MPL-2.0 | 1 (certifi — CA bundle) |
| PSF-2.0 | 1 (typing_extensions) |
| Unlicense | 1 (email-validator) |
| **Total** | **70** |

## Runtime dependencies (`requirements.txt`)

| Package | Version | License |
|---------|---------|---------|
| aiofile | 3.11.1 | Apache-2.0 |
| annotated-types | 0.7.0 | MIT |
| anyio | 4.14.0 | MIT |
| attrs | 26.1.0 | MIT |
| Authlib | 1.7.2 | BSD |
| backports.tarfile | 1.2.0 | MIT |
| beartype | 0.22.9 | MIT |
| cachetools | 7.1.4 | MIT |
| caio | 0.9.25 | Apache-2.0 |
| certifi | 2026.5.20 | MPL-2.0 (CA bundle) |
| cffi | 2.0.0 | MIT |
| click | 8.4.1 | BSD-3-Clause |
| cryptography | 49.0.0 | Apache-2.0 OR BSD-3-Clause |
| cyclopts | 4.18.0 | Apache-2.0 |
| dnspython | 2.8.0 | ISC |
| docstring_parser | 0.18.0 | MIT |
| email-validator | 2.3.0 | Unlicense |
| exceptiongroup | 1.3.1 | MIT |
| fastmcp | 3.4.2 | Apache-2.0 |
| fastmcp-slim | 3.4.2 | Apache-2.0 |
| griffelib | 2.0.2 | ISC |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD |
| httpx-sse | 0.4.3 | MIT |
| idna | 3.18 | BSD-3-Clause |
| importlib_metadata | 9.0.0 | Apache-2.0 |
| jaraco.classes | 3.4.0 | MIT |
| jaraco.context | 6.1.2 | MIT |
| jaraco.functools | 4.5.0 | MIT |
| joserfc | 1.7.1 | BSD |
| jsonref | 1.1.0 | MIT |
| jsonschema | 4.26.0 | MIT |
| jsonschema-path | 0.5.0 | Apache-2.0 |
| jsonschema-specifications | 2025.9.1 | MIT |
| keyring | 25.7.0 | MIT |
| markdown-it-py | 4.2.0 | MIT |
| mcp | 1.27.2 | MIT |
| mdurl | 0.1.2 | MIT |
| more-itertools | 11.1.0 | MIT |
| openapi-pydantic | 0.5.1 | MIT |
| opentelemetry-api | 1.42.1 | Apache-2.0 |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause |
| pathable | 0.6.0 | Apache-2.0 |
| platformdirs | 4.10.0 | MIT |
| py-key-value-aio | 0.4.5 | Apache-2.0 |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| pydantic-settings | 2.14.2 | MIT |
| pydantic_core | 2.46.4 | MIT |
| Pygments | 2.20.0 | BSD-2-Clause |
| PyJWT | 2.13.0 | MIT |
| pyperclip | 1.11.0 | BSD |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT |
| RapidFuzz | 3.14.5 | MIT |
| referencing | 0.37.0 | MIT |
| rich | 15.0.0 | MIT |
| rich-rst | 2.0.1 | MIT |
| rpds-py | 2026.5.1 | MIT |
| sse-starlette | 3.4.4 | BSD-3-Clause |
| starlette | 1.3.1 | BSD-3-Clause |
| typing-inspection | 0.4.2 | MIT |
| typing_extensions | 4.15.0 | PSF-2.0 |
| uncalled-for | 0.3.2 | MIT |
| uvicorn | 0.49.0 | BSD-3-Clause |
| watchfiles | 1.2.0 | MIT |
| websockets | 16.0 | BSD-3-Clause |
| zipp | 4.1.0 | MIT |

## Test dependencies (`requirements-dev.txt`)

| Package | Version | License |
|---------|---------|---------|
| pytest | 9.1.1 | MIT |

## Optional JS tier (`requirements-js.txt`) — not installed by default

| Package | Version | License |
|---------|---------|---------|
| playwright | 1.60.0 | Apache-2.0 |

> The optional Playwright tier additionally downloads a Chromium build at
> `playwright install chromium` time. It is **not** a default runtime dependency
> and is only used by the opt-in `scrape_js_coverage.py` enrichment job.

## Telemetry note

`opentelemetry-api` is pulled in transitively by FastMCP. **No OpenTelemetry SDK and
no exporter package is installed** — the API alone is a no-op that cannot emit or
export telemetry anywhere. The application sends no telemetry.
