#!/usr/bin/env python3
# Vast-agent: Fresh GPU provisioning via Vast Template + Redis-configurable idle/stale logic
# Behavior:
# - Watches Redis queue "transcript_jobs"
# - If jobs arrive and no instance is active: search offers => accept best => create instance with TEMPLATE_HASH + IMAGE
# - Your Vast Template's OnStart launches the agent that connects to Redis and processes jobs
# - If queue is idle past IDLE_TIMEOUT => DESTROY instance
# - If queue length is stale (no progress) for STALE_TICKS * LOOP_INTERVAL => DESTROY and relaunch a fresh instance

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

# =========================
# Static config / ENV
# =========================
VAST_BASE = "https://console.vast.ai/api/v0"

# From your .env
VAST_API_KEY       = os.getenv("VAST_API_KEY", "").strip()
TEMPLATE_HASH      = os.getenv("VAST_TEMPLATE_HASH", "").strip()
# Allow explicit override via VAST_IMAGE. If not provided, build a GHCR path from GITHUB_OWNER/IMAGE_REPO
VAST_IMAGE_ENV     = os.getenv("VAST_IMAGE")
GITHUB_OWNER       = os.getenv("GITHUB_OWNER") or os.getenv("IMAGE_REPO_OWNER") or os.getenv("IMAGE_REPO")
IMAGE_REPO         = os.getenv("IMAGE_REPO", "transcript-coach-agent")

# Default image selection logic:
# - If VAST_IMAGE env is set, use it verbatim
# - Else if GITHUB_OWNER (or IMAGE_REPO_OWNER) is set, derive ghcr.io/<owner>/<repo>:cuda129
# - Else fall back to a hard-coded ghcr path used historically
if VAST_IMAGE_ENV:
    IMAGE = VAST_IMAGE_ENV
else:
    owner = GITHUB_OWNER or "mikesilen"
    image_name = IMAGE_REPO
    # prefer cuda129 variant for modern CUDA hosts
    IMAGE = f"ghcr.io/{owner}/{image_name}:cuda129"

REDIS_URL          = os.getenv("PUBLIC_REDIS_URL", "redis://38.242.200.197:6379/0")
QUEUE_NAME         = os.getenv("QUEUE_NAME", "transcript_jobs")

INSTANCE_DISK_GB   = int(os.getenv("INSTANCE_DISK_GB", "32"))
INSTANCE_LABEL     = os.getenv("INSTANCE_LABEL", "TranscriptCoach GPU")

MIN_GPUS           = int(os.getenv("MIN_GPUS", "1"))
ONLY_VERIFIED      = os.getenv("ONLY_VERIFIED", "true").lower() == "true"
# Country filtering: comma-separated ISO or name fragments. Default to US and CA (North America)
VAST_ALLOWED_COUNTRIES = [c.strip().lower() for c in os.getenv("VAST_ALLOWED_COUNTRIES", "US").split(",") if c.strip()]
VAST_EXCLUDE_COUNTRIES = [c.strip().lower() for c in os.getenv("VAST_EXCLUDE_COUNTRIES", "CN,CHN,PRC,CHINA").split(",") if c.strip()]
_price_env = os.getenv('priceInstanceHourlyMax') or os.getenv('PRICE_INSTANCE_HOURLY_MAX') or os.getenv('VAST_PRICE_INSTANCE_HOURLY_MAX')
# Default budget cap is $0.30/hr unless overridden
if not _price_env:
    _price_env = '0.30'
try:
    PRICE_INSTANCE_HOURLY_MAX = float(_price_env)
except Exception:
    PRICE_INSTANCE_HOURLY_MAX = None

# Loop cadence: 20s to match your old controller
LOOP_INTERVAL      = int(os.getenv("LOOP_INTERVAL", "20"))

# Defaults (used if no override in Redis)
DEFAULT_IDLE_TIMEOUT = int(os.getenv("DEFAULT_IDLE_TIMEOUT", "1800"))  # 30 min
DEFAULT_STALE_TICKS  = int(os.getenv("DEFAULT_STALE_TICKS", "15"))     # 15 * 20s ≈ 5 min

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [vast-agent] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vast-agent")

