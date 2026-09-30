"""Receiver integration: publish ready receipts only after durable ACK state."""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
from omi_local.cli import SessionWriter
from omi_local.file_store import FileStore
from omi_local.server import SessionWriterFactory
from omi_local.state import StateStore
from .config import read_json
from .io import write_json


def publish_receipts(incoming: Path, files: list[dict]):
    for item in files:
        audio = Path(item["file"]).resolve()
        if not audio.is_relative_to(incoming.resolve()):
            raise ValueError("Receiver state points outside its configured incoming directory")
        sidecar = audio.with_suffix(".json")
        if not sidecar.exists():
            continue
        metadata = read_json(sidecar)
        if metadata.get("complete") is not True:
            continue
        key = hashlib.sha256(f"{metadata['device']}:{audio}".encode()).hexdigest()
        receipt = incoming / ".ready" / (key + ".json")
        if not receipt.exists():
            write_json(receipt, {"audio": str(audio), "metadata": metadata})


class DurableWriter(SessionWriter):
    incoming: Path

    def fsync(self):
        super().fsync()
        publish_receipts(self.incoming, self.state.files)


class ReceiverFactory(SessionWriterFactory):
    def make(self, device_id):
        # Honor existing receiver state when upgrading an already paired receiver.
        legacy = StateStore(self.dest)
        root = self.dest if device_id in legacy._data else self.dest / "devices" / device_id
        store = StateStore(root)
        writer = DurableWriter(root, device_id, store.get(device_id), store)
        writer.incoming = self.dest
        return writer

    def recover_receipts(self):
        # A crash after durable audio/cursor but before receipt publication is recoverable.
        paths = [self.dest / ".omi-local" / "state.json"]
        paths += list((self.dest / "devices").glob("*/.omi-local/state.json"))
        for path in paths:
            if path.exists():
                for state in read_json(path).values():
                    publish_receipts(self.dest, state.get("files", []))


class MeetingStore(FileStore):
    """Meeting captures from the Mac app. Receipts live in `<meetings>/.ready`,
    separate from Omi receipts, and are published only after the file is durable."""

    def committed(self, audio: Path, sidecar: dict):
        receipt = self.root / ".ready" / f"{sidecar['capture_id']}.json"
        if not receipt.exists():
            write_json(receipt, {"audio": str(audio), "metadata": {**sidecar, "source": "meeting"}})
