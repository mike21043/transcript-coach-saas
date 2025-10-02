#!/usr/bin/env python3
"""
End-to-end provisioning harness:
- Generates runner user-data (calls scripts/provision_runner.sh)
- Calls Vast API to create an instance with embedded RUNNER_USER_DATA_B64
- Waits for instance to become running
- Polls GitHub for the registered runner with the expected label
- Dispatches the smoke workflow and waits for completion

This script requires the following env vars when running non-dry:
 - VAST_API_KEY
 - VAST_TEMPLATE_HASH
 - GITHUB_PAT
 - GITHUB_OWNER (or provide --owner)
 - IMAGE_REPO (or provide --repo)

Use --dry to validate steps without performing remote actions.
"""

import os
import sys
import time
import json
import base64
import tempfile
import subprocess
from pathlib import Path
import re
import random

try:
    import requests
except Exception as e:
    print("requests library is required. Install with: pip install requests")
    raise

VAST_BASE = "https://cloud.vast.ai/api/v0"

def api_headers(vast_api_key: str):
    return {"Authorization": f"Bearer {vast_api_key}", "Accept": "application/json", "Content-Type": "application/json"}

def vast_req(method, path, token, body=None, timeout=60):
    """Perform a request to the Vast API with retries on transient errors.

    Retries on network errors, HTTP 429 (rate limit), and 5xx server errors.
    Behavior is configurable with env vars:
      VAST_REQ_MAX_RETRIES (default 5)
      VAST_REQ_BACKOFF_BASE (seconds, default 1.0)
    """
    url = f"{VAST_BASE}{path}"
    max_retries = int(os.getenv('VAST_REQ_MAX_RETRIES', '5'))
    backoff_base = float(os.getenv('VAST_REQ_BACKOFF_BASE', '1.0'))

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.request(method, url, headers=api_headers(token), json=body, timeout=timeout)
        except Exception as e:
            last_exc = e
            # network-level error: retryable
            if attempt >= max_retries:
                raise RuntimeError(f"Vast request failed after {attempt} attempts: {e}") from e
            sleep = backoff_base * (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5)
            print(f"Vast request network error (attempt {attempt}/{max_retries}): {e}; retrying in {sleep:.1f}s")
            time.sleep(sleep)
            continue

        # Got a response; check status
        try:
            r.raise_for_status()
        except Exception as e:
            # Include response body for debugging
            msg = None
            try:
                msg = r.json()
            except Exception:
                msg = r.text
            status = r.status_code
            # Retry on rate limit (429) or server errors (5xx)
            if status == 429 or (500 <= status < 600):
                last_exc = RuntimeError(f"Vast API error {status}: {msg}")
                if attempt >= max_retries:
                    print(f"Vast API persistent error after {attempt} attempts: {msg}")
                    raise last_exc from e
                sleep = backoff_base * (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5)
                print(f"Vast API transient status {status} (attempt {attempt}/{max_retries}): {msg}; retrying in {sleep:.1f}s")
                time.sleep(sleep)
                continue
            # Non-retryable error: surface immediately
            print(f"Vast API error {status}: {msg}")
            raise RuntimeError(f"Vast API error {status}: {msg}") from e

        # Success
        try:
            return r.json()
        except Exception:
            return {"raw": r.text, "status": r.status_code}

def search_offers(vast_api_key, min_gpus=1, only_verified=True):
    # Allow caller to override GPU count and minimum CUDA via env
    preferred_num_gpus = int(os.getenv('VAST_NUM_GPUS', str(min_gpus)))
    min_cuda = float(os.getenv('VAST_MIN_CUDA', '12.9'))

    # Budget cap (env support for multiple names for compatibility)
    price_env = os.getenv('priceInstanceHourlyMax') or os.getenv('PRICE_INSTANCE_HOURLY_MAX') or os.getenv('VAST_PRICE_INSTANCE_HOURLY_MAX')
    price_max = None
    try:
        price_max = float(price_env) if price_env else None
    except Exception:
        price_max = None

    # Build initial server-side query. Prefer exact GPU count when requested, otherwise allow >=.
    if 'VAST_NUM_GPUS' in os.environ:
        q = {"rentable": {"eq": True}, "rented": {"eq": False}, "num_gpus": {"eq": preferred_num_gpus}}
    else:
        q = {"rentable": {"eq": True}, "rented": {"eq": False}, "num_gpus": {"gte": preferred_num_gpus}}

    # Push numeric filters to the server where possible to reduce payload size and client work.
    # Add a Max CUDA floor and a price upper bound when configured.
    if price_max is not None:
        # Many Vast search endpoints accept price_hour_usd numeric filters; include as a hint.
        try:
            q['price_hour_usd'] = {'lte': float(price_max)}
        except Exception:
            pass
    # Minimum CUDA requirement
    try:
        q['cuda_max_good'] = {'gte': float(min_cuda)}
    except Exception:
        pass
    if only_verified:
        q["verified"] = {"eq": True}
    # Allowed countries from env (opt-in). By default we do not restrict by country.
    # Note: machine_countries filter is not reliably accepted by the Vast API; we'll filter client-side below.
    allowed = [c.strip().lower() for c in os.getenv('VAST_ALLOWED_COUNTRIES', '').split(',') if c.strip()]
    # Budget cap: ask API for price_hour_usd <= price_max
    # default budget to $0.30/hr if not provided
    if price_max is None:
        try:
            price_max = float(os.getenv('priceInstanceHourlyMax', os.getenv('PRICE_INSTANCE_HOURLY_MAX', '0.30')))
        except Exception:
            price_max = None

    # Some Vast search endpoints reject price_hour_usd in the query; we'll filter client-side below
    # Call the API and capture HTTP errors for debugging
    # Execute search and return offers. Some Vast endpoints reject certain
    # numeric filter keys (e.g. price_hour_usd). Attempt the query, and if the
    # server rejects it, retry with a reduced/safer query payload so we still
    # get results rather than aborting the whole run.
    try:
        res = vast_req("PUT", "/search/asks/", vast_api_key, {"q": q}).get("offers", [])
    except RuntimeError as e:
        print('Initial search query rejected by Vast API, retrying with safer query:', e)
        # Build a conservative fallback query containing only boolean/enum keys
        safe_keys = ('rentable', 'rented', 'num_gpus', 'verified')
        q2 = {k: v for k, v in q.items() if k in safe_keys}
        try:
            res = vast_req("PUT", "/search/asks/", vast_api_key, {"q": q2}).get("offers", [])
        except Exception as e2:
            print('Safe query also failed, falling back to an unrestricted search (no q) to gather offers:', e2)
            try:
                res = vast_req("PUT", "/search/asks/", vast_api_key, {}).get("offers", [])
            except Exception:
                # If everything fails, re-raise the original error so callers can handle it
                raise
    # Apply country filters from env if provided
    exclude = [c.strip().lower() for c in os.getenv('VAST_EXCLUDE_COUNTRIES', 'CN,CHN,PRC,CHINA').split(',') if c.strip()]

    def ok(o):
        # Require a known machine_country; unknown locations tend to be outside preferred regions
        mc = str(o.get('machine_country') or o.get('country') or '').lower()
        if not mc:
            return False
        for ex in exclude:
            if ex and ex in mc:
                return False
        if allowed:
            return any(a in mc for a in allowed)
        return True

    filtered = [o for o in res if ok(o)]
    # Optional: hint the server to filter by GPU model when requested. Many Vast search
    # endpoints accept basic text matches via 'like' or similar operators, but behavior
    # varies. We include a 'like' hint here and still perform a robust client-side
    # substring check below so this is only an optimization.
    # Default to searching for RTX 3090 and Tesla T4 if the env var isn't set
    gpu_model_env = os.getenv('VAST_GPU_MODEL', '3090,t4')
    models = []
    if gpu_model_env:
        models = [m.strip().lower() for m in gpu_model_env.split(',') if m.strip()]
        gm = models[0] if models else ''
        # sanitize gm to avoid embedded newlines or control characters that can break the API
        try:
            gm = gm.replace('\n', ' ').replace('\r', ' ').strip()
        except Exception:
            pass
        # Do not attempt server-side text 'like' filters — Vast rejects unsupported
        # operators (e.g. 'like'). We rely on robust client-side substring checks
        # below to match GPU model substrings.

    # Execute search with server-side hints. Reuse the same resilient call
    # pattern above to avoid fatal 400 errors when the provider rejects
    # particular search keys.
    try:
        res = vast_req("PUT", "/search/asks/", vast_api_key, {"q": q}).get("offers", [])
    except RuntimeError as e:
        print('Search with hints rejected by Vast API, retrying without hints:', e)
        try:
            res = vast_req("PUT", "/search/asks/", vast_api_key, {}).get("offers", [])
        except Exception:
            raise

    # Client-side filtering for model substring remains (robust fallback)
    if models:
        def model_ok(o):
            # Check several candidate fields where the GPU model may appear
            candidates = []
            for k in ('machine_gpu', 'gpu', 'gpu_name', 'gpu_model', 'machine_type', 'title', 'name', 'image', 'machine_image'):
                v = o.get(k)
                if v:
                    candidates.append(str(v).lower())
            # Also include any nested 'search' or 'desc' text
            s = o.get('search') or {}
            if isinstance(s, dict):
                for vv in s.values():
                    try:
                        candidates.append(str(vv).lower())
                    except Exception:
                        pass
            # If any candidate contains any requested substring, accept the offer
            return any(any(m in c for m in models) for c in candidates if isinstance(c, str))

        filtered = [o for o in filtered if model_ok(o)]
    else:
        filtered = res
    # Further filter by min CUDA and exact GPU preference when possible
    def cuda_ok(o):
        try:
            cm = float(o.get('cuda_max_good') or 0.0)
        except Exception:
            cm = 0.0
        g = int(o.get('num_gpus') or 0)
        if cm < min_cuda:
            return False
        # prefer exact GPU count match when VAST_NUM_GPUS is set
        if 'VAST_NUM_GPUS' in os.environ:
            return g == int(os.environ['VAST_NUM_GPUS'])
        return True

    filtered = [o for o in filtered if cuda_ok(o)]
    # If caller explicitly requested a GPU count, prefer exact matches.
    try:
        if 'VAST_NUM_GPUS' in os.environ:
            desired = int(os.environ['VAST_NUM_GPUS'])
            exact = [o for o in filtered if int(o.get('num_gpus') or 0) == desired]
            if exact:
                filtered = exact
    except Exception:
        pass
    if not filtered and res:
        print(f"No offers matched allowed countries {allowed}; falling back to all offers excluding {exclude}")
        filtered = [o for o in res if not any(ex in str(o.get('machine_country','')).lower() for ex in exclude)]
        # apply cuda/gpu filter to fallback list as well
        filtered = [o for o in filtered if cuda_ok(o)]
    return filtered


