"""Publish only gate-approved windows; never overwrite a human-edited note."""

from __future__ import annotations
import hashlib
import json
import os
import tempfile
from pathlib import Path
from .io import atomic_write


class NoteConflict(RuntimeError):
    pass


def publish(vault: Path, folder: str, job: dict, manifest: dict) -> list[str]:
    if not vault.is_dir():
        raise FileNotFoundError("Configured Obsidian vault is unavailable")
    root = vault.resolve()
    dest = (root / folder).resolve()
    if not dest.is_relative_to(root):
        raise ValueError("Vault output escapes the configured vault")
    metadata = json.loads(job["metadata"])
    written = []
    for window in manifest["windows"]:
        if window.get("route_to_knowledge_router") is not True or window.get("memory_keep") is not True:
            continue
        text = window["final_transcript"].strip()
        if not text:
            raise ValueError("Kept window has no transcript")
        window_id = window["id"]
        if not isinstance(window_id, str) or not window_id.isalnum():
            raise ValueError("Invalid window ID")
        # Full job identity prevents collision even for unknown timestamps.
        path = dest / f"{job['id']}-{window_id}.md"
        if path.resolve().parent != dest:
            raise ValueError("Note path escapes output directory")
        front = {
            "type": "omi-conversation",
            "source_id": job["id"],
            "window_id": window_id,
            "recorded_at": metadata.get("first_utc", "unknown-time"),
            "device": metadata["device"],
            "source_sha256": job["sha256"],
            "audio": Path(job["audio"]).as_uri(),
            "start_seconds": window["start"],
            "end_seconds": window["end"],
            "asr_engine": window["final_engine"],
            "conversation_type": window.get("memory_gate", {}).get("conversation_type", "other"),
        }
        # JSON scalars are valid YAML and cannot inject new frontmatter keys.
        lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items()] + ["---", ""]
        lines += [
            f"# Omi conversation · {front['recorded_at']}",
            "",
            f"[Source audio]({front['audio']}) · {window['start']:.1f}–{window['end']:.1f} seconds",
            "",
            "Speaker labels are local to this recording/chunk, not identified people.",
            "",
            "## Transcript",
            "",
            text,
            "",
            "## Memory gate",
            "",
            str(window.get("memory_reason", "Durable content")),
            "",
        ]
        content = "\n".join(lines).encode("utf-8")
        if path.exists():
            if path.read_bytes() != content:
                raise NoteConflict("A generated note was edited; existing content was preserved")
        else:
            # Publication is atomic; a durable per-job manifest makes retries deterministic.
            dest.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=dest, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(content)
                    out.flush()
                    os.fsync(out.fileno())
                try:
                    if os.name == "nt":
                        os.rename(temporary, path)  # Windows rename refuses an existing destination
                    else:
                        os.link(temporary, path)  # POSIX rename would replace an existing note
                except FileExistsError:
                    if path.read_bytes() != content:
                        raise NoteConflict("A note appeared during publication; existing content was preserved")
            finally:
                Path(temporary).unlink(missing_ok=True)
        written.append(str(path.relative_to(root)))
    return written
