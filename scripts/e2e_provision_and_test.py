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
    # Allow caller to override GPU count and minimum CUDA via env
    preferred_num_gpus = int(os.getenv('VAST_NUM_GPUS', str(min_gpus)))
    min_cuda = float(os.getenv('VAST_MIN_CUDA', '12.9'))

    # Budget cap (env support for multiple names for compatibility)
    price_env = os.getenv('priceInstanceHourlyMax') or os.getenv('PRICE_INSTANCE_HOURLY_MAX') or os.getenv('VAST_PRICE_INSTANCE_HOURLY_MAX')
    price_max = None
    try:
        price_max = float(price_env) if price_env else None
    except Exception:
        price_max = None

    q = {"rentable": {"eq": True}, "rented": {"eq": False}, "num_gpus": {"gte": preferred_num_gpus}}
    if only_verified:
        q["verified"] = {"eq": True}
    # Allowed countries from env (default to US). Note: machine_countries filter is not reliably
    # accepted by the Vast API; we'll filter client-side below.
    allowed = [c.strip().lower() for c in os.getenv('VAST_ALLOWED_COUNTRIES', 'US').split(',') if c.strip()]
    # Budget cap: ask API for price_hour_usd <= price_max
    # default budget to $0.30/hr if not provided
    if price_max is None:
        try:
            price_max = float(os.getenv('priceInstanceHourlyMax', os.getenv('PRICE_INSTANCE_HOURLY_MAX', '0.30')))
        except Exception:
            price_max = None

    # Some Vast search endpoints reject price_hour_usd in the query; we'll filter client-side below
    res = vast_req("PUT", "/search/asks/", vast_api_key, {"q": q}).get("offers", [])
    # Apply country filters from env if provided
    exclude = [c.strip().lower() for c in os.getenv('VAST_EXCLUDE_COUNTRIES', 'CN,CHN,PRC,CHINA').split(',') if c.strip()]

    def ok(o):
        mc = str(o.get('machine_country') or o.get('country') or '').lower()
        if not mc:
            return True
        for ex in exclude:
            if ex and ex in mc:
                return False
        if allowed:
            return any(a in mc for a in allowed)
        return True

    filtered = [o for o in res if ok(o)]
    # Further filter by min CUDA and exact GPU preference when possible
    def cuda_ok(o):
        try:
            cm = float(o.get('cuda_max_good') or 0.0)
        except Exception:
            cm = 0.0
        g = int(o.get('num_gpus') or 0)
        if cm < min_cuda:
            return False
        # prefer exact GPU count match when VAST_NUM_GPUS is set
        if 'VAST_NUM_GPUS' in os.environ:
            return g == int(os.environ['VAST_NUM_GPUS'])
        return True

    filtered = [o for o in filtered if cuda_ok(o)]
    if not filtered and res:
        print(f"No offers matched allowed countries {allowed}; falling back to all offers excluding {exclude}")
        filtered = [o for o in res if not any(ex in str(o.get('machine_country','')).lower() for ex in exclude)]
        # apply cuda/gpu filter to fallback list as well
        filtered = [o for o in filtered if cuda_ok(o)]
    return filtered

def pick_best(offers):
    if not offers:
        return None
    def extract_price(o):
        # try several common price fields; fall back to large number when missing
        for k in ('price_hour_usd', 'price', 'price_usd'):
            v = o.get(k)
            try:
                if v is not None:
                    return float(v)
            except Exception:
                continue
        # nested shapes
        try:
            s = o.get('search') or {}
            if s and s.get('totalHour') is not None:
                return float(s.get('totalHour'))
        except Exception:
            pass
        try:
            if o.get('dph_total') is not None:
                return float(o.get('dph_total'))
        except Exception:
            pass
        return 1e9

    def extract_perf(o):
        # lower is better for dlperf_usd style fields
        for k in ('dlperf_usd', 'dlperf_usd_per_hour', 'dlperf_usd_per_sec'):
            try:
                v = o.get(k)
                if v is not None:
                    return float(v)
            except Exception:
                continue
        return 1e9

    # Primary sort: absolute price (lower is better). Secondary: dlperf_usd (lower is better).
    return sorted(offers, key=lambda o: (extract_price(o), extract_perf(o)))[0]

