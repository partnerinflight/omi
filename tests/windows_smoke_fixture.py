"""Prepare or drive an isolated Windows Service test; synthetic audio only."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from tests.helpers import configuration, records, upload
from second_brain.io import write_json

p = argparse.ArgumentParser()
p.add_argument("action", choices=["prepare", "upload"])
p.add_argument("--root", type=Path)
p.add_argument("--port", type=int, default=17331)
a = p.parse_args()
if a.action == "prepare":
    cfg = configuration(a.root)
    pipeline = json.loads(cfg.pipeline_config.read_text())
    fake_engine = a.root / "engine"
    fake_engine.mkdir()
    (fake_engine / "moss-transcribe.exe").write_text("Unused: smoke test uses the explicit fixture command")
    model = a.root / "fixture.gguf"
    model.write_text("Fixture; no model weights")
    pipeline.update(moss_cpp_engine_dir=str(fake_engine), moss_model=str(model))
    write_json(cfg.pipeline_config, pipeline)
else:
    asyncio.run(upload(a.port, records()))
