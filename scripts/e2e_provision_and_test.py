#!/usr/bin/env python3
"""
End-to-end provisioning harness:
- Generates runner user-data (calls scripts/provision_runner.sh)
- Calls Vast API to create an instance with embedded RUNNER_USER_DATA_B64
- Waits for instance to become running
- Polls GitHub for the registered runner with the expected label
- Dispatches the smoke workflow and waits for completion

This script requires the following env vars when running non-dry:
 - VAST_API_KEY
 - VAST_TEMPLATE_HASH
 - GITHUB_PAT
 - GITHUB_OWNER (or provide --owner)
 - IMAGE_REPO (or provide --repo)

Use --dry to validate steps without performing remote actions.
"""

import os
import sys
import time
import json
import base64
import tempfile
import subprocess
from pathlib import Path
import re

try:
    import requests
except Exception as e:
    print("requests library is required. Install with: pip install requests")
    raise

VAST_BASE = "https://console.vast.ai/api/v0"

def api_headers(vast_api_key: str):
    return {"Authorization": f"Bearer {vast_api_key}", "Accept": "application/json", "Content-Type": "application/json"}

def vast_req(method, path, token, body=None, timeout=60):
    url = f"{VAST_BASE}{path}"
    r = requests.request(method, url, headers=api_headers(token), json=body, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"raw": r.text, "status": r.status_code}

def search_offers(vast_api_key, min_gpus=1, only_verified=True):
    q = {"rentable": {"eq": True}, "rented": {"eq": False}, "num_gpus": {"gte": min_gpus}}
    if only_verified:
        q["verified"] = {"eq": True}
    return vast_req("PUT", "/search/asks/", vast_api_key, {"q": q}).get("offers", [])

def pick_best(offers):
    if not offers:
        return None
    def score(o):
        try:
            return float(o.get("dlperf_usd") or o.get("dlperf_usd_per_sec") or o.get("dlperf_usd_per_hour") or 1e9)
        except Exception:
            return 1e9
    return sorted(offers, key=score)[0]

def create_instance(vast_api_key, ask_id, template_hash, ud_b64, runner_labels, disk_gb=32):
    body = {
        "disk": disk_gb,
        "label": f"e2e-{int(time.time())}",
        "env": {
            "RUNNER_USER_DATA_B64": ud_b64,
            "RUNNER_LABELS": runner_labels,
        },
        "target_state": "running",
        "template_hash": template_hash,
    }
    return vast_req("PUT", f"/asks/{ask_id}/", vast_api_key, body)

def show_instance(vast_api_key, inst_id):
    return vast_req("GET", f"/instances/{inst_id}/", vast_api_key)

def list_instances(vast_api_key):
    return vast_req("GET", "/instances/", vast_api_key).get("instances", [])

def wait_until_running(vast_api_key, inst_id, timeout_sec=900):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            info = show_instance(vast_api_key, inst_id)
            inst = info.get("instances", info)
            status = str(inst.get("cur_state") or inst.get("actual_status") or inst.get("intended_status") or inst.get("state") or "").lower()
            print(f"Instance {inst_id} status={status}")
            if any(s in status for s in ("open", "running", "active", "connected")):
                return True
        except Exception as e:
            print(f"Waiting: error checking instance {inst_id}: {e}")
        time.sleep(10)
    return False

# GitHub helpers
GITHUB_API = "https://api.github.com"

def gh_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

def list_repo_runners(owner, repo, token):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runners"
    r = requests.get(url, headers=gh_headers(token), timeout=30)
    r.raise_for_status()
    return r.json().get("runners", [])

def dispatch_workflow(owner, repo, workflow_filename, ref, token):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/workflows/{workflow_filename}/dispatches"
    body = {"ref": ref}
    r = requests.post(url, headers=gh_headers(token), json=body, timeout=30)
    r.raise_for_status()
    return r.status_code

def find_latest_workflow_run(owner, repo, workflow_filename, token):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/workflows/{workflow_filename}/runs"
    r = requests.get(url, headers=gh_headers(token), timeout=30)
    r.raise_for_status()
    runs = r.json().get("workflow_runs", [])
    return runs[0] if runs else None

