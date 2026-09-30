"""Durable whole-file uploads from protocol-v2 file clients (meeting captures).

Layout under `root`:
  .captures/<id>.json   capture marker: state open | cancelled | closed
  <id>.partial          bytes received so far (fsynced before every ACK)
  <id>.partial.json     declared total_len / sha256 / metadata of the partial
  <id>.json, <id>.caf   committed sidecar and audio (sidecar is written first)

A file is only committed after its length and SHA-256 match what the client
declared. `committed()` is a hook for subclasses (e.g. ready receipts); it runs
after the audio is durable and again from `recover()`, so it must be idempotent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from .state import atomic_json
from .upload_protocol import UploadProtocolError


class FileMismatch(UploadProtocolError):
    """The client's bytes do not match what it declared; nothing is committed."""


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # directory handles cannot be fsynced on Windows
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


log = logging.getLogger("omi_local.file_store")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class FileStore:
    SUFFIX = ".caf"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # --- paths ---------------------------------------------------------------
    def _marker(self, cid: str) -> Path:
        return self.root / ".captures" / f"{cid}.json"

    def _partial(self, cid: str) -> Path:
        return self.root / f"{cid}.partial"

    def _expect(self, cid: str) -> Path:
        return self.root / f"{cid}.partial.json"

    def _audio(self, cid: str) -> Path:
        return self.root / f"{cid}{self.SUFFIX}"

    def _sidecar(self, cid: str) -> Path:
        return self.root / f"{cid}.json"

    # --- capture markers -----------------------------------------------------
    def _write_marker(self, cid: str, value: dict) -> None:
        path = self._marker(cid)
        atomic_json(path, value)
        _fsync_dir(path.parent)

    def capture_state(self, cid: str) -> dict | None:
        path = self._marker(cid)
        return _read(path) if path.exists() else None

    def open_capture(self, client: str, cid: str, start_ms: int, app: str) -> None:
        if self.capture_state(cid) is not None:
            return  # a repeated OPEN never reopens a cancelled or closed capture
        self._write_marker(cid, {"capture_id": cid, "client": client, "state": "open",
                                        "start_ms": start_ms, "app": app, "updated": time.time()})

    def cancel_capture(self, client: str, cid: str) -> None:
        current = self.capture_state(cid) or {"capture_id": cid, "client": client}
        if current.get("state") == "closed":
            return
        self._write_marker(cid, {**current, "state": "cancelled", "updated": time.time()})
        self._partial(cid).unlink(missing_ok=True)
        self._expect(cid).unlink(missing_ok=True)

    def _close_marker(self, client: str, cid: str, sidecar: dict) -> None:
        current = self.capture_state(cid) or {"capture_id": cid, "client": client}
        if current.get("state") == "closed":
            return
        self._write_marker(cid, {**current, "state": "closed", "start_ms": sidecar.get("start_ms"),
                                        "end_ms": sidecar.get("end_ms"), "app": sidecar.get("app"),
                                        "updated": time.time()})

    # --- file transfer -------------------------------------------------------
    def begin(self, client: str, cid: str, total_len: int, sha256: str, metadata: dict) -> int:
        """Return how many bytes of this file are already durable (0 = start over)."""
        audio = self._audio(cid)
        state = self.capture_state(cid)
        if state and state.get("state") == "cancelled":
            raise FileMismatch("capture was cancelled")
        if audio.exists():
            side = _read(self._sidecar(cid))
            if side.get("sha256") == sha256 and side.get("total_len") == total_len:
                return total_len
            raise FileMismatch("capture already committed with different content")
        partial, expect = self._partial(cid), self._expect(cid)
        if partial.exists() and expect.exists():
            declared = _read(expect)
            size = partial.stat().st_size
            if declared["total_len"] == total_len and declared["sha256"] == sha256 and size <= total_len:
                return size
        self.root.mkdir(parents=True, exist_ok=True)
        expect.unlink(missing_ok=True)  # never pair a new declaration with old bytes
        with open(partial, "wb") as f:
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(self.root)
        atomic_json(expect, {"total_len": total_len, "sha256": sha256, "metadata": metadata})
        return 0

    def append(self, cid: str, offset: int, data: bytes) -> int:
        """Append at exactly the durable end; return the new durable length."""
        partial, expect = self._partial(cid), self._expect(cid)
        if not (partial.exists() and expect.exists()):
            raise FileMismatch("FILE_DATA before FILE_BEGIN")
        size = partial.stat().st_size
        if offset != size:
            raise FileMismatch(f"FILE_DATA offset {offset}, expected {size}")
        if size + len(data) > _read(expect)["total_len"]:
            raise FileMismatch("FILE_DATA beyond declared length")
        with open(partial, "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return size + len(data)

    def commit(self, client: str, cid: str) -> Path:
        audio, partial, expect = self._audio(cid), self._partial(cid), self._expect(cid)
        if audio.exists() and not partial.exists():
            side = _read(self._sidecar(cid))
            self._close_marker(client, cid, side)
            self.committed(audio, side)
            return audio
        if not (partial.exists() and expect.exists()):
            raise FileMismatch("FILE_END before FILE_BEGIN")
        declared = _read(expect)
        digest = hashlib.sha256()
        with open(partial, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        size = partial.stat().st_size
        if size < declared["total_len"]:
            raise FileMismatch("FILE_END before all bytes were received")  # keep partial: resumable
        if size != declared["total_len"] or digest.hexdigest() != declared["sha256"]:
            partial.unlink(missing_ok=True)
            expect.unlink(missing_ok=True)
            raise FileMismatch("uploaded bytes do not match the declared length/SHA-256")
        side = {**declared["metadata"], "capture_id": cid, "client": client, "sha256": declared["sha256"],
                "total_len": declared["total_len"], "file": str(audio), "complete": True}
        atomic_json(self._sidecar(cid), side)
        _fsync_dir(self.root)
        os.replace(partial, audio)
        _fsync_dir(self.root)
        expect.unlink(missing_ok=True)
        self._close_marker(client, cid, side)
        self.committed(audio, side)
        return audio

    def committed(self, audio: Path, sidecar: dict) -> None:
        """Hook: called after a file is durable. Must be idempotent."""

    def recover(self) -> None:
        """Finish bookkeeping for files committed just before a crash."""
        if not self.root.exists():
            return
        for audio in self.root.glob(f"*{self.SUFFIX}"):
            sidecar = audio.with_suffix(".json")
            if not sidecar.exists():
                continue
            try:
                side = _read(sidecar)
                self._expect(side["capture_id"]).unlink(missing_ok=True)
                self._close_marker(side["client"], side["capture_id"], side)
                self.committed(audio, side)
            except (OSError, ValueError, KeyError):
                log.exception("recovery failed for %s", audio)
