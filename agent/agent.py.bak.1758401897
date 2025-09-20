import sys
import os
import json
import redis
import time
import subprocess
# Heavy ML libraries are imported lazily inside the functions that need them
from typing import Dict, List
from pyannote.audio import Pipeline, Model, Inference
from pyannote.core import Segment

# ==============================================================
# Environment configuration
# ==============================================================
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
QUEUE_NAME = os.getenv("QUEUE_NAME", "transcript_jobs")
DATA_DIR = "/data/results"
UPLOADS_DIR = "/data/uploads"
HF_TOKEN = os.getenv("HF_TOKEN", None)
SKIP_EMBEDDINGS = os.getenv("SKIP_EMBEDDINGS", "0").lower() in ("1","true","yes")
SKIP_DIARIZATION = os.getenv("SKIP_DIARIZATION", "0").lower() in ("1","true","yes")
WHISPERX_SIZE = os.getenv("WHISPERX_SIZE", "small")
AGENT_SMOKE = os.getenv("AGENT_SMOKE", "0").lower() in ("1","true","yes")

os.makedirs(DATA_DIR, exist_ok=True)

# ==============================================================
# Redis connection
# ==============================================================
def connect_redis():
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        r.ping()
        print(f"[Agent] Connected to Redis at {REDIS_URL}")
    except Exception as e:
        print(f"[Agent] FATAL: Cannot connect to Redis at {REDIS_URL}: {e}")
        sys.exit(1)
    return r

# ==============================================================
# WhisperX model loader (lazy, with device selection & fallback)
# ==============================================================
_whisperx_model = None
def load_whisperx_model():
    global _whisperx_model
    # import heavy libs lazily to avoid top-level import cost
    import torch
    import whisperx
    # Determine desired device and compute_type first (env-driven)
    device_env = os.getenv("WHISPERX_DEVICE", "cuda")
    device = device_env

    if device_env == "cuda":
        if torch.cuda.is_available():
            try:
                sm = torch.cuda.get_device_capability()
                print(f"[Agent] CUDA device capability: {sm}")
            except Exception as e:
                print(f"[Agent] Could not read compute capability: {e}")
            device = "cuda"
        else:
            print("[Agent] CUDA not available, falling back to CPU for WhisperX.")
            device = "cpu"
    else:
        device = "cpu"

    # Decide compute type: allow override via WHISPERX_COMPUTE_TYPE
    compute_type_env = os.getenv("WHISPERX_COMPUTE_TYPE", "").strip().lower()
    if compute_type_env:
        compute_type = compute_type_env
    else:
        # Default to float32 on CPU, float16 on CUDA
        compute_type = "float32" if device == "cpu" else "float16"

    # If a model is already loaded, try to inspect its device/dtype and reload if it doesn't match desired settings
    if _whisperx_model is not None:
        try:
            candidate = _whisperx_model
            inner = getattr(candidate, 'model', None) or getattr(candidate, 'whisper_model', None) or getattr(candidate, 'asr_model', None) or None
            if inner is not None:
                params = list(inner.parameters())
                if params:
                    p = params[0]
                    p_dev = 'cuda' if p.device.type == 'cuda' else 'cpu'
                    p_dtype = str(p.dtype).lower()
                    desired_dtype_str = 'float16' if compute_type == 'float16' else 'float32'
                    if p_dev == device and desired_dtype_str in p_dtype:
                        # existing model matches desired device and dtype
                        return _whisperx_model
                    else:
                        print(f"[Agent] Existing WhisperX model device/dtype mismatch (have {p_dev}/{p_dtype}, want {device}/{desired_dtype_str}), reloading.")
                        _whisperx_model = None
            else:
                # Unknown internal structure — assume reload to be safe
                print("[Agent] Could not introspect existing WhisperX model; reloading to enforce desired compute type.")
                _whisperx_model = None
        except Exception as e:
            print(f"[Agent] Warning: failed to inspect existing WhisperX model: {e}; reloading to be safe.")
            _whisperx_model = None

    if _whisperx_model is None:
        print(f"[Agent] Loading WhisperX model on device: {device} (compute_type={compute_type}, size={WHISPERX_SIZE})")
        # Try loading with the selected compute_type; use WHISPERX_SIZE env var to control size
        try:
            _whisperx_model = whisperx.load_model(WHISPERX_SIZE, device=device, compute_type=compute_type)
        except Exception as e:
            print(f"[Agent] Warning: failed to load WhisperX with compute_type={compute_type}: {e}")
            if compute_type != "float32":
                try:
                    print("[Agent] Retrying WhisperX load with compute_type=float32")
                    _whisperx_model = whisperx.load_model(WHISPERX_SIZE, device=device, compute_type="float32")
                except Exception as e2:
                    print(f"[Agent] FATAL: failed to load WhisperX with compute_type=float32: {e2}")
                    raise
            else:
                # Already tried float32 and failed
                raise

    return _whisperx_model

