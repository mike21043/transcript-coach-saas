#!/usr/bin/env python3
"""Fetch Vast instance console/serial outputs and save into repo debug/INSTANCEID/.

Usage: VAST_API_KEY=<key> python3 scripts/fetch_vast_console.py <instance_id>
"""
import os
import sys
import json
import requests
from pathlib import Path

VAST_BASE = "https://cloud.vast.ai/api/v0"

def api_headers(key):
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}

def fetch_and_save(instance_id: str, out_dir: Path, vast_api_key: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    endpoints = [
        f"/instances/{instance_id}/console/",
        f"/instances/{instance_id}/console",
        f"/instances/{instance_id}/console_raw/",
        f"/instances/{instance_id}/console_raw",
        f"/instances/{instance_id}/serial/",
        f"/instances/{instance_id}/serial",
    ]
    for ep in endpoints:
        url = VAST_BASE + ep
        #!/usr/bin/env python3
        """Wrapper CLI to fetch Vast console output and decode embedded bootstrap logs.

        Usage: VAST_API_KEY=<key> python3 scripts/fetch_vast_console.py <instance_id>
        """
        import os
        import sys
        from pathlib import Path

        from vast_agent.vast_utils import fetch_console_and_decode


        def main():
            if len(sys.argv) < 2:
                print("Usage: VAST_API_KEY=<key> python3 scripts/fetch_vast_console.py <instance_id>")
                sys.exit(2)
            inst = sys.argv[1]
            key = os.getenv('VAST_API_KEY')
            if not key:
                print('VAST_API_KEY env var is required')
                sys.exit(2)
            out = Path(__file__).resolve().parents[1] / 'debug' / f'vast-debug-{inst}'
            res = fetch_console_and_decode(key, int(inst), str(out))
            if res:
                print(f'Saved console artifacts to: {res}')
            else:
                print('Failed to fetch console artifacts')


        if __name__ == '__main__':
            main()
