#!/bin/bash
export REG_TOKEN=REDACTED
export REPO_URL='https://github.com/mike21043/transcript-coach-saas'
export RUNNER_LABELS='self-hosted,cuda-test,transcript-coach'
# The ops/runner-cloud-init.sh will run and bootstrap the runner
bash -lc '#!/bin/bash
# Cloud-init style startup script for ephemeral GitHub self-hosted runner
# This template expects the following environment variables to be substituted
# before instance creation:
#   - REG_TOKEN  : GitHub registration token (short lived)
#   - REPO_URL   : https://github.com/<owner>/<repo>
#   - RUNNER_LABELS : comma-separated labels (e.g., "self-hosted,cuda-test,transcript-coach")

set -euo pipefail

# Basic OS packages
apt-get update
apt-get install -y ca-certificates curl jq git tar build-essential

# Install Docker
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker $SUDO_USER || true
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
./config.sh --unattended --url "$REPO_URL" --token "$REG_TOKEN" --labels "$RUNNER_LABELS" --work _work || true
./svc.sh install
./svc.sh start

# Ensure cleanup on shutdown
cat > /usr/local/bin/runner-cleanup.sh <<'''EOF'''
#!/bin/bash
set -euo pipefail
cd "$WORKDIR"
./svc.sh stop || true
./config.sh remove --token "$REG_TOKEN" || true
EOF
chmod +x /usr/local/bin/runner-cleanup.sh

# Hook into shutdown
cat > /etc/systemd/system/runner-cleanup.service <<'''EOF'''
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

# End of cloud-init script'
