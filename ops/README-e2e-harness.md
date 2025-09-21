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
