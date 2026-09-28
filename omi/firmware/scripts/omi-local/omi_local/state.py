"""Per-destination download bookkeeping so pulls resume and deletes are exact.

State lives in `<dest>/.omi-local/state.json`. `downloaded_through` is the seq
up to which EVERY record from the device's read_seq has been written to disk
and verified. `omi-local delete --downloaded` never deletes past it.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading

_STORE_LOCK = threading.Lock()

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class OpenFile:
    """An Ogg file that can still be appended to (no EOS page written yet)."""

    path: str
    last_seq: int
    last_timestamp: int
    mux: dict
    bytes_written: int | None = None
    metadata: dict | None = None


@dataclass
class DeviceState:
    device_id: str
    downloaded_through: int = 0
    write_seq_seen: int = 0
    open_file: OpenFile | None = None
    files: list[dict] = field(default_factory=list)


class StateStore:
    def __init__(self, dest: Path) -> None:
        self.dest = Path(dest)
        self.dir = self.dest / ".omi-local"
        self.path = self.dir / "state.json"
        self._data: dict = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def get(self, device_id: str) -> DeviceState:
        d = self._data.get(device_id)
        if not d:
            return DeviceState(device_id=device_id)
        of = d.get("open_file")
        return DeviceState(
            device_id=device_id,
            downloaded_through=int(d.get("downloaded_through", 0)),
            write_seq_seen=int(d.get("write_seq_seen", 0)),
            open_file=OpenFile(**of) if of else None,
            files=list(d.get("files", [])),
        )

    def put(self, st: DeviceState) -> None:
        d = asdict(st)
        d.pop("device_id")
        with _STORE_LOCK:
            # Different devices can complete DATA writes on different pool threads.
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            self._data[st.device_id] = d
            atomic_json(self.path, self._data)

    def fsync(self) -> None:
        import os

        # Windows FlushFileBuffers requires a handle with write access.
        # A failed open/flush must propagate: the receiver must not ACK it.
        fd = os.open(self.path, os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
