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
        self.assertFalse(self.runtime.queue.known(meetings / f"{CID}.caf"))
        self.assertIsNone(self.runtime.queue.claim())  # Plan 2 adds meeting processing


if __name__ == "__main__":
    unittest.main()