# =========================
# HTTP Helpers
# =========================
def _headers() -> Dict[str, str]:
    if not VAST_API_KEY:
        raise RuntimeError("VAST_API_KEY not set")
    return {
        "Authorization": f"Bearer {VAST_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def _req(method: str, path: str, body: Optional[dict] = None) -> dict:
    url = f"{VAST_BASE}{path}"
    r = requests.request(method, url, headers=_headers(), json=body, timeout=60)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"raw": r.text, "status": r.status_code}


def _head_manifest(image: str, timeout: int = 10) -> bool:
    """Return True if the image manifest is accessible anonymously (200)."""
    try:
        # Expect image in form [registry/]repo/name:tag
        if ':' in image and '/' in image:
            # split tag
            img, tag = image.rsplit(':', 1)
        else:
            img = image
            tag = 'latest'

        if '/' not in img:
            # docker hub shorthand (library/...) — don't try here
            return True

        # registry is first component if it contains a dot
        parts = img.split('/', 1)
        registry = parts[0] if ('.' in parts[0] or ':' in parts[0]) else 'registry-1.docker.io'
        repo = parts[1] if len(parts) > 1 else parts[0]

        url = f"https://{registry}/v2/{repo}/manifests/{tag}"
        headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
        resp = requests.head(url, headers=headers, timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False

# =========================
# Vast API
# =========================
def search_offers() -> List[dict]:
    q: Dict[str, Any] = {
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "num_gpus": {"gte": MIN_GPUS},
    }
    if ONLY_VERIFIED:
        q["verified"] = {"eq": True}
    # include machineCountries if provided (Vast API accepts country filters)
    allowed_raw = [c.strip() for c in os.getenv('VAST_ALLOWED_COUNTRIES', 'US,CA').split(',') if c.strip()]
    # Note: machineCountries/machine_countries filters can be rejected by the API; we'll filter client-side below
    # apply budget cap if configured
    # We'll apply any price cap client-side to avoid API query rejections

    res = _req("PUT", "/search/asks/", {"q": q})
    offers = res.get("offers", []) or res.get("matches", []) or []

    # Filter offers by allowed/excluded countries if present
    def country_ok(o: dict) -> bool:
        mc = str(o.get("machine_country") or o.get("country") or "").lower()
        if not mc:
            return True
        # exclude explicit excludes first
        for ex in VAST_EXCLUDE_COUNTRIES:
            if ex and ex in mc:
                return False
        if VAST_ALLOWED_COUNTRIES:
            for allow in VAST_ALLOWED_COUNTRIES:
                if allow and allow in mc:
                    return True
            return False
        return True

    filtered = [o for o in offers if country_ok(o)]
    # Apply client-side price cap if configured
    try:
        if PRICE_INSTANCE_HOURLY_MAX is not None:
            def price_ok(o: dict) -> bool:
                # try several fields that may contain hourly pricing
                for k in ('price_hour_usd', 'price', 'price_usd'):
                    if k in o and o.get(k) is not None:
                        try:
                            return float(o.get(k)) <= float(PRICE_INSTANCE_HOURLY_MAX)
                        except Exception:
                            continue
                # fallback to nested search.totalHour or dph_total
                try:
                    s = o.get('search') or {}
                    if s and s.get('totalHour') is not None:
                        return float(s.get('totalHour')) <= float(PRICE_INSTANCE_HOURLY_MAX)
                except Exception:
                    pass
                try:
                    if o.get('dph_total') is not None:
                        return float(o.get('dph_total')) <= float(PRICE_INSTANCE_HOURLY_MAX)
                except Exception:
                    pass
                # if price couldn't be determined, be conservative and reject
                return False

            filtered = [o for o in filtered if price_ok(o)]
    except Exception:
        log.warning('Price filtering failed; proceeding without price cap')
    if not filtered:
        log.info(f"No offers matched strict VAST_ALLOWED_COUNTRIES={VAST_ALLOWED_COUNTRIES} and PRICE_INSTANCE_HOURLY_MAX={PRICE_INSTANCE_HOURLY_MAX}; attempting progressive fallback")

        # Progressive fallback config
        PRICE_STEP = float(os.getenv('PRICE_STEP', '0.10'))
        PRICE_MAX_FALLBACK = float(os.getenv('PRICE_MAX_FALLBACK', str(PRICE_INSTANCE_HOURLY_MAX + 0.40 if PRICE_INSTANCE_HOURLY_MAX else 0.50)))
        FALLBACK_COUNTRIES = [c.strip().lower() for c in os.getenv('VAST_ALLOWED_COUNTRIES_FALLBACK', 'CA').split(',') if c.strip()]

        def price_ok_dynamic(o, cap):
            for k in ('price_hour_usd', 'price', 'price_usd'):
                if k in o and o.get(k) is not None:
                    try:
                        return float(o.get(k)) <= cap
                    except Exception:
                        continue
            try:
                s = o.get('search') or {}
                if s and s.get('totalHour') is not None:
                    return float(s.get('totalHour')) <= cap
            except Exception:
                pass
            try:
                if o.get('dph_total') is not None:
                    return float(o.get('dph_total')) <= cap
            except Exception:
                pass
            return False

        # step price up incrementally
        current = PRICE_INSTANCE_HOURLY_MAX or 0.30
        while current < PRICE_MAX_FALLBACK:
            current = round(current + PRICE_STEP, 2)
            cand = [o for o in offers if country_ok(o) and price_ok_dynamic(o, current)]
            if cand:
                log.info(f"Found {len(cand)} offers by increasing price cap to ${current}/hr")
                return cand

        # expand allowed countries slightly
        if FALLBACK_COUNTRIES:
            expanded_allowed = VAST_ALLOWED_COUNTRIES + FALLBACK_COUNTRIES
            def country_ok_wide(o: dict) -> bool:
                mc = str(o.get("machine_country") or o.get("country") or "").lower()
                if not mc:
                    return False
                for ex in VAST_EXCLUDE_COUNTRIES:
                    if ex and ex in mc:
                        return False
                return any(a in mc for a in expanded_allowed)
            cand = [o for o in offers if country_ok_wide(o) and (PRICE_INSTANCE_HOURLY_MAX is None or price_ok_dynamic(o, PRICE_INSTANCE_HOURLY_MAX))]
            if cand:
                log.info(f"Found {len(cand)} offers by expanding allowed countries to: {expanded_allowed}")
                return cand

        log.warning('Progressive fallback did not find any acceptable offers; returning empty list')
        return []
    return filtered

def score_offer(o: dict) -> tuple:
    # Return a tuple key to sort offers by: (price, gpu_mismatch, perf)
    # - price: lower is better
    # - gpu_mismatch: 0 if exact match to VAST_NUM_GPUS, else 1 (prefer exact matches)
    # - perf: dlperf_usd-like value as tiebreaker (lower better)
    def price(o: dict) -> float:
        for k in ('price_hour_usd', 'price', 'price_usd'):
            v = o.get(k)
            try:
                if v is not None:
                    return float(v)
            except Exception:
                continue
        try:
            s = o.get('search') or {}
            if s and s.get('totalHour') is not None:
                return float(s.get('totalHour'))
        except Exception:
            pass
        try:
            if o.get('dph_total') is not None:
                return float(o.get('dph_total'))
        except Exception:
            pass
        return 1e9

    def perf(o: dict) -> float:
        try:
            return float(o.get('dlperf_usd') or o.get('dlperf_usd_per_hour') or o.get('dlperf_usd_per_sec') or 1e9)
        except Exception:
            return 1e9

    p = price(o)
    q = perf(o)
    try:
        desired = int(os.getenv('VAST_NUM_GPUS', str(MIN_GPUS)))
        g = int(o.get('num_gpus') or 0)
    except Exception:
        desired = MIN_GPUS
        g = int(o.get('num_gpus') or 0)
    gpu_mismatch = 0 if g == desired else 1
    return (p, gpu_mismatch, q)

def pick_best_offer(offers: List[dict]) -> Optional[dict]:
    if not offers:
        return None
    return sorted(offers, key=score_offer)[0]

def create_instance_from_ask(ask_id: int, label_suffix: str, offer: Optional[dict] = None) -> int:
    label = f"{INSTANCE_LABEL}-{label_suffix}"
    body: Dict[str, Any] = {
        "disk": INSTANCE_DISK_GB,
        "label": label,
        "env": {
            "REDIS_URL": REDIS_URL,
            "QUEUE_NAME": QUEUE_NAME,
        },
        "target_state": "running",
        "template_hash": TEMPLATE_HASH,
    }
    # If an explicit image was provided via VAST_IMAGE, include it; otherwise rely on the template (Ubuntu-only templates omit image)
    selected_image = IMAGE

    # Optional: provision ephemeral GitHub self-hosted runner user-data and inject into instance env
    try:
        provision_runner_flag = os.getenv("VAST_PROVISION_RUNNER", "false").lower() in ("1", "true", "yes")
    except Exception:
        provision_runner_flag = False

    if provision_runner_flag:
        tmp_out = None
        try:
            # Determine repo root and helper script path (work from repository root)
            repo_root = Path(__file__).resolve().parents[1]
            script_path = repo_root / "scripts" / "provision_runner.sh"

            owner = GITHUB_OWNER or "mike21043"
            repo = IMAGE_REPO or "transcript-coach-saas"

            # Create a safe temporary file for the helper to write the user-data into
            tf = tempfile.NamedTemporaryFile(prefix=f"runner-user-data-{label_suffix}-", suffix=".sh", delete=False)
            tmp_out = tf.name
            tf.close()

            cmd = ["/bin/bash", str(script_path), owner, repo, tmp_out]
            log.info(f"Provisioning runner user-data via: {' '.join(cmd)} (cwd={repo_root})")
            # Run helper from repo root so relative paths inside the helper resolve correctly
            subprocess.run(cmd, check=True, cwd=str(repo_root))

            # Read generated user-data and embed (base64) into instance env. Protect against huge payloads.
            with open(tmp_out, 'rb') as f:
                ud = f.read()

            max_size = 1_000_000  # 1MB limit for embedding into env
            if len(ud) > max_size:
                log.warning(f"Runner user-data is large ({len(ud)} bytes) — skipping embedding; consider delivering via cloud-init or artifact.")
            else:
                ud_b64 = base64.b64encode(ud).decode('ascii')
                body.setdefault('env', {})
                body['env']['RUNNER_USER_DATA_B64'] = ud_b64

                # Allow override of labels via env VAST_RUNNER_LABELS, otherwise use sensible defaults
                labels = os.getenv("VAST_RUNNER_LABELS", "self-hosted,cuda-test,transcript-coach")
                body['env']['RUNNER_LABELS'] = labels
                log.info("Embedded runner user-data into instance env (base64) and set RUNNER_LABELS")
        except subprocess.CalledProcessError as e:
            log.warning(f"Provision helper failed (exit {e.returncode}): {e}")
        except Exception as e:
            log.warning(f"Failed to provision runner user-data: {e}")
        finally:
            # Best-effort cleanup of temp file
            try:
                if tmp_out and Path(tmp_out).exists():
                    Path(tmp_out).unlink()
            except Exception:
                pass

    # IMAGE_MAP: optional env mapping keys to image tags. Format: "key1=ghcr.io/org/repo:tag1,key2=ghcr.io/org/repo:tag2"
    IMAGE_MAP_RAW = os.getenv("IMAGE_MAP", "")
    if IMAGE_MAP_RAW and offer:
        try:
            # parse into dict
            m = dict(x.split("=", 1) for x in IMAGE_MAP_RAW.split(",") if "=" in x)
            offer_text = str(offer)
            for k, v in m.items():
                if k.strip() and k.strip().lower() in offer_text.lower():
                    selected_image = v.strip()
                    log.info(f"Selecting image from IMAGE_MAP: key={k} -> {selected_image}")
                    break
        except Exception as e:
            log.warning(f"Failed to parse IMAGE_MAP: {e}")

    # verify accessibility for registry-hosted images (e.g. ghcr.io)
    fallback_img = os.getenv("VAST_FALLBACK_IMAGE", "ubuntu:22.04")
    try:
        if selected_image and ('ghcr.io' in selected_image or ('/' in selected_image and '.' in selected_image.split('/')[0])):
            ok = _head_manifest(selected_image)
            if not ok:
                log.warning(f"Image {selected_image} not publicly accessible; falling back to {fallback_img}")
                selected_image = fallback_img
    except Exception as e:
        log.warning(f"Image accessibility check failed: {e}; proceeding with requested image")

    if selected_image:
        body["image"] = selected_image

    # attempt create; if server rejects template-related args, retry without template_hash
    try:
        res = _req("PUT", f"/asks/{ask_id}/", body)
    except Exception as e:
        msg = str(e)
        log.warning(f"Initial create attempt failed: {msg}. Trying again without template_hash if applicable.")
        # remove template_hash and retry once
        if 'template_hash' in body:
            body.pop('template_hash', None)
            try:
                res = _req("PUT", f"/asks/{ask_id}/", body)
            except Exception as e2:
                log.error(f"Retry without template_hash also failed: {e2}")
                raise
        else:
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


def show_instance(inst_id: int) -> dict:
    return _req("GET", f"/instances/{inst_id}/")

def list_instances() -> List[dict]:
    res = _req("GET", "/instances/")
    return res.get("instances", []) or []

def destroy_instance(inst_id: int) -> None:
    try:
        log.info(f"Destroying instance {inst_id} ...")
        _req("DELETE", f"/instances/{inst_id}/")
        log.info("Instance destroyed")
    except Exception as e:
        log.warning(f"Destroy failed for {inst_id}: {e}")

def wait_until_running(inst_id: int, timeout_sec: int) -> bool:
    """Poll Vast until instance is reported as running/active/open."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            info = show_instance(inst_id)
            inst = info.get("instances", info)

            # normalize possible fields
            status = str(
                inst.get("cur_state")
                or inst.get("actual_status")
                or inst.get("intended_status")
                or inst.get("state")
                or ""
            ).lower()
            msg = str(inst.get("status_msg") or "")

            log.info(f"Instance {inst_id} status='{status}' msg='{msg[:80]}'")

            if any(s in status for s in ("open", "running", "active", "connected")):
                return True
        except Exception as e:
            log.warning(f"Polling instance {inst_id} failed: {e}")

        time.sleep(10)
    return False



# =========================
# Redis & Config
# =========================
def redis_client(url: str) -> redis.Redis:
    return redis.from_url(url)

def get_timeouts(r: redis.Redis) -> (int, int):
    try:
        cfg = r.hgetall("agent_config")
        idle = int(cfg.get(b"IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT))
        stale = int(cfg.get(b"STALE_TICKS", DEFAULT_STALE_TICKS))
        return idle, stale
    except Exception as e:
        log.error(f"ERROR reading agent_config: {e}")
        return DEFAULT_IDLE_TIMEOUT, DEFAULT_STALE_TICKS

# =========================
# Controller loop
# =========================
def controller_loop():
    log.info("Starting Vast.ai controller loop (fresh provisioning mode)...")
    log.info(f"Redis: {REDIS_URL} | Queue: {QUEUE_NAME}")
    if not TEMPLATE_HASH:
        raise RuntimeError("VAST_TEMPLATE_HASH is not set")

    r = redis_client(REDIS_URL)

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
            idle_timeout, stale_ticks = get_timeouts(r)
            log.info(f"Queue length: {qlen} | Idle timeout={idle_timeout}s | Stale ticks={stale_ticks}")

            if qlen > 0 and active_instance is None:
                offers = search_offers()
                best = pick_best_offer(offers)
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
                                destroy_instance(active_instance)
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
                    log.info(f"DEBUG: Instance {active_instance} appears stale: {stale_counter}/{stale_ticks}")
                    if stale_counter >= stale_ticks:
                        log.info(f"Forcing fresh instance after ~{stale_ticks * LOOP_INTERVAL}s with no progress...")
                        destroy_instance(active_instance)
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
                    log.info(f"Idle timer: {idle_elapsed}s / {idle_timeout}s")
                    if idle_elapsed >= idle_timeout:
                        log.info(f"Idle for {idle_timeout}s → destroying instance {active_instance}")
                        destroy_instance(active_instance)
                        active_instance = None
                        idle_elapsed = 0

        except Exception as e:
            log.error(f"ERROR in controller loop: {e}")

        time.sleep(LOOP_INTERVAL)

# =========================
# Entry
# =========================
if __name__ == "__main__":
    controller_loop()
