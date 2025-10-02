#!/bin/bash
# Cloud-init style startup script for ephemeral GitHub self-hosted runner
# This template expects the following environment variables to be substituted
# before instance creation:
#   - REG_TOKEN  : GitHub registration token (short lived)
#   - REPO_URL   : https://github.com/<owner>/<repo>
#   - RUNNER_LABELS : comma-separated labels (e.g., "self-hosted,cuda-test,transcript-coach")

set -euo pipefail

# ensure SUDO_USER has a sensible default when run via cloud-init
SUDO_USER=${SUDO_USER:-ubuntu}

# bootstrap logging: capture stdout/stderr to a persistent file for diagnostics
LOG_FILE="/var/log/runner-bootstrap.log"
mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"
chown root:root "$LOG_FILE"
chmod 644 "$LOG_FILE"
# tee the log so we can still see console output in instance logs
exec > >(tee -a "$LOG_FILE") 2>&1
set -x

# Emit an early base64-wrapped snapshot of the bootstrap log to the provider
# console so remote callers can fetch it from provider console endpoints even
# if SSH or rclone uploads are unavailable. This prints markers that the
# harness will scan for and decode.
echo "[early] BEGIN-BASE64-BOOTSTRAP-LOG"
# -w0 avoids line wrapping (GNU base64). On systems lacking -w, this will
# still work because base64 ignores unknown options; keep a portable fallback.
if base64 --help >/dev/null 2>&1; then
  base64 -w0 "$LOG_FILE" || true
else
  base64 "$LOG_FILE" || true
fi
echo "[early] END-BASE64-BOOTSTRAP-LOG"


# If a base64-encoded reg token was provided, decode it early into REG_TOKEN
if [ -n "${REG_TOKEN_B64:-}" ]; then
  # Use printf -n to avoid trailing newlines
  REG_TOKEN=$(printf '%s' "$REG_TOKEN_B64" | base64 -d 2>/dev/null || echo "$REG_TOKEN_B64")
  export REG_TOKEN
fi
# Background periodic emitter: repeatedly print the bootstrap log (base64)
# every 10s up to a limit while bootstrapping. This improves the chance the
# provider console or serial capture includes at least one payload. The loop
# will stop when the sentinel file $LOG_DONE is present.
LOG_DONE="/var/log/runner-bootstrap-done"
# Allow tuning of the periodic emitter so we can increase frequency in
# diagnostic runs without changing the rest of the script. For diagnostics
# we default to a denser emitter: emit every 2s up to 300 times (~10m).
# These env vars can be overridden when rendering user-data to adapt behavior
# per-run without editing this file.
: "${PERIODIC_EMIT_INTERVAL:=2}"
: "${PERIODIC_EMIT_MAX:=300}"
periodic_emit() {
  local i=0
  while [ $i -lt "$PERIODIC_EMIT_MAX" ] && [ ! -f "$LOG_DONE" ]; do
    echo "[periodic] BEGIN-BASE64-BOOTSTRAP-LOG"
    if base64 --help >/dev/null 2>&1; then
      base64 -w0 "$LOG_FILE" || true
    else
      base64 "$LOG_FILE" || true
    fi
    echo "[periodic] END-BASE64-BOOTSTRAP-LOG"
    i=$((i+1))
    sleep "$PERIODIC_EMIT_INTERVAL"
  done
}
# run in background
periodic_emit &

