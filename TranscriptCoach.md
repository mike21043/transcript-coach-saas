# Transcript Coach — Deep Research Handoff & Status (as of 2025-09-21)

> **Project alias**: Transcript Coach / Listening Coach / TranscriptCoach SaaS  
> **Owner**: Michael (Mike) Silen — *finance exec & builder; prefers precise, step-by-step, “full replacements”, explicit server labels, and short logs.*  
> **Primary goal**: A cloud-based transcription + coaching platform using WhisperX + diarization, with GPU workers and a queue-driven pipeline, UI for uploads & results, and eventually coaching/LLM analysis over persisted transcripts.

---

## 0) TL;DR (What’s working vs. what’s not)

**Working / Established**
- **Two-server architecture**:  
  - **Main** server (API/UI/Redis via docker-compose).  
  - **GPU** server (agent worker via docker-compose.gpu.yml).  
- **Redis job queue** on Main reachable on `0.0.0.0:6379`; GPU agent connects and listens on `transcript_jobs` (confirmed in multiple logs).  
- **API** (FastAPI) exposes **POST `/upload`** that saves files to **`/data/uploads`** on Main and enqueues jobs in Redis.  
- **Agent (GPU)** pulls jobs from Redis, loads WhisperX on CUDA, diarization path planned.  
- **Dockerized services** with separate compose files for Main/GPU.  
- **Consistent step‑by‑step debug cycles** and job injection via `redis-cli lpush` for E2E tests.

**CI & Published Images (new)**
- The agent image is now built and published to GitHub Container Registry (GHCR) with two tags:
  - `ghcr.io/mike21043/transcript-coach-agent:latest`
  - `ghcr.io/mike21043/transcript-coach-agent:cuda129`
- A `publish-images` workflow builds and pushes these images. A `smoke-published` workflow verifies published images:
  - It runs `:latest` with `AGENT_SMOKE=1` against a redis service to confirm the container starts and writes a smoke result file and (optionally) a Redis key.
  - For the heavy CUDA image, the workflow uses a lightweight `docker manifest inspect` check to confirm the `:cuda129` tag exists without pulling large layers on GitHub-hosted runners.


**Primary Blockers**
- **Shared storage gap between Main and GPU**: `/data/uploads` on Main is **not visible** to GPU; agent errors such as “Input file not found: /data/uploads/RealTest.m4a” are due to this.  
  - Intended solution: **pCloud remote** (rclone) as a shared backing store: upload on Main → pCloud → GPU pulls from pCloud.  
  - Current state indicates **copy/sync not yet reliable** (GPU not seeing new files or agent not fetching them).  
- **Package & version conflicts** in WhisperX/Torch/Lightning noted (Torch `2.2.2` target; Lightning auto-upgrading checkpoints; WhisperX pins).  
- **Occasional Redis connectivity issues** (e.g., `Connection refused` on `127.0.0.1:6379` when invoked on wrong host).

**Near-Term Priority**
1. **Fix shared storage pathing** (Main → pCloud → GPU → local `/data/uploads`).  
2. **Lock Python/WhisperX/Torch versions** for reproducible builds.  
3. **Re-run E2E test from UI** (not only Redis push), verify diarization output saved to `/data/results` on Main.  
4. **Update docs + compose files** to reflect working paths & commands; add smoke tests and health endpoints.

---

## 1) Architecture Overview

### 1.1 Components & Roles

- **Main Server** (public IP in logs: **38.242.200.197** — referred to as **Main**)
  - **Runs**: API (FastAPI), UI (React/Next.js or React SPA), Redis.
  - **Docker**: `docker-compose.yml` for API/UI/Redis.
  - **Volumes**: Binds `./data:/data` (host path `/root/transcript-coach-saas/data`), including:
    - `/data/uploads` — incoming audio files.
    - `/data/results` — outputs (JSON, error logs, etc.).
  - **API**: `POST /upload` saves file to `/data/uploads` and enqueues `{"job_id","filename"}` into Redis list `transcript_jobs`.
  - **Status**: Logs show requests pushing jobs; Redis up at `0.0.0.0:6379` and queue used (`llen`, `lrange` outputs).

- **GPU Server** (example logs show instance **25614017**; sometimes “scheduling mode”)
  - **Runs**: Agent Worker (PyTorch/WhisperX/diarization) via `docker-compose.gpu.yml`.
  - **Connects to**: Redis on Main (`redis://38.242.200.197:6379/0`).
  - **Volumes**: Binds `./data:/data` (host path on GPU), meant to be mirrored or synchronized to Main via pCloud.
  - **Controller**: Optional *vast-agent* process that watches queue and manages Vast.ai GPUs (re-using instance when available).

