import struct
import unittest

from omi_local import protocol as P
from tests.fake_device import make_record, make_frame


class EncodeDecodeTests(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(P.encode_info(), b"\x10")
        self.assertEqual(P.encode_read(5, 3), b"\x11" + (5).to_bytes(8, "big") + (3).to_bytes(4, "big"))
        self.assertEqual(P.encode_advance(9), b"\x12" + (9).to_bytes(8, "big"))
        self.assertEqual(P.encode_clear(), b"\x13")
        self.assertEqual(P.encode_stop(), b"\x03")
        self.assertEqual(P.encode_time_sync(0x01020304), b"\x04\x03\x02\x01")  # little-endian, like the MCU

    def test_info_with_and_without_codec(self):
        raw31 = struct.pack(">BQQIQH", 2, 10, 20, 1000, 3, 444)
        info = P.parse_notification(raw31)
        self.assertEqual((info.read_seq, info.write_seq, info.capacity_packets, info.dropped_packets, info.packet_size),
                         (10, 20, 1000, 3, 444))
        self.assertIsNone(info.codec_id)
        self.assertEqual(info.unread_packets, 10)
        self.assertEqual(info.free_packets, 990)
        info32 = P.parse_notification(raw31 + bytes([21]))
        self.assertEqual(info32.codec_id, 21)

    def test_other_notifications(self):
        self.assertEqual(P.parse_notification(b"\x01\x09"), P.Ack(9))
        self.assertEqual(P.parse_notification(b"\x03abc"), P.Data(b"abc"))
        self.assertEqual(P.parse_notification(struct.pack(">BBQ", 4, 0, 77)), P.Done(0, 77))
        self.assertEqual(P.parse_notification(struct.pack(">BQI", 5, 7, 2)), P.ReadBegin(7, 2))
        with self.assertRaises(P.ProtocolError):
            P.parse_notification(b"\x42")
        with self.assertRaises(P.ProtocolError):
            P.parse_notification(b"")

    def test_status(self):
        st = P.parse_status(struct.pack("<IIII", 444, 1, 999, 1))
        self.assertEqual(st, P.Status(444, 1, 999, True))


class RecordTests(unittest.TestCase):
    def test_parse_record_frames_and_padding(self):
        raw = make_record(3, 1_700_000_000, frames_per_record=5)
        rec = P.parse_record(3, raw)
        self.assertEqual(rec.timestamp, 1_700_000_000)
        self.assertEqual(len(rec.frames), 5)
        self.assertEqual(rec.frames[0], make_frame(3 * 16))
        self.assertTrue(rec.has_time)

    def test_record_without_time(self):
        rec = P.parse_record(0, make_record(0, 0))
        self.assertFalse(rec.has_time)

    def test_bad_length_rejected(self):
        with self.assertRaises(P.ProtocolError):
            P.parse_record(0, b"\0" * 10)
        bad = bytearray(make_record(0, 1))
        bad[4 + 4 * 81] = 200  # 5th frame's length byte: 329 + 200 > 444 overruns the record
        with self.assertRaises(P.ProtocolError):
            P.parse_record(0, bytes(bad))

    def test_iter_records_ignores_trailing_partial(self):
        data = make_record(0, 5) + make_record(1, 5) + b"\x00" * 10
        recs = list(P.iter_records(0, data))
        self.assertEqual([r.seq for r in recs], [0, 1])


class OpusTimingTests(unittest.TestCase):
    def test_toc_durations(self):
        self.assertEqual(P.opus_packet_duration_ms(bytes([23 << 3])), 20.0)  # CELT WB 20 ms (what the Omi produces)
        self.assertEqual(P.opus_packet_duration_ms(bytes([20 << 3])), 2.5)
        self.assertEqual(P.opus_packet_duration_ms(bytes([1 << 3])), 20.0)  # SILK NB 20 ms
        self.assertEqual(P.opus_packet_duration_ms(bytes([13 << 3])), 20.0)  # hybrid 20 ms
        self.assertEqual(P.opus_packet_duration_ms(bytes([(23 << 3) | 1])), 40.0)  # two frames
        self.assertEqual(P.opus_packet_duration_ms(bytes([(23 << 3) | 3, 3])), 60.0)  # code 3, 3 frames
        self.assertEqual(P.opus_packet_samples_48k(bytes([23 << 3])), 960)
        self.assertEqual(P.opus_packet_duration_ms(b""), 0.0)


class SessionSplitTests(unittest.TestCase):
    def _recs(self, spec):
        return [P.Record(seq, ts, (make_frame(seq),)) for seq, ts in spec]

    def test_splits_on_time_gap_seq_gap_and_clock_state(self):
        recs = self._recs([(0, 100), (1, 100), (2, 101), (3, 200), (4, 200), (6, 201), (7, 0), (8, 0), (9, 300)])
        sessions = P.split_sessions(recs, gap_s=60)
        ranges = [(s.start_seq, s.end_seq, s.has_time) for s in sessions]
        self.assertEqual(ranges, [(0, 3, True), (3, 5, True), (6, 7, True), (7, 9, False), (9, 10, True)])
        self.assertEqual(sessions[0].frames, 3)
        self.assertEqual(sessions[0].audio_ms, 60.0)

    def test_small_backwards_jitter_does_not_split(self):
        recs = self._recs([(0, 100), (1, 99), (2, 100)])
        self.assertEqual(len(P.split_sessions(recs)), 1)


if __name__ == "__main__":
    unittest.main()
