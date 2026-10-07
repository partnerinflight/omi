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


class DecisionTests(unittest.TestCase):
    SHAPE = [0.0, 1.0, 9.0, 3.0, 0.0, 0.0] * 4        # 24 frames = 1.2 s of structured speech
    ROOM = [9.0, 0.0, 0.0, 1.0, 9.0, 0.0] * 4

    def meeting(self, left, right, start_ms=1_000_000, end_ms=1_100_000, cid="ab" * 16):
        return {"capture_id": cid, "start_ms": start_ms, "end_ms": end_ms, "left": left, "right": right}

    def at(self, pad, frames=None):
        """A meeting envelope holding `frames` starting `pad` frames in."""
        return [0.0] * pad + (self.SHAPE if frames is None else frames) + [0.0] * 500

    def segment(self, start, text, duration=1.2):
        return {"start": start, "end": start + duration, "text": text}

    # The Omi recording starts at epoch 1000; the meeting starts at epoch 1000 too (start_ms
    # 1_000_000), so an Omi offset of 10 s is 10 s into the meeting (frame 200).

    def test_a_segment_matching_the_mic_channel_is_dropped(self):
        from second_brain.adaptive.dedupe import decide
        decisions = decide([self.segment(10.0, "same words")], self.at(200), 1000,
                           [self.meeting(self.at(200), [0.0] * 800)])
        self.assertEqual(len(decisions.dropped), 1)
        dropped = decisions.dropped[0]
        self.assertEqual((dropped["channel"], dropped["deduped_by"]), ("L", "ab" * 16))
        self.assertAlmostEqual(dropped["score"], 1.0, places=3)
        self.assertEqual(dropped["offset_seconds"], 0.0)
        self.assertEqual(decisions.kept, [])
        self.assertFalse(decisions.alignment_failed)

    def test_in_room_speech_during_a_meeting_is_kept(self):
        from second_brain.adaptive.dedupe import decide
        omi = [0.0] * 200 + self.SHAPE + self.ROOM + [0.0] * 500
        decisions = decide([self.segment(10.0, "owner on the call"),
                            self.segment(11.2, "spouse in the room")],
                           omi, 1000, [self.meeting(self.at(200), [0.0] * 800)])
        self.assertEqual([d["text"] for d in decisions.dropped], ["owner on the call"])
        self.assertEqual([k["text"] for k in decisions.kept], ["spouse in the room"])
        self.assertLess(decisions.kept[0]["dedupe_score"], 0.6)

    def test_a_clock_offset_within_ten_seconds_is_tolerated_and_reported(self):
        from second_brain.adaptive.dedupe import decide
        # The Mac holds the same audio 3.7 s later than the Omi's clock claims.
        decisions = decide([self.segment(10.0, "same words")], self.at(200), 1000,
                           [self.meeting(self.at(274), [0.0] * 800)])
        self.assertEqual(len(decisions.dropped), 1)
        self.assertEqual(decisions.dropped[0]["offset_seconds"], 3.7)

    def test_an_offset_beyond_the_search_window_fails_alignment_and_keeps_everything(self):
        """The spec's alignment-failure rule: an overlapping segment that matches nothing anywhere
        is evidence the clocks cannot be reconciled, so no segment may be dropped."""
        from second_brain.adaptive.dedupe import decide
        decisions = decide([self.segment(10.0, "same words")], self.at(200), 1000,
                           [self.meeting(self.at(600), [0.0] * 1200)])
        self.assertEqual(decisions.dropped, [])
        self.assertEqual([k["text"] for k in decisions.kept], ["same words"])
        self.assertTrue(decisions.alignment_failed)

    def test_segments_outside_the_meeting_span_are_never_compared(self):
        from second_brain.adaptive.dedupe import decide
        # The meeting covers epoch 1000-1100; this segment is at epoch 1200.
        decisions = decide([self.segment(200.0, "after the meeting")], self.at(4000), 1000,
                           [self.meeting(self.at(200), [0.0] * 800)])
        self.assertEqual(decisions.dropped, [])
        self.assertEqual(len(decisions.kept), 1)
        self.assertNotIn("dedupe_score", decisions.kept[0])
        self.assertFalse(decisions.alignment_failed)   # nothing overlapped, so alignment never ran

    def test_matching_the_remote_channel_counts_too(self):
        from second_brain.adaptive.dedupe import decide
        decisions = decide([self.segment(10.0, "remote words the omi also heard")], self.at(200), 1000,
                           [self.meeting([0.0] * 800, self.at(200))])
        self.assertEqual(decisions.dropped[0]["channel"], "R")

    def test_a_segment_too_short_to_identify_is_kept(self):
        from second_brain.adaptive.dedupe import decide
        decisions = decide([self.segment(10.0, "yeah", duration=0.4)], self.at(200), 1000,
                           [self.meeting(self.at(200), [0.0] * 800)])
        self.assertEqual(decisions.dropped, [])
        self.assertEqual(len(decisions.kept), 1)
        self.assertNotIn("dedupe_score", decisions.kept[0])   # never compared, so no score

    def test_alignment_failure_restores_a_segment_that_did_match(self):
        """One segment matches the meeting exactly, another overlapping segment matches nothing.
        The drop still stands, because alignment only fails when *nothing* reaches the floor."""
        from second_brain.adaptive.dedupe import decide, ALIGNMENT_THRESHOLD, DROP_THRESHOLD
        self.assertEqual((DROP_THRESHOLD, ALIGNMENT_THRESHOLD), (0.6, 0.3))
        decisions = decide([self.segment(10.0, "matches"), self.segment(40.0, "silence here")],
                           self.at(200) + [0.0] * 400, 1000, [self.meeting(self.at(200), [0.0] * 1600)])
        self.assertEqual([d["text"] for d in decisions.dropped], ["matches"])
        self.assertEqual([k["text"] for k in decisions.kept], ["silence here"])
        self.assertFalse(decisions.alignment_failed)

    def test_a_segment_too_short_to_attempt_is_not_evidence_of_misalignment(self):
        from second_brain.adaptive.dedupe import decide
        decisions = decide([self.segment(10.0, "yeah", duration=0.4)], self.at(200), 1000,
                           [self.meeting([0.0] * 800, [0.0] * 800)])
        self.assertFalse(decisions.alignment_failed)
        self.assertEqual(len(decisions.kept), 1)

    def test_an_only_weakly_aligned_segment_is_kept_without_failing_alignment(self):
        from second_brain.adaptive.dedupe import decide
        # Correlates about 0.47: real alignment, but not the same passage.
        offbeat = [0.0] * 200 + [1.0, 0.0, 9.0, 0.0, 3.0, 0.0] * 4 + [0.0] * 500
        decisions = decide([self.segment(10.0, "unclear")], self.at(200), 1000,
                           [self.meeting(offbeat, [0.0] * 800)])
        peak = decisions.kept[0]["dedupe_score"]
        self.assertTrue(0.3 <= peak < 0.6, peak)
        self.assertEqual(decisions.dropped, [])
        self.assertFalse(decisions.alignment_failed)


if __name__ == "__main__":
    unittest.main()
