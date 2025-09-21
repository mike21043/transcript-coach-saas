#!/usr/bin/env bash
# Helper to export variables from a repo-root .env into the current shell
# Usage: source scripts/export_env.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOTENV="$REPO_ROOT/.env"
if [ ! -f "$DOTENV" ]; then
  echo "No .env file found at $DOTENV"
  return 1
fi

# Parse simple KEY=VALUE pairs, preserve quotes
while IFS= read -r line; do
  line="${line%%#*}"  # strip comments
  if [[ -z "$line" ]]; then
    continue
  fi
  if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
    key=${line%%=*}
    val=${line#*=}
    # Trim surrounding quotes
    if [[ "$val" =~ ^\".*\"$ || "$val" =~ ^\'.*\'$ ]]; then
      val=${val:1:-1}
    fi
    export "$key=$val"
  fi
done < "$DOTENV"

echo "Exported env from $DOTENV"
return 0
