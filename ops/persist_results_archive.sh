#!/usr/bin/env bash
# Archive /data/results into data-backup with timestamp
set -euo pipefail
TS=$(date -u +%Y%m%dT%H%M%SZ)
SRC_DIR=/data/results
DEST_DIR=/root/transcript-coach-saas/data-backup/results-archive
mkdir -p "$DEST_DIR"
ARCHIVE="$DEST_DIR/results-$TS.tar.gz"
if [ ! -d "$SRC_DIR" ]; then
  echo "No $SRC_DIR found; nothing to archive"
  exit 0
fi

tar -czf "$ARCHIVE" -C "$SRC_DIR" .
ls -l "$ARCHIVE"
echo "Archived $SRC_DIR -> $ARCHIVE"
