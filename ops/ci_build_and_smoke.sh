#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME=${IMAGE_NAME:-transcript-coach-worker:ci}
DOCKERFILE=${DOCKERFILE:-./agent/Dockerfile}

echo "Building ${IMAGE_NAME} from ${DOCKERFILE}..."
docker build -t "${IMAGE_NAME}" -f "${DOCKERFILE}" .

# Run a container with AGENT_SMOKE to validate it starts and writes results
TMP_DIR=$(mktemp -d)
mkdir -p "${TMP_DIR}/uploads" "${TMP_DIR}/results"
echo "dummy audio placeholder" > "${TMP_DIR}/uploads/dummy.txt"

docker run --rm \
  -e AGENT_SMOKE=1 \
  -v "${TMP_DIR}/uploads:/data/uploads" \
  -v "${TMP_DIR}/results:/data/results" \
  "${IMAGE_NAME}"

echo "Smoke run complete. Results in: ${TMP_DIR}/results"
