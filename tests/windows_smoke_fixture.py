"""Prepare or drive an isolated Windows Service test; synthetic audio only."""

import argparse
import asyncio
import json
import sys
import time
import uuid
import wave
from pathlib import Path
from tests.helpers import configuration, records, upload
from second_brain.io import write_json

p = argparse.ArgumentParser()
p.add_argument("action", choices=["prepare", "upload", "review", "verify-review"])
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
elif a.action == "upload":
    asyncio.run(upload(a.port, records()))
else:
    review = a.root / "machine/review"
    for _ in range(100):
        try:
            catalog = json.loads((review / "catalog.json").read_text())
            if catalog["speakers"]:
                break
        except FileNotFoundError:
            pass
        time.sleep(0.1)
    if a.action == "review":
        speaker = catalog["speakers"][0]
        with wave.open(str(review / "clips" / speaker["clips"][0]["file"])) as wav:
            assert wav.getnframes() > 0 and wav.getframerate() == 16000
        key = str(uuid.uuid4())
        write_json(
            review / "requests" / f"{key}.json",
            dict(id=key, action="assign", observation=speaker["id"], name="Fixture Person"),
        )
        response = review / "responses" / f"{key}.json"
        for _ in range(100):
            if response.exists():
                break
            time.sleep(0.1)
        assert json.loads(response.read_text())["ok"]
    else:
        assert any(s["name"] == "Fixture Person" and s["state"] == "confirmed" for s in catalog["speakers"])
