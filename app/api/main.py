from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import PlainTextResponse
import os, uuid, redis, json

app = FastAPI(title="TranscriptCoach API")

r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))

DATA_DIR = os.getenv("DATA_DIR", "/data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
TRANSCRIPTS_DIR = os.path.join(DATA_DIR, "transcripts")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/upload")
async def upload_audio(file: UploadFile = File(...)):
    tid = str(uuid.uuid4())
    # keep a clean basename; some browsers send just "file"
    orig_name = file.filename or "audio.wav"
    ext = os.path.splitext(orig_name)[1] or ".wav"
    out_path = os.path.join(UPLOAD_DIR, f"{tid}{ext}")
    with open(out_path, "wb") as f:
        f.write(await file.read())

    job = {"tid": tid, "filename": orig_name, "path": out_path}
    r.lpush("jobs", json.dumps(job))

    return {"job_id": tid, "message": "File uploaded, job enqueued."}

@app.get("/transcripts")
def list_transcripts():
    items = []
    for fn in os.listdir(TRANSCRIPTS_DIR):
        if fn.endswith(".txt"):
            items.append(fn)
    return {"transcripts": sorted(items)}

@app.get("/transcripts/{name}", response_class=PlainTextResponse)
def get_transcript(name: str):
    safe = os.path.basename(name)
    path = os.path.join(TRANSCRIPTS_DIR, safe)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Not found")
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()
