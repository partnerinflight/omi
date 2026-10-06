"""Wi-Fi upload path: protocol codecs, receiver server, and a software model of
the device's upload client (mirrors wifi_upload.c), including auth failures,
disconnects mid-chunk and resume."""

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omi_local import protocol as P
from omi_local import upload_protocol as U
from omi_local.oggopus import iter_pages
from omi_local.server import UploadServer
from omi_local.state import StateStore
from tests.fake_device import FakeRing


class FakeUploader:
    """The device side of the protocol, as wifi_upload.c implements it."""

    def __init__(self, ring: FakeRing, secret: bytes, device_id: bytes = b"\x11\x22\x33\x44\x55\x66",
                 chunk: int = 36, drop_after_chunks: int | None = None, advance_every: int = 4 * 36,
                 stall_after_chunks: int | None = None):
        self.ring = ring
        self.secret = secret
        self.device_id = device_id
        self.chunk = chunk
        self.drop_after_chunks = drop_after_chunks
        self.advance_every = advance_every
        self.result = None
        self.uploaded = 0
        self.stall_after_chunks = stall_after_chunks

    async def _recv(self, reader):
        hdr = await reader.readexactly(U.HEADER_LEN)
        t, n = U.parse_header(hdr)
        return t, (await reader.readexactly(n) if n else b"")

    async def run(self, host, port):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            cn = secrets.token_bytes(16)
            writer.write(U.frame(U.MSG_HELLO, U.encode_hello(self.device_id, cn)))
            await writer.drain()
            t, payload = await self._recv(reader)
            assert t == U.MSG_CHALLENGE and len(payload) == 48
            sn, tag = payload[:16], payload[16:]
            if not hmac.compare_digest(tag, U.auth_tag(self.secret, U.LABEL_SERVER, cn, sn)):
                self.result = "auth"
                return
            info = P.Info(self.ring.read_seq, self.ring.write_seq, self.ring.capacity, self.ring.dropped,
                          P.RECORD_SIZE, P.CODEC_ID_OPUS)
            writer.write(U.frame(U.MSG_AUTH, U.encode_auth(U.auth_tag(self.secret, U.LABEL_CLIENT, cn, sn), info)))
            await writer.drain()
            t, payload = await self._recv(reader)
            if t == U.MSG_REJECT:
                self.result = f"rejected {payload[0]}"
                return
            assert t == U.MSG_START
            seq = U.parse_u64(payload)
            assert self.ring.read_seq <= seq <= self.ring.write_seq
            end = self.ring.write_seq
            last_adv = seq
            chunks = 0
            while seq < end:
                if chunks == self.stall_after_chunks:
                    await asyncio.sleep(.15)
                n = min(self.chunk, end - seq)
                recs = b"".join(self.ring.records[s] for s in range(seq, seq + n))
                if self.drop_after_chunks is not None and chunks == self.drop_after_chunks:
                    # simulate the Wi-Fi link dying mid-frame: send half a DATA frame and vanish
                    f = U.frame(U.MSG_DATA, U.encode_data(seq, recs))
                    writer.write(f[: len(f) // 2])
                    await writer.drain()
                    self.result = "link lost"
                    return
                writer.write(U.frame(U.MSG_DATA, U.encode_data(seq, recs)))
                await writer.drain()
                t, payload = await self._recv(reader)
                assert t == U.MSG_ACK, t
                nxt = U.parse_u64(payload)
                assert nxt == seq + n
                seq = nxt
                self.uploaded += n
                chunks += 1
                if seq - last_adv >= self.advance_every:
                    assert self.ring.advance(seq) == P.STATUS_OK
                    last_adv = seq
            if seq > last_adv:
                self.ring.advance(seq)
            writer.write(U.frame(U.MSG_DONE, U.encode_u64(seq)))
            await writer.drain()
            t, payload = await self._recv(reader)
            assert t == U.MSG_BYE
            self.result = "ok"
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


def run(coro):
    return asyncio.run(coro)


class ProtocolCodecTests(unittest.TestCase):
    def test_status_extension_keeps_old_firmware_compatible(self):
        old = struct.pack('<BBBbiIIIII', 1, 0, 10, 0, -140, 1, 40, 80, 100, 200)
        self.assertIsNone(U.parse_upload_status(old).last_stage)
        status = U.parse_upload_status(old + bytes([6, 1, 0, 0, 192, 168, 1, 40, 7, 5]))
        self.assertEqual((status.last_stage, status.retries_remaining), (7, 5))
        self.assertEqual(status.result_name, 'aborted')

    def test_frame_timeout_identifies_header_or_payload(self):
        async def check():
            server = UploadServer(b'x' * 32, Path('.'))
            reader = asyncio.StreamReader()
            with self.assertRaisesRegex(TimeoutError, 'frame header'):
                await server._read_frame(reader, 64, .01)
            reader.feed_data(U.frame(U.MSG_DATA, b'123')[:U.HEADER_LEN])
            with self.assertRaisesRegex(TimeoutError, 'frame payload'):
                await server._read_frame(reader, 64, .01)
        run(check())
    def test_frames_and_payloads_roundtrip(self):
        cn = bytes(range(16))
        h = U.parse_hello(U.encode_hello(b"\x01\x02\x03\x04\x05\x06", cn))
        self.assertEqual((h.version, h.device_id_str, h.client_nonce), (1, "01-02-03-04-05-06", cn))
        info = P.Info(5, 99, 1000, 2, 444, 21)
        a = U.parse_auth(U.encode_auth(b"\xaa" * 32, info))
        self.assertEqual((a.client_tag, a.info), (b"\xaa" * 32, info))
        d = U.parse_data(U.encode_data(7, b"\x00" * (2 * 444)))
        self.assertEqual((d.seq, d.count, len(d.records)), (7, 2, 888))
        t, n = U.parse_header(U.frame(U.MSG_ACK, U.encode_u64(9))[:5])
        self.assertEqual((t, n), (U.MSG_ACK, 8))
        with self.assertRaises(U.UploadProtocolError):
            U.parse_data(struct.pack(">QH", 1, 3) + b"\x00" * 444)
        with self.assertRaises(U.UploadProtocolError):
            U.parse_hello(b"NOPE" + bytes(23))

    def test_auth_tag_matches_firmware_definition(self):
        sec, cn, sn = b"s" * 32, b"c" * 16, b"n" * 16
        self.assertEqual(U.auth_tag(sec, U.LABEL_SERVER, cn, sn),
                         hmac.new(sec, b"omi-local-srv" + cn + sn, hashlib.sha256).digest())

    def test_tlv_encoding(self):
        blob = U.encode_wifi_config(ssid="Home", password="hunter22", host="192.168.1.20", port=7331,
                                    secret=b"\x01" * 32, enabled=True)
        self.assertEqual(blob[:6], bytes([U.TLV_SSID, 4]) + b"Home")
        self.assertIn(bytes([U.TLV_HOSTNAME, 12]) + b"192.168.1.20", blob)
        self.assertIn(bytes([U.TLV_PORT, 2]) + (7331).to_bytes(2, "big"), blob)
        self.assertTrue(blob.endswith(bytes([U.TLV_ENABLE, 1, 1])))
        self.assertLessEqual(len(blob), 192)  # fits the firmware's pending buffer
        self.assertEqual(U.encode_wifi_config(forget=True), bytes([U.TLV_FORGET, 0]))
        with self.assertRaises(ValueError):
            U.encode_wifi_config(host="http://receiver.local")
        with self.assertRaises(ValueError):
            U.encode_wifi_config(password="short")

    def test_status_parsing(self):
        raw = struct.pack("<BBBbiIIIII", 1, 7, 0, 0, 0, 3, 5000, 120, 40000, 71000)
        st = U.parse_upload_status(raw)
        self.assertTrue(st.configured)
        self.assertEqual((st.state_name, st.result_name, st.sessions_ok, st.heap_free), ("uploading", "ok", 3, 40000))


class ServerTests(unittest.TestCase):
    def test_timeout_keeps_checkpoint_and_next_session_resumes(self):
        up = FakeUploader(self.ring, self.secret, stall_after_chunks=3)
        async def stall(server):
            try:
                await up.run('127.0.0.1', server.bound_port)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            return server
        with patch('omi_local.server.DATA_TIMEOUT_S', .05), self.assertLogs('omi_local.serve', level='WARNING') as logs:
            server = run(self._with_server(stall))
        self.assertEqual(server.sessions_failed, 1)
        self.assertIn('waiting for frame header', logs.output[0])
        self.assertIn('next_seq=108', logs.output[0])
        self.assertIn('target_seq=300', logs.output[0])
        self.assertEqual(StateStore(self.dest).get('11-22-33-44-55-66').downloaded_through, 108)
        resumed = FakeUploader(self.ring, self.secret)
        async def resume(server):
            await resumed.run('127.0.0.1', server.bound_port)
        run(self._with_server(resume))
        self.assertEqual(resumed.uploaded, 192)
        self.assertEqual(resumed.result, 'ok')
        self.assertEqual(self.ring.read_seq, 300)

    def setUp(self):
        self.secret = secrets.token_bytes(32)
        self.ring = FakeRing(capacity=5000)
        self.ring.record_session(1_700_000_000, 300)  # 8+ chunks
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def _with_server(self, coro_fn, secret=None):
        server = UploadServer(secret or self.secret, self.dest, host="127.0.0.1", port=0)
        await server.start()
        try:
            return await coro_fn(server)
        finally:
            await server.close()

    def test_full_upload_persists_files_and_device_deletes_only_acked(self):
        # Explicit recorder boundary, independent of the upload's DONE frame.
        self.ring.records[300] = struct.pack('>I', 1_700_000_030) + P.RECORD_END_MAGIC + bytes(
            P.AUDIO_PAYLOAD_BYTES - len(P.RECORD_END_MAGIC))
        self.ring.write_seq = 301
        up = FakeUploader(self.ring, self.secret)

        async def go(server):
            await up.run("127.0.0.1", server.bound_port)
            return server

        server = run(self._with_server(go))
        self.assertEqual(up.result, "ok")
        self.assertEqual(up.uploaded, 301)
        self.assertEqual(self.ring.read_seq, 301)  # delete-after-verified-upload
        self.assertEqual(server.sessions_ok, 1)
        files = list(self.dest.glob("*.opus"))
        self.assertEqual(len(files), 1)
        pages = list(iter_pages(files[0].read_bytes()))
        self.assertTrue(all(p.crc_ok for p in pages))
        self.assertEqual(sum(len(p.packets) for p in pages[2:]), 300 * 5)
        self.assertEqual(pages[-1].header_type & 0x04, 0x04)
        meta = json.loads(files[0].with_suffix(".json").read_text())
        self.assertEqual((meta["start_seq"], meta["end_seq"], meta["complete"]), (0, 300, True))
        st = StateStore(self.dest).get("11-22-33-44-55-66")
        self.assertEqual(st.downloaded_through, 301)
        self.assertIsNone(st.open_file)

    def test_completed_upload_does_not_split_a_recording(self):
        async def upload(server):
            up = FakeUploader(self.ring, self.secret)
            await up.run('127.0.0.1', server.bound_port)
            self.assertEqual(up.result, 'ok')
        run(self._with_server(upload))
        self.assertIsNotNone(StateStore(self.dest).get('11-22-33-44-55-66').open_file)
        self.ring.record_session(1_700_000_030, 10)
        run(self._with_server(upload))
        files = list(self.dest.glob('*.opus'))
        self.assertEqual(len(files), 1)
        pages = list(iter_pages(files[0].read_bytes()))
        self.assertTrue(all(p.crc_ok for p in pages))
        self.assertEqual(sum(len(p.packets) for p in pages[2:]), 310 * 5)
        self.assertFalse(pages[-1].header_type & 4)

    def test_wrong_secret_is_rejected_before_any_audio(self):
        up = FakeUploader(self.ring, secrets.token_bytes(32))

        async def go(server):
            await up.run("127.0.0.1", server.bound_port)
            return server

        server = run(self._with_server(go))
        self.assertEqual(up.result, "auth")  # device refused to talk to an impostor receiver
        self.assertEqual(self.ring.read_seq, 0)
        self.assertEqual(list(self.dest.glob("*.opus")), [])

        # And a device with the wrong secret is refused by the receiver.
        server_secret = secrets.token_bytes(32)
        up2 = FakeUploader(self.ring, server_secret)
        # Make the device present a good server check but a bad client tag by
        # giving the server a different secret than the device.
        async def go2(server):
            await up2.run("127.0.0.1", server.bound_port)
            return server

        run(self._with_server(go2, secret=secrets.token_bytes(32)))
        self.assertIn(up2.result, ("auth", "rejected 1"))
        self.assertEqual(self.ring.read_seq, 0)

    def test_link_lost_mid_chunk_then_resume(self):
        up = FakeUploader(self.ring, self.secret, drop_after_chunks=3)

        async def go(server):
            await up.run("127.0.0.1", server.bound_port)
            return server

        server = run(self._with_server(go))
        self.assertEqual(up.result, "link lost")
        acked = up.uploaded
        self.assertEqual(acked, 3 * 36)
        self.assertEqual(server.sessions_failed, 1)
        # The receiver persisted exactly the ACKed chunks; the device kept the rest.
        st = StateStore(self.dest).get("11-22-33-44-55-66")
        self.assertEqual(st.downloaded_through, acked)
        self.assertGreaterEqual(self.ring.read_seq, 0)
        self.assertLessEqual(self.ring.read_seq, acked)
        # Second session (device reconnects on the next charger poll) resumes from what the receiver has.
        up2 = FakeUploader(self.ring, self.secret)

        async def go2(server):
            await up2.run("127.0.0.1", server.bound_port)
            return server

        server2 = run(self._with_server(go2))
        self.assertEqual(up2.result, "ok")
        self.assertEqual(server2.sessions_ok, 1)
        self.assertEqual(up2.uploaded, 300 - acked)  # no re-upload of persisted chunks
        self.assertEqual(self.ring.read_seq, 300)
        files = sorted(self.dest.glob("*.opus"))
        self.assertEqual(len(files), 1)  # resumed into the same session file
        pages = list(iter_pages(files[0].read_bytes()))
        self.assertTrue(all(p.crc_ok for p in pages))
        self.assertEqual(sum(len(p.packets) for p in pages[2:]), 300 * 5)
        self.assertEqual([p.page_seq for p in pages], list(range(len(pages))))

    def test_corrupt_record_is_never_acked(self):
        bad = bytearray(self.ring.records[10])
        bad[4 + 4 * 81] = 200  # overrunning frame length
        self.ring.records[10] = bytes(bad)
        up = FakeUploader(self.ring, self.secret, chunk=5)

        async def go(server):
            try:
                await up.run("127.0.0.1", server.bound_port)
            except (AssertionError, asyncio.IncompleteReadError):
                pass
            return server

        run(self._with_server(go))
        self.assertLessEqual(self.ring.read_seq, 10)
        self.assertEqual(StateStore(self.dest).get("11-22-33-44-55-66").downloaded_through, 10)


if __name__ == "__main__":
    unittest.main()
