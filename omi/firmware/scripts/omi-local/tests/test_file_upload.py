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
