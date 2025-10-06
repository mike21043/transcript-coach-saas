#!/usr/bin/env python3
"""Simple smoke worker: consumes `transcript_jobs` from Redis and writes a
minimal result JSON to /data/results and sets Redis key `result:<job_id>`.

This avoids importing heavy ML libs and is intended for local e2e testing.
"""
import os
import time
import json
import redis
from pathlib import Path

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
QUEUE_NAME = os.getenv("QUEUE_NAME", "transcript_jobs")
DATA_RESULTS = Path(os.getenv("DATA_RESULTS", "/data/results"))
DATA_UPLOADS = Path(os.getenv("DATA_UPLOADS", "/data/uploads"))

DATA_RESULTS.mkdir(parents=True, exist_ok=True)

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
print(f"[smoke_agent] Connected to Redis at {REDIS_URL}; listening on {QUEUE_NAME}")

while True:
    try:
        item = r.blpop(QUEUE_NAME, timeout=5)
        if not item:
            continue
        _, job_json = item
        job = json.loads(job_json)
        job_id = job.get("job_id")
        filename = job.get("filename")
        input_path = DATA_UPLOADS / filename
        print(f"[smoke_agent] Got job {job_id} -> {filename}")

        if not input_path.exists():
            err = f"Input file not found: {input_path}"
            print(f"[smoke_agent] ERROR: {err}")
            r.set(f"result:{job_id}", json.dumps({"job_id": job_id, "status": "error", "error": err}))
            continue

        # Minimal transcript result
        transcript = {
            "job_id": job_id,
            "filename": filename,
            "segments": [{"text": "(smoke test)", "start": 0.0, "end": 1.0, "speaker": "unknown"}],
            "embeddings": {},
            "status": "completed",
        }

        outpath = DATA_RESULTS / f"{job_id}.json"
        with open(outpath, "w", encoding="utf-8") as f:
            json.dump(transcript, f)
        r.set(f"result:{job_id}", json.dumps(transcript))
        print(f"[smoke_agent] Wrote {outpath} and set result:{job_id}")

    except Exception as e:
        print(f"[smoke_agent] ERROR: {e}")
        time.sleep(1)
