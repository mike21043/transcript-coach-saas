#!/usr/bin/env python3
"""
Usage: python3 enqueue_to_master.py <filename>
This script enqueues a job to the Redis master at 109.244.159.27:29846
"""
import sys, json, time
try:
    import redis
except Exception as e:
    print("Missing redis module. Install with: pip install redis")
    raise

if len(sys.argv) < 2:
    print("Usage: python3 enqueue_to_master.py <filename>")
    sys.exit(1)

filename = sys.argv[1]
url = "redis://109.244.159.27:29846/0"
try:
    r = redis.Redis.from_url(url, decode_responses=True)
    print("PING ->", r.ping())
    job = {"job_id":"smoke-"+str(int(time.time())), "filename": filename}
    r.lpush("transcript_jobs", json.dumps(job))
    print("Enqueued:", job)
except Exception as e:
    print("Failed to enqueue:", e)
    sys.exit(2)
