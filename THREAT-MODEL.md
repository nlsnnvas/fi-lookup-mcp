# Threat Model — fi-lookup-mcp

A lightweight threat model for the MCP server, its optional local dashboard, and the
data-build/enrichment jobs. Scope is the software in this repository. The LLM that
*calls* these tools lives in the host (Claude Desktop/Code) and is out of scope except
at the host↔MCP boundary noted below. See [`SECURITY.md`](SECURITY.md) for the security
posture summary and [`SBOM.md`](SBOM.md) for the dependency/vuln inventory.

## Assets

| Asset | Sensitivity | Notes |
|-------|-------------|-------|
| Snapshot + caches (`cache/`) | Low | Public regulatory data only; no PII, no secrets |
| Dated release exports (CSV/SQLite/Parquet) | Low | Same public data |
| Source code | Low | Open-source |
| Credentials / API keys | — | **None exist** — every data source is credential-free |
| The local dashboard | Low | Read-only view over the snapshot |

There is **no confidential data asset** to protect — the security objective is integrity
and availability of a correct snapshot, not confidentiality.

## Trust boundaries

```
[ Public data APIs / bank homepages ]  ──HTTPS(outbound)──▶  data-build / scrape jobs
                                                                   │ writes
                                                                   ▼
                                                            cache/ (local disk)
                                                                   │ reads
   host (Claude + user)  ──MCP stdio(JSON)──▶  server.py (tools)  ─┘
                                                                   │ optional
                                                            FI Explorer dashboard
                                                            (Starlette, 127.0.0.1)
```

1. **Host ↔ MCP (stdio):** trusted local IPC. The MCP returns structured JSON; it takes
   no action on third parties and has no write-back to any external system.
2. **MCP/jobs ↔ public internet (outbound only):** HTTPS to public endpoints; no inbound
   listener. Runtime (warm start) makes **zero** network calls.
3. **Dashboard ↔ local user:** binds `127.0.0.1`; optional basic-auth + rate-limit for
   shared scenarios.

## STRIDE summary

| Threat | Exposure | Mitigation |
|--------|----------|------------|
| **Spoofing** | No auth surface at runtime (no inbound, no credentials). Dashboard exposure is opt-in. | Dashboard binds localhost; opt-in basic-auth + rate-limit + portal gate for share scenarios; `share.sh` tunnel is demo-only, out of scope for deployment. |
| **Tampering** | A poisoned upstream source or a tampered local cache could corrupt the snapshot. | HTTPS to public sources; atomic cache writes (`.tmp`+rename); `source_manifest.json` content-hash guard detects upstream changes; deterministic rebuild from source. No privileged action depends on the data. |
| **Repudiation** | Low — single-user local tool. | `data_as_of` per record; `accuracy_history.jsonl` audit trail of every refresh. |
| **Information disclosure** | Low — no confidential data, no secrets, no PII. | Credential-free by design; `detect-secrets` in CI; never writes to stdout (would leak into the MCP JSON channel) — test-guarded. |
| **Denial of service** | A job could hang on a slow/hostile remote host. | Scrapers use timeouts and record unreachable sites as `unreachable` (never retried into a loop); runtime is offline so a remote outage can't affect serving. No inbound listener to flood. |
| **Elevation of privilege** | Minimal — no auth/roles, no shell-out on untrusted input, runs as the invoking user. | Tools are pure lookups over an in-memory snapshot; no `eval`/dynamic import of remote data; dependencies pinned + SCA-gated. |

## AI-specific risks (host↔MCP boundary)

Although the MCP is not itself an AI system, it feeds an LLM. The relevant risks:

- **Prompt injection via tool output:** scraped homepage text could contain adversarial
  instructions that reach the model through a tool result. *Mitigation:* tool outputs are
  **structured fields** (yes/no/unknown flags, names, URLs, scores) — not free-form pasted
  HTML — and the server takes no action on the model's behalf, so an injected instruction
  has no actuator. Inferred fields are explicitly labelled best-effort/`unknown`.
- **Hallucination / ungrounded answers:** *Mitigation:* answers are grounded in the JSON
  the MCP returns; `reconcile_institution` returns a confidence score with human-readable
  match reasons; deterministic lending data is preferred over the scrape
  (`business_banking` + `business_basis`); accuracy is measured (gold-set P/R/F1 +
  continuous monitoring), not asserted.
- **Over-trust of inferred signals:** *Mitigation:* `_yn()` maps unknown/unreachable to
  `unknown` (never a false "no"); provider inference excludes embedded marketing widgets
  to avoid false positives.

## Out of scope / residual

- The host model's own data-handling (Anthropic API terms, retention, training posture) is
  a host/contract concern — confirm against the deployment's Anthropic agreement, not this
  repo. The MCP sends the model only public regulatory data + tool schemas.
- The optional Playwright JS tier downloads a Chromium build; treat that browser's own
  supply chain per the org's standard controls if the tier is enabled.
- FFIEC NIC ZIPs are downloaded manually (403-gated); verify their provenance on refresh.
