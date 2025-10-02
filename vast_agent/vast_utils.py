"""Shared Vast.ai helpers extracted from the controller and harness.

Provides: vast_req, api_headers, get_offer_price, search_offers, pick_best_offer,
show_instance, list_instances, wait_until_running, fetch_console_and_decode
"""
from typing import Optional, List, Dict, Any
import os
import time
import json
import requests
import random
import base64
from pathlib import Path

VAST_BASE = os.getenv('VAST_BASE', 'https://cloud.vast.ai/api/v0')


def api_headers(key: str):
    return {"Authorization": f"Bearer {key}", "Accept": "application/json", "Content-Type": "application/json"}


def vast_req(method: str, path: str, token: str, body: Optional[dict] = None, timeout: int = 60) -> dict:
    url = f"{VAST_BASE}{path}"
    max_retries = int(os.getenv('VAST_REQ_MAX_RETRIES', '5'))
    backoff_base = float(os.getenv('VAST_REQ_BACKOFF_BASE', '1.0'))
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.request(method, url, headers=api_headers(token), json=body, timeout=timeout)
        except Exception as e:
            last_exc = e
            if attempt >= max_retries:
                raise RuntimeError(f"Vast request failed after {attempt} attempts: {e}") from e
            sleep = backoff_base * (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5)
            time.sleep(sleep)
            continue
        try:
            r.raise_for_status()
        except Exception as e:
            status = r.status_code
            msg = None
            try:
                msg = r.json()
            except Exception:
                msg = r.text
            if status == 429 or (500 <= status < 600):
                last_exc = RuntimeError(f"Vast API error {status}: {msg}")
                if attempt >= max_retries:
                    raise last_exc from e
                sleep = backoff_base * (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5)
                time.sleep(sleep)
                continue
            raise RuntimeError(f"Vast API error {status}: {msg}") from e
        try:
            return r.json()
        except Exception:
            return {"raw": r.text, "status": r.status_code}


def get_offer_price(o: dict) -> Optional[float]:
    for k in ('price_hour_usd', 'price', 'price_usd', 'discounted_hourly'):
        if k in o and o.get(k) is not None:
            try:
                v = float(o.get(k))
                if v > 1e-8:
                    return v
            except Exception:
                continue
    s = o.get('search') or {}
    if isinstance(s, dict) and s.get('totalHour') is not None:
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


def search_offers(vast_api_key: str, min_gpus: int = 1, only_verified: bool = True) -> List[dict]:
    q = {"rentable": {"eq": True}, "rented": {"eq": False}, "num_gpus": {"gte": min_gpus}}
    if only_verified:
        q['verified'] = {"eq": True}
    try:
        res = vast_req('PUT', '/search/asks/', vast_api_key, {'q': q})
        offers = res.get('offers', []) or res.get('matches', []) or []
    except Exception as e:
        # Retry with safe query
        res = vast_req('PUT', '/search/asks/', vast_api_key, {})
        offers = res.get('offers', []) or []

    # basic client-side country/exclude filtering (honor env)
    allowed = [c.strip().lower() for c in os.getenv('VAST_ALLOWED_COUNTRIES', '').split(',') if c.strip()]
    exclude = [c.strip().lower() for c in os.getenv('VAST_EXCLUDE_COUNTRIES', 'CN,CHN,PRC,CHINA').split(',') if c.strip()]

    def country_ok(o: dict) -> bool:
        mc = str(o.get('machine_country') or o.get('country') or o.get('geolocation') or '').lower()
        if not mc:
            return False
        for ex in exclude:
            if ex and ex in mc:
                return False
        if allowed:
            return any(a in mc for a in allowed)
        return True

    filtered = [o for o in offers if country_ok(o)]

    # model substring filtering optional
    gpu_model_env = os.getenv('VAST_GPU_MODEL', '')
    if gpu_model_env:
        models = [m.strip().lower() for m in gpu_model_env.split(',') if m.strip()]
        def model_ok(o: dict) -> bool:
            candidates = []
            for k in ('machine_gpu','gpu','gpu_name','gpu_model','machine_type','title','name','image','machine_image'):
                v = o.get(k)
                if v:
                    candidates.append(str(v).lower())
            s = o.get('search') or {}
            if isinstance(s, dict):
                for vv in s.values():
                    try:
                        candidates.append(str(vv).lower())
                    except Exception:
                        pass
            return any(any(m in c for m in models) for c in candidates if isinstance(c, str))
        filtered = [o for o in filtered if model_ok(o)]

    # min cuda filter
    try:
        min_cuda = float(os.getenv('VAST_MIN_CUDA', '0'))
    except Exception:
        min_cuda = 0.0
    def cuda_ok(o: dict) -> bool:
        try:
            cm = float(o.get('cuda_max_good') or 0.0)
        except Exception:
            cm = 0.0
        return cm >= min_cuda
    filtered = [o for o in filtered if cuda_ok(o)]

    # price cap
    price_env = os.getenv('priceInstanceHourlyMax') or os.getenv('PRICE_INSTANCE_HOURLY_MAX')
    try:
        price_cap = float(price_env) if price_env else None
    except Exception:
        price_cap = None
    if price_cap is not None:
        filtered = [o for o in filtered if (get_offer_price(o) is not None and get_offer_price(o) <= price_cap)]

    return filtered


