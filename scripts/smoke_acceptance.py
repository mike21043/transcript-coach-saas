#!/usr/bin/env python3
"""Minimal smoke acceptance test for the transcript-coach API and queue contract.

This script does two checks:
- GET /health returns status: ok
- POST /upload with a tiny synthetic WAV (or existing test.wav) enqueues a job and returns a job_id

It's designed to be safely runnable in CI (no Vast.io or GPU provisioning).
"""
import os
import sys
import json
import requests
from pathlib import Path

API_URL = os.getenv('API_URL', 'http://127.0.0.1:8000')
HEALTH = f"{API_URL}/health"
UPLOAD = f"{API_URL}/upload"


def check_health():
    try:
        r = requests.get(HEALTH, timeout=5)
        r.raise_for_status()
        j = r.json()
        if j.get('status') == 'ok':
            print('health: ok')
            return True
        print('health: unexpected response', j)
    except Exception as e:
        print('health check failed:', e)
    return False


def find_audio():
    # Prefer a repo test.wav if present
    repo_file = Path('test.wav')
    if repo_file.exists():
        return repo_file
    # Else create a tiny silent WAV (1s) using wave module
    tmp = Path('tmp_smoke.wav')
    try:
        import wave, struct
        with wave.open(str(tmp), 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            frames = b''.join([struct.pack('<h', 0) for _ in range(16000)])
            wf.writeframes(frames)
        return tmp
    except Exception as e:
        print('Failed to create synthetic wav:', e)
        return None


def upload_audio(path: Path):
    try:
        with open(path, 'rb') as f:
            files = {'file': (path.name, f, 'audio/wav')}
            r = requests.post(UPLOAD, files=files, timeout=10)
            r.raise_for_status()
            j = r.json()
            print('upload response:', j)
            return j
    except Exception as e:
        print('upload failed:', e)
        return None


if __name__ == '__main__':
    ok = check_health()
    if not ok:
        print('API health failed — aborting smoke test')
        sys.exit(2)

    p = find_audio()
    if not p:
        print('No audio available for smoke upload')
        sys.exit(3)

    resp = upload_audio(p)
    if not resp:
        sys.exit(4)

    if resp.get('job_id'):
        print('Smoke acceptance: upload/enqueue OK (job_id=', resp.get('job_id'), ')')
        sys.exit(0)
    else:
        print('Smoke acceptance: unexpected upload response')
        sys.exit(5)
