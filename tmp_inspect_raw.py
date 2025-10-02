#!/usr/bin/env python3
import os, json
from pathlib import Path
import requests

VAST_BASE = "https://console.vast.ai/api/v0"

# load .env from repo root
repo_root = Path(__file__).resolve().parents[0]
dotenv = repo_root / '.env'
loaded = {}
if dotenv.exists():
    for line in dotenv.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k,v = line.split('=',1)
        v = v.strip().strip('"').strip("'")
        loaded[k.strip()] = v
VAST_API_KEY = os.getenv('VAST_API_KEY') or loaded.get('VAST_API_KEY')
if not VAST_API_KEY:
    print('VAST_API_KEY not found in env or .env')
    raise SystemExit(2)

headers = {"Authorization": f"Bearer {VAST_API_KEY}", "Accept": "application/json", "Content-Type": "application/json"}
q = {"rentable": {"eq": True}, "rented": {"eq": False}, "num_gpus": {"gte": 1}}
resp = requests.put(VAST_BASE + '/search/asks/', headers=headers, json={"q": q}, timeout=60)
resp.raise_for_status()
offers = resp.json().get('offers', [])
print(f'Total offers returned: {len(offers)}')
for i,o in enumerate(offers[:8], start=1):
    print('\n--- OFFER', i, 'id=', o.get('id'))
    print(json.dumps(o, indent=2)[:4000])

print('\nDone')
