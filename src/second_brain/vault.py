"""Publish only gate-approved windows; never overwrite a human-edited note."""

from __future__ import annotations
import hashlib
import json
import os
import tempfile
from pathlib import Path
from .io import utc_from_ms


class NoteConflict(RuntimeError):
    pass


def _destination(vault: Path, folder: str):
    if not vault.is_dir():
        raise FileNotFoundError("Configured Obsidian vault is unavailable")
    root = vault.resolve()
    dest = (root / folder).resolve()
    if not dest.is_relative_to(root):
        raise ValueError("Vault output escapes the configured vault")
    return root, dest


def _frontmatter(front: dict) -> list[str]:
    # JSON scalars are valid YAML and cannot inject new frontmatter keys.
    return ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items()] + ["---", ""]


def _write_note(path: Path, dest: Path, content: bytes) -> None:
    if path.resolve().parent != dest:
        raise ValueError("Note path escapes output directory")
    if path.exists():
        if path.read_bytes() != content:
            raise NoteConflict("A generated note was edited; existing content was preserved")
        return
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


def publish(vault: Path, folder: str, job: dict, manifest: dict, audio_retained: bool = True) -> list[str]:
    root, dest = _destination(vault, folder)
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
        front = {
            "type": "omi-conversation",
            "source_id": job["id"],
            "window_id": window_id,
            "recorded_at": metadata.get("first_utc", "unknown-time"),
            "device": metadata["device"],
            "source_sha256": job["sha256"],
            **({"audio": Path(job["audio"]).as_uri()} if audio_retained else {}),
            "start_seconds": window["start"],
            "end_seconds": window["end"],
            "asr_engine": window["final_engine"],
            "conversation_type": window.get("memory_gate", {}).get("conversation_type", "other"),
            "speaker_identities": window.get("speaker_identities", {}),
        }
        lines = _frontmatter(front)
        lines += [
            f"# Omi conversation · {front['recorded_at']}",
            "",
            # The recording is deleted after processing unless retention is configured; never link a missing file.
            (f"[Source audio]({front['audio']})" if audio_retained else "Audio deleted after processing")
            + f" · {window['start']:.1f}–{window['end']:.1f} seconds",
            "",
            "Unnamed speaker labels are local to this recording/chunk. Named speakers include confirmation or voice-match provenance in the note properties.",
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
        _write_note(path, dest, "\n".join(lines).encode("utf-8"))
        written.append(str(path.relative_to(root)))
    return written


HIGHLIGHT_KINDS = ("decision", "task", "commitment", "date_or_event")


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def publish_meeting(vault: Path, folder: str, job: dict, manifest: dict) -> list[str]:
    """One note per meeting capture: highlights first, then the whole transcript. The memory gate
    never filters a meeting; its findings only choose what is highlighted. Meeting audio is
    retained (Omi dedupe needs it), so the note links it."""
    root, dest = _destination(vault, folder)
    metadata = json.loads(job["metadata"])
    windows = [w for w in manifest["windows"] if w.get("final_transcript", "").strip()]
    identities = {k: v for w in windows for k, v in w.get("speaker_identities", {}).items()}
    front = {
        "type": "omi-meeting",
        "source_id": job["id"],
        "capture_id": metadata["capture_id"],
        "app": metadata.get("app", "unknown"),
        "started_at": utc_from_ms(metadata["start_ms"]),
        "ended_at": utc_from_ms(metadata["end_ms"]),
        "source_sha256": job["sha256"],
        "audio": Path(job["audio"]).as_uri(),
        "participants": sorted({v["name"] for v in identities.values()}),
        "speaker_identities": identities,
    }
    highlights = []
    for w in windows:
        contains = w.get("memory_gate", {}).get("contains", {})
        kinds = [k.replace("_", " ") for k in HIGHLIGHT_KINDS if contains.get(k)]
        if kinds:
            highlights.append(f"- {_clock(w['start'])}–{_clock(w['end'])} · {', '.join(kinds)}")
    lines = _frontmatter(front) + [
        f"# Meeting · {front['app']} · {front['started_at']}",
        "",
        f"[Source audio]({front['audio']}) · L = microphone, R = meeting app",
        "",
        "Unnamed speaker labels are local to this capture. The microphone channel is the owner.",
        "",
        "## Highlights",
        "",
        *(highlights or ["No decisions, tasks or dates detected."]),
        "",
        "## Transcript",
        "",
    ]
    for w in windows:
        lines += [f"### {_clock(w['start'])}", "", w["final_transcript"].strip(), ""]
    if not windows:
        lines += ["No speech was transcribed.", ""]
    path = dest / f"{job['id']}.md"
    _write_note(path, dest, "\n".join(lines).encode("utf-8"))
    return [str(path.relative_to(root))]