def create_instance(vast_api_key, ask_id, template_hash, ud_b64, runner_labels, disk_gb=32, image: str = None):
    body = {
        "disk": disk_gb,
        "label": f"e2e-{int(time.time())}",
        "env": {
            "RUNNER_USER_DATA_B64": ud_b64,
            "RUNNER_LABELS": runner_labels,
        },
        "target_state": "running",
    }
    # Only include template_hash if explicitly provided; some asks reject null template values
    if template_hash is not None:
        body["template_hash"] = template_hash
    if image:
        body["image"] = image
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
    p.add_argument("--ask-id", type=int, help="Use specific ask id instead of auto-selecting")
    p.add_argument("--no-template", action="store_true", help="Omit template_hash when creating the instance")
    p.add_argument("--auto-destroy-on-failure", action="store_true", help="Automatically destroy the instance if runner doesn't register or workflow fails")
    p.add_argument("--owner", default=os.getenv("GITHUB_OWNER", "mike21043"))
    p.add_argument("--repo", default=os.getenv("IMAGE_REPO", "transcript-coach-saas"))
    p.add_argument("--branch", default=os.getenv("GITHUB_REF", "local-save-20250920-225817"))
    p.add_argument("--workflow", default="smoke-published.yml")
    p.add_argument("--runner-labels", default=os.getenv("VAST_RUNNER_LABELS", "self-hosted,cuda-test,transcript-coach"))
    p.add_argument("--disk", type=int, default=int(os.getenv("INSTANCE_DISK_GB", "32")))
    p.add_argument("--image", default=None, help="Optional image to request for the instance (e.g. ubuntu:22.04)")
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

    # Generate user-data via helper or in-Python (preferred)
    repo_root = Path(__file__).resolve().parents[1]
    helper = repo_root / "scripts" / "provision_runner.sh"
    tmp_ud = tempfile.NamedTemporaryFile(prefix="runner-ud-", suffix=".sh", delete=False)
    tmp_ud.close()
    tmp_path = tmp_ud.name
    owner = args.owner
    repo = args.repo

    def get_github_reg_token(owner: str, repo: str, token: str) -> str:
        """Request a short-lived GitHub Actions runner registration token via the API."""
        url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runners/registration-token"
        headers = gh_headers(token)
        r = requests.post(url, headers=headers, timeout=30)
        r.raise_for_status()
        j = r.json()
        t = j.get('token')
        if not t:
            raise RuntimeError(f"Failed to obtain registration token: {j}")
        return t

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
        # Preferred path: generate registration token in-python and inline cloud-init
        if not gh_pat:
            print("GITHUB_PAT required for non-dry runs")
            sys.exit(2)
        # obtain a fresh registration token
        reg_token = get_github_reg_token(owner, repo, gh_pat)

        # load cloud-init script and escape it safely for embedding
        cloud_init_path = repo_root / "ops" / "runner-cloud-init.sh"
        if cloud_init_path.exists():
            cloud_text = cloud_init_path.read_text()
        else:
            cloud_text = "# <missing ops/runner-cloud-init.sh>"

        runner_labels = args.runner_labels
        repo_url = f"https://github.com/{owner}/{repo}"

        # Render a small wrapper script that exports the token and then runs the cloud-init content
        # We avoid shell heredocs that require complex escaping by writing a minimal launcher
        ud_lines = ["#!/bin/bash"]
        ud_lines.append(f"export REG_TOKEN='{reg_token}'")
        ud_lines.append(f"export REPO_URL='{repo_url}'")
        ud_lines.append(f"export RUNNER_LABELS='{runner_labels}'")
        # ensure SUDO_USER is defined to avoid 'set -u' failures in cloud-init
        ud_lines.append("export SUDO_USER='ubuntu'")
        ud_lines.append("set -euo pipefail")
        ud_lines.append("# Begin inlined ops/runner-cloud-init.sh")
        ud_lines.extend(cloud_text.splitlines())
        ud_text = "\n".join(ud_lines) + "\n"
        with open(tmp_path, 'w') as f:
            f.write(ud_text)
        os.chmod(tmp_path, 0o755)
        ud = ud_text.encode('utf-8')

    ud_b64 = base64.b64encode(ud).decode('ascii')
    if len(ud_b64) > 1_000_000:
        print(f"User-data too large to embed ({len(ud_b64)} bytes) — aborting")
        sys.exit(2)

    print(f"User-data size: {len(ud)} bytes (base64 {len(ud_b64)} chars)")

    if args.dry:
        print("Dry run complete. User-data generated and validated.")
        return

    # Determine image to request (ghcr default) and then search offers
    owner = owner or os.getenv('GITHUB_OWNER') or os.getenv('IMAGE_REPO_OWNER') or os.getenv('IMAGE_REPO') or 'mike21043'
    repo_name = args.repo or os.getenv('IMAGE_REPO') or 'transcript-coach-saas'
    default_image = f"ghcr.io/{owner}/{repo_name}:cuda129"
    if args.image:
        default_image = args.image

    # Search offers
    offers = search_offers(vast_api_key)
    if not offers:
        print("No offers found")
        sys.exit(2)
    # Client-side enforcement: prefer US machines and enforce budget cap
    allowed = [c.strip().lower() for c in os.getenv('VAST_ALLOWED_COUNTRIES', 'US').split(',') if c.strip()]
    price_cap = float(os.getenv('priceInstanceHourlyMax') or os.getenv('PRICE_INSTANCE_HOURLY_MAX') or '0.30')

    def country_ok(o):
        mc = str(o.get('machine_country') or o.get('country') or o.get('geolocation') or '').lower()
        if not mc:
            return False
        if allowed and not any(a in mc for a in allowed):
            return False
        return True

    def price_ok(o):
        try:
            p = float(o.get('price_hour_usd') or o.get('price') or o.get('price_usd') or 0)
        except Exception:
            p = 0
        return p <= price_cap

    filtered_offers = [o for o in offers if country_ok(o) and price_ok(o)]

    # Progressive fallback policy: if nothing matches strict filters, gradually widen the price cap
    # and then allowed countries in a controlled manner. Configurable via env vars.
    PRICE_STEP = float(os.getenv('PRICE_STEP', '0.10'))
    PRICE_MAX_FALLBACK = float(os.getenv('PRICE_MAX_FALLBACK', str(price_cap + 0.40)))
    # comma-separated additional countries to consider when widening (e.g. 'CA,US')
    FALLBACK_COUNTRIES = [c.strip().lower() for c in os.getenv('VAST_ALLOWED_COUNTRIES_FALLBACK', 'CA').split(',') if c.strip()]

    def try_widen_offers(offers):
        # try stepping price upward
        current = price_cap
        while current < PRICE_MAX_FALLBACK:
            current = round(current + PRICE_STEP, 2)
            def price_ok_dynamic(o):
                try:
                    p = float(o.get('price_hour_usd') or o.get('price') or o.get('price_usd') or 0)
                except Exception:
                    p = 1e9
                return p <= current
            cand = [o for o in offers if country_ok(o) and price_ok_dynamic(o)]
            if cand:
                print(f'Found {len(cand)} offers by increasing price cap to ${current}/hr')
                return cand
        # try expanding allowed countries slightly (append fallback list)
        if FALLBACK_COUNTRIES:
            expanded_allowed = allowed + FALLBACK_COUNTRIES
            def country_ok_wide(o):
                mc = str(o.get('machine_country') or o.get('country') or o.get('geolocation') or '').lower()
                if not mc:
                    return False
                for ex in exclude:
                    if ex and ex in mc:
                        return False
                return any(a in mc for a in expanded_allowed)
            cand = [o for o in offers if country_ok_wide(o) and price_ok(o)]
            if cand:
                print(f'Found {len(cand)} offers by expanding allowed countries to: {expanded_allowed}')
                return cand
        return []

    if not filtered_offers:
        print(f"No offers in allowed countries {allowed} under price ${price_cap}/hr; attempting progressive fallback")
        filtered_offers = try_widen_offers(offers)
    if not filtered_offers:
        print(f"Progressive fallback did not find acceptable offers; aborting to avoid unexpected costs")
        sys.exit(2)
    # prefer offers with exact GPU count match as a secondary key
    desired_gpus = int(os.getenv('VAST_NUM_GPUS', '1'))
    def score_with_gpu(o):
        # reuse pick_best helpers: price then perf
        def extract_price(o):
            for k in ('price_hour_usd', 'price', 'price_usd'):
                v = o.get(k)
                try:
                    if v is not None:
                        return float(v)
                except Exception:
                    continue
            try:
                s = o.get('search') or {}
                if s and s.get('totalHour') is not None:
                    return float(s.get('totalHour'))
            except Exception:
                pass
            try:
                if o.get('dph_total') is not None:
                    return float(o.get('dph_total'))
            except Exception:
                pass
            return 1e9

        def extract_perf(o):
            for k in ('dlperf_usd', 'dlperf_usd_per_hour', 'dlperf_usd_per_sec'):
                try:
                    v = o.get(k)
                    if v is not None:
                        return float(v)
                except Exception:
                    continue
            return 1e9

        p = extract_price(o)
        perf = extract_perf(o)
        try:
            g = int(o.get('num_gpus') or 0)
        except Exception:
            g = 0
        gpu_mismatch = 0 if g == desired_gpus else 1
        return (p, gpu_mismatch, perf)

    best = sorted(filtered_offers, key=score_with_gpu)[0]
    ask_id = int(best['id'])
    print(f"Selected ask id: {ask_id}")

    # Create instance
    create_kwargs = {}
    if args.no_template:
        # create_instance expects template_hash arg; pass None by omitting template in body
        create_kwargs['template_hash'] = None
    else:
        create_kwargs['template_hash'] = template_hash

    # If the user specified an ask id, use that
    if args.ask_id:
        ask_id = int(args.ask_id)

    res = None
    try:
        if create_kwargs.get('template_hash') is None:
            # call create_instance but ensure template is omitted by passing template_hash=None
            res = create_instance(vast_api_key, ask_id, None, ud_b64, args.runner_labels, disk_gb=args.disk, image=default_image)
        else:
            res = create_instance(vast_api_key, ask_id, create_kwargs['template_hash'], ud_b64, args.runner_labels, disk_gb=args.disk, image=default_image)
    except Exception as e:
        print('Error creating instance:', e)
        raise
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
        if args.auto_destroy_on_failure:
            print('Auto-destroy enabled; destroying instance', inst_id)
            try:
                vast_req('DELETE', f'/instances/{inst_id}/', vast_api_key, None)
            except Exception as e:
                print('Failed to destroy instance:', e)
        sys.exit(2)

    print(f"Runner registered: id={found_runner.get('id')} name={found_runner.get('name')}")

    # Dispatch workflow
    print(f"Dispatching workflow {args.workflow} on branch {args.branch}")
    dispatch_workflow(owner, repo, args.workflow, args.branch, gh_pat)

    conclusion, run = wait_for_run_completion(owner, repo, args.workflow, timeout_sec=3600, token=gh_pat)
    print(f"Workflow finished: conclusion={conclusion}")
    if conclusion != 'success' and args.auto_destroy_on_failure:
        print('Workflow failed or timed out; auto-destroying instance', inst_id)
        try:
            vast_req('DELETE', f'/instances/{inst_id}/', vast_api_key, None)
        except Exception as e:
            print('Failed to destroy instance:', e)
    if run:
        print(json.dumps(run, indent=2))

if __name__ == '__main__':
    main()
