#!/bin/bash
set -e

echo "=== Checking API service health ==="
curl -s http://localhost:8000/health || echo "API health endpoint not responding"

echo
echo "=== Checking Agent service health ==="
curl -s http://localhost:7000/health || echo "Agent health endpoint not responding"

echo
echo "=== Inspecting Agent Python environment ==="
docker compose exec -T agent python - <<'EOF'
import importlib, sys
pkgs = [
    "requests", "fastapi", "starlette", "pydantic",
    "pydantic_core", "numpy", "pandas", "whisperx",
    "pyannote.audio", "redis"
]
for pkg in pkgs:
    try:
        importlib.import_module(pkg)
        print(f"✅ {pkg} loaded")
    except Exception as e:
        print(f"❌ {pkg} missing or failed: {e}")
EOF
