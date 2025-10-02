#!/usr/bin/env bash
# Generate a base64-encoded docker config.json for ghcr.io using a GitHub PAT.
# Usage:
#   GITHUB_PAT=ghp_xxx ./scripts/generate_docker_config.sh > docker_config.b64
#   or
#   ./scripts/generate_docker_config.sh --interactive

set -eu

if [ "${1:-}" = "--interactive" ]; then
  read -r -p "GitHub username: " GH_USER
  read -r -s -p "GitHub PAT: " GH_PAT
  echo
else
  GH_PAT="${GITHUB_PAT:-}"
  if [ -z "$GH_PAT" ]; then
    echo "GITHUB_PAT not set and no --interactive flag provided" >&2
    exit 2
  fi
  # try to discover username
  if command -v curl >/dev/null 2>&1 && [ -n "$GH_PAT" ]; then
    GH_USER=$(curl -sS -H "Authorization: token $GH_PAT" https://api.github.com/user | awk -F'"' '/"login":/ {print $4; exit}') || true
    if [ -z "$GH_USER" ]; then
      echo "Could not discover GitHub username; please provide interactively" >&2
      exit 3
    fi
  fi
fi

AUTH_RAW="${GH_USER}:${GH_PAT}"
AUTH_B64=$(printf "%s" "$AUTH_RAW" | base64 -w0 2>/dev/null || base64)
CFG=$(printf '{"auths":{"ghcr.io":{"auth":"%s"}}}' "$AUTH_B64")
printf "%s" "$CFG" | base64 -w0 2>/dev/null || printf "%s" "$CFG" | base64
