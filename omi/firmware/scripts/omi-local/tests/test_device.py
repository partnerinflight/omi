import asyncio
import unittest

from omi_local import protocol as P
from omi_local.device import DumpClient, TransferError
from tests.fake_device import FakeRing, FakeTransport, make_record


def run(coro):
    return asyncio.run(coro)


class DumpClientTests(unittest.TestCase):
    def setUp(self):
        self.ring = FakeRing(capacity=5000)
        self.ring.record_session(1_700_000_000, 1000)

    def test_info_and_status(self):
        t = FakeTransport(self.ring)
        c = DumpClient(t)

        async def go():
            await c.open()
            info = await c.info()
            st = await c.status()
            await c.close()
            return info, st

        info, st = run(go())
        self.assertEqual((info.read_seq, info.write_seq, info.codec_id), (0, 1000, P.CODEC_ID_OPUS))
        self.assertEqual(st.unread_packets, 1000)
        self.assertFalse(t.connected)

    def test_read_range_is_complete_and_never_deletes(self):
        t = FakeTransport(self.ring)
        c = DumpClient(t)

        async def go():
            await c.open()
            data = await c.read_range(10, 20)
            info = await c.info()
            return data, info

        data, info = run(go())
        self.assertEqual(len(data), 20 * P.RECORD_SIZE)
        self.assertEqual(data[:P.RECORD_SIZE], make_record(10, 1_700_000_001))
        self.assertEqual(info.read_seq, 0)  # reading moved nothing
        self.assertFalse(any(cmd[0] in (P.CMD_ADVANCE, P.CMD_CLEAR) for cmd in t.commands))

    def test_pull_all_delivers_everything_in_order(self):
        t = FakeTransport(self.ring)
        c = DumpClient(t)
        got = []

        async def go():
            await c.open()
            nxt = await c.pull(0, 1000, lambda seq, d: got.append((seq, d)), chunk_packets=150)
            return nxt

        nxt = run(go())
        self.assertEqual(nxt, 1000)
        stream = b"".join(d for _, d in got)
        self.assertEqual(stream, b"".join(self.ring.records[s] for s in range(1000)))
        self.assertEqual(self.ring.read_seq, 0)

    def test_pull_resumes_after_disconnects(self):
        # Drop the link twice mid-transfer at odd byte offsets (not record aligned).
        t = FakeTransport(self.ring, disconnect_after_bytes=[7000, 3333])
        c = DumpClient(t)
        got = []

        async def go():
            await c.open()
            await c.pull(0, 400, lambda seq, d: got.append((seq, d)), chunk_packets=100)

        run(go())
        seqs = []
        for seq, d in got:
            self.assertEqual(len(d) % P.RECORD_SIZE, 0)
            seqs.extend(range(seq, seq + len(d) // P.RECORD_SIZE))
        self.assertEqual(seqs, list(range(400)))  # every record exactly once, in order
        stream = b"".join(d for _, d in got)
        self.assertEqual(stream, b"".join(self.ring.records[s] for s in range(400)))
        self.assertGreaterEqual(t.connect_count, 3)
        self.assertEqual(self.ring.read_seq, 0)  # source intact throughout
        self.assertFalse(any(cmd[0] in (P.CMD_ADVANCE, P.CMD_CLEAR) for cmd in t.commands))

    def test_out_of_range_read_is_not_retried_forever(self):
        t = FakeTransport(self.ring)
        c = DumpClient(t)

        async def go():
            await c.open()
            await c.read_range(5000, 1)

        with self.assertRaises(TransferError) as ctx:
            run(go())
        self.assertFalse(ctx.exception.retryable)

    def test_explicit_delete_only(self):
        t = FakeTransport(self.ring)
        c = DumpClient(t)

        async def go():
            await c.open()
            await c.advance(300)
            a = await c.info()
            await c.clear()
            b = await c.info()
            return a, b

        a, b = run(go())
        self.assertEqual((a.read_seq, a.write_seq), (300, 1000))
        self.assertEqual((b.read_seq, b.write_seq), (0, 0))

    def test_time_sync_writes_little_endian_epoch(self):
        t = FakeTransport(self.ring, rtc_valid=False)
        c = DumpClient(t)

        async def go():
            await c.open()
            e = await c.sync_time(1_800_000_000)
            return e, await c.status()

        e, st = run(go())
        self.assertEqual(t.time_writes, [1_800_000_000])
        self.assertTrue(st.rtc_valid)


class RingFullModelTests(unittest.TestCase):
    """Documents the storage-full contract the firmware implements (sd_card.c)."""

    def test_full_ring_keeps_old_audio_and_drops_new(self):
        ring = FakeRing(capacity=10)
        ring.record_session(100, 10)
        self.assertFalse(ring.record(200))
        self.assertTrue(ring.full)
        self.assertEqual(ring.dropped, 1)
        self.assertEqual(ring.records[0][:4], (100).to_bytes(4, "big"))  # oldest untouched
        ring.advance(2)  # explicit delete frees space
        self.assertTrue(ring.record(200))
        self.assertFalse(ring.full)


if __name__ == "__main__":
    unittest.main()
