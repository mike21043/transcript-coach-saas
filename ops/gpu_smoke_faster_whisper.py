#!/usr/bin/env python3
"""
GPU smoke test using faster-whisper.

Usage (on the VM, inside your venv):
  python3 /root/transcript-coach-saas/ops/gpu_smoke_faster_whisper.py \
    --input /data/uploads/Mike.m4a \
    --output /data/results/gpu-transcript-fasterwhisper.json \
    --model small

Environment variables respected (optional):
  WHISPER_MODEL (default: small)
  WHISPERX_DEVICE (default: cuda)
  WHISPERX_COMPUTE_TYPE (default: float16)

This script uses faster-whisper's simple API and writes a compact JSON with
segments. It's intended as a quick GPU verification step.
"""

import argparse
import json
import os
import sys

try:
    from faster_whisper import WhisperModel
except Exception as e:
    print("ERROR: faster_whisper not importable:", e, file=sys.stderr)
    print("Install it inside the venv with: pip install faster-whisper", file=sys.stderr)
    raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=os.environ.get("SMOKE_INPUT", "/data/uploads/Mike.m4a"))
    parser.add_argument("--output", default=os.environ.get("SMOKE_OUTPUT", "/data/results/gpu-transcript-fasterwhisper.json"))
    parser.add_argument("--model", default=os.environ.get("WHISPER_MODEL", "small"))
    parser.add_argument("--device", default=os.environ.get("WHISPERX_DEVICE", "cuda"))
    parser.add_argument("--compute_type", default=os.environ.get("WHISPERX_COMPUTE_TYPE", "float16"))
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: input file does not exist: {args.input}", file=sys.stderr)
        sys.exit(2)

    os.makedirs(os.path.dirname(args.output), exist_ok=True)

    print(f"Loading faster-whisper model='{args.model}' device={args.device} compute_type={args.compute_type}")
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)

    print("Starting transcription...")
    segments, info = model.transcribe(args.input, beam_size=5)

    out = {
        "language": getattr(info, "language", None),
        "duration": getattr(info, "duration", None),
        "segments": []
    }

    for seg in segments:
        out["segments"].append({
            "start": float(seg.start),
            "end": float(seg.end),
            "text": seg.text.strip()
        })

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    print("Done. Wrote:", args.output)


if __name__ == "__main__":
    main()
