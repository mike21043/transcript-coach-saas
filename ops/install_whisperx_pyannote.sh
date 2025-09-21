#!/usr/bin/env bash
# Helper to install whisperx and pyannote into a venv (for local dev)
set -euo pipefail
python3 -m venv /opt/tc-venv
source /opt/tc-venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r /root/transcript-coach-saas/agent/requirements.txt

echo "Installed whisperx, faster-whisper, pyannote and dependencies into /opt/tc-venv"