# Optional: public paste uploader (opt-in). When UPLOAD_PASTE=1 is set in the
# provisioning environment, this background task will wait for the bootstrap
# log to become non-empty and then attempt to upload it to a public paste
# service (0x0.st). The returned URL is printed so the provisioning harness
# or caller can capture it. This is intended for short-lived diagnostic
# captures when other console/SSH channels are unavailable. Make this opt-in
# because it posts logs to a public service.
paste_uploader() {
  # Only run if explicitly requested
  if [ "${UPLOAD_PASTE:-0}" != "1" ]; then
    return 0
  fi

  echo "[paste] public paste uploader enabled; will attempt to upload $LOG_FILE to 0x0.st when available"

  # Wait for curl to be available (fastpath installs curl early but ensure)
  remain=60
  while [ $remain -gt 0 ] && ! command -v curl >/dev/null 2>&1; do
    sleep 1
    remain=$((remain-1))
  done

  attempt=0
  # Try a bounded number of times to avoid spinning forever
  while [ $attempt -lt 120 ] && [ ! -f "$LOG_DONE" ]; do
    attempt=$((attempt+1))
    if [ -s "$LOG_FILE" ]; then
      echo "[paste] attempt $attempt: uploading non-empty $LOG_FILE to https://0x0.st"
      # Upload the file to 0x0.st and capture the single-line response
      url=$(curl -sS -F file=@"$LOG_FILE" https://0x0.st 2>/dev/null || true)
      if [ -n "$url" ]; then
        echo "[paste] upload succeeded: $url"
        # Persist the URL locally for later retrieval
        echo "$url" > /tmp/runner-bootstrap-paste.url || true
        # Also emit to stdout so provider console (if captured) contains it
        echo "[paste] URL: $url"
        break
      else
        echo "[paste] upload attempt $attempt failed or returned empty response"
      fi
    fi
    sleep 3
  done

  echo "[paste] uploader exiting"
}
paste_uploader &

# Fast-path: install minimal packages and configure the GitHub Actions runner
# as early as possible so the short-lived GitHub registration token is used
# before it expires. The heavier installs (Docker/NVIDIA) happen later.
echo "[fastpath] Installing minimal packages and attempting early runner config"
apt-get update || true
apt-get install -y ca-certificates curl tar jq git || true

# Setup workspace and download runner
WORKDIR="/home/runner/actions-runner"
mkdir -p "$WORKDIR"
chown -R $SUDO_USER:$SUDO_USER "$WORKDIR"
cd "$WORKDIR"

RUNNER_VERSION="2.298.0"
ARCHIVE="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
if [ ! -f "$ARCHIVE" ]; then
  curl -O -L "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${ARCHIVE}" || true
  tar xzf "$ARCHIVE" || true
fi

# If REG_TOKEN is present, try to configure the runner immediately (fast path).
if [ -n "${REG_TOKEN:-}" ] && [ ! -f "$WORKDIR/.runner_config_done" ]; then
  echo "[fastpath] Running config.sh (fast)"
  set +e
  ./config.sh --unattended --url "$REPO_URL" --token "$REG_TOKEN" --labels "$RUNNER_LABELS" --work _work
  rc=$?
  set -e
  if [ $rc -eq 0 ]; then
    echo "[fastpath] config.sh succeeded"
    touch "$WORKDIR/.runner_config_done"
    ./svc.sh install || true
    ./svc.sh start || true
    # mark done so the periodic emitter can stop
    touch "$LOG_DONE" || true
  else
    echo "[fastpath] config.sh failed with rc=$rc; continuing to bootstrap (full path will capture logs)"
  fi
fi

# Prepare for best-effort log upload on any exit. This trap will attempt to upload
# the bootstrap log to the configured remote (UPLOAD_LOG_REMOTE) using rclone.
# It will not mask the original exit code.
upload_logs_on_exit() {
  local rc=$?
  if [ -n "${UPLOAD_LOG_REMOTE:-}" ]; then
    # If RCLONE_CONF_B64 provided, write it early so the trap can use it
    if [ -n "${RCLONE_CONF_B64:-}" ]; then
      echo "Decoding provided RCLONE_CONF_B64 to /root/rclone.conf"
      echo "$RCLONE_CONF_B64" | base64 -d > /root/rclone.conf || true
      mkdir -p /root/.config/rclone
      cp /root/rclone.conf /root/.config/rclone/rclone.conf || true
    fi
    # Ensure rclone exists (best-effort, don't fail the trap)
    if ! command -v rclone >/dev/null 2>&1; then
      curl https://rclone.org/install.sh | bash >/dev/null 2>&1 || true
    fi
    if [ -f /root/.config/rclone/rclone.conf ]; then
      TS=$(date -u +%Y%m%dT%H%M%SZ)
      BASENAME="$(hostname)-runner-bootstrap-${TS}.log"
      echo "[trap] Attempting rclone copyto $LOG_FILE -> $UPLOAD_LOG_REMOTE/$BASENAME"
      rclone copyto "$LOG_FILE" "$UPLOAD_LOG_REMOTE/$BASENAME" --progress >/dev/null 2>&1 || echo "[trap] rclone upload failed"
    else
      echo "[trap] UPLOAD_LOG_REMOTE set but rclone config missing; skipping upload"
    fi
      # As a last-resort, print the bootstrap log to the provider console (base64 wrapped)
      if [ -f "$LOG_FILE" ]; then
        echo "[trap] BEGIN-BASE64-BOOTSTRAP-LOG"
        base64 "$LOG_FILE" || true
        echo "[trap] END-BASE64-BOOTSTRAP-LOG"
      else
        echo "[trap] No $LOG_FILE to dump"
      fi
  fi
  return $rc
}
trap upload_logs_on_exit EXIT

# Background uploader: attempt to upload the bootstrap log periodically while
# bootstrapping so we don't rely only on the EXIT trap (which may not run if
# the instance is forcibly terminated). The uploader decodes RCLONE_CONF_B64
# early (if provided), ensures rclone exists, and repeatedly tries to copy
# the log to UPLOAD_LOG_REMOTE until success or until $LOG_DONE is present.
background_rclone_uploader() {
  # Only run if an upload remote is configured
  if [ -z "${UPLOAD_LOG_REMOTE:-}" ]; then
    return 0
  fi

  echo "[uploader] background uploader starting (remote=$UPLOAD_LOG_REMOTE)"

  # Decode provided rclone config early so background uploader can use it
  if [ -n "${RCLONE_CONF_B64:-}" ]; then
    echo "[uploader] decoding RCLONE_CONF_B64 to /root/rclone.conf"
    mkdir -p /root/.config/rclone || true
    echo "$RCLONE_CONF_B64" | base64 -d > /root/rclone.conf 2>/dev/null || true
    cp /root/rclone.conf /root/.config/rclone/rclone.conf 2>/dev/null || true
    chmod 600 /root/rclone.conf || true
    chmod 600 /root/.config/rclone/rclone.conf 2>/dev/null || true
  fi

  # Ensure rclone is available (best-effort). Installation may be slow; do not
  # fail the boot process if install fails.
  if ! command -v rclone >/dev/null 2>&1; then
    echo "[uploader] rclone not found; attempting install"
    curl -sS https://rclone.org/install.sh | bash >/dev/null 2>&1 || echo "[uploader] rclone install attempt failed"
  fi

  # Prepare a stable basename for uploads so multiple attempts don't create
  # wildly different filenames. Use UTC timestamp.
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  BASENAME="$(hostname)-runner-bootstrap-${TS}.log"

  # Try a bounded number of attempts but keep it generous to survive transient
  # network issues. Make defaults more aggressive for diagnostics: many
  # attempts with a small base sleep. These can be overridden by environment
  # variables when launching the instance.
  max_attempts=${RCLONE_UPLOAD_MAX_ATTEMPTS:-120}
  base_sleep=${RCLONE_UPLOAD_BASE_SECS:-2}
  # cap the exponential backoff so we don't sleep ridiculously long
  max_sleep_cap=${RCLONE_UPLOAD_MAX_SLEEP_SECS:-120}
  attempt=0
  while [ $attempt -lt $max_attempts ] && [ ! -f "$LOG_DONE" ]; do
    attempt=$((attempt+1))
    if command -v rclone >/dev/null 2>&1 && [ -f /root/.config/rclone/rclone.conf ]; then
      echo "[uploader] attempt $attempt: rclone copyto $LOG_FILE -> $UPLOAD_LOG_REMOTE/$BASENAME"
      if rclone copyto "$LOG_FILE" "$UPLOAD_LOG_REMOTE/$BASENAME" --progress >/dev/null 2>&1; then
        echo "[uploader] upload succeeded on attempt $attempt -> $UPLOAD_LOG_REMOTE/$BASENAME"
        # mark uploaded so other processes can check
        touch /var/log/runner-bootstrap-uploaded || true
        break
      else
        echo "[uploader] upload attempt $attempt failed"
      fi
    else
      echo "[uploader] rclone or config missing; retrying"
    fi
    # backoff with jitter. Use exponential growth but cap the max sleep.
    sleep_secs=$(( base_sleep * (2 ** (attempt - 1)) ))
    if [ $sleep_secs -gt $max_sleep_cap ]; then
      sleep_secs=$max_sleep_cap
    fi
    # small jitter (0..5s) to avoid thundering herd
    jitter=$((RANDOM % 6))
    sleep_time=$((sleep_secs + jitter))
    echo "[uploader] sleeping ${sleep_time}s before next attempt (base=${base_sleep}, attempt=${attempt})"
    sleep $sleep_time
  done
  echo "[uploader] background uploader exiting"
}

# Start the background uploader (it will no-op if UPLOAD_LOG_REMOTE isn't set)
background_rclone_uploader &

# Background workspace uploader: tar the runner workspace and journal and
# attempt to upload periodically to the configured remote. This increases
# the chance of capturing workflow-level logs (e.g. docker build failures)
# which may occur after the runner has registered and started executing jobs.
background_workspace_uploader() {
  # Only run when an upload remote is configured
  if [ -z "${UPLOAD_LOG_REMOTE:-}" ]; then
    return 0
  fi

  echo "[workspace-uploader] starting (remote=$UPLOAD_LOG_REMOTE)"

  # Ensure rclone config decoded if provided
  if [ -n "${RCLONE_CONF_B64:-}" ]; then
    mkdir -p /root/.config/rclone || true
    echo "$RCLONE_CONF_B64" | base64 -d > /root/rclone.conf 2>/dev/null || true
    cp /root/rclone.conf /root/.config/rclone/rclone.conf 2>/dev/null || true
    chmod 600 /root/rclone.conf 2>/dev/null || true
  fi

  # install rclone if missing (best-effort)
  if ! command -v rclone >/dev/null 2>&1; then
    echo "[workspace-uploader] rclone not found; attempting install"
    curl -sS https://rclone.org/install.sh | bash >/dev/null 2>&1 || echo "[workspace-uploader] rclone install attempt failed"
  fi

  # Loop until LOG_DONE is present (boot complete) or until we succeed
  while [ ! -f "$LOG_DONE" ]; do
    TS=$(date -u +%Y%m%dT%H%M%SZ)
    WORK_TAR="/tmp/runner-work-${TS}.tar.gz"
    JNL_FILE="/var/log/runner-journal-${TS}.log"

    echo "[workspace-uploader] creating workspace tar $WORK_TAR"
    # Try to archive the runner workspace; tolerate failures
    if [ -d "/home/runner/_work" ]; then
      tar -czf "$WORK_TAR" -C /home/runner "_work" 2>/dev/null || true
    else
      # fallback: try to capture whatever is in /home/runner
      tar -czf "$WORK_TAR" -C /home/runner . 2>/dev/null || true
    fi

    # Capture systemd journal (best-effort) for recent boots
    if command -v journalctl >/dev/null 2>&1; then
      journalctl -b -o short-iso > "$JNL_FILE" 2>/dev/null || true
    else
      # Fallback: capture dmesg and syslog if journalctl is missing
      dmesg > "$JNL_FILE" 2>/dev/null || true
    fi

    # Attempt upload of both files when rclone/config available
    if command -v rclone >/dev/null 2>&1 && [ -f /root/.config/rclone/rclone.conf ]; then
      echo "[workspace-uploader] attempt rclone copyto $WORK_TAR -> $UPLOAD_LOG_REMOTE/"
      if [ -f "$WORK_TAR" ]; then
        if rclone copyto "$WORK_TAR" "$UPLOAD_LOG_REMOTE/$(basename $WORK_TAR)" --progress >/dev/null 2>&1; then
          echo "[workspace-uploader] workspace upload succeeded -> $UPLOAD_LOG_REMOTE/$(basename $WORK_TAR)"
        else
          echo "[workspace-uploader] workspace upload failed"
        fi
      else
        echo "[workspace-uploader] workspace tar $WORK_TAR missing; skipping"
      fi
      if [ -f "$JNL_FILE" ]; then
        if rclone copyto "$JNL_FILE" "$UPLOAD_LOG_REMOTE/$(basename $JNL_FILE)" --progress >/dev/null 2>&1; then
          echo "[workspace-uploader] journal upload succeeded -> $UPLOAD_LOG_REMOTE/$(basename $JNL_FILE)"
        else
          echo "[workspace-uploader] journal upload failed"
        fi
      else
        echo "[workspace-uploader] journal file $JNL_FILE missing; skipping"
      fi
    else
      echo "[workspace-uploader] rclone or config missing; will retry later"
    fi

    # Sleep a minute between attempts to avoid spamming the remote
    sleep 60
  done

  echo "[workspace-uploader] exiting"
}

background_workspace_uploader &

# If a base64-encoded Docker config is supplied, decode it early so it's available
# for any later steps (including optional Docker installation). This prevents
# 'denied' errors when pulling images if config is present but Docker is installed
# later in the boot process.
if [ -n "${DOCKER_CONFIG_B64:-}" ]; then
  echo "Decoding provided DOCKER_CONFIG_B64 to /root/.docker/config.json"
  mkdir -p /root/.docker
  echo "$DOCKER_CONFIG_B64" | base64 -d > /root/.docker/config.json || true
  chmod 600 /root/.docker/config.json || true
fi

# Basic OS packages
apt-get update
apt-get install -y ca-certificates curl jq git tar build-essential

# Install Docker and NVIDIA toolkit only when explicitly requested.
# Prevents on-instance docker pulls which often trigger "unauthorized" errors
# in environments without registry credentials. To enable, set INSTALL_DOCKER=1
if [ "${INSTALL_DOCKER:-0}" = "1" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "INSTALL_DOCKER=1: installing docker"
    curl -fsSL https://get.docker.com | sh
    usermod -aG docker "$SUDO_USER" || true
  fi

  # If a base64-encoded Docker config is supplied, write it so docker can authenticate to private registries
  if [ -n "${DOCKER_CONFIG_B64:-}" ]; then
    echo "Writing provided DOCKER_CONFIG_B64 to /root/.docker/config.json"
    mkdir -p /root/.docker
    echo "$DOCKER_CONFIG_B64" | base64 -d > /root/.docker/config.json || true
    chmod 600 /root/.docker/config.json || true
  fi

  # Install NVIDIA container toolkit if GPU present (best-effort)
  if command -v nvidia-smi >/dev/null 2>&1; then
    # add NVIDIA package repos (Ubuntu example)
    distribution="$(. /etc/os-release; echo $ID$VERSION_ID)"
    curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | apt-key add -
    curl -s -L https://nvidia.github.io/nvidia-docker/${distribution}/nvidia-docker.list | tee /etc/apt/sources.list.d/nvidia-docker.list
    apt-get update
    apt-get install -y nvidia-container-toolkit
    systemctl restart docker || true
  else
    # Best-effort: attempt to install NVIDIA drivers on Ubuntu if none present.
    # This helps the container runtime detect devices for NVIDIA GPUs.
    if command -v ubuntu-drivers >/dev/null 2>&1; then
      echo "No nvidia-smi found; attempting ubuntu-drivers autoinstall (best-effort)"
      set +e
      ubuntu-drivers autoinstall || true
      set -e
      # Allow drivers to settle and re-probe
      sleep 5
      if command -v nvidia-smi >/dev/null 2>&1; then
        echo "nvidia-smi available after driver install; attempting nvidia-container-toolkit"
        distribution="$(. /etc/os-release; echo $ID$VERSION_ID)"
        curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | apt-key add -
        curl -s -L https://nvidia.github.io/nvidia-docker/${distribution}/nvidia-docker.list | tee /etc/apt/sources.list.d/nvidia-docker.list
        apt-get update
        apt-get install -y nvidia-container-toolkit || true
        systemctl restart docker || true
      else
        echo "nvidia-smi still not available after attempted driver install; continuing without nvidia-container-toolkit"
      fi
    fi
  fi
else
  echo "INSTALL_DOCKER not set (default); skipping Docker and NVIDIA toolkit installation to avoid registry auth issues"
fi

# If a TARGET_AGENT_IMAGE is provided in the environment and Docker is
# installed with credentials, attempt a best-effort authenticated pull after
# installation completes. This avoids provider pre-pull attempts which may
# happen before credentials are available and cause 'denied' or 'invalid
# reference format' errors.
if [ "${INSTALL_DOCKER:-0}" = "1" ] && [ -n "${TARGET_AGENT_IMAGE:-}" ]; then
  echo "Attempting to docker pull TARGET_AGENT_IMAGE=${TARGET_AGENT_IMAGE}"
  # retry a few times with backoff to account for transient network issues
  attempt=0
  until [ $attempt -ge 5 ]
  do
    if docker pull "${TARGET_AGENT_IMAGE}"; then
      echo "Successfully pulled ${TARGET_AGENT_IMAGE}"
      break
    fi
    attempt=$((attempt+1))
    echo "docker pull failed for ${TARGET_AGENT_IMAGE}; retrying in 5s (attempt $attempt)"
    sleep 5
  done
fi

# Setup workspace
WORKDIR="/home/runner/actions-runner"
mkdir -p "$WORKDIR"
chown -R $SUDO_USER:$SUDO_USER "$WORKDIR"
cd "$WORKDIR"

# Download GitHub Actions runner
RUNNER_VERSION="2.298.0"
ARCHIVE="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
if [ ! -f "$ARCHIVE" ]; then
  curl -O -L "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${ARCHIVE}"
  tar xzf "$ARCHIVE"
fi

# Configure runner (REG_TOKEN substituted by provisioner)
if [ -z "${REG_TOKEN:-}" ]; then
  echo "REG_TOKEN not provided; exiting"
  exit 1
fi

# Default repo URL if not provided
: "${REPO_URL:=https://github.com/mike21043/transcript-coach-saas}"
: "${RUNNER_LABELS:=self-hosted,cuda-test}"

# Configure and install as service
# Run config.sh and capture failures: if config.sh fails, emit a base64-wrapped
# bootstrap log immediately so the provisioning harness can fetch it from
# the provider console for debugging.
./config.sh --unattended --url "$REPO_URL" --token "$REG_TOKEN" --labels "$RUNNER_LABELS" --work _work || {
  echo "[config-failed] BEGIN-BASE64-BOOTSTRAP-LOG"
  if base64 --help >/dev/null 2>&1; then
    base64 -w0 "$LOG_FILE" || true
  else
    base64 "$LOG_FILE" || true
  fi
  echo "[config-failed] END-BASE64-BOOTSTRAP-LOG"
  # exit non-zero so cloud provisioning captures the failure
  exit 1
}
./svc.sh install
./svc.sh start

# After starting the runner service, emit another base64 snapshot of the
# bootstrap log to ensure we have a copy reflecting any runtime errors.
echo "[post-start] BEGIN-BASE64-BOOTSTRAP-LOG"
if base64 --help >/dev/null 2>&1; then
  base64 -w0 "$LOG_FILE" || true
else
  base64 "$LOG_FILE" || true
fi
echo "[post-start] END-BASE64-BOOTSTRAP-LOG"
# mark done so periodic emitter can stop
touch "$LOG_DONE" || true

# NOTE: agent image pull/build removed from cloud-init to avoid triggering docker auth errors

# Ensure cleanup on shutdown
cat > /usr/local/bin/runner-cleanup.sh <<'EOF'
#!/bin/bash
set -euo pipefail
cd "$WORKDIR"
./svc.sh stop || true
./config.sh remove --token "$REG_TOKEN" || true
EOF
chmod +x /usr/local/bin/runner-cleanup.sh

# Hook into shutdown
cat > /etc/systemd/system/runner-cleanup.service <<'EOF'
[Unit]
Description=Cleanup GitHub runner on shutdown
DefaultDependencies=no
Before=shutdown.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/runner-cleanup.sh

[Install]
WantedBy=shutdown.target
EOF

systemctl daemon-reload
systemctl enable runner-cleanup.service || true

# End of cloud-init script

# Optional: upload bootstrap log to remote via rclone if requested.
# To enable, the provisioning environment should inject /root/rclone.conf and set UPLOAD_LOG_REMOTE
# Example UPLOAD_LOG_REMOTE value: "pcloudtc:transcript-coach-logs/" (see repo rclone.conf for an example remote)
if [ -n "${UPLOAD_LOG_REMOTE:-}" ]; then
  # If RCLONE_CONF_B64 provided, decode it to /root/rclone.conf
  if [ -n "${RCLONE_CONF_B64:-}" ]; then
    echo "$RCLONE_CONF_B64" | base64 -d > /root/rclone.conf || true
  fi
  if [ -f /root/rclone.conf ]; then
    echo "Attempting to upload $LOG_FILE to $UPLOAD_LOG_REMOTE via rclone"
  else
    echo "UPLOAD_LOG_REMOTE set but /root/rclone.conf missing; skipping upload"
  fi
  # install rclone (deb) if missing (best-effort)
  if ! command -v rclone >/dev/null 2>&1; then
    curl https://rclone.org/install.sh | bash || true
  fi
  # create rclone config dir and copy provided config
  mkdir -p /root/.config/rclone
  cp /root/rclone.conf /root/.config/rclone/rclone.conf || true
  # attempt upload (timestamped filename)
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  BASENAME="$(hostname)-runner-bootstrap-${TS}.log"
  rclone copyto "$LOG_FILE" "$UPLOAD_LOG_REMOTE/$BASENAME" --progress || echo "rclone upload failed"

  # Fallback: always emit the bootstrap log as base64 to stdout so provider console/serial captures it
  if [ -f "$LOG_FILE" ]; then
    echo "\nFALLBACK-BEGIN-BASE64-BOOTSTRAP-LOG"
    base64 "$LOG_FILE" || true
    echo "FALLBACK-END-BASE64-BOOTSTRAP-LOG"
  else
    echo "Fallback: no $LOG_FILE present"
  fi
fi
