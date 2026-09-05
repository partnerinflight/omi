import io
import struct
import unittest

from omi_local import oggopus
from tests.fake_device import make_frame


def _bitwise_ogg_crc(data: bytes) -> int:
    """Independent bit-serial CRC-32 with poly 0x04c11db7, init 0, no reflection, no xor-out (RFC 3533 §6)."""
    crc = 0
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


class CrcTests(unittest.TestCase):
    def test_matches_bit_serial_reference(self):
        self.assertEqual(oggopus.ogg_crc(b""), 0)
        for sample in (b"a", b"OggS", bytes(range(256)), b"\xff" * 300, b"omi-local" * 40):
            self.assertEqual(oggopus.ogg_crc(sample), _bitwise_ogg_crc(sample), sample[:8])


class MuxRoundTripTests(unittest.TestCase):
    def test_write_and_parse_back(self):
        buf = io.BytesIO()
        w = oggopus.OggOpusWriter(buf, serial=0x1234, comments=["A=b"])
        frames = [make_frame(i) for i in range(120)]  # > MAX_PACKETS_PER_PAGE, forces multiple pages
        for f in frames:
            w.write_packet(f)
        w.close(eos=True)
        pages = list(oggopus.iter_pages(buf.getvalue()))
        self.assertTrue(all(p.crc_ok for p in pages))
        self.assertEqual(pages[0].header_type, 0x02)  # BOS
        self.assertTrue(pages[0].packets[0].startswith(b"OpusHead"))
        head = pages[0].packets[0]
        version, channels, preskip, rate = struct.unpack_from("<BBHI", head, 8)
        self.assertEqual((version, channels, preskip, rate), (1, 1, 0, 16000))
        self.assertTrue(pages[1].packets[0].startswith(b"OpusTags"))
        self.assertIn(b"A=b", pages[1].packets[0])
        audio = [pkt for p in pages[2:] for pkt in p.packets]
        self.assertEqual(audio, frames)
        self.assertEqual(pages[-1].header_type & 0x04, 0x04)  # EOS
        self.assertEqual(pages[-1].granule, 120 * 960)  # 120 x 20 ms at 48 kHz
        self.assertEqual([p.page_seq for p in pages], list(range(len(pages))))
        self.assertTrue(all(p.serial == 0x1234 for p in pages))

    def test_resume_appends_consistently(self):
        buf = io.BytesIO()
        w = oggopus.OggOpusWriter(buf, serial=7)
        for i in range(10):
            w.write_packet(make_frame(i))
        state = w.close(eos=False)  # resumable: no EOS page yet
        w2 = oggopus.OggOpusWriter(buf, serial=0, resume_state=oggopus.MuxState.from_dict(state.to_dict()))
        for i in range(10, 20):
            w2.write_packet(make_frame(i))
        w2.close(eos=True)
        pages = list(oggopus.iter_pages(buf.getvalue()))
        self.assertTrue(all(p.crc_ok for p in pages))
        self.assertEqual([p.page_seq for p in pages], list(range(len(pages))))
        self.assertEqual(pages[-1].granule, 20 * 960)
        self.assertEqual(pages[-1].header_type & 0x04, 0x04)
        self.assertEqual(sum(len(p.packets) for p in pages[2:]), 20)

    def test_long_packet_lacing(self):
        buf = io.BytesIO()
        w = oggopus.OggOpusWriter(buf, serial=1)
        big = bytes([0xB8]) + bytes(600)  # needs 255+255+91 lacing values
        w.write_packet(big)
        w.close()
        pages = list(oggopus.iter_pages(buf.getvalue()))
        self.assertEqual(pages[2].packets, (big,))
        self.assertTrue(all(p.crc_ok for p in pages))


if __name__ == "__main__":
    unittest.main()
