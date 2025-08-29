import os, time, json, requests, redis, subprocess, pathlib, shlex

# ---- ENV ----
VAST_API_KEY = os.getenv("VAST_API_KEY", "")
WORKER_IMAGE = os.getenv("WORKER_IMAGE", "")
WORKER_CMD   = os.getenv("WORKER_CMD", "python worker.py --input /data/{basename} --out_dir /data/{tid}")
MAX_PRICE    = float(os.getenv("MAX_PRICE", "0.45"))
GPU_NAME     = os.getenv("GPU_NAME", "T4")
REDIS_URL    = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATA_DIR     = os.getenv("DATA_DIR", "/data")

# Local paths on your VPS
UPLOAD_DIR    = f"{DATA_DIR}/uploads"
TRANSCRIPTS_DIR = f"{DATA_DIR}/transcripts"
JOBS_DIR      = f"{DATA_DIR}/jobs"

pathlib.Path(TRANSCRIPTS_DIR).mkdir(parents=True, exist_ok=True)
pathlib.Path(JOBS_DIR).mkdir(parents=True, exist_ok=True)

# Vast API
BASE_URL = "https://vast.ai/api/v0"
R = redis.Redis.from_url(REDIS_URL)

def _request(method, endpoint, **kwargs):
    url = f"{BASE_URL}{endpoint}"
    params = kwargs.pop("params", {})
    params["api_key"] = VAST_API_KEY
    return requests.request(method, url, params=params, **kwargs)

def find_offer():
    # NOTE: filter for verified, direct, specific GPU, under price cap
    q = f"verified=1,external=false,direct=true,gpu_name={GPU_NAME}"
    resp = _request("GET", "/bundles", params={"q": q})
    resp.raise_for_status()
    offers = resp.json().get("offers", [])
    offers = [o for o in offers if o.get("dph_base", 999) <= MAX_PRICE]
    if not offers:
        raise RuntimeError("No suitable GPU offers found.")
    # pick cheapest by $/hr
    best = min(offers, key=lambda o: o["dph_base"])
    return best

def launch_instance(worker_cmd):
    best = find_offer()
    bid_id = best["id"]
    payload = {
        "bid": bid_id,
        "image": WORKER_IMAGE,
        "onstart_cmd": worker_cmd,
        "disk": 20,  # GB
        "label": "transcript-worker",
    }
    r = _request("POST", "/launch", json=payload)
    r.raise_for_status()
    data = r.json()
    if "new_contract" not in data:
        raise RuntimeError(f"Unexpected launch response: {data}")
    instance_id = data["new_contract"]["id"]
    return instance_id

def get_instance(instance_id):
    r = _request("GET", f"/instances/{instance_id}")
    r.raise_for_status()
    return r.json()["instance"]

def wait_ready(instance_id, timeout=600):
    print(f"[Agent] Waiting for instance {instance_id} to be ready...")
    start = time.time()
    while time.time() - start < timeout:
        inst = get_instance(instance_id)
        state = inst.get("status_msg","")
        ssh_host = inst.get("ssh_host")
        ssh_port = inst.get("ssh_port")
        if ssh_host and ssh_port and "running" in state.lower():
            print(f"[Agent] Instance ready: {ssh_host}:{ssh_port}")
            return ssh_host, ssh_port
        time.sleep(5)
    raise TimeoutError("Instance failed to become ready in time.")

def scp_to(ssh_host, ssh_port, local_path, remote_path="/root/in.wav"):
    cmd = f"scp -P {ssh_port} -o StrictHostKeyChecking=no {shlex.quote(local_path)} root@{ssh_host}:{shlex.quote(remote_path)}"
    print("[Agent] SCP->", cmd)
    subprocess.check_call(cmd, shell=True)

def ssh_run(ssh_host, ssh_port, remote_cmd):
    cmd = f"ssh -p {ssh_port} -o StrictHostKeyChecking=no root@{ssh_host} {shlex.quote(remote_cmd)}"
    print("[Agent] SSH$", remote_cmd)
    subprocess.check_call(cmd, shell=True)

def scp_from(ssh_host, ssh_port, remote_path, local_dir):
    pathlib.Path(local_dir).mkdir(parents=True, exist_ok=True)
    cmd = f"scp -P {ssh_port} -o StrictHostKeyChecking=no root@{ssh_host}:{shlex.quote(remote_path)} {shlex.quote(local_dir)}/"
    print("[Agent] SCP<-", cmd)
    subprocess.check_call(cmd, shell=True)

def teardown(instance_id):
    try:
        _request("POST", f"/instances/{instance_id}/kill")
    finally:
        _request("DELETE", f"/instances/{instance_id}")

def process_job(job):
    """
    job = {"tid": "...", "filename": "...", "path": "/data/uploads/<tid>_<name>"}
    """
    tid = job["tid"]
    src_audio = job["path"]
    basename = os.path.basename(src_audio)

    print(f"[Agent] Processing job {tid} ({basename})")

    # 1) Launch instance with a 'no-op' start (we'll run docker manually via SSH after file upload)
    instance_id = launch_instance("sleep infinity")
    try:
        ssh_host, ssh_port = wait_ready(instance_id)

        # 2) Copy audio to instance
        scp_to(ssh_host, ssh_port, src_audio, "/root/in.wav")

        # 3) Run your worker container on the instance
        #    Mount /root to /io so outputs land in /root/out
        remote = (
            f"docker run --rm --gpus all "
            f"-v /root:/io {WORKER_IMAGE} "
            f"{WORKER_CMD.replace('{basename}', 'in.wav').replace('{tid}', tid).replace('/data','/io')}"
        )
        # Ensure output dir exists
        ssh_run(ssh_host, ssh_port, "mkdir -p /root/out")
        ssh_run(ssh_host, ssh_port, remote)

        # 4) Copy results back to VPS
        out_local_dir = f"{TRANSCRIPTS_DIR}/{tid}"
        scp_from(ssh_host, ssh_port, "/root/out/*", out_local_dir)

        # Optional: write a convenience .txt next to others for UI listing
        # If the worker writes transcript.txt, symlink/copy to /data/transcripts/<tid>.txt
        src_txt = f"{out_local_dir}/transcript.txt"
        dst_txt = f"{TRANSCRIPTS_DIR}/{tid}.txt"
        if os.path.exists(src_txt):
            with open(src_txt, "rb") as rfp, open(dst_txt, "wb") as wfp:
                wfp.write(rfp.read())

        print(f"[Agent] Job {tid} complete.")
    except Exception as e:
        print(f"[Agent] ERROR processing {tid}:", e)
        raise
    finally:
        try:
            teardown(instance_id)
        except Exception as te:
            print("[Agent] Teardown error:", te)

def main():
    print("[Agent] Vast.ai agent online. Polling Redis for jobs...")
    # blocking pop with 15s timeout to avoid busy loop
    while True:
        item = R.brpop("jobs", timeout=15)
        if not item:
            continue
        _, payload = item
        try:
            job = json.loads(payload.decode("utf-8"))
        except Exception:
            print("[Agent] Skipping malformed job:", payload)
            continue

        try:
            process_job(job)
        except Exception as e:
            print("[Agent] Job failed:", e)
            # (Optional) push to a dead-letter queue:
            # R.lpush("jobs_failed", json.dumps(job))

if __name__ == "__main__":
    if not VAST_API_KEY or not WORKER_IMAGE:
        print("[Agent] Missing VAST_API_KEY or WORKER_IMAGE in environment.")
        time.sleep(60)
        raise SystemExit(1)
    main()
