Transcript Coach SaaS Hybrid v1 Production Scaffold
---------------------------------------------------

1) Upload the ZIP to your Cloud VPS 10 and unzip
2) Install Docker & Docker Compose: sudo apt update && sudo apt install -y docker.io docker-compose
3) Copy .env.example to .env and fill in your OpenAI, Vast.ai, and Docker Hub keys
4) Build & start services:
   docker compose build
   docker compose up -d
5) API: http://YOUR_VPS_IP:8000
   Web UI: http://YOUR_VPS_IP:8501
6) Build GPU worker Docker image, push to Docker Hub, set WORKER_IMAGE in .env
7) Start Vast.ai agent if using autoscaling:
   docker compose -f docker-compose.vast-agent.yml up -d
8) Upload audio, monitor logs:
   docker compose logs -f api

Notes:
- Replace placeholder worker & agent code before production.
- Admin dashboard & hybrid rules editor ready; preloaded rules included.
