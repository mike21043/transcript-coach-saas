#!/usr/bin/env bash
set -euo pipefail

# Installs rclone and common archive utilities on Ubuntu 22.04.
# Run as root or with sudo.

echo "Installing apt packages: unzip p7zip-full curl"
apt update
apt install -y unzip p7zip-full curl

# Install rclone using official install script
echo "Installing rclone"
curl https://rclone.org/install.sh | bash

# Verify installation
echo "rclone version:"
rclone --version || true

echo "Done. You can now mount using:"
echo "rclone mount --config /root/rclone.conf pcloudtc:transcript-coach-saas/data/uploads /data/uploads --vfs-cache-mode full --allow-other --daemon"
