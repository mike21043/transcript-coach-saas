#!/usr/bin/env python3
"""
Poll GitHub for a self-hosted runner with specific labels and dispatch the smoke-published workflow.
Saves artifacts under /tmp:
 - /tmp/runner-<id>.json
 - /tmp/workflow-dispatch-response.json
 - /tmp/workflow-run-<id>.json

Usage: python3 scripts/poll_runner_and_dispatch.py
"""
import os
import time
import json
from pathlib import Path
import requests

REPO_OWNER = os.getenv('GITHUB_OWNER') or 'mike21043'
REPO_NAME = os.getenv('GITHUB_REPO') or 'transcript-coach-saas'
BRANCH = os.getenv('GITHUB_REF') or 'local-save-20250920-225817'
WORKFLOW_FILENAME = 'smoke-published.yml'
POLL_TIMEOUT = int(os.getenv('POLL_TIMEOUT_SECONDS') or 10 * 60)
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL') or 15)

def load_env_dotenv(repo_root=None):
    env = {}
    repo_root = Path(repo_root or Path(__file__).resolve().parents[1])
    dot = repo_root / '.env'
    if dot.exists():
        for l in dot.read_text().splitlines():
            if '=' in l and not l.strip().startswith('#'):
                k,v = l.split('=',1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def main():
    env = load_env_dotenv()
    token = os.getenv('GITHUB_PAT') or env.get('GITHUB_PAT')
    if not token:
        print('GITHUB_PAT missing in env or .env; cannot proceed')
        raise SystemExit(2)

    labels_raw = os.getenv('VAST_RUNNER_LABELS') or env.get('VAST_RUNNER_LABELS') or 'self-hosted,cuda-test,transcript-coach'
    desired_labels = [l.strip() for l in labels_raw.split(',') if l.strip()]

    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json'
    }

    base = f'https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}'

    deadline = time.time() + POLL_TIMEOUT
    found_runner = None
    print(f'Looking for runner with labels {desired_labels} in {REPO_OWNER}/{REPO_NAME} (timeout {POLL_TIMEOUT}s)')
    while time.time() < deadline:
        r = requests.get(f'{base}/actions/runners', headers=headers, timeout=30)
        if r.status_code != 200:
            print('Failed listing runners', r.status_code, r.text[:500])
            time.sleep(POLL_INTERVAL)
            continue
        j = r.json()
        runners = j.get('runners', [])
        print(time.strftime('%H:%M:%S'), 'found', len(runners), 'runners')
        for runner in runners:
            # runner labels come as objects under 'labels'
            runner_labels = [lb.get('name') for lb in runner.get('labels', []) if lb.get('name')]
            # check desired subset
            if all(d in runner_labels for d in desired_labels):
                status = runner.get('status')
                print('Candidate runner', runner.get('id'), runner.get('name'), 'status', status, 'labels', runner_labels)
                if str(status).lower() == 'online':
                    found_runner = runner
                    break
        if found_runner:
            break
        time.sleep(POLL_INTERVAL)

    if not found_runner:
        print('No matching online runner found within timeout')
        raise SystemExit(3)

    rid = found_runner['id']
    outp = Path(f'/tmp/runner-{rid}.json')
    outp.write_text(json.dumps(found_runner, indent=2))
    print('Saved runner snapshot to', outp)

    # Dispatch workflow
    dispatch_url = f'{base}/actions/workflows/{WORKFLOW_FILENAME}/dispatches'
    payload = {'ref': BRANCH}
    print('Dispatching workflow', WORKFLOW_FILENAME, 'on ref', BRANCH)
    r2 = requests.post(dispatch_url, headers=headers, json=payload, timeout=30)
    Path('/tmp/workflow-dispatch-response.json').write_text(json.dumps({'status_code': r2.status_code, 'text': r2.text}, indent=2))
    print('Dispatch response', r2.status_code)
    if r2.status_code not in (204, 201):
        print('Dispatch may have failed; saved response to /tmp/workflow-dispatch-response.json')

    # Try to find the workflow run that was created
    wf_runs_url = f'{base}/actions/workflows/{WORKFLOW_FILENAME}/runs'
    print('Polling for new workflow run (timeout 10 minutes)')
    run_deadline = time.time() + 10*60
    found_run = None
    while time.time() < run_deadline:
        rr = requests.get(wf_runs_url, headers=headers, timeout=30)
        if rr.status_code != 200:
            print('Failed listing workflow runs', rr.status_code)
            time.sleep(8)
            continue
        runs = rr.json().get('workflow_runs', [])
        # prefer most recent run on our branch
        for run in runs:
            if run.get('head_branch') == BRANCH:
                print('Found run', run.get('id'), 'status', run.get('status'), 'conclusion', run.get('conclusion'))
                found_run = run
                break
        if found_run:
            break
        time.sleep(8)

    if not found_run:
        print('No workflow run found for', WORKFLOW_FILENAME, 'on branch', BRANCH)
        raise SystemExit(4)

    run_id = found_run['id']
    run_path = Path(f'/tmp/workflow-run-{run_id}.json')
    run_path.write_text(json.dumps(found_run, indent=2))
    print('Saved workflow run to', run_path)

    # Save job details
    jobs_url = f'{base}/actions/runs/{run_id}/jobs'
    jr = requests.get(jobs_url, headers=headers, timeout=30)
    if jr.status_code == 200:
        Path(f'/tmp/workflow-run-{run_id}-jobs.json').write_text(json.dumps(jr.json(), indent=2))
        print('Saved jobs to /tmp/workflow-run-{}-jobs.json'.format(run_id))
    else:
        print('Failed to fetch jobs for run', run_id, jr.status_code)

    print('Done. Runner id', rid, 'workflow run id', run_id)

if __name__ == '__main__':
    main()
