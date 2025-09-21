Quick helper: `ops/create_and_push_tag.sh`

If you create tags frequently, use `ops/create_and_push_tag.sh` (make it executable) and run it from the repo root to push a release tag that triggers the publish workflow. Example:

```bash
bash ops/create_and_push_tag.sh v1.0.0
```

You can also run the workflow manually in GitHub Actions and pass a tag/name when prompted.
Worker deployment runbook

This document describes how to build, test, and run the GPU worker image for the transcript-coach agent.

1) Choose the correct base image

- Match the CUDA/cuDNN version of the host GPU drivers. You reported CUDA 12.9 on the host; pick a base image that matches the driver stack (or use the host's NVIDIA Container Toolkit with a compatible image). The Dockerfile at `agent/Dockerfile` currently targets `pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel`. If your host uses CUDA 12.9, consider switching to a tag built for CUDA 12.9 or using `nvidia/cuda:12.9.1-cudnn8-runtime-ubuntu22.04` and installing a matching torch wheel.

2) Build image locally

Run from the repository root where this README lives.

```bash
# build image (adjust tag & Dockerfile if needed)
IMAGE_NAME=transcript-coach-worker:local
docker build -t "$IMAGE_NAME" -f ./agent/Dockerfile .
```

If you prefer to control the CUDA base image, update `agent/Dockerfile` accordingly.

3) Smoke test locally

This repo includes `ops/ci_build_and_smoke.sh` that performs a local build and runs a container with `AGENT_SMOKE=1` to verify the agent starts and writes a quick result. Run it as:

```bash
bash ops/ci_build_and_smoke.sh
```

If you run into build errors because the host Docker can't access the required base image or CUDA toolkits, adjust the base image or build on a machine with a compatible NVIDIA driver and the NVIDIA Container Toolkit.

CI: A GitHub Actions workflow `CI - Build & Smoke` has been added at `.github/workflows/ci-build-smoke.yml` which builds the image and runs a smoke container on push/pull requests to `main`/`master`.

Archived results: if you ran the agent earlier on this VM, the current `/data/results` was archived into `data-backup/results-archive/` as a timestamped tarball for safekeeping.

4) Running the container in production (ephemeral GPU)

When launching ephemeral GPU instances, provide the following env vars to the container:

- `REDIS_URL` - URL for Redis (e.g. redis://x:6379/0)
- `QUEUE_NAME` - queue name used by the controller
- `RCLONE_REMOTE` - optional rclone remote target (e.g. pcloud:uploads/transcripts)
- `RCLONE_CONFIG_CONTENT` - optional base64 (or raw) rclone config to inject at container start
- `HF_TOKEN` - Hugging Face token (required for pyannote models)
- `WHISPERX_DEVICE`, `WHISPERX_COMPUTE_TYPE` - optional device and compute type
- `AGENT_SMOKE` - set to 1 to perform a fast smoke-test

Example docker run (ephemeral instance):

```bash
docker run --gpus all --rm -e REDIS_URL=redis://10.0.0.1:6379/0 \
  -e RCLONE_REMOTE="pcloud:transcripts" \
  -e RCLONE_CONFIG_CONTENT="$(cat ~/.config/rclone/rclone.conf | base64 -w0)" \
  -e HF_TOKEN="<your-hf-token>" \
  -v /host/uploads:/data/uploads -v /host/results:/data/results \
  transcript-coach-worker:local
```

5) Shut down and cleanup

When the job completes and you have persisted results, stop the container or terminate the VM.

6) Troubleshooting

- If faster-whisper or torch fails to use GPU due to cuDNN library errors, ensure your image's CUDA/cuDNN versions are compatible with the host driver. The recommended approach is to pin the image to the host driver (or use nvidia's runtime with a matching image).
- For rclone issues (missing remote), ensure `RCLONE_CONFIG_CONTENT` is correct and that `RCLONE_REMOTE` points to a valid remote path.

7) Next improvements

- Add automated CI (GitHub Actions) to build & publish images for known CUDA variations. This repo includes a publish workflow at `.github/workflows/publish-images.yml` which will build and push two tags to GitHub Container Registry (GHCR): `ghcr.io/<owner>/transcript-coach-agent:latest` and `ghcr.io/<owner>/transcript-coach-agent:cuda129`. The workflow triggers on push tags (e.g. `v1.2.3` or `release-...`).

- Add a deploy script to the controller that chooses the correct image per GPU type. The `vast-agent` controller defaults to using a `:cuda129` tag when no explicit `VAST_IMAGE` is provided; you can override this with `VAST_IMAGE` or by setting `IMAGE_REPO`/`GITHUB_OWNER` environment variables (see below).

Publishing the images (how-to)

You can trigger the publish workflow by creating and pushing a Git tag. For example:

```bash
# create an annotated tag (adjust version)
git tag -a v1.0.0 -m "Release: worker image v1.0.0"
git push origin v1.0.0
```

When the workflow runs it will build both image variants and push them to GHCR under your repository owner. You can also run the GitHub Actions workflow manually from the Actions tab and provide a tag name if you prefer.

Using the published image in the controller

The `vast-agent` controller looks for an environment variable `VAST_IMAGE` to explicitly set the container image used when creating instances. If not provided it will attempt to prefer the `:cuda129` variant from GHCR. To explicitly reference the published GHCR image, set `VAST_IMAGE` like:

```bash
export VAST_IMAGE=ghcr.io/<your-github-username-or-org>/transcript-coach-agent:cuda129
```

If you'd rather let the controller derive the owner from an environment variable, set `IMAGE_REPO` or `GITHUB_OWNER` and leave `VAST_IMAGE` unset; the controller will build a GHCR image path from that information.

Example controller env for Vast provisioning:

```bash
export VAST_API_KEY="<your-vast-key>"
export TEMPLATE_HASH="<your-template-hash>"
export GITHUB_OWNER="<your-github-username-or-org>"
export VAST_IMAGE="ghcr.io/${GITHUB_OWNER}/transcript-coach-agent:cuda129"
export PUBLIC_REDIS_URL="redis://<host>:6379/0"
```

Notes

- GHCR publishing uses the built-in `GITHUB_TOKEN` to authenticate; ensure your repository's Actions permissions allow writing packages/containers. For publishing to other registries (DockerHub, ECR) update the workflow to use the appropriate login step and secrets.
- If you expect other CUDA variants (for older GPUs) add more Dockerfiles and extend the publish workflow's build steps accordingly.

