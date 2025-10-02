<!--
This file is a living catalog of scripts in the repository. It classifies each script as
 - ops: operational helpers for provisioning, bootstrapping, or platform integration
 - diagnostic: one-off inspection or smoke tools to check environment or provider
 - shared: utilities intended to be imported by other code (preferred over copy/paste)

Recommended workflow: keep the controller (`vast_agent.vast_agent`) as the canonical provisioning implementation (run with `python -m vast_agent.vast_agent`).
Use scripts here for diagnostics/ops and consolidate shared logic into `vast_agent/vast_utils.py` when practical.
-->

# Scripts catalog

| Path | Purpose (one-line) | Classification | Keep / Consolidate / Deprecate | Notes / Recommended action |
|------|---------------------|----------------|-------------------------------|---------------------------|
| `scripts/e2e_provision_and_test.py` | E2E harness: create ask, wait for runner, dispatch GH workflow | ops | Keep (ops harness) | Useful for full integration tests; keep as wrapper that uses canonical controller or dry-print mode. |
| `scripts/run_e2e.sh` | Safe wrapper that sources env and runs the e2e harness | ops | Keep | Keep as a convenience wrapper; it enforces dry-run unless FORCE=1. |
| `scripts/provision_runner.sh` | Request GitHub registration token and render cloud-init user-data | ops | Keep | Ops helper; does not duplicate runtime code. |
| `scripts/poll_runner_and_dispatch.py` | Poll GH for runner going online and dispatch a workflow run | ops | Keep | Useful orchestration after provisioning; keep. |
| `scripts/enqueue_to_master.py` | Quick script to push a job to a remote Redis master (for testing) | diagnostic | Keep (move to smoke/) | Keep as a smoke test helper; consider moving to `scripts/smoke/` and referencing in `DEVELOPER.md`. |
| `scripts/vast_inspect_offers.py` | Offer discovery and filtering helper for Vast.ai (price/CUDA/country) | diagnostic/shared | Consolidate | Duplicate logic used by controller; extract helpers into `vast_agent/vast_utils.py` and import from both. |
| `scripts/inspect_offers_na.py` | Variant of offer inspection with North America preferences | diagnostic/shared | Consolidate | Merge into `vast_inspect_offers.py` or share helpers in `vast_utils.py`. |
| `scripts/vast_probe.py` | Simple Vast API probe (token presence, endpoint snippets) | diagnostic | Keep | Lightweight probe; keep as ops diagnostic. |
| `scripts/whisperx_pyannote_smoke.py` | Environment smoke test: torch, whisperx, pyannote basic loading | diagnostic | Keep (smoke) | Keep as a node-local runtime smoke test; consider running in CI for runner images. |
| `scripts/inspect_offers_na.py` | (duplicate entry) NA-focused offer inspector | diagnostic/shared | Consolidate | See `vast_inspect_offers.py` row — consolidate. |
| `scripts/smoke_acceptance.py` | API contract smoke test (health + upload + enqueue) | diagnostic | Keep (test) | Keep and run in CI as a pre-deploy smoke test. |

## How to use this catalog

- Before modifying or deleting a script, update this file and `DEVELOPER.md` with the reason.
 - When consolidating offer-inspection logic, prefer a single `vast_agent/vast_utils.py` module and update scripts and the controller to import from it.
- Move purely diagnostic smoke checks under `scripts/smoke/` to make their role explicit.