# ==============================================================
# Diarization pipeline loader (lazy, forced to CPU)
# ==============================================================
_diarize_pipeline = None
def load_diarization_pipeline():
    global _diarize_pipeline
    if _diarize_pipeline is None:
        if HF_TOKEN is None:
            raise RuntimeError("HF_TOKEN must be set for diarization/embedding models.")
        print("[Agent] Loading Pyannote diarization pipeline (forced to CPU)...")
        _diarize_pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization",
            use_auth_token=HF_TOKEN,
        )
        _diarize_pipeline.to(torch.device("cpu"))
    return _diarize_pipeline

# ==============================================================
# Embedding inference loader (lazy, forced to CPU)
# ==============================================================
_embedding_inference = None
def load_embedding_inference():
    global _embedding_inference
    if _embedding_inference is None:
        if HF_TOKEN is None:
            raise RuntimeError("HF_TOKEN must be set for embedding models.")
        print("[Agent] Loading Pyannote embedding inference (CPU)...")
        model = Model.from_pretrained("pyannote/embedding", use_auth_token=HF_TOKEN)
        _embedding_inference = Inference(model, device="cpu")
    return _embedding_inference

# ==============================================================
# Map segments with speaker labels
# ==============================================================
def map_segments_with_speakers(segments: List[Dict], diarization) -> List[Dict]:
    labeled = []
    turns = [(turn.start, turn.end, spk) for turn, _, spk in diarization.itertracks(yield_label=True)]
    for s in segments:
        s_start = float(s.get("start", 0.0))
        s_end = float(s.get("end", 0.0))
        speaker = "unknown"
        for t_start, t_end, spk in turns:
            if s_start >= t_start and s_end <= t_end:
                speaker = spk
                break
        labeled.append(
            {
                "text": s.get("text", ""),
                "start": s_start,
                "end": s_end,
                "speaker": speaker,
            }
        )
    return labeled

# ==============================================================
# Audio preprocessing: convert to WAV via ffmpeg
# ==============================================================
def convert_to_wav(filepath: str, job_id: str) -> str:
    tmp_wav = f"/tmp/{job_id}.wav"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", filepath, "-ar", "16000", "-ac", "1", tmp_wav],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        print(f"[Agent] Converted {filepath} → {tmp_wav}")
        return tmp_wav
    except subprocess.CalledProcessError as e:
        print(f"[Agent] ffmpeg stderr: {e.stderr.decode()}")
        raise RuntimeError(f"ffmpeg conversion failed for {filepath}")

