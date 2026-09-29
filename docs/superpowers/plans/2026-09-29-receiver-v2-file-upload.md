# Receiver v2 File Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an authenticated "file client" (the future Mac meeting-capture app) upload whole capture files to the existing Omi receiver, durably and resumably, without changing the v1 Omi device protocol.

**Architecture:** Protocol version 2 reuses the v1 framing and HMAC handshake, then switches to capture/file messages. A generic `FileStore` in `omi_local` owns durable partial files, SHA-256 verification, atomic commit and capture-marker state. `second_brain.receiver.MeetingStore` subclasses it to publish ready receipts into `incoming/meetings/.ready/` — a directory the current Omi discovery loop does not scan, so meetings are stored but not processed until Plan 2 (pipeline dedupe).

**Tech Stack:** Python 3.12, asyncio streams, `unittest` (no pytest), real loopback sockets.

Spec: `docs/superpowers/specs/2026-09-29-meeting-capture-design.md` §2. This is plan 1 of 3 (receiver v2 → pipeline dedupe → Mac app).

---

## Conventions for this plan

- Receiver package root: `omi/firmware/scripts/omi-local` (called **RX** below). Its tests run from RX:
  `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest <module> -v`
  (create the venv first if missing: `python3 -m venv .venv && .venv/bin/pip install -e .`).
- Pipeline tests run from the repo root:
  `PYTHONPATH=src:omi/firmware/scripts/omi-local omi/firmware/scripts/omi-local/.venv/bin/python -m unittest <module> -v`
- Full suite: `omi/firmware/scripts/omi-local/.venv/bin/python scripts/test.py` from the repo root.
- Both RX and the repo root have a package named `tests`; never import one suite's tests from the other.
- Wire integers are big-endian, like v1.

## File structure

| File | Responsibility |
|---|---|
| `RX/omi_local/upload_protocol.py` (modify) | v2 constants and pure codecs (no I/O) |
| `RX/omi_local/file_store.py` (create) | Durable per-capture file storage and capture markers |
| `RX/omi_local/server.py` (modify) | Shared `_authenticate`, v2 connection handler, `file_store` parameter |
| `RX/tests/test_file_protocol.py` (create) | Codec tests |
| `RX/tests/test_file_store.py` (create) | Storage unit tests |
| `RX/tests/test_file_upload.py` (create) | Loopback server tests with a fake file client |
| `src/second_brain/receiver.py` (modify) | `MeetingStore` publishing meeting receipts |
| `src/second_brain/runtime.py` (modify) | Construct/recover `MeetingStore`, pass it to `UploadServer` |
| `tests/helpers.py` (modify) | Minimal v2 client for pipeline tests |
| `tests/test_meeting_receiver.py` (create) | Receipt publication, recovery, runtime wiring |
| Docs (modify) | `upload_protocol.py` docstring, `src/second_brain/ARCHITECTURE.md`, `SYSTEM.md` |

---

### Task 1: v2 protocol codecs

**Files:**
- Modify: `RX/omi_local/upload_protocol.py`
- Test: `RX/tests/test_file_protocol.py`

- [ ] **Step 1: Write the failing test**

Create `RX/tests/test_file_protocol.py`:

```python
"""Protocol v2 (file client) codecs: pure encode/parse, no sockets."""

import unittest

from omi_local import upload_protocol as U

CID = bytes(range(16))


class FileProtocolCodecTests(unittest.TestCase):
    def test_hello_accepts_both_versions(self):
        nonce = bytes(16)
        v1 = U.parse_hello(U.encode_hello(b"\x01" * 6, nonce))
        v2 = U.parse_hello(U.encode_hello(b"\x02" * 6, nonce, U.VERSION_FILE))
        self.assertEqual((v1.version, v2.version), (U.VERSION, U.VERSION_FILE))
        with self.assertRaises(U.UploadProtocolError):
            U.parse_hello(U.MAGIC + bytes([3]) + b"\x00" * 22)

    def test_file_auth_is_tag_only(self):
        tag = bytes(range(32))
        self.assertEqual(U.parse_file_auth(tag).client_tag, tag)
        with self.assertRaises(U.UploadProtocolError):
            U.parse_file_auth(tag + b"\x00")

    def test_capture_open_roundtrip_and_limits(self):
        m = U.parse_capture_open(U.encode_capture_open(CID, 1_790_000_000_123, "us.zoom.xos"))
        self.assertEqual((m.capture_id, m.start_ms, m.app), (CID, 1_790_000_000_123, "us.zoom.xos"))
        with self.assertRaises(U.UploadProtocolError):
            U.parse_capture_open(CID + bytes(8) + b"a" * (U.MAX_APP_LEN + 1))
        with self.assertRaises(U.UploadProtocolError):
            U.parse_capture_open(CID + bytes(8) + b"\xff\xfe")
        with self.assertRaises(U.UploadProtocolError):
            U.parse_capture_open(CID)

    def test_capture_id_payload(self):
        self.assertEqual(U.parse_capture_id(CID), CID)
        with self.assertRaises(U.UploadProtocolError):
            U.parse_capture_id(CID[:15])

    def test_file_begin_roundtrip_and_limits(self):
        sha = bytes(range(32))
        b = U.parse_file_begin(U.encode_file_begin(CID, 12345, sha, {"app": "x", "start_ms": 1}))
        self.assertEqual((b.capture_id, b.total_len, b.sha256, b.metadata), (CID, 12345, sha, {"app": "x", "start_ms": 1}))
        head = CID + (5).to_bytes(8, "big") + sha
        with self.assertRaises(U.UploadProtocolError):
            U.parse_file_begin(head + b"[1]")  # metadata must be an object
        with self.assertRaises(U.UploadProtocolError):
            U.parse_file_begin(head + b"{")
        with self.assertRaises(U.UploadProtocolError):
            U.parse_file_begin(head + b" " * (U.MAX_METADATA_LEN + 1))

    def test_file_data_roundtrip_and_limits(self):
        d = U.parse_file_data(U.encode_file_data(4096, b"abc"))
        self.assertEqual((d.offset, d.data), (4096, b"abc"))
        with self.assertRaises(U.UploadProtocolError):
            U.parse_file_data(bytes(8) + bytes(U.MAX_FILE_CHUNK + 1))
        with self.assertRaises(U.UploadProtocolError):
            U.parse_file_data(bytes(7))

    def test_payload_bounds_cover_largest_messages(self):
        self.assertGreaterEqual(U.MAX_FILE_PAYLOAD, 8 + U.MAX_FILE_CHUNK)
        self.assertGreaterEqual(U.MAX_FILE_PAYLOAD, 16 + 8 + 32 + U.MAX_METADATA_LEN)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_file_protocol -v`
