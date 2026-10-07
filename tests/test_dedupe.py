"""Omi recordings overlapping a Mac meeting lose only the duplicated speech (spec §3, Plan 2b)."""

from __future__ import annotations
import asyncio
import dataclasses
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tone_wav(path: Path, spec: str, seconds: float, rate: int = 16000, channels: int = 1) -> Path:
    """One ffmpeg aevalsrc expression per channel, separated by '|'. Quoted: the expressions
    contain commas (see the Plan 2a fixture)."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"aevalsrc='{spec}':s={rate}:d={seconds}", "-ac", str(channels),
         "-c:a", "pcm_s16le", str(path)],
        check=True,
    )
    return path


class EnvelopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        # 10 s mono: 1 kHz speech-band bursts at 1-3 s and 6-8 s, silence elsewhere.
        cls.bursts = tone_wav(cls.root / "bursts.wav",
                              "0.4*sin(2*PI*1000*t)*(between(t,1,3)+between(t,6,8))", 10)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_mono_frames_are_50ms_speech_band_mean_squares(self):
        from second_brain.adaptive.channels import FRAME_SECONDS, mono_frames
        frames = mono_frames(self.bursts)
        self.assertAlmostEqual(len(frames), 200, delta=2)
        self.assertEqual(FRAME_SECONDS, 0.05)
        loud = [i * FRAME_SECONDS for i, v in enumerate(frames) if v > max(frames) / 100]
        self.assertTrue(all(1 <= t < 3 or 6 <= t < 8.1 for t in loud), loud[:5])

    def test_rms_envelope_is_the_square_root_of_a_frame_span(self):
        from second_brain.adaptive.channels import rms_envelope
        self.assertEqual(rms_envelope([0.0, 4.0, 9.0], 0, 3), [0.0, 2.0, 3.0])
        self.assertEqual(rms_envelope([4.0] * 10, 0.1, 0.2), [2.0, 2.0])  # seconds -> frames

    def test_an_aligned_segment_matches_exactly_and_an_offset_one_is_found(self):
        from second_brain.adaptive.channels import match_envelope
        shape = [0.0, 1.0, 9.0, 3.0, 0.0, 0.0] * 4          # 24 frames = 1.2 s
        envelope = [math.sqrt(v) for v in shape]
        meeting = lambda pad: [0.0] * pad + shape + [0.0] * 500
        # The matching audio sits 10 s into the meeting; the segment claims to be there.
        self.assertEqual(match_envelope(envelope, meeting(200), 10.0, 10.0), (1.0, 0.0))
        self.assertEqual(match_envelope(envelope, meeting(274), 10.0, 10.0), (1.0, 3.7))   # Omi clock behind
        self.assertEqual(match_envelope(envelope, meeting(126), 10.0, 10.0), (1.0, -3.7))  # Omi clock ahead

    def test_an_offset_beyond_the_search_window_is_not_matched(self):
        from second_brain.adaptive.channels import match_envelope
        shape = [0.0, 1.0, 9.0, 3.0, 0.0, 0.0] * 4
        envelope = [math.sqrt(v) for v in shape]
        self.assertEqual(match_envelope(envelope, [0.0] * 600 + shape + [0.0] * 500, 10.0, 10.0), (None, 0.0))

    def test_unrelated_speech_in_the_room_does_not_match(self):
        from second_brain.adaptive.channels import match_envelope
        shape = [0.0, 1.0, 9.0, 3.0, 0.0, 0.0] * 4
        room = [math.sqrt(v) for v in [9.0, 0.0, 0.0, 1.0, 9.0, 0.0] * 4]
        peak, _ = match_envelope(room, [0.0] * 200 + shape + [0.0] * 500, 10.0, 10.0)
        self.assertLess(peak, 0.4)

    def test_only_full_length_windows_are_compared(self):
        """A partial overlap of two frames correlates at 1.0 by construction; it must never count,
        or real room speech near a meeting edge is dropped."""
        from second_brain.adaptive.channels import match_envelope
        envelope = [math.sqrt(v) for v in [0.0, 1.0, 9.0, 3.0, 0.0, 0.0] * 4]
        # Meeting frames shorter than the segment: no full-length window exists anywhere.
        self.assertEqual(match_envelope(envelope, [0.0, 1.0, 9.0], 0.0, 10.0), (None, 0.0))

    def test_a_segment_too_short_to_identify_is_never_matched(self):
        from second_brain.adaptive.channels import MIN_MATCH_FRAMES, match_envelope
        self.assertEqual(MIN_MATCH_FRAMES, 16)              # 0.8 s at 50 ms frames
        shape = [0.0, 1.0, 9.0, 3.0, 0.0, 0.0] * 4
        short = [math.sqrt(v) for v in shape[:8]]
        self.assertEqual(match_envelope(short, [0.0] * 200 + shape + [0.0] * 500, 10.0, 10.0), (None, 0.0))

    def test_a_flat_envelope_on_either_side_never_matches(self):
        from second_brain.adaptive.channels import match_envelope
        shape = [0.0, 1.0, 9.0, 3.0, 0.0, 0.0] * 4
        envelope = [math.sqrt(v) for v in shape]
        self.assertEqual(match_envelope([0.0] * 24, [0.0] * 200 + shape + [0.0] * 500, 10.0, 10.0), (None, 0.0))
        self.assertEqual(match_envelope(envelope, [1.0] * 800, 10.0, 10.0), (None, 0.0))
        self.assertEqual(match_envelope([], [1.0] * 800, 10.0, 10.0), (None, 0.0))


if __name__ == "__main__":
    unittest.main()
