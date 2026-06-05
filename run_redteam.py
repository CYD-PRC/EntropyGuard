#!/usr/bin/env python3
"""
Entropy Runtime · Red Team Evolution Runner
Called by system crontab daily at 4:30
"""
import sys
import os
import json
from datetime import datetime

# Ensure project importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ENTROPY_RUNTIME_API_KEY", os.environ.get("ENTROPY_RUNTIME_API_KEY", ""))

from security.redteam_evolver import run_evolution

if __name__ == "__main__":
    print(f"[{datetime.now().isoformat()}] Redteam evolution START")
    try:
        result = run_evolution()
        summary = result.get("summary", {})
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"[{datetime.now().isoformat()}] Redteam evolution OK")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Redteam evolution FAILED: {e}")
        sys.exit(1)
