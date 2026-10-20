#!/usr/bin/env python3
"""Describe a staged installer payload (installer/build-installer.ps1) in release-manifest.json.

Components follow from what was built into the payload. Each component names the pinned models the
installer downloads for it (repository, revision, files, SHA-256 from config/engines.lock.json).
An incomplete payload is refused, so a half-built installer is never produced."""

from __future__ import annotations
import argparse
import datetime as dt
import fnmatch
import json
import sys
from pathlib import Path

CORE_FILES = ["app/service/SecondBrain.Service.exe", "app/tray/SecondBrain.Tray.exe", "app/python/python.exe",
              "engines/moss-cpu/moss-transcribe.exe", "engines/ffmpeg/bin/ffmpeg.exe",
              "engines/ffmpeg/bin/ffprobe.exe", "scripts/install-core.ps1", "config/engines.lock.json"]
# component -> (folder that marks it present, file that must exist when it is)
OPTIONAL = {
    "gpu": ("engines/moss-cuda", "engines/moss-cuda/moss-transcribe.exe"),
    "voice": ("runtimes/voice", "runtimes/voice/python/python.exe"),
    "linking": ("engines/nemo-speech", "engines/nemo-speech/nemo-speech.exe"),
    "refinement": ("runtimes/vibe", "runtimes/vibe/python/python.exe"),
}
MODELS = {"moss_model": "core", "speaker_model": "voice", "diar_model": "linking", "vibe_model": "refinement"}
CORE_FOLDERS = ["app", "engines/moss-cpu", "engines/ffmpeg", "scripts", "config", "licenses"]


def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def verify_payload(payload: Path) -> list[str]:
    problems = [f"missing core file: {name}" for name in CORE_FILES if not (payload / name).is_file()]
    for component, (folder, required) in OPTIONAL.items():
        if (payload / folder).exists() and not (payload / required).is_file():
            problems.append(f"incomplete {component} component: {required} missing")
    return problems


def present(payload: Path) -> dict:
    found = {"core": not any(p.startswith("missing core") for p in verify_payload(payload))}
    for component, (folder, required) in OPTIONAL.items():
        found[component] = (payload / required).is_file()
    return found


def make_manifest(lock: dict, payload: Path, version: str) -> dict:
    found = present(payload)
    sizes = {"core": sum(_size(payload / f) for f in CORE_FOLDERS)}
    for component, (folder, _) in OPTIONAL.items():
        sizes[component] = _size(payload / folder)
    models = []
    for key, component in MODELS.items():
        if not found[component]:
            continue
        pin = lock[key]
        hashes = pin.get("sha256", {})
        models.append({"key": key, "component": component, "repo": pin["repo"], "revision": pin["revision"],
                       "patterns": pin["patterns"],
                       "files": [{"path": name, "sha256": digest} for name, digest in sorted(hashes.items())
                                 if any(fnmatch.fnmatch(name, p) for p in pin["patterns"])]})
    return {"version": version,
            "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "components": {c: {"present": found[c], "size_bytes": sizes[c]} for c in ["core", *OPTIONAL]},
            "models": models}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    problems = verify_payload(args.payload)
    if problems:
        print("Payload is incomplete:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    lock = json.loads(args.lock.read_text(encoding="utf-8-sig"))
    manifest = make_manifest(lock, args.payload, args.version)
    args.out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({c: v["present"] for c, v in manifest["components"].items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
