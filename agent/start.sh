#!/usr/bin/env bash
set -euo pipefail

echo "[Start] Starting sshd..."
service ssh start

# ---- Hugging Face token check ----
if [ -z "${HF_TOKEN:-}" ]; then
  echo "[Start] ERROR: HF_TOKEN not set."
  exit 1
else
  echo "[Start] HF_TOKEN detected."
fi

# ---- Data dirs ----
mkdir -p /data/uploads /data/results

# ---- pCloud sync loop (no FUSE) ----
if rclone listremotes 2>/dev/null | grep -q '^pcloudtc:'; then
  echo "[Start] Launching pCloud sync loop..."
  while true; do
    rclone sync pcloudtc:transcript-coach-saas/data/uploads /data/uploads --ignore-errors
    rclone sync /data/results pcloudtc:transcript-coach-saas/data/results --ignore-errors
    sleep 30
  done &
fi

# ---- Torch/Torchaudio per-GPU ----
echo "[Start] Checking for NVIDIA GPU..."
if command -v nvidia-smi &> /dev/null; then
  echo "[Start] NVIDIA GPU detected → installing CUDA 12.4 wheels"
  pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu124 \
    torch==2.5.1+cu124 torchaudio==2.5.1+cu124 torchvision==0.20.1+cu124
  GPU_PRESENT=1
else
  echo "[Start] No GPU found → installing CPU wheels"
  pip install --no-cache-dir \
    torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1
  GPU_PRESENT=0
fi

# ---- Agent requirements ----
echo "[Start] Installing Python requirements..."
pip install --no-cache-dir -r /app/requirements.txt
pip install --no-cache-dir "nvidia-cudnn-cu12>=9,<10" "nvidia-cublas-cu12>=12,<13" "nvidia-cuda-runtime-cu12>=12,<13"

# ---- Resolve CUDA/cuDNN library paths from NVIDIA Python wheels ----
echo "[Start] Resolving CUDA/cuDNN library paths from nvidia Python packages..."
LIBS=$(python3 - <<'PY'
import os
def libdir(mod):
    try:
        m = __import__(mod, fromlist=['__file__'])
        return os.path.join(os.path.dirname(m.__file__), 'lib')
    except Exception:
        return ''
print('|'.join(filter(None, [
    libdir('nvidia.cudnn'),
    libdir('nvidia.cublas'),
    libdir('nvidia.cuda_runtime')
])))
PY
)

IFS='|' read -r CUDNN_LIB CUBLAS_LIB CUDA_RUNTIME_LIB <<< "$LIBS"

for p in "$CUDNN_LIB" "$CUBLAS_LIB" "$CUDA_RUNTIME_LIB"; do
  if [ -n "${p:-}" ] && [ -d "$p" ]; then
    export LD_LIBRARY_PATH="$p:${LD_LIBRARY_PATH:-}"
    echo "[Start] Added to LD_LIBRARY_PATH: $p"
  fi
done

# ---- Add missing cuDNN symlinks if needed ----
if [ -n "${CUDNN_LIB:-}" ] && [ -d "$CUDNN_LIB" ]; then
  pushd "$CUDNN_LIB" >/dev/null
  for base in libcudnn_cnn libcudnn_ops; do
    if ls $base.so.* >/dev/null 2>&1; then
      latest=$(ls -1 $base.so.* | sort -V | tail -n1)
      [ ! -e $base.so.9 ] && ln -s "$latest" $base.so.9 && echo "[Start] Symlinked $base.so.9 -> $latest"
      [ ! -e $base.so ] && ln -s "$latest" $base.so && echo "[Start] Symlinked $base.so -> $latest"
    fi
  done
  popd >/dev/null
fi

# ---- Force pyannote to CPU to avoid cuDNN crashes ----
export PYANNOTE_DEVICE=cpu
echo "[Start] pyannote device forced to CPU (PYANNOTE_DEVICE=$PYANNOTE_DEVICE)"

# ---- Log verification ----
echo "[Start] LD_LIBRARY_PATH=$LD_LIBRARY_PATH"

# ---- Launch agent ----
echo "[Start] Launching agent... (GPU_PRESENT=$GPU_PRESENT)"
exec python3 /app/agent.py
