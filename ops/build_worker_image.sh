#!/usr/bin/env bash
# Build a local Docker image for the agent worker
set -euo pipefail

IMAGE_NAME=${IMAGE_NAME:-transcript-coach-worker:local}
VARIANT=${VARIANT:-default}
DOCKERFILE_PATH=./agent/Dockerfile

if [ "${VARIANT}" = "cuda129" ]; then
	DOCKERFILE_PATH=./agent/Dockerfile.cuda129
	IMAGE_NAME=${IMAGE_NAME//:/:cuda129-}
fi

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
echo "Building image ${IMAGE_NAME} from ${DOCKERFILE_PATH} (variant=${VARIANT}) using context ${REPO_ROOT}"
docker build -t "${IMAGE_NAME}" -f "${REPO_ROOT}/${DOCKERFILE_PATH#./}" "${REPO_ROOT}"

echo "Built ${IMAGE_NAME}"