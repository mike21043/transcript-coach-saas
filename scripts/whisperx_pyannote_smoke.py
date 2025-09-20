#!/usr/bin/env python3
"""
Smoke test for Torch, WhisperX and Pyannote availability and basic model loading.
Saves output to stdout. Use on your target (local or GPU) instance.
"""
import os
import sys
import traceback

def info(msg):
    print(msg)

info("--- environment ---")
info(f"python: {sys.version.splitlines()[0]}")
info(f"cwd: {os.getcwd()}")

try:
    import torch
    info(f"torch: {torch.__version__}")
    try:
        info(f"cuda available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            try:
                info(f"cuda devices: {torch.cuda.device_count()}")
            except Exception as e:
                info(f"cuda device info error: {e}")
    except Exception as e:
        info(f"cuda detect error: {e}")
except Exception as e:
    info("torch import FAILED")
    traceback.print_exc()

# WhisperX import & model load
try:
    import whisperx
    info(f"whisperx: {whisperx.__version__}")
    # Try to load a small model (device from env or fallback to cpu)
    device_env = os.getenv('WHISPERX_DEVICE', 'cuda')
    device = 'cpu'
    if device_env == 'cuda' and torch and getattr(torch, 'cuda', None) and torch.cuda.is_available():
        device = 'cuda'
    else:
        device = 'cpu'
    info(f"attempting whisperx.load_model('small', device={device})")
    try:
        model = whisperx.load_model('small', device=device)
        info("whisperx model loaded OK")
    except Exception as e:
        info(f"whisperx.load_model FAILED: {e}")
        traceback.print_exc()
except Exception as e:
    info("whisperx import FAILED")
    traceback.print_exc()

# Pyannote import and pipeline (requires HF_TOKEN)
try:
    from pyannote.audio import Pipeline
    info("pyannote.audio import OK")
    hf = os.getenv('HF_TOKEN')
    if not hf:
        info("HF_TOKEN not set — skipping pyannote pipeline load (need token to download models)")
    else:
        info("HF_TOKEN present — attempting to load pyannote pipeline 'pyannote/speaker-diarization' (CPU)")
        try:
            p = Pipeline.from_pretrained('pyannote/speaker-diarization', use_auth_token=hf)
            info("pyannote pipeline loaded OK")
        except Exception as e:
            info(f"pyannote Pipeline.from_pretrained FAILED: {e}")
            traceback.print_exc()
except Exception as e:
    info("pyannote import FAILED")
    traceback.print_exc()

info('--- test complete ---')
