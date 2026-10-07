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
    The meeting started at the same epoch. Meeting audio is exactly as long as the meeting
    (no padding): alignment must work from inside the overlap, in both offset directions. The
    remote channel is busy with other speech throughout, which is what makes chance matches likely."""

    @classmethod
    def setUpClass(cls):
        cls.call, cls.room, cls.remote = pseudo_speech(120, 1), pseudo_speech(120, 2), pseudo_speech(200, 3)
        cls.omi = cls.call[:1200] + cls.room[1200:2400]
        cls.meeting = cls.make_meeting(74)               # the Mac clock is 3.7 s late
        r = random.Random(7)
        cls.segments, t = [], 0.0
        while t < 118:
            length = r.uniform(2.0, 6.0)
            cls.segments.append({"start": round(t, 2), "end": round(t + length, 2), "text": f"s{t:.0f}"})
            t += length + r.uniform(0.2, 1.0)

    @classmethod
    def make_meeting(cls, lag_frames, cid="ab" * 16):
        """Meeting frames exactly 120 s long. Positive lag: the Mac holds each sound later than the
        Omi's clock says; negative: earlier."""
        mic = second_mic(cls.call[:1200], 4) + second_mic([0.0] * 1300, 5)
        left = [0.0] * lag_frames + mic if lag_frames >= 0 else mic[-lag_frames:]
        return {"capture_id": cid, "start_ms": 1_000_000, "end_ms": 1_120_000,
                "left": (left + [0.0] * 2400)[:2400], "right": cls.remote[:2400]}

    def call_and_room(self, decisions, segments=None):
        segments = segments or self.segments
        dropped = {d["text"] for d in decisions.dropped}
        call = {s["text"] for s in segments if s["end"] <= 60}
        room = {s["text"] for s in segments if s["start"] >= 60}
        return dropped & call, call, dropped & room

    def test_constants_are_the_measured_ones(self):
        from second_brain.adaptive import dedupe
        self.assertEqual((dedupe.DROP_THRESHOLD, dedupe.ALIGNMENT_THRESHOLD, dedupe.SEARCH_SECONDS,
                          dedupe.SEGMENT_TOLERANCE, dedupe.MIN_SEGMENT_SECONDS, dedupe.BLOCK_SECONDS,
                          dedupe.MIN_BLOCK_SECONDS),
                         (0.6, 0.7, 10.0, 0.1, 2.0, 60.0, 30.0))

    def test_a_late_mac_clock_is_found_without_padding(self):
        from second_brain.adaptive.dedupe import decide
        decisions = decide(self.segments, self.omi, 1000, [self.meeting])
        (alignment,) = decisions.alignments
        self.assertEqual(alignment["status"], "aligned")
        self.assertEqual({(b["channel"], b["offset_seconds"]) for b in alignment["blocks"]}, {("L", 3.7)})
        self.assertGreaterEqual(min(b["score"] for b in alignment["blocks"]), 0.7)
        self.assertFalse(decisions.alignment_failed)

    def test_an_early_mac_clock_is_found_too(self):
        """The reviewed defect: with the Omi already recording when the meeting began, negative
        shifts had no room inside the overlap and alignment silently failed."""
        from second_brain.adaptive.dedupe import decide
        decisions = decide(self.segments, self.omi, 1000, [self.make_meeting(-74)])
        (alignment,) = decisions.alignments
        self.assertEqual(alignment["status"], "aligned")
        self.assertEqual({b["offset_seconds"] for b in alignment["blocks"]}, {-3.7})
        got, call, wrong = self.call_and_room(decisions)
        self.assertEqual(wrong, set())
        self.assertGreaterEqual(len(got) / len(call), 0.8)

    def test_the_call_is_dropped_and_the_room_is_kept(self):
        from second_brain.adaptive.dedupe import decide
        decisions = decide(self.segments, self.omi, 1000, [self.meeting])
        got, call, wrong = self.call_and_room(decisions)
        # Segments in the first and last 10 s of the overlap cannot be aligned, so a few edge
        # segments may survive as duplicates; that is the conservative direction.
        self.assertGreaterEqual(len(got) / len(call), 0.8)
        self.assertEqual(wrong, set())
        record = decisions.dropped[0]
        self.assertEqual((record["deduped_by"], record["deduped_channel"]), ("ab" * 16, "L"))
        self.assertNotIn("channel", record)          # `channel` means mic/remote on meeting segments
        self.assertGreaterEqual(record["score"], 0.6)
        self.assertAlmostEqual(record["offset_seconds"], 3.7, delta=0.1)

    def test_short_room_speech_against_a_busy_meeting_is_almost_never_dropped(self):
        """The regression the first revision exists for: 400 random 2-3 s room segments while the
        remote channel is full of other speech, all scored against one alignment."""
        from second_brain.adaptive.dedupe import decide
        r = random.Random(11)
        room = []
        for index in range(400):
            start = r.uniform(60, 105)                  # inside the aligned part of the overlap
            room.append({"start": start, "end": start + r.uniform(2.0, 3.0), "text": f"room{index}"})
        decisions = decide(room, self.omi, 1000, [self.meeting])   # segments are scored independently
        self.assertEqual(decisions.alignments[0]["status"], "aligned")
        self.assertLessEqual(len(decisions.dropped) / 400, 0.02)

    def test_clock_drift_is_tracked_block_by_block(self):
        """Five minutes on a call with the Mac clock drifting 1000 ppm (0.3 s by the end): one
        offset for the whole meeting would miss the late segments; per-block offsets follow it."""
        from second_brain.adaptive.dedupe import decide
        call = pseudo_speech(300, 21)
        heard = second_mic(call, 22)
        left = [heard[min(len(call) - 1, round(k * (1 - 1000e-6)))] for k in range(6000)]
        meeting = {"capture_id": "ef" * 16, "start_ms": 1_000_000, "end_ms": 1_300_000,
                   "left": left, "right": pseudo_speech(300, 23)}
        late = [{"start": float(t), "end": float(t) + 4.0, "text": f"late{t}"} for t in range(200, 284, 6)]
        decisions = decide(late, call, 1000, [meeting])
        offsets = [b["offset_seconds"] for b in decisions.alignments[0]["blocks"]]
        self.assertGreater(max(offsets) - min(offsets), 0.15)     # drift really is present
        self.assertGreaterEqual(len(decisions.dropped) / len(late), 0.8)

    def test_an_owner_silent_at_first_still_aligns_later(self):
        from second_brain.adaptive.dedupe import decide
        # Five minutes: room speech only for the first 3 min, then the owner on the call.
        call, room = pseudo_speech(300, 31), pseudo_speech(300, 32)
        omi = room[:3600] + call[3600:6000]
        left = second_mic([0.0] * 3600 + call[3600:6000], 33)
        meeting = {"capture_id": "aa" * 16, "start_ms": 1_000_000, "end_ms": 1_300_000,
                   "left": left, "right": pseudo_speech(300, 34)}
        late = [{"start": float(t), "end": float(t) + 4.0, "text": f"t{t}"} for t in range(190, 280, 6)]
        decisions = decide(late, omi, 1000, [meeting])
        self.assertEqual(decisions.alignments[0]["status"], "aligned")
        self.assertTrue(all(b["start"] >= 170 for b in decisions.alignments[0]["blocks"]))
        self.assertGreaterEqual(len(decisions.dropped) / len(late), 0.8)

    def test_segments_shorter_than_two_seconds_are_never_dropped(self):
        from second_brain.adaptive.dedupe import decide
        short = [{"start": 20.0, "end": 21.9, "text": "duplicate but short"}]
        decisions = decide(short, self.omi, 1000, [self.meeting])
        self.assertEqual(decisions.dropped, [])
        self.assertNotIn("dedupe_score", decisions.kept[0])

    def test_an_unrelated_meeting_fails_alignment_and_drops_nothing(self):
        from second_brain.adaptive.dedupe import decide
        other = {"capture_id": "cd" * 16, "start_ms": 1_000_000, "end_ms": 1_120_000,
                 "left": pseudo_speech(120, 9), "right": pseudo_speech(120, 10)}
        decisions = decide(self.segments, self.omi, 1000, [other])
        self.assertEqual(decisions.alignments[0]["status"], "failed")
        self.assertLess(decisions.alignments[0]["best_score"], 0.7)
        self.assertTrue(decisions.alignment_failed)
        self.assertEqual(decisions.dropped, [])
        self.assertEqual(len(decisions.kept), len(self.segments))

    def test_too_little_overlap_is_not_an_alignment_failure(self):
        from second_brain.adaptive.dedupe import decide
        # The meeting starts 75 s into the Omi recording: 45 s of overlap, 25 s after the margins.
        late = dict(self.meeting, start_ms=1_075_000, end_ms=1_195_000)
        decisions = decide(self.segments, self.omi, 1000, [late])
        self.assertEqual(decisions.alignments[0]["status"], "too little overlap")
        self.assertFalse(decisions.alignment_failed)
        self.assertEqual(decisions.dropped, [])

    def test_a_meeting_on_the_remote_channel_aligns_too(self):
        from second_brain.adaptive.dedupe import decide
        swapped = dict(self.meeting, left=self.meeting["right"], right=self.meeting["left"])
        decisions = decide(self.segments, self.omi, 1000, [swapped])
        self.assertEqual({b["channel"] for b in decisions.alignments[0]["blocks"]}, {"R"})
        self.assertTrue(decisions.dropped)
        self.assertTrue(all(d["deduped_channel"] == "R" for d in decisions.dropped))

    def test_no_meetings_keeps_everything_without_failing(self):
        from second_brain.adaptive.dedupe import decide
        decisions = decide(self.segments, self.omi, 1000, [])
        self.assertEqual((decisions.dropped, decisions.alignments, decisions.alignment_failed), ([], [], False))
        self.assertEqual(len(decisions.kept), len(self.segments))


class OverlapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.meetings = Path(self.tmp.name) / "meetings"
        (self.meetings / ".captures").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def marker(self, cid, state, start_ms, end_ms=None, updated=None):
        value = {"capture_id": cid, "client": "mac", "state": state, "start_ms": start_ms,
                 "app": "us.zoom.xos", "updated": updated if updated is not None else 1_791_300_000.0}
        if end_ms is not None:
            value["end_ms"] = end_ms
        (self.meetings / ".captures" / f"{cid}.json").write_text(json.dumps(value))
        if state == "closed":
            (self.meetings / f"{cid}.caf").write_bytes(b"audio")
        return value

    # An Omi recording covering epoch 1000..1600.
    def omi(self, first=1000, seconds=600):
        return {"first_timestamp": first, "audio_seconds": seconds}

    def test_a_closed_overlapping_capture_is_offered_with_its_audio(self):
        from second_brain.meetings import overlapping
        self.marker("ab" * 16, "closed", 1_200_000, 1_500_000)
        found = overlapping(self.meetings, self.omi())
        self.assertEqual([m["capture_id"] for m in found.closed], ["ab" * 16])
        self.assertEqual(Path(found.closed[0]["audio"]), self.meetings / f"{'ab' * 16}.caf")
        self.assertEqual(found.open, [])

    def test_an_open_overlapping_capture_asks_the_caller_to_wait(self):
        from second_brain.meetings import overlapping
        self.marker("cd" * 16, "open", 1_200_000)
        # `now` is pinned: the marker's `updated` is fixed, and a real clock would expire it a day later.
        found = overlapping(self.meetings, self.omi(), now=1_791_300_100.0)
        self.assertEqual(found.open, ["cd" * 16])   # open holds ids, like expired
        self.assertEqual(found.closed, [])

    def test_an_open_capture_older_than_a_day_no_longer_blocks(self):
        from second_brain.meetings import overlapping
        self.marker("cd" * 16, "open", 1_200_000, updated=1_791_300_000.0)
        found = overlapping(self.meetings, self.omi(), now=1_791_300_000.0 + 24 * 3600 + 1)
        self.assertEqual(found.open, [])
        self.assertEqual(found.expired, ["cd" * 16])

    def test_cancelled_and_non_overlapping_captures_are_ignored(self):
        from second_brain.meetings import overlapping
        self.marker("11" * 16, "cancelled", 1_200_000)
        self.marker("22" * 16, "closed", 9_000_000, 9_100_000)      # long after the recording
        self.marker("33" * 16, "closed", 10_000, 90_000)            # long before it
        found = overlapping(self.meetings, self.omi())
        self.assertEqual((found.closed, found.open, found.expired), ([], [], []))

    def test_a_capture_touching_the_edge_within_the_search_window_counts(self):
        from second_brain.meetings import overlapping
        self.marker("44" * 16, "closed", 1_605_000, 1_800_000)      # starts 5 s after the Omi ends
        self.assertEqual(len(overlapping(self.meetings, self.omi()).closed), 1)

    def test_a_recording_without_a_device_clock_is_never_deduped(self):
        from second_brain.meetings import overlapping
        self.marker("ab" * 16, "closed", 1_200_000, 1_500_000)
        found = overlapping(self.meetings, {"first_timestamp": 0, "audio_seconds": 600})
        self.assertEqual((found.closed, found.open), ([], []))

    def test_a_meeting_job_is_never_deduped_against_meetings(self):
        from second_brain.meetings import overlapping
        self.marker("ab" * 16, "closed", 1_200_000, 1_500_000)
        found = overlapping(self.meetings, {"source": "meeting", "capture_id": "ab" * 16,
                                            "start_ms": 1_200_000, "end_ms": 1_500_000})
        self.assertEqual((found.closed, found.open), ([], []))

    def test_an_unreadable_marker_is_skipped_without_losing_the_others(self):
        from second_brain.meetings import overlapping
        (self.meetings / ".captures" / "bad.json").write_text("{not json")
        self.marker("ab" * 16, "closed", 1_200_000, 1_500_000)
        self.assertEqual(len(overlapping(self.meetings, self.omi()).closed), 1)

    def test_a_closed_capture_whose_audio_vanished_is_skipped(self):
        from second_brain.meetings import overlapping
        self.marker("ab" * 16, "closed", 1_200_000, 1_500_000)
        (self.meetings / f"{'ab' * 16}.caf").unlink()
        self.assertEqual(overlapping(self.meetings, self.omi()).closed, [])

    def test_an_open_capture_from_days_earlier_does_not_hold_a_later_recording(self):
        from second_brain.meetings import overlapping
        now = 1000 + 2 * 86400 + 700
        self.marker("cd" * 16, "open", 1_000_000, updated=now)
        found = overlapping(self.meetings, self.omi(first=1000 + 2 * 86400), now=now)
        self.assertEqual(found.open, [])
        self.assertEqual(found.expired, [])

    def test_an_open_capture_still_holds_a_recording_within_its_first_hours(self):
        from second_brain.meetings import overlapping
        now = 1000 + 3 * 3600 + 700
        self.marker("cd" * 16, "open", 1_000_000, updated=now)
        found = overlapping(self.meetings, self.omi(first=1000 + 3 * 3600), now=now)
        self.assertEqual(found.open, ["cd" * 16])

    def test_a_future_dated_open_marker_still_expires(self):
        from second_brain.meetings import overlapping
        now = 1000 + 2 * 86400
        self.marker("cd" * 16, "open", 1_000_000, updated=now + 10 * 86400)
        found = overlapping(self.meetings, self.omi(), now=now)
        self.assertEqual(found.expired, ["cd" * 16])
        self.assertEqual(found.open, [])


