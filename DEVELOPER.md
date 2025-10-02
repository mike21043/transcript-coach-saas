This project is a queue-driven transcription pipeline: a web API accepts audio uploads, enqueues jobs in Redis, a GPU worker (agent) on Vast.ai is provisioned and consumes jobs, the GPU runs WhisperX + Pyannote diarization/embeddings, writes structured results back to the main server for analysis and interactions using an LLM.

Overview
- API: `api/app.py` (FastAPI) — accepts uploads (`/upload`), enqueues jobs to Redis (`transcript_jobs`), and serves results at `/transcript/{job_id}` and `/history`.
- Agent: `agent/agent.py` — a GPU consumer that BLPOP's `transcript_jobs`, runs WhisperX and pyannote to transcribe with diarization and voiceprint, writes JSON results to `DATA_DIR` and persists with `rclone` when configured.
- Controller: `vast_agent.vast_agent` (run with `python -m vast_agent.vast_agent`) — provisions Vast.ai instances (bootstrap/template/image) when queue activity requires GPUs and destroys them when idle/stale.

Scripts catalog: see `scripts/CATALOG.md` for a classified list of repository scripts (ops / diagnostic / shared) and recommended actions.

Shared Vast helpers
- Many provisioning and diagnostics scripts previously duplicated Vast API logic (search/filter/price parsing, console fetch, decoding). Those helpers are now consolidated in `vast_agent/vast_utils.py`.
- Scripts that use the shared helpers:
  - `scripts/vast_inspect_offers.py` — now uses `vast_agent.vast_utils.search_offers` and `get_offer_price` for offer inspection.
  - `scripts/fetch_vast_console.py` — lightweight wrapper that calls `vast_agent.vast_utils.fetch_console_and_decode` to save provider console output and decode embedded bootstrap logs.
  - `scripts/e2e_provision_and_test.py` still contains higher-level harness flow (GitHub token generation, user-data rendering, workflow dispatch). It should prefer importing helpers from `vast_agent.vast_utils` if further consolidation is desired.

Recommended next steps:
- Use `vast_agent.vast_utils` as the canonical implementation for provider interactions. Remove duplicate implementations in scripts or import these helpers when adding new ops tools.
- Keep controller logic in `vast_agent.vast_agent` (packaged module) as the runtime orchestration; it reuses the same helper ideas and imports `vast_agent.vast_utils` as the canonical provider helpers.

Canonical contract (short)
- Input: audio file uploaded to `/upload` → server saves under `DATA_DIR/uploads` and pushes job JSON to Redis list `QUEUE_NAME`.
- Worker behavior: BLPOP job JSON → download/convert audio → transcribe → (optional) diarize and embed → save result JSON to `DATA_DIR` and optionally call `/ingest`.
- Persistence: results are kept locally under `DATA_DIR/results` and synchronized to remote via `rclone` if `RCLONE_CONF_B64` / related env are configured.

Important environment variables (developer-focused)
- REDIS_URL: Redis connection string (e.g., redis://redis:6379/0)
- QUEUE_NAME: Redis list name (default `transcript_jobs`)
- DATA_DIR: where uploads and results are stored (default `/data`)
- VAST_API_KEY: Vast.ai API key (for controller/harness)
- VAST_TEMPLATE_HASH: Template hash used when `provision-mode=template`
- VAST_IMAGE: Explicit provider image (used when `provision-mode=image`)
- PROVISION_MODE: One of `bootstrap` (default), `template`, `image` — controls how instances are requested.
- DOCKER_CONFIG_B64 / GITHUB_PAT: used by bootstrap user-data to pull private images when needed.
- HF_TOKEN: HuggingFace token required for Pyannote diarization and embeddings.

Provisioning modes
- bootstrap (default): send runner cloud-init / user-data in `RUNNER_USER_DATA_B64` so the instance self-bootstraps (recommended; works across providers and private images via DOCKER credentials embedded into user-data).
- template: request an instance using a `template_hash` on the provider side. Useful when you control provider templates that pre-install dependencies.
- image: request a provider image explicitly via the `image` field. Use when you know the host can run your image directly.

Quick developer commands
- Dry-print a create body for inspection (no provider calls):
  python -m vast_agent.vast_agent --dry-print-create-body --provision-mode bootstrap
- Run the controller locally (reads env):
  python -m vast_agent.vast_agent
- Run the API server (dev):
  uvicorn api.app:app --reload --host 0.0.0.0 --port 8000
- Run the smoke acceptance test:
  python scripts/smoke_acceptance.py

Notes and troubleshooting
- Use the dry-print option to inspect the exact instance create payload before making real API calls.
- If you rely on private GHCR images, prefer `bootstrap` with `DOCKER_CONFIG_B64` embedded into the user-data so instances can pull private images after boot.
- If pyannote features are missing, ensure `HF_TOKEN` is set in env; the agent disables diarization without it.

Contributing
- Keep this file as the single authoritative developer guide. Replace or update other README/MD files by pointing back to this document.
