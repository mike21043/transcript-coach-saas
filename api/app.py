from fastapi import FastAPI, UploadFile, File, Body, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
import os, json, shutil, uuid, redis, subprocess, time, requests
from typing import Optional

app = FastAPI()
DATA_DIR = os.getenv("DATA_DIR", "/data")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
# Allow overriding the queue name via environment for smoke tests
QUEUE_NAME = os.getenv("QUEUE_NAME", "transcript_jobs")

# CORS: allow the UI origin (use UI_ORIGIN env var to override, or allow all in dev)
UI_ORIGIN = os.getenv("UI_ORIGIN", "http://38.242.200.197:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[UI_ORIGIN] if UI_ORIGIN != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Control guard: only allow starting processes via the API when this env is set.
ADMIN_ALLOW_PROC_CONTROL = os.getenv("ADMIN_ALLOW_PROC_CONTROL", "false").lower() in ("1", "true", "yes")


# Docker helper (optional): attempt to use the docker SDK to check/start containers
_DOCKER_CLIENT = None


def _get_docker_client() -> Optional[object]:
    global _DOCKER_CLIENT
    if _DOCKER_CLIENT is not None:
        return _DOCKER_CLIENT
    try:
        import docker
        _DOCKER_CLIENT = docker.from_env()
        return _DOCKER_CLIENT
    except Exception:
        _DOCKER_CLIENT = None
        return None


def _container_running(name: str) -> bool:
    client = _get_docker_client()
    if not client:
        return False
    try:
        c = client.containers.get(name)
        return getattr(c, "status", "") == "running"
    except Exception:
        return False


def _start_container(name: str) -> tuple[bool, str]:
    """Attempt to start a container by name using Docker SDK. Returns (ok, detail)."""
    client = _get_docker_client()
    if not client:
        return False, "docker-client-unavailable"
    try:
        c = client.containers.get(name)
        if getattr(c, "status", "") == "running":
            return True, "already-running"
        c.start()
        try:
            c.reload()
        except Exception:
            pass
        return (getattr(c, "status", "") == "running"), f"status={getattr(c, 'status', '')}"
    except Exception as e:
        return False, str(e)

# Define the canonical processes we consider required. Each entry contains
# an id, human name, check type and args, and a suggested start command.
REQUIRED_PROCESSES = [
    {
        "id": "api",
        "name": "API server",
        "container": "transcript-coach-saas-api",
        "check": {"type": "http", "url": "http://transcript-coach-saas-api:8000/health"},
        "start_cmd": "# managed by docker",
    },
    {"id": "redis", "name": "Redis", "check": {"type": "redis_ping"}, "start_cmd": "# managed by docker"},
    {
        "id": "worker",
        "name": "Worker agent",
        "check": {"type": "pgrep", "pattern": "agent/agent.py"},
        "start_cmd": "nohup python3 agent/agent.py > debug/process_worker.log 2>&1 &",
    },
    {
        "id": "vast_controller",
        "name": "Vast controller",
        "container": "transcript-coach-saas-vast_agent",
        "check": {"type": "pgrep", "pattern": "vast_agent.vast_agent"},
        "start_cmd": "# managed by docker",
    },
    {
        "id": "pcloud",
        "name": "pCloud rclone mount",
        "check": {"type": "mount", "path": "/root/transcript-coach-saas/data/shared"},
        "start_cmd": "# start via docker-compose: docker-compose up -d pcloud",
    },
    {
        "id": "ui",
        "name": "UI dev server",
        "container": "transcript-coach-saas-ui",
        "check": {"type": "http", "url": "http://transcript-coach-saas-ui:3000/"},
        "start_cmd": "# managed by docker",
    },
]


def _ensure_debug_dir():
    d = os.path.join(os.getcwd(), "debug")
    os.makedirs(d, exist_ok=True)
    return d


def check_process(proc: dict) -> dict:
    """Return status dict for a process definition."""
    chk = proc.get("check", {})
    typ = chk.get("type")
    status = False
    detail = ""
    try:
        if typ == "http":
            url = chk.get("url")
            resp = requests.get(url, timeout=2)
            status = resp.status_code == 200
            detail = f"HTTP {resp.status_code}"
        elif typ == "redis_ping":
            status = r.ping()
            detail = "PONG" if status else "no-pong"
        elif typ == "pgrep":
            pat = chk.get("pattern")
            # prefer docker container status if container name provided
            container_name = proc.get("container") or chk.get("container")
            client = _get_docker_client()
            if container_name and client:
                status = _container_running(container_name)
                detail = f"container={container_name},running={status}"
            elif client:
                try:
                    found = False
                    for c in client.containers.list(all=True):
                        if pat in c.name or pat in " ".join(c.attrs.get("Config", {}).get("Cmd") or []):
                            if getattr(c, "status", "") == "running":
                                found = True
                                break
                    status = bool(found)
                    detail = f"docker-scan,matched={found}"
                except Exception:
                    res = subprocess.run(["pgrep", "-f", pat], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    status = res.returncode == 0
                    detail = res.stdout.decode().strip()[:200]
            else:
                res = subprocess.run(["pgrep", "-f", pat], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                status = res.returncode == 0
                detail = res.stdout.decode().strip()[:200]
        elif typ == "mount":
            path = chk.get("path")
            status = os.path.ismount(path) or os.path.exists(path)
            detail = f"exists={os.path.exists(chk.get('path'))}"
        else:
            detail = "unknown-check"
    except Exception as e:
        detail = str(e)

    return {"id": proc["id"], "name": proc["name"], "ok": bool(status), "detail": detail}


def start_process(proc: dict) -> dict:
    """Attempt to start a process using its start_cmd. Returns outcome dict."""
    if not ADMIN_ALLOW_PROC_CONTROL:
        raise HTTPException(status_code=403, detail="Process control disabled by server configuration")

    cmd = proc.get("start_cmd") or ""
    container_name = proc.get("container")

    if not cmd and not container_name:
        raise HTTPException(status_code=400, detail="No start command or container configured for this process")

    # ensure debug dir exists so logs can be written
    _ensure_debug_dir()

    # If a container name exists, prefer starting via Docker SDK
    if container_name:
        ok, detail = _start_container(container_name)
        if ok:
            time.sleep(0.5)
            return check_process(proc)

    if cmd:
        try:
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            return check_process(proc)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=500, detail=f"failed to start container {container_name}: {detail}")



@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat() + "Z"}


@app.get('/settings')
def get_settings():
    try:
        cfg = r.hgetall('agent_config') or {}
        # fallbacks match defaults used elsewhere in the project
        idle = int(cfg.get('IDLE_TIMEOUT', 1800))
        stale = int(cfg.get('STALE_TICKS', 15))
        return {'IDLE_TIMEOUT': idle, 'STALE_TICKS': stale}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/settings')
def set_settings(idle_timeout: int = None, stale_ticks: int = None):
    try:
        updates = {}
        if idle_timeout is not None:
            updates['IDLE_TIMEOUT'] = str(int(idle_timeout))
        if stale_ticks is not None:
            updates['STALE_TICKS'] = str(int(stale_ticks))
        if updates:
            r.hset('agent_config', mapping=updates)
        return {'ok': True, 'updated': updates}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    try:
        job_id = str(uuid.uuid4())

        upload_dir = os.path.join(DATA_DIR, "uploads")
        os.makedirs(upload_dir, exist_ok=True)

        saved_filename = f"{job_id}_{file.filename}"
        dest_path = os.path.join(upload_dir, saved_filename)
        with open(dest_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        job = {"job_id": job_id, "filename": saved_filename}
        try:
            r.rpush(QUEUE_NAME, json.dumps(job))
        except Exception as e:
            # Redis push failed — return JSON so browser receives useful info
            print(f"[api] ERROR: failed to enqueue job: {e}")
            return JSONResponse(status_code=500, content={"status": "error", "detail": "failed to enqueue job", "error": str(e)})

        return {"job_id": job_id, "filename": file.filename, "saved_filename": saved_filename, "status": "queued"}
    except Exception as e:
        # Catch-all so the API returns JSON and CORS headers instead of a plain 500
        print(f"[api] ERROR in /upload: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "detail": "upload failed", "error": str(e)})


@app.post("/ingest")
def ingest(payload: dict = Body(...)):
    job_id = payload.get("job_id")
    if not job_id:
        return {"error": "job_id required"}
    path = os.path.join(DATA_DIR, f"{job_id}.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    return {"ok": True, "saved": path, "ts": datetime.utcnow().isoformat() + "Z"}


@app.get("/transcript/{job_id}")
def transcript(job_id: str):
    path = os.path.join(DATA_DIR, f"{job_id}.json")
    if not os.path.exists(path):
        return {"status": "processing"}
    with open(path, "r") as f:
        return json.load(f)


@app.get("/history")
def history():
    jobs = []

    # 1. Completed jobs
    for file in os.listdir(DATA_DIR):
        if file.endswith(".json"):
            with open(os.path.join(DATA_DIR, file), "r") as f:
                jobs.append(json.load(f))

    # 2. Pending jobs still in Redis
    pending = r.lrange(QUEUE_NAME, 0, -1)
    for job_json in pending:
        job = json.loads(job_json)
        jobs.append({
            "job_id": job["job_id"],
            "filename": job["filename"],
            "status": "processing"
        })

    return {"jobs": jobs}


@app.get("/admin/processes")
def list_processes():
    """Return the list of required processes and their current statuses."""
    statuses = [check_process(p) for p in REQUIRED_PROCESSES]
    return {"processes": statuses}


@app.post("/admin/processes/{proc_id}/start")
def api_start_process(proc_id: str):
    """Start a specific process by id, then return updated status."""
    matches = [p for p in REQUIRED_PROCESSES if p["id"] == proc_id]
    if not matches:
        raise HTTPException(status_code=404, detail="Unknown process id")
    proc = matches[0]
    result = start_process(proc)
    return {"process": result}
