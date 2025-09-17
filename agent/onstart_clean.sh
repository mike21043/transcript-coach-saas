#!/bin/bash
set -e

echo "[OnStart] Starting Transcript Coach GPU container..."

mkdir -p /data/uploads /data/results /workspace

nohup /app/start.sh >> /workspace/agent.log 2>&1 &

echo "[OnStart] Setup complete. Agent running in background."
