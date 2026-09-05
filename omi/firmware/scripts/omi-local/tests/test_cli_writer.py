import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from omi_local import protocol as P
from omi_local.cli import SessionWriter, build_parser, cmd_verify
from omi_local.device import DumpClient
from omi_local.oggopus import iter_pages
from omi_local.state import StateStore
from tests.fake_device import FakeRing, FakeTransport


class SessionWriterTests(unittest.TestCase):
    def _ring(self):
        ring = FakeRing()
        ring.record_session(1_700_000_000, 50)   # session A
        ring.record_session(1_700_000_500, 30)   # session B (gap 495 s)
        ring.record_session(0, 20)               # session C: clock unknown
        return ring

    def test_pull_writes_one_playable_file_per_session_and_tracks_state(self):
        ring = self._ring()
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            t = FakeTransport(ring)
            store = StateStore(dest)
            state = store.get(t.device_id)
            w = SessionWriter(dest, t.device_id, state, store, gap_s=60)
            c = DumpClient(t)

            async def go():
                await c.open()
                await c.pull(0, 100, w.add, chunk_packets=33)

            asyncio.run(go())
            w.finish(final=True)
            files = sorted(p.name for p in dest.glob("*.opus"))
            self.assertEqual(files, ["omi_20231114-221320_seq000000000000.opus",
                                     "omi_20231114-222140_seq000000000050.opus",
                                     "omi_unknown-time_seq000000000080.opus"])
            for f in dest.glob("*.opus"):
                pages = list(iter_pages(f.read_bytes()))
                self.assertTrue(all(p.crc_ok for p in pages), f)
                self.assertEqual(pages[-1].header_type & 0x04, 0x04, f)
                meta = json.loads(f.with_suffix(".json").read_text())
                self.assertTrue(meta["complete"])
                self.assertEqual(meta["packets"], meta["end_seq"] - meta["start_seq"])
            a = json.loads((dest / "omi_20231114-221320_seq000000000000.json").read_text())
            self.assertEqual((a["start_seq"], a["end_seq"], a["frames"]), (0, 50, 250))
            self.assertAlmostEqual(a["audio_seconds"], 5.0)
            st = StateStore(dest).get(t.device_id)
            self.assertEqual(st.downloaded_through, 100)
            self.assertIsNone(st.open_file)
            self.assertEqual(len(st.files), 3)
            self.assertEqual(ring.read_seq, 0)  # nothing deleted

    def test_interrupted_pull_resumes_into_same_file(self):
        ring = self._ring()
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            # First run: transfer fails for good after the first chunk (simulate by pulling a sub-range).
            t = FakeTransport(ring)
            store = StateStore(dest)
            w = SessionWriter(dest, t.device_id, store.get(t.device_id), store)
            c = DumpClient(t)
            asyncio.run(self._pull(c, 0, 20, w))
            w.finish(final=False)
            st = StateStore(dest).get(t.device_id)
            self.assertEqual(st.downloaded_through, 20)
            self.assertIsNotNone(st.open_file)
            self.assertEqual(st.open_file.last_seq, 19)
            # Second run resumes from the state and continues the SAME file.
            t2 = FakeTransport(ring)
            store2 = StateStore(dest)
            w2 = SessionWriter(dest, t2.device_id, store2.get(t2.device_id), store2)
            asyncio.run(self._pull(DumpClient(t2), 20, 100, w2))
            w2.finish(final=True)
            files = sorted(p.name for p in dest.glob("*.opus"))
            self.assertEqual(len(files), 3)
            first = dest / "omi_20231114-221320_seq000000000000.opus"
            pages = list(iter_pages(first.read_bytes()))
            self.assertTrue(all(p.crc_ok for p in pages))
            self.assertEqual([p.page_seq for p in pages], list(range(len(pages))))
            self.assertEqual(sum(len(p.packets) for p in pages[2:]), 250)
            self.assertEqual(pages[-1].granule, 250 * 960)
            self.assertEqual(StateStore(dest).get(t2.device_id).downloaded_through, 100)

    def test_explicit_range_does_not_extend_downloaded_prefix(self):
        ring = self._ring()
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            t = FakeTransport(ring)
            store = StateStore(dest)
            w = SessionWriter(dest, t.device_id, store.get(t.device_id), store)
            asyncio.run(self._pull(DumpClient(t), 60, 70, w))
            w.finish(final=False)
            self.assertEqual(StateStore(dest).get(t.device_id).downloaded_through, 0)

    @staticmethod
    async def _pull(c, a, b, w):
        await c.open()
        await c.pull(a, b, w.add, chunk_packets=10)

    def test_verify_command(self):
        ring = self._ring()
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d)
            t = FakeTransport(ring)
            store = StateStore(dest)
            w = SessionWriter(dest, t.device_id, store.get(t.device_id), store)
            asyncio.run(self._pull(DumpClient(t), 0, 50, w))
            w.finish(final=True)
            f = next(dest.glob("*.opus"))
            args = build_parser().parse_args(["verify", str(f)])
            self.assertEqual(cmd_verify(args), 0)


class ParserTests(unittest.TestCase):
    def test_delete_requires_an_explicit_target(self):
        p = build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["delete"])
        self.assertTrue(p.parse_args(["delete", "--all"]).all)
        self.assertEqual(p.parse_args(["delete", "--through", "5"]).through, 5)
        self.assertEqual(p.parse_args(["pull", "out"]).chunk_packets, 400)


if __name__ == "__main__":
    unittest.main()