- **Storage / Sync**: **pCloud via rclone**
  - Goal: Single source of truth for `/data/uploads` and `/data/results` across Main and GPU.
  - Observed gap: Files written on Main not yet present on GPU path (e.g., “Input file not found” errors illustrate misalignment).

### 1.2 Data Flow (intended E2E)

1. **User uploads audio** from UI → **Main API `/upload`** → saves file to **`/data/uploads`** (Main).  
2. **API enqueues job** `{"job_id","filename"}` to **Redis list `transcript_jobs`**.  
3. **GPU agent** (subscribed to Redis) receives job → **fetches input audio** (should come from **pCloud mount** or synced mirror) → runs WhisperX (+ diarization).  
4. **Agent writes output** (JSON, SRT, TXT, CSV) to **`/data/results`** on GPU (and/or pCloud).  
5. **Results sync back** to Main `/data/results` (via pCloud/rclone) → UI polls `/result?job_id=...` or equivalent to display/download.

---

## 2) Servers, Containers, Images, Paths

### 2.1 Servers

- **Main** = `38.242.200.197`  
  - **Services**: Redis (6379), API (FastAPI), UI.  
  - **Project root**: `/root/transcript-coach-saas`  
  - **Data**: `/root/transcript-coach-saas/data` (mapped to `/data` in containers)

- **GPU** = example Vast.ai instance (id `25614017`; IPs redacted in logs).  
  - **Service**: `transcript-coach-saas-agent` via `docker-compose.gpu.yml`  
  - **CUDA**: e.g., “CUDA Version 12.2.2” observed.  
  - **Data**: `/root/transcript-coach-saas/data` (mapped to `/data` in containers)

### 2.2 Containers (examples from logs)

- `transcript-coach-saas-api` — FastAPI/uvicorn, exposes `/upload`.  
- `transcript-coach-saas-vast-agent` — Vast controller loop (optional orchestrator).  
- `transcript-coach-saas-agent` — Worker that loads WhisperX/cuda, listens on `transcript_jobs`.  
- Redis — listens on `0.0.0.0:6379` on Main.

### 2.3 Docker Images & Key Packages

- **Agent image**: `listenercoach/transcript-coach-agent:latest`
  - **Torch** target **2.2.2** (logs show this requirement alongside torchaudio 2.2.2).  
  - **WhisperX** + **Pyannote diarization** (planned/partially integrated).  
  - **python-multipart** installed in API Dockerfile to support file uploads.

### 2.4 Paths & Files

- **On Main (host)**
  - `/root/transcript-coach-saas/docker-compose.yml`
  - `/root/transcript-coach-saas/data/uploads/` (incoming audio)
  - `/root/transcript-coach-saas/data/results/` (transcripts, errors) — sample files from Sep 5–7 present.
  - **Known doc**: `TranscriptCoach-Status-20250912.md` (earlier status doc).

- **On GPU (host)**
  - `/root/transcript-coach-saas/docker-compose.gpu.yml`
  - `/root/transcript-coach-saas/data/` (should sync with Main via pCloud)

---

## 3) Current Issues & Error Log Highlights

### 3.1 File visibility / Shared storage
- **Symptoms**: Agent errors like  
  - `ERROR: Input file not found: /data/uploads/RealTest.m4a`  
  - `ValueError: File /data/uploads/Amanda3.m4a.wav does not exist`
- **Cause**: **Main** writes to local `/data/uploads`; **GPU** expects to read from `/data/uploads` too, but **disks aren’t shared**.  
- **Intended fix**: **pCloud (rclone) mount/sync** so both hosts see identical contents.  
- **Action needed**: Confirm rclone remote (e.g., `pcloudtc:`), mount points on both servers, and ensure **write-once** & **atomicity** for agent consumption.

### 3.2 Dependency conflicts
- Logs show:
  - “ERROR: Cannot install -r /app/requirements.txt ... and torch==2.2.2 because these package versions have conflicting dependencies.”
  - Lightning auto-migration messages; WhisperX pinning to match Torch/CUDA.  
- **Action needed**: Pin a **known-good matrix** (CUDA 12.1/12.2, torch/torchaudio 2.2.2, whisperx compatible version) and cache wheels.

### 3.3 Redis connectivity & targeting
- Example: `Could not connect to Redis at 127.0.0.1:6379: Connection refused` when run on wrong host.  
- **Action needed**: Always use **Main** IP when running Redis CLI remotely; label commands by **Main vs GPU**.

