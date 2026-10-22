"""Durable local publication and single-instance ownership."""

from __future__ import annotations
import datetime as dt
import json
import os
import secrets
import time
from pathlib import Path


def temp_beside(path: Path) -> tuple[int, str]:
    """A new private temp file next to path. Not tempfile.mkstemp: on Windows it treats
    PermissionError as a name clash and retries up to os.TMP_MAX (2**31 - 1) times, so a folder
    the service account may not write hung the caller silently instead of raising."""
    name = str(path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp"))
    return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600), name


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = temp_beside(path)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 9:
                    raise
                time.sleep(0.02)  # a short-lived Windows reader may deny delete sharing
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, value) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            # Windows locks deny reads too, including through another handle
            # in this process. Lock byte zero without reading/initializing it;
            # msvcrt permits locking beyond EOF, even on a new empty file.
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.file.close()
            self.file = None
            raise RuntimeError("Another Second Brain worker owns this data directory") from e
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.close()
            self.file = None


def utc_from_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