# Speech-like test audio: a carrier inside a slowly varying envelope, so 50 ms envelopes have the
# timing structure the alignment needs. Different modulation rates make them mutually unrelated.
CALL = "0.4*sin(2*PI*1000*t)*abs(sin(2*PI*1.3*t)*sin(2*PI*0.37*t+1))"
ROOM = "0.4*sin(2*PI*1500*t)*abs(sin(2*PI*1.7*t+2)*sin(2*PI*0.53*t))"
OTHER = "0.4*sin(2*PI*800*t)*abs(sin(2*PI*1.1*t+0.5)*sin(2*PI*0.29*t+2))"
OTHER2 = "0.4*sin(2*PI*600*t)*abs(sin(2*PI*0.9*t)*sin(2*PI*0.41*t+1))"


def pipeline_config(root: Path, moss="fake_moss.py") -> Path:
    cfg = json.loads((ROOT / "config/pipeline.example.json").read_text())
    cfg.update(moss_command=[sys.executable, str(ROOT / "tests/fixtures" / moss)],
               scan_vault_for_novelty=False, long_silence_seconds=999, memory_gate_min_words=1,
               work_root=str(root / "work"))
    path = root / "pipeline.json"
    path.write_text(json.dumps(cfg))
    return path


def run_pipeline(root: Path, audio: Path, out: Path, *extra) -> dict:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
    proc = subprocess.run(
        [sys.executable, "-m", "second_brain.adaptive.pipeline", "--audio", str(audio),
         "--config", str(pipeline_config(root, "fake_moss_halves.py")), "--output-dir", str(out),
         "--no-hermes", "--skip-vibe7", *extra],
        capture_output=True, text=True, env=env,
    )
    if proc.returncode:
        raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
    return json.loads((out / "manifest.json").read_text())


class PipelineDedupeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        # Two minutes: the owner on the call for 60 s, then someone in the room for 60 s. Alignment
        # needs >= 30 s of overlap and envelope structure, so constant tones cannot be used.
        cls.omi = tone_wav(cls.root / "omi.wav", f"if(lt(t,60),{CALL},{ROOM})", 120)
        # The meeting's mic holds the call; its remote channel carries unrelated speech throughout.
        cls.capture = tone_wav(cls.root / "capture.wav", f"if(lt(t,60),{CALL},0)|{OTHER}", 120, channels=2)
        index = [{"capture_id": "ab" * 16, "start_ms": 1_000_000, "end_ms": 1_120_000,
                  "audio": str(cls.capture)}]
        (cls.root / "index.json").write_text(json.dumps({"recording_epoch": 1000, "captures": index}))
        cls.manifest = run_pipeline(cls.root, cls.omi, cls.root / "run",
                                    "--meeting-dedupe", str(cls.root / "index.json"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_duplicated_half_is_dropped_and_the_room_half_survives(self):
        kept = [s["text"] for w in self.manifest["windows"] for s in w["moss_segments"]]
        self.assertEqual(kept, ["second half"])
        self.assertEqual([d["text"] for d in self.manifest["dedupe"]["dropped"]], ["first half"])

    def test_the_dropped_record_names_the_capture_and_channel(self):
        dropped = self.manifest["dedupe"]["dropped"][0]
        self.assertEqual(dropped["deduped_by"], "ab" * 16)
        self.assertEqual(dropped["deduped_channel"], "L")
        self.assertGreaterEqual(dropped["score"], 0.6)
        self.assertEqual(self.manifest["dedupe"]["dropped_count"], 1)
        self.assertFalse(self.manifest["dedupe"]["alignment_failed"])

    def test_windows_are_built_only_from_the_surviving_segments(self):
        self.assertTrue(self.manifest["windows"])
        for window in self.manifest["windows"]:
            self.assertNotIn("first half", window["moss_transcript"])
            self.assertGreaterEqual(window["start"], 59.9)

    def test_without_the_flag_nothing_is_deduped(self):
        manifest = run_pipeline(self.root, self.omi, self.root / "plain")
        kept = [s["text"] for w in manifest["windows"] for s in w["moss_segments"]]
        self.assertEqual(kept, ["first half", "second half"])
        self.assertNotIn("dedupe", manifest)

    def test_an_unrelated_capture_drops_nothing_and_reports_failed_alignment(self):
        index = {"recording_epoch": 1000, "captures": [
            {"capture_id": "cd" * 16, "start_ms": 1_000_000, "end_ms": 1_120_000,
             "audio": str(tone_wav(self.root / "other.wav", f"{OTHER}|{OTHER2}", 120, channels=2))}]}
        (self.root / "other-index.json").write_text(json.dumps(index))
        manifest = run_pipeline(self.root, self.omi, self.root / "unrelated",
                                "--meeting-dedupe", str(self.root / "other-index.json"))
        kept = [s["text"] for w in manifest["windows"] for s in w["moss_segments"]]
        self.assertEqual(kept, ["first half", "second half"])
        self.assertTrue(manifest["dedupe"]["alignment_failed"])
        self.assertEqual(manifest["dedupe"]["dropped_count"], 0)


if __name__ == "__main__":
    unittest.main()
