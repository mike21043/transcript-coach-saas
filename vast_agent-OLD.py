#!/usr/bin/env python3
import os, time, requests, redis

# ======================
# Config
# ======================
REDIS_URL = "redis://localhost:6379/0"
QUEUE_NAME = "transcript_jobs"

VAST_API_KEY = os.getenv("VAST_API_KEY")
VAST_BASE = "https://api.vast.ai/v0"
GPU_TEMPLATE_ID = os.getenv("VAST_TEMPLATE_ID")

# Track current GPU instance
GPU_INSTANCE_ID = None
IDLE_TICKS = 0
MAX_IDLE_TICKS = 2   # stop GPU if queue empty for 10 loops (10*30s = 5 min)

# ======================
# Redis helper
# ======================
def queue_len():
    r = redis.Redis.from_url(REDIS_URL)
    return r.llen(QUEUE_NAME)

# ======================
# Vast.ai helpers
# ======================
def start_gpu():
    global GPU_INSTANCE_ID
    resp = requests.post(f"{VAST_BASE}/launch/", headers={"Authorization": f"Bearer {VAST_API_KEY}"},
                         json={"template_id": GPU_TEMPLATE_ID})
    resp.raise_for_status()
    GPU_INSTANCE_ID = resp.json()["id"]
    print(f"[vast-agent] Launched GPU instance {GPU_INSTANCE_ID}")

def stop_gpu():
    global GPU_INSTANCE_ID
    if not GPU_INSTANCE_ID:
        return
    resp = requests.put(f"{VAST_BASE}/instances/{GPU_INSTANCE_ID}/stop/",
                        headers={"Authorization": f"Bearer {VAST_API_KEY}"})
    resp.raise_for_status()
    print(f"[vast-agent] Stopped GPU instance {GPU_INSTANCE_ID}")
    GPU_INSTANCE_ID = None

# ======================
# Main loop
# ======================
if __name__ == "__main__":
    print("[vast-agent] Starting Vast.ai controller loop...")
    while True:
        try:
            qlen = queue_len()
            print(f"[vast-agent] Queue length: {qlen}")

            if qlen > 0:
                IDLE_TICKS = 0
                if not GPU_INSTANCE_ID:
                    start_gpu()
            else:
                if GPU_INSTANCE_ID:
                    IDLE_TICKS += 1
                    print(f"[vast-agent] Idle tick {IDLE_TICKS}/{MAX_IDLE_TICKS}")
                    if IDLE_TICKS >= MAX_IDLE_TICKS:
                        stop_gpu()
                        IDLE_TICKS = 0

        except Exception as e:
            print(f"[vast-agent] ERROR: {e}")

        time.sleep(30)
