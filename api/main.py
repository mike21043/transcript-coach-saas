from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import settings
import shutil
import os
import uuid
import redis
import json
from datetime import datetime

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

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    filename = file.filename
    save_path = os.path.join(UPLOAD_DIR, filename)

    with open(save_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Validate file
    try:
        if not os.path.exists(save_path):
            raise FileNotFoundError(f"{save_path} not found after save.")
        if os.path.getsize(save_path) == 0:
            raise ValueError(f"{save_path} is empty after save.")
    except Exception as e:
        return {"job_id": job_id, "filename": filename, "status": "error", "detail": str(e)}

    # Enqueue job
    job = {"job_id": job_id, "filename": filename}
    r.rpush("transcript_jobs", json.dumps(job))

    return {"job_id": job_id, "filename": filename, "status": "queued"}

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