Expected: errors such as `AttributeError: module 'omi_local.upload_protocol' has no attribute 'VERSION_FILE'`.

- [ ] **Step 3: Implement the codecs**

In `RX/omi_local/upload_protocol.py`:

1. Add `import json` next to the existing imports.
2. After `VERSION = 1` add `VERSION_FILE = 2`.
3. After `MSG_REJECT = 0x7F` add:

```python
# Protocol v2 (file client, e.g. the Mac meeting-capture app)
MSG_CAPTURE_OPEN = 0x10
MSG_CAPTURE_CANCEL = 0x11
MSG_FILE_BEGIN = 0x12
MSG_FILE_DATA = 0x13
MSG_FILE_END = 0x14
MSG_FILE_START = 0x15
MSG_FILE_ACK = 0x16
MSG_FILE_BYE = 0x17
MSG_OK = 0x18

CAPTURE_ID_LEN = 16
SHA256_LEN = 32
MAX_APP_LEN = 256
MAX_METADATA_LEN = 4096
MAX_FILE_CHUNK = 64 * 1024
MAX_FILE_PAYLOAD = 8 + MAX_FILE_CHUNK  # largest v2 frame (FILE_DATA); FILE_BEGIN is smaller
```

4. Replace `parse_hello` and `encode_hello` with:

```python
def parse_hello(payload: bytes) -> Hello:
    if len(payload) != 4 + 1 + DEVICE_ID_LEN + NONCE_LEN or payload[:4] != MAGIC:
        raise UploadProtocolError("bad HELLO")
    ver = payload[4]
    if ver not in (VERSION, VERSION_FILE):
        raise UploadProtocolError(f"unsupported protocol version {ver}")
    return Hello(ver, bytes(payload[5:5 + DEVICE_ID_LEN]), bytes(payload[5 + DEVICE_ID_LEN:]))


def encode_hello(device_id: bytes, client_nonce: bytes, version: int = VERSION) -> bytes:
    return MAGIC + bytes([version]) + device_id + client_nonce
```

5. After `encode_data`, add:

```python
# --- protocol v2: file client -----------------------------------------------------
@dataclass(frozen=True)
class FileAuth:
    client_tag: bytes


def parse_file_auth(payload: bytes) -> FileAuth:
    if len(payload) != TAG_LEN:
        raise UploadProtocolError("bad v2 AUTH")
    return FileAuth(bytes(payload))


@dataclass(frozen=True)
class CaptureOpen:
    capture_id: bytes
    start_ms: int
    app: str


def encode_capture_open(capture_id: bytes, start_ms: int, app: str) -> bytes:
    return capture_id + struct.pack(">Q", start_ms) + app.encode("utf-8")


def parse_capture_open(payload: bytes) -> CaptureOpen:
    head = CAPTURE_ID_LEN + 8
    if len(payload) < head or len(payload) - head > MAX_APP_LEN:
        raise UploadProtocolError("bad CAPTURE_OPEN")
    try:
        app = bytes(payload[head:]).decode("utf-8")
    except UnicodeDecodeError as e:
        raise UploadProtocolError("CAPTURE_OPEN app is not UTF-8") from e
    (start_ms,) = struct.unpack_from(">Q", payload, CAPTURE_ID_LEN)
    return CaptureOpen(bytes(payload[:CAPTURE_ID_LEN]), start_ms, app)


def parse_capture_id(payload: bytes) -> bytes:
    if len(payload) != CAPTURE_ID_LEN:
        raise UploadProtocolError("expected 16-byte capture id")
    return bytes(payload)


@dataclass(frozen=True)
class FileBegin:
    capture_id: bytes
    total_len: int
    sha256: bytes
    metadata: dict


def encode_file_begin(capture_id: bytes, total_len: int, sha256: bytes, metadata: dict) -> bytes:
    return capture_id + struct.pack(">Q", total_len) + sha256 + json.dumps(metadata, sort_keys=True).encode("utf-8")


def parse_file_begin(payload: bytes) -> FileBegin:
    head = CAPTURE_ID_LEN + 8 + SHA256_LEN
    if len(payload) < head or len(payload) - head > MAX_METADATA_LEN:
        raise UploadProtocolError("bad FILE_BEGIN")
    try:
        metadata = json.loads(bytes(payload[head:]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise UploadProtocolError("FILE_BEGIN metadata is not JSON") from e
    if not isinstance(metadata, dict):
        raise UploadProtocolError("FILE_BEGIN metadata must be an object")
    (total_len,) = struct.unpack_from(">Q", payload, CAPTURE_ID_LEN)
    return FileBegin(bytes(payload[:CAPTURE_ID_LEN]), total_len,
                     bytes(payload[CAPTURE_ID_LEN + 8:head]), metadata)


@dataclass(frozen=True)
class FileData:
    offset: int
    data: bytes


def encode_file_data(offset: int, data: bytes) -> bytes:
    return struct.pack(">Q", offset) + data


def parse_file_data(payload: bytes) -> FileData:
    if len(payload) < 8 or len(payload) - 8 > MAX_FILE_CHUNK:
        raise UploadProtocolError("bad FILE_DATA")
    (offset,) = struct.unpack_from(">Q", payload, 0)
    return FileData(offset, bytes(payload[8:]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_file_protocol tests.test_upload -v`
