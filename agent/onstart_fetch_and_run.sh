#!/usr/bin/env bash
set -euo pipefail

# onstart_fetch_and_run.sh
# Fetches an authoritative agent/agent.py, verifies its sha256, installs to /app/agent.py,
# py_compile checks it, ensures FUSE and rclone mount are available, and starts the agent with safe defaults.
# Usage:
#   bash onstart_fetch_and_run.sh <AGENT_URL> <EXPECTED_SHA>
# or set AGENT_URL/EXPECTED_SHA environment variables before running.

AGENT_URL=${1:-${AGENT_URL:-"http://38.242.200.197:8000/agent/agent.py"}}
EXPECTED_SHA=${2:-${EXPECTED_SHA:-"0ded68cdfee20b16ab5aa0f23288f513f3a266c43d902e01d7ade47afacaf422"}}

TMP_FETCH=/tmp/agent.py.fetch
DEST=/app/agent.py

echo "[onstart] Fetching agent from: $AGENT_URL"
mkdir -p /app /workspace /data/results /data/uploads || true

if ! curl -fsSL "$AGENT_URL" -o "$TMP_FETCH"; then
  echo "[onstart][error] Failed to download $AGENT_URL" >&2
  exit 2
fi

CALC_SHA=$(sha256sum "$TMP_FETCH" | awk '{print $1}')
if [ "$CALC_SHA" != "$EXPECTED_SHA" ]; then
  echo "[onstart][error] SHA mismatch for fetched agent.py" >&2
  echo "  expected: $EXPECTED_SHA" >&2
  echo "  got:      $CALC_SHA" >&2
  ls -l "$TMP_FETCH" || true
  exit 3
fi

echo "[onstart] SHA256 verified: $CALC_SHA"

echo "[onstart] py_compile check"
if ! python3 -m py_compile "$TMP_FETCH"; then
  echo "[onstart][error] python -m py_compile failed" >&2
  exit 4
fi

echo "[onstart] Installing agent to $DEST"
mv "$TMP_FETCH" "$DEST"
chmod 644 "$DEST"


echo "[onstart] Stopping any running agent process (if present)"
pkill -f "/app/agent.py" || true

# Default envs suitable for CPU-only fallback. Override via environment in OnStart if desired.
REDIS_URL=${REDIS_URL:-"redis://38.242.200.197:6379/0"}
QUEUE_NAME=${QUEUE_NAME:-"transcript_jobs"}
HF_TOKEN=${HF_TOKEN:-""}
PYANNOTE_DEVICE=${PYANNOTE_DEVICE:-"cpu"}
SKIP_EMBEDDINGS=${SKIP_EMBEDDINGS:-"1"}
WHISPERX_DEVICE=${WHISPERX_DEVICE:-"cpu"}
WHISPERX_COMPUTE_TYPE=${WHISPERX_COMPUTE_TYPE:-"float32"}

LOGFILE=${LOGFILE:-"/workspace/agent.log"}

echo "[onstart] Preparing to start agent with device=$WHISPERX_DEVICE compute_type=$WHISPERX_COMPUTE_TYPE"

