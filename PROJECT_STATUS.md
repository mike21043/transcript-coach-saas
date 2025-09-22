Project: transcript-coach-saas
Branch: local-save-20250920-225817
Date: 2025-09-22

Summary of recent work
----------------------
- Implemented an end-to-end provisioning harness for ephemeral self-hosted GitHub Actions runners on Vast.ai.
- Added client-side enforcement of region and price caps to avoid selecting expensive or foreign hosts.
- Wired default budget cap to $0.30/hr (env aliases supported: priceInstanceHourlyMax, PRICE_INSTANCE_HOURLY_MAX, VAST_PRICE_INSTANCE_HOURLY_MAX).
- Implemented progressive fallback policy (price stepping + limited country expansion) to safely widen selection when strict filters find nothing.
- Hardened the instance bootstrap (`ops/runner-cloud-init.sh`) to write logs to `/var/log/runner-bootstrap.log` and fail loudly on runner config errors.
- Adjusted scoring to prefer lower price, exact GPU-count match, then perf as a tiebreaker.

Files changed (high level)
- scripts/e2e_provision_and_test.py: added progressive fallback, price/gpu/perf scoring, strict filtering enforcement
- scripts/inspect_offers_na.py: score changes and strict filter behavior
- vast-agent/vast_agent.py: search_offers() improved with progressive fallback and tuple-based scoring
- ops/runner-cloud-init.sh: added persistent logging and removed masking of config failures

How to run the E2E harness (recommended safe flow)
1. Ensure the environment contains the required secrets:
   - VAST_API_KEY
   - VAST_TEMPLATE_HASH
   - GITHUB_PAT
   - Optionally: VAST_NUM_GPUS, VAST_MIN_CUDA, VAST_ALLOWED_COUNTRIES, priceInstanceHourlyMax

2. Dry-run to validate user-data generation:

```bash
python3 scripts/e2e_provision_and_test.py --dry --image ubuntu:22.04
```

3. Full run (will abort if no safe offers found):

```bash
export VAST_API_KEY=... VAST_TEMPLATE_HASH=... GITHUB_PAT=... VAST_NUM_GPUS=1
python3 scripts/e2e_provision_and_test.py --image ubuntu:22.04
```

4. If the harness creates an instance but the runner does not register, fetch logs from the instance (the cloud-init writes to `/var/log/runner-bootstrap.log`) and provide them for debugging.

Progress & status
-----------------
- The progressive fallback and cloud-init hardening are implemented and saved.
- The harness now refuses to fall back to the global offer pool silently; it will try controlled widenings and then abort if nothing acceptable is found.

Remaining work (high priority candidates)
- Attempt server-side Vast API filters (machineCountries, price_hour_usd): blocked by API 400 responses; requires vendor documentation or experiments to determine the exact accepted query syntax.
- Run a live provisioning test and validate the runner registration flow. Capture bootstrap logs if failure occurs.
- Add a small automated log retrieval or remote-upload step in cloud-init (e.g., upload /var/log/runner-bootstrap.log to your rclone target or S3) so logs can be fetched after instance creation even if instance is destroyed.
- Add unit tests for selection/scoring logic.

Notes
-----
- All edits were made on branch: local-save-20250920-225817. Commit history contains the recent changes.
- If you want an automatic widening policy that is more aggressive (e.g., escalate price to a higher cap or include more regions), update the env vars:
  - PRICE_STEP
  - PRICE_MAX_FALLBACK
  - VAST_ALLOWED_COUNTRIES_FALLBACK