def get_offer_price(o):
    # Robust price extraction, return None when unknown
    for k in ('price_hour_usd', 'price', 'price_usd', 'discounted_hourly'):
        if k in o and o.get(k) is not None:
            try:
                v = float(o.get(k))
                if v > 1e-8:
                    return v
            except Exception:
                continue
    s = o.get('search') or {}
    if s.get('totalHour') is not None:
        try:
            return float(s.get('totalHour'))
        except Exception:
            pass
    if o.get('dph_total') is not None:
        try:
            return float(o.get('dph_total'))
        except Exception:
            pass
    return None

def pick_best(offers):
    if not offers:
        return None
    def extract_price(o):
        # try several common price fields; fall back to large number when missing
        for k in ('price_hour_usd', 'price', 'price_usd'):
            v = o.get(k)
            try:
                if v is not None:
                    return float(v)
            except Exception:
                continue
        # nested shapes
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

    def extract_perf(o):
        # lower is better for dlperf_usd style fields
        for k in ('dlperf_usd', 'dlperf_usd_per_hour', 'dlperf_usd_per_sec'):
            try:
                v = o.get(k)
                if v is not None:
                    return float(v)
            except Exception:
                continue
        return 1e9

    # Primary sort: absolute price (lower is better). Secondary: dlperf_usd (lower is better).
    return sorted(offers, key=lambda o: (extract_price(o), extract_perf(o)))[0]

