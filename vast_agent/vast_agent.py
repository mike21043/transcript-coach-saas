#!/usr/bin/env python3
"""
Packaged controller. This module is the canonical controller for the
`vast_agent` package. Run it with:

    python -m vast_agent.vast_agent

Other modules should import helpers from `vast_agent.vast_utils`.
"""
from __future__ import annotations

# ...existing code...
import os
import time
import uuid
import signal
import logging
from typing import Optional, Dict, Any, List

import requests
import redis
import subprocess
import base64
import tempfile
from pathlib import Path
import argparse
import json

# Import shared helpers from the package
from vast_agent import vast_utils

# Re-exported names from vast_utils for backwards compatibility with the
# original executable style. Code below uses these names (search_offers,
# pick_best_offer, create_instance_from_ask, etc.)
search_offers = vast_utils.search_offers
pick_best_offer = vast_utils.pick_best_offer
get_offer_price = vast_utils.get_offer_price
show_instance = vast_utils.show_instance
list_instances = vast_utils.list_instances
wait_until_running = vast_utils.wait_until_running
fetch_console_and_decode = vast_utils.fetch_console_and_decode
score_offer = vast_utils.score_offer

# The controller historically called wait_until_running(inst_id, timeout_sec=...)
# directly; vast_utils.wait_until_running requires the API key first. Provide a
# thin wrapper with the historic signature for compatibility.
def wait_until_running(inst_id: int, timeout_sec: int = 900) -> bool:
    return vast_utils.wait_until_running(os.getenv('VAST_API_KEY', ''), inst_id, timeout_sec)

# The rest of the controller logic is shared with the original file. To
# avoid duplicating the whole file here (and keep history clear) we import
# the implementation from the old script if present, otherwise we provide
# the canonical implementation inline. For now we will inline the main
# controller implementation so the module is self-contained.

# =========================
# Static config / ENV
# =========================
VAST_BASE = "https://console.vast.ai/api/v0"

# From your .env
VAST_API_KEY       = os.getenv("VAST_API_KEY", "").strip()
VAST_IMAGE_ENV     = os.getenv("VAST_IMAGE")
GITHUB_OWNER       = os.getenv("GITHUB_OWNER") or os.getenv("IMAGE_REPO_OWNER") or os.getenv("IMAGE_REPO")
IMAGE_REPO         = os.getenv("IMAGE_REPO", "transcript-coach-agent")

if VAST_IMAGE_ENV:
    IMAGE = VAST_IMAGE_ENV
else:
    owner = GITHUB_OWNER or "mikesilen"
    image_name = IMAGE_REPO
    IMAGE = f"ghcr.io/{owner}/{image_name}:cuda129"

REDIS_URL          = os.getenv("PUBLIC_REDIS_URL", "redis://38.242.200.197:6379/0")
QUEUE_NAME         = os.getenv("QUEUE_NAME", "transcript_jobs")

PROVISION_MODE_ENV = os.getenv("PROVISION_MODE", "").strip() or 'image'
INSTANCE_DISK_GB   = int(os.getenv("INSTANCE_DISK_GB", "32"))
INSTANCE_LABEL     = os.getenv("INSTANCE_LABEL", "TranscriptCoach GPU")

MIN_GPUS           = int(os.getenv("MIN_GPUS", "1"))
ONLY_VERIFIED      = os.getenv("ONLY_VERIFIED", "true").lower() == "true"
VAST_ALLOWED_COUNTRIES = [c.strip().lower() for c in os.getenv("VAST_ALLOWED_COUNTRIES", "US").split(",") if c.strip()]
VAST_EXCLUDE_COUNTRIES = [c.strip().lower() for c in os.getenv("VAST_EXCLUDE_COUNTRIES", "CN,CHN,PRC,CHINA").split(",") if c.strip()]
_price_env = os.getenv('priceInstanceHourlyMax') or os.getenv('PRICE_INSTANCE_HOURLY_MAX') or os.getenv('VAST_PRICE_INSTANCE_HOURLY_MAX')
if not _price_env:
    _price_env = '0.30'
try:
    PRICE_INSTANCE_HOURLY_MAX = float(_price_env)
except Exception:
    PRICE_INSTANCE_HOURLY_MAX = None
LOOP_INTERVAL      = int(os.getenv("LOOP_INTERVAL", "20"))

