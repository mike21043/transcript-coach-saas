from fastapi import FastAPI, UploadFile, File, Body
from datetime import datetime
import os, json, shutil, uuid, redis

app = FastAPI()
DATA_DIR = os.getenv("DATA_DIR", "/data")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
QUEUE_NAME = "transcript_jobs"

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


@app.get("/health")
def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat() + "Z"}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())

    upload_dir = os.path.join(DATA_DIR, "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    dest_path = os.path.join(upload_dir, f"{job_id}_{file.filename}")
    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    job = {"job_id": job_id, "filename": f"{job_id}_{file.filename}"}
    r.rpush(QUEUE_NAME, json.dumps(job))

    return {"job_id": job_id, "filename": file.filename, "status": "processing"}


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
