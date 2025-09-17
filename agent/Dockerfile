# Base: PyTorch 2.5.1 + CUDA 12.4 + cuDNN 9 (devel image, includes full libs + headers)
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System deps + unzip for rclone installer
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git curl unzip build-essential binutils nano openssh-server pkg-config \
    libavdevice-dev libavfilter-dev libavformat-dev libavcodec-dev libavutil-dev \
    libswscale-dev libswresample-dev libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

# ✅ install latest rclone (with full vfs support)
RUN curl https://rclone.org/install.sh | bash

# SSHD setup
RUN mkdir -p /var/run/sshd

WORKDIR /app

# Copy requirements and install
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip setuptools wheel \
 && pip install --only-binary av --no-cache-dir -r /app/requirements.txt

# ✅ early health check
RUN python3 - <<'EOF'
import torch, whisperx, pyannote.audio, redis
print("Torch version:", torch.__version__)
print("CUDA available:", torch.cuda.is_available())
print("WhisperX import OK")
print("Pyannote.audio version:", pyannote.audio.__version__)
print("Redis client OK")
EOF

# Copy agent + start script
COPY agent.py /app/agent.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# rclone config
RUN mkdir -p /root/.config/rclone
COPY rclone.conf /root/.config/rclone/rclone.conf

# default entry
CMD ["/app/start.sh"]
