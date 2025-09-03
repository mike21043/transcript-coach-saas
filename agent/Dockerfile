# Base image with CUDA and PyTorch (saves hours of build time)
FROM pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime

# Prevent interactive prompts (no pink screens)
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    build-essential \
    binutils \
 && rm -rf /var/lib/apt/lists/*

# Install Python dependencies for agent
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir \
    redis \
    whisperx \
    torch \
    torchaudio \
    pyannote.audio \
    requests

# Create app directory
WORKDIR /app

# Copy agent code
COPY . /app

# Default command: run the agent
CMD ["python", "agent.py"]