def create_instance(vast_api_key, ask_id, template_hash, ud_b64, runner_labels, disk_gb=32, image: str = None):
    body = {
        "disk": disk_gb,
        "label": f"e2e-{int(time.time())}",
        "env": {
            # Prefer a small pointer URL when provided to avoid exceeding
            # provider limits on total env length. If RUNNER_USER_DATA_URL is
            # set in the local environment, include that instead of the
            # full base64 user-data payload. Otherwise embed the base64 blob
            # as before.
            # ud_b64 is already base64-encoded by the caller
            **({"RUNNER_USER_DATA_URL": os.getenv('RUNNER_USER_DATA_URL')} if os.getenv('RUNNER_USER_DATA_URL') else {"RUNNER_USER_DATA_B64": ud_b64}),
            "RUNNER_LABELS": runner_labels,
        },
        "target_state": "running",
    }
    # Only include template_hash if explicitly provided; some asks reject null template values
    if template_hash is not None:
        body["template_hash"] = template_hash
    # Only include the 'image' key when the caller explicitly requested an image
    # or provided a non-empty string. Sending an empty string caused some
    # providers to attempt to pre-pull an invalid reference ("invalid reference format").
    if image:
        body["image"] = image
    # Optional: if caller wants logs uploaded, include RCLONE_CONF_B64 when available
    upload_remote = os.getenv('UPLOAD_LOG_REMOTE') or os.getenv('UPLOAD_LOG_REMOTE')
    if upload_remote:
        repo_root = Path(__file__).resolve().parents[1]
        rconf = repo_root / 'rclone.conf'
        if rconf.exists():
            try:
                rc = rconf.read_bytes()
                body.setdefault('env', {})
                body['env']['RCLONE_CONF_B64'] = base64.b64encode(rc).decode('ascii')
                body['env']['UPLOAD_LOG_REMOTE'] = upload_remote
            except Exception:
                print('Failed to include rclone.conf for log upload; continuing without it')

    # Include Docker config (base64) only when explicitly provided via env
    # to avoid exceeding provider user-data/env size limits. The harness
    # previously attempted to embed the host's ~/.docker/config.json or
    # auto-generate one from GITHUB_PAT which could produce very large
    # env payloads and cause Vast API 'total length > 32KB' errors. Only
    # include DOCKER_CONFIG_B64 when the caller intentionally sets it.
    docker_cfg_b64 = os.getenv('DOCKER_CONFIG_B64')

    if docker_cfg_b64:
        body.setdefault('env', {})
        body['env']['DOCKER_CONFIG_B64'] = docker_cfg_b64
        # If we provide Docker credentials, enable installing Docker so the
        # instance can pull authenticated images. This avoids 'denied' errors.
        body['env']['INSTALL_DOCKER'] = '1'
        # If the caller did not request a pre-pulled instance image, but we
        # supplied Docker credentials, set a TARGET_AGENT_IMAGE so the instance
        # will perform an authenticated pull after Docker and credentials are
        # in place. This avoids provider-side pre-pulls which often fail for
        # private GHCR images.
        try:
            if not image:
                owner_env = os.getenv('GITHUB_OWNER') or os.getenv('IMAGE_REPO_OWNER') or os.getenv('GITHUB_REPO_OWNER')
                repo_env = os.getenv('IMAGE_REPO') or os.getenv('GITHUB_REPO') or os.getenv('IMAGE_REPO_NAME')
                owner_use = owner_env or os.getenv('GITHUB_ACTOR') or os.getenv('GITHUB_OWNER') or os.getenv('VAST_GH_OWNER') or 'mike21043'
                repo_use = repo_env or os.getenv('IMAGE_REPO') or os.getenv('VAST_IMAGE_REPO') or 'transcript-coach-agent'
                # default to the cuda129 tag used by the repo publishing workflow
                body['env']['TARGET_AGENT_IMAGE'] = f"ghcr.io/{owner_use}/{repo_use}:cuda129"
        except Exception:
            pass
    # Allow forcing Docker installation and a post-boot container pull even when
    # no docker config was provided. This enables the "container-on-plain-VM"
    # fallback flow where we request a known-good cloud image (e.g. ubuntu)
    # and have cloud-init install Docker + NVIDIA toolkit and then pull the
    # runner container. Controlled by env var FORCE_INSTALL_DOCKER=1.
    try:
        if os.getenv('FORCE_INSTALL_DOCKER', '') in ('1', 'true', 'True'):
            body.setdefault('env', {})
            # Instruct cloud-init to install Docker/NVIDIA tooling
            body['env']['INSTALL_DOCKER'] = '1'
            # If we don't have a TARGET_AGENT_IMAGE yet, derive one from env
            if not image and not body['env'].get('TARGET_AGENT_IMAGE'):
                try:
                    owner_env = os.getenv('GITHUB_OWNER') or os.getenv('IMAGE_REPO_OWNER') or os.getenv('GITHUB_REPO_OWNER')
                    repo_env = os.getenv('IMAGE_REPO') or os.getenv('GITHUB_REPO') or os.getenv('IMAGE_REPO_NAME')
                    owner_use = owner_env or os.getenv('GITHUB_ACTOR') or os.getenv('GITHUB_OWNER') or os.getenv('VAST_GH_OWNER') or 'mike21043'
                    repo_use = repo_env or os.getenv('IMAGE_REPO') or os.getenv('VAST_IMAGE_REPO') or 'transcript-coach-agent'
                    body['env']['TARGET_AGENT_IMAGE'] = f"ghcr.io/{owner_use}/{repo_use}:cuda129"
                except Exception:
                    pass
    except Exception:
        pass
    # Sanitise the body: remove keys with None or empty-string values to avoid
    # sending invalid types that some Vast endpoints reject (e.g. image=None).
    def clean(obj):
        """Recursively remove None and empty-string values from dicts and lists.

        Many Vast API variants are picky about nulls (JSON null -> Python None). The
        previous implementation only handled dicts and could leave None inside
        lists or nested containers. This more robust cleaner will:
          - remove dict keys with value None or empty string
          - remove list/tuple elements that are None or empty string
          - recurse into nested dicts/lists
        """
        # Dicts: clean per-key and drop empty values
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if v is None:
                    continue
                if isinstance(v, str) and v == "":
                    continue
                cleaned = clean(v)
                # skip cleaned empty containers
                if cleaned is None:
                    continue
                if isinstance(cleaned, (list, dict)) and len(cleaned) == 0:
                    continue
                out[k] = cleaned
            return out
        # Lists/tuples: clean elements and drop empty ones
        if isinstance(obj, (list, tuple)):
            out_list = []
            for v in obj:
                if v is None:
                    continue
                if isinstance(v, str) and v == "":
                    continue
                cleaned = clean(v)
                if cleaned is None:
                    continue
                if isinstance(cleaned, (list, dict)) and len(cleaned) == 0:
                    continue
                out_list.append(cleaned)
            return out_list
        # Other scalar types: return as-is
        return obj

    body_clean = clean(body)
    # Extra guard: some Vast endpoints loudly complain when 'image' exists but is not a string
    if isinstance(body_clean, dict) and 'image' in body_clean and not isinstance(body_clean.get('image'), str):
        try:
            del body_clean['image']
        except Exception:
            pass
    # Recursively remove any nested 'image' keys that are None or non-string
    def remove_null_image(obj):
        if isinstance(obj, dict):
            keys = list(obj.keys())
            for k in keys:
                v = obj.get(k)
                if k == 'image' and (v is None or not isinstance(v, str)):
                    try:
                        del obj[k]
                    except Exception:
                        pass
                    continue
                remove_null_image(v)
        elif isinstance(obj, list):
            for v in obj:
                remove_null_image(v)

    try:
        remove_null_image(body_clean)
    except Exception:
        pass
    # DEBUG: print the full create body (helpful when diagnosing 400 responses)
    try:
        print('DEBUG create body full:', json.dumps(body_clean, indent=2))
    except Exception:
        try:
            print('DEBUG create body repr:', repr(body_clean)[:4000])
        except Exception:
            print('DEBUG create body: <unserializable>')
    # Extra diagnostics: locate any remaining 'image' keys and any None values
    def find_image_keys(obj, path=''):
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == 'image':
                    found.append((path + '/' + k, v, type(v).__name__))
                found.extend(find_image_keys(v, path + '/' + k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                found.extend(find_image_keys(v, f"{path}[{i}]") )
        return found

    def find_none_values(obj, path=''):
        found = []
        if obj is None:
            return [(path or '/')]
        if isinstance(obj, dict):
            for k, v in obj.items():
                found.extend(find_none_values(v, path + '/' + k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                found.extend(find_none_values(v, f"{path}[{i}]"))
        return found

    try:
        imgs = find_image_keys(body_clean)
        if imgs:
            print('DIAG: found image keys in cleaned body:')
            for p, val, t in imgs:
                try:
                    print(f" - {p}: type={t} value={repr(val)[:200]}")
                except Exception:
                    print(f" - {p}: <unprintable>")
        else:
            print('DIAG: no image keys found in cleaned body')
        none_locs = find_none_values(body_clean)
        if none_locs:
            print('DIAG: found None values at paths:')
            for p in none_locs[:30]:
                print(' -', p)
        else:
            print('DIAG: no None values found in cleaned body')
    except Exception as e:
        print('DIAG: diagnostics failed:', e)
    # If we generated an ephemeral SSH public key earlier, include it in the
    # create payload using a few common field names some providers accept so
    # the provider-side SSH proxy can register it for immediate access. This
    # is best-effort: if the provider ignores unknown fields this is harmless,
    # but when accepted it fixes 'Permission denied (publickey)' issues.
    try:
        ssh_pub = globals().get('ssh_public_key')
        if ssh_pub:
            # include several likely accepted keys
            body_clean.setdefault('ssh_keys', [])
            # ensure it's a list
            if isinstance(body_clean.get('ssh_keys'), list):
                body_clean['ssh_keys'].append(ssh_pub)
            body_clean.setdefault('ssh_key', ssh_pub)
            body_clean.setdefault('public_key', ssh_pub)
    except Exception:
        pass
    # DEBUG: print the full create body (helpful when diagnosing 400 responses)
    try:
        print('DEBUG create body:', json.dumps(body_clean, indent=2)[:4000])
    except Exception:
        print('DEBUG create body: <unserializable>')
    # EXTRA DEBUG: fetch the ask object the provider will use as a template
    try:
        ask_info = vast_req('GET', f"/asks/{ask_id}/", vast_api_key)
        try:
            print('DEBUG ask object:', json.dumps(ask_info, indent=2)[:4000])
        except Exception:
            print('DEBUG ask object repr:', repr(ask_info)[:4000])
        # Look for image keys in the ask
        def find_image_in_ask(obj, path=''):
            res = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k == 'image':
                        res.append((path + '/' + k, v, type(v).__name__))
                    res.extend(find_image_in_ask(v, path + '/' + k))
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    res.extend(find_image_in_ask(v, f"{path}[{i}]"))
            return res
        imgs_ask = find_image_in_ask(ask_info)
        if imgs_ask:
            print('DIAG: ask object contains image keys:')
            for p, val, t in imgs_ask:
                print(f' - {p}: type={t} value={repr(val)[:200]}')
        else:
            print('DIAG: ask object contains no image keys')
    except Exception as e:
        print('DEBUG: failed to fetch ask object for diagnostics:', e)

    # Perform create with extra diagnostics on specific image-type errors.
    try:
        return vast_req("PUT", f"/asks/{ask_id}/", vast_api_key, body_clean)
    except Exception as e:
        msg = str(e)
        print(f"Create failed for ask {ask_id}: {e}")
        # If provider complains about image being None/non-string, attempt extra diagnostics
        if 'image must be' in msg or 'image must be a str' in msg or "image must be a str" in msg:
            print("DIAG: provider reported image-type error; attempting extra diagnostics...")
            # Try alternative ask fetch patterns (without trailing slash and search-by-id)
            try:
                alt = vast_req('GET', f"/asks/{ask_id}", vast_api_key)
                try:
                    print('DIAG: ask object (no-trailing-slash):', json.dumps(alt, indent=2)[:4000])
                except Exception:
                    print('DIAG: ask object (no-trailing-slash) repr:', repr(alt)[:4000])
            except Exception as e2:
                print('DIAG: failed alt GET /asks/{id} (no slash):', e2)
            try:
                qbody = {"q": {"id": {"eq": int(ask_id)}}}
                search_res = vast_req('PUT', '/search/asks/', vast_api_key, qbody)
                try:
                    print('DIAG: search result for ask id:', json.dumps(search_res, indent=2)[:4000])
                except Exception:
                    print('DIAG: search result repr:', repr(search_res)[:4000])
            except Exception as e3:
                print('DIAG: failed search/asks for id:', e3)

            # Optionally retry once without template_hash when env enables it; this is only
            # for diagnostics and will not be performed by default to preserve explicit
            # template intent. Set VAST_DIAG_ALLOW_REMOVE_TEMPLATE=1 in env to enable.
            if os.getenv('VAST_DIAG_ALLOW_REMOVE_TEMPLATE') in ('1', 'true', 'True'):
                try:
                    bc = dict(body_clean) if isinstance(body_clean, dict) else body_clean
                    if isinstance(bc, dict) and 'template_hash' in bc:
                        print('DIAG: retrying create after removing template_hash from payload (diagnostic)')
                        bc2 = dict(bc)
                        bc2.pop('template_hash', None)
                        try:
                            res_retry = vast_req("PUT", f"/asks/{ask_id}/", vast_api_key, bc2)
                            print('DIAG: retry succeeded (without template_hash):', json.dumps(res_retry, indent=2)[:4000])
                            return res_retry
                        except Exception as e4:
                            print('DIAG: retry without template_hash also failed:', e4)
                except Exception as e5:
                    print('DIAG: unexpected error while attempting retry diagnostics:', e5)
        # Re-raise original error after diagnostics
        raise


def probe_instance_is_minimized(vast_api_key, inst_id, timeout_sec=60):
    """Best-effort probe to detect minimized/container-like images that lack cloud-init.

    Strategy:
      - Fetch provider console/serial endpoints and search for keywords like
        'minimiz', 'unminimize', or 'cloud-init' absence messages.
      - If an ephemeral SSH key exists, try a quick ssh to test for
        /var/lib/cloud/instances presence.
    Returns True when the instance appears to be a minimized image and should be
    aborted/destroyed; False otherwise.
    """
    print(f"Probing instance {inst_id} for minimized image indicators...")
    console_paths = [
        f"/instances/{inst_id}/console/",
        f"/instances/{inst_id}/console_raw/",
        f"/instances/{inst_id}/serial/",
    ]
    keywords = ['minimiz', 'unminimize', 'no cloud-init', 'cloud-init not', 'This system has been minimized', 'unminimize', 'BEGIN-BASE64-BOOTSTRAP-LOG']
    for cp in console_paths:
        try:
            creq = vast_req('GET', cp, vast_api_key)
            txt = ''
            if isinstance(creq, dict):
                try:
                    txt = json.dumps(creq)
                except Exception:
                    txt = str(creq)
            else:
                txt = str(creq)
            low = txt.lower()
            for k in keywords:
                if k.lower() in low:
                    print(f"Probe: found keyword '{k}' in console {cp}; marking as minimized")
                    return True
        except Exception as e:
            print(f"Probe: console fetch {cp} failed (continuing): {e}")

    # Try SSH test if we have an ephemeral private key and the instance reports ssh info
    try:
        sk = globals().get('ssh_private_key_path')
        if sk and Path(sk).exists():
            info = show_instance(vast_api_key, inst_id)
            inst = info.get('instances', info)
            ssh_host = inst.get('ssh_host') or inst.get('ssh') or inst.get('public_ipaddr') or inst.get('ip')
            ssh_port = inst.get('ssh_port') or inst.get('ssh_port4') or inst.get('port')
            if ssh_host:
                ssh_cmd = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-i', str(sk)]
                if ssh_port:
                    ssh_cmd += ['-p', str(int(ssh_port))]
                ssh_cmd += [f"ubuntu@{ssh_host}", 'test -d /var/lib/cloud/instances && echo CLOUDINIT || echo NO']
                try:
                    out = subprocess.check_output(ssh_cmd, timeout=15, stderr=subprocess.STDOUT)
                    s = out.decode('utf-8', errors='ignore').strip()
                    print(f"Probe: ssh test output='{s}'")
                    if 'NO' in s or 'no' in s:
                        print('Probe: ssh indicates no /var/lib/cloud/instances; marking as minimized')
                        return True
                    else:
                        return False
                except Exception as e:
                    print(f"Probe: ssh test failed (continuing): {e}")
    except Exception as e:
        print(f"Probe: ssh probe raised error (continuing): {e}")

    # No indicators found — assume it's OK
    print(f"Probe: no minimized indicators found for instance {inst_id}")
    return False

def show_instance(vast_api_key, inst_id):
    return vast_req("GET", f"/instances/{inst_id}/", vast_api_key)

def list_instances(vast_api_key):
    return vast_req("GET", "/instances/", vast_api_key).get("instances", [])

def wait_until_running(vast_api_key, inst_id, timeout_sec=900):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            info = show_instance(vast_api_key, inst_id)
            inst = info.get("instances", info)
            status = str(inst.get("cur_state") or inst.get("actual_status") or inst.get("intended_status") or inst.get("state") or "").lower()
            print(f"Instance {inst_id} status={status}")
            if any(s in status for s in ("open", "running", "active", "connected")):
                return True
        except Exception as e:
            print(f"Waiting: error checking instance {inst_id}: {e}")
        time.sleep(10)
    return False

# GitHub helpers
GITHUB_API = "https://api.github.com"

def gh_headers(token):
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

def list_repo_runners(owner, repo, token):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runners"
    r = requests.get(url, headers=gh_headers(token), timeout=30)
    r.raise_for_status()
    return r.json().get("runners", [])

def dispatch_workflow(owner, repo, workflow_filename, ref, token):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/workflows/{workflow_filename}/dispatches"
    body = {"ref": ref}
    r = requests.post(url, headers=gh_headers(token), json=body, timeout=30)
    r.raise_for_status()
    return r.status_code

def find_latest_workflow_run(owner, repo, workflow_filename, token):
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/workflows/{workflow_filename}/runs"
    r = requests.get(url, headers=gh_headers(token), timeout=30)
    r.raise_for_status()
    runs = r.json().get("workflow_runs", [])
    return runs[0] if runs else None

def wait_for_run_completion(owner, repo, workflow_filename, timeout_sec, token):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        run = find_latest_workflow_run(owner, repo, workflow_filename, token)
        if not run:
            print("No workflow run found yet; waiting...")
            time.sleep(5)
            continue
        status = run.get("status")
        conclusion = run.get("conclusion")
        print(f"Run id={run.get('id')} status={status} conclusion={conclusion}")
        if status == "completed":
            return conclusion, run
        time.sleep(5)
    return "timeout", None

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry", action="store_true", help="Dry run; do not call remote APIs")
    p.add_argument("--dry-widen", action="store_true", help="Dry preview of progressive fallback steps: show candidate offers at each widening step")
    p.add_argument("--ask-id", type=int, help="Use specific ask id instead of auto-selecting")
    p.add_argument("--no-template", action="store_true", help="Omit template_hash when creating the instance")
    p.add_argument("--auto-destroy-on-failure", action="store_true", help="Automatically destroy the instance if runner doesn't register or workflow fails")
    p.add_argument("--no-auto-destroy", action="store_true", help="Explicitly disable auto-destroy even if quick mode sets it (keeps instances alive on failure)")
    p.add_argument("--preserve-debug", action="store_true", help="When destroying on failure, save debug artifacts (instance JSON, user-data, provider console) locally")
    p.add_argument("--simulate", action="store_true", help="Run a fast local simulated end-to-end flow (no network calls) to validate logic")
    p.add_argument("--quick", action="store_true", help="Convenience: run a short live test with reduced timeouts (overrides instance/runner wait secs to 30s) and enables preserve-debug + auto-destroy")
    p.add_argument("--confirm-high-price", action="store_true", help="Allow creating instances above the configured price cap (use with caution)")
    p.add_argument("--inject-ssh", action="store_true", help="Generate an ephemeral SSH keypair and inject the public key into the instance so the harness can SSH in for diagnostics")
    p.add_argument("--instance-wait-secs", type=int, default=int(os.getenv('INSTANCE_WAIT_SECS','300')), help="Seconds to wait for the instance to reach running state (default 300)")
    p.add_argument("--runner-wait-secs", type=int, default=int(os.getenv('RUNNER_WAIT_SECS','300')), help="Seconds to wait for the GitHub runner to register (default 300)")
    p.add_argument("--owner", default=os.getenv("GITHUB_OWNER", "mike21043"))
    p.add_argument("--repo", default=os.getenv("IMAGE_REPO", "transcript-coach-saas"))
    p.add_argument("--branch", default=os.getenv("GITHUB_REF", "local-save-20250920-225817"))
    p.add_argument("--workflow", default="smoke-published.yml")
    p.add_argument("--runner-labels", default=os.getenv("VAST_RUNNER_LABELS", "self-hosted,cuda-test,transcript-coach"))
    p.add_argument("--disk", type=int, default=int(os.getenv("INSTANCE_DISK_GB", "32")))
    p.add_argument("--image", default=None, help="Optional image to request for the instance (e.g. ubuntu:22.04)")
    p.add_argument("--cleanup", choices=['list','destroy'], help="List or destroy instances matching label (safe: requires VAST_API_KEY)")
    p.add_argument("--cleanup-label", default=os.getenv('CLEANUP_LABEL_PREFIX','e2e-'), help="Label prefix to match for cleanup (default 'e2e-')")
    p.add_argument("--cleanup-age-min", type=int, default=int(os.getenv('CLEANUP_AGE_MIN','0')), help="Only affect instances older than this many minutes (default 0)")
    p.add_argument("--preserve-instance", type=int, help="Fetch and save debug artifacts for an existing instance id and exit")
    args = p.parse_args()

    vast_api_key = os.getenv("VAST_API_KEY")
    template_hash = os.getenv("VAST_TEMPLATE_HASH")
    gh_pat = os.getenv("GITHUB_PAT")

    # If required env vars are missing, attempt to load them from a .env file at repo root
    def load_dotenv(path: Path):
        # Import regex locally to avoid accidental shadowing / closure issues
        # where an enclosing scope assigns to the name `re` (which makes
        # Python treat it as a local in the enclosing scope and breaks the
        # nested function reference). Using a local alias prevents that.
        import re as _re

        if not path.exists():
            return {}
        env = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = _re.match(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
            if not m:
                continue
            k, v = m.group(1), m.group(2)
            # strip optional surrounding quotes
            if (v.startswith("\"") and v.endswith("\"")) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            env[k] = v
        return env

    repo_root = Path(__file__).resolve().parents[1]
    dotenv = repo_root / '.env'
    if dotenv.exists():
        loaded = load_dotenv(dotenv)
        # Only set env vars that are missing
        if not vast_api_key and 'VAST_API_KEY' in loaded:
            vast_api_key = loaded['VAST_API_KEY']
        if not template_hash and 'VAST_TEMPLATE_HASH' in loaded:
            template_hash = loaded['VAST_TEMPLATE_HASH']
        if not gh_pat and 'GITHUB_PAT' in loaded:
            gh_pat = loaded['GITHUB_PAT']

    # Quick convenience mode: override timeouts and enable safe debug/cleanup settings.
    if getattr(args, 'quick', False):
        # Allow environment overrides for quick timeouts
        quick_wait = int(os.getenv('QUICK_WAIT_SECS', '30'))
        args.instance_wait_secs = quick_wait
        args.runner_wait_secs = quick_wait
        # Enable debug preservation and auto-destroy so failed instances are preserved then cleaned up
        args.preserve_debug = True
        args.auto_destroy_on_failure = True
        print(f"Quick mode enabled: instance_wait_secs={args.instance_wait_secs}, runner_wait_secs={args.runner_wait_secs}, preserve-debug and auto-destroy ON")

    # Allow an explicit --no-auto-destroy to override quick mode or any other
    # logic that might enable automatic destruction. This makes it easy for
    # callers to request that failed instances be left running for manual
    # debugging (the flag is idempotent when auto-destroy is already off).
    if getattr(args, 'no_auto_destroy', False):
        args.auto_destroy_on_failure = False

    # Optionally generate an ephemeral SSH keypair to inject into the instance for debugging
    ssh_private_key_path = None
    ssh_public_key = None
    if getattr(args, 'inject_ssh', False):
        # create a short-lived keypair in /tmp
        try:
            tmppfx = f"/tmp/vast_e2e_ssh_{int(time.time())}"
            # Prefer modern ed25519 keys (smaller, widely supported). Fall back to RSA if ed25519 isn't available.
            try:
                subprocess.check_call(["ssh-keygen", "-t", "ed25519", "-f", tmppfx, "-N", "", "-q"])
            except Exception:
                # older systems may not support ed25519; fall back to 2048-bit RSA
                subprocess.check_call(["ssh-keygen", "-t", "rsa", "-b", "2048", "-f", tmppfx, "-N", "", "-q"])
            ssh_private_key_path = tmppfx
            with open(tmppfx + ".pub", 'r') as f:
                ssh_public_key = f.read().strip()
            # expose to module globals so other helpers (preserve_debug_artifacts)
            # can attempt SSH fetches when an instance fails.
            globals()['ssh_private_key_path'] = ssh_private_key_path
            globals()['ssh_public_key'] = ssh_public_key
            print(f"Generated ephemeral SSH keypair; private key: {ssh_private_key_path} (keep it private)")
            # Best-effort: if we have a Vast API key, register the public key at
            # account level so it can be attached to instances or used by the
            # provider SSH proxy. We record the returned key id so we can
            # remove it later.
            try:
                if vast_api_key:
                    resp = vast_req('POST', '/ssh/', vast_api_key, {'ssh_key': ssh_public_key})
                    # Response may include an 'id' or 'ssh_id'; try common keys
                    key_id = None
                    if isinstance(resp, dict):
                        key_id = resp.get('id') or resp.get('ssh_id') or resp.get('key_id')
                    if key_id:
                        globals()['vast_ephemeral_ssh_key_id'] = key_id
                        print(f'Registered ephemeral SSH key with Vast account id={key_id}')
                    else:
                        print('Registered ephemeral SSH key but no id returned; will attempt instance attach as fallback')
            except Exception as e:
                print('Failed to register ephemeral SSH key with Vast account (continuing):', e)
        except Exception as e:
            print('Failed to generate ephemeral SSH keypair for injection:', e)
            ssh_private_key_path = None
            ssh_public_key = None

    if args.dry:
        print("Dry run: will not perform remote operations. Validating local steps...")
    elif args.dry_widen:
        # Preview mode: allow live Vast API calls but skip GitHub/template requirements.
        if not vast_api_key:
            # attempt to load from .env below; if still missing we'll error later
            pass
        print("Dry-widen preview: will query Vast API to show widening candidates (no instance creation)")
    else:
        if not vast_api_key or not template_hash or not gh_pat:
            print("Missing required env vars for non-dry run: VAST_API_KEY, VAST_TEMPLATE_HASH, GITHUB_PAT")
            sys.exit(2)

    # Generate user-data via helper or in-Python (preferred)
    repo_root = Path(__file__).resolve().parents[1]
    helper = repo_root / "scripts" / "provision_runner.sh"
    tmp_ud = tempfile.NamedTemporaryFile(prefix="runner-ud-", suffix=".sh", delete=False)
    tmp_ud.close()
    tmp_path = tmp_ud.name
    # Determine owner/repo values early so dry runs can reference them
    owner = args.owner if getattr(args, 'owner', None) else os.getenv("GITHUB_OWNER", "mike21043")
    repo = args.repo if getattr(args, 'repo', None) else os.getenv("GITHUB_REPO", "transcript-coach-saas")

    def get_github_reg_token(owner: str, repo: str, token: str) -> str:
        """Request a short-lived GitHub Actions runner registration token via the API."""
        url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/runners/registration-token"
        headers = gh_headers(token)
        r = requests.post(url, headers=headers, timeout=30)
        r.raise_for_status()
        j = r.json()
        t = j.get('token')
        if not t:
            raise RuntimeError(f"Failed to obtain registration token: {j}")
        # Defensive: remove any internal whitespace/newlines and trim
        try:
            import re as _re
            cleaned = _re.sub(r"\s+", "", str(t))
            return cleaned
        except Exception:
            try:
                return str(t).strip()
            except Exception:
                return t

    print(f"Generating runner user-data to: {tmp_path}")
    if args.dry:
        # Create a redacted user-data that mirrors what the helper would produce
        cloud_init_path = repo_root / "ops" / "runner-cloud-init.sh"
        if cloud_init_path.exists():
            cloud_text = cloud_init_path.read_text()
        else:
            cloud_text = "# <missing ops/runner-cloud-init.sh>"

        ud_text = (
            "#!/bin/bash\n"
            "export REG_TOKEN=REDACTED\n"
            f"export REPO_URL='https://github.com/{owner}/{repo}'\n"
            f"export RUNNER_LABELS='{args.runner_labels}'\n"
            "# The ops/runner-cloud-init.sh will run and bootstrap the runner\n"
            "bash -lc " + json.dumps(cloud_text)
        )
        with open(tmp_path, 'w') as f:
            f.write(ud_text)
        ud = ud_text.encode('utf-8')
    elif args.dry_widen:
        # Preview mode: do not attempt GH token generation or inline cloud-init; create a tiny placeholder UD
        ud_text = "#!/bin/bash\necho 'preview-only user-data'\n"
        with open(tmp_path, 'w') as f:
            f.write(ud_text)
        os.chmod(tmp_path, 0o644)
        ud = ud_text.encode('utf-8')
    else:
        # Preferred path: generate registration token in-python and inline cloud-init
        if not gh_pat:
            print("GITHUB_PAT required for non-dry runs")
            sys.exit(2)
        # obtain a fresh registration token
        reg_token = get_github_reg_token(owner, repo, gh_pat)

        # load cloud-init script and escape it safely for embedding
        cloud_init_path = repo_root / "ops" / "runner-cloud-init.sh"
        if cloud_init_path.exists():
            cloud_text = cloud_init_path.read_text()
        else:
            cloud_text = "# <missing ops/runner-cloud-init.sh>"

        runner_labels = args.runner_labels
        repo_url = f"https://github.com/{owner}/{repo}"

        # Render a small wrapper script that exports the token and then runs the cloud-init content
        # We avoid shell heredocs that require complex escaping by writing a minimal launcher
        ud_lines = ["#!/bin/bash", "export REG_TOKEN_B64=$(echo -n $REG_TOKEN | base64)"]
        # Use json.dumps to produce a safely-escaped quoted string for shell embedding
        ud_lines.append(f"export REG_TOKEN={json.dumps(reg_token)}")
        ud_lines.append(f"export REPO_URL={json.dumps(repo_url)}")
        ud_lines.append(f"export RUNNER_LABELS={json.dumps(runner_labels)}")
        # ensure SUDO_USER is defined to avoid 'set -u' failures in cloud-init
        ud_lines.append("export SUDO_USER='ubuntu'")
        ud_lines.append("set -euo pipefail")
        # If we generated an ephemeral SSH public key, install it early so the
        # provider's SSH proxy will see it as soon as possible. Install for both
        # the ubuntu user and root to increase the chance of successful auth.
        if ssh_public_key:
            inject_lines = [
                "# Inject ephemeral SSH public key for debugging (early)",
                "echo '[DEBUG] injecting ephemeral ssh public key'",
                "mkdir -p /home/ubuntu/.ssh /root/.ssh",
                f"echo {json.dumps(ssh_public_key)} >> /home/ubuntu/.ssh/authorized_keys",
                f"echo {json.dumps(ssh_public_key)} >> /root/.ssh/authorized_keys",
                "chown -R ubuntu:ubuntu /home/ubuntu/.ssh || true",
                "chmod 700 /home/ubuntu/.ssh || true",
                "chmod 600 /home/ubuntu/.ssh/authorized_keys || true",
                "chmod 700 /root/.ssh || true",
                "chmod 600 /root/.ssh/authorized_keys || true",
                "echo '[DEBUG] ephemeral-ssh-key-installed'",
            ]
            ud_lines.extend(inject_lines)
        ud_lines.append("# Begin inlined ops/runner-cloud-init.sh")
        ud_lines.extend(cloud_text.splitlines())
        ud_text = "\n".join(ud_lines) + "\n"
        with open(tmp_path, 'w') as f:
            f.write(ud_text)
        os.chmod(tmp_path, 0o755)
        ud = ud_text.encode('utf-8')

    ud_b64 = base64.b64encode(ud).decode('ascii')
    if len(ud_b64) > 1_000_000:
        print(f"User-data too large to embed ({len(ud_b64)} bytes) — aborting")
        sys.exit(2)

    print(f"User-data size: {len(ud)} bytes (base64 {len(ud_b64)} chars)")

    if args.dry:
        print("Dry run complete. User-data generated and validated.")
        return

    if args.simulate:
        # Quick simulated end-to-end flow for fast local testing
        print("SIMULATE: Running fast local simulation of provisioning flow (no remote calls)")
        fake_inst_id = 999900 + int(time.time() % 1000)
        print(f"SIMULATE: Would create instance with label prefix 'e2e-' and user-data size {len(ud)} bytes -> simulated instance id {fake_inst_id}")
        print("SIMULATE: Waiting briefly for instance to reach running (simulated)")
        time.sleep(1)
        print(f"SIMULATE: Instance {fake_inst_id} state=running")
        print("SIMULATE: Waiting briefly for GitHub runner registration (simulated)")
        # simulate runner registration quickly
        time.sleep(1)
        print(f"SIMULATE: Runner registered: id=sim-{fake_inst_id} name=sim-runner-{fake_inst_id}")
        print(f"SIMULATE: Dispatching workflow {args.workflow} on branch {args.branch} (simulated)")
        time.sleep(1)
        print("SIMULATE: Workflow finished: conclusion=success")
        return

    # Cleanup helper: list or destroy instances matching a label prefix and optional age
    def cleanup_instances(vast_api_key, label_prefix='e2e-', destroy=False, age_min=0):
        try:
            insts = list_instances(vast_api_key)
        except Exception as e:
            print('Failed to list instances for cleanup:', e)
            return 1
        now = time.time()
        matched = []
        for i in insts:
            try:
                lid = str(i.get('label') or '')
                iid = int(i.get('id') or i.get('instance_id') or 0)
                start = float(i.get('start_date') or 0)
            except Exception:
                continue
            if not lid:
                continue
            if label_prefix and not lid.startswith(label_prefix):
                continue
            age_min_actual = (now - start) / 60.0 if start > 0 else 0
            if age_min and age_min_actual < age_min:
                continue
            matched.append((iid, lid, age_min_actual))

        if not matched:
            print('No instances matched cleanup criteria')
            return 0

        print(f"Matched {len(matched)} instances for label_prefix='{label_prefix}' age_min={age_min}min")
        for iid, lid, age in matched:
            print(f" - id={iid} label={lid} age_min={age:.1f}")
        if destroy:
            for iid, lid, age in matched:
                try:
                    print(f'Destroying instance {iid} (label={lid})...')
                    vast_req('DELETE', f'/instances/{iid}/', vast_api_key)
                    print('  OK')
                except Exception as e:
                    print(f'  Failed to destroy {iid}: {e}')
            print('Cleanup destroy complete')
        return 0

    # Helper: save debug artifacts for a failing instance before deletion
    def preserve_debug_artifacts(vast_api_key, inst_id, out_dir_base=None):
        """Save instance JSON, decode RUNNER_USER_DATA_B64 to a file, and try to fetch provider console endpoints.
        Returns the path to the directory containing artifacts.
        """
        try:
            info = show_instance(vast_api_key, inst_id)
        except Exception as e:
            print(f"preserve_debug_artifacts: failed to fetch instance {inst_id}: {e}")
            return None
        now_ts = int(time.time())
        out_base = out_dir_base or Path("/tmp") / f"vast-debug-{inst_id}-{now_ts}"
        try:
            out_base = Path(out_base)
            out_base.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"preserve_debug_artifacts: failed to create out dir {out_base}: {e}")
            return None
        # Save raw instance JSON
        try:
            inst_file = out_base / f"instance_{inst_id}.json"
            inst_file.write_text(json.dumps(info, indent=2))
            print(f"Saved instance JSON -> {inst_file}")
        except Exception as e:
            print(f"Failed to write instance JSON: {e}")

        # Extract and decode RUNNER_USER_DATA_B64 if present
        try:
            inst = info.get('instances', info)
            env = inst.get('env') or inst.get('extra_env') or inst.get('environment') or {}
            if isinstance(env, dict):
                # Prefer saving a small URL pointer when present
                ud_url = env.get('RUNNER_USER_DATA_URL')
                if ud_url:
                    try:
                        url_path = out_base / f"instance_{inst_id}_RUNNER_USER_DATA_URL.txt"
                        url_path.write_text(str(ud_url))
                        print(f"Saved RUNNER_USER_DATA_URL -> {url_path}")
                    except Exception as e:
                        print(f"Failed to save RUNNER_USER_DATA_URL: {e}")

                ud_b64 = env.get('RUNNER_USER_DATA_B64') or env.get('RUNNER_USER_DATA')
                if ud_b64:
                    ud_path = out_base / f"instance_{inst_id}_RUNNER_USER_DATA.sh"
                    try:
                        ud_bytes = base64.b64decode(ud_b64)
                        ud_path.write_bytes(ud_bytes)
                        print(f"Saved decoded RUNNER_USER_DATA_B64 -> {ud_path}")
                    except Exception as e:
                        # if it's not base64 (already decoded), write raw
                        try:
                            ud_path.write_text(str(ud_b64))
                            print(f"Saved RUNNER_USER_DATA (raw) -> {ud_path}")
                        except Exception:
                            print(f"Failed to save runner user-data: {e}")
        except Exception as e:
            print(f"Error extracting RUNNER_USER_DATA_B64: {e}")

        # Try several console endpoints (best-effort)
        console_paths = [
            f"/instances/{inst_id}/console/",
            f"/instances/{inst_id}/console",
            f"/instances/{inst_id}/serial/",
            f"/instances/{inst_id}/console_raw/",
            f"/instances/{inst_id}/console_raw",
        ]
        for cp in console_paths:
            try:
                print(f"Attempting to fetch provider console: {cp}")
                creq = vast_req('GET', cp, vast_api_key)
                outf = out_base / (cp.replace('/', '_').strip('_') + '.txt')
                try:
                    # vast_req returns dict or raw text wrapper; try to serialize
                    if isinstance(creq, dict):
                        outf.write_text(json.dumps(creq, indent=2))
                    else:
                        outf.write_text(str(creq))
                    print(f"Saved console output -> {outf}")
                except Exception as e:
                    print(f"Failed to write console output for {cp}: {e}")
            except Exception as e:
                print(f"Console fetch {cp} failed: {e}")

        # Heuristic: scan saved console files for base64-wrapped bootstrap logs
        # (markers: BEGIN-BASE64-BOOTSTRAP-LOG / END-BASE64-BOOTSTRAP-LOG, or
        # FALLBACK-BEGIN-BASE64-BOOTSTRAP-LOG / FALLBACK-END-BASE64-BOOTSTRAP-LOG,
        # or [early] BEGIN-BASE64-BOOTSTRAP-LOG / [early] END-BASE64-BOOTSTRAP-LOG)
        try:
            for f in out_base.iterdir():
                if not f.is_file():
                    continue
                text = None
                try:
                    text = f.read_text(errors='ignore')
                except Exception:
                    continue
                # patterns to look for
                markers = [
                    ('BEGIN-BASE64-BOOTSTRAP-LOG','END-BASE64-BOOTSTRAP-LOG'),
                    ('FALLBACK-BEGIN-BASE64-BOOTSTRAP-LOG','FALLBACK-END-BASE64-BOOTSTRAP-LOG'),
                    ('[early] BEGIN-BASE64-BOOTSTRAP-LOG','[early] END-BASE64-BOOTSTRAP-LOG'),
                ]
                for start_m, end_m in markers:
                    if start_m in text and end_m in text:
                        try:
                            # extract the first base64 block between markers
                            start_ix = text.index(start_m) + len(start_m)
                            end_ix = text.index(end_m, start_ix)
                            b64_blob = text[start_ix:end_ix].strip()\
                                .replace('\n','').replace('\r','')
                            if not b64_blob:
                                continue
                            # attempt to decode
                            try:
                                decoded = base64.b64decode(b64_blob)
                            except Exception:
                                # not valid base64; skip
                                continue
                            out_log = out_base / f"instance_{inst_id}_bootstrap_log_from_console.log"
                            out_log.write_bytes(decoded)
                            print(f"Decoded bootstrap log from console markers in {f} -> {out_log}")
                            # stop after first successful decode
                            raise StopIteration
                        except StopIteration:
                            break
                        except Exception as e:
                            print(f"Failed to decode base64 from {f}: {e}")
        except StopIteration:
            pass
        except Exception as e:
            print('Error scanning console files for base64 logs:', e)

        # If we generated an ephemeral SSH key earlier, attempt to SSH and fetch the bootstrap log
        try:
            global ssh_private_key_path
        except Exception:
            ssh_private_key_path = None
        try:
            if ssh_private_key_path:
                # Look for ssh info in instance metadata
                ssh_host = inst.get('ssh_host') or inst.get('ssh') or inst.get('public_ipaddr') or inst.get('ip')
                ssh_port = inst.get('ssh_port') or inst.get('ssh_port4') or inst.get('port') or inst.get('machine_dir_ssh_port')
                if ssh_host:
                    sk = Path(ssh_private_key_path)
                    if sk.exists():
                        out_file = out_base / f"instance_{inst_id}_bootstrap_log_from_ssh.log"
                        print(f"Attempting SSH fetch of /var/log/runner-bootstrap.log from {ssh_host}:{ssh_port} using key {ssh_private_key_path}")
                        # Try scp first (simpler), then fall back to ssh cat if scp fails.
                        scp_cmd = [
                            'scp', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-i', str(sk)
                        ]
                        if ssh_port:
                            scp_cmd += ['-P', str(int(ssh_port))]
                        scp_cmd += [f"ubuntu@{ssh_host}:/var/log/runner-bootstrap.log", str(out_file)]
                        tried = False
                        try:
                            subprocess.check_call(scp_cmd, timeout=30)
                            print(f"Fetched bootstrap log via scp -> {out_file}")
                            tried = True
                        except Exception as e:
                            print('SSH/SCP fetch failed (continuing):', e)
                        if not tried:
                            # Try ssh and cat, with multiple usernames and a few retries
                            for user in ('ubuntu', 'root'):
                                for attempt in range(3):
                                    ssh_cmd = [
                                        'ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-i', str(sk)
                                    ]
                                    if ssh_port:
                                        ssh_cmd += ['-p', str(int(ssh_port))]
                                    ssh_cmd += [f"{user}@{ssh_host}", 'cat /var/log/runner-bootstrap.log']
                                    try:
                                        print(f"Trying ssh cat as {user}@{ssh_host} (attempt {attempt+1})")
                                        out = subprocess.check_output(ssh_cmd, timeout=20, stderr=subprocess.STDOUT)
                                        out_file.write_bytes(out)
                                        print(f"Fetched bootstrap log via ssh -> {out_file} (user={user})")
                                        tried = True
                                        break
                                    except Exception as e:
                                        print(f"ssh cat attempt failed for {user}@{ssh_host}: {e}")
                                        time.sleep(2)
                                if tried:
                                    break
                        if not tried:
                            print('SSH fetch attempts exhausted (continuing)')
        except Exception as e:
            print('SSH fetch attempt raised error (continuing):', e)

        return str(out_base)

    # If the user requested cleanup, perform it and exit
    if args.cleanup:
        if not vast_api_key:
            dotenv = repo_root / '.env'
            if dotenv.exists():
                loaded = load_dotenv(dotenv)
                vast_api_key = vast_api_key or loaded.get('VAST_API_KEY')
        if not vast_api_key:
            print('VAST_API_KEY required for cleanup operations; set it in environment or .env')
            sys.exit(2)
        do_destroy = args.cleanup == 'destroy'
        rc = cleanup_instances(vast_api_key, label_prefix=args.cleanup_label, destroy=do_destroy, age_min=args.cleanup_age_min)
        sys.exit(rc)

    # Determine image to request. By default we prefer a known-good Ubuntu cloud
    # image to avoid minimized/container-like images that lack cloud-init. This
    # behavior is configurable via environment variables or CLI --image.
    # - If --image is provided, it overrides everything.
    # - If VAST_ALWAYS_USE_UBUNTU=1 (default), we request VAST_DEFAULT_IMAGE (default ubuntu:22.04).
    # - To opt-out and keep previous behavior, set VAST_ALWAYS_USE_UBUNTU=0 in env.
    # Note: some providers will pre-pull the requested image; this was previously
    # avoided, but requesting a known cloud-init-enabled image increases the
    # chance that our inlined user-data executes correctly.
    owner = owner or os.getenv('GITHUB_OWNER') or os.getenv('IMAGE_REPO_OWNER') or os.getenv('IMAGE_REPO') or 'mike21043'
    repo_name = args.repo or os.getenv('IMAGE_REPO') or 'transcript-coach-saas'
    default_image = None
    if args.image:
        default_image = args.image
    else:
        # Respect existing FORCE_INSTANCE_IMAGE for backwards compatibility
        force_img = os.getenv('FORCE_INSTANCE_IMAGE') == '1'
        # New env var to prefer Ubuntu images (default ON). Set to '0' to disable.
        always_ubuntu = os.getenv('VAST_ALWAYS_USE_UBUNTU', '1')
        if force_img or (str(always_ubuntu) == '1'):
            default_image = os.getenv('VAST_DEFAULT_IMAGE', 'ubuntu:22.04')

    # Search offers
    offers = search_offers(vast_api_key)
    if not offers:
        print("No offers found")
        sys.exit(2)
    # Client-side enforcement: prefer US machines and enforce budget cap
    allowed = [c.strip().lower() for c in os.getenv('VAST_ALLOWED_COUNTRIES', 'US').split(',') if c.strip()]
    price_cap = float(os.getenv('priceInstanceHourlyMax') or os.getenv('PRICE_INSTANCE_HOURLY_MAX') or '0.30')
    # Countries to explicitly exclude (common high-risk/blocked locations)
    exclude = [c.strip().lower() for c in os.getenv('VAST_EXCLUDE_COUNTRIES', 'CN,CHN,PRC,CHINA').split(',') if c.strip()]

    def country_ok(o):
        mc = str(o.get('machine_country') or o.get('country') or o.get('geolocation') or '').lower()
        if not mc:
            return False
        if allowed and not any(a in mc for a in allowed):
            return False
        return True

    def price_ok(o):
        try:
            p = float(o.get('price_hour_usd') or o.get('price') or o.get('price_usd') or 0)
        except Exception:
            p = 0
        return p <= price_cap

    filtered_offers = [o for o in offers if country_ok(o) and price_ok(o)]

    # Progressive fallback policy: if nothing matches strict filters, gradually widen the price cap
    # and then allowed countries in a controlled manner. Configurable via env vars.
    PRICE_STEP = float(os.getenv('PRICE_STEP', '0.10'))
    PRICE_MAX_FALLBACK = float(os.getenv('PRICE_MAX_FALLBACK', str(price_cap + 0.40)))
    # comma-separated additional countries to consider when widening (e.g. 'CA,US'). Default empty = no country expansion.
    FALLBACK_COUNTRIES = [c.strip().lower() for c in os.getenv('VAST_ALLOWED_COUNTRIES_FALLBACK', '').split(',') if c.strip()]

    def try_widen_offers(offers, dry_widen=False):
        # try stepping price upward
        current = price_cap
        while current < PRICE_MAX_FALLBACK:
            current = round(current + PRICE_STEP, 2)
            def price_ok_dynamic(o):
                try:
                    p = float(o.get('price_hour_usd') or o.get('price') or o.get('price_usd') or 0)
                except Exception:
                    p = 1e9
                return p <= current
            cand = [o for o in offers if country_ok(o) and price_ok_dynamic(o)]
            if dry_widen:
                print(f'[widen-price] cap ${current}/hr -> {len(cand)} candidates')
                # print a short summary of the top 5 candidates including GPU name and CUDA
                for o in sorted(cand, key=lambda x: float(x.get('price_hour_usd') or x.get('price') or x.get('dph_total') or 1e9))[:5]:
                    gpu_name = o.get('gpu_name') or o.get('gpu_model') or o.get('gpu') or o.get('machine_gpu')
                    print(f"  id={o.get('id')} price={o.get('price_hour_usd') or o.get('price') or o.get('dph_total')} gpu={o.get('num_gpus')} gpu_name={gpu_name} cuda_max_good={o.get('cuda_max_good')} loc={o.get('machine_country') or o.get('geolocation')}")
            if cand:
                print(f'Found {len(cand)} offers by increasing price cap to ${current}/hr')
                return cand
        # try expanding allowed countries slightly (append fallback list)
        if FALLBACK_COUNTRIES:
            expanded_allowed = allowed + FALLBACK_COUNTRIES
            def country_ok_wide(o):
                mc = str(o.get('machine_country') or o.get('country') or o.get('geolocation') or '').lower()
                if not mc:
                    return False
                for ex in exclude:
                    if ex and ex in mc:
                        return False
                return any(a in mc for a in expanded_allowed)
            cand = [o for o in offers if country_ok_wide(o) and price_ok(o)]
            if dry_widen:
                    print(f'[widen-countries] expanded to {expanded_allowed} -> {len(cand)} candidates')
                    for o in sorted(cand, key=lambda x: float(x.get('price_hour_usd') or x.get('price') or x.get('dph_total') or 1e9))[:5]:
                        gpu_name = o.get('gpu_name') or o.get('gpu_model') or o.get('gpu') or o.get('machine_gpu')
                        print(f"  id={o.get('id')} price={o.get('price_hour_usd') or o.get('price') or o.get('dph_total')} gpu={o.get('num_gpus')} gpu_name={gpu_name} cuda_max_good={o.get('cuda_max_good')} loc={o.get('machine_country') or o.get('geolocation')}")
            if cand:
                print(f'Found {len(cand)} offers by expanding allowed countries to: {expanded_allowed}')
                return cand
        return []

    if not filtered_offers:
        print(f"No offers in allowed countries {allowed} under price ${price_cap}/hr; attempting progressive fallback")
        filtered_offers = try_widen_offers(offers, dry_widen=args.dry_widen)
    if not filtered_offers:
        print(f"Progressive fallback did not find acceptable offers; aborting to avoid unexpected costs")
        sys.exit(2)
    # If running in dry-widen (preview) mode, print a concise summary of the
    # selected candidate offers and exit without creating any instances. This
    # ensures preview runs are read-only against the Vast API.
    if args.dry_widen:
        def summarize(o):
            price = get_offer_price(o)
            price_str = f"${price:.3f}" if price is not None else 'n/a'
            gpu_name = o.get('gpu_name') or o.get('gpu_model') or o.get('gpu') or o.get('machine_gpu')
            return f"id={o.get('id')} price={price_str} gpus={o.get('num_gpus')} gpu_name={gpu_name} cuda_max_good={o.get('cuda_max_good')} loc={o.get('machine_country') or o.get('geolocation')} image={o.get('image') or o.get('machine_image') or ''}"

        print('\nDry-widen preview results: top candidate offers (no instance will be created)')
        # sort by price where unknown prices are considered high
        def sort_key(x):
            p = get_offer_price(x)
            return p if p is not None else 1e9

        for o in sorted(filtered_offers, key=sort_key)[:10]:
            print('  ' + summarize(o))
        print('\nTo create an instance, re-run without --dry-widen and with the appropriate flags.')
        return
    # prefer offers with exact GPU count match as a secondary key
    desired_gpus = int(os.getenv('VAST_NUM_GPUS', '1'))
    def score_with_gpu(o):
        # reuse pick_best helpers: price then perf
        def extract_price(o):
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

        def extract_perf(o):
            for k in ('dlperf_usd', 'dlperf_usd_per_hour', 'dlperf_usd_per_sec'):
                try:
                    v = o.get(k)
                    if v is not None:
                        return float(v)
                except Exception:
                    continue
            return 1e9

        p = extract_price(o)
        perf = extract_perf(o)
        try:
            g = int(o.get('num_gpus') or 0)
        except Exception:
            g = 0
        gpu_mismatch = 0 if g == desired_gpus else 1
        return (p, gpu_mismatch, perf)

    # If the caller explicitly provided an ask id, prefer that instead of auto-selecting
    if args.ask_id:
        ask_id = int(args.ask_id)
        # Try to find the matching offer in the filtered list
        best = None
        for o in filtered_offers:
            try:
                if int(o.get('id') or 0) == ask_id:
                    best = o
                    break
            except Exception:
                continue
        # If not found locally, attempt to fetch the ask details from the API
        if not best:
            try:
                best_resp = vast_req('GET', f'/asks/{ask_id}/', vast_api_key)
                # vast_req may return the offer directly or wrapped; try to normalize
                if isinstance(best_resp, dict) and best_resp.get('offer'):
                    best = best_resp.get('offer')
                else:
                    best = best_resp
            except Exception:
                best = None
        # Determine price for the requested ask
        price_of_best = get_offer_price(best) if best else None
        print(f"Requested ask id: {ask_id} price=${price_of_best:.4f}" if price_of_best else f"Requested ask id: {ask_id} (price unknown)")
    else:
        best = sorted(filtered_offers, key=score_with_gpu)[0]
        ask_id = int(best['id'])
        # Ensure selected offer has a determinable non-zero price (safety)
        price_of_best = get_offer_price(best)
    if price_of_best is None:
        print('Selected offer has no determinable price; aborting to avoid unexpected costs')
        sys.exit(2)
    print(f"Selected ask id: {ask_id} price=${price_of_best:.4f}")
    # Enforce client-side price cap (defensive): abort if selected offer exceeds the configured cap.
    try:
        price_cap = float(os.getenv('priceInstanceHourlyMax') or os.getenv('PRICE_INSTANCE_HOURLY_MAX') or os.getenv('VAST_PRICE_INSTANCE_HOURLY_MAX') or '0.30')
    except Exception:
        price_cap = 0.30
    if price_of_best > price_cap and not args.confirm_high_price:
        print(f"Selected offer price ${price_of_best:.4f} exceeds configured price cap ${price_cap:.2f}/hr. Aborting to avoid unexpected charges.")
        print("To override and proceed anyway, re-run with --confirm-high-price (only do this if you accept the higher cost).")
        print("Alternatively set PRICE_INSTANCE_HOURLY_MAX or priceInstanceHourlyMax to a higher value in your environment and re-run.")
        sys.exit(2)
    if price_of_best > price_cap and args.confirm_high_price:
        print(f"Warning: proceeding despite price ${price_of_best:.4f} > configured cap ${price_cap:.2f}/hr because --confirm-high-price was provided")
    # Print which field provided the price where possible for diagnostics
    price_src = None
    for k in ('price_hour_usd','price','price_usd','discounted_hourly'):
        if k in best and best.get(k) and float(best.get(k)) > 1e-8:
            price_src = k
            break
    if not price_src:
        if best.get('search') and best['search'].get('totalHour'):
            price_src = 'search.totalHour'
        elif best.get('dph_total'):
            price_src = 'dph_total'
    print(f'Price derived from: {price_src or "unknown"}')

    # Create instance. Instead of a single attempt, iterate candidate offers
    # (sorted by price/perf) and try each in turn. Some asks may be rented
    # between selection and creation and return transient errors (e.g. "GPU
    # conflict: requesting already rented gpus {id}"). In that case we should
    # try the next candidate rather than aborting entirely.
    create_kwargs = {}
    if args.no_template:
        # create_instance expects template_hash arg; pass None by omitting template in body
        create_kwargs['template_hash'] = None
    else:
        create_kwargs['template_hash'] = template_hash

    # Build sorted candidate list we can iterate
    candidates = sorted(filtered_offers, key=score_with_gpu)
    # If the user requested a specific ask id, prioritize it first
    if args.ask_id:
        requested = int(args.ask_id)
        candidates = [c for c in candidates if int(c.get('id') or 0) == requested] + [c for c in candidates if int(c.get('id') or 0) != requested]

    res = None
    last_err = None
    for cand in candidates:
        try:
            ask_try = int(cand.get('id'))
        except Exception:
            continue
        price_try = get_offer_price(cand)
        print(f"Attempting create with ask {ask_try} price=${price_try:.4f}" if price_try else f"Attempting create with ask {ask_try} (price unknown)")
        try:
            if create_kwargs.get('template_hash') is None:
                res = create_instance(vast_api_key, ask_try, None, ud_b64, args.runner_labels, disk_gb=args.disk, image=default_image)
            else:
                res = create_instance(vast_api_key, ask_try, create_kwargs['template_hash'], ud_b64, args.runner_labels, disk_gb=args.disk, image=default_image)
            # success
            ask_id = ask_try
            break
        except RuntimeError as e:
            last_err = e
            msg = str(e).lower()
            print(f"Create failed for ask {ask_try}: {e}")
            # If the ask disappeared between search and create (common transient
            # provider condition), treat it as transient and try the next
            # candidate instead of aborting the whole run.
            if 'no_such_ask' in msg or 'no such ask' in msg or 'not available' in msg:
                print(f"Ask {ask_try} no longer available (transient); trying next candidate")
                continue
            # If our probe flagged the instance as minimized, try the next candidate
            if 'minimized_image_detected' in msg:
                print(f"Detected minimized image during create with ask {ask_try}; moving to next candidate")
                continue
            # Transient conditions: GPU conflict / already rented; try next candidate
            if 'gpu conflict' in msg or 'already rented' in msg or 'rented gpus' in msg:
                print('Transient GPU conflict detected; trying next candidate')
                continue
            # Some Vast API variants require the 'image' field to be a string; if
            # the server complained about 'image must be a str' we previously
            # retried once with a safe public image (ubuntu) to satisfy
            # validation. However, if the caller explicitly requested a
            # template_hash we must NOT silently override that intent by
            # retrying with an image. In that case skip the fallback so the
            # template path remains honored.
            if ('image must be a str' in msg or ('invalid args' in msg and 'image' in msg)):
                # If a template hash was explicitly requested, skip the image
                # fallback because that would override the template selection.
                if create_kwargs.get('template_hash') is not None:
                    print(f"API complained about image args for ask {ask_try}, and a template_hash was requested; skipping fallback to an explicit image to preserve template usage")
                    # try next candidate rather than retrying with an image
                    continue
                try:
                    safe_image = os.getenv('FALLBACK_INSTANCE_IMAGE') or 'ubuntu:22.04'
                    print(f"Retrying ask {ask_try} with fallback image {safe_image} to satisfy API")
                    if create_kwargs.get('template_hash') is None:
                        res = create_instance(vast_api_key, ask_try, None, ud_b64, args.runner_labels, disk_gb=args.disk, image=safe_image)
                    else:
                        res = create_instance(vast_api_key, ask_try, create_kwargs['template_hash'], ud_b64, args.runner_labels, disk_gb=args.disk, image=safe_image)
                    ask_id = ask_try
                    break
                except Exception as e2:
                    print(f"Retry with fallback image failed for ask {ask_try}: {e2}")
                    last_err = e2
                    continue
            # For other errors, surface immediately
            raise
    if res is None:
        print('Failed to create an instance from any candidate asks')
        if last_err:
            print('Last error:', last_err)
        # Optionally pivot to a "container-on-plain-VM" approach: request a
        # known cloud-init-enabled image (e.g. ubuntu:22.04) and instruct
        # cloud-init to install Docker and pull the runner container.
        # This is a conservative fallback when provider-side templates cause
        # validation errors (image must be a str / NoneType). Controlled by
        # environment variable VAST_ALLOW_PIVOT_TO_PLAIN_VM=1.
        allow_pivot = os.getenv('VAST_ALLOW_PIVOT_TO_PLAIN_VM', '') in ('1', 'true', 'True')
        if allow_pivot:
            print('VAST_ALLOW_PIVOT_TO_PLAIN_VM enabled: attempting pivoted create pass (no template, explicit image, force docker)')
            # ensure we have a default_image to request; fall back to ubuntu
            pivot_image = default_image or os.getenv('FALLBACK_INSTANCE_IMAGE') or 'ubuntu:22.04'
            # perform one pass over candidates trying create without template_hash
            for cand in candidates:
                try:
                    ask_try = int(cand.get('id'))
                except Exception:
                    continue
                price_try = get_offer_price(cand)
                print(f"Pivot attempt: creating ask {ask_try} with explicit image={pivot_image} price=${price_try:.4f}" if price_try else f"Pivot attempt: creating ask {ask_try} with explicit image={pivot_image} (price unknown)")
                try:
                    # Set env FORCE_INSTALL_DOCKER so create_instance will include INSTALL_DOCKER and TARGET_AGENT_IMAGE
                    os.environ['FORCE_INSTALL_DOCKER'] = '1'
                    res = create_instance(vast_api_key, ask_try, None, ud_b64, args.runner_labels, disk_gb=args.disk, image=pivot_image)
                    ask_id = ask_try
                    break
                except Exception as e:
                    print(f"Pivot create failed for ask {ask_try}: {e}")
                    last_err = e
                    continue
            if res is None:
                print('Pivot attempts failed as well; aborting')
                if last_err:
                    print('Last pivot error:', last_err)
                sys.exit(2)
        else:
            sys.exit(2)
    newc = res.get('new_contract')
    if isinstance(newc, dict):
        inst_id = newc.get('instance_id')
    elif isinstance(newc, int):
        inst_id = newc
    else:
        inst_id = None
    if not inst_id:
        insts = list_instances(vast_api_key)
        inst_id = int(sorted(insts, key=lambda i: i.get('id',0), reverse=True)[0]['id'])
    print(f"Created instance id: {inst_id}")

    # Probe the instance for minimized image indicators. If we detect a minimized
    # image that likely lacks cloud-init, preserve debug artifacts (if asked)
    # and destroy the instance so we can try the next candidate.
    try:
        # Wait a short time for console/ssh to become available before probing
        time.sleep(5)
        is_min = probe_instance_is_minimized(vast_api_key, inst_id)
        if is_min:
            print(f"Instance {inst_id} appears to be a minimized image; aborting this instance and trying next candidate")
            try:
                if args.preserve_debug:
                    d = preserve_debug_artifacts(vast_api_key, inst_id)
                    print(f'Preserved debug artifacts at: {d}')
            except Exception as e:
                print(f"Failed to preserve debug artifacts before destroying minimized instance: {e}")
            try:
                vast_req('DELETE', f'/instances/{inst_id}/', vast_api_key)
                print(f"Destroyed minimized instance {inst_id}")
            except Exception as e:
                print(f"Failed to destroy minimized instance {inst_id}: {e}")
            # Try next candidate by returning to the caller with a control signal
            # We'll raise a custom exception to signal the outer loop to continue.
            raise RuntimeError('MINIMIZED_IMAGE_DETECTED')
    except RuntimeError as re:
        if str(re) == 'MINIMIZED_IMAGE_DETECTED':
            # Let the outer candidate loop try the next candidate
            pass
        else:
            print(f"Probe raised runtime error: {re}")
    except Exception as e:
        print(f"Probe check encountered error (continuing): {e}")

    # Best-effort: attach the ephemeral SSH public key to the instance so the
    # provider-side SSH proxy accepts it immediately. Some Vast deployments
    # require attaching rather than relying on account registration.
    try:
        ssh_pub = globals().get('ssh_public_key')
        if ssh_pub and vast_api_key:
            try:
                attach_resp = vast_req('POST', f'/instances/{inst_id}/ssh/', vast_api_key, {'ssh_key': ssh_pub})
                print(f'Attached ephemeral SSH key to instance {inst_id}: {attach_resp}')
            except Exception as e:
                print(f'Failed to attach ephemeral SSH key to instance {inst_id} (continuing): {e}')
    except Exception:
        pass

    # Wait for running (respect CLI timeout)
    print(f"Waiting up to {args.instance_wait_secs}s for instance to become running")
    if not wait_until_running(vast_api_key, inst_id, timeout_sec=args.instance_wait_secs):
        print("Instance failed to become ready within timeout")
        if args.auto_destroy_on_failure:
            print('Auto-destroy enabled; destroying instance', inst_id)
            try:
                if args.preserve_debug:
                    d = preserve_debug_artifacts(vast_api_key, inst_id)
                    print(f'Preserved debug artifacts at: {d}')
                vast_req('DELETE', f'/instances/{inst_id}/', vast_api_key, None)
                # Remove ephemeral account SSH key if we registered one
                try:
                    key_id = globals().get('vast_ephemeral_ssh_key_id')
                    if key_id:
                        vast_req('DELETE', f'/ssh/{key_id}/', vast_api_key, None)
                        print(f'Deleted ephemeral Vast account SSH key id={key_id}')
                except Exception as e:
                    print('Failed to delete ephemeral Vast SSH key (continuing):', e)
            except Exception as e:
                print('Failed to destroy instance:', e)
        else:
            # If we're not auto-destroying but the caller requested debug preservation,
            # save artifacts so the user can inspect the instance that failed to become
            # ready. We do not destroy the instance.
            if args.preserve_debug:
                try:
                    d = preserve_debug_artifacts(vast_api_key, inst_id)
                    print(f'Preserved debug artifacts at: {d}')
                except Exception as e:
                    print(f'Failed to preserve debug artifacts: {e}')
        sys.exit(2)

    # Wait for GitHub runner to appear
    print("Waiting for GitHub runner registration (label search)...")
    print(f"Waiting up to {args.runner_wait_secs}s for GitHub runner registration")
    deadline = time.time() + args.runner_wait_secs
    found_runner = None
    while time.time() < deadline:
        runners = list_repo_runners(owner, repo, gh_pat)
        for r in runners:
            labels = [l['name'] for l in r.get('labels', [])]
            if any(lbl in labels for lbl in args.runner_labels.split(',')) and r.get('status') == 'online':
                found_runner = r
                break
        if found_runner:
            break
        print("Runner not yet registered/online; sleeping 10s...")
        time.sleep(10)

    if not found_runner:
        print("Runner did not register within timeout")
        if args.auto_destroy_on_failure:
            print('Auto-destroy enabled; destroying instance', inst_id)
            try:
                if args.preserve_debug:
                    d = preserve_debug_artifacts(vast_api_key, inst_id)
                    print(f'Preserved debug artifacts at: {d}')
                vast_req('DELETE', f'/instances/{inst_id}/', vast_api_key, None)
                # Remove ephemeral account SSH key if we registered one
                try:
                    key_id = globals().get('vast_ephemeral_ssh_key_id')
                    if key_id:
                        vast_req('DELETE', f'/ssh/{key_id}/', vast_api_key, None)
                        print(f'Deleted ephemeral Vast account SSH key id={key_id}')
                except Exception as e:
                    print('Failed to delete ephemeral Vast SSH key (continuing):', e)
            except Exception as e:
                print('Failed to destroy instance:', e)
        else:
            # Not auto-destroying: optionally preserve debug artifacts for later
            if args.preserve_debug:
                try:
                    d = preserve_debug_artifacts(vast_api_key, inst_id)
                    print(f'Preserved debug artifacts at: {d}')
                except Exception as e:
                    print(f'Failed to preserve debug artifacts: {e}')
        sys.exit(2)

    print(f"Runner registered: id={found_runner.get('id')} name={found_runner.get('name')}")

    # Dispatch workflow
    print(f"Dispatching workflow {args.workflow} on branch {args.branch}")
    dispatch_workflow(owner, repo, args.workflow, args.branch, gh_pat)

    conclusion, run = wait_for_run_completion(owner, repo, args.workflow, timeout_sec=3600, token=gh_pat)
    print(f"Workflow finished: conclusion={conclusion}")
    if conclusion != 'success' and args.auto_destroy_on_failure:
        print('Workflow failed or timed out; auto-destroying instance', inst_id)
        try:
            if args.preserve_debug:
                d = preserve_debug_artifacts(vast_api_key, inst_id)
                print(f'Preserved debug artifacts at: {d}')
            vast_req('DELETE', f'/instances/{inst_id}/', vast_api_key, None)
            # Remove ephemeral account SSH key if we registered one
            try:
                key_id = globals().get('vast_ephemeral_ssh_key_id')
                if key_id:
                    vast_req('DELETE', f'/ssh/{key_id}/', vast_api_key, None)
                    print(f'Deleted ephemeral Vast account SSH key id={key_id}')
            except Exception as e:
                print('Failed to delete ephemeral Vast SSH key (continuing):', e)
        except Exception as e:
            print('Failed to destroy instance:', e)
    if run:
        print(json.dumps(run, indent=2))

if __name__ == '__main__':
    main()