DEFAULT_IDLE_TIMEOUT = int(os.getenv("DEFAULT_IDLE_TIMEOUT", "1800"))
DEFAULT_STALE_TICKS  = int(os.getenv("DEFAULT_STALE_TICKS", "15"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [vast_agent] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vast_agent")

# The controller reuses vast_utils for many functions (search, price, console fetch).

def _head_manifest(image: str, timeout: int = 10) -> bool:
    try:
        if ':' in image and '/' in image:
            img, tag = image.rsplit(':', 1)
        else:
            img = image
            tag = 'latest'
        if '/' not in img:
            return True
        parts = img.split('/', 1)
        registry = parts[0] if ('.' in parts[0] or ':' in parts[0]) else 'registry-1.docker.io'
        repo = parts[1] if len(parts) > 1 else parts[0]
        url = f"https://{registry}/v2/{repo}/manifests/{tag}"
        headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
        resp = requests.head(url, headers=headers, timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False

# (Keep the rest of the controller code as in the original script.)

def create_instance_from_ask(ask_id: int, label_suffix: str, offer: Optional[dict] = None) -> int:
    # Reuse most of the logic from the original file but call vast_utils where appropriate.
    label = f"{INSTANCE_LABEL}-{label_suffix}"
    body: Dict[str, Any] = {
        "disk": INSTANCE_DISK_GB,
        "label": label,
        "env": {
            "REDIS_URL": REDIS_URL,
            "QUEUE_NAME": QUEUE_NAME,
        },
        "target_state": "running",
    }

    selected_image = IMAGE

    provision_runner_flag = os.getenv("VAST_PROVISION_RUNNER", "false").lower() in ("1", "true", "yes")

    if provision_runner_flag:
        tmp_out = None
        try:
            repo_root = Path(__file__).resolve().parents[1]
            script_path = repo_root / "scripts" / "provision_runner.sh"
            owner = GITHUB_OWNER or "mike21043"
            repo = IMAGE_REPO or "transcript-coach-saas"
            tf = tempfile.NamedTemporaryFile(prefix=f"runner-user-data-{label_suffix}-", suffix=".sh", delete=False)
            tmp_out = tf.name
            tf.close()
            cmd = ["/bin/bash", str(script_path), owner, repo, tmp_out]
            log.info(f"Provisioning runner user-data via: {' '.join(cmd)} (cwd={repo_root})")
            subprocess.run(cmd, check=True, cwd=str(repo_root))
            with open(tmp_out, 'rb') as f:
                ud = f.read()
            ud_url = os.getenv('RUNNER_USER_DATA_URL')
            body.setdefault('env', {})
            if ud_url:
                body['env']['RUNNER_USER_DATA_URL'] = ud_url
                log.info('Using RUNNER_USER_DATA_URL from environment instead of embedding full user-data')
            else:
                max_size = 1_000_000
                if len(ud) > max_size:
                    log.warning(f"Runner user-data is large ({len(ud)} bytes) — skipping embedding; consider delivering via cloud-init or artifact.")
                else:
                    ud_b64 = base64.b64encode(ud).decode('ascii')
                    body['env']['RUNNER_USER_DATA_B64'] = ud_b64
                labels = os.getenv("VAST_RUNNER_LABELS", "self-hosted,cuda-test,transcript-coach")
                body['env']['RUNNER_LABELS'] = labels
                log.info("Embedded runner user-data into instance env (base64) and set RUNNER_LABELS")
        except subprocess.CalledProcessError as e:
            log.warning(f"Provision helper failed (exit {e.returncode}): {e}")
        except Exception as e:
            log.warning(f"Failed to provision runner user-data: {e}")
        finally:
            try:
                if tmp_out and Path(tmp_out).exists():
                    Path(tmp_out).unlink()
            except Exception:
                pass

    IMAGE_MAP_RAW = os.getenv("IMAGE_MAP", "")
    if IMAGE_MAP_RAW and offer:
        try:
            m = dict(x.split("=", 1) for x in IMAGE_MAP_RAW.split(",") if "=" in x)
            offer_text = str(offer)
            for k, v in m.items():
                if k.strip() and k.strip().lower() in offer_text.lower():
                    selected_image = v.strip()
                    log.info(f"Selecting image from IMAGE_MAP: key={k} -> {selected_image}")
                    break
        except Exception as e:
            log.warning(f"Failed to parse IMAGE_MAP: {e}")

    fallback_img = os.getenv("VAST_FALLBACK_IMAGE", "ubuntu:22.04")
    try:
        if selected_image and ('ghcr.io' in selected_image or ('/' in selected_image and '.' in selected_image.split('/')[0])):
            ok = _head_manifest(selected_image)
            if not ok:
                log.warning(f"Image {selected_image} not publicly accessible; falling back to {fallback_img}")
                selected_image = fallback_img
    except Exception as e:
        log.warning(f"Image accessibility check failed: {e}; proceeding with requested image")

    # Only 'image' provisioning is supported now. Prefer explicit env override
    # or the GHCR-derived default image.
    mode = (PROVISION_MODE_ENV or os.getenv('PROVISION_MODE', '')).strip().lower() or 'image'
    if selected_image:
        body['image'] = selected_image

    try:
        res = vast_utils.vast_req("PUT", f"/asks/{ask_id}/", os.getenv('VAST_API_KEY', ''), body)
    except Exception as e:
        msg = str(e)
        log.error(f"Create attempt failed: {msg}")
        raise

    newc = res.get("new_contract")
    if isinstance(newc, dict):
        inst_id = newc.get("instance_id")
    elif isinstance(newc, int):
        inst_id = newc
    else:
        inst_id = None

    if inst_id is None:
        all_inst = list_instances()
        newest = sorted(all_inst, key=lambda i: i.get("id", 0), reverse=True)
        return int(newest[0]["id"]) if newest else -1
    return int(inst_id)


def controller_loop():
    # Minimal wrapper that reuses vast_utils.search_offers, pick_best_offer, etc.
    log.info("Starting Vast.ai controller loop (packaged)...")
    # Debugging: log the effective Redis and Queue configuration so we can
    # verify the controller is connected to the intended Redis instance.
    try:
        log.info(f"Effective REDIS_URL={os.getenv('REDIS_URL')} PUBLIC_REDIS_URL={os.getenv('PUBLIC_REDIS_URL')} QUEUE_NAME={os.getenv('QUEUE_NAME', 'transcript_jobs')}")
    except Exception:
        pass
    r = redis.from_url(REDIS_URL)
    active_instance: Optional[int] = None
    last_qlen = 0
    stale_counter = 0
    idle_elapsed = 0

    def _stop(sig, frm):
        log.info("Stopping controller...")
        raise SystemExit

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while True:
        try:
            qlen = int(r.llen(QUEUE_NAME))
            idle_timeout, stale_ticks = DEFAULT_IDLE_TIMEOUT, DEFAULT_STALE_TICKS
            log.info(f"Queue length: {qlen} | Idle timeout={idle_timeout}s | Stale ticks={stale_ticks}")

            if qlen > 0 and active_instance is None:
                offers = vast_utils.search_offers(os.getenv('VAST_API_KEY', ''), min_gpus=MIN_GPUS, only_verified=ONLY_VERIFIED)
                best = vast_utils.pick_best_offer(offers)
                if not best:
                    log.error("No suitable offers found — will retry next tick.")
                else:
                    ask_id = int(best["id"])
                    suffix = uuid.uuid4().hex[:6]
                    try:
                        active_instance = create_instance_from_ask(ask_id, suffix)
                        if active_instance <= 0:
                            log.error("Failed to get instance id after create; will retry.")
                            active_instance = None
                        else:
                            if not wait_until_running(active_instance, timeout_sec=900):
                                log.error("Instance failed to become ready; destroying.")
                                vast_utils.vast_req('DELETE', f'/instances/{active_instance}/', os.getenv('VAST_API_KEY', ''))
                                active_instance = None
                            else:
                                log.info(f"Instance {active_instance} is running.")
                                stale_counter = 0
                                idle_elapsed = 0
                                last_qlen = qlen
                    except Exception as e:
                        log.error(f"Create instance failed: {e}")
                        active_instance = None

            if qlen > 0 and active_instance is not None:
                idle_elapsed = 0
                if qlen == last_qlen:
                    stale_counter += 1
                    log.info(f"DEBUG: Instance {active_instance} appears stale: {stale_counter}/{DEFAULT_STALE_TICKS}")
                    if stale_counter >= DEFAULT_STALE_TICKS:
                        log.info(f"Forcing fresh instance after ~{DEFAULT_STALE_TICKS * LOOP_INTERVAL}s with no progress...")
                        vast_utils.vast_req('DELETE', f'/instances/{active_instance}/', os.getenv('VAST_API_KEY', ''))
                        active_instance = None
                        stale_counter = 0
                else:
                    stale_counter = 0
                last_qlen = qlen

            if qlen == 0:
                last_qlen = 0
                stale_counter = 0
                if active_instance is not None:
                    idle_elapsed += LOOP_INTERVAL
                    log.info(f"Idle timer: {idle_elapsed}s / {DEFAULT_IDLE_TIMEOUT}s")
                    if idle_elapsed >= DEFAULT_IDLE_TIMEOUT:
                        log.info(f"Idle for {DEFAULT_IDLE_TIMEOUT}s → destroying instance {active_instance}")
                        vast_utils.vast_req('DELETE', f'/instances/{active_instance}/', os.getenv('VAST_API_KEY', ''))
                        active_instance = None
                        idle_elapsed = 0

        except Exception as e:
            log.error(f"ERROR in controller loop: {e}")

        time.sleep(LOOP_INTERVAL)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for programmatic invocation and CLI.

    If called with argv (list of args, excluding program name) it will parse
    and run; returns 0 on success or non-zero on error.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--provision-mode", choices=["image"], help="Provision mode for instance creation (overrides PROVISION_MODE env). Only 'image' is supported")
    parser.add_argument("--dry-print-create-body", action="store_true", help="Build and print a sample create body then exit (no API calls)")
    parser.add_argument("--ask-id", type=int, default=1, help="Ask ID to use when building sample create body")
    parser.add_argument("--label-suffix", type=str, default="dryrun", help="Label suffix for sample create body")
    parser.add_argument("--image", type=str, default=None, help="Optional image override for sample create body")
    args = parser.parse_args(argv)

    try:
        if args.provision_mode:
            global PROVISION_MODE_ENV
            PROVISION_MODE_ENV = args.provision_mode

        if args.dry_print_create_body:
            # This controller is image-only. Build a create body for image mode.
            body = vast_utils.build_create_body_for_mode(args.ask_id, args.label_suffix, None, 'image', ud_bytes=b"", image_arg=args.image)
            print(json.dumps(body, indent=2))
        else:
            controller_loop()
        return 0
    except SystemExit:
        raise
    except Exception as e:
        log.exception('Unhandled error in vast_agent main: %s', e)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