# ==============================================================
# Process a transcription job
# ==============================================================
def process_job(job: Dict, r: redis.Redis):
    job_id = job.get("job_id") or job.get("id") or "unknown"
    filename = job.get("filename")
    filepath = os.path.join(UPLOADS_DIR, filename) if filename else None
    result_key = f"result:{job_id}"

    print(f"[Agent] Received job: {job_id} (filename={filename})")

    if not filename or not filepath or not os.path.exists(filepath):
        err = f"Input file not found: {filepath}"
        print(f"[Agent] ERROR: {err}")
        r.set(result_key, json.dumps({"job_id": job_id, "status": "error", "error": err}))
        return

    # Fast smoke-path (no ML) for quick mount/queue validation
    if AGENT_SMOKE:
        print(f"[Agent] AGENT_SMOKE enabled — writing quick result for {job_id}")
        transcript = {
            "job_id": job_id,
            "filename": filename,
            "segments": [{"text": "(smoke test)", "start": 0.0, "end": 1.0, "speaker": "unknown"}],
            "embeddings": {},
            "status": "completed",
        }
        outpath = os.path.join(DATA_DIR, f"{job_id}.json")
        with open(outpath, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False)
        r.set(result_key, json.dumps(transcript))
        print(f"[Agent] AGENT_SMOKE: wrote {outpath} and set {result_key}")
        return

    try:
        # Always convert audio to deterministic temp WAV
        wav_file = convert_to_wav(filepath, job_id)

        # Transcription
        model = load_whisperx_model()
        audio = whisperx.load_audio(wav_file)
        asr = model.transcribe(audio)
        segments = asr.get("segments", [])

        # Diarization (optional)
        if SKIP_DIARIZATION:
            print("[Agent] SKIP_DIARIZATION is set; skipping diarization.")
            diar = None
            # label segments without speaker info
            labeled_segments = [
                {
                    "text": s.get("text", ""),
                    "start": float(s.get("start", 0.0)),
                    "end": float(s.get("end", 0.0)),
                    "speaker": "unknown",
                }
                for s in segments
            ]
        else:
            diarize = load_diarization_pipeline()
            diar = diarize(wav_file)
            labeled_segments = map_segments_with_speakers(segments, diar)

        segments = labeled_segments

        # Embeddings (optional). Can be skipped entirely with SKIP_EMBEDDINGS=1
        embeddings = {}
        if SKIP_EMBEDDINGS or diar is None:
            if SKIP_EMBEDDINGS:
                print("[Agent] SKIP_EMBEDDINGS is set; skipping embedding extraction.")
            else:
                print("[Agent] Diarization skipped; skipping embeddings.")
            embeddings = {}
        else:
            # Embeddings: best-effort — if embedding extraction fails, continue
            try:
                embedding_inference = load_embedding_inference()
                for turn, _, speaker in diar.itertracks(yield_label=True):
                    segment = Segment(turn.start, turn.end)
                    try:
                        emb = embedding_inference.crop(wav_file, segment)
                        # If emb is a tensor/ndarray-like, convert to list; otherwise store None
                        try:
                            embeddings[speaker] = emb.tolist()
                        except Exception:
                            embeddings[speaker] = None
                    except Exception as e:
                        print(f"[Agent] WARNING: failed to compute embedding for {speaker}: {e}")
                        embeddings[speaker] = None
            except Exception as e:
                print(f"[Agent] WARNING: embedding inference setup failed: {e}")
                embeddings = {}

        # Build transcript object
        transcript = {
            "job_id": job_id,
            "filename": filename,
            "segments": segments,
            "embeddings": embeddings,
            "status": "completed",
        }

        # Save to disk
        outpath = os.path.join(DATA_DIR, f"{job_id}.json")
        with open(outpath, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False)
        print(f"[Agent] Saved transcript to {outpath}")

        # Save to Redis
        r.set(result_key, json.dumps(transcript))
        print(f"[Agent] Job {job_id} completed.")

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[Agent] ERROR processing {job_id}: {err}")
        r.set(result_key, json.dumps({"job_id": job_id, "status": "error", "error": err}))

# ==============================================================
# Main loop
# ==============================================================
def main():
    r = connect_redis()
    print(f"[Agent] Listening on queue: {QUEUE_NAME}")

    while True:
        try:
            _, job_data = r.blpop(QUEUE_NAME)
            job = json.loads(job_data)
            process_job(job, r)
        except Exception as e:
            print(f"[Agent] ERROR reading job from queue: {e}")
            time.sleep(1.0)

if __name__ == "__main__":
    main()
