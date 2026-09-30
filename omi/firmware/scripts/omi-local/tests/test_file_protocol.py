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
