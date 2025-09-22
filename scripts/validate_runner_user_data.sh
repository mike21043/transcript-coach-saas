#!/usr/bin/env bash
# Validate generated runner user-data and give actionable checks.
# Usage: ./scripts/validate_runner_user_data.sh /tmp/runner-user-data.sh

set -euo pipefail
FILE=${1:-/tmp/runner-user-data.sh}
if [ ! -f "$FILE" ]; then
  echo "ERROR: user-data file not found: $FILE"
  exit 2
fi

echo "Validating: $FILE"

# Check for REG_TOKEN, REPO_URL, RUNNER_LABELS
grep -qE "^\s*export\s+REG_TOKEN=" "$FILE" && echo "FOUND: REG_TOKEN (will be redacted)" || echo "MISSING: REG_TOKEN"
grep -qE "^\s*export\s+REPO_URL=" "$FILE" && echo "FOUND: REPO_URL" || echo "MISSING: REPO_URL"
grep -qE "^\s*export\s+RUNNER_LABELS=" "$FILE" && echo "FOUND: RUNNER_LABELS" || echo "MISSING: RUNNER_LABELS"

# Check it calls ops/runner-cloud-init.sh or contains expected bootstrap header
if grep -q "ops/runner-cloud-init.sh" "$FILE"; then
  echo "FOUND: ops/runner-cloud-init.sh invocation"
else
  echo "WARNING: ops/runner-cloud-init.sh not referenced — ensure user-data includes bootstrap" 
fi

# Check ops bootstrap script exists
if [ -f ops/runner-cloud-init.sh ]; then
  echo "FOUND: ops/runner-cloud-init.sh in repo"
else
  echo "MISSING: ops/runner-cloud-init.sh — bootstrap script not present in repo"
fi

# Check for REG_TOKEN length (not revealing token)
REG_LINE=$(grep -E "^\s*export\s+REG_TOKEN=" "$FILE" || true)
if [ -n "$REG_LINE" ]; then
  # Extract value after '=' robustly, strip surrounding quotes
  token=$(echo "$REG_LINE" | awk -F'=' '{print $2; exit}' | sed -E "s/^[ \t]*['\"]?//; s/['\"]?[ \t]*$//")
  token=${token%%[ \t]*} # strip trailing spaces
  echo "REG_TOKEN length: ${#token} characters (redacted)"
  if [ ${#token} -lt 10 ]; then
    echo "WARNING: REG_TOKEN looks short — likely invalid"
  fi
fi

# Suggest next action
cat <<EOF

Summary:
- If all FOUND lines are present, the user-data looks structurally correct.
- Next: execute the user-data on a test VM immediately (token expires). Example:
  scp $FILE user@vm:/tmp/
  ssh user@vm 'sudo bash /tmp/runner-user-data.sh'

Caveats:
- Tokens are short lived, provision immediately.
- Do NOT paste tokens publicly.
EOF

exit 0
