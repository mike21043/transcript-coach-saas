#!/usr/bin/env bash
set -euo pipefail
VER=${1:-}
if [ -z "$VER" ]; then
  echo "Usage: $0 <tag>    e.g. $0 v1.0.0" >&2
  exit 2
fi

git tag -a "$VER" -m "Release $VER"
git push origin "$VER"

echo "Tag pushed. Watch Actions -> Publish worker images to complete." 
