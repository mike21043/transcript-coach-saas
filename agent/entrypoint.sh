#!/usr/bin/env bash
set -euo pipefail

# If RCLONE_CONFIG_CONTENT is provided (base64 or raw), write it to RCLONE_CONFIG path
: ${RCLONE_CONFIG:=/root/.config/rclone/rclone.conf}
if [ -n "${RCLONE_CONFIG_CONTENT:-}" ]; then
  echo "[entrypoint] Writing rclone config to ${RCLONE_CONFIG}"
  mkdir -p "$(dirname "${RCLONE_CONFIG}")"
  # try base64 decode; fall back to raw
  if echo "${RCLONE_CONFIG_CONTENT}" | base64 --decode >/dev/null 2>&1; then
    echo "${RCLONE_CONFIG_CONTENT}" | base64 --decode > "${RCLONE_CONFIG}"
  else
    echo "${RCLONE_CONFIG_CONTENT}" > "${RCLONE_CONFIG}"
  fi
  chmod 600 "${RCLONE_CONFIG}"
fi

# If RCLONE_REMOTE is set, verify it can be listed
if [ -n "${RCLONE_REMOTE:-}" ]; then
  echo "[entrypoint] Validating rclone remote: ${RCLONE_REMOTE}"
  if ! rclone lsd "${RCLONE_REMOTE}" --config "${RCLONE_CONFIG}" >/dev/null 2>&1; then
    echo "[entrypoint] Warning: rclone lsd failed for ${RCLONE_REMOTE} (this may be fine if credentials are not yet provisioned)"
  else
    echo "[entrypoint] rclone remote looks reachable"
  fi
fi

# Exec the original start script (should start the agent process)
exec /app/agent/start.sh
