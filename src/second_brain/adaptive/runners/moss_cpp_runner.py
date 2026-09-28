from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path


def normalize_segments(obj):
    candidates = obj
    if isinstance(obj, dict):
        for key in ("segments", "transcript", "items", "result"):
            if isinstance(obj.get(key), list):
                candidates = obj[key]
                break

    if not isinstance(candidates, list):
        raise ValueError("MOSS returned an unsupported segment schema")

    out = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        low = {str(k).lower(): v for k, v in item.items()}
        start = (
            low.get("start") if low.get("start") is not None else low.get("start_time", low.get("begin", low.get("t0")))
        )
        end = low.get("end") if low.get("end") is not None else low.get("end_time", low.get("finish", low.get("t1")))
        speaker = low.get("speaker", low.get("speaker_id", low.get("spk")))
        text = low.get("text", low.get("content", low.get("utterance", "")))
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            raise ValueError("MOSS segment has invalid timestamps")
        if start < 0 or end < start:
            raise ValueError("MOSS segment has invalid time range")
        out.append(
            {
                "start": start,
                "end": end,
                "speaker": str(speaker) if speaker is not None else "S?",
                "text": str(text).strip(),
            }
        )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--command-json", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--max-new", type=int, default=2048)
    args = ap.parse_args()

    out_path = Path(args.output)
    payload = {
        "engine": "moss-transcribe.cpp",
        "model": args.model,
        "audio": str(Path(args.audio).resolve()),
        "threads": args.threads,
    }

    try:
        env = os.environ.copy()
        env["MTD_DEVICE"] = "cpu"
        env["MTD_THREADS"] = str(args.threads)

        prefix = json.loads(args.command_json)
        if not isinstance(prefix, list) or not prefix or not all(isinstance(x, str) and x for x in prefix):
            raise ValueError("MOSS command must be a nonempty argument list")
        cmd = prefix + ["transcribe", args.model, args.audio, "--max-new", str(args.max_new), "--format", "json"]
        print("[MOSS] " + subprocess.list2cmdline(cmd), flush=True)
        started = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        elapsed = time.time() - started
        payload["generation_seconds"] = elapsed
        payload["return_code"] = proc.returncode
        payload["stderr"] = proc.stderr
        payload["raw_stdout"] = proc.stdout

        if proc.returncode != 0:
            raise RuntimeError(f"moss-transcribe exited with {proc.returncode}: {proc.stderr[-2000:]}")

        parsed = json.loads(proc.stdout)
        payload["result"] = parsed
        payload["segments"] = normalize_segments(parsed)
        payload["status"] = "ok"
        print(f"[MOSS] done in {elapsed:.1f}s; {len(payload['segments'])} turns", flush=True)

    except Exception as exc:
        payload["status"] = "error"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        payload["traceback"] = traceback.format_exc()
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        raise

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
