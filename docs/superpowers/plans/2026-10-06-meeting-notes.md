# Meeting Notes (Pipeline Plan 2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn committed Mac meeting captures (`incoming/meetings/.ready/`) into one Obsidian meeting note per capture. The owner's mic channel is attributed to the owner, and the remote channel is diarized and voice-matched.

**Architecture:** Discovery queues meeting receipts as ordinary SQLite jobs when `meetings_enabled` is set. The existing pipeline subprocess gains a `--meeting` mode. It transcribes the L (mic) and R (remote) channels separately, keeps a mic segment only when the mic is ≥ 6 dB louder than the remote channel in the speech band, and labels kept mic segments `owner`. It then scores the merged segments with the unchanged v3 windows. Publication writes a single note with highlights first and the full transcript, ignoring the memory gate (spec: the ephemeral filter is not applied to meetings). The gate still decides what is routed to Hermes.

**Tech Stack:** Python 3.12 stdlib, ffmpeg/ffprobe, SQLite, unittest. No new dependencies.

Spec: `docs/superpowers/specs/2026-09-29-meeting-capture-design.md` §3 "Meeting jobs". Omi dedupe (§3 "Omi jobs overlapping a meeting") and tray meeting counts (§4) are **Plan 2b** and out of scope here.

Deliberate deviations from the spec, to be documented in `SYSTEM.md` (Task 10):
- The owner is named by a new service setting `owner_name`. The speaker database has no "owner" identity today, so mic segments are not voice-matched.
- 7B refinement is disabled for meeting jobs. It refines a mono mix, so its speaker turns could no longer be mapped back to mic or remote. Meeting windows always use MOSS.

---

## File map

| File | Responsibility |
|---|---|
| `src/second_brain/config.py` (modify) | `meetings_enabled`, `meetings_vault_folder`, `owner_name` + validation |
| `src/second_brain/io.py` (modify) | `utc_from_ms()` shared timestamp formatter |
| `src/second_brain/adaptive/channels.py` (create) | Stereo helpers: ffmpeg channel selection, speech-band frame levels, mic-dominance rule |
| `src/second_brain/adaptive/pipeline.py` (modify) | Channel-aware extraction/silence detection, `transcribe_spans()`, `transcribe_meeting()`, `--meeting` |
| `src/second_brain/speaker_audio.py` (modify) | Skip owner (L) segments for review; extract R-channel clips |
| `src/second_brain/speakers.py` (modify) | Render mic segments as the owner; meeting speakers always count as published |
| `src/second_brain/vault.py` (modify) | Shared atomic publication helper; `publish_meeting()` |
| `src/second_brain/queue.py` (modify) | Job identity for meeting metadata |
| `src/second_brain/runtime.py` (modify) | Discover meeting receipts, run `--meeting`, publish meeting note |
| `tests/fixtures/fake_moss_tones.py` (create) | Deterministic ASR fixture whose text depends on the input tone |
| `tests/test_meetings.py` (create) | Config, channels, pipeline meeting mode, vault, end-to-end |
| `tests/test_meeting_receiver.py` (modify) | Existing "not enqueued" test now means "flag off" |
| `config/service.example.json`, `README.md`, `SYSTEM.md`, `src/second_brain/ARCHITECTURE.md`, `docs/superpowers/specs/2026-09-29-meeting-capture-design.md` (modify) | Docs move with code |

Run all Python tests with `python scripts/test.py`. A single test module runs with
`PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings -v` from the repo root.

---

### Task 1: Configuration keys

**Files:**
- Modify: `src/second_brain/config.py`
- Test: `tests/test_meetings.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_meetings.py`:

```python
"""Meeting captures from the Mac app become one vault note each (spec §3, Plan 2a)."""

from __future__ import annotations
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from second_brain.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))


def write_config(root: Path, **extra):
    for name in ("data", "incoming", "vault"):
        (root / name).mkdir(exist_ok=True)
    raw = {
        "data_dir": str(root / "data"),
        "incoming_dir": str(root / "incoming"),
        "secret_file": str(root / "secret.hex"),
        "pipeline_config": str(root / "pipeline.json"),
        "vault_path": str(root / "vault"),
        "status_file": str(root / "status.json"),
        **extra,
    }
    path = root / "service.json"
    path.write_text(json.dumps(raw))
    return path


class MeetingConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_meetings_are_off_by_default(self):
        cfg = Config.load(write_config(self.root))
        self.assertFalse(cfg.meetings_enabled)
        self.assertEqual(cfg.meetings_vault_folder, "Omi/Meetings")
        self.assertEqual(cfg.owner_name, "Me")

    def test_meeting_keys_load(self):
        cfg = Config.load(write_config(self.root, meetings_enabled=True, meetings_vault_folder="Meetings",
                                       owner_name="Eugene"))
        self.assertTrue(cfg.meetings_enabled)
        self.assertEqual((cfg.meetings_vault_folder, cfg.owner_name), ("Meetings", "Eugene"))

    def test_unsafe_meeting_folder_is_rejected(self):
        for folder in ("../x", "/abs", "a/./b", "C:x", ""):
            with self.assertRaisesRegex(ValueError, "meetings_vault_folder must be a safe relative folder"):
                Config.load(write_config(self.root, meetings_vault_folder=folder))

    def test_owner_name_is_a_plain_display_name(self):
        for name in ("", " ", "x" * 81, "Bad [[link]]", "tab\tname"):
            with self.assertRaisesRegex(ValueError, "owner_name"):
                Config.load(write_config(self.root, owner_name=name))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'meetings_enabled'` (and `Unknown service configuration keys` for the load test).

- [ ] **Step 3: Implement**

In `src/second_brain/config.py`, add after `delete_audio_after_processing: bool = True`:

```python
    # Mac meeting captures (docs/superpowers/specs/2026-09-29-meeting-capture-design.md §3).
    meetings_enabled: bool = False
    meetings_vault_folder: str = "Omi/Meetings"
    owner_name: str = "Me"  # who speaks on a meeting capture's mic channel
```

Add a module-level helper above `class Config`:

```python
def check_folder(key: str, value: str) -> None:
    folder = value.replace("\\", "/")
    if (
        not folder
        or Path(folder).is_absolute()
        or any(x in ("..", ".", "") for x in folder.split("/"))
        or ":" in folder
    ):
        raise ValueError(f"{key} must be a safe relative folder")
```

In `Config.load`, replace the existing `folder = ...` block (the eight lines ending in `raise ValueError("vault_folder must be a safe relative folder")`) with:

```python
        check_folder("vault_folder", cfg.vault_folder)
        check_folder("meetings_vault_folder", cfg.meetings_vault_folder)
        owner = cfg.owner_name.strip()
        if not 1 <= len(owner) <= 80 or owner != cfg.owner_name or any(
            ord(c) < 32 or c in "[]<>\\|" for c in owner
        ):
            raise ValueError("owner_name must be 1–80 characters without control characters or markup brackets")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings -v`
Expected: 4 tests PASS. Then run `python scripts/test.py` and confirm that both Python suites print `OK`. The existing vault_folder error text is unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/config.py tests/test_meetings.py
git commit -m "feat(meetings): service settings for meeting notes"
```

---

### Task 2: Stereo channel helpers

**Files:**
- Create: `src/second_brain/adaptive/channels.py`
- Modify: `src/second_brain/io.py`
- Test: `tests/test_meetings.py`

The fixture audio is a 10 s stereo capture. L is a 440 Hz "owner" tone for 0–5 s, then −20 dB bleed of the remote 880 Hz tone. R is silent for 0–5 s, then the 880 Hz remote tone. It is Ogg Opus, as the Mac app's CAF is also Opus. The receiver stores uploads as `.caf` regardless of container and ffmpeg probes content, so tests never need macOS `afconvert`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meetings.py` (above `if __name__`):