### 3.4 Vast.ai controller & GPU lifecycle
- *vast-agent* loop shows **reusing instance** and **stale counter** increments; ensure lifecycle matches queue length and idle shutdown strategy.

---

## 4) Canonical E2E Test (UI-first)

> Purpose: Validate the **actual user flow** (UI → API → Redis → GPU Agent → Results → UI).

### Step A — Verify services (Main)
**Main**
```bash
# API / UI / Redis up
docker compose ps
docker compose logs --tail=20 api
docker compose logs --tail=20 redis  # if named; else use container id
```

### Step B — Upload via UI
**User**
1. Open the UI in browser, upload `RealTest.m4a` (or similar).  
2. Note/record the returned **Job ID**.

### Step C — Confirm enqueue (Main)
**Main**
```bash
redis-cli -h 38.242.200.197 -p 6379 llen transcript_jobs
redis-cli -h 38.242.200.197 -p 6379 lrange transcript_jobs 0 -1
```

### Step D — Ensure GPU sees the file
**GPU**
```bash
# Verify pCloud mount is live and path contains the new file
rclone ls pcloudtc:transcript-coach-saas/data/uploads | grep RealTest.m4a || true

# If using mount:
ls -l /root/transcript-coach-saas/data/uploads | grep RealTest.m4a || true
```

### Step E — Watch Agent logs
**GPU**
```bash
docker ps | grep transcript-coach-saas-agent
docker logs -f transcript-coach-saas-agent --tail=20
```
Expect: “Received job …”, WhisperX loading, and writing outputs.

### Step F — Validate results on Main
**Main**
```bash
ls -l /root/transcript-coach-saas/data/results
tail -n +1 /root/transcript-coach-saas/data/results/*.done.json 2>/dev/null | head -n 200
```

### Step G — UI download/view
**User**
- Refresh job status → verify diarized transcript; download files (JSON/TXT/SRT/CSV).

---

## 5) Queue-Only Smoke Test (bypass UI)

> Useful for quick debugging of the worker & storage.

**Main**
```bash
# Push a test job (ensure the filename exists in pCloud + both hosts /data/uploads)
redis-cli -h 38.242.200.197 -p 6379 lpush transcript_jobs '{{"job_id":"job_test_smoke","filename":"RealTest.m4a"}}'
```

**GPU**
```bash
docker logs -f transcript-coach-saas-agent --tail=20
```

**Main**
```bash
ls -l /root/transcript-coach-saas/data/results
cat /root/transcript-coach-saas/data/results/job_test_smoke.done.json || true
```

---

## 6) Storage Design: pCloud via rclone

### 6.1 Goals
- **Single source of truth** for `/data` (uploads/results).  
- **Atomic ingestion**: API writes → rclone copy to pCloud; GPU reads from pCloud-mounted (or `rclone copy` down) and processes; results copied back.

### 6.2 Suggested pattern
- **On Main** (post-upload hook or background loop):
```bash
# Example: sync uploads up to pCloud
rclone copy /root/transcript-coach-saas/data/uploads pcloudtc:transcript-coach-saas/data/uploads --create-empty-src-dirs
```
- **On GPU** (before processing and/or on a short interval):
```bash
# Pull latest inputs
rclone copy pcloudtc:transcript-coach-saas/data/uploads /root/transcript-coach-saas/data/uploads --create-empty-src-dirs

# After job complete: push results up
rclone copy /root/transcript-coach-saas/data/results pcloudtc:transcript-coach-saas/data/results --create-empty-src-dirs
```
- **On Main** (to read results locally for UI):
```bash
rclone copy pcloudtc:transcript-coach-saas/data/results /root/transcript-coach-saas/data/results --create-empty-src-dirs
```

> You can convert these into **systemd timers** or containerized sidecars to avoid manual steps. For robustness, consider checksum-based transfers and filename staging (`.part` → rename).

---

## 7) Environment, Builds & Reproducibility

### 7.1 Python/WhisperX/Torch Matrix
- Target **CUDA 12.1/12.2** with **torch/torchaudio==2.2.2** (as referenced in logs).  
- Pick WhisperX commit/version compatible with 2.2.2 (verify `pip install whisperx==<ver>` or pin by git SHA).  
- Pin **pytorch-lightning** to a version that does **not** auto-upgrade checkpoints unexpectedly.  
- Lock to a **requirements.txt** with exact pins; use a **constraints.txt** to force transitive deps.  
- Prebuild wheels cache for GPU image to speed rebuilds.

