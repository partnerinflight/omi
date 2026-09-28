#!/usr/bin/env python3
"""Shared local/CI test entry point; no physical device or model download."""

import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
receiver = root / "omi/firmware/scripts/omi-local"
env = os.environ.copy()
env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), str(receiver)])
for cwd in [root, receiver]:
    subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=cwd, env=env, check=True
    )
