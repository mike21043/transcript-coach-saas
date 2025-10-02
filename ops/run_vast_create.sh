#!/usr/bin/env bash
set -euo pipefail

# Wrapper to run the vast_agent controller locally.
# Usage:
#   VAST_API_KEY=... VAST_TEMPLATE_HASH=... [VAST_IMAGE=...] bash ops/run_vast_create.sh

if [ -z "${VAST_API_KEY:-}" ]; then
  echo "Set VAST_API_KEY environment variable"
  exit 1
fi
if [ -z "${VAST_TEMPLATE_HASH:-}" ]; then
  echo "Set VAST_TEMPLATE_HASH environment variable"
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON=${PYTHON:-python3}

echo "Running vast_agent controller..."
echo "  TEMPLATE_HASH=${VAST_TEMPLATE_HASH}"
if [ -n "${VAST_IMAGE:-}" ]; then
  echo "  VAST_IMAGE=${VAST_IMAGE}"
else
  echo "  VAST_IMAGE not set; will rely on template image/config"
fi

# Run the packaged controller module
exec "$PYTHON" -m vast_agent.vast_agent
