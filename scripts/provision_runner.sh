#!/usr/bin/env bash
# Helper script to request a GitHub Actions runner registration token and render cloud-init
# Usage: ./provision_runner.sh <owner> <repo> <output-user-data-file>
# Requires GITHUB_PAT in environment with repo:admin or repo scope for registration tokens.

set -euo pipefail
OWNER=${1:-mike21043}
REPO=${2:-transcript-coach-saas}
OUT_FILE=${3:-/tmp/runner-user-data.sh}

if [ -z "${GITHUB_PAT:-}" ]; then
  echo "Please set GITHUB_PAT in environment with repo:admin permissions" >&2
  exit 1
fi

API_URL="https://api.github.com/repos/${OWNER}/${REPO}/actions/runners/registration-token"

echo "Requesting registration token from GitHub API..."
TOKEN_JSON=$(curl -sS -X POST -H "Authorization: token ${GITHUB_PAT}" -H "Accept: application/vnd.github+json" "$API_URL")
REG_TOKEN=$(echo "$TOKEN_JSON" | jq -r .token)
if [ -z "$REG_TOKEN" ] || [ "$REG_TOKEN" = "null" ]; then
  echo "Failed to obtain registration token: $TOKEN_JSON" >&2
  exit 1
fi

# Render cloud-init user-data by embedding REG_TOKEN and optional fields
REPO_URL="https://github.com/${OWNER}/${REPO}"
RUNNER_LABELS="self-hosted,cuda-test,transcript-coach"

cat > "$OUT_FILE" <<EOF
#!/bin/bash
export REG_TOKEN='${REG_TOKEN}'
export REPO_URL='${REPO_URL}'
export RUNNER_LABELS='${RUNNER_LABELS}'
# The ops/runner-cloud-init.sh will run and bootstrap the runner
bash -lc '$(sed -e "s/'/'\\''/g" ops/runner-cloud-init.sh)'
EOF

chmod +x "$OUT_FILE"

echo "Wrote user-data to $OUT_FILE"

echo "Next: use your provisioning system (vast_agent) to create a VM and pass this script as user-data or run it on the instance after boot. The token expires shortly, so provision immediately."
