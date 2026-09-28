from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import traceback
from pathlib import Path

import torch
from huggingface_hub import hf_hub_download
from vibevoice.modular.modeling_vibevoice_asr import VibeVoiceASRForConditionalGeneration
from vibevoice.processor.audio_utils import load_audio_use_ffmpeg
from vibevoice.processor.vibevoice_asr_processor import VibeVoiceASRProcessor


def frame_config(model_path: str) -> dict:
    name = "preprocessor_config.json"
    path = os.path.join(model_path, name) if os.path.isdir(model_path) else hf_hub_download(model_path, name)
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

    sr = cfg["target_sample_rate"]
    frame_seconds = cfg["speech_tok_compress_ratio"] / sr
    return {
        "sample_rate": sr,
        "chunk_duration": cfg["chunk_frames"] * frame_seconds,
        "text_audio_delay": cfg["lookahead_frames"] * frame_seconds,
    }


def parse_chunk(text: str):
    s = text.strip()
    if not s:
        return []

    objs = []
    try:
        obj = json.loads(s)
        objs = obj if isinstance(obj, list) else [obj]
    except Exception:
        for m in re.finditer(r"\{[^{}]*\}", s):
            try:
                objs.append(json.loads(m.group(0)))
            except Exception:
                pass

    out = []
    for obj in objs:
        if not isinstance(obj, dict):
            continue
        d = {str(k).lower(): v for k, v in obj.items()}
        speaker = d.get("speaker") or d.get("speaker id") or d.get("speaker_id")
        value = d.get("content") or d.get("text")
        if speaker is not None or value is not None:
            out.append(
                {
                    "speaker": None if speaker is None else str(speaker),
                    "text": "" if value is None else str(value).strip(),
                }
            )

    return out or [{"speaker": None, "text": s}]


class Heartbeat:
    def __init__(self, label):
        self.label = label
        self.stop = threading.Event()
        self.started = None

    def __enter__(self):
        self.started = time.time()

        def loop():
            while not self.stop.wait(30):
                extra = ""
                if torch.cuda.is_available():
                    extra = (
                        f"; CUDA alloc={torch.cuda.memory_allocated()/1024**3:.2f}GiB"
                        f" reserved={torch.cuda.memory_reserved()/1024**3:.2f}GiB"
                    )
                print(f"[{self.label}] elapsed={(time.time()-self.started)/60:.1f} min{extra}", flush=True)

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=1)


def dtype_from_name(name):
    return {
        "float32": torch.float32,
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
    }[name]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--device", choices=["cuda", "cpu"], required=True)
    ap.add_argument("--dtype", choices=["float32", "float16", "bfloat16"], required=True)
    ap.add_argument("--max-new-tokens", type=int, default=256)
    args = ap.parse_args()

    jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    if not jobs:
        print("[Vibe 7B] no jobs", flush=True)
        return

    if args.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but torch.cuda.is_available() is False")

    dtype = dtype_from_name(args.dtype)
    fc = frame_config(args.model)

    print(f"[Vibe 7B] loading {args.model} on {args.device} as {dtype}", flush=True)
    with Heartbeat("Vibe 7B model load"):
        processor = VibeVoiceASRProcessor.from_pretrained(args.model)
        model = VibeVoiceASRForConditionalGeneration.from_pretrained(
            args.model,
            dtype=dtype,
            device_map=None,
            low_cpu_mem_usage=True,
            attn_implementation="sdpa",
            trust_remote_code=True,
        ).eval()
        model = model.to(args.device)

    print(f"[Vibe 7B] loaded; processing {len(jobs)} selected windows", flush=True)

    for job_num, job in enumerate(jobs, 1):
        output = Path(job["output"])
        hotwords = job.get("hotwords") or []
        context_info = ",".join(hotwords) if hotwords else None

        payload = {
            "engine": "vibevoice-asr-streaming-7b",
            "model": args.model,
            "audio": job["audio"],
            "window_id": job["window_id"],
            "device": args.device,
            "dtype": args.dtype,
            "context_start": job.get("context_start"),
            "context_end": job.get("context_end"),
            "hotwords": hotwords,
            "context_info": context_info,
        }

        try:
            audio, _ = load_audio_use_ffmpeg(job["audio"], resample=True, target_sr=fc["sample_rate"])
            duration = len(audio) / fc["sample_rate"]
            chunks = []
            segments = []
            started = time.time()

            print(
                f"[Vibe 7B {job_num}/{len(jobs)}] {job['window_id']} " f"({duration:.1f}s), hotwords={hotwords}",
                flush=True,
            )

            with Heartbeat(f"Vibe 7B {job['window_id']}"):
                for i, n, txt in model.streaming_generate(
                    audio_tensor=torch.from_numpy(audio),
                    tokenizer=processor.tokenizer,
                    chunk_duration=fc["chunk_duration"],
                    text_audio_delay=fc["text_audio_delay"],
                    sample_rate=fc["sample_rate"],
                    max_new_tokens_per_chunk=args.max_new_tokens,
                    temperature=0.0,
                    context_info=context_info,
                ):
                    parsed = parse_chunk(txt)
                    st = i * fc["chunk_duration"]
                    en = min(duration, (i + 1) * fc["chunk_duration"])

                    chunks.append(
                        {
                            "chunk_index": i,
                            "total_chunks": n,
                            "approx_start": st,
                            "approx_end": en,
                            "raw_text": txt,
                            "parsed": parsed,
                        }
                    )

                    for p in parsed:
                        segments.append(
                            {
                                "approx_start": st,
                                "approx_end": en,
                                "speaker": p["speaker"],
                                "text": p["text"],
                            }
                        )

                    print(f"[Vibe 7B {job['window_id']}] [{i+1}/{n}] {txt}", flush=True)

            payload.update(
                {
                    "status": "ok",
                    "generation_seconds": time.time() - started,
                    "duration_seconds": duration,
                    "chunks": chunks,
                    "segments": segments,
                    "raw_text": "".join(x["raw_text"] for x in chunks),
                }
            )

            if args.device == "cuda":
                payload["peak_vram_allocated_gb"] = round(torch.cuda.max_memory_allocated() / 1024**3, 2)

        except Exception as exc:
            payload["status"] = "error"
            payload["error"] = f"{type(exc).__name__}: {exc}"
            payload["traceback"] = traceback.format_exc()

        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
