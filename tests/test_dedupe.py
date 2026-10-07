"""Omi recordings overlapping a Mac meeting lose only the duplicated speech (spec §3, Plan 2b)."""

from __future__ import annotations
import asyncio
import dataclasses
import json
import math
import os
import random
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


def pseudo_speech(seconds, seed):
    """Mean-square 50 ms frames shaped like speech: syllable bursts with pauses. Burstier than
    real speech, so chance matches are more likely here than on real recordings."""
    r = random.Random(seed)
    out = []
    while len(out) < seconds / 0.05:
        if r.random() < 0.25:
            out += [r.uniform(0, 40) ** 2] * r.randint(4, 16)
        else:
            amp, k = r.uniform(300, 3000), r.randint(2, 6)
            out += [(amp * math.sin(math.pi * (i + 0.5) / k)) ** 2 for i in range(k)]
    return out[: int(seconds / 0.05)]


def second_mic(frames, seed, gain=0.5, floor=60.0):
    """The same speech heard by another microphone: different level, jitter, a noise floor."""
    r = random.Random(seed)
    return [v * gain * r.uniform(0.6, 1.4) + floor ** 2 * r.uniform(0.5, 1.5) for v in frames]


class DecisionTests(unittest.TestCase):
    """A 2-minute Omi recording: the owner on a call for 60 s, then someone in the room for 60 s.
    The meeting started at the same epoch; the Mac clock is 3.7 s late (74 frames). The meeting's
    remote channel is busy with other speech throughout, which is what makes chance matches likely."""

    @classmethod
    def setUpClass(cls):
        call, room, remote = pseudo_speech(120, 1), pseudo_speech(120, 2), pseudo_speech(200, 3)
        cls.omi = call[:1200] + room[1200:2400]
        left = [0.0] * 74 + second_mic(call[:1200], 4) + second_mic([0.0] * 1200, 5)
        cls.meeting = {"capture_id": "ab" * 16, "start_ms": 1_000_000, "end_ms": 1_120_000,
                        "left": left + [0.0] * 500, "right": remote[: len(left) + 500]}
        r = random.Random(7)
        cls.segments, t = [], 0.0
        while t < 118:
            length = r.uniform(2.0, 6.0)
            cls.segments.append({"start": round(t, 2), "end": round(t + length, 2), "text": f"s{t:.0f}"})
            t += length + r.uniform(0.2, 1.0)

    def test_constants_are_the_measured_ones(self):
        from second_brain.adaptive import dedupe
        self.assertEqual((dedupe.DROP_THRESHOLD, dedupe.ALIGNMENT_THRESHOLD, dedupe.SEARCH_SECONDS,
                          dedupe.SEGMENT_TOLERANCE, dedupe.MIN_SEGMENT_SECONDS, dedupe.MIN_ALIGN_SECONDS,
                          dedupe.MAX_ALIGN_SECONDS),
                         (0.6, 0.5, 10.0, 0.1, 2.0, 30.0, 600.0))

    def test_one_offset_is_estimated_per_meeting(self):
        from second_brain.adaptive.dedupe import decide
        decisions = decide(self.segments, self.omi, 1000, [self.meeting])
        (alignment,) = decisions.alignments
        self.assertEqual((alignment["status"], alignment["channel"], alignment["offset_seconds"]),
                         ("aligned", "L", 3.7))
        self.assertGreaterEqual(alignment["score"], 0.5)
        self.assertFalse(decisions.alignment_failed)

    def test_the_call_is_dropped_and_the_room_is_kept(self):
        from second_brain.adaptive.dedupe import decide
        decisions = decide(self.segments, self.omi, 1000, [self.meeting])
        dropped = {d["text"] for d in decisions.dropped}
        call = [s["text"] for s in self.segments if s["end"] <= 60]
        room = [s["text"] for s in self.segments if s["start"] >= 60]
        self.assertEqual(sorted(dropped & set(call)), sorted(call))
        self.assertEqual(dropped & set(room), set())
        record = decisions.dropped[0]
        self.assertEqual((record["deduped_by"], record["deduped_channel"]), ("ab" * 16, "L"))
        self.assertNotIn("channel", record)          # `channel` means mic/remote on meeting segments
        self.assertGreaterEqual(record["score"], 0.6)
        self.assertAlmostEqual(record["offset_seconds"], 3.7, delta=0.1)

    def test_short_room_speech_against_a_busy_meeting_is_almost_never_dropped(self):
        """The regression this revision exists for: 150 random 2-3 s room segments while the
        remote channel is full of other speech. (Each call re-estimates the alignment, so the
        sample is kept small enough to run in a few seconds.)"""
        from second_brain.adaptive.dedupe import decide
        r, dropped = random.Random(11), 0
        for _ in range(150):
            start = r.uniform(60, 115)
            segment = {"start": start, "end": start + r.uniform(2.0, 3.0), "text": "room"}
            dropped += bool(decide([segment], self.omi, 1000, [self.meeting]).dropped)
        self.assertLessEqual(dropped / 150, 0.02)

    def test_segments_shorter_than_two_seconds_are_never_dropped(self):
        from second_brain.adaptive.dedupe import decide
        short = [{"start": 10.0, "end": 11.9, "text": "duplicate but short"}]
        decisions = decide(short, self.omi, 1000, [self.meeting])
        self.assertEqual(decisions.dropped, [])
        self.assertNotIn("dedupe_score", decisions.kept[0])

    def test_an_unrelated_meeting_fails_alignment_and_drops_nothing(self):
        from second_brain.adaptive.dedupe import decide
        other = {"capture_id": "cd" * 16, "start_ms": 1_000_000, "end_ms": 1_120_000,
                  "left": pseudo_speech(130, 9), "right": pseudo_speech(130, 10)}
        decisions = decide(self.segments, self.omi, 1000, [other])
        self.assertEqual(decisions.alignments[0]["status"], "failed")
        self.assertLess(decisions.alignments[0]["score"], 0.5)
        self.assertTrue(decisions.alignment_failed)
        self.assertEqual(decisions.dropped, [])
        self.assertEqual(len(decisions.kept), len(self.segments))

    def test_too_little_overlap_is_not_an_alignment_failure(self):
        from second_brain.adaptive.dedupe import decide
        # The meeting starts 100 s into the Omi recording, leaving 20 s of overlap.
        late = dict(self.meeting, start_ms=1_100_000, end_ms=1_220_000)
        decisions = decide(self.segments, self.omi, 1000, [late])
        self.assertEqual(decisions.alignments[0]["status"], "too little overlap")
        self.assertFalse(decisions.alignment_failed)
        self.assertEqual(decisions.dropped, [])

    def test_a_meeting_on_the_remote_channel_aligns_too(self):
        from second_brain.adaptive.dedupe import decide
        swapped = dict(self.meeting, left=self.meeting["right"], right=self.meeting["left"])
        decisions = decide(self.segments, self.omi, 1000, [swapped])
        self.assertEqual(decisions.alignments[0]["channel"], "R")
        self.assertTrue(decisions.dropped)
        self.assertTrue(all(d["deduped_channel"] == "R" for d in decisions.dropped))

    def test_no_meetings_keeps_everything_without_failing(self):
        from second_brain.adaptive.dedupe import decide
        decisions = decide(self.segments, self.omi, 1000, [])
        self.assertEqual((decisions.dropped, decisions.alignments, decisions.alignment_failed), ([], [], False))
        self.assertEqual(len(decisions.kept), len(self.segments))


if __name__ == "__main__":
    unittest.main()
