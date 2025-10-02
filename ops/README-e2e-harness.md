This file has been consolidated into `DEVELOPER.md`. Please open `DEVELOPER.md` in the repository root for the canonical developer guide and e2e harness instructions.

E2E Provision + Test Harness

This document explains `scripts/e2e_provision_and_test.py` which performs an end-to-end provisioning and test flow.

What it does
- Calls `scripts/provision_runner.sh` to generate runner user-data
- Calls Vast API to create an instance with `RUNNER_USER_DATA_B64` and `RUNNER_LABELS` in its env
- Waits for the instance to become running
- Polls GitHub for the runner registration (looking for label from `VAST_RUNNER_LABELS`)
- Dispatches the `smoke-published.yml` workflow and waits for completion

Required environment variables for a real run (non-dry)
- VAST_API_KEY
- VAST_TEMPLATE_HASH
- GITHUB_PAT (with repo actions runner registration permission)
- Optionally: GITHUB_OWNER, IMAGE_REPO

Dry run
- Use `--dry` to generate the user-data and validate it locally without performing any remote calls.

Example (dry run):

```bash
cd /root/transcript-coach-saas
python3 scripts/e2e_provision_and_test.py --dry
```

Example (real run):

```bash
export VAST_API_KEY=...
export VAST_TEMPLATE_HASH=...
export GITHUB_PAT=...
python3 scripts/e2e_provision_and_test.py
```

Caveats
- Running the real script will incur charges on Vast for instance time.
- The registration token is short-lived — the instance must execute the user-data immediately.
- This script is a convenience harness. For production, prefer direct cloud-init delivery via Vast API if available.

Providing private registry credentials
------------------------------------

If your agent image is hosted on a private registry (for example `ghcr.io`), provide a base64-encoded
Docker `config.json` in the `DOCKER_CONFIG_B64` environment variable when running the harness. The
cloud-init will write this to `/root/.docker/config.json` on the instance so `docker pull` can authenticate.

You can generate the base64 config locally with the included helper script:

```bash
# using an env var
GITHUB_PAT=ghp_xxx ./scripts/generate_docker_config.sh > docker_config.b64

# or interactively
./scripts/generate_docker_config.sh --interactive > docker_config.b64
```

Then export it before running a real harness run:

```bash
export DOCKER_CONFIG_B64=$(cat docker_config.b64)
export VAST_API_KEY=...
export VAST_TEMPLATE_HASH=...
export GITHUB_PAT=...
FORCE=1 ./scripts/run_e2e.sh --image ubuntu:22.04 --no-template
```

If you prefer not to provide registry credentials, the harness will skip authenticated pulls and you
must ensure the instance can access your image (public image or build-on-boot). For debugging, you can
also set `INSTALL_DOCKER=0` to ensure no docker install/pulls happen on the instance.
