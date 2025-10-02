#!/usr/bin/env bash
# Safely load a .env file into the current shell (when sourced).
# Usage:
#   source scripts/load_env.sh /path/to/.env   # loads and exports variables into the caller
#   ./scripts/load_env.sh /path/to/.env        # prints a masked preview (does not export to caller)

set -eu

ENV_FILE="${1:-./.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: env file not found: $ENV_FILE" >&2
  # if being sourced, return non-zero; else exit
  (return 1 2>/dev/null) || exit 1
fi

# Create a sanitized temporary copy (strip CR) to avoid quoting/CRLF issues
TMP_ENV="$(mktemp)"
tr -d '\r' < "$ENV_FILE" > "$TMP_ENV"

# Detect whether the script is being sourced or executed
is_sourced=false
# In bash, compare BASH_SOURCE
if [ "${BASH_SOURCE[0]:-}" != "${0:-}" ]; then
  is_sourced=true
fi

if [ "$is_sourced" = true ]; then
  # Export all variables defined in the file into the caller
  set -a
  # shellcheck disable=SC1090
  . "$TMP_ENV"
  set +a
  # Clean up
  rm -f "$TMP_ENV"
  # Masked preview of VAST_API_KEY
  if [ -n "${VAST_API_KEY:-}" ]; then
    K="$VAST_API_KEY"
    if [ ${#K} -ge 8 ]; then
      echo "Loaded VAST_API_KEY: ${K:0:4}...${K: -4}"
    else
      echo "Loaded VAST_API_KEY: (masked)"
    fi
    return 0
  else
    echo "WARNING: VAST_API_KEY not set after loading $ENV_FILE" >&2
    return 2
  fi
else
  # Executed directly: just print a masked preview and do not modify caller environment
  # Load variables into a subshell to inspect VAST_API_KEY
  (
    set -a
    . "$TMP_ENV" >/dev/null 2>&1 || true
    set +a
    if [ -n "${VAST_API_KEY:-}" ]; then
      K="$VAST_API_KEY"
      if [ ${#K} -ge 8 ]; then
        echo "Found VAST_API_KEY in $ENV_FILE: ${K:0:4}...${K: -4}"
      else
        echo "Found VAST_API_KEY in $ENV_FILE: (masked)"
      fi
      exit 0
    else
      echo "VAST_API_KEY not found in $ENV_FILE" >&2
      exit 2
    fi
  )
  rm -f "$TMP_ENV"
fi
