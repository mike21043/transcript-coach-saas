GPU smoke-test setup (for the VM)

Steps to run on the GPU VM (assumes Ubuntu 22.04 and you are root or using sudo):

1) Prepare directories

sudo mkdir -p /data/uploads /data/results
sudo chown -R $(whoami):$(whoami) /data

2) Install system deps (ffmpeg, git, build tools, rclone if needed)

sudo apt update
sudo apt install -y ffmpeg git build-essential libsndfile1

# Optional: install rclone and configure using /root/rclone.conf if you copied it
# curl https://rclone.org/install.sh | sudo bash

3) Create and activate a Python venv (we use /opt/tc-venv in this repo)

python3 -m venv /opt/tc-venv
source /opt/tc-venv/bin/activate
pip install --upgrade pip

4) Install Python ML packages (torch already installed in your venv if you followed earlier steps)

# If torch with cuda is not installed, install matching wheel. Example for torch 2.8.0+cu129:
# pip install --index-url https://download.pytorch.org/whl/cu129 torch==2.8.0+cu129 torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu129

pip install faster-whisper
pip install git+https://github.com/openai/whisper.git@main#egg=whisper
# whisperx is optional and heavier. Install if you plan to use whisperx:
# pip install whisperx

5) (Optional) Mount remote uploads with rclone

# Example using /root/rclone.conf and the remote name 'pcloudtc'
# rclone mount --config /root/rclone.conf pcloudtc:transcript-coach-saas/data/uploads /data/uploads --vfs-cache-mode full --allow-other --daemon

6) Run the smoke test

# Activate venv if not active
source /opt/tc-venv/bin/activate
python3 /root/transcript-coach-saas/ops/gpu_smoke_faster_whisper.py --input /data/uploads/Mike.m4a --output /data/results/gpu-transcript-fasterwhisper.json --model small


Notes:
- If you encounter cuda/torch compatibility issues, install the torch wheel that matches your CUDA driver (on the VM we verified CUDA 12.9, so use the cu129 wheel if available).
- faster-whisper is a lighter test and avoids heavier whisperx dependencies.
- For production, consider building a pre-baked container with all dependencies to avoid slow pip installs on boot.