```python
L_EXPR = "if(lt(t,5),0.5*sin(2*PI*440*t),0.05*sin(2*PI*880*t))"
R_EXPR = "if(lt(t,5),0,0.5*sin(2*PI*880*t))"


def make_capture(path: Path, seconds: int = 10) -> Path:
    """Stereo Opus: owner tone on L, remote tone on R, remote bleed into L after 5 s."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"aevalsrc='{L_EXPR}|{R_EXPR}':s=48000:d={seconds}",
         "-c:a", "libopus", "-b:a", "96k", "-f", "ogg", str(path)],
        check=True,
    )
    return path


class ChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.capture = make_capture(Path(cls.tmp.name) / "capture.caf")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_frames_cover_the_capture_in_50_ms_steps(self):
        from second_brain.adaptive.channels import stereo_frames
        left, right = stereo_frames(self.capture)
        self.assertEqual(len(left), len(right))
        self.assertAlmostEqual(len(left), 200, delta=2)

    def test_mic_dominates_only_where_the_owner_speaks(self):
        from second_brain.adaptive.channels import mic_dominates, span_level_db, stereo_frames
        left, right = stereo_frames(self.capture)
        self.assertTrue(mic_dominates(left, right, 0.5, 4.5))
        self.assertFalse(mic_dominates(left, right, 5.5, 9.5))  # bleed is ~20 dB below the remote channel
        self.assertAlmostEqual(span_level_db(right, 5.5, 9.5) - span_level_db(left, 5.5, 9.5), 20, delta=3)

    def test_silence_on_both_channels_is_not_the_owner(self):
        from second_brain.adaptive.channels import mic_dominates
        self.assertFalse(mic_dominates([0.0] * 10, [0.0] * 10, 0, 0.5))
        self.assertEqual(mic_dominates([4.0] * 10, [0.0] * 10, 0, 0.5), True)

    def test_downmix_selects_a_channel_or_mixes(self):
        from second_brain.adaptive.channels import downmix
        self.assertEqual(downmix(None), ["-ac", "1"])
        self.assertEqual(downmix("L"), ["-af", "pan=mono|c0=c0"])
        self.assertEqual(downmix("R"), ["-af", "pan=mono|c0=c1"])

    def test_utc_from_ms(self):
        from second_brain.io import utc_from_ms
        self.assertEqual(utc_from_ms(1759761000000), "2025-10-06T14:30:00Z")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings.ChannelTests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'second_brain.adaptive.channels'`.

- [ ] **Step 3: Implement**

Create `src/second_brain/adaptive/channels.py`:

```python
"""Stereo meeting captures (L = owner's mic, R = meeting app): channel selection and
speech-band levels. Shared by the pipeline (owner attribution) and speaker clips."""

from __future__ import annotations
import math
import operator
import subprocess
from array import array

CHANNEL_INDEX = {"L": 0, "R": 1}
FRAME_SECONDS = 0.05
RATE = 8000
FRAME = int(RATE * FRAME_SECONDS)  # samples per channel per frame
OWNER_MARGIN_DB = 6.0


def downmix(channel):
    """ffmpeg arguments selecting one channel of a stereo capture, or the usual mono mix."""
    if channel is None:
        return ["-ac", "1"]
    return ["-af", f"pan=mono|c0=c{CHANNEL_INDEX[channel]}"]


def stereo_frames(audio, timeout=14400):
    """Mean-square 300–3400 Hz level of each 50 ms frame: ([L...], [R...]). Streams the decode."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(audio), "-af", "highpass=f=300,lowpass=f=3400",
           "-ac", "2", "-ar", str(RATE), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    left, right = [], []
    try:
        while chunk := proc.stdout.read(FRAME * 4):
            samples = array("h")
            samples.frombytes(chunk[: len(chunk) - len(chunk) % 4])
            if not samples:
                break
            l, r = samples[0::2], samples[1::2]
            left.append(sum(map(operator.mul, l, l)) / len(l))
            right.append(sum(map(operator.mul, r, r)) / len(r))
        error = proc.stderr.read()
        if proc.wait(timeout=timeout) != 0:
            raise RuntimeError("ffmpeg could not decode the capture: " + error.decode(errors="replace")[-2000:])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return left, right


def span_level_db(frames, start, end):
    first = max(0, int(start / FRAME_SECONDS))
    last = max(first + 1, math.ceil(end / FRAME_SECONDS))
    window = frames[first:last]
    mean = sum(window) / len(window) if window else 0.0
    return 10 * math.log10(mean) if mean > 0 else -math.inf


def mic_dominates(left, right, start, end, margin_db=OWNER_MARGIN_DB):
    """Owner attribution: the mic is at least margin_db louder than the remote channel, so remote
    audio bleeding into the mic is never credited to the owner."""
    mic = span_level_db(left, start, end)
    return mic > -math.inf and mic - span_level_db(right, start, end) >= margin_db
```

In `src/second_brain/io.py`, add `import datetime as dt` to the imports and append:

```python
def utc_from_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/adaptive/channels.py src/second_brain/io.py tests/test_meetings.py
git commit -m "feat(meetings): speech-band channel levels and owner attribution rule"
```

---

### Task 3: Channel-aware extraction and a reusable transcription stage

**Files:**
- Modify: `src/second_brain/adaptive/pipeline.py` (`detect_long_silences`, `extract_wav`, Stage 1 in `main`)
- Test: `tests/test_meetings.py`

This is a pure refactor for Omi jobs. `EndToEndTests` in `tests/test_service.py` asserts `c0000:S1` labels and must keep passing unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meetings.py`:

```python
def dominant_hz(path: Path) -> float:
    with wave.open(str(path)) as w:
        frames = w.readframes(w.getnframes())
        rate, n = w.getframerate(), w.getnframes()
    samples = array("h")
    samples.frombytes(frames)
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (n / rate)


class ChannelExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.capture = make_capture(cls.root / "capture.caf")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_extract_wav_selects_the_requested_channel(self):
        from second_brain.adaptive.pipeline import extract_wav
        left = extract_wav(str(self.capture), 0, 5, 16000, self.root / "l.wav", "L")
        right = extract_wav(str(self.capture), 5, 10, 16000, self.root / "r.wav", "R")
        self.assertAlmostEqual(dominant_hz(left), 440, delta=30)
        self.assertAlmostEqual(dominant_hz(right), 880, delta=30)
        with wave.open(str(left)) as w:
            self.assertEqual((w.getnchannels(), w.getframerate()), (1, 16000))

    def test_silence_detection_can_listen_to_one_channel(self):
        from second_brain.adaptive.pipeline import detect_long_silences
        silences = detect_long_silences(str(self.capture), -42.0, 2.0, "R")
        self.assertEqual(len(silences), 1)
        self.assertAlmostEqual(silences[0][1], 5.0, delta=0.3)
        self.assertEqual(detect_long_silences(str(self.capture), -42.0, 2.0, "L"), [])

    def test_channel_count(self):
        from second_brain.adaptive.pipeline import ffprobe_channels
        self.assertEqual(ffprobe_channels(str(self.capture)), 2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings.ChannelExtractionTests -v`
Expected: FAIL. `extract_wav() takes 5 positional arguments but 6 were given` and `ImportError: cannot import name 'ffprobe_channels'`.

- [ ] **Step 3: Implement**

In `src/second_brain/adaptive/pipeline.py`:

1. Add to imports: `from second_brain.adaptive.channels import CHANNEL_INDEX, downmix`.

2. Below `ffprobe_duration`, add:

```python
def ffprobe_channels(audio):
    proc = run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
         "-of", "default=noprint_wrappers=1:nokey=1", audio]
    )
    return int(proc.stdout.strip())
