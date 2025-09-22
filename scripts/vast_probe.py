#!/usr/bin/env python3
"""
Safe Vast API probe: reads VAST_API_KEY from .env or environment and queries a set of endpoints.
Prints token presence/length/prefix (not full token) and HTTP status + short response snippet.
"""
import os
from pathlib import Path
import requests

def read_env_token(env_path: Path):
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#'): continue
        if line.startswith('VAST_API_KEY='):
            v=line.split('=',1)[1].strip().strip('"').strip("'")
            return v
    return None

def probe(token):
    base='https://console.vast.ai'
    endpoints=['/api/v0/instances/','/api/v0/accounts/','/api/v0/users/me','/api/v0/auth/']
    print(f"Probing {len(endpoints)} endpoints on Vast (no secrets will be printed)")
    headers={'Authorization': f'Bearer {token}'} if token else {}
    for ep in endpoints:
        url=base+ep
        try:
            r=requests.get(url, headers=headers, timeout=10)
            status=r.status_code
            text=(r.text or '')[:500].replace('\n',' ')
            print('ENDPOINT', ep, 'STATUS', status)
            if text:
                print('  snippet:', text[:200])
        except Exception as e:
            print('ENDPOINT', ep, 'ERROR', str(e))

def main():
    repo_root=Path(__file__).resolve().parents[1]
    env_path=repo_root/'.env'
    token=os.getenv('VAST_API_KEY') or read_env_token(env_path)
    if token:
        print('VAST_API_KEY present: yes length=', len(token), 'prefix=', token[:6])
    else:
        print('VAST_API_KEY present: no')
    probe(token)

if __name__=='__main__':
    main()
