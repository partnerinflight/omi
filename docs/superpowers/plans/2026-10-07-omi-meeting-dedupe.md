# Omi Meeting Dedupe (Pipeline Plan 2b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an Omi recording overlaps a Mac meeting capture, drop the Omi transcript segments that are the same audio the meeting already captured, and keep the rest (someone talking to the owner in the room).

**Architecture:** The runtime owns meeting knowledge: before processing an Omi job it reads the capture markers, defers the job while an overlapping capture is still `open`, and otherwise writes a small JSON index of overlapping closed captures that it passes to the pipeline subprocess with `--meeting-dedupe`. The pipeline, after ASR and before conversation windows, slides each Omi segment's whole 50 ms speech-band RMS envelope through ±10 s of the meeting's L and R envelopes around the same wall-clock instant and drops segments whose best Pearson correlation reaches 0.6.

**The matching rule is deliberate and was prototype-verified before this plan was written. Keep both guards:**
- **Only full-length windows count.** The segment envelope is compared against an equally long slice of the meeting envelope at every shift. Allowing partial overlap lets a 2-frame tail correlate at exactly 1.0, which falsely drops real room speech — measured, not hypothetical.
- **Segments shorter than `MIN_MATCH_FRAMES` (16 frames, 0.8 s) are never deduped.** Too few frames to identify reliably.

Prototype numbers the tests below encode: identical envelopes 1.0; a ±3.7 s clock offset still 1.0 with the offset reported; a 20 s offset no match at all; unrelated room speech 0.37. Dropped segments stay in the manifest; no audio is deleted.

**Tech Stack:** Python 3.12 stdlib only (no numpy — the Windows service installs from wheels and must stay dependency-light), ffmpeg/ffprobe, SQLite, unittest.

Spec: `docs/superpowers/specs/2026-09-29-meeting-capture-design.md` §3 "Omi jobs overlapping a meeting". Plan 2a (meeting notes) is implemented and merged on this branch.

**Owner decisions recorded 2026-10-07:** audio correlation as specified (not simple time-overlap); deferral only while a capture is `open` (a meeting uploaded after the Omi job has already run leaves two notes, accepted).

**Deliberate deviations from the spec, to document in Task 8:**
- An `open` capture older than 24 h is *treated* as expired; its marker file is not rewritten. The receiver owns `.captures/` state, and a second writer would risk clobbering a concurrent `CAPTURE_CANCEL`/close.
- Segment timing comes from the Omi sidecar's integer `first_timestamp` (epoch seconds), not the formatted `first_utc` string. A recording with `first_timestamp == 0` (device clock never set) is never deduped.
- Tray meeting counts (spec §4) stay out of scope; this plan only adds the per-job counts to the job result that the tray will later read.

---

## File map

| File | Responsibility |
|---|---|
| `src/second_brain/adaptive/channels.py` (modify) | `mono_frames()`, `rms_envelope()`, `match_envelope()` |
| `src/second_brain/adaptive/dedupe.py` (create) | Per-segment decisions against a meeting index: thresholds, alignment-failure rule |
| `src/second_brain/adaptive/pipeline.py` (modify) | `--meeting-dedupe`; apply decisions after ASR, before windows; manifest records |
| `src/second_brain/meetings.py` (create) | Runtime-side: read capture markers, find overlaps, classify open/closed/expired |
| `src/second_brain/queue.py` (modify) | `defer(job_id, seconds, stage)` that does not consume an attempt |
| `src/second_brain/runtime.py` (modify) | Defer on open captures; write the dedupe index; pass the flag; report counts |
| `tests/test_dedupe.py` (create) | Channel math, dedupe decisions, overlap/defer logic, pipeline and end-to-end |
| `README.md`, `SYSTEM.md`, `src/second_brain/ARCHITECTURE.md`, spec (modify) | Docs move with code |

