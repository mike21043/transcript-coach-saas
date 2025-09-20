#!/usr/bin/env bash
# Usage: ./enqueue_master.sh <user@main-vps> <filename>
# Copies enqueue_to_master.py to the main VPS and runs it there.
set -euo pipefail
if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <user@main-vps> <filename>"
  exit 2
fi
TARGET="$1"
FNAME="$2"
scp -q ./scripts/enqueue_to_master.py "$TARGET":/tmp/enqueue_to_master.py
ssh "$TARGET" "python3 /tmp/enqueue_to_master.py '$FNAME'"
