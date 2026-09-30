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
        declared = json.loads((self.root / f"{CID}.partial.json").read_text())
        self.assertEqual((declared["total_len"], declared["sha256"]), (5, other))

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

    def test_cancel_removes_partial_and_blocks_begin(self):
        self.upload(upto=4096)
        self.store.cancel_capture("AA-BB", CID)
        self.assertFalse((self.root / f"{CID}.partial").exists())
        self.assertFalse((self.root / f"{CID}.partial.json").exists())
        with self.assertRaises(FileMismatch):
            self.store.begin("AA-BB", CID, len(DATA), SHA, META)

    def test_short_commit_keeps_partial_and_can_resume(self):
        self.upload(upto=4096)
        with self.assertRaises(FileMismatch):
            self.store.commit("AA-BB", CID)
        self.assertEqual((self.root / f"{CID}.partial").stat().st_size, 4096)
        offset = self.store.begin("AA-BB", CID, len(DATA), SHA, META)
        self.assertEqual(offset, 4096)
        self.store.append(CID, offset, DATA[offset:])
        self.assertEqual(self.store.commit("AA-BB", CID).read_bytes(), DATA)

    def test_metadata_cannot_override_reserved_sidecar_keys(self):
        meta = {**META, "sha256": "x", "file": "y", "complete": False, "capture_id": "z"}
        offset = self.store.begin("AA-BB", CID, len(DATA), SHA, meta)
        self.store.append(CID, offset, DATA)
        self.store.commit("AA-BB", CID)
        side = json.loads((self.root / f"{CID}.json").read_text())
        self.assertEqual((side["sha256"], side["complete"], side["capture_id"]), (SHA, True, CID))
        self.assertEqual(side["file"], str(self.root / f"{CID}.caf"))

    def test_recover_continues_past_malformed_sidecar(self):
        cid2 = "ff" * 16
        self.upload()
        self.store.commit("AA-BB", CID)
        (self.root / f"{cid2}.caf").write_bytes(b"x")
        (self.root / f"{cid2}.json").write_text("not json")
        cid3 = "ee" * 16
        (self.root / f"{cid3}.caf").write_bytes(b"x")
        (self.root / f"{cid3}.json").write_text("null")
        (self.root / ".captures" / f"{CID}.json").write_text(json.dumps({"capture_id": CID, "state": "open"}))
        fresh = RecordingStore(self.root)
        with self.assertLogs("omi_local.file_store", level="ERROR"):
            fresh.recover()
        self.assertEqual(fresh.capture_state(CID)["state"], "closed")
        self.assertEqual(fresh.hooks, [(f"{CID}.caf", CID)])

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
