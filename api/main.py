from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import settings
import shutil
import os
import uuid
import redis
import json
from datetime import datetime
import subprocess, time, requests
from typing import Optional
from fastapi import HTTPException, Body

app = FastAPI()

# include the settings routes
app.include_router(settings.router)

# CORS for UI access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared storage
UPLOAD_DIR = "/data/uploads"
RESULTS_DIR = "/data/results"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)

# Redis
import redis
r = redis.Redis.from_url("redis://38.242.200.197:6379/0")

# Process control guard
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
        # If already running, nothing to do
        if getattr(c, "status", "") == "running":
            return True, "already-running"
        c.start()
        # refresh
        try:
            c.reload()
        except Exception:
            pass
        return (getattr(c, "status", "") == "running"), f"status={getattr(c, 'status', '')}"
    except Exception as e:
        return False, str(e)

# Required processes list (tweak patterns/commands to match your deployment)
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
        "check": {"type": "mount", "path": "/data/shared"},
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
            # If a container name is provided, prefer checking the container status
            container_name = proc.get("container") or chk.get("container")
            client = _get_docker_client()
            if container_name and client:
                status = _container_running(container_name)
                detail = f"container={container_name},running={status}"
            elif client:
                # try to infer from docker containers if possible
                try:
                    found = False
                    for c in client.containers.list(all=True):
                        # check container name and command/args for the pattern
                        if pat in c.name or pat in " ".join(c.attrs.get("Config", {}).get("Cmd") or []):
                            if getattr(c, "status", "") == "running":
                                found = True
                                break
                    status = bool(found)
                    detail = f"docker-scan,matched={found}"
                except Exception as e:
                    # fallback to pgrep on failure
                    res = subprocess.run(["pgrep", "-f", pat], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    status = res.returncode == 0
                    detail = res.stdout.decode().strip()[:200]
            else:
                # No docker SDK available; fall back to pgrep
                res = subprocess.run(["pgrep", "-f", pat], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                status = res.returncode == 0
                detail = res.stdout.decode().strip()[:200]
        elif typ == "mount":
            path = chk.get("path")
            status = os.path.ismount(path) or os.path.exists(path)
            detail = f"exists={os.path.exists(path)}"
        else:
            detail = "unknown-check"
    except Exception as e:
        detail = str(e)
    return {"id": proc["id"], "name": proc["name"], "ok": bool(status), "detail": detail}


def start_process(proc: dict) -> dict:
    if not ADMIN_ALLOW_PROC_CONTROL:
        raise HTTPException(status_code=403, detail="Process control disabled by server configuration")
    cmd = proc.get("start_cmd") or ""
    container_name = proc.get("container")

    if not cmd and not container_name:
        raise HTTPException(status_code=400, detail="No start command or container configured for this process")

    _ensure_debug_dir()

    # If this process is managed by docker (has a container name), try to start the container
    if container_name:
        ok, detail = _start_container(container_name)
        if ok:
            # give it a moment then return refreshed status
            time.sleep(0.5)
            return check_process(proc)
        # fall through to try shell start if a start_cmd is present

    if cmd:
        try:
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            return check_process(proc)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=500, detail=f"failed to start container {container_name}: {detail}")


@app.get('/settings')
def get_settings():
    try:
        cfg = r.hgetall('agent_config') or {}
        idle = int(cfg.get('IDLE_TIMEOUT', 1800))
        stale = int(cfg.get('STALE_TICKS', 15))
        return {'IDLE_TIMEOUT': idle, 'STALE_TICKS': stale}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/settings')
def set_settings(idle_timeout: int = Body(None), stale_ticks: int = Body(None)):
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


@app.get("/admin/processes")
def list_processes():
    statuses = [check_process(p) for p in REQUIRED_PROCESSES]
    return {"processes": statuses}


@app.post("/admin/processes/{proc_id}/start")
def api_start_process(proc_id: str):
    matches = [p for p in REQUIRED_PROCESSES if p["id"] == proc_id]
    if not matches:
        raise HTTPException(status_code=404, detail="Unknown process id")
    proc = matches[0]
    result = start_process(proc)
    return {"process": result}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    filename = file.filename
    saved_filename = f"{job_id}_{filename}"
    save_path = os.path.join(UPLOAD_DIR, saved_filename)

    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Validate file
    try:
        if not os.path.exists(save_path):
            raise FileNotFoundError(f"{save_path} not found after save.")
        if os.path.getsize(save_path) == 0:
            raise ValueError(f"{save_path} is empty after save.")
    except Exception as e:
        return {"job_id": job_id, "filename": filename, "saved_filename": saved_filename, "status": "error", "detail": str(e)}

    # Enqueue job (worker expects the saved filename)
    job = {"job_id": job_id, "filename": saved_filename}
    r.rpush("transcript_jobs", json.dumps(job))

    return {"job_id": job_id, "filename": filename, "saved_filename": saved_filename, "status": "queued"}

@app.get("/status/{job_id}")
def get_status(job_id: str):
    if r.exists(f"result:{job_id}"):
        return {"job_id": job_id, "status": "done"}
    else:
        return {"job_id": job_id, "status": "processing"}

@app.get("/result/{job_id}")
def get_result(job_id: str):
    data = r.get(f"result:{job_id}")
    if not data:
        return {"job_id": job_id, "status": "processing"}
    try:
        return json.loads(data)
    except Exception:
        return {"job_id": job_id, "status": "error", "detail": "Invalid JSON in result"}

@app.get("/results")
def list_results():
    """
    List transcripts from RESULTS_DIR.
    Only return .srt files, cleaned of counters and timestamps,
    with extra line breaks between speakers.
    """
    results = []
    try:
        for fname in os.listdir(RESULTS_DIR):
            if not fname.endswith(".srt"):
                continue

            fpath = os.path.join(RESULTS_DIR, fname)
            if not os.path.isfile(fpath):
                continue

            dt = datetime.fromtimestamp(os.path.getmtime(fpath)).isoformat()
            record = {
                "id": fname.split(".")[0],
                "filename": fname,
                "datetime": dt,
                "transcript": None,
            }

            try:
                with open(fpath, "r") as f:
                    lines = f.readlines()
                clean_lines = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    if line.isdigit() or "-->" in line:
                        continue
                    clean_lines.append(line)
                # add blank line between each speaker line
                record["transcript"] = "\n\n".join(clean_lines)
            except Exception:
                record["transcript"] = None

            results.append(record)

        results.sort(key=lambda x: x["datetime"], reverse=True)

    except Exception as e:
        return {"error": str(e)}

    return results

@app.get("/health")
def health():
    try:
        r.ping()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