def wait_for_run_completion(owner, repo, workflow_filename, timeout_sec, token):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        run = find_latest_workflow_run(owner, repo, workflow_filename, token)
        if not run:
            print("No workflow run found yet; waiting...")
            time.sleep(5)
            continue
        status = run.get("status")
        conclusion = run.get("conclusion")
        print(f"Run id={run.get('id')} status={status} conclusion={conclusion}")
        if status == "completed":
            return conclusion, run
        time.sleep(5)
    return "timeout", None

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry", action="store_true", help="Dry run; do not call remote APIs")
    p.add_argument("--owner", default=os.getenv("GITHUB_OWNER", "mike21043"))
    p.add_argument("--repo", default=os.getenv("IMAGE_REPO", "transcript-coach-saas"))
    p.add_argument("--branch", default=os.getenv("GITHUB_REF", "local-save-20250920-225817"))
    p.add_argument("--workflow", default="smoke-published.yml")
    p.add_argument("--runner-labels", default=os.getenv("VAST_RUNNER_LABELS", "self-hosted,cuda-test,transcript-coach"))
    p.add_argument("--disk", type=int, default=int(os.getenv("INSTANCE_DISK_GB", "32")))
    args = p.parse_args()

    vast_api_key = os.getenv("VAST_API_KEY")
    template_hash = os.getenv("VAST_TEMPLATE_HASH")
    gh_pat = os.getenv("GITHUB_PAT")

    # If required env vars are missing, attempt to load them from a .env file at repo root
    def load_dotenv(path: Path):
        if not path.exists():
            return {}
        env = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
            if not m:
                continue
            k, v = m.group(1), m.group(2)
            # strip optional surrounding quotes
            if (v.startswith("\"") and v.endswith("\"")) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            env[k] = v
        return env

    repo_root = Path(__file__).resolve().parents[1]
    dotenv = repo_root / '.env'
    if dotenv.exists():
        loaded = load_dotenv(dotenv)
        # Only set env vars that are missing
        if not vast_api_key and 'VAST_API_KEY' in loaded:
            vast_api_key = loaded['VAST_API_KEY']
        if not template_hash and 'VAST_TEMPLATE_HASH' in loaded:
            template_hash = loaded['VAST_TEMPLATE_HASH']
        if not gh_pat and 'GITHUB_PAT' in loaded:
            gh_pat = loaded['GITHUB_PAT']

    if args.dry:
        print("Dry run: will not perform remote operations. Validating local steps...")
    else:
        if not vast_api_key or not template_hash or not gh_pat:
            print("Missing required env vars for non-dry run: VAST_API_KEY, VAST_TEMPLATE_HASH, GITHUB_PAT")
            sys.exit(2)

    # Generate user-data via helper script
    repo_root = Path(__file__).resolve().parents[1]
    helper = repo_root / "scripts" / "provision_runner.sh"
    if not helper.exists():
        print(f"Helper not found: {helper}")
        sys.exit(2)

    tmp_ud = tempfile.NamedTemporaryFile(prefix="runner-ud-", suffix=".sh", delete=False)
    tmp_ud.close()
    tmp_path = tmp_ud.name
    owner = args.owner
    repo = args.repo

    print(f"Generating runner user-data to: {tmp_path}")
    if args.dry:
        # Create a redacted user-data that mirrors what the helper would produce
        cloud_init_path = repo_root / "ops" / "runner-cloud-init.sh"
        if cloud_init_path.exists():
            cloud_text = cloud_init_path.read_text()
        else:
            cloud_text = "# <missing ops/runner-cloud-init.sh>"

        ud_text = (
            "#!/bin/bash\n"
            "export REG_TOKEN=REDACTED\n"
            f"export REPO_URL='https://github.com/{owner}/{repo}'\n"
            f"export RUNNER_LABELS='{args.runner_labels}'\n"
            "# The ops/runner-cloud-init.sh will run and bootstrap the runner\n"
            "bash -lc " + json.dumps(cloud_text)
        )
        with open(tmp_path, 'w') as f:
            f.write(ud_text)
        ud = ud_text.encode('utf-8')
    else:
        subprocess.run(["/bin/bash", str(helper), owner, repo, tmp_path], check=True, cwd=str(repo_root))
        with open(tmp_path, 'rb') as f:
            ud = f.read()

    ud_b64 = base64.b64encode(ud).decode('ascii')
    if len(ud_b64) > 1_000_000:
        print(f"User-data too large to embed ({len(ud_b64)} bytes) — aborting")
        sys.exit(2)

    print(f"User-data size: {len(ud)} bytes (base64 {len(ud_b64)} chars)")

    if args.dry:
        print("Dry run complete. User-data generated and validated.")
        return

    # Search offers
    offers = search_offers(vast_api_key)
    if not offers:
        print("No offers found")
        sys.exit(2)
    best = pick_best(offers)
    ask_id = int(best['id'])
    print(f"Selected ask id: {ask_id}")

    # Create instance
    res = create_instance(vast_api_key, ask_id, template_hash, ud_b64, args.runner_labels, disk_gb=args.disk)
    newc = res.get('new_contract')
    if isinstance(newc, dict):
        inst_id = newc.get('instance_id')
    elif isinstance(newc, int):
        inst_id = newc
    else:
        inst_id = None
    if not inst_id:
        insts = list_instances(vast_api_key)
        inst_id = int(sorted(insts, key=lambda i: i.get('id',0), reverse=True)[0]['id'])
    print(f"Created instance id: {inst_id}")

    # Wait for running
    if not wait_until_running(vast_api_key, inst_id, timeout_sec=900):
        print("Instance failed to become ready")
        sys.exit(2)

    # Wait for GitHub runner to appear
    print("Waiting for GitHub runner registration (label search)...")
    deadline = time.time() + 900
    found_runner = None
    while time.time() < deadline:
        runners = list_repo_runners(owner, repo, gh_pat)
        for r in runners:
            labels = [l['name'] for l in r.get('labels', [])]
            if any(lbl in labels for lbl in args.runner_labels.split(',')) and r.get('status') == 'online':
                found_runner = r
                break
        if found_runner:
            break
        print("Runner not yet registered/online; sleeping 10s...")
        time.sleep(10)

    if not found_runner:
        print("Runner did not register within timeout")
        sys.exit(2)

    print(f"Runner registered: id={found_runner.get('id')} name={found_runner.get('name')}")

    # Dispatch workflow
    print(f"Dispatching workflow {args.workflow} on branch {args.branch}")
    dispatch_workflow(owner, repo, args.workflow, args.branch, gh_pat)

    conclusion, run = wait_for_run_completion(owner, repo, args.workflow, timeout_sec=3600, token=gh_pat)
    print(f"Workflow finished: conclusion={conclusion}")
    if run:
        print(json.dumps(run, indent=2))

if __name__ == '__main__':
    main()