```

3. Change `detect_long_silences` to accept a channel. Replace its signature and `-af` value:

```python
def detect_long_silences(audio, noise_db, duration, channel=None):
    select = f"pan=mono|c0=c{CHANNEL_INDEX[channel]}," if channel else ""
```

and in its command list replace `f"silencedetect=noise={noise_db}dB:d={duration}",` with
`f"{select}silencedetect=noise={noise_db}dB:d={duration}",`.

4. Change `extract_wav`:

```python
def extract_wav(audio, start, end, sample_rate, out, channel=None):
```

and in its command replace the two list items `"-ac",` / `"1",` with `*downmix(channel),`.

5. Above `def main():`, add the extracted Stage 1 loop:

```python
def transcribe_spans(audio, spans, cfg, coarse_dir, prefix="c", channel=None):
    """MOSS each span of one channel (or the mono mix). Returns (segments, chunk records)."""
    moss_exe = str(Path(cfg["moss_cpp_engine_dir"]) / ("moss-transcribe.exe" if os.name == "nt" else "moss-transcribe"))
    moss_runner = str(Path(__file__).parent / "runners" / "moss_cpp_runner.py")
    segments, chunks = [], []
    for idx, (st, en) in enumerate(spans):
        cid = f"{prefix}{idx:04d}"
        wav = coarse_dir / f"{cid}.wav"
        jout = coarse_dir / f"{cid}.json"
        extract_wav(audio, st, en, 16000, wav, channel)
        run(
            [
                sys.executable,
                moss_runner,
                "--command-json",
                json.dumps(cfg.get("moss_command", [moss_exe])),
                "--model",
                cfg["moss_model"],
                "--audio",
                str(wav),
                "--output",
                str(jout),
                "--threads",
                str(cfg["moss_threads"]),
                "--device",
                cfg.get("moss_device", "cpu"),
                "--max-new",
                str(cfg["moss_max_new"]),
            ],
            capture=False,
        )
        count = 0
        for seg in load_json(jout).get("segments", []):
            abs_seg = dict(seg)
            abs_seg["start"] = st + float(seg["start"])
            abs_seg["end"] = st + float(seg["end"])
            abs_seg["coarse_chunk"] = cid
            abs_seg["speaker"] = cid + ":" + str(seg.get("speaker", "S?"))
            if channel:
                abs_seg["channel"] = channel
            segments.append(abs_seg)
            count += 1
        chunk = {"id": cid, "start": st, "end": en, "audio": str(wav), "moss_json": str(jout), "segment_count": count}
        if channel:
            chunk["channel"] = channel
        chunks.append(chunk)
    return segments, chunks