### 7.2 Docker build notes
- Prefer a **single multi-stage Dockerfile** for agent to cache heavy deps.  
- Use **`COMPOSE_BAKE=true`** if bake offers speedups in your environment; verify with small test builds.  
- Add **healthchecks** to agent and api services.

### 7.3 Node/UI
- If UI uses React/Next.js:
  - Include `package.json` & `package-lock.json`/`pnpm-lock.yaml`.  
  - Document `npm ci && npm run build` and `.env` expectations (without committing secrets).

### 7.4 Secrets & Git hygiene
- **Never commit** `.env` files; confirm they’re in `.gitignore`.  
- Provide a sample: `.env.example` and scripts to generate `.env` locally.  
- Clarify which files will **not** be pushed (e.g., `.env`, `/data/**`, build artifacts).

### 7.5 CI + Published Images (notes)

- Workflows live in `.github/workflows/` in the repository. Key workflows added recently:
  - `publish-images.yml` — builds and pushes the agent images to GHCR (tags: `:latest`, `:cuda129`). Uses GitHub Actions build/push steps and relies on repository secrets for GHCR credentials where needed.
  - `smoke-published.yml` — smoke-tests published images:
    - pulls and runs `ghcr.io/${{ github.repository_owner }}/transcript-coach-agent:latest` with `AGENT_SMOKE=1` and a `redis` service; the agent writes `/data/results/smoke_result.json` and (optionally) sets a Redis key `result:smoke` which confirms basic runtime/startup + connectivity.
    - verifies `:cuda129` tag existence via `docker manifest inspect` to avoid pulling large CUDA/Torch layers on GitHub-hosted runners (prevents disk exhaustion during CI).

- AGENT_SMOKE contract (short): when the agent container is started with `AGENT_SMOKE=1`, it should:
  1. Create `/data/results/smoke_result.json` containing a small JSON payload with basic metadata (image tag, time, success).
  2. If `REDIS_URL` is provided, set a Redis key `result:smoke` with the same payload (used by the smoke job to validate connectivity).

These changes were applied to the branch `local-save-20250920-225817` and were used to debug CI behavior (workflow parsing errors and runner disk exhaustion when pulling large CUDA images). The current `smoke-published.yml` uses the manifest-inspect approach for CUDA images.

---

## 8) Git & Onboarding (Windows + VS Code)

1. **Clone repo**
```bash
git clone https://github.com/<YOUR_HANDLE>/transcript-coach-saas.git
cd transcript-coach-saas
```

2. **Environment setup**
   - **Python**: `py -3.11 -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt`
   - **Node**: `npm ci` (from UI folder if separate)
   - **Docker Desktop** (for local dev) or remote attach via SSH.

3. **Untracked files**
```bash
git status
git add -A         # stage all new/modified
git commit -m "feat: add latest changes"
git push origin main
```
- Files ignored by `.gitignore` (e.g., `.env`, `/data/`) **will not be pushed**. Confirm with `git check-ignore -v <path>`.

4. **Run locally (if desired)**
```bash
docker compose up --build -d
docker compose logs --tail=50 api
```

---

## 9) Operational Runbooks

### 9.1 Verify API on Main
**Main**
```bash
docker compose logs --tail=20 api
curl -sS http://localhost:8000/health || true   # if a /health endpoint exists
```

### 9.2 Trigger job via API
**Main (or from UI)**
```bash
# Using curl
curl -F "file=@/path/to/RealTest.m4a" http://<MAIN_HOST>:8000/upload
```

### 9.3 Observe GPU worker
**GPU**
```bash
docker logs -f transcript-coach-saas-agent --tail=20
nvidia-smi
```

### 9.4 Cleanup & Disk Reclaim
**Any host**
```bash
docker system df
docker image prune -a -f
docker builder prune -a -f
docker volume prune -f
docker system prune -a --volumes -f
```
*(Review carefully before removing images/volumes if you rely on them.)*

---

## 10) Conventions & Collaboration Style

- **“One step at a time”** with explicit **Main vs GPU** labels.  
- **Full file replacements** for any file edits.  
- **Short logs** with `--tail=20` unless deeper needed.  
- **Exact copy/paste commands**, explicit paths, no assumptions.  
- When starting a **new chat**, provide a compact **handoff** (see next section).

---

## 11) Suggested Handoff Intro (paste this to kick off a fresh chat)