Run from the repo root. Single module: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe -v`. Full suite: `python3 scripts/test.py` — its trailing `swift test` step fails in a sandboxed shell (`sandbox-exec: Operation not permitted`); that is pre-existing. Both Python suites must print `OK`. End-to-end tests here are timing-sensitive: run the full Python suite at least twice.

**Facts you need (already verified, do not re-derive):**
- Omi job metadata (from the receiver sidecar) has `first_timestamp` (int epoch seconds), `last_timestamp`, `first_utc` (`"%Y-%m-%d %H:%M:%SZ"`, note the space), `audio_seconds`, `device`, `start_seq`. Meeting job metadata has `source: "meeting"`, `capture_id`, `app`, `start_ms`, `end_ms` (both int ms), and `first_utc` added at discovery.
- Capture markers live at `<incoming>/meetings/.captures/<capture_id>.json` with `{"capture_id", "client", "state": "open"|"cancelled"|"closed", "start_ms", "app", "updated"}` and, once closed, `"end_ms"`. Read them with `omi_local.file_store.FileStore(root).capture_state(cid)` or plain JSON.
- Meeting audio is `<incoming>/meetings/<capture_id>.caf`, stereo (L = owner mic, R = meeting app), and is never deleted by `purge_audio`.
- `channels.stereo_frames(audio, timeout)` returns `([L...], [R...])` of **mean-square** 300–3400 Hz values per 50 ms frame (`channels.FRAME_SECONDS = 0.05`). RMS is its square root.
- `queue.claim()` already does `attempts=attempts+1`, so a job deferred after being claimed must give that attempt back or deferral will exhaust `max_attempts`.

---

### Task 1: Mono frames and shifted envelope matching

**Files:**
- Modify: `src/second_brain/adaptive/channels.py`
- Test: `tests/test_dedupe.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dedupe.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe -v`
Expected: FAIL with `ImportError: cannot import name 'mono_frames' from 'second_brain.adaptive.channels'`.

- [ ] **Step 3: Implement**

In `src/second_brain/adaptive/channels.py`, refactor the decode so mono and stereo share it, and add the envelope/correlation helpers. Replace the existing `stereo_frames` with:

```python
def _decode_frames(audio, channel_count, timeout):
    """Mean-square 300-3400 Hz level of each 50 ms frame, one list per channel. Streams the decode."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio), "-af", "highpass=f=300,lowpass=f=3400",
           "-ac", str(channel_count), "-ar", str(RATE), "-f", "s16le", "-"]
    errors = tempfile.TemporaryFile()  # a file, not a pipe: ffmpeg can never block on a full stderr
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors)
    out = [[] for _ in range(channel_count)]
    width = 2 * channel_count
    try:
        while chunk := proc.stdout.read(FRAME * width):
            samples = array("h")
            samples.frombytes(chunk[: len(chunk) - len(chunk) % width])
            if not samples:
                break
            for index, values in enumerate(out):
                one = samples[index::channel_count]
                values.append(sum(map(operator.mul, one, one)) / len(one))
        if proc.wait(timeout=timeout) != 0:
            errors.seek(0)
            raise RuntimeError("ffmpeg could not decode the capture: " + errors.read().decode(errors="replace")[-2000:])
    finally:
        proc.stdout.close()
        errors.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return out


def stereo_frames(audio, timeout=14400):
    """([L...], [R...]) for a stereo meeting capture."""
    left, right = _decode_frames(audio, 2, timeout)
    return left, right


def mono_frames(audio, timeout=14400):
    """[...] for a mono Omi recording (any multi-channel input is mixed down)."""
    return _decode_frames(audio, 1, timeout)[0]


def rms_envelope(frames, start, end):
    """RMS values of the 50 ms frames covering [start, end) seconds."""
    first = max(0, int(start / FRAME_SECONDS))
    last = max(first + 1, math.ceil(end / FRAME_SECONDS))
    return [math.sqrt(value) for value in frames[first:last]]


MIN_MATCH_FRAMES = 16   # 0.8 s: fewer frames cannot identify a passage reliably


def _pearson(x, y):
    """None when either side is flat: a constant envelope carries no timing information."""
    n = len(x)
    mean_x, mean_y = sum(x) / n, sum(y) / n
    dx = [v - mean_x for v in x]
    dy = [v - mean_y for v in y]
    norm = math.sqrt(sum(v * v for v in dx) * sum(v * v for v in dy))
    if norm <= 1e-12:
        return None
    return sum(map(operator.mul, dx, dy)) / norm


def match_envelope(envelope, frames, at, max_shift):
    """Best correlation of `envelope` against equally long slices of `frames` (mean squares),
    centred on `at` seconds and shifted by up to +-max_shift seconds. Returns (peak, offset
    seconds) or (None, 0.0) when no comparable window exists.

    Only full-length windows are compared. A partial overlap of two frames correlates at exactly
    1.0 regardless of content, which would drop real speech near a meeting's edge.
    """
    length = len(envelope)
    if length < MIN_MATCH_FRAMES:
        return None, 0.0
    base = int(round(at / FRAME_SECONDS))
    span = int(round(max_shift / FRAME_SECONDS))
    best, best_shift = None, 0
    for shift in range(-span, span + 1):
        start = base + shift
        if start < 0 or start + length > len(frames):
            continue
        score = _pearson(envelope, [math.sqrt(v) for v in frames[start : start + length]])
        if score is not None and (best is None or score > best):
            best, best_shift = score, shift
    if best is None:
        return None, 0.0
    return best, round(best_shift * FRAME_SECONDS, 3)
```

Add `import math` to the imports if it is not already there (it is used by `span_level_db`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe -v`
Expected: 8 tests PASS.
Run: `python3 scripts/test.py`
Expected: both Python suites `OK` — `tests/test_meetings.py` covers `stereo_frames` (pipe hygiene, decode errors, owner attribution) and must be unaffected by the refactor.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/adaptive/channels.py tests/test_dedupe.py
git commit -m "feat(dedupe): mono envelopes and shifted envelope matching"
```

---

### Task 2: Segment decisions (revised 2026-10-07)

**Why this task was revised.** The first implementation (commit 2e12c3a8d7) followed the spec's
rule: search +-10 s for every segment. Review and then measurement on the owner's real Omi
recordings showed it falsely drops unrelated speech by chance: 93% of 0.8 s segments, 47% at 2 s,
9% at 8 s. The replacement estimates one offset per meeting and scores segments only near it.
Measured on real speech: false drops 0.7% at 2 s; a simulated second microphone with noise and a
3.7 s clock offset was aligned exactly (correlation 0.99) and its segments detected 80-87%. A
missed duplicate leaves one repeated line; a false drop loses real speech, so the design leans
toward keeping. This task replaces `src/second_brain/adaptive/dedupe.py` and the `DecisionTests`
class wholesale.

**Second revision (same day):** whole-overlap alignment scored only 0.50 when half the overlap
was the owner talking to the room: that speech dilutes the correlation. Alignment now takes the
strongest 60 s block in the first 10 min of overlap. More blocks means more chances for unrelated
audio, so the threshold rose to 0.7: across 110 pairs of the owner's unrelated real recordings the
best block peaked at 0.52 (95th percentile 0.32), while true alignments score about 0.99. Even a
false alignment only risks about 1% false drops, because segments are still checked within
+-0.1 s of the offset.

**Third revision (same day), after review of commit 325cb15b30:** alignment searched +-10 s but
built its envelope from the *whole* overlap, so with real (unpadded) meeting audio no full-length
window existed for a negative offset whenever the Omi was already recording when the meeting began,
the normal case. Alignment silently failed and nothing was deduped. The tests passed only because
the fixture padded the meeting audio. Now: alignment blocks start and end 10 s inside the overlap,
every 60 s block is aligned on its own (each segment uses the nearest aligned block's offset,
which tracks clock drift and an owner who is silent at first), and the tests use exact-length
meeting audio with offsets in both directions, 1000 ppm drift and a silent-at-first owner.
Cost: about 2 s of alignment per minute of overlap.

**Interface change for later tasks:** dropped records use `deduped_channel` (not `channel`,
which already means mic/remote on meeting segments). `Decisions` gains `alignments`, one dict per
meeting: `capture_id`, `status` (`aligned` / `failed` / `too little overlap`), `best_score`, and
`blocks` (each aligned block's `start`/`end` in meeting seconds, `score`, `offset_seconds`, `channel`). `alignment_failed` is true when at least one meeting was
attempted and none aligned.

**Files:**
- Replace: `src/second_brain/adaptive/dedupe.py`
- Replace: class `DecisionTests` in `tests/test_dedupe.py`; add the two synthetic helpers

- [ ] **Step 1: Replace the tests**

In `tests/test_dedupe.py`, delete the whole `class DecisionTests` and put in its place:

```python
import random


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
```

Add `import random` to the module's top-level imports instead of above the helpers if you prefer;
do not import it twice.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe.DecisionTests -v`
Expected: FAIL (no `alignments` attribute, `deduped_channel` missing, and the 150-segment test
drops far more than 2%).

- [ ] **Step 3: Replace the implementation**

Replace the whole of `src/second_brain/adaptive/dedupe.py` with:

```python
"""Drop Omi transcript segments that duplicate a Mac meeting capture (spec section 3).

