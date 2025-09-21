# Runner provisioning quick guide

This file explains how to generate and test the GitHub Actions self-hosted runner user-data used by `vast_agent`.

Prerequisites
- A GitHub Personal Access Token (`GITHUB_PAT`) with repository `actions:write` / `repo` or `repo:admin` permissions to create registration tokens.
- Access to a test VM (or your Vast template that executes user-data on boot).
- The repository checked out (this file lives at `ops/README-runner-provision.md`).

1) Generate user-data locally
From the repository root:

```bash
cd /root/transcript-coach-saas
chmod +x scripts/provision_runner.sh
# Replace GH_PAT_HERE with a short-lived PAT (do NOT share it)
GITHUB_PAT=GH_PAT_HERE ./scripts/provision_runner.sh <owner> <repo> /tmp/runner-user-data.sh
```

This writes `/tmp/runner-user-data.sh` which exports `REG_TOKEN`, `REPO_URL` and `RUNNER_LABELS` and then invokes `ops/runner-cloud-init.sh` to bootstrap the runner.

2) Inspect user-data
- `cat /tmp/runner-user-data.sh` should show an exported `REG_TOKEN` (short-lived) and the bootstrap invocation.

3) Manual VM test
- Copy to your test VM and run it immediately since the token expires quickly:

```bash
scp /tmp/runner-user-data.sh user@vm:/tmp/
ssh user@vm 'sudo bash /tmp/runner-user-data.sh'
```

- The script will install the GitHub runner and register it with labels defined by `RUNNER_LABELS` (defaults to `self-hosted,cuda-test,transcript-coach`).

4) Automated Vast test (if using `vast_agent`)
- Ensure your Vast template runs an OnStart that decodes `RUNNER_USER_DATA_B64` and executes it, e.g.:

```sh
echo "$RUNNER_USER_DATA_B64" | base64 -d > /tmp/runner-user-data.sh
chmod +x /tmp/runner-user-data.sh
bash /tmp/runner-user-data.sh
```

- Run `vast_agent` with `VAST_PROVISION_RUNNER=true` and valid `VAST_API_KEY` and `VAST_TEMPLATE_HASH`.

Safety notes
- The registration token is short-lived — provision immediately.
- Do not paste tokens or PATs into public chat or logs.
- For large payloads, prefer cloud-init or artifact delivery instead of embedding via env.

Troubleshooting
- If the runner doesn't appear in GitHub, ensure token scopes and repository-owner are correct and check `ops/runner-cloud-init.sh` logs on the instance.

Contact
- If you'd like, I can modify `vast_agent` to deliver cloud-init via the Vast API instead of embedding the user-data into env (recommended for production).