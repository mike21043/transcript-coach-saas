#!/usr/bin/env python3
"""Local end-to-end smoke test (no ML).

This script simulates an upload from the UI by copying an audio file into
"/data/uploads", creates a job object like the API would enqueue, and then
runs a minimal "agent smoke" processor that writes a transcript JSON to
"/data/results" and stores the result in a fake Redis object.

Use this to validate that the upload directory is writable by the agent and
that the agent can locate input files at /data/uploads. It avoids importing
heavy ML dependencies.
"""

import os
import shutil
import uuid
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_TEST_WAV = Path("/root/test.wav")
UPLOADS_DIR = Path("/data/uploads")
RESULTS_DIR = Path("/data/results")

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

if not SRC_TEST_WAV.exists():
    print(f"ERROR: repo test audio not found at {SRC_TEST_WAV}. Please add a test.wav to the repository root to continue.")
    raise SystemExit(1)

# Copy test audio into /data/uploads
job_id = uuid.uuid4().hex
filename = f"{job_id}_test.wav"
dst_path = UPLOADS_DIR / filename
shutil.copy2(SRC_TEST_WAV, dst_path)
print(f"Copied {SRC_TEST_WAV} -> {dst_path}")

# Simulate API job payload
job = {"job_id": job_id, "filename": filename}
print(f"Simulated job: {job}")

# Fake redis implementation (only set/get used by agent)
class FakeRedis:
    def __init__(self):
        self._store = {}
    def set(self, k, v):
        self._store[k] = v
    def get(self, k):
        return self._store.get(k)
    def dump(self):
        return dict(self._store)

r = FakeRedis()

# Minimal smoke processor: validate file exists, write small transcript JSON
input_path = dst_path
if not input_path.exists():
    err = f"Input file not found: {input_path}"
    print(err)
    r.set(f"result:{job_id}", json.dumps({"job_id": job_id, "status": "error", "error": err}))
    raise SystemExit(1)

transcript = {
    "job_id": job_id,
    "filename": filename,
    "segments": [{"text": "(smoke test)", "start": 0.0, "end": 1.0, "speaker": "unknown"}],
    "embeddings": {},
    "status": "completed",
}

outpath = RESULTS_DIR / f"{job_id}.json"
with open(outpath, "w", encoding="utf-8") as f:
    json.dump(transcript, f, ensure_ascii=False, indent=2)

r.set(f"result:{job_id}", json.dumps(transcript))

print(f"Wrote smoke transcript to {outpath}")
print("FakeRedis contents:")
print(json.dumps(r.dump(), indent=2))

print("SUCCESS: local e2e smoke complete.")
