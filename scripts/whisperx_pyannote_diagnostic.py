#!/usr/bin/env python3
"""
Comprehensive diagnostic for GPU/Torch/WhisperX/Pyannote.
Writes JSON report to /workspace/smoke_diagnostic.json and prints a short human-readable summary.

Behavior:
- Runs lightweight checks (nvidia-smi if present, torch import)
- Optionally attempts to load WhisperX model and Pyannote pipeline if DO_DOWNLOAD_MODELS=1
  and HF_TOKEN is set. By default DO_DOWNLOAD_MODELS=0 to avoid heavy downloads in OnStart.
- Masks secrets in output and avoids printing HF_TOKEN value.
"""
import json
import os
import sys
import traceback
import subprocess
from datetime import datetime

OUT_JSON = '/workspace/smoke_diagnostic.json'
DO_DOWNLOAD = os.getenv('DO_DOWNLOAD_MODELS', '0') == '1'
HF_TOKEN = os.getenv('HF_TOKEN')

report = {
    'ts': datetime.utcnow().isoformat() + 'Z',
    'host_cwd': os.getcwd(),
    'nvidia_smi': None,
    'torch': {'present': False},
    'whisperx': {'present': False},
    'pyannote': {'present': False},
    'notes': [],
}

def run_cmd(cmd):
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=15)
        return out.decode('utf-8', errors='replace')
    except Exception as e:
        return f'ERROR: {e}'

# 1) nvidia-smi
report['nvidia_smi'] = run_cmd('nvidia-smi || true')

# 2) torch
try:
    import torch
    report['torch']['present'] = True
    report['torch']['version'] = getattr(torch, '__version__', 'unknown')
    try:
        report['torch']['cuda_available'] = bool(torch.cuda.is_available())
        if report['torch']['cuda_available']:
            try:
                report['torch']['cuda_device_count'] = int(torch.cuda.device_count())
            except Exception as e:
                report['torch']['cuda_device_count'] = f'ERROR: {e}'
    except Exception as e:
        report['torch']['cuda_available'] = f'ERROR: {e}'
except Exception as e:
    report['torch']['error'] = traceback.format_exc()

# 3) whisperx import
try:
    import whisperx
    report['whisperx']['present'] = True
    # whisperx may not have a __version__ attribute
    report['whisperx']['version'] = getattr(whisperx, '__version__', None)
except Exception as e:
    report['whisperx']['error'] = traceback.format_exc()

# 4) pyannote import
try:
    import pyannote
    from pyannote.audio import Pipeline
    report['pyannote']['present'] = True
    report['pyannote']['version'] = getattr(pyannote, '__version__', None)
except Exception as e:
    report['pyannote']['error'] = traceback.format_exc()

# 5) optionally try to load models (controlled to avoid long downloads)
if DO_DOWNLOAD:
    # WhisperX model load attempt: prefer GPU if available
    try:
        device = 'cpu'
        if report.get('torch', {}).get('cuda_available'):
            device = 'cuda'
        report['whisperx']['attempt_load'] = {'device': device}
        try:
            mdl = whisperx.load_model('small', device=device)
            report['whisperx']['attempt_load']['ok'] = True
        except Exception as e:
            report['whisperx']['attempt_load']['ok'] = False
            report['whisperx']['attempt_load']['error'] = traceback.format_exc()
    except Exception as e:
        report['whisperx']['attempt_load'] = {'ok': False, 'error': traceback.format_exc()}

    # Pyannote pipeline attempt (needs HF_TOKEN)
    if not HF_TOKEN:
        report['pyannote']['attempt_load'] = {'ok': False, 'error': 'HF_TOKEN not set'}
    else:
        try:
            report['pyannote']['attempt_load'] = {'ok': False}
            # force CPU to avoid cuDNN issues in some setups
            p = Pipeline.from_pretrained('pyannote/speaker-diarization', use_auth_token=HF_TOKEN)
            report['pyannote']['attempt_load']['ok'] = True
        except Exception as e:
            report['pyannote']['attempt_load']['ok'] = False
            report['pyannote']['attempt_load']['error'] = traceback.format_exc()

# Masked env summary
def mask_secret(s):
    if not s:
        return None
    return s[:4] + '...' + s[-4:]

report['env'] = {
    'REDIS_URL': os.getenv('REDIS_URL'),
    'QUEUE_NAME': os.getenv('QUEUE_NAME'),
    'HF_TOKEN_masked': mask_secret(HF_TOKEN),
    'DO_DOWNLOAD_MODELS': DO_DOWNLOAD,
}

# Save JSON
try:
    os.makedirs('/workspace', exist_ok=True)
    with open(OUT_JSON, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"Wrote diagnostic to {OUT_JSON}")
except Exception as e:
    print(f"Failed to write {OUT_JSON}: {e}")

# Short human summary
print('--- diagnostic summary ---')
print('nvidia-smi (first 200 chars):')
print(report['nvidia_smi'][:200])
print('\nTorch present:', report['torch'].get('present'))
if report['torch'].get('present'):
    print(' torch.version:', report['torch'].get('version'))
    print(' cuda_available:', report['torch'].get('cuda_available'))
    print(' cuda_device_count:', report['torch'].get('cuda_device_count'))
else:
    print(' torch error: (see JSON)')

print('\nwhisperx present:', report['whisperx'].get('present'))
if report['whisperx'].get('present'):
    print(' whisperx.version:', report['whisperx'].get('version'))
else:
    print(' whisperx error: (see JSON)')

print('\npyannote present:', report['pyannote'].get('present'))
if report['pyannote'].get('present'):
    print(' pyannote.version:', report['pyannote'].get('version'))
else:
    print(' pyannote error: (see JSON)')

print('\nDO_DOWNLOAD_MODELS =', DO_DOWNLOAD)
print('If you want the script to attempt loading models (may download >1GB), set DO_DOWNLOAD_MODELS=1 and provide HF_TOKEN to allow pyannote downloads.')
print('--- end ---')
