#!/usr/bin/env python3
"""Hourly local housekeeping; only the 06:00 Pacific tick wakes the agent."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def run_gate(now=None, runner=subprocess.run):
    now = now or datetime.now(ZoneInfo("America/Los_Angeles"))
    completed = runner(
        [sys.executable, str(Path(__file__).with_name("scrub-todos.py")),
         "--vault", "/home/eugene/second-brain"],
        check=True, capture_output=True, text=True, timeout=45,
    )
    cleanup = json.loads(completed.stdout)
    if cleanup.get("success") is not True:
        raise RuntimeError("ToDos housekeeping failed")
    if now.hour != 6:
        return {"wakeAgent": False}
    return {"wakeAgent": True, "context": {
        "local_date": now.date().isoformat(), "timezone": "America/Los_Angeles",
        "scheduled_time": "06:00", "todos_housekeeping": cleanup,
    }}


if __name__ == "__main__":
    print(json.dumps(run_gate()))