```

6. In `main`, replace the whole Stage 1 body: everything from `moss_exe = str(...` through the end of the `for idx, (st, en) in enumerate(coarse_spans):` loop. Keep the line `all_segments.sort(...)` that follows. The replacement is:

```python
    all_segments, manifest["coarse_chunks"] = transcribe_spans(audio, coarse_spans, cfg, coarse_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings -v`
Expected: all PASS.
Run: `python scripts/test.py`
Expected: both Python suites `OK`; `EndToEndTests` still finds `c0000:S1`/`c0000:S2`.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/adaptive/pipeline.py tests/test_meetings.py
git commit -m "refactor(pipeline): channel-aware extraction and reusable MOSS stage"
```

---

### Task 4: Pipeline `--meeting` mode

**Files:**
- Create: `tests/fixtures/fake_moss_tones.py`
- Modify: `src/second_brain/adaptive/pipeline.py`
- Test: `tests/test_meetings.py`

- [ ] **Step 1: Create the tone-aware ASR fixture**

Create `tests/fixtures/fake_moss_tones.py`. Like `fake_moss.py`, it is a deterministic contract fixture that is never installed as production configuration. The tests import its two texts, so the transcription runs only under `__main__`:

```python
"""Deterministic ASR contract fixture for meeting tests, never used in production configuration.

Each half of the chunk becomes one segment if it is not silent. The text names the tone, so a test
can tell which channel's audio reached the transcriber: below 660 Hz is the owner, above is remote."""

import json
import sys
import wave
from array import array

OWNER_TEXT = "We decided the project launch moves to next Friday at four PM."
REMOTE_TEXT = "I will send the final pricing proposal to the client tomorrow."


def main():
    if sys.argv[1] != "transcribe":
        raise SystemExit(2)
    with wave.open(sys.argv[3]) as audio:
        rate = audio.getframerate()
        samples = array("h")
        samples.frombytes(audio.readframes(audio.getnframes()))
    half = len(samples) // 2
    segments = []
    for index, part in enumerate((samples[:half], samples[half:])):
        if not part:
            continue
        rms = (sum(x * x for x in part) / len(part)) ** 0.5
        if rms < 300:
            continue
        crossings = sum(1 for a, b in zip(part, part[1:]) if (a < 0) != (b < 0))
        hz = crossings / 2 / (len(part) / rate)
        start = (0 if index == 0 else half) / rate
        end = (half if index == 0 else len(samples)) / rate
        segments.append({"start": start, "end": end, "speaker": "S1",
                         "text": OWNER_TEXT if hz < 660 else REMOTE_TEXT})
    print(json.dumps({"segments": segments}))


if __name__ == "__main__":
    main()
```

Check how `moss_cpp_runner.py` invokes the command (`sed -n 1,125p src/second_brain/adaptive/runners/moss_cpp_runner.py`). It must pass `transcribe` as `argv[1]` and the WAV as `argv[3]`, exactly as for `tests/fixtures/fake_moss.py`. If not, mirror whatever `fake_moss.py` reads.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_meetings.py`:

```python
from fake_moss_tones import OWNER_TEXT, REMOTE_TEXT  # noqa: E402  (fixtures dir is on sys.path, see top)


def meeting_pipeline_config(root: Path) -> Path:
    cfg = json.loads((ROOT / "config/pipeline.example.json").read_text())
    cfg.update(moss_command=[sys.executable, str(ROOT / "tests/fixtures/fake_moss_tones.py")],
               scan_vault_for_novelty=False, long_silence_seconds=999, memory_gate_min_words=1,
               work_root=str(root / "work"))
    path = root / "pipeline.json"
    path.write_text(json.dumps(cfg))
    return path


class MeetingPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        capture = make_capture(root / "capture.caf")
        out = root / "run"
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
        proc = subprocess.run(
            [sys.executable, "-m", "second_brain.adaptive.pipeline", "--audio", str(capture),
             "--config", str(meeting_pipeline_config(root)), "--output-dir", str(out), "--meeting",
             "--no-hermes"],
            capture_output=True, text=True, env=env,
        )
        if proc.returncode:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
        cls.manifest = json.loads((out / "manifest.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def segments(self):
        return [s for w in self.manifest["windows"] for s in w["final_segments"]]

    def test_owner_speech_comes_from_the_mic_channel(self):
        owner = [s for s in self.segments() if s.get("channel") == "L"]
        self.assertEqual([(s["speaker"], s["text"]) for s in owner], [("owner", OWNER_TEXT)])
        self.assertAlmostEqual(owner[0]["end"], 5.0, delta=0.2)

    def test_remote_speech_is_diarized_on_the_remote_channel(self):
        remote = [s for s in self.segments() if s.get("channel") == "R"]
        self.assertEqual([(s["speaker"], s["text"]) for s in remote], [("r0000:S1", REMOTE_TEXT)])

    def test_remote_bleed_into_the_mic_is_dropped_and_recorded(self):
        meeting = self.manifest["meeting"]
        self.assertEqual(meeting["owner_segments"], 1)
        self.assertEqual(meeting["remote_segments"], 1)
        self.assertEqual([s["text"] for s in meeting["bleed_dropped"]], [REMOTE_TEXT])
        self.assertEqual(sum(s["text"] == REMOTE_TEXT for s in self.segments()), 1)

    def test_meeting_windows_never_use_mono_refinement(self):
        self.assertTrue(self.manifest["windows"])
        self.assertEqual({w["asr_tier"] for w in self.manifest["windows"]}, {"moss"})
        self.assertEqual({c["channel"] for c in self.manifest["coarse_chunks"]}, {"L", "R"})

    def test_owner_is_not_offered_for_speaker_review(self):
        labels = {o["label"] for o in self.manifest["speaker_observations"]}
        self.assertEqual(labels, {"r0000:S1"})
        self.assertEqual(self.manifest["speaker_observations"][0]["channel"], "R")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings.MeetingPipelineTests -v`
Expected: ERROR in `setUpClass` with `error: unrecognized arguments: --meeting`.

- [ ] **Step 4: Implement the pipeline mode**

In `src/second_brain/adaptive/pipeline.py`:

1. Above `def main():` (after `transcribe_spans`), add:

```python
def merge_spans(spans):
    merged = []
    for st, en in sorted(spans):
        if merged and st <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], en))
        else:
            merged.append((st, en))
    return merged


def transcribe_meeting(audio, total, cfg, coarse_dir, manifest):
    """Meeting capture: L = owner's mic, R = meeting app. Each channel is transcribed on its own.
    A mic segment is the owner's only where the mic is >= 6 dB louder than the remote channel in the
    speech band; other mic segments are remote audio bleeding into the mic and are dropped (kept in
    the manifest as `bleed_dropped`)."""
    from second_brain.adaptive.channels import mic_dominates, stereo_frames

    if ffprobe_channels(audio) != 2:
        raise ValueError("Meeting capture must be stereo (L = mic, R = meeting app)")
    spans, active, silences = {}, [], {}
    for channel in ("L", "R"):
        silences[channel] = detect_long_silences(audio, cfg["silence_noise_db"], cfg["long_silence_seconds"], channel)
        channel_active = complement_silences(total, silences[channel])
        active += channel_active
        spans[channel] = split_spans(channel_active, cfg["coarse_max_seconds"])
    manifest["long_silences"] = silences
    manifest["active_duration_seconds"] = sum(en - st for st, en in merge_spans(active))
    progress("transcribing")
    print("=== Stage 1: MOSS per meeting channel ===", flush=True)
    mic, mic_chunks = transcribe_spans(audio, spans["L"], cfg, coarse_dir, "l", "L")
    remote, remote_chunks = transcribe_spans(audio, spans["R"], cfg, coarse_dir, "r", "R")
    manifest["coarse_chunks"] = mic_chunks + remote_chunks
    left, right = stereo_frames(audio, COMMAND_TIMEOUT)
    owner, bleed = [], []
    for seg in mic:
        seg["speaker"] = "owner"
        (owner if mic_dominates(left, right, seg["start"], seg["end"]) else bleed).append(seg)
    manifest["meeting"] = {
        "channels": {"L": "mic", "R": "remote"},
        "owner_segments": len(owner),
        "remote_segments": len(remote),
        "bleed_dropped": bleed,
        "refinement": "disabled: 7B refines a mono mix, which loses mic/remote attribution",
    }
    return owner + remote
```

2. In `main`, add the argument after `--progress-file`:

```python
    ap.add_argument("--meeting", action="store_true", help="stereo meeting capture: L mic (owner), R remote")
```

3. In `main`, replace the Stage 0 + Stage 1 region. That is from `progress("segmentation")` through the `all_segments, manifest["coarse_chunks"] = transcribe_spans(...)` line added in Task 3. Use this:

```python
    progress("segmentation")
    print("=== Stage 0: long-silence segmentation ===", flush=True)
    if args.meeting:
        all_segments = transcribe_meeting(audio, total_duration, cfg, coarse_dir, manifest)
    else:
        silences = detect_long_silences(audio, cfg["silence_noise_db"], cfg["long_silence_seconds"])
        active_spans = complement_silences(total_duration, silences)
        coarse_spans = split_spans(active_spans, cfg["coarse_max_seconds"])
        manifest["active_duration_seconds"] = sum(en - st for st, en in active_spans)
        manifest["long_silences"] = silences
        print(
            f"Audio {total_duration/3600:.2f}h -> {manifest['active_duration_seconds']/3600:.2f}h "
            f"outside long silences -> {len(coarse_spans)} MOSS chunks",
            flush=True,
        )
        progress("transcribing")
        print("=== Stage 1: MOSS first pass ===", flush=True)
        all_segments, manifest["coarse_chunks"] = transcribe_spans(audio, coarse_spans, cfg, coarse_dir)
```

4. In Stage 2, replace the `tier = (...)` assignment with:

```python
        tier = (
            "vibe7"
            if not args.meeting
            and should_escalate_to_vibe7(cfg, importance, uncertainty, heuristic, preliminary_memory_gate)
            else "moss"
        )
```

- [ ] **Step 5: Make speaker clips channel-aware**

In `src/second_brain/speaker_audio.py`:

1. Add the import `from .adaptive.channels import downmix`.

2. In `candidates`, inside `for index, source in enumerate(...)`, add as the first statement:

```python
            if source.get("channel") == "L":
                continue  # a meeting's mic is the owner (service owner_name), not someone to review
```

3. In `candidates`, record each label's channel. Add `channels = {}` next to `segments = []`. Directly after `segments.append(s)` add `channels[label] = s.get("channel")`. When building `result`, replace `result.append(dict(label=label, clips=sorted(selected, key=lambda s: s["start"])))` with:

```python
        observation = dict(label=label, clips=sorted(selected, key=lambda s: s["start"]))
        if channels.get(label):
            observation["channel"] = channels[label]
        result.append(observation)
```

4. In `prepare`, in the ffmpeg command, replace `"-ac",` / `"1",` with `*downmix(obs.get("channel")),`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings -v`
Expected: all PASS.
Run: `python scripts/test.py`
Expected: both Python suites `OK` (Omi speaker clips still mono mixes: `downmix(None)`).

- [ ] **Step 7: Commit**

```bash
git add src/second_brain/adaptive/pipeline.py src/second_brain/speaker_audio.py tests/fixtures/fake_moss_tones.py tests/test_meetings.py
git commit -m "feat(meetings): transcribe mic and remote channels with owner attribution"
```

---

### Task 5: Owner rendering and meeting speaker visibility

**Files:**
- Modify: `src/second_brain/speakers.py` (`ingest`, `render`)
- Test: `tests/test_meetings.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meetings.py`:

```python
class MeetingSpeakerTests(unittest.TestCase):
    def setUp(self):
        from second_brain.speakers import Speakers
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.speakers = Speakers(root / "data", root / "review")
        self.manifest = {"windows": [{
            "id": "w0000", "memory_keep": False, "scores": {"importance": 20},
            "final_segments": [
                {"start": 0.0, "end": 5.0, "speaker": "owner", "channel": "L", "text": OWNER_TEXT},
                {"start": 5.0, "end": 10.0, "speaker": "r0000:S1", "channel": "R", "text": REMOTE_TEXT},
            ]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_mic_segments_render_as_the_owner(self):
        window = self.speakers.render("job", self.manifest, owner_name="Eugene")["windows"][0]
        self.assertIn(f"[0.00-5.00] Eugene (mic): {OWNER_TEXT}", window["final_transcript"])
        self.assertIn(f"r0000:S1 (r0000:S1): {REMOTE_TEXT}", window["final_transcript"])
        self.assertEqual(window["speaker_identities"]["owner"], {"name": "Eugene", "source": "meeting microphone"})

    def test_omi_rendering_is_unchanged_without_an_owner(self):
        window = self.speakers.render("job", self.manifest)["windows"][0]
        self.assertIn("owner (owner):", window["final_transcript"])

    def test_meeting_speakers_are_reviewable_even_if_the_gate_dropped_the_window(self):
        clip = Path(self.tmp.name) / "data" / "jobs" / "job" / "c.wav"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"RIFF")
        manifest = dict(self.manifest, speaker_observations=[{"label": "r0000:S1", "channel": "R", "clips": [
            {"start": 5.0, "end": 10.0, "text": REMOTE_TEXT, "quality": "clean turn", "path": str(clip)}]}])
        job = {"id": "job", "metadata": json.dumps({"source": "meeting", "first_utc": "2026-10-06T14:30:00Z"})}
        self.speakers.ingest(job, manifest)
        with self.speakers.connect() as db:
            self.assertEqual(db.execute("SELECT published FROM observations").fetchone()[0], 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings.MeetingSpeakerTests -v`
Expected: FAIL. `render() got an unexpected keyword argument 'owner_name'`, and `published` is `0`.

- [ ] **Step 3: Implement**

In `src/second_brain/speakers.py`:

1. In `ingest`, after `metadata = json.loads(job["metadata"])`, add:

```python
        # Every meeting becomes a note, so its speakers are worth naming whatever the gate said.
        meeting = metadata.get("source") == "meeting"
```

and replace the final tuple element `*conversation_value(manifest, obs["label"]),` with:

```python
                        *((1, conversation_value(manifest, obs["label"])[1]) if meeting
                          else conversation_value(manifest, obs["label"])),
```

2. Change `render`'s signature to `def render(self, job_id, manifest, owner_name=None):`. In its inner loop, add before `label = seg["speaker"]`:

```python
                if owner_name and seg.get("channel") == "L":
                    rows.append(f"[{seg['start']:.2f}-{seg['end']:.2f}] {owner_name} (mic): {seg['text'].strip()}")
                    identities["owner"] = {"name": owner_name, "source": "meeting microphone"}
                    continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings -v`
Expected: all PASS. Run `python scripts/test.py` and confirm `tests/test_speakers.py` still passes.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/speakers.py tests/test_meetings.py
git commit -m "feat(meetings): render mic turns as the owner; meeting speakers stay reviewable"
```

---

### Task 6: One meeting note per capture

**Files:**
- Modify: `src/second_brain/vault.py`
- Test: `tests/test_meetings.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meetings.py`:

```python
def meeting_job(audio="C:/x/meetings/abc.caf"):
    return {"id": "f" * 64, "audio": audio, "sha256": "0" * 64, "metadata": json.dumps({
        "source": "meeting", "capture_id": "ab" * 16, "app": "us.zoom.xos",
        "start_ms": 1759761000000, "end_ms": 1759764600000, "first_utc": "2025-10-06T14:30:00Z"})}


def meeting_manifest():
    return {"windows": [
        {"id": "w0000", "start": 0.0, "end": 65.0, "memory_keep": False,
         "memory_gate": {"contains": {"decision": True, "task": True, "date_or_event": False}},
         "final_transcript": "[0.00-5.00] Eugene (mic): We decided.",
         "speaker_identities": {"owner": {"name": "Eugene", "source": "meeting microphone"}}},
        {"id": "w0001", "start": 3700.0, "end": 3720.0, "memory_keep": False,
         "memory_gate": {"contains": {}},
         "final_transcript": "[3700.00-3720.00] Ana (r0001:S1): Small talk.",
         "speaker_identities": {"r0001:S1": {"id": "p1", "name": "Ana", "source": "voice match"}}},
    ]}


class MeetingNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, manifest=None):
        from second_brain.vault import publish_meeting
        return publish_meeting(self.vault, "Omi/Meetings", meeting_job(), manifest or meeting_manifest())

    def test_one_note_with_highlights_first_and_every_window(self):
        written = self.publish()
        self.assertEqual(written, [f"Omi/Meetings/{'f' * 64}.md"])
        text = (self.vault / written[0]).read_text()
        self.assertIn('type: "omi-meeting"', text)
        self.assertIn('app: "us.zoom.xos"', text)
        self.assertIn('started_at: "2025-10-06T14:30:00Z"', text)
        self.assertIn('ended_at: "2025-10-06T15:30:00Z"', text)
        self.assertIn('participants: ["Ana", "Eugene"]', text)
        self.assertLess(text.index("## Highlights"), text.index("## Transcript"))
        self.assertIn("- 0:00–1:05 · decision, task", text)
        # The memory gate dropped both windows; meetings publish them anyway.
        self.assertIn("We decided.", text)
        self.assertIn("### 1:01:40", text)
        self.assertIn("Small talk.", text)

    def test_republishing_is_idempotent_and_edits_are_preserved(self):
        from second_brain.vault import NoteConflict
        path = self.vault / self.publish()[0]
        self.assertEqual(self.publish(), [str(path.relative_to(self.vault))])
        path.write_text(path.read_text() + "\nmy notes\n")
        with self.assertRaises(NoteConflict):
            self.publish()
        self.assertTrue(path.read_text().endswith("my notes\n"))

    def test_a_meeting_without_speech_still_gets_a_note(self):
        text = (self.vault / self.publish({"windows": []})[0]).read_text()
        self.assertIn("No decisions, tasks or dates detected.", text)
        self.assertIn("No speech was transcribed.", text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings.MeetingNoteTests -v`
Expected: FAIL with `ImportError: cannot import name 'publish_meeting'`.

- [ ] **Step 3: Implement**

Replace the body of `src/second_brain/vault.py` from `def publish(` to the end with the following. `publish` keeps its exact output; only its directory checks and atomic write move into helpers.

```python
def _destination(vault: Path, folder: str):
    if not vault.is_dir():
        raise FileNotFoundError("Configured Obsidian vault is unavailable")
    root = vault.resolve()
    dest = (root / folder).resolve()
    if not dest.is_relative_to(root):
        raise ValueError("Vault output escapes the configured vault")
    return root, dest


def _frontmatter(front: dict) -> list[str]:
    # JSON scalars are valid YAML and cannot inject new frontmatter keys.
    return ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items()] + ["---", ""]


def _write_note(path: Path, dest: Path, content: bytes) -> None:
    if path.resolve().parent != dest:
        raise ValueError("Note path escapes output directory")
    if path.exists():
        if path.read_bytes() != content:
            raise NoteConflict("A generated note was edited; existing content was preserved")
        return
    # Publication is atomic; a durable per-job manifest makes retries deterministic.
    dest.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=dest, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        try:
            if os.name == "nt":
                os.rename(temporary, path)  # Windows rename refuses an existing destination
            else:
                os.link(temporary, path)  # POSIX rename would replace an existing note
        except FileExistsError:
            if path.read_bytes() != content:
                raise NoteConflict("A note appeared during publication; existing content was preserved")
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish(vault: Path, folder: str, job: dict, manifest: dict, audio_retained: bool = True) -> list[str]:
    root, dest = _destination(vault, folder)
    metadata = json.loads(job["metadata"])
    written = []
    for window in manifest["windows"]:
        if window.get("route_to_knowledge_router") is not True or window.get("memory_keep") is not True:
            continue
        text = window["final_transcript"].strip()
        if not text:
            raise ValueError("Kept window has no transcript")
        window_id = window["id"]
        if not isinstance(window_id, str) or not window_id.isalnum():
            raise ValueError("Invalid window ID")
        # Full job identity prevents collision even for unknown timestamps.
        path = dest / f"{job['id']}-{window_id}.md"
        front = {
            "type": "omi-conversation",
            "source_id": job["id"],
            "window_id": window_id,
            "recorded_at": metadata.get("first_utc", "unknown-time"),
            "device": metadata["device"],
            "source_sha256": job["sha256"],
            **({"audio": Path(job["audio"]).as_uri()} if audio_retained else {}),
            "start_seconds": window["start"],
            "end_seconds": window["end"],
            "asr_engine": window["final_engine"],
            "conversation_type": window.get("memory_gate", {}).get("conversation_type", "other"),
            "speaker_identities": window.get("speaker_identities", {}),
        }
        lines = _frontmatter(front)
        lines += [
            f"# Omi conversation · {front['recorded_at']}",
            "",
            # The recording is deleted after processing unless retention is configured; never link a missing file.
            (f"[Source audio]({front['audio']})" if audio_retained else "Audio deleted after processing")
            + f" · {window['start']:.1f}–{window['end']:.1f} seconds",
            "",
            "Unnamed speaker labels are local to this recording/chunk. Named speakers include confirmation or voice-match provenance in the note properties.",
            "",
            "## Transcript",
            "",
            text,
            "",
            "## Memory gate",
            "",
            str(window.get("memory_reason", "Durable content")),
            "",
        ]
        _write_note(path, dest, "\n".join(lines).encode("utf-8"))
        written.append(str(path.relative_to(root)))
    return written


HIGHLIGHT_KINDS = ("decision", "task", "commitment", "date_or_event")


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def publish_meeting(vault: Path, folder: str, job: dict, manifest: dict) -> list[str]:
    """One note per meeting capture: highlights first, then the whole transcript. The memory gate
    never filters a meeting; its findings only choose what is highlighted. Meeting audio is
    retained (Omi dedupe needs it), so the note links it."""
    root, dest = _destination(vault, folder)
    metadata = json.loads(job["metadata"])
    windows = [w for w in manifest["windows"] if w.get("final_transcript", "").strip()]
    identities = {k: v for w in windows for k, v in w.get("speaker_identities", {}).items()}
    front = {
        "type": "omi-meeting",
        "source_id": job["id"],
        "capture_id": metadata["capture_id"],
        "app": metadata.get("app", "unknown"),
        "started_at": utc_from_ms(metadata["start_ms"]),
        "ended_at": utc_from_ms(metadata["end_ms"]),
        "source_sha256": job["sha256"],
        "audio": Path(job["audio"]).as_uri(),
        "participants": sorted({v["name"] for v in identities.values()}),
        "speaker_identities": identities,
    }
    highlights = []
    for w in windows:
        contains = w.get("memory_gate", {}).get("contains", {})
        kinds = [k.replace("_", " ") for k in HIGHLIGHT_KINDS if contains.get(k)]
        if kinds:
            highlights.append(f"- {_clock(w['start'])}–{_clock(w['end'])} · {', '.join(kinds)}")
    lines = _frontmatter(front) + [
        f"# Meeting · {front['app']} · {front['started_at']}",
        "",
        f"[Source audio]({front['audio']}) · L = microphone, R = meeting app",
        "",
        "Unnamed speaker labels are local to this capture. The microphone channel is the owner.",
        "",
        "## Highlights",
        "",
        *(highlights or ["No decisions, tasks or dates detected."]),
        "",
        "## Transcript",
        "",
    ]
    for w in windows:
        lines += [f"### {_clock(w['start'])}", "", w["final_transcript"].strip(), ""]
    if not windows:
        lines += ["No speech was transcribed.", ""]
    path = dest / f"{job['id']}.md"
    _write_note(path, dest, "\n".join(lines).encode("utf-8"))
    return [str(path.relative_to(root))]
```

Change the import line `from .io import atomic_write` to `from .io import utc_from_ms` (`atomic_write` is unused in this module; confirm with `grep -n atomic_write src/second_brain/vault.py` before removing it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings -v`
Expected: all PASS.
Run: `python scripts/test.py`
Expected: both Python suites `OK` (`QueueAndVaultTests` covers the unchanged Omi note).

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/vault.py tests/test_meetings.py
git commit -m "feat(meetings): one vault note per meeting with highlights first"
```

---

### Task 7: Queue identity and discovery

**Files:**
- Modify: `src/second_brain/queue.py`, `src/second_brain/runtime.py` (`discover`)
- Modify: `tests/test_meeting_receiver.py`
- Test: `tests/test_meetings.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meetings.py`:

```python
class MeetingDiscoveryTests(unittest.TestCase):
    def setUp(self):
        from second_brain.runtime import Runtime
        from tests.helpers import configuration
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dataclasses.replace(configuration(Path(self.tmp.name)), meetings_enabled=True)
        self.runtime = Runtime(self.cfg)
        meetings = self.cfg.incoming_dir / "meetings"
        (meetings / ".ready").mkdir(parents=True)
        self.audio = meetings / ("ab" * 16 + ".caf")
        self.audio.write_bytes(b"capture")
        (meetings / ".ready" / ("ab" * 16 + ".json")).write_text(json.dumps({"audio": str(self.audio), "metadata": {
            "source": "meeting", "capture_id": "ab" * 16, "app": "us.zoom.xos",
            "start_ms": 1759761000000, "end_ms": 1759764600000}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_enabled_meetings_are_queued_once_with_a_start_time(self):
        self.runtime.discover()
        self.runtime.discover()
        job = self.runtime.queue.claim()
        self.assertEqual(Path(job["audio"]), self.audio.resolve())
        metadata = json.loads(job["metadata"])
        self.assertEqual(metadata["first_utc"], "2025-10-06T14:30:00Z")
        self.assertIsNone(self.runtime.queue.claim())

    def test_disabled_meetings_are_not_queued(self):
        self.runtime.cfg = dataclasses.replace(self.cfg, meetings_enabled=False)
        self.runtime.discover()
        self.assertIsNone(self.runtime.queue.claim())

    def test_a_receipt_pointing_outside_incoming_is_refused(self):
        receipt = next((self.cfg.incoming_dir / "meetings" / ".ready").glob("*.json"))
        value = json.loads(receipt.read_text())
        value["audio"] = str(Path(self.tmp.name) / "elsewhere.caf")
        receipt.write_text(json.dumps(value))
        self.runtime.discover()
        self.assertIsNone(self.runtime.queue.claim())
        self.assertIn("discovery failed", self.runtime.last_scan_error)
```

Tests import helpers as `tests.helpers`, as the existing test modules do. Run from the repo root.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings.MeetingDiscoveryTests -v`
Expected: FAIL. `claim()` returns `None` (meeting receipts aren't scanned).

- [ ] **Step 3: Implement**

In `src/second_brain/queue.py` `enqueue`, replace `identity = f"{metadata['device']}:{metadata['start_seq']}:{digest}"` with:

```python
        if metadata.get("source") == "meeting":
            identity = f"meeting:{metadata['capture_id']}:{digest}"
        else:
            identity = f"{metadata['device']}:{metadata['start_seq']}:{digest}"
```

In `src/second_brain/runtime.py`:

1. Change `from .io import InstanceLock, write_json` to `from .io import InstanceLock, utc_from_ms, write_json`.

2. Replace `discover` with:

```python
    def receipts(self):
        yield from (self.cfg.incoming_dir / ".ready").glob("*.json")
        if self.cfg.meetings_enabled:
            yield from (self.cfg.incoming_dir / "meetings" / ".ready").glob("*.json")

    def discover(self):
        self.last_scan_error = None
        for receipt in self.receipts():
            try:
                value = read_json(receipt)
                audio = Path(value["audio"]).resolve()
                if not audio.is_relative_to(self.cfg.incoming_dir.resolve()):
                    raise ValueError("Receipt path escapes incoming directory")
                metadata = value["metadata"]
                if metadata.get("source") == "meeting":
                    # Speaker review, routing and notes read the start time from first_utc.
                    metadata = {**metadata, "first_utc": utc_from_ms(metadata["start_ms"])}
                if not self.queue.known(audio):
                    self.queue.enqueue(audio, metadata)
            except (OSError, ValueError, KeyError) as e:
                self.last_scan_error = f"Recording discovery failed ({type(e).__name__}); check private service log"
                log.exception("Could not discover recording receipt %s", receipt.name)
```

3. In `tests/test_meeting_receiver.py`, rename `test_service_accepts_meeting_upload_but_does_not_enqueue_it` to `test_meeting_upload_is_stored_but_not_queued_while_meetings_are_disabled`. Replace its last comment `# Plan 2 adds meeting processing` with `# meetings_enabled defaults to False`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings tests.test_meeting_receiver -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/queue.py src/second_brain/runtime.py tests/test_meetings.py tests/test_meeting_receiver.py
git commit -m "feat(meetings): queue meeting receipts when meetings are enabled"
```

---

### Task 8: Process meeting jobs

**Files:**
- Modify: `src/second_brain/runtime.py` (`process`)
- Test: `tests/test_meetings.py`

- [ ] **Step 1: Write the failing end-to-end test**

Append to `tests/test_meetings.py`:

```python
class MeetingEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from second_brain.runtime import Runtime
        from tests.helpers import configuration
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        base = configuration(self.root)
        pipe = json.loads(base.pipeline_config.read_text())
        pipe["moss_command"] = [sys.executable, str(ROOT / "tests/fixtures/fake_moss_tones.py")]
        base.pipeline_config.write_text(json.dumps(pipe))
        self.cfg = dataclasses.replace(base, meetings_enabled=True, owner_name="Eugene")
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

    async def wait_complete(self):
        for _ in range(600):
            counts = self.runtime.queue.snapshot()["counts"]
            if counts.get("complete") or counts.get("failed"):
                break
            await asyncio.sleep(0.05)
        logs = "\n".join(p.read_text()[-3000:] for p in (self.root / "data/jobs").rglob("pipeline.log"))
        self.assertEqual(counts.get("complete"), 1, str(self.runtime.queue.snapshot()) + logs)

    async def test_uploaded_meeting_becomes_one_attributed_note(self):
        from tests.helpers import upload_file
        from omi_local import upload_protocol as U
        capture = make_capture(self.root / "upload.ogg").read_bytes()
        cid = bytes.fromhex("cd" * 16)
        meta = {"app": "us.zoom.xos", "start_ms": 1759761000000, "end_ms": 1759761010000,
                "channels": {"L": "mic", "R": "remote"}}
        self.assertEqual(await upload_file(self.runtime.server.bound_port, cid, capture, meta), U.MSG_FILE_BYE)
        await self.wait_complete()
        notes = list((self.cfg.vault_path / "Omi" / "Meetings").glob("*.md"))
        self.assertEqual(len(notes), 1)
        text = notes[0].read_text()
        self.assertIn(f"Eugene (mic): {OWNER_TEXT}", text)
        self.assertEqual(text.count(REMOTE_TEXT), 1)  # the mic's bleed copy is not attributed to Eugene
        self.assertIn("r0000:S1", text)
        self.assertIn("decision", text.split("## Transcript")[0])
        self.assertEqual(list((self.cfg.vault_path / "Omi" / "Conversations").glob("*.md")), [])
        # Meeting audio is retained for Omi dedupe; job scratch audio is not.
        self.assertTrue((self.cfg.incoming_dir / "meetings" / ("cd" * 16 + ".caf")).exists())
        self.assertEqual(list((self.root / "data/jobs").rglob("*.wav")), [])
        # Crash replay republishes from the saved manifest without another ASR run.
        with self.runtime.queue.connect() as db:
            db.execute("UPDATE jobs SET state='processing'")
        self.runtime.queue.recover()
        await self.wait_complete()
        self.assertEqual(notes[0].read_text(), text)
        self.assertEqual(len(list((self.root / "data/jobs").rglob("pipeline.log"))), 1)
```

Remote speakers are offered for review. That is covered by `MeetingPipelineTests.test_owner_is_not_offered_for_speaker_review` and `MeetingSpeakerTests`.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings.MeetingEndToEndTests -v`
Expected: FAIL. The pipeline runs in Omi mode and publishes to `Omi/Conversations`, or `publish` raises `KeyError: 'device'`.

- [ ] **Step 3: Implement**

In `src/second_brain/runtime.py`:

1. Change `from .vault import publish` to `from .vault import publish, publish_meeting`.

2. In `process`, after `manifest_path = root / "manifest.json"`, add:

```python
        metadata = json.loads(job["metadata"])
        meeting = metadata.get("source") == "meeting"
```

3. After the `if cfg.no_hermes: cmd.append("--no-hermes")` lines, add:

```python
            if meeting:
                cmd.append("--meeting")
```

4. Replace `write_json(publication, self.speakers.render(job["id"], manifest))` with:

```python
            write_json(publication, self.speakers.render(job["id"], manifest, cfg.owner_name if meeting else None))
```

5. Replace the `notes = await asyncio.to_thread(publish, ...)` statement with:

```python
        if meeting:
            notes = await asyncio.to_thread(publish_meeting, cfg.vault_path, cfg.meetings_vault_folder, job, manifest)
        else:
            notes = await asyncio.to_thread(
                publish, cfg.vault_path, cfg.vault_folder, job, manifest, not cfg.delete_audio_after_processing
            )
```

6. In the router block, replace `json.loads(job["metadata"]).get("first_utc")` with `metadata.get("first_utc")`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings -v`
Expected: all PASS.
Run: `python scripts/test.py`
Expected: both Python suites `OK`.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/runtime.py tests/test_meetings.py
git commit -m "feat(meetings): process meeting captures into vault notes"
```

---

### Task 9: Preflight check for Opus CAF decoding

On Windows, meetings fail at ffmpeg if its build cannot read Opus inside CAF. ffmpeg 9.0 on macOS can; the Windows build at `C:\ffmpeg\bin` is unverified. Make the service tell the owner at install time rather than failing jobs later.

**Files:**
- Modify: `src/second_brain/preflight.py`
- Test: `tests/test_meetings.py`

`check(cfg)` collects strings in `errors` and returns `{"ok": not errors, "errors": errors}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_meetings.py`:

```python
class MeetingPreflightTests(unittest.TestCase):
    def test_ffmpeg_with_opus_passes(self):
        from second_brain.preflight import meeting_decoder_problem
        self.assertIsNone(meeting_decoder_problem("ffmpeg"))

    def test_missing_ffmpeg_is_reported(self):
        from second_brain.preflight import meeting_decoder_problem
        problem = meeting_decoder_problem(str(Path(tempfile.gettempdir()) / "no-such-ffmpeg"))
        self.assertIn("meeting captures cannot be decoded", problem)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings.MeetingPreflightTests -v`
Expected: FAIL with `ImportError: cannot import name 'meeting_decoder_problem'`.

- [ ] **Step 3: Implement**

In `src/second_brain/preflight.py`, add `import subprocess` to the imports and this function above `check`:

```python
def meeting_decoder_problem(ffmpeg="ffmpeg"):
    """None if ffmpeg lists an Opus decoder (the Mac app's CAF codec), else a short problem."""
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-decoders"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "ffmpeg is not runnable; meeting captures cannot be decoded"
    names = {line.split()[1] for line in proc.stdout.splitlines() if len(line.split()) > 1}
    if proc.returncode or not names & {"opus", "libopus"}:
        return "ffmpeg has no Opus decoder; meeting captures cannot be decoded"
    return None
```

In `check`, directly after the `for name in ["ffmpeg", "ffprobe"]:` loop, add:

```python
    if cfg.meetings_enabled:
        search = cfg.ffmpeg_dir + os.pathsep + os.environ.get("PATH", "") if cfg.ffmpeg_dir else None
        ffmpeg = shutil.which("ffmpeg", path=search)
        if ffmpeg and (problem := meeting_decoder_problem(ffmpeg)):
            errors.append(problem)
```

This checks for a decoder, not the CAF demuxer. The real-file check on Windows is listed in Task 11.

- [ ] **Step 4: Run tests and commit**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python -m unittest tests.test_meetings -v` → all PASS.

```bash
git add src/second_brain/preflight.py tests/test_meetings.py
git commit -m "feat(meetings): preflight checks ffmpeg can decode meeting Opus"
```

---

### Task 10: Documentation

**Files:**
- Modify: `config/service.example.json`, `README.md`, `SYSTEM.md`, `src/second_brain/ARCHITECTURE.md`, `docs/superpowers/specs/2026-09-29-meeting-capture-design.md`

- [ ] **Step 1: Example configuration**

In `config/service.example.json`, add after `"delete_audio_after_processing": true,`:

```json
  "meetings_enabled": false,
  "meetings_vault_folder": "Omi/Meetings",
  "owner_name": "Me",
```

- [ ] **Step 2: README**

In `README.md`, find the service configuration key descriptions (`grep -n "delete_audio_after_processing" README.md`) and add:

```markdown
- `meetings_enabled` (false): process Mac meeting captures from `incoming/meetings/` into one note
  each under `meetings_vault_folder` (default `Omi/Meetings`). The capture's microphone channel is
  attributed to `owner_name`; the meeting app's channel is diarized and voice-matched like Omi
  speakers. Meetings are never dropped by the memory gate, and meeting audio is retained.
  Captures uploaded before enabling are processed on the next scan.
```

- [ ] **Step 3: SYSTEM.md**

In `SYSTEM.md` item 6 (around line 129), replace `The pipeline does not yet process these receipts.` with:

```markdown
With `meetings_enabled`, discovery queues these receipts as ordinary jobs. The pipeline's
`--meeting` mode transcribes L (mic) and R (meeting app) separately. A mic segment is kept as
`owner_name` only where the mic is ≥ 6 dB louder than R in the 300–3400 Hz band; the rest is remote
bleed, kept only in the manifest (`meeting.bleed_dropped`). R is diarized and voice-matched.
Windows are scored as usual and the gate still decides routing, but every window is published in
one meeting note (highlights first). 7B refinement is off for meetings because it works on a mono
mix. Omi recordings overlapping a meeting are not yet deduplicated (Plan 2b).
```

In the storage table, change the `incoming/meetings/.ready/` row to say `v2 commit receipts; queued when meetings_enabled`.

- [ ] **Step 4: ARCHITECTURE.md**

In `src/second_brain/ARCHITECTURE.md` step 2, replace `which Omi discovery does not scan; processing is added separately.` with `scanned only when meetings_enabled; meeting jobs run the pipeline with --meeting and publish one note per capture.`

- [ ] **Step 5: Spec status**

In `docs/superpowers/specs/2026-09-29-meeting-capture-design.md`, change `Status: approved design, not yet implemented.` to:

```markdown
Status: approved design. Implemented: §1 Mac app, §2 receiver v2, §3 meeting jobs
(plan `docs/superpowers/plans/2026-10-06-meeting-notes.md`; owner named by `owner_name`, 7B
refinement off for meetings). Not yet: §3 Omi dedupe and §4 tray meeting counts (Plan 2b).
```

- [ ] **Step 6: Verify and commit**

Run: `python scripts/test.py` → both Python suites `OK`.

```bash
git add config/service.example.json README.md SYSTEM.md src/second_brain/ARCHITECTURE.md docs/superpowers/specs/2026-09-29-meeting-capture-design.md
git commit -m "docs(meetings): meeting notes behind meetings_enabled"
```

---

### Task 11: Final verification and handoff

- [ ] **Step 1: Full suite**

Run: `python scripts/test.py > /tmp/test.log 2>&1; grep -E "^Ran|^OK|FAILED" /tmp/test.log`
Expected: two `OK` lines. The Swift suite needs a non-sandboxed shell. If it can't run, say so.

- [ ] **Step 2: Report what was not verified**

State in the handoff:
- Real ASR quality on meeting audio and real voice matching of remote speakers were not exercised (fixtures only).
- The owner's Windows ffmpeg decoding a real Mac CAF. Preflight only proves an Opus decoder exists. On the Windows box run
  `C:\ffmpeg\bin\ffprobe -v error -show_entries stream=codec_name,channels -of compact <a file in incoming\meetings>.caf`
  and expect `codec_name=opus|channels=2`.
- Enabling requires setting `"meetings_enabled": true` and `"owner_name"` in `C:\ProgramData\SecondBrain\config\service.json`, then reinstalling with `windows/install.ps1`. Deployment is a separate, explicitly requested step.
