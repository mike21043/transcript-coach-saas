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
# Copy the entire repository into the image - simplifies relative paths
COPY . /app

# Install python requirements for the agent. When the build context is the `agent/` directory
# the requirements file will be at /app/requirements.txt. If the file is not present, skip.
RUN pip install --upgrade pip setuptools wheel || true
RUN if [ -f /app/requirements.txt ]; then pip install --only-binary av --no-cache-dir -r /app/requirements.txt; else echo "No requirements.txt in context; skipping pip install"; fi

# Make entry scripts executable if present (some build contexts may place them at /app/)
RUN [ -f /app/start.sh ] && chmod +x /app/start.sh || true
RUN [ -f /app/entrypoint.sh ] && chmod +x /app/entrypoint.sh || true

# Ensure rclone config dir exists (we may override it at runtime via entrypoint)
RUN mkdir -p /root/.config/rclone || true

# default entrypoint → use /app/entrypoint.sh and default cmd to /app/start.sh
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/app/start.sh"]
