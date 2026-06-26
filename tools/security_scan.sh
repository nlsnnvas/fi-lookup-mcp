#!/usr/bin/env bash
# One-command local security gate for fi-lookup-mcp. Mirrors the CI `security`
# job so contributors can reproduce it before pushing. Read-only except for
# regenerating the SBOMs. Requires `uv` (uvx) so nothing is installed into the
# project virtualenv.
#
#   tools/security_scan.sh
#
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/4  SCA: pip-audit (runtime + dev) =="
uvx pip-audit -r requirements.txt -r requirements-dev.txt --progress-spinner off

echo
echo "== 2/4  Secret scan: detect-secrets =="
count=$(uvx detect-secrets scan --exclude-files '\.venv/|cache/|/\.git/' \
  | python -c 'import sys,json; print(len(json.load(sys.stdin)["results"]))')
if [ "$count" != "0" ]; then
  echo "FAIL: detect-secrets flagged $count file(s)"; exit 1
fi
echo "OK: no committed secrets"

echo
echo "== 3/4  Regenerate machine-readable SBOM (CycloneDX 1.6) =="
uvx --from cyclonedx-bom cyclonedx-py environment .venv -o SBOM.cdx.json
echo "wrote SBOM.cdx.json"

echo
echo "== 4/4  Regenerate human-readable license inventory =="
python tools/gen_sbom.py > /dev/null && echo "tools/gen_sbom.py OK (paste output into SBOM.md)"

echo
echo "All security checks passed."
