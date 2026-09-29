"""Bounded review clips and optional offline voice embeddings, inside the job process."""

from __future__ import annotations
from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
from .io import write_json

# Clips shorter than this, or whose turn text has no words ("...", ""), cannot identify a
# voice and are not worth listening to; they are left out of speaker review entirely.
MIN_REVIEW_SECONDS = 2.0
WORD = re.compile(r"\w")


def useful_clip(clip):
    return clip["end"] - clip["start"] >= MIN_REVIEW_SECONDS and bool(WORD.search(clip.get("text") or ""))


def candidates(manifest):
    groups = defaultdict(list)
    segments = []
    for window in manifest["windows"]:
        for index, source in enumerate(window.get("final_segments", window.get("moss_segments", []))):
            s = dict(source)
            label = str(s.get("speaker", "S?"))
            if label.endswith("S?"):
                label += ":" + window["id"] + ":" + str(index)
                s["speaker"] = label
                source["speaker"] = label
            start, end = float(s["start"]), float(s["end"])
            if not math.isfinite(start + end) or start < 0 or end <= start:
                continue
            s.update(start=start, end=end)
            segments.append(s)
    for s in segments:
        for index in range(min(5, math.ceil((s["end"] - s["start"]) / 12))):
            start = s["start"] + index * 12
            end = min(s["end"], start + 12)
            if not useful_clip(dict(start=start, end=end, text=s.get("text"))):
                continue
            overlaps = any(t["speaker"] != s["speaker"] and t["start"] < end and t["end"] > start for t in segments)
            clean = not overlaps and not s.get("approximate") and end - start >= 2 and "S?" not in s["speaker"]
            groups[s["speaker"]].append(
                dict(
                    start=start,
                    end=end,
                    text=s.get("text", ""),
                    eligible=clean,
                    quality="clean turn" if clean else "short, overlapping, unknown, or approximate turn",
                )
            )
    result = []
    for label, clips in groups.items():
        # Prefer clean and longer turns, retain several different utterances.
        unique = {(s["start"], s["end"]): s for s in clips}
        selected = sorted(unique.values(), key=lambda s: (not s["eligible"], -(s["end"] - s["start"])))[:5]
        result.append(dict(label=label, clips=sorted(selected, key=lambda s: s["start"])))
    return result


def prepare(manifest, audio: Path, directory: Path, cfg: dict):
    directory = directory / "speaker-clips"
    directory.mkdir(exist_ok=True)
    observations = candidates(manifest)
    for obs in observations:
        for index, clip in enumerate(obs["clips"]):
            name = hashlib.sha256(obs["label"].encode()).hexdigest()[:24] + f"-{index}.wav"
            output = directory / name
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    str(clip["start"]),
                    "-i",
                    str(audio),
                    "-t",
                    str(clip["end"] - clip["start"]),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(output),
                ],
                check=True,
                timeout=60,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            clip["path"] = str(output)
    manifest["speaker_observations"] = observations
    manifest["speaker_embedding_status"] = "not configured"
    model = cfg.get("speaker_model")
    python = cfg.get("speaker_python")
    if not model or not python:
        return
    request = directory / "request.json"
    output = directory / "embeddings.json"
    files = [s["path"] for obs in observations for s in obs["clips"] if s["eligible"]]
    if not files:
        manifest["speaker_embedding_status"] = "no clean turns"
        return
    write_json(request, files)
    runner = Path(__file__).parent / "adaptive" / "runners" / "speaker_encoder.py"
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    try:
        subprocess.run(
            [python, str(runner), "--model", model, "--input", str(request), "--output", str(output)],
            check=True,
            timeout=cfg.get("speaker_timeout_seconds", 300),
            env=env,
        )
        encoded = json.loads(output.read_text(encoding="utf-8"))
        for obs in observations:
            obs["model"] = encoded["model"]
            for clip in obs["clips"]:
                clip["embedding"] = encoded["vectors"].get(clip["path"])
        manifest["speaker_embedding_status"] = "ready"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # Review/playback still works; never pretend a name was voice-matched.
        manifest["speaker_embedding_status"] = "unavailable"
        manifest["fallbacks"].append(
            {"from": "voice recognition", "to": "manual speaker review", "reason": type(error).__name__}
        )
