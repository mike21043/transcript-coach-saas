#!/usr/bin/env bash
# Optional helper to install pyannote dependencies in a venv or image
set -euo pipefail

python3 -m venv /opt/tc-venv
source /opt/tc-venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install pyannote.audio==3.3.2 pyannote.core==6.0.0 pyannote.metrics==4.0.0
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

echo "Pyannote + torch installed in /opt/tc-venv"