def score_offer(o: dict) -> tuple:
    p = get_offer_price(o) or float('inf')
    try:
        g = int(o.get('num_gpus') or 0)
    except Exception:
        g = 0
    desired = int(os.getenv('VAST_NUM_GPUS') or 1)
    gpu_mismatch = 0 if g == desired else 1
    perf = float(o.get('dlperf_usd') or o.get('dlperf_usd_per_hour') or 1e9)
    return (p, gpu_mismatch, perf)


def pick_best_offer(offers: List[dict]) -> Optional[dict]:
    if not offers:
        return None
    return sorted(offers, key=score_offer)[0]


def show_instance(vast_api_key: str, inst_id: int) -> dict:
    return vast_req('GET', f'/instances/{inst_id}/', vast_api_key)


def list_instances(vast_api_key: str) -> List[dict]:
    return vast_req('GET', '/instances/', vast_api_key).get('instances', [])


def wait_until_running(vast_api_key: str, inst_id: int, timeout_sec: int = 900) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            info = show_instance(vast_api_key, inst_id)
            inst = info.get('instances', info)
            status = str(inst.get('cur_state') or inst.get('actual_status') or inst.get('intended_status') or inst.get('state') or '').lower()
            if any(s in status for s in ('open','running','active','connected')):
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def fetch_console_and_decode(vast_api_key: str, inst_id: int, out_dir: Optional[str] = None) -> Optional[str]:
    """Fetch several console endpoints and try to decode embedded base64 bootstrap logs.

    Returns path to artifact dir or None on failure.
    """
    try:
        info = show_instance(vast_api_key, inst_id)
    except Exception:
        return None
    out_base = Path(out_dir) if out_dir else Path('/tmp') / f'vast-debug-{inst_id}-{int(time.time())}'
    out_base.mkdir(parents=True, exist_ok=True)
    # save instance JSON
    (out_base / f'instance_{inst_id}.json').write_text(json.dumps(info, indent=2))

    console_paths = [f'/instances/{inst_id}/console/', f'/instances/{inst_id}/console', f'/instances/{inst_id}/serial/', f'/instances/{inst_id}/console_raw/', f'/instances/{inst_id}/console_raw']
    for cp in console_paths:
        try:
            creq = vast_req('GET', cp, vast_api_key)
            fname = cp.replace('/', '_').strip('_') + '.txt'
            p = out_base / fname
            try:
                if isinstance(creq, dict):
                    p.write_text(json.dumps(creq, indent=2))
                else:
                    p.write_text(str(creq))
            except Exception:
                try:
                    p.write_text(str(creq))
                except Exception:
                    pass
        except Exception:
            pass

    markers = [
        ('BEGIN-BASE64-BOOTSTRAP-LOG','END-BASE64-BOOTSTRAP-LOG'),
        ('FALLBACK-BEGIN-BASE64-BOOTSTRAP-LOG','FALLBACK-END-BASE64-BOOTSTRAP-LOG'),
        ('[early] BEGIN-BASE64-BOOTSTRAP-LOG','[early] END-BASE64-BOOTSTRAP-LOG'),
        ('[periodic] BEGIN-BASE64-BOOTSTRAP-LOG','[periodic] END-BASE64-BOOTSTRAP-LOG'),
    ]
    for f in out_base.iterdir():
        if not f.is_file():
            continue
        try:
            text = f.read_text(errors='ignore')
        except Exception:
            continue
        for start_m, end_m in markers:
            if start_m in text and end_m in text:
                try:
                    start_ix = text.index(start_m) + len(start_m)
                    end_ix = text.index(end_m, start_ix)
                    b64_blob = text[start_ix:end_ix].strip().replace('\n','').replace('\r','')
                    if not b64_blob:
                        continue
                    decoded = base64.b64decode(b64_blob)
                    out_log = out_base / f'instance_{inst_id}_bootstrap_decoded.log'
                    out_log.write_bytes(decoded)
                    return str(out_base)
                except Exception:
                    continue
    return str(out_base)