> **Context**: We’re building **Transcript Coach**, a SaaS that uploads audio on **Main**, enqueues a job in **Redis**, and processes on a **GPU agent** (Vast.ai). **Current blocker**: Shared storage between Main and GPU — files in `/data/uploads` on Main aren’t visible on GPU. We intend to use **pCloud (rclone)** to sync uploads/results.  
> **Servers**: **Main=38.242.200.197** (API/UI/Redis via `docker-compose.yml`), **GPU** (agent via `docker-compose.gpu.yml`, CUDA 12.2.2). Both mount `./data:/data`.  
> **What I need**: One step at a time, **explicit Main vs GPU**, **full replacements**, and **short logs**. First task: Fix storage sync (pCloud) so a UI upload triggers agent processing and results appear back on Main. Then run an **E2E UI test**.  
> **Helpful commands**: (E2E & smoke tests included above).  
> **Dependencies**: We target **torch/torchaudio==2.2.2** and compatible **whisperx**; please provide a pinned requirements/constraints set.

---

## 12) Backlog / Next Steps

1. **Storage sync implementation**
   - Confirm rclone `pcloudtc:` remote on both hosts.
   - Establish **upload → pCloud** and **GPU → pull** flows.
   - Ensure **results → pCloud → Main** sync path.

2. **Agent I/O contract**
   - Agent should **not** assume local file presence; on job receipt, perform a **blocking fetch** from pCloud if file missing.  
   - Write results to local `/data/results` then **push** to pCloud.

3. **Version pin & wheels cache**
   - Provide **requirements.txt** + **constraints.txt** with known-good matrix.  
   - Add a **buildx cache** to speed GPU image builds.

4. **Health & observability**
   - API `/health` and Agent `/metrics` (or logs pattern).  
   - Add **retry** and **dead-letter** behavior for stuck jobs.

5. **UI polish**
   - Clear job status polling, line breaks between speakers, download buttons for JSON/SRT/TXT/CSV.

6. **Diarization & coaching**
  - Integrate Pyannote; persist speaker profiles; store transcripts in DB for future LLM coaching.
  - Output requirements (explicit): when an audio job is processed the agent MUST produce speaker-separated transcripts and speaker embeddings (voiceprints):
    - Speaker-separated transcript files (per job):
      - `<job_id>.speakers.json` — JSON array of speaker segments with timestamps and text. Example schema:
        ```json
        [
          {"speaker": "spk_0", "start": 0.12, "end": 3.45, "text": "Hello, I'm Alice."},
          {"speaker": "spk_1", "start": 3.46, "end": 6.12, "text": "Hi Alice, this is Bob."}
        ]
        ```
      - `<job_id>.speakers.srt` — SRT file with speaker labels for playback.
    - Speaker embeddings (voiceprints):
      - `<job_id>.speaker_embeddings.json` — mapping of speaker id to embedding vector (float array), plus metadata (model, dimension, generation time). Example schema:
        ```json
        {
          "model": "pyannote/embedding",
          "dim": 192,
          "generated_at": "2025-09-21T19:30:00Z",
          "embeddings": {
            "spk_0": [0.00123, -0.0004, ...],
            "spk_1": [0.00211, -0.0017, ...]
          }
        }
        ```
    - Contract notes:
      - Speaker ids must be stable within a job (e.g., `spk_0`, `spk_1`). If cross-job speaker linking is required later, add a canonical speaker UUID to each profile.
      - Store these files in `/data/results/` with the job prefix so UI and downstream LLM coaching components can retrieve them reliably.
      - If embeddings are computed remotely (e.g., HF models), ensure `HF_TOKEN` is available to the agent or provide a secure embedding service.

7. **Autoscaling**
   - Expand *vast-agent* to launch/stop GPUs based on queue depth & staleness.

---

## 13) Appendix — Notable Log Excerpts (paraphrased)

- **Agent**: “Connected to Redis … Listening on queue … Using device: cuda …”  
- **Agent**: “Received job … ERROR: Input file not found: /data/uploads/RealTest.m4a”  
- **API**: Enqueues jobs on upload; **python-multipart** installed to support file form-data.  
- **Redis**: `llen transcript_jobs` and `lrange` show queued items.  
- **Build conflicts**: Torch/WhisperX/Lightning pinning issues.  
- **Vast controller**: Reusing GPU instance, staleness counters.

---

## 14) Glossary

- **Main**: Primary host running API/UI/Redis; path `/root/transcript-coach-saas`.  
- **GPU**: Worker host running WhisperX agent; path `/root/transcript-coach-saas`.  
- **rclone/pCloud**: Cloud storage/sync layer for `/data`.  
- **WhisperX**: ASR with better alignment & diarization hooks.  
- **Redis queue**: `transcript_jobs` list with `{"job_id","filename"}` JSON.  

---

*Prepared for next-chat efficiency. Paste the **Handoff Intro** block to spin up a fresh session fast.*