# Ensure FUSE is present and available so rclone can mount remotes into /data/uploads
if command -v rclone >/dev/null 2>&1; then
  echo "[onstart] rclone detected; ensuring FUSE is available"
  if ! command -v fusermount3 >/dev/null 2>&1; then
    echo "[onstart] fusermount3 not found; installing fuse3 (idempotent)"
    apt-get update -y || true
    apt-get install -y fuse3 || true
  fi

  # Load kernel module if possible
  if ! ls /dev/fuse >/dev/null 2>&1; then
    echo "[onstart] trying modprobe fuse"
    modprobe fuse || true
  fi

  # Allow other users in fuse config
  if ! grep -q '^user_allow_other' /etc/fuse.conf 2>/dev/null; then
    echo "user_allow_other" >> /etc/fuse.conf || true
  fi

  # Prepare uploads dir; if it contains local files move to a timestamped backup to avoid mount failure.
  # Important: if /data/uploads is already a mountpoint (rclone active) *do not* move files from it
  # because moving will operate on the mounted remote and can delete/mutate remote data.
  if [ -d /data/uploads ] && [ "$(ls -A /data/uploads || true)" != "" ]; then
    if mountpoint -q /data/uploads; then
      echo "[onstart] /data/uploads is a mountpoint; skipping local backup/move to avoid touching mounted remote"
    else
      TS=$(date +%s)
      BACKUP_DIR="/data/uploads.backup.$TS"
      echo "[onstart] /data/uploads not mounted and not empty; moving existing files to $BACKUP_DIR"
      mkdir -p "$BACKUP_DIR"
      mv /data/uploads/* "$BACKUP_DIR" || true
    fi
  fi

  # Try mounting a few candidate remote paths. You can override RCLONE_REMOTE env to pick exact remote.
  RCLONE_REMOTE=${RCLONE_REMOTE:-"pcloudtc:transcript-coach-saas/data/uploads"}
  CANDIDATES=("$RCLONE_REMOTE" "pcloudtc:transcript-coach-saas/uploads" "pcloudtc:uploads" "pcloudtc:")

  MOUNT_OK=0
  for cand in "${CANDIDATES[@]}"; do
    echo "[onstart] attempting rclone mount of '$cand' -> /data/uploads"
    nohup rclone mount "$cand" /data/uploads --vfs-cache-mode writes --allow-other --uid 0 --gid 0 > /tmp/rclone-mount.log 2>&1 &
    sleep 3
    if mount | grep -q "/data/uploads"; then
      echo "[onstart] mounted $cand -> /data/uploads"
      MOUNT_OK=1
      break
    else
      echo "[onstart] mount of $cand failed; check /tmp/rclone-mount.log for details"
      tail -n 20 /tmp/rclone-mount.log || true
    fi
  done

  if [ "$MOUNT_OK" -ne 1 ]; then
    echo "[onstart][warning] unable to mount any rclone remote to /data/uploads; agent will still start but /data/uploads may be empty"
  fi
else
  echo "[onstart] rclone not found on this system; skipping mount step"
fi

# Use systemd-run only if systemd is PID 1 and systemd-run exists. Many cloud images don't run systemd as PID 1.
if [ "$(cat /proc/1/comm 2>/dev/null || echo '')" = "systemd" ] && command -v systemd-run >/dev/null 2>&1; then
  echo "[onstart] systemd PID 1 and systemd-run available — launching transient unit 'transcript-agent'"
  systemd-run --unit=transcript-agent --property=Restart=always --description="transcript agent" \
    --setenv=REDIS_URL="$REDIS_URL" \
    --setenv=QUEUE_NAME="$QUEUE_NAME" \
    --setenv=HF_TOKEN="$HF_TOKEN" \
    --setenv=PYANNOTE_DEVICE="$PYANNOTE_DEVICE" \
    --setenv=SKIP_EMBEDDINGS="$SKIP_EMBEDDINGS" \
    --setenv=WHISPERX_DEVICE="$WHISPERX_DEVICE" \
    --setenv=WHISPERX_COMPUTE_TYPE="$WHISPERX_COMPUTE_TYPE" \
    /usr/bin/python3 "$DEST"

  echo "[onstart] Launched transient systemd unit 'transcript-agent'. Follow logs with: journalctl -u transcript-agent -f"
  exit 0
else
  echo "[onstart] systemd-run not usable; falling back to nohup (agent will not be supervised by systemd)"
  nohup env \
    REDIS_URL="$REDIS_URL" \
    QUEUE_NAME="$QUEUE_NAME" \
    HF_TOKEN="$HF_TOKEN" \
    PYANNOTE_DEVICE="$PYANNOTE_DEVICE" \
    SKIP_EMBEDDINGS="$SKIP_EMBEDDINGS" \
    WHISPERX_DEVICE="$WHISPERX_DEVICE" \
    WHISPERX_COMPUTE_TYPE="$WHISPERX_COMPUTE_TYPE" \
    python3 "$DEST" > "$LOGFILE" 2>&1 &

  echo "[onstart] Agent started (nohup), logs -> $LOGFILE"
  exit 0
fi
