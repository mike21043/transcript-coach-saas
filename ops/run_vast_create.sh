#!/usr/bin/env bash
set -euo pipefail

# Wrapper to run the vast_agent controller locally.
# Usage:
#   VAST_API_KEY=... VAST_IMAGE=... bash ops/run_vast_create.sh

if [ -z "${VAST_API_KEY:-}" ]; then
  echo "Set VAST_API_KEY environment variable"
  exit 1
fi
# Enforce image-only provisioning: VAST_IMAGE is required.
if [ -z "${VAST_IMAGE:-}" ]; then
  echo "Set VAST_IMAGE environment variable (e.g. ghcr.io/<org>/<repo>:tag)."
  echo "This controller enforces image-based provisioning only."
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON=${PYTHON:-python3}

echo "Running vast_agent controller (image-only mode)..."
echo "  VAST_IMAGE=${VAST_IMAGE}"

# Run the packaged controller module
exec "$PYTHON" -m vast_agent.vast_agent