Expected: all pass (the v1 `test_upload` suite still passes).

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/scripts/omi-local/omi_local/upload_protocol.py omi/firmware/scripts/omi-local/tests/test_file_protocol.py
git commit -m "receiver: add protocol v2 file-client codecs"
```

---

### Task 2: Durable `FileStore`

**Files:**
- Create: `RX/omi_local/file_store.py`
- Test: `RX/tests/test_file_store.py`

- [ ] **Step 1: Write the failing test**

Create `RX/tests/test_file_store.py`:

```python
"""FileStore: resumable partials, verified atomic commit, capture markers, recovery."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from omi_local.file_store import FileMismatch, FileStore

CID = "00112233445566778899aabbccddeeff"
DATA = bytes(range(256)) * 40  # 10 KiB
SHA = hashlib.sha256(DATA).hexdigest()
META = {"app": "us.zoom.xos", "start_ms": 1000, "end_ms": 61000, "channels": {"L": "mic", "R": "remote"}}


class RecordingStore(FileStore):
    def __init__(self, root):
        super().__init__(root)
        self.hooks = []

    def committed(self, audio, sidecar):
        self.hooks.append((audio.name, sidecar["capture_id"]))


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "meetings"
        self.store = RecordingStore(self.root)

    def upload(self, store=None, upto=len(DATA)):
        store = store or self.store
        offset = store.begin("AA-BB", CID, len(DATA), SHA, META)
        while offset < upto:
            offset = store.append(CID, offset, DATA[offset:min(offset + 4096, upto)])
        return offset

    def test_commit_writes_audio_sidecar_marker_and_calls_hook_once(self):
        self.upload()
        audio = self.store.commit("AA-BB", CID)
        self.assertEqual(audio, self.root / f"{CID}.caf")
        self.assertEqual(audio.read_bytes(), DATA)
        side = json.loads((self.root / f"{CID}.json").read_text())
        self.assertEqual((side["capture_id"], side["client"], side["sha256"], side["complete"]), (CID, "AA-BB", SHA, True))
        self.assertEqual(side["channels"], META["channels"])
        self.assertFalse((self.root / f"{CID}.partial").exists())
        self.assertFalse((self.root / f"{CID}.partial.json").exists())
        self.assertEqual(self.store.capture_state(CID)["state"], "closed")
        self.assertEqual(self.store.hooks, [(f"{CID}.caf", CID)])

    def test_begin_resumes_from_persisted_bytes(self):
        self.upload(upto=8192)
        fresh = RecordingStore(self.root)  # e.g. after a receiver restart
        self.assertEqual(fresh.begin("AA-BB", CID, len(DATA), SHA, META), 8192)

    def test_begin_restarts_when_declared_file_changes(self):
        self.upload(upto=8192)
        other = hashlib.sha256(b"other").hexdigest()
        self.assertEqual(self.store.begin("AA-BB", CID, 5, other, META), 0)
        self.assertEqual((self.root / f"{CID}.partial").stat().st_size, 0)

    def test_append_rejects_wrong_offset_and_overrun(self):
        self.store.begin("AA-BB", CID, len(DATA), SHA, META)
        with self.assertRaises(FileMismatch):
            self.store.append(CID, 1, DATA[:10])
        with self.assertRaises(FileMismatch):
            self.store.append(CID, 0, DATA + b"x")
        self.assertEqual((self.root / f"{CID}.partial").stat().st_size, 0)

    def test_append_without_begin_is_rejected(self):
        with self.assertRaises(FileMismatch):
            self.store.append(CID, 0, b"x")

    def test_hash_mismatch_discards_partial_and_publishes_nothing(self):
        self.store.begin("AA-BB", CID, len(DATA), SHA, META)
        bad = bytearray(DATA)
        bad[0] ^= 0xFF
        self.store.append(CID, 0, bytes(bad))
        with self.assertRaises(FileMismatch):
            self.store.commit("AA-BB", CID)
        self.assertFalse((self.root / f"{CID}.caf").exists())
        self.assertFalse((self.root / f"{CID}.partial").exists())
        self.assertEqual(self.store.hooks, [])

    def test_short_file_cannot_commit(self):
        self.upload(upto=4096)
        with self.assertRaises(FileMismatch):
            self.store.commit("AA-BB", CID)

    def test_committed_capture_is_idempotent(self):
        self.upload()
        self.store.commit("AA-BB", CID)
        self.assertEqual(self.store.begin("AA-BB", CID, len(DATA), SHA, META), len(DATA))
        self.store.commit("AA-BB", CID)
        self.assertEqual((self.root / f"{CID}.caf").read_bytes(), DATA)
        with self.assertRaises(FileMismatch):
            self.store.begin("AA-BB", CID, 5, hashlib.sha256(b"x").hexdigest(), META)

    def test_capture_marker_transitions(self):
        self.store.open_capture("AA-BB", CID, 1000, "us.zoom.xos")
        self.assertEqual(self.store.capture_state(CID)["state"], "open")
        self.store.cancel_capture("AA-BB", CID)
        self.assertEqual(self.store.capture_state(CID)["state"], "cancelled")
        self.store.open_capture("AA-BB", CID, 1000, "us.zoom.xos")  # cancelled is final
        self.assertEqual(self.store.capture_state(CID)["state"], "cancelled")
        self.assertIsNone(self.store.capture_state("ff" * 16))

    def test_cancel_after_close_keeps_closed(self):
        self.store.open_capture("AA-BB", CID, 1000, "us.zoom.xos")
        self.upload()
        self.store.commit("AA-BB", CID)
        self.store.cancel_capture("AA-BB", CID)
        self.assertEqual(self.store.capture_state(CID)["state"], "closed")

    def test_recover_after_crash_between_rename_and_marker(self):
        self.store.open_capture("AA-BB", CID, 1000, "us.zoom.xos")
        self.upload()
        self.store.commit("AA-BB", CID)
        # Simulate a crash after the rename: marker still open, hook never ran.
        (self.root / ".captures" / f"{CID}.json").write_text(json.dumps({"capture_id": CID, "state": "open"}))
        fresh = RecordingStore(self.root)
        fresh.recover()
        self.assertEqual(fresh.capture_state(CID)["state"], "closed")
        self.assertEqual(fresh.hooks, [(f"{CID}.caf", CID)])

    def test_crash_after_sidecar_before_rename_resumes_and_commits(self):
        self.upload()
        (self.root / f"{CID}.json").write_text("{}")  # sidecar written, audio not yet renamed
        fresh = RecordingStore(self.root)
        self.assertEqual(fresh.begin("AA-BB", CID, len(DATA), SHA, META), len(DATA))
        fresh.commit("AA-BB", CID)
        self.assertEqual((self.root / f"{CID}.caf").read_bytes(), DATA)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_file_store -v`
Expected: `ModuleNotFoundError: No module named 'omi_local.file_store'`.

- [ ] **Step 3: Implement `FileStore`**

Create `RX/omi_local/file_store.py`:

```python
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
    def capture_state(self, cid: str) -> dict | None:
        path = self._marker(cid)
        return _read(path) if path.exists() else None

    def open_capture(self, client: str, cid: str, start_ms: int, app: str) -> None:
        if self.capture_state(cid) is not None:
            return  # a repeated OPEN never reopens a cancelled or closed capture
        atomic_json(self._marker(cid), {"capture_id": cid, "client": client, "state": "open",
                                        "start_ms": start_ms, "app": app, "updated": time.time()})

    def cancel_capture(self, client: str, cid: str) -> None:
        current = self.capture_state(cid) or {"capture_id": cid, "client": client}
        if current.get("state") == "closed":
            return
        atomic_json(self._marker(cid), {**current, "state": "cancelled", "updated": time.time()})

    def _close_marker(self, client: str, cid: str, sidecar: dict) -> None:
        current = self.capture_state(cid) or {"capture_id": cid, "client": client}
        if current.get("state") == "closed":
            return
        atomic_json(self._marker(cid), {**current, "state": "closed", "start_ms": sidecar.get("start_ms"),
                                        "end_ms": sidecar.get("end_ms"), "app": sidecar.get("app"),
                                        "updated": time.time()})

    # --- file transfer -------------------------------------------------------
    def begin(self, client: str, cid: str, total_len: int, sha256: str, metadata: dict) -> int:
        """Return how many bytes of this file are already durable (0 = start over)."""
        audio = self._audio(cid)
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
        atomic_json(expect, {"total_len": total_len, "sha256": sha256, "metadata": metadata})
        with open(partial, "wb") as f:
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(self.root)
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
        if partial.stat().st_size != declared["total_len"] or digest.hexdigest() != declared["sha256"]:
            partial.unlink(missing_ok=True)
            expect.unlink(missing_ok=True)
            raise FileMismatch("uploaded bytes do not match the declared length/SHA-256")
        side = {**declared["metadata"], "capture_id": cid, "client": client, "sha256": declared["sha256"],
                "total_len": declared["total_len"], "file": str(audio), "complete": True}
        atomic_json(self._sidecar(cid), side)
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
            side = _read(sidecar)
            self._expect(side["capture_id"]).unlink(missing_ok=True)
            self._close_marker(side["client"], side["capture_id"], side)
            self.committed(audio, side)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_file_store -v`
Expected: 12 tests, all OK.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/scripts/omi-local/omi_local/file_store.py omi/firmware/scripts/omi-local/tests/test_file_store.py
git commit -m "receiver: add durable FileStore for v2 file uploads"
```

---

### Task 3: Server v2 handler

**Files:**
- Modify: `RX/omi_local/server.py` (`UploadServer.__init__`, `_handle`, new `_authenticate` and `_serve_file_client`)
- Test: `RX/tests/test_file_upload.py`

- [ ] **Step 1: Write the failing test**

Create `RX/tests/test_file_upload.py`:

```python
"""Protocol v2 over real loopback sockets: auth, upload, resume, rejection, and
coexistence with a v1 Omi device on the same receiver."""

import asyncio
import hashlib
import hmac
import secrets
import tempfile
import unittest
from pathlib import Path

from omi_local import upload_protocol as U
from omi_local.file_store import FileStore
from omi_local.server import UploadServer
from tests.fake_device import FakeRing
from tests.test_upload import FakeUploader

CID = bytes.fromhex("00112233445566778899aabbccddeeff")
DATA = secrets.token_bytes(200_000)
META = {"app": "us.zoom.xos", "start_ms": 1000, "end_ms": 61000}


def run(coro):
    return asyncio.run(coro)


class FakeFileClient:
    """The Mac capture app's side of protocol v2."""

    def __init__(self, secret, client_id=b"\xaa\xbb\xcc\xdd\xee\xff", chunk=16384,
                 drop_after_chunks=None, corrupt=False, bad_offset=False):
        self.secret = secret
        self.client_id = client_id
        self.chunk = chunk
        self.drop_after_chunks = drop_after_chunks
        self.corrupt = corrupt
        self.bad_offset = bad_offset
        self.result = None
        self.start_offset = None

    async def _recv(self, reader):
        t, n = U.parse_header(await reader.readexactly(U.HEADER_LEN))
        return t, (await reader.readexactly(n) if n else b"")

    async def _call(self, reader, writer, msg_type, payload=b""):
        writer.write(U.frame(msg_type, payload))
        await writer.drain()
        return await self._recv(reader)

    async def run(self, host, port, data=DATA, metadata=META, open_first=True, cancel=False):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            cn = secrets.token_bytes(16)
            t, p = await self._call(reader, writer, U.MSG_HELLO, U.encode_hello(self.client_id, cn, U.VERSION_FILE))
            if t == U.MSG_REJECT:
                self.result = f"rejected {p[0]}"
                return
            sn, tag = p[:16], p[16:]
            if not hmac.compare_digest(tag, U.auth_tag(self.secret, U.LABEL_SERVER, cn, sn)):
                self.result = "auth"
                return
            t, p = await self._call(reader, writer, U.MSG_AUTH, U.auth_tag(self.secret, U.LABEL_CLIENT, cn, sn))
            if t == U.MSG_REJECT:
                self.result = f"rejected {p[0]}"
                return
            assert t == U.MSG_OK
            if open_first:
                t, _ = await self._call(reader, writer, U.MSG_CAPTURE_OPEN,
                                        U.encode_capture_open(CID, metadata["start_ms"], metadata["app"]))
                assert t == U.MSG_OK
            if cancel:
                t, _ = await self._call(reader, writer, U.MSG_CAPTURE_CANCEL, CID)
                assert t == U.MSG_OK
                self.result = "cancelled"
                return
            t, p = await self._call(reader, writer, U.MSG_FILE_BEGIN,
                                    U.encode_file_begin(CID, len(data), hashlib.sha256(data).digest(), metadata))
            if t == U.MSG_REJECT:
                self.result = f"rejected {p[0]}"
                return
            assert t == U.MSG_FILE_START
            offset = self.start_offset = U.parse_u64(p)
            body = bytes([data[0] ^ 0xFF]) + data[1:] if self.corrupt else data
            chunks = 0
            while offset < len(data):
                piece = body[offset:offset + self.chunk]
                frame = U.frame(U.MSG_FILE_DATA, U.encode_file_data(offset + int(self.bad_offset), piece))
                if chunks == self.drop_after_chunks:
                    writer.write(frame[:len(frame) // 2])
                    await writer.drain()
                    self.result = "link lost"
                    return
                writer.write(frame)
                await writer.drain()
                t, p = await self._recv(reader)
                if t == U.MSG_REJECT:
                    self.result = f"rejected {p[0]}"
                    return
                assert t == U.MSG_FILE_ACK
                offset = U.parse_u64(p)
                chunks += 1
            t, p = await self._call(reader, writer, U.MSG_FILE_END)
            if t == U.MSG_REJECT:
                self.result = f"rejected {p[0]}"
                return
            assert t == U.MSG_FILE_BYE and p == b"\x01"
            self.result = "ok"
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


class FileUploadServerTests(unittest.TestCase):
    def setUp(self):
        self.secret = secrets.token_bytes(32)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name)
        self.store = FileStore(self.dest / "meetings")

    async def _serve(self, fn, store="default"):
        server = UploadServer(self.secret, self.dest, host="127.0.0.1", port=0,
                              file_store=self.store if store == "default" else store)
        await server.start()
        try:
            await fn(server.bound_port)
            return server
        finally:
            await server.close()

    def test_full_upload_commits_verified_file(self):
        client = FakeFileClient(self.secret)
        run(self._serve(lambda port: client.run("127.0.0.1", port)))
        self.assertEqual(client.result, "ok")
        self.assertEqual((self.dest / "meetings" / f"{CID.hex()}.caf").read_bytes(), DATA)
        self.assertEqual(self.store.capture_state(CID.hex())["state"], "closed")

    def test_wrong_secret_is_rejected_before_any_file(self):
        client = FakeFileClient(secrets.token_bytes(32))
        run(self._serve(lambda port: client.run("127.0.0.1", port)))
        self.assertEqual(client.result, "auth")
        self.assertFalse((self.dest / "meetings").exists())

    def test_link_lost_then_resume_from_acked_offset(self):
        first = FakeFileClient(self.secret, drop_after_chunks=3)
        run(self._serve(lambda port: first.run("127.0.0.1", port)))
        self.assertEqual(first.result, "link lost")
        second = FakeFileClient(self.secret)
        run(self._serve(lambda port: second.run("127.0.0.1", port)))
        self.assertEqual((second.result, second.start_offset), ("ok", 3 * 16384))
        self.assertEqual((self.dest / "meetings" / f"{CID.hex()}.caf").read_bytes(), DATA)

    def test_wrong_offset_is_rejected(self):
        client = FakeFileClient(self.secret, bad_offset=True)
        run(self._serve(lambda port: client.run("127.0.0.1", port)))
        self.assertEqual(client.result, f"rejected {U.REJECT_PROTOCOL}")
        self.assertEqual((self.dest / "meetings" / f"{CID.hex()}.partial").stat().st_size, 0)

    def test_hash_mismatch_commits_nothing(self):
        client = FakeFileClient(self.secret, corrupt=True)
        run(self._serve(lambda port: client.run("127.0.0.1", port)))
        self.assertEqual(client.result, f"rejected {U.REJECT_PROTOCOL}")
        self.assertEqual(list((self.dest / "meetings").glob("*.caf")), [])

    def test_cancel_marks_capture(self):
        client = FakeFileClient(self.secret)
        run(self._serve(lambda port: client.run("127.0.0.1", port, cancel=True)))
        self.assertEqual(client.result, "cancelled")
        self.assertEqual(self.store.capture_state(CID.hex())["state"], "cancelled")

    def test_v2_rejected_when_file_uploads_disabled(self):
        client = FakeFileClient(self.secret)
        run(self._serve(lambda port: client.run("127.0.0.1", port), store=None))
        self.assertEqual(client.result, f"rejected {U.REJECT_PROTOCOL}")

    def test_oversized_frame_is_rejected(self):
        result = {}

        async def go(port):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            cn = secrets.token_bytes(16)
            writer.write(U.frame(U.MSG_HELLO, U.encode_hello(b"\x01" * 6, cn, U.VERSION_FILE)))
            await writer.drain()
            challenge = (await reader.readexactly(U.HEADER_LEN + 48))[U.HEADER_LEN:]
            writer.write(U.frame(U.MSG_AUTH, U.auth_tag(self.secret, U.LABEL_CLIENT, cn, challenge[:16])))
            await writer.drain()
            await reader.readexactly(U.HEADER_LEN)  # OK
            writer.write(bytes([U.MSG_FILE_DATA]) + (U.MAX_FILE_PAYLOAD + 1).to_bytes(4, "big"))
            await writer.drain()
            hdr = await reader.readexactly(U.HEADER_LEN)
            result["reply"] = (hdr[0], (await reader.readexactly(1))[0])
            writer.close()

        run(self._serve(go))
        self.assertEqual(result["reply"], (U.MSG_REJECT, U.REJECT_PROTOCOL))

    def test_v1_device_and_v2_client_share_one_receiver(self):
        ring = FakeRing(capacity=5000)
        ring.record_session(1_700_000_000, 100)
        device = FakeUploader(ring, self.secret)
        client = FakeFileClient(self.secret)

        async def both(port):
            await asyncio.gather(device.run("127.0.0.1", port), client.run("127.0.0.1", port))

        run(self._serve(both))
        self.assertEqual((device.result, client.result), ("ok", "ok"))
        self.assertEqual(len(list(self.dest.glob("*.opus"))), 1)
        self.assertEqual(len(list((self.dest / "meetings").glob("*.caf"))), 1)


if __name__ == "__main__":
    unittest.main()
```


- [ ] **Step 2: Run test to verify it fails**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_file_upload -v`
Expected: `TypeError: UploadServer.__init__() got an unexpected keyword argument 'file_store'`.

- [ ] **Step 3: Implement the v2 handler**

In `RX/omi_local/server.py`:

1. Change the constructor signature and store the sink:

```python
    def __init__(self, secret: bytes, dest: Path, host: str = "0.0.0.0", port: int = 7331,
                 writer_factory: SessionWriterFactory | None = None, file_store=None) -> None:
```

and after `self.writers = ...` add:

```python
        self.files = file_store  # omi_local.file_store.FileStore, or None to refuse v2 clients
```

2. Add this method above `_handle`:

```python
    async def _authenticate(self, reader, writer, hello: U.Hello, parse):
        """Mutual HMAC handshake shared by v1 devices and v2 file clients.
        Returns the parsed AUTH, or None after sending REJECT_AUTH."""
        server_nonce = _secrets.token_bytes(U.NONCE_LEN)
        await self._send(writer, U.MSG_CHALLENGE,
                         U.encode_challenge(server_nonce, U.auth_tag(self.secret, U.LABEL_SERVER,
                                                                     hello.client_nonce, server_nonce)))
        msg_type, payload = await self._read_frame(reader, U.MAX_CTRL_PAYLOAD, CTRL_TIMEOUT_S)
        if msg_type != U.MSG_AUTH:
            raise U.UploadProtocolError("expected AUTH")
        auth = parse(payload)
        expected = U.auth_tag(self.secret, U.LABEL_CLIENT, hello.client_nonce, server_nonce)
        if not hmac.compare_digest(auth.client_tag, expected):
            log.warning("client %s failed authentication", hello.device_id_str)
            await self._send(writer, U.MSG_REJECT, bytes([U.REJECT_AUTH]))
            return None
        return auth
```

3. In `_handle`, replace everything from `server_nonce = _secrets.token_bytes(U.NONCE_LEN)` through the `return` after the failed-authentication `REJECT_AUTH` with:

```python
            if hello.version == U.VERSION_FILE:
                ok = await self._serve_file_client(reader, writer, hello)
                return
            auth = await self._authenticate(reader, writer, hello, U.parse_auth)
            if auth is None:
                return
```

(The v1 code that follows — busy check, `info = auth.info`, … — is unchanged.)

4. Add this method below `_handle`:

```python
    async def _serve_file_client(self, reader, writer, hello: U.Hello) -> bool:
        """Protocol v2: capture markers and whole-file uploads (see file_store.py)."""
        client = hello.device_id_str
        if self.files is None:
            raise U.UploadProtocolError("file uploads are not enabled on this receiver")
        if await self._authenticate(reader, writer, hello, U.parse_file_auth) is None:
            return False
        key = "file:" + client
        if key in self._busy:
            await self._send(writer, U.MSG_REJECT, bytes([U.REJECT_BUSY]))
            return False
        self._busy.add(key)
        try:
            await self._send(writer, U.MSG_OK)
            log.info("file client %s connected", client)
            current = None
            while True:
                try:
                    msg_type, payload = await self._read_frame(reader, U.MAX_FILE_PAYLOAD, DATA_TIMEOUT_S)
                except asyncio.IncompleteReadError as e:
                    if not e.partial:
                        return True  # clean close between messages
                    raise
                if msg_type == U.MSG_CAPTURE_OPEN:
                    m = U.parse_capture_open(payload)
                    await asyncio.to_thread(self.files.open_capture, client, m.capture_id.hex(), m.start_ms, m.app)
                    await self._send(writer, U.MSG_OK)
                elif msg_type == U.MSG_CAPTURE_CANCEL:
                    cid = U.parse_capture_id(payload).hex()
                    await asyncio.to_thread(self.files.cancel_capture, client, cid)
                    await self._send(writer, U.MSG_OK)
                elif msg_type == U.MSG_FILE_BEGIN:
                    b = U.parse_file_begin(payload)
                    current = b.capture_id.hex()
                    offset = await asyncio.to_thread(self.files.begin, client, current, b.total_len,
                                                     b.sha256.hex(), b.metadata)
                    await self._send(writer, U.MSG_FILE_START, U.encode_u64(offset))
                elif msg_type == U.MSG_FILE_DATA and current is not None:
                    d = U.parse_file_data(payload)
                    persisted = await asyncio.to_thread(self.files.append, current, d.offset, d.data)
                    await self._send(writer, U.MSG_FILE_ACK, U.encode_u64(persisted))
                elif msg_type == U.MSG_FILE_END and current is not None:
                    await asyncio.to_thread(self.files.commit, client, current)
                    log.info("file client %s: committed capture %s", client, current)
                    await self._send(writer, U.MSG_FILE_BYE, b"\x01")
                    current = None
                else:
                    raise U.UploadProtocolError(f"unexpected v2 message 0x{msg_type:02x}")
        finally:
            self._busy.discard(key)
```

`FileMismatch` subclasses `UploadProtocolError`, so the existing `except U.UploadProtocolError` branch in `_handle` sends `REJECT_PROTOCOL` for wrong offsets and hash mismatches. An oversized frame raises `UploadProtocolError` in `_read_frame` before its payload is read.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd omi/firmware/scripts/omi-local && .venv/bin/python -m unittest tests.test_file_upload tests.test_upload tests.test_file_store tests.test_file_protocol -v`
Expected: all OK, including every existing v1 `test_upload` test.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/scripts/omi-local/omi_local/server.py omi/firmware/scripts/omi-local/tests/test_file_upload.py
git commit -m "receiver: serve protocol v2 file clients alongside v1 devices"
```

---

### Task 4: Pipeline `MeetingStore` and runtime wiring

**Files:**
- Modify: `src/second_brain/receiver.py`
- Modify: `src/second_brain/runtime.py:219-227` (`Runtime.run`)
- Modify: `tests/helpers.py` (add `upload_file`)
- Test: `tests/test_meeting_receiver.py`

- [ ] **Step 1: Add the v2 test client to `tests/helpers.py`**

Append to `tests/helpers.py` (it already imports `asyncio`, `secrets`, `U`, `SECRET`; add `import hashlib` at the top):

```python
async def upload_file(port, capture_id: bytes, data: bytes, metadata: dict, secret=SECRET):
    """Minimal protocol-v2 client: open the capture, upload one file, return the final reply type."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        async def call(kind, payload=b""):
            writer.write(U.frame(kind, payload))
            await writer.drain()
            return await receive(reader)

        cn = secrets.token_bytes(16)
        kind, challenge = await call(U.MSG_HELLO, U.encode_hello(b"\xaa\xbb\xcc\xdd\xee\xff", cn, U.VERSION_FILE))
        assert kind == U.MSG_CHALLENGE
        kind, _ = await call(U.MSG_AUTH, U.auth_tag(secret, U.LABEL_CLIENT, cn, challenge[:16]))
        if kind != U.MSG_OK:
            return kind
        await call(U.MSG_CAPTURE_OPEN, U.encode_capture_open(capture_id, metadata["start_ms"], metadata["app"]))
        kind, start = await call(U.MSG_FILE_BEGIN,
                                 U.encode_file_begin(capture_id, len(data), hashlib.sha256(data).digest(), metadata))
        assert kind == U.MSG_FILE_START
        offset = U.parse_u64(start)
        while offset < len(data):
            kind, ack = await call(U.MSG_FILE_DATA, U.encode_file_data(offset, data[offset:offset + 16384]))
            assert kind == U.MSG_FILE_ACK
            offset = U.parse_u64(ack)
        kind, _ = await call(U.MSG_FILE_END)
        return kind
    finally:
        writer.close()
        await writer.wait_closed()
```

(`receive(reader)` is the existing helper used by `upload`.)

- [ ] **Step 2: Write the failing test**

Create `tests/test_meeting_receiver.py`:

```python
from __future__ import annotations
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from second_brain.receiver import MeetingStore
from second_brain.runtime import Runtime
from omi_local import upload_protocol as U
from tests.helpers import configuration, upload_file

CID = "00112233445566778899aabbccddeeff"
DATA = b"caf-bytes" * 1000
META = {"app": "us.zoom.xos", "start_ms": 1000, "end_ms": 61000, "channels": {"L": "mic", "R": "remote"}}


def commit(store: MeetingStore):
    offset = store.begin("AA-BB", CID, len(DATA), hashlib.sha256(DATA).hexdigest(), META)
    store.append(CID, offset, DATA)
    return store.commit("AA-BB", CID)


class MeetingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "incoming" / "meetings"

    def test_commit_publishes_meeting_receipt(self):
        audio = commit(MeetingStore(self.root))
        receipt = json.loads((self.root / ".ready" / f"{CID}.json").read_text())
        self.assertEqual(Path(receipt["audio"]), audio)
        self.assertEqual((receipt["metadata"]["source"], receipt["metadata"]["capture_id"]), ("meeting", CID))

    def test_recover_recreates_missing_receipt_without_duplicating(self):
        store = MeetingStore(self.root)
        commit(store)
        receipt = self.root / ".ready" / f"{CID}.json"
        receipt.unlink()
        MeetingStore(self.root).recover()
        MeetingStore(self.root).recover()
        self.assertTrue(receipt.exists())
        self.assertEqual(len(list((self.root / ".ready").glob("*.json"))), 1)


class MeetingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = configuration(self.root)
        self.runtime = Runtime(self.cfg)
        self.task = asyncio.create_task(self.runtime.run())
        for _ in range(100):
            if self.runtime.server and self.runtime.server._server:
                break
            if self.task.done():
                await self.task
            await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        self.runtime.stop.set()
        await asyncio.wait_for(self.task, 10)
        self.tmp.cleanup()

    async def test_service_accepts_meeting_upload_but_does_not_enqueue_it(self):
        kind = await upload_file(self.runtime.server.bound_port, bytes.fromhex(CID), DATA, META)
        self.assertEqual(kind, U.MSG_FILE_BYE)
        meetings = self.cfg.incoming_dir / "meetings"
        self.assertEqual((meetings / f"{CID}.caf").read_bytes(), DATA)
        self.assertTrue((meetings / ".ready" / f"{CID}.json").exists())
        self.runtime.discover()
        self.assertIsNone(self.runtime.queue.claim())  # Plan 2 adds meeting processing


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local omi/firmware/scripts/omi-local/.venv/bin/python -m unittest tests.test_meeting_receiver -v`
Expected: `ImportError: cannot import name 'MeetingStore' from 'second_brain.receiver'`.

- [ ] **Step 4: Implement `MeetingStore` and wire it**

In `src/second_brain/receiver.py` add the import `from omi_local.file_store import FileStore` and append:

```python
class MeetingStore(FileStore):
    """Meeting captures from the Mac app. Receipts live in `<meetings>/.ready`,
    separate from Omi receipts, and are published only after the file is durable."""

    def committed(self, audio: Path, sidecar: dict):
        receipt = self.root / ".ready" / f"{sidecar['capture_id']}.json"
        if not receipt.exists():
            write_json(receipt, {"audio": str(audio), "metadata": {**sidecar, "source": "meeting"}})
```

In `src/second_brain/runtime.py` change the import to `from .receiver import MeetingStore, ReceiverFactory`, and in `Runtime.run` replace the server construction with:

```python
            factory = ReceiverFactory(self.cfg.incoming_dir)
            factory.recover_receipts()
            meetings = MeetingStore(self.cfg.incoming_dir / "meetings")
            meetings.recover()
            secret = load_or_create_secret(self.cfg.secret_file, create=False)
            self.server = UploadServer(
                secret, self.cfg.incoming_dir, self.cfg.host, self.cfg.port, writer_factory=factory,
                file_store=meetings,
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local omi/firmware/scripts/omi-local/.venv/bin/python -m unittest tests.test_meeting_receiver tests.test_service -v`
Expected: all OK, including the existing service tests.

- [ ] **Step 6: Commit**

```bash
git add src/second_brain/receiver.py src/second_brain/runtime.py tests/helpers.py tests/test_meeting_receiver.py
git commit -m "service: store meeting captures from v2 file clients"
```

---

### Task 5: Documentation and full verification

**Files:**
- Modify: `RX/omi_local/upload_protocol.py` (module docstring)
- Modify: `src/second_brain/ARCHITECTURE.md`
- Modify: `SYSTEM.md`

- [ ] **Step 1: Document v2 in the protocol docstring**

In `RX/omi_local/upload_protocol.py`, insert after the line `"omi-local-srv" (receiver proves itself first) / "omi-local-cli".` and before `Pure data helpers`:

```text

Protocol v2 (file clients such as the Mac meeting-capture app): HELLO ver=2,
the same CHALLENGE, then AUTH carrying only client_tag:32, answered by OK 0x18.
  C->S CAPTURE_OPEN   0x10  capture_id:16 start_ms:u64 app:utf8(<=256)   -> OK
  C->S CAPTURE_CANCEL 0x11  capture_id:16                                -> OK
  C->S FILE_BEGIN     0x12  capture_id:16 total_len:u64 sha256:32 json   -> FILE_START 0x15 offset:u64
  C->S FILE_DATA      0x13  offset:u64 bytes(<=64 KiB)                   -> FILE_ACK 0x16 persisted:u64
  C->S FILE_END       0x14                                               -> FILE_BYE 0x17 committed:u8
Offsets must equal the durable length; a length/SHA-256 mismatch discards the
partial. Both are REJECT_PROTOCOL. The client closes the socket when done.
```

- [ ] **Step 2: Update `src/second_brain/ARCHITECTURE.md`**

After step 2 of "Receipt → job → note" add:

```markdown
   Protocol-v2 file clients (the Mac meeting-capture app) upload whole files
   into `incoming/meetings/` through `MeetingStore` (`omi_local.file_store`):
   resumable fsynced partials, SHA-256-verified atomic commit, and capture
   markers (`open`/`cancelled`/`closed`). Meeting receipts go to
   `incoming/meetings/.ready/`, which Omi discovery does not scan; processing is
   added separately.
```

- [ ] **Step 3: Correct `SYSTEM.md`**

In `SYSTEM.md` "Firmware and transport": replace `3.0.22-localwifi.13` with `3.0.22-localwifi.14` in the "Last installed/verified image" bullet, and in the VOX bullet replace `**300**` with `**400**`. Then add a bullet under the same section:

```markdown
- Wake feedback: an 80 ms vibration only after at least 5 minutes of acoustic
  sleep; every mic restart discards 500 ms of PCM (startup transient + motor).
```

Run `grep -n 'localwifi\|threshold' SYSTEM.md` and confirm no remaining `.13`/`300` claims about the current image.

- [ ] **Step 4: Run the full suite**

Run from the repo root: `omi/firmware/scripts/omi-local/.venv/bin/python scripts/test.py 2>&1 | grep -E '^Ran|^OK|FAILED'`
Expected: two `Ran N tests` lines, each followed by `OK`.

- [ ] **Step 5: Commit**

```bash
git add omi/firmware/scripts/omi-local/omi_local/upload_protocol.py src/second_brain/ARCHITECTURE.md SYSTEM.md
git commit -m "docs: describe receiver protocol v2 and firmware .14"
```
