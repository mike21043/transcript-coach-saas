#!/usr/bin/env bash
# Wrapper to safely load .env and run the e2e harness.
# Usage:
#   source scripts/run_e2e.sh --dry        # run dry locally (default)
#   ./scripts/run_e2e.sh                    # runs dry by default
#   FORCE=1 ./scripts/run_e2e.sh            # allow non-dry runs (be careful)

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}/.."
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

# Default to dry run unless user passes args and FORCE=1 for real runs
if [ "$#" -eq 0 ]; then
  ARGS=(--dry)
else
  ARGS=("$@")
fi

# Load env into this shell
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/load_env.sh" "$ENV_FILE"

echo "Using VAST_API_KEY: ${VAST_API_KEY:0:4}...${VAST_API_KEY: -4}"

# Check for required files
if [ ! -f "${ROOT_DIR}/scripts/e2e_provision_and_test.py" ]; then
  echo "ERROR: e2e_provision_and_test.py not found in ${ROOT_DIR}/scripts" >&2
  exit 2
fi

# If running non-dry, require explicit FORCE=1
is_dry=false
for a in "${ARGS[@]}"; do
  if [ "$a" = "--dry" ]; then is_dry=true; fi
done

if [ "$is_dry" = false ] && [ "${FORCE:-0}" != "1" ]; then
  echo "Refusing to run a real provisioning without FORCE=1. To do this intentionally: FORCE=1 ./scripts/run_e2e.sh <args>" >&2
  exit 3
fi

echo "Running harness: python3 scripts/e2e_provision_and_test.py ${ARGS[*]}"
python3 "${ROOT_DIR}/scripts/e2e_provision_and_test.py" "${ARGS[@]}"