Two stages, because the Omi and the Mac clocks differ by one roughly constant offset:
1. `align`: estimate that offset once per meeting from the whole overlapping stretch (at least
   30 s). A long envelope gives an unambiguous peak; below ALIGNMENT_THRESHOLD the recordings do
   not line up and nothing is dropped against that meeting.
2. Each segment of at least 2 s is scored only within +-0.1 s of that offset.

Searching +-10 s per segment instead (the spec's original rule) was measured on real Omi speech
to drop 93% of unrelated 0.8 s segments and 9% of 8 s ones by chance: the best of ~800 shifted
comparisons routinely exceeds 0.6. That would delete exactly the in-room speech this keeps.
Nothing here deletes audio.
"""

from __future__ import annotations
from dataclasses import dataclass, field

from .channels import FRAME_SECONDS, match_envelope, rms_envelope

DROP_THRESHOLD = 0.6         # per-segment correlation that means "the meeting already has this"
ALIGNMENT_THRESHOLD = 0.7    # block peak needed to trust an offset (unrelated real pairs: max 0.52)
SEARCH_SECONDS = 10.0        # clock offset tolerated between the Omi and the Mac
SEGMENT_TOLERANCE = 0.1      # per-segment wiggle around its block's offset
MIN_SEGMENT_SECONDS = 2.0    # shorter segments are never dropped (chance matches; a duplicate is cheaper)
BLOCK_SECONDS = 60.0         # alignment block; each block gets its own offset, so drift is tracked
MIN_BLOCK_SECONDS = 30.0     # a final partial block shorter than this is not used


@dataclass
class Decisions:
    kept: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    alignment_failed: bool = False
    alignments: list = field(default_factory=list)   # one per meeting, with its aligned blocks

def align(omi_frames, omi_epoch, meeting):
    """Clock offsets for one meeting, one per 60 s block of overlap.

    Each block is searched +-SEARCH_SECONDS, so blocks start SEARCH_SECONDS inside the overlap:
    a full-length comparison window must exist for every shift, including a negative offset when
    the Omi was already recording as the meeting began. Blocks below ALIGNMENT_THRESHOLD are
    unusable (the owner silent, or only room speech); a meeting with no usable block fails.
    """
    m_start, m_end = meeting["start_ms"] / 1000, meeting["end_ms"] / 1000
    o_end = omi_epoch + len(omi_frames) * FRAME_SECONDS
    lo = max(omi_epoch, m_start) + SEARCH_SECONDS
    hi = min(o_end, m_end) - SEARCH_SECONDS
    result = {"capture_id": meeting["capture_id"], "blocks": []}
    if hi - lo < MIN_BLOCK_SECONDS:
        return {**result, "status": "too little overlap"}
    block = lo
    while hi - block >= MIN_BLOCK_SECONDS:
        end = min(block + BLOCK_SECONDS, hi)
        if hi - end < MIN_BLOCK_SECONDS:
            end = hi                          # fold a short remainder into the last block
        envelope = rms_envelope(omi_frames, block - omi_epoch, end - omi_epoch)
        best = (None, 0.0, None)
        for name, frames in (("L", meeting["left"]), ("R", meeting["right"])):
            peak, offset = match_envelope(envelope, frames, block - m_start, SEARCH_SECONDS)
            if peak is not None and (best[0] is None or peak > best[0]):
                best = (peak, offset, name)
        if best[0] is not None and best[0] >= ALIGNMENT_THRESHOLD:
            result["blocks"].append({"start": round(block - m_start, 3), "end": round(end - m_start, 3),
                                     "score": round(best[0], 3), "offset_seconds": best[1],
                                     "channel": best[2]})
        result.setdefault("best_score", None)
        if best[0] is not None and (result["best_score"] is None or best[0] > result["best_score"]):
            result["best_score"] = round(best[0], 3)
        block = end
    result["status"] = "aligned" if result["blocks"] else "failed"
    return result


def _offset_at(alignment, meeting_seconds):
    """The offset of the aligned block nearest to a point in the meeting's timeline."""
    return min(alignment["blocks"],
               key=lambda b: abs((b["start"] + b["end"]) / 2 - meeting_seconds))["offset_seconds"]


def decide(segments, omi_frames, omi_epoch, meetings):
    result = Decisions()
    aligned = []
    for meeting in meetings:
        a = align(omi_frames, omi_epoch, meeting)
        result.alignments.append(a)
        if a["status"] == "aligned":
            aligned.append((meeting, a))
    attempted = [a for a in result.alignments if a["status"] != "too little overlap"]
    result.alignment_failed = bool(attempted) and not aligned
    for segment in segments:
        start, end = float(segment["start"]), float(segment["end"])
        best = (None, None, None, None)
        if end - start >= MIN_SEGMENT_SECONDS:
            envelope = rms_envelope(omi_frames, start, end)
            for meeting, alignment in aligned:
                expected = omi_epoch + start - meeting["start_ms"] / 1000
                offset = _offset_at(alignment, expected)
                into = expected + offset
                for name, frames in (("L", meeting["left"]), ("R", meeting["right"])):
                    peak, shift = match_envelope(envelope, frames, into, SEGMENT_TOLERANCE)
                    if peak is not None and (best[0] is None or peak > best[0]):
                        best = (peak, round(offset + shift, 3), name, meeting["capture_id"])
        peak, offset, channel, capture = best
        if peak is not None and peak >= DROP_THRESHOLD:
            result.dropped.append({**segment, "deduped_by": capture, "deduped_channel": channel,
                                   "score": round(peak, 3), "offset_seconds": offset})
        elif peak is not None:
            result.kept.append({**segment, "dedupe_score": round(peak, 3)})
        else:
            result.kept.append(dict(segment))
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe -v`
Expected: all PASS. These exact numbers were produced by this exact code before the plan was
revised. If one differs, report the value and investigate; never loosen a threshold or assertion.
Run `python3 scripts/test.py`: both Python suites `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/adaptive/dedupe.py tests/test_dedupe.py
git commit -m "fix(dedupe): align per 60 s block inside the overlap so any offset and drift are found"
```

---

### Task 3: Runtime-side meeting index

**Files:**
- Create: `src/second_brain/meetings.py`
- Test: `tests/test_dedupe.py`

The runtime decides what the pipeline may look at; the pipeline never reads the receiver's layout.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dedupe.py`:

```python
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
        found = overlapping(self.meetings, self.omi())
        self.assertEqual([m["capture_id"] for m in found.open], ["cd" * 16])
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe.OverlapTests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'second_brain.meetings'`.

- [ ] **Step 3: Implement**

Create `src/second_brain/meetings.py`:

```python
"""Which Mac meeting captures overlap an Omi recording.

The runtime owns this: it reads the receiver's capture markers and hands the pipeline only a
list of files to compare against, so the pipeline keeps no knowledge of the receiver layout.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import time

log = logging.getLogger("second_brain.meetings")

SEARCH_SECONDS = 10.0        # same tolerance the correlation search uses
OPEN_EXPIRY_SECONDS = 24 * 3600


@dataclass
class Overlaps:
    closed: list = field(default_factory=list)   # dicts: capture_id, start_ms, end_ms, audio
    open: list = field(default_factory=list)     # capture ids still being recorded or uploaded
    expired: list = field(default_factory=list)  # open too long to keep waiting for


def recording_span(metadata):
    """(start, end) epoch seconds of an Omi recording, or None when it cannot be placed in time."""
    if metadata.get("source") == "meeting":
        return None                              # a meeting is never deduped against meetings
    start = metadata.get("first_timestamp")
    if not isinstance(start, int) or start <= 0:
        return None                              # the device clock was never set
    seconds = metadata.get("audio_seconds")
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    return float(start), float(start) + float(seconds)


def overlapping(meetings_dir: Path, metadata: dict, now=None) -> Overlaps:
    span = recording_span(metadata)
    result = Overlaps()
    if span is None:
        return result
    start, end = span
    now = time.time() if now is None else now
    for marker in sorted((Path(meetings_dir) / ".captures").glob("*.json")):
        try:
            value = json.loads(marker.read_text(encoding="utf-8-sig"))
            state = value["state"]
            if state == "cancelled":
                continue
            capture_start = value["start_ms"] / 1000
            # An open capture has no end yet; treat it as still running.
            capture_end = value["end_ms"] / 1000 if state == "closed" else max(capture_start, now)
            if capture_end + SEARCH_SECONDS < start or capture_start - SEARCH_SECONDS > end:
                continue
            if state == "open":
                if now - float(value.get("updated", 0)) > OPEN_EXPIRY_SECONDS:
                    result.expired.append(value["capture_id"])
                else:
                    result.open.append(value["capture_id"])
                continue
            audio = Path(meetings_dir) / f"{value['capture_id']}.caf"
            if not audio.is_file():
                continue                          # committed audio deleted or moved: nothing to compare
            result.closed.append({"capture_id": value["capture_id"], "start_ms": value["start_ms"],
                                  "end_ms": value["end_ms"], "audio": str(audio)})
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
            log.exception("Ignoring unreadable capture marker %s", marker.name)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/meetings.py tests/test_dedupe.py
git commit -m "feat(dedupe): find the meeting captures that overlap an Omi recording"
```

---

### Task 4: Pipeline `--meeting-dedupe`

**Files:**
- Modify: `src/second_brain/adaptive/pipeline.py`
- Test: `tests/test_dedupe.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dedupe.py`:

```python
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
```

Create `tests/fixtures/fake_moss_halves.py`:

```python
"""Deterministic ASR contract fixture: one segment per half of the chunk, named by position.
Never used in production configuration."""

import json
import sys
import wave

if sys.argv[1] != "transcribe":
    raise SystemExit(2)
with wave.open(sys.argv[3]) as audio:
    duration = audio.getnframes() / audio.getframerate()
print(json.dumps({"segments": [
    {"start": 0, "end": duration / 2, "speaker": "S1", "text": "first half"},
    {"start": duration / 2, "end": duration, "speaker": "S2", "text": "second half"},
]}))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe.PipelineDedupeTests -v`
Expected: ERROR in `setUpClass`: `error: unrecognized arguments: --meeting-dedupe`.

- [ ] **Step 3: Implement**

In `src/second_brain/adaptive/pipeline.py`:

1. Above `def main():`, add:

```python
def apply_meeting_dedupe(index_path, audio, segments, manifest):
    """Drop segments a Mac meeting capture already holds (spec section 3). The index is written by
    the service, which owns capture state; this only reads audio it was pointed at."""
    from second_brain.adaptive.channels import mono_frames, stereo_frames
    from second_brain.adaptive.dedupe import decide

    index = load_json(index_path)
    captures = []
    for capture in index["captures"]:
        left, right = stereo_frames(capture["audio"], COMMAND_TIMEOUT)
        captures.append({**capture, "left": left, "right": right})
    print(f"=== Meeting dedupe: {len(captures)} overlapping capture(s) ===", flush=True)
    decisions = decide(segments, mono_frames(audio, COMMAND_TIMEOUT), index["recording_epoch"], captures)
    manifest["dedupe"] = {
        "captures": [c["capture_id"] for c in captures],
        "dropped_count": len(decisions.dropped),
        "dropped": decisions.dropped,
        "alignments": decisions.alignments,
        "alignment_failed": decisions.alignment_failed,
    }
    print(f"[dedupe] dropped {len(decisions.dropped)} of {len(segments)} segments"
          + (" (alignment failed: kept everything)" if decisions.alignment_failed else ""), flush=True)
    return decisions.kept
```

2. In `main`, add after the `--meeting` argument:

```python
    ap.add_argument("--meeting-dedupe", help="JSON index of overlapping meeting captures (service-written)")
```

3. In `main`, find the three statements that end Stage 1: `all_segments.sort(...)`, the
`moss_all_segments.json` write, and the `moss_transcript.txt` write. Immediately after the
`moss_transcript.txt` write and **before** the `# Stage 2: natural conversation windows.` comment,
insert:

```python
    if args.meeting_dedupe:
        all_segments = apply_meeting_dedupe(args.meeting_dedupe, audio, all_segments, manifest)
        (run_dir / "deduped_segments.json").write_text(
            json.dumps(all_segments, ensure_ascii=False, indent=2), encoding="utf-8"
        )
```

The full-transcript artefacts keep every segment on purpose: the dedupe is a windowing decision, and the archive stays complete.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe -v`
Expected: all PASS.
Run: `python3 scripts/test.py`
Expected: both Python suites `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/adaptive/pipeline.py tests/fixtures/fake_moss_halves.py tests/test_dedupe.py
git commit -m "feat(dedupe): pipeline drops Omi segments a meeting already captured"
```

---

### Task 5: Queue deferral

**Files:**
- Modify: `src/second_brain/queue.py`
- Test: `tests/test_dedupe.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dedupe.py`:

```python
class DeferTests(unittest.TestCase):
    def setUp(self):
        from second_brain.queue import Queue
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.queue = Queue(root / "q.sqlite3")
        audio = root / "a.opus"
        audio.write_bytes(b"audio")
        self.key = self.queue.enqueue(audio, {"device": "d", "start_seq": 1})

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_deferred_job_comes_back_without_spending_an_attempt(self):
        job = self.queue.claim()
        self.assertEqual(job["attempts"], 1)
        self.queue.defer(job, 0.0, "waiting-for-meeting")
        with self.queue.connect() as db:
            row = dict(db.execute("SELECT * FROM jobs WHERE id=?", (self.key,)).fetchone())
        self.assertEqual((row["state"], row["stage"], row["attempts"]), ("pending", "waiting-for-meeting", 0))
        self.assertIsNotNone(self.queue.claim())

    def test_a_deferred_job_is_not_claimable_until_its_delay_passes(self):
        self.queue.defer(self.queue.claim(), 30.0, "waiting-for-meeting")
        self.assertIsNone(self.queue.claim())

    def test_deferring_never_drives_attempts_below_zero(self):
        for _ in range(3):
            self.queue.defer(self.queue.claim(), 0.0, "waiting-for-meeting")
        with self.queue.connect() as db:
            self.assertEqual(db.execute("SELECT attempts FROM jobs WHERE id=?", (self.key,)).fetchone()[0], 0)

    def test_a_deferred_job_keeps_no_error_and_stays_in_the_snapshot(self):
        self.queue.defer(self.queue.claim(), 0.0, "waiting-for-meeting")
        snapshot = self.queue.snapshot()
        self.assertEqual(snapshot["counts"], {"pending": 1})
        self.assertEqual(snapshot["recent"][0]["stage"], "waiting-for-meeting")
        self.assertIsNone(snapshot["recent"][0]["error"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe.DeferTests -v`
Expected: FAIL with `AttributeError: 'Queue' object has no attribute 'defer'`.

- [ ] **Step 3: Implement**

In `src/second_brain/queue.py`, add after `progress`:

```python
    def defer(self, job, seconds, stage):
        """Put a claimed job back without spending an attempt: it was never tried, only postponed."""
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state='pending',stage=?,error=NULL,next_attempt=?,"
                "attempts=max(0,attempts-1),updated=? WHERE id=?",
                (stage, time.time() + seconds, time.time(), job["id"]),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/queue.py tests/test_dedupe.py
git commit -m "feat(dedupe): defer a job without consuming a retry attempt"
```

---

### Task 6: Wire the runtime

**Files:**
- Modify: `src/second_brain/runtime.py`
- Test: `tests/test_dedupe.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dedupe.py`:

```python
class RuntimeDedupeTests(unittest.TestCase):
    def setUp(self):
        from second_brain.runtime import Runtime
        from tests.helpers import configuration
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = dataclasses.replace(configuration(self.root), meetings_enabled=True)
        self.runtime = Runtime(self.cfg)
        self.captures = self.cfg.incoming_dir / "meetings" / ".captures"
        self.captures.mkdir(parents=True)
        audio = self.cfg.incoming_dir / "omi.opus"
        audio.write_bytes(b"audio")
        self.job_id = self.runtime.queue.enqueue(
            audio, {"device": "d", "start_seq": 1, "first_timestamp": 1000, "audio_seconds": 600})

    def tearDown(self):
        self.tmp.cleanup()

    def marker(self, cid, state, **extra):
        (self.captures / f"{cid}.json").write_text(json.dumps(
            {"capture_id": cid, "client": "mac", "state": state, "start_ms": 1_200_000,
             "app": "us.zoom.xos", "updated": 1_791_300_000.0, **extra}))

    def test_an_open_overlapping_capture_defers_the_job(self):
        self.marker("cd" * 16, "open")
        job = self.runtime.queue.claim()
        self.assertTrue(self.runtime.defer_for_meeting(job))
        with self.runtime.queue.connect() as db:
            row = dict(db.execute("SELECT stage,state,attempts FROM jobs").fetchone())
        self.assertEqual(row, {"stage": "waiting-for-meeting", "state": "pending", "attempts": 0})

    def test_a_closed_capture_does_not_defer_and_yields_an_index(self):
        self.marker("ab" * 16, "closed", end_ms=1_500_000)
        (self.cfg.incoming_dir / "meetings" / f"{'ab' * 16}.caf").write_bytes(b"caf")
        job = self.runtime.queue.claim()
        self.assertFalse(self.runtime.defer_for_meeting(job))
        index = self.runtime.meeting_index(job, self.cfg.data_dir / "jobs" / job["id"])
        value = json.loads(Path(index).read_text())
        self.assertEqual(value["recording_epoch"], 1000)
        self.assertEqual([c["capture_id"] for c in value["captures"]], ["ab" * 16])

    def test_no_overlap_means_no_index_and_no_flag(self):
        job = self.runtime.queue.claim()
        self.assertFalse(self.runtime.defer_for_meeting(job))
        self.assertIsNone(self.runtime.meeting_index(job, self.cfg.data_dir / "jobs" / job["id"]))

    def test_meetings_disabled_skips_dedupe_entirely(self):
        self.marker("cd" * 16, "open")
        self.runtime.cfg = dataclasses.replace(self.cfg, meetings_enabled=False)
        job = self.runtime.queue.claim()
        self.assertFalse(self.runtime.defer_for_meeting(job))
        self.assertIsNone(self.runtime.meeting_index(job, self.cfg.data_dir / "jobs" / job["id"]))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe.RuntimeDedupeTests -v`
Expected: FAIL with `AttributeError: 'Runtime' object has no attribute 'defer_for_meeting'`.

- [ ] **Step 3: Implement**

In `src/second_brain/runtime.py`:

1. Add to the imports: `from .meetings import overlapping`.

2. Add a module constant next to `STAGES`:

```python
MEETING_WAIT_SECONDS = 300  # recheck an Omi job whose meeting is still uploading
```

3. Add these two methods to `Runtime` (above `process`):

```python
    def meeting_overlaps(self, job):
        """Meeting captures overlapping this Omi recording, or None when dedupe does not apply."""
        if not self.cfg.meetings_enabled:
            return None
        return overlapping(self.cfg.incoming_dir / "meetings", json.loads(job["metadata"]))

    def defer_for_meeting(self, job):
        """True when an overlapping capture is still open: the job waits rather than publishing
        speech the meeting note will also contain."""
        found = self.meeting_overlaps(job)
        if not found or not found.open:
            return False
        log.info("Job %s waits for %d uploading meeting capture(s)", job["id"], len(found.open))
        self.queue.defer(job, MEETING_WAIT_SECONDS, "waiting-for-meeting")
        return True

    def meeting_index(self, job, root: Path):
        """Write the dedupe index for the pipeline, or None when there is nothing to compare."""
        found = self.meeting_overlaps(job)
        if not found or not found.closed:
            return None
        span = recording_span(json.loads(job["metadata"]))
        path = root / "meeting-dedupe.json"
        write_json(path, {"recording_epoch": int(span[0]), "captures": found.closed})
        return str(path)
```

Import `recording_span` alongside `overlapping`. `defer_for_meeting` and `meeting_index` each read
the markers once; a job therefore reads them twice. That is a handful of small files per job and is
not worth caching.

4. In `process`, inside the `if not manifest_path.exists():` block, after the `if meeting: cmd.append("--meeting")` lines (so a replay from a saved manifest never re-runs dedupe), add:

```python
            index = None if meeting else self.meeting_index(job, root)
            if index:
                cmd += ["--meeting-dedupe", index]
```

5. In the returned result dict at the end of `process`, add the dedupe counts:

```python
            **({"deduped": manifest["dedupe"]["dropped_count"],
                "alignment_failed": manifest["dedupe"]["alignment_failed"]}
               if "dedupe" in manifest else {}),
```

6. In `worker`, immediately after `job = self.queue.claim()` and before the `if job:` body runs the work, skip deferred jobs:

```python
            if job and await asyncio.to_thread(self.defer_for_meeting, job):
                job = None
```

`STAGES` needs no change: it filters stage names read from the pipeline's progress file, while
`defer` writes its stage straight to SQLite.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe -v`
Expected: all PASS.
Run: `python3 scripts/test.py`
Expected: both Python suites `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/runtime.py tests/test_dedupe.py
git commit -m "feat(dedupe): defer Omi jobs for uploading meetings and index closed ones"
```

---

### Task 7: End-to-end through the real service

**Files:**
- Test: `tests/test_dedupe.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dedupe.py`:

```python
class DedupeEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from second_brain.runtime import Runtime
        from tests.helpers import configuration
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        base = configuration(self.root)
        cfg = json.loads(base.pipeline_config.read_text())
        cfg["moss_command"] = [sys.executable, str(ROOT / "tests/fixtures/fake_moss_halves.py")]
        base.pipeline_config.write_text(json.dumps(cfg))
        self.cfg = dataclasses.replace(base, meetings_enabled=True, owner_name="Eugene",
                                       delete_audio_after_processing=False)
        self.runtime = Runtime(self.cfg)
        self.task = asyncio.create_task(self.runtime.run())
        for _ in range(100):
            if self.runtime.server and self.runtime.server._server:
                break
            if self.task.done():
                await self.task
            await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        self.runtime.stop.set()
        await asyncio.wait_for(self.task, 10)
        self.tmp.cleanup()

    def place_capture(self, cid, state, start_ms, end_ms=None, audio=None):
        """Write a capture marker (and audio) the way the receiver would have."""
        captures = self.cfg.incoming_dir / "meetings" / ".captures"
        captures.mkdir(parents=True, exist_ok=True)
        value = {"capture_id": cid, "client": "mac", "state": state, "start_ms": start_ms,
                 "app": "us.zoom.xos", "updated": __import__("time").time()}
        if end_ms is not None:
            value["end_ms"] = end_ms
        (captures / f"{cid}.json").write_text(json.dumps(value))
        if audio is not None:
            (self.cfg.incoming_dir / "meetings" / f"{cid}.caf").write_bytes(Path(audio).read_bytes())

    def place_omi(self, first_timestamp, audio):
        """Publish an Omi recording receipt the way the receiver would have."""
        incoming = self.cfg.incoming_dir
        target = incoming / "omi.wav"
        target.write_bytes(Path(audio).read_bytes())
        metadata = {"device": "112233445566", "start_seq": 1, "first_timestamp": first_timestamp,
                    "audio_seconds": 120.0, "first_utc": "2026-10-06 14:30:00Z", "complete": True}
        (incoming / "omi.json").write_text(json.dumps(metadata))
        (incoming / ".ready").mkdir(exist_ok=True)
        (incoming / ".ready" / "omi.json").write_text(json.dumps({"audio": str(target), "metadata": metadata}))

    async def wait(self, predicate, what, seconds=40):
        for _ in range(int(seconds / 0.05)):
            if predicate():
                return
            await asyncio.sleep(0.05)
        logs = "\n".join(p.read_text()[-3000:] for p in (self.root / "data/jobs").rglob("pipeline.log"))
        self.fail(f"timed out waiting for {what}: {self.runtime.queue.snapshot()}\n{logs}")

    async def test_an_omi_recording_loses_only_the_meeting_half(self):
        omi = tone_wav(self.root / "omi-src.wav", f"if(lt(t,60),{CALL},{ROOM})", 120)
        capture = tone_wav(self.root / "cap.wav", f"if(lt(t,60),{CALL},0)|{OTHER}", 120, channels=2)
        self.place_capture("ab" * 16, "closed", 1_000_000, 1_120_000, capture)
        self.place_omi(1000, omi)
        await self.wait(lambda: self.runtime.queue.snapshot()["counts"].get("complete") == 1, "the job")
        notes = list((self.cfg.vault_path / "Omi" / "Conversations").glob("*.md"))
        text = "\n".join(n.read_text() for n in notes)
        self.assertIn("second half", text)
        self.assertNotIn("first half", text)
        with self.runtime.queue.connect() as db:
            saved = json.loads(db.execute("SELECT result FROM jobs").fetchone()[0])
        self.assertEqual(saved["deduped"], 1)
        self.assertFalse(saved["alignment_failed"])
        # The full archive keeps every segment; only windows and the note lose the duplicate.
        manifest = json.loads(next((self.root / "data/jobs").rglob("attempt-*/manifest.json")).read_text())
        self.assertEqual([d["text"] for d in manifest["dedupe"]["dropped"]], ["first half"])

    async def test_an_uploading_meeting_holds_the_omi_job_until_it_closes(self):
        omi = tone_wav(self.root / "omi2.wav", f"if(lt(t,60),{CALL},{ROOM})", 120)
        capture = tone_wav(self.root / "cap2.wav", f"if(lt(t,60),{CALL},0)|{OTHER}", 120, channels=2)
        self.place_capture("cd" * 16, "open", 1_000_000)
        self.place_omi(1000, omi)
        await self.wait(lambda: self.runtime.queue.snapshot()["recent"]
                        and self.runtime.queue.snapshot()["recent"][0]["stage"] == "waiting-for-meeting",
                        "the deferral")
        self.assertEqual(self.runtime.queue.snapshot()["counts"], {"pending": 1})
        self.assertEqual(list((self.cfg.vault_path / "Omi" / "Conversations").glob("*.md")), [])
        # The capture finishes uploading; the job must stop waiting and dedupe against it.
        self.place_capture("cd" * 16, "closed", 1_000_000, 1_120_000, capture)
        with self.runtime.queue.connect() as db:          # do not wait out MEETING_WAIT_SECONDS
            db.execute("UPDATE jobs SET next_attempt=0")
        await self.wait(lambda: self.runtime.queue.snapshot()["counts"].get("complete") == 1, "the job")
        text = "\n".join(n.read_text() for n in (self.cfg.vault_path / "Omi" / "Conversations").glob("*.md"))
        self.assertNotIn("first half", text)
        self.assertIn("second half", text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe.DedupeEndToEndTests -v`
Expected: both fail. The first publishes "first half", and the second never reaches the `waiting-for-meeting` stage.
(If they already pass, Tasks 4–6 are wired; confirm by checking the manifest's `dedupe` block exists.)

- [ ] **Step 3: Make them pass**

No new production code should be needed. If a test fails, diagnose the real cause (read the pipeline log the failure prints), fix the production code, and record what was wrong. Likely gaps: the `.ready` receipt shape, the stage name missing from `STAGES`, or `discover` re-enqueuing while a job is deferred.

- [ ] **Step 4: Verify**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_dedupe -v`
Then run `python3 scripts/test.py` **three times**: both Python suites must print `OK` every time. End-to-end tests here are timing-sensitive; report any run that differs.

- [ ] **Step 5: Commit**

```bash
git add tests/test_dedupe.py
git commit -m "test(dedupe): omi recording overlapping a meeting end to end"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md`, `SYSTEM.md`, `src/second_brain/ARCHITECTURE.md`, `docs/superpowers/specs/2026-09-29-meeting-capture-design.md`

- [ ] **Step 1: README**

Extend the `meetings_enabled` bullet (find it with `grep -n meetings_enabled README.md`) with:

```markdown
  When an Omi recording overlaps a finished capture, the Omi segments the meeting already holds
  are dropped from its note; anything else the Omi heard (someone in the room) is kept. An Omi
  recording whose meeting is still uploading waits up to 24 hours for it.
```

- [ ] **Step 2: SYSTEM.md**

In item 6, replace `Omi recordings overlapping a meeting are not yet deduplicated (Plan 2b).` with:

```markdown
   An Omi recording overlapping a capture is deduplicated before windows are built: each Omi
   segment's 50 ms speech-band RMS envelope is cross-correlated against the capture's L and R
   envelopes over the same wall-clock span, searching +-10 s for clock offset. A peak of 0.6 or
   more drops the segment (`dedupe.dropped` in the manifest records `deduped_by`, `channel`,
   `score` and `offset_seconds`); no audio is deleted and the full transcript archive keeps every
   segment. If no segment anywhere reaches 0.3 the alignment is treated as failed and everything
   is kept. While an overlapping capture is still `open` the Omi job waits (stage
   `waiting-for-meeting`, rechecked every 5 minutes) without spending a retry attempt; an `open`
   marker older than 24 h stops blocking. A recording whose device clock was never set
   (`first_timestamp` 0) is never deduplicated.
```

- [ ] **Step 3: SYSTEM.md known limits**

Add to the dated known-limits list:

```markdown
- Omi meeting dedupe, known limits (2026-10-07): a meeting uploaded after its overlapping Omi
  recording was already processed leaves two notes; nothing reconciles them afterwards. The
  owner accepted this. Dedupe decides per ASR segment, so a segment that mixes meeting speech
  with room speech is dropped or kept as a whole. Meeting audio is never deleted, so
  `incoming/meetings/` grows without bound. Correlation thresholds (0.6 drop, 0.3 alignment)
  are the spec's values, verified only against synthetic tones.
```

- [ ] **Step 4: ARCHITECTURE.md**

In the receipt → job → note section, after the meeting-jobs sentence, add:

```markdown
   The service resolves capture overlap (`second_brain.meetings`) and passes the pipeline only a
   written index of files to compare, so the pipeline holds no receiver-layout knowledge.
```

- [ ] **Step 5: Spec status**

Update the Status line to record §3 dedupe as implemented by this plan, leaving §4 tray counts outstanding. Note the two deviations: an expired `open` marker is treated as expired without rewriting it, and timing comes from `first_timestamp`.

- [ ] **Step 6: Verify and commit**

Run: `python3 scripts/test.py` → both Python suites `OK`.

```bash
git add README.md SYSTEM.md src/second_brain/ARCHITECTURE.md docs/superpowers/specs/2026-09-29-meeting-capture-design.md
git commit -m "docs(dedupe): omi recordings overlapping a meeting"
```

---

### Task 9: Final verification and handoff

- [ ] **Step 1: Full suite, three runs**

Run: `for i in 1 2 3; do python3 scripts/test.py > /tmp/dedupe-$i.log 2>&1; grep -E "^Ran|^OK|FAILED" /tmp/dedupe-$i.log; done`
Expected: two `OK` lines per run. The trailing `swift test` step needs a non-sandboxed shell; say so if it could not run.

- [ ] **Step 2: Report what was not verified**

State plainly in the handoff:
- Dedupe was exercised only against synthetic tones and fixture ASR. Real speech correlation, real clock drift between this Omi and this Mac, and the 0.6/0.3 thresholds on real recordings are unverified.
- No deployment was performed. The owner enables this with the already-shipped `meetings_enabled`; dedupe needs no new setting.
- The first real check worth doing on the owner's machine: take a meeting with the Omi also recording, then confirm the Omi note keeps room speech and loses the call, and that `dedupe.dropped` names the capture.
