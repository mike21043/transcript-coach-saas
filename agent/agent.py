import sys
import importlib

# ==============================================================
# Startup self-check: print versions of all critical packages
# ==============================================================

print("=== Transcript Coach Agent Startup ===")
print("Python:", sys.version)

def check_version(pkg, attr="__version__"):
    try:
        m = importlib.import_module(pkg)
        v = getattr(m, attr, "unknown")
        print(f"{pkg}: {v}")
    except Exception as e:
        print(f"{pkg}: missing ({e})")

check_version("torch")
check_version("torchaudio")
check_version("whisperx")
check_version("pyannote.audio")
check_version("fastapi")
check_version("pydantic")
check_version("pydantic_core")
check_version("redis")
check_version("requests")
check_version("numpy")
check_version("pandas")

print("======================================\n")

# ==============================================================
# Normal Agent logic with WhisperX + Pyannote
# ==============================================================

import os
import json
import redis
import requests
import torch
import time
from fastapi import FastAPI
import whisperx

DATA_DIR = os.getenv("DATA_DIR", "/data")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
QUEUE_NAME = "transcript_jobs"

HF_TOKEN = os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

def process_job(job):
    job_id = job["job_id"]
    filename = job["filename"]
    filepath = os.path.join(DATA_DIR, "uploads", filename)

    print(f"[Agent] Processing {filepath} (job_id={job_id})")

    try:
        # --------------------------
        # Load WhisperX model
        # --------------------------
        device = "cpu"
        model = whisperx.load_model("small", device=device)

        # --------------------------
        # Transcription
        # --------------------------
        audio = whisperx.load_audio(filepath)
        result = model.transcribe(audio)

        # --------------------------
        # Diarization (optional)
        # --------------------------
        diarization_failed = False
        try:
            if HF_TOKEN:
                from pyannote.audio import Pipeline
                diarize_pipeline = Pipeline.from_pretrained(
                    "pyannote/speaker-diarization@2.1",
                    use_auth_token=HF_TOKEN,
                )
                diarization = diarize_pipeline(filepath)
                # Assign speakers (basic merge)
                segments = []
                for seg in result["segments"]:
                    speaker = "unknown"
                    for turn, _, spk in diarization.itertracks(yield_label=True):
                        if seg["start"] >= turn.start and seg["end"] <= turn.end:
                            speaker = spk
                            break
                    segments.append({
                        "text": seg["text"],
                        "start": seg["start"],
                        "end": seg["end"],
                        "speaker": speaker,
                    })
            else:
                raise RuntimeError("HF_TOKEN not set")
        except Exception as e:
            diarization_failed = True
            print(f"[Agent] Diarization failed, continuing without speakers: {e}")
            segments = [
                {
                    "text": seg["text"],
                    "start": seg["start"],
                    "end": seg["end"],
                }
                for seg in result["segments"]
            ]

        # --------------------------
        # Save transcript
        # --------------------------
        transcript = {
            "job_id": job_id,
            "filename": filename,
            "segments": segments,
        }

        outpath = os.path.join(DATA_DIR, f"{job_id}.json")
        with open(outpath, "w") as f:
            json.dump(transcript, f)

        print(f"[Agent] Saved transcript to {outpath}")

    except Exception as e:
        print(f"[Agent] ERROR processing {job_id}: {e}")

def main():
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    print(f"[Agent] Starting job processor...")

    while True:
        _, job_data = r.blpop(QUEUE_NAME)
        job = json.loads(job_data)
        process_job(job)

if __name__ == "__main__":
    import threading
    import uvicorn

    # Run FastAPI health server in background
    def run_api():
        uvicorn.run(app, host="0.0.0.0", port=7000, log_level="info")

    t = threading.Thread(target=run_api, daemon=True)
    t.start()

    # Start job loop
    main()
