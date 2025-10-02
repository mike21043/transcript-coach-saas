#!/usr/bin/env python3
"""
Inspect Vast.ai offers with North America preference.
Prints top offers and useful fields to help select a safe host (country, gpu_model, price, os, template details).

Requires VAST_API_KEY in env or in repo .env file.
"""
import os, sys, json
from pathlib import Path
import requests

VAST_BASE = "https://console.vast.ai/api/v0"

def load_dotenv(repo_root: Path):
    dot = repo_root / '.env'
    env = {}
    if not dot.exists():
        return env
    for line in dot.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        k,v = line.split('=',1)
        v = v.strip().strip('"').strip("'")
        env[k.strip()] = v
    return env


def api_headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}


def vast_search_offers(token, min_gpus=1, only_verified=True):
    preferred_num_gpus = int(os.getenv('VAST_NUM_GPUS', str(min_gpus)))
    q = {"rentable": {"eq": True}, "rented": {"eq": False}, "num_gpus": {"gte": preferred_num_gpus}}
    if only_verified:
        q["verified"] = {"eq": True}
    # The Vast API may reject some server-side filters; we'll perform country and price filtering client-side
    r = requests.put(VAST_BASE + '/search/asks/', headers=api_headers(token), json={"q": q}, timeout=60)
    r.raise_for_status()
    return r.json().get('offers', [])


def get_offer_price(o):
    # Robust extraction of an offer's hourly price. Return None if unknown.
    # Check common top-level fields first, then nested 'search' and 'instance' dicts,
    # then known dph/dph_total fields.
    keys = ('price_hour_usd', 'price', 'price_usd', 'discounted_hourly', 'discounted_dph_total')
    for k in keys:
        if k in o and o.get(k) is not None:
            try:
                v = float(o.get(k))
                # treat zero or negative as missing so we can fall back to computed fields
                if v > 1e-8:
                    return v
            except Exception:
                continue
    s = o.get('search') or {}
    for k in ('totalHour', 'gpuCostPerHour', 'total_hour'):
        if k in s and s.get(k) is not None:
            try:
                return float(s.get(k))
            except Exception:
                pass
    inst = o.get('instance') or {}
    if inst.get('totalHour') is not None:
        try:
            return float(inst.get('totalHour'))
        except Exception:
            pass
    for k in ('dph_total', 'dph_total_adj', 'dph_base', 'discounted_hourly'):
        if k in o and o.get(k) is not None:
            try:
                return float(o.get(k))
            except Exception:
                pass
    return None


def score(o):
    # Prefer lower absolute hourly price first, then dlperf_usd as tiebreaker
    def get_price(o):
        for k in ('price_hour_usd','price','price_usd'):
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

    def get_perf(o):
        for k in ('dlperf_usd','dlperf_usd_per_sec','dlperf_usd_per_hour'):
            try:
                v = o.get(k)
                if v is not None:
                    return float(v)
            except Exception:
                continue
        return 1e9

    return (get_price(o), get_perf(o))


def print_offer(o):
    keys = ['id','ask_id','num_gpus','gpu_model','machine_country','machine_city','os','version','is_offer_verified','is_offer_compatible','is_host_secure','template_hash']
    out = {k: o.get(k) for k in keys if k in o}
    # include a canonical price field for readability
    price = get_offer_price(o)
    out['hourly_price_usd'] = price if price is not None else 'n/a'
    print(json.dumps(out, indent=2))
    # print some status-related fields if present
    extras = {}
    for k in ['disk','image','label','vendor','instance_type']:
        if k in o:
            extras[k]=o.get(k)
    if extras:
        print('extras:', extras)
    # print a short snippet of the raw offer for debug
    raw = json.dumps(o)
    if len(raw) > 2000:
        print('raw_offer_snippet:', raw[:2000])
    else:
        print('raw_offer:', raw)


def main():
    repo_root = Path(__file__).resolve().parents[1]
    env = load_dotenv(repo_root)
    token = os.getenv('VAST_API_KEY') or env.get('VAST_API_KEY')
    if not token:
        print('VAST_API_KEY not found in env or .env')
        sys.exit(2)
    offers = vast_search_offers(token)
    if not offers:
        print('No offers returned')
        return
    # allow env override
    allowed = [c.strip().lower() for c in os.getenv('VAST_ALLOWED_COUNTRIES', 'US').split(',') if c.strip()]
    exclude = [c.strip().lower() for c in os.getenv('VAST_EXCLUDE_COUNTRIES', 'CN,CHN,PRC,CHINA').split(',') if c.strip()]
    # budget cap (client-side)
    try:
        price_cap = float(os.getenv('priceInstanceHourlyMax') or os.getenv('PRICE_INSTANCE_HOURLY_MAX') or '0.30')
    except Exception:
        price_cap = 0.30

    def ok(o):
        mc = str(o.get('machine_country') or o.get('country') or o.get('geolocation') or '').lower()
        if not mc:
            return True
        for ex in exclude:
            if ex and ex in mc:
                return False
        if allowed:
            return any(a in mc for a in allowed)
        return True

    filtered = [o for o in offers if ok(o)]
    # apply CUDA and GPU count preferences
    min_cuda = float(os.getenv('VAST_MIN_CUDA', '12.9'))
    preferred_num_gpus = int(os.getenv('VAST_NUM_GPUS', '1'))

    def cuda_ok(o):
        try:
            cm = float(o.get('cuda_max_good') or 0.0)
        except Exception:
            cm = 0.0
        g = int(o.get('num_gpus') or 0)
        if cm < min_cuda:
            return False
        return g == preferred_num_gpus

    filtered = [o for o in filtered if cuda_ok(o)]
    # client-side country and budget enforcement
    def country_ok_offer(o):
        mc = str(o.get('machine_country') or o.get('country') or '').lower()
        if not mc:
            return False
        if allowed and not any(a.lower() in mc for a in allowed):
            return False
        for ex in exclude:
            if ex and ex in mc:
                return False
        return True

    def price_ok(o):
        p = get_offer_price(o)
        if p is None:
            # conservatively reject offers with unknown price
            return False
        return p <= price_cap

    filtered = [o for o in filtered if country_ok_offer(o) and price_ok(o)]
    print(f'Total offers: {len(offers)} | After allowed/exclude filter: {len(filtered)}')
    if not filtered:
        print(f'No offers matched strict country={allowed} and price<={price_cap}; aborting to avoid expensive/foreign hosts')
        return
    # prefer offers with exact GPU count match as a secondary key
    desired_gpus = int(os.getenv('VAST_NUM_GPUS', '1'))
    def score_with_gpu(o):
        p, perf = score(o)
        try:
            g = int(o.get('num_gpus') or 0)
        except Exception:
            g = 0
        gpu_mismatch = 0 if g == desired_gpus else 1
        return (p, gpu_mismatch, perf)
    sorted_offers = sorted(filtered, key=score_with_gpu)
    for i,o in enumerate(sorted_offers[:30], start=1):
        print('\n==== OFFER', i, '====')
        print_offer(o)

if __name__ == '__main__':
    main()
