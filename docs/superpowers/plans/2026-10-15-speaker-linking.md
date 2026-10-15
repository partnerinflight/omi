# Recording-wide Speaker Linking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each person one speaker label across a whole recording. MOSS keeps its text and its within-chunk speaker turns. Nemotron-3-Diarization, run once over the recording, links MOSS's chunk-local labels (`c0000:S1`, `c0001:S2`, …) into recording-wide labels (`spk1`, `spk2`, …).

**Architecture:** After MOSS transcription and before meeting dedupe, the pipeline runs the NeMo-Speech.cpp CLI (`nemo-speech diarize … --format rttm`) on a 16 kHz mono copy of the audio. For meeting captures it uses the remote (R) channel. A pure function, `link_speakers`, maps each MOSS label to the Nemotron speaker it overlaps most. Two exceptions keep a label's own identity: two labels MOSS kept apart inside one chunk never merge, and a label whose best overlap covers less than 30% of its speech is not linked. Both cases get a new recording-wide label. Speaker linking is off unless the pipeline config names the CLI and model, and any failure keeps today's chunk-local labels.

**Tech Stack:** Python 3.12 stdlib, ffmpeg, NeMo-Speech.cpp 0.2.0 (C++/ggml, CPU), Nemotron-3-Diarization q8_0 GGUF, unittest, PowerShell for Windows setup.

## Evidence this plan rests on (2026-10-15, `~/diar-eval` on the owner's Mac, outside the repo)

- **AMI test set:** 4 meetings (ES2004a, IS1009a, TS3003a, EN2002a), 92 min, about 15,700 human-transcribed words, 4 speakers each. All numbers are speaker-attributed WER (cpWER, meeteval) over the whole recording, with fillers and punctuation normalised away.

  | System | cpWER | Labels for 4 real people |
  |---|---|---|
  | MOSS today | not scorable | 15–36 |
  | Nemotron ASR + Nemotron diarization | 41.5% | 4 |
  | MOSS + linking, NeMo PyTorch reference runtime | 37.5% | 4 |
  | MOSS + linking, NeMo-Speech.cpp CPU, naive best overlap | 37.61% | — |
  | **MOSS + linking, NeMo-Speech.cpp CPU, never-merge + 30% overlap (this plan)** | **37.82%** | 4 for ES2004a and EN2002a; 6–7 for IS1009a and TS3003a |

- **MOSS within each 4-minute chunk:** 37.1%. Linking therefore costs about 0.7 points of attribution error.
- **The owner's ~10-person Zoom call:** MOSS produced 38 labels. Nemotron is capped at 8 speakers. Naive linking merged MOSS labels from the same chunk in 7 places, which is what the never-merge rule prevents. With the rule, the call yields 18 speakers.
- **NeMo-Speech.cpp `cpu-diar` build on the M4 Pro:** about 10% of real time (106 s for 17.5 min). It found the correct 4 speakers in every AMI meeting.
- **Pinned revisions:**
  - NeMo-Speech.cpp: `083861c241061562b8ec29eb06d7f6cdc3af8775`.
  - Model: `nvidia/Nemotron-3-Diarization` revision `f667ed73aee57d40cc39428eb768b4fd87a0a29e`, file `Nemotron-3-Diarization.q8_0.gguf`, 107,012,128 bytes, SHA-256 `08456d9e22cd9a323c0364d98375f3746d6e68507ebb705cd46438c534c7a3a1`.

## Known limits, to be documented in Task 6

- **Refined windows keep their own labels.** Windows refined by VibeVoice 7B carry VibeVoice's window-local speaker labels, because refined segments replace MOSS's.
- **More than 8 people.** On calls with more than 8 people, some people may still appear under two labels across chunks. The never-merge rule only stops two people sharing one label.
- **Not measured on Omi audio.** Linking was not measured on Omi recordings (chest mic, car noise). There is no answer key for them.
- **Windows build unverified.** The Windows CPU build of NeMo-Speech.cpp was not run; Task 5 lists the manual check.

---

## File map

| File | Responsibility |
|---|---|
| `src/second_brain/adaptive/linking.py` (create) | `parse_rttm`, `link_speakers`: pure, no I/O |
| `src/second_brain/adaptive/pipeline.py` (modify) | `link_recording_speakers()`: run the CLI, relabel segments, record the report or a fallback |
| `tests/fixtures/fake_nemo_speech.py` (create) | Deterministic stand-in for the `nemo-speech diarize` CLI |
| `tests/test_speaker_linking.py` (create) | Unit tests for linking; pipeline integration (Omi, meeting, failure) |
| `config/pipeline.example.json` (modify) | `speaker_linking_command`, `speaker_linking_model` (both `null`) |
| `config/engines.lock.json`, `windows/download-model.py`, `windows/build-nemo-speech.ps1` (create), `windows/setup-engines.ps1`, `windows/setup.ps1` (modify) | Pinned Windows build and model download, opt-in with `-SpeakerLinking` |
| `SYSTEM.md`, `README.md`, `src/second_brain/ARCHITECTURE.md` (modify) | Docs move with code |

Run from the repo root:
- Single module: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_speaker_linking -v`.
- Full suite: `python3 scripts/test.py`. Its trailing `swift test` step fails in a sandboxed shell (pre-existing); both Python suites must print `OK`.

---

### Task 1: Linking rules

**Files:**
- Create: `src/second_brain/adaptive/linking.py`
- Test: `tests/test_speaker_linking.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_speaker_linking.py`:

```python
"""MOSS chunk-local speaker labels become recording-wide speakers via Nemotron turns."""

from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def seg(start, end, speaker, text="words"):
    return {"start": start, "end": end, "speaker": speaker, "text": text}


class ParseRttmTests(unittest.TestCase):
    def test_reads_speaker_lines_and_skips_everything_else(self):
        from second_brain.adaptive.linking import parse_rttm
        text = ("SPEAKER rec 1 1.500 2.000 <NA> <NA> speaker_0 <NA> <NA>\n"
                "# a comment\n\n"
                "SPEAKER rec 1 4.000 0.000 <NA> <NA> speaker_1 <NA> <NA>\n"   # zero length
                "SPEAKER rec.Mix-Headset 1 5.000 1.250 <NA> <NA> speaker_1 <NA> <NA>\n")
        self.assertEqual(parse_rttm(text), [(1.5, 3.5, "speaker_0"), (5.0, 6.25, "speaker_1")])


class LinkSpeakersTests(unittest.TestCase):
    def link(self, segments, turns, **kw):
        from second_brain.adaptive.linking import link_speakers
        return link_speakers(segments, turns, **kw)

    def test_the_same_voice_in_different_chunks_becomes_one_speaker(self):
        segments = [seg(0, 4, "c0000:S1"), seg(4, 8, "c0000:S2"),
                    seg(240, 244, "c0001:S1"), seg(244, 248, "c0001:S2")]
        # MOSS numbers speakers per chunk by appearance; here chunk 1 happens to swap them.
        turns = [(0, 4, "a"), (4, 8, "b"), (240, 244, "b"), (244, 248, "a")]
        mapping, report = self.link(segments, turns)
        self.assertEqual(mapping, {"c0000:S1": "spk1", "c0000:S2": "spk2",
                                   "c0001:S1": "spk2", "c0001:S2": "spk1"})
        self.assertEqual(report, {"labels": 4, "linked": 4, "kept_apart": 0, "unlinked": 0, "speakers": 2})

    def test_two_voices_moss_separated_in_one_chunk_never_merge(self):
        # Nemotron (capped at 8 speakers) puts both onto "a"; MOSS heard two people.
        segments = [seg(0, 6, "c0000:S1"), seg(6, 8, "c0000:S2")]
        turns = [(0, 8, "a")]
        mapping, report = self.link(segments, turns)
        self.assertNotEqual(mapping["c0000:S1"], mapping["c0000:S2"])
        self.assertEqual(mapping["c0000:S1"], "spk1")           # the stronger claim keeps the link
        self.assertEqual(report["kept_apart"], 1)

    def test_a_label_with_weak_evidence_keeps_its_own_identity(self):
        segments = [seg(0, 10, "c0000:S1")]
        turns = [(0, 2, "a")]                                    # 20% of the label's speech
        mapping, report = self.link(segments, turns)
        self.assertEqual(report, {"labels": 1, "linked": 0, "kept_apart": 0, "unlinked": 1, "speakers": 1})
        self.assertEqual(mapping, {"c0000:S1": "spk1"})
        mapping, report = self.link(segments, turns, min_share=0.1)
        self.assertEqual(report["linked"], 1)

    def test_owner_and_unknown_labels_are_left_alone(self):
        segments = [seg(0, 2, "owner"), seg(2, 4, "c0000:S?"), seg(4, 6, "c0000:S1")]
        mapping, _ = self.link(segments, [(0, 6, "a")])
        self.assertEqual(mapping, {"c0000:S1": "spk1"})

    def test_global_names_follow_first_appearance_and_are_deterministic(self):
        segments = [seg(10, 12, "c0000:S2"), seg(0, 2, "c0000:S1"), seg(300, 302, "c0001:S1")]
        turns = [(0, 2, "x"), (10, 12, "y"), (300, 302, "z")]
        first, _ = self.link(segments, turns)
        again, _ = self.link(list(reversed(segments)), list(reversed(turns)))
        self.assertEqual(first, {"c0000:S1": "spk1", "c0000:S2": "spk2", "c0001:S1": "spk3"})
        self.assertEqual(first, again)

    def test_no_turns_means_every_label_stays_its_own_speaker(self):
        segments = [seg(0, 2, "c0000:S1"), seg(240, 242, "c0001:S1")]
        mapping, report = self.link(segments, [])
        self.assertEqual(mapping, {"c0000:S1": "spk1", "c0001:S1": "spk2"})
        self.assertEqual(report["unlinked"], 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_speaker_linking -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'second_brain.adaptive.linking'`.

- [ ] **Step 3: Implement**

Create `src/second_brain/adaptive/linking.py`:

```python
"""Recording-wide speakers for MOSS transcripts (docs/superpowers/plans/2026-10-15-speaker-linking.md).

MOSS diarizes each 4-minute chunk on its own, so one person gets a new label in every chunk.
Nemotron-3-Diarization tracks up to 8 speakers across a whole recording. Each MOSS label joins
the Nemotron speaker it overlaps most, with two guards measured on AMI and the owner's 10-person
call: labels MOSS separated inside one chunk never merge (Nemotron's 8-speaker cap otherwise
forces it to), and a label whose best overlap covers less than MIN_SHARE of its speech keeps its
own identity."""

from __future__ import annotations

MIN_SHARE = 0.3


def parse_rttm(text):
    """(start, end, speaker) turns from RTTM SPEAKER lines; zero-length turns are dropped."""
    turns = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[0] == "SPEAKER":
            start, length = float(fields[3]), float(fields[4])
            if length > 0:
                turns.append((start, start + length, fields[7]))
    return turns


def _linkable(label):
    return label != "owner" and ":" in label and not label.endswith("S?")


def link_speakers(segments, turns, min_share=MIN_SHARE):
    """Map each chunk-local MOSS label to a recording-wide label `spk<n>`.

    Returns (mapping, report). Labels that are not linkable (the meeting owner, unknown `S?`
    turns) are absent from the mapping and must be left unchanged."""
    speech, overlap, chunk_of, first = {}, {}, {}, {}
    for segment in segments:
        label = str(segment["speaker"])
        if not _linkable(label):
            continue
        start, end = float(segment["start"]), float(segment["end"])
        speech[label] = speech.get(label, 0.0) + max(0.0, end - start)
        chunk_of[label] = label.split(":")[0]
        first[label] = min(first.get(label, start), start)
        for turn_start, turn_end, speaker in turns:
            shared = min(turn_end, end) - max(turn_start, start)
            if shared > 0:
                votes = overlap.setdefault(label, {})
                votes[speaker] = votes.get(speaker, 0.0) + shared
    claims = {}
    for label, votes in overlap.items():
        speaker = max(sorted(votes), key=votes.get)          # sorted: ties resolve deterministically
        if speech[label] > 0 and votes[speaker] / speech[label] >= min_share:
            claims[label] = (speaker, votes[speaker])
    strongest = {}
    for label in sorted(claims):
        key = (chunk_of[label], claims[label][0])
        if key not in strongest or claims[label][1] > claims[strongest[key]][1]:
            strongest[key] = label
    linked = set(strongest.values())
    order = sorted(speech, key=lambda label: (first[label], label))
    names, mapping = {}, {}
    for label in order:
        if label in linked:
            speaker = claims[label][0]
            names.setdefault(speaker, f"spk{len(names) + 1}")
            mapping[label] = names[speaker]
    count = len(names)
    for label in order:
        if label not in mapping:
            count += 1
            mapping[label] = f"spk{count}"
    report = {"labels": len(speech), "linked": len(linked), "kept_apart": len(claims) - len(linked),
              "unlinked": len(speech) - len(claims), "speakers": len(set(mapping.values()))}
    return mapping, report
```

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_speaker_linking -v`
Expected: 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/adaptive/linking.py tests/test_speaker_linking.py
git commit -m "feat(speakers): link MOSS chunk labels into recording-wide speakers"
```

---

### Task 2: Pipeline integration for Omi recordings

**Files:**
- Create: `tests/fixtures/fake_nemo_speech.py`
- Modify: `src/second_brain/adaptive/pipeline.py`, `config/pipeline.example.json`
- Test: `tests/test_speaker_linking.py`

- [ ] **Step 1: Create the CLI fixture**

Create `tests/fixtures/fake_nemo_speech.py`. It is a deterministic contract fixture and is never installed as production configuration:

```python
"""Deterministic stand-in for `nemo-speech diarize INPUT --model M --format rttm --output OUT`,
never used in production configuration.

Usage as a speaker_linking_command prefix: [python, fake_nemo_speech.py, "--block", "<seconds>"].
Each block's first half is speaker_a and second half speaker_b, matching tests/fixtures/fake_moss.py,
which splits every MOSS chunk into an S1 half and an S2 half. Records what it was asked to read in
OUT.input.json so tests can check the audio it received."""

import json
import sys
import wave

args = sys.argv[1:]
block = float(args[args.index("--block") + 1]) if "--block" in args else 1e9
command = args[args.index("diarize"):]
audio, out = command[1], command[command.index("--output") + 1]
if command[command.index("--format") + 1] != "rttm" or "--model" not in command:
    raise SystemExit(2)
with wave.open(audio) as source:
    duration = source.getnframes() / source.getframerate()
    channels, rate = source.getnchannels(), source.getframerate()
lines, start = [], 0.0
while start < duration:
    middle, end = min(duration, start + block / 2), min(duration, start + block)
    lines.append(f"SPEAKER rec 1 {start:.3f} {middle - start:.3f} <NA> <NA> speaker_a <NA> <NA>")
    if end > middle:
        lines.append(f"SPEAKER rec 1 {middle:.3f} {end - middle:.3f} <NA> <NA> speaker_b <NA> <NA>")
    start = end
open(out, "w").write("\n".join(lines) + "\n")
json.dump({"audio": audio, "channels": channels, "rate": rate, "duration": duration}, open(out + ".input.json", "w"))
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_speaker_linking.py`, above `if __name__`:

```python
def sine_wav(path: Path, seconds: float, channels: int = 1) -> Path:
    spec = "|".join(["0.3*sin(2*PI*700*t)"] * channels)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"aevalsrc='{spec}':s=16000:d={seconds}", "-c:a", "pcm_s16le", str(path)], check=True)
    return path


def pipeline_config(root: Path, **extra) -> Path:
    cfg = json.loads((ROOT / "config/pipeline.example.json").read_text())
    cfg.update(moss_command=[sys.executable, str(ROOT / "tests/fixtures/fake_moss.py")],
               scan_vault_for_novelty=False, long_silence_seconds=999, memory_gate_min_words=1,
               work_root=str(root / "work"), **extra)
    path = root / "pipeline.json"
    path.write_text(json.dumps(cfg))
    return path


def run_pipeline(root: Path, audio: Path, out: Path, config: Path, *extra) -> dict:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
    proc = subprocess.run([sys.executable, "-m", "second_brain.adaptive.pipeline", "--audio", str(audio),
                           "--config", str(config), "--output-dir", str(out), "--no-hermes", "--skip-vibe7", *extra],
                          capture_output=True, text=True, env=env)
    if proc.returncode:
        raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
    return json.loads((out / "manifest.json").read_text())


FAKE = [sys.executable, str(ROOT / "tests/fixtures/fake_nemo_speech.py")]


class PipelineLinkingTests(unittest.TestCase):
    """Three 2 s MOSS chunks (coarse_max_seconds=2): fake MOSS labels each half S1/S2 per chunk,
    so without linking there are six labels; the fake diarizer says it is two people."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.audio = sine_wav(cls.root / "omi.wav", 6)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def segments(self, manifest):
        return [s for w in manifest["windows"] for s in w["moss_segments"]]

    def test_chunk_labels_become_two_recording_wide_speakers(self):
        config = pipeline_config(self.root, coarse_max_seconds=2,
                                 speaker_linking_command=FAKE + ["--block", "2"],
                                 speaker_linking_model=str(self.root / "model.gguf"))
        manifest = run_pipeline(self.root, self.audio, self.root / "linked", config)
        labels = {s["speaker"] for s in self.segments(manifest)}
        self.assertEqual(labels, {"spk1", "spk2"})
        self.assertEqual({s["chunk_speaker"] for s in self.segments(manifest)},
                         {f"c000{i}:S{j}" for i in range(3) for j in (1, 2)})
        self.assertEqual(manifest["speaker_linking"],
                         {"labels": 6, "linked": 6, "kept_apart": 0, "unlinked": 0, "speakers": 2})
        received = json.loads((self.root / "linked/linking.rttm.input.json").read_text())
        self.assertEqual((received["channels"], received["rate"]), (1, 16000))
        self.assertAlmostEqual(received["duration"], 6, delta=0.1)
        # The archive carries the linked labels too, with the original kept alongside.
        archive = json.loads((self.root / "linked/moss_all_segments.json").read_text())
        self.assertEqual({s["speaker"] for s in archive}, {"spk1", "spk2"})

    def test_linking_is_off_unless_configured(self):
        manifest = run_pipeline(self.root, self.audio, self.root / "plain",
                                pipeline_config(self.root, coarse_max_seconds=2))
        self.assertNotIn("speaker_linking", manifest)
        self.assertEqual({s["speaker"] for s in self.segments(manifest)},
                         {f"c000{i}:S{j}" for i in range(3) for j in (1, 2)})

    def test_a_failing_diarizer_keeps_chunk_labels_and_records_a_fallback(self):
        config = pipeline_config(self.root, coarse_max_seconds=2,
                                 speaker_linking_command=[sys.executable, "-c", "import sys; sys.exit(3)"],
                                 speaker_linking_model=str(self.root / "model.gguf"))
        manifest = run_pipeline(self.root, self.audio, self.root / "failed", config)
        self.assertNotIn("speaker_linking", manifest)
        self.assertIn({"from": "speaker linking", "to": "chunk-local labels", "reason": "RuntimeError"},
                      manifest["fallbacks"])
        self.assertEqual(len({s["speaker"] for s in self.segments(manifest)}), 6)
```

Check `tests/fixtures/fake_moss.py` before relying on the six labels. It must emit `S1` for the first half and `S2` for the second half of every chunk. Also check `pipeline.split_spans` with `coarse_max_seconds=2` over 6 s of audio and `long_silence_seconds=999`: it must yield exactly three chunks. If either differs, adjust the expected label set to what the code really does and report it. Never loosen the linking assertions.

- [ ] **Step 3: Run to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_speaker_linking.PipelineLinkingTests -v`
Expected: two FAIL (no linking happens, `KeyError: 'speaker_linking'` / six labels). `test_linking_is_off_unless_configured` passes already.

- [ ] **Step 4: Implement**

1. In `config/pipeline.example.json`, add after `"speaker_timeout_seconds": 300,`:

```json
  "speaker_linking_command": null,
  "speaker_linking_model": null,
```

2. In `src/second_brain/adaptive/pipeline.py`, above `def main():`, add:

```python
def link_recording_speakers(audio, segments, cfg, run_dir, manifest, channel=None):
    """Relabel MOSS chunk-local speakers with recording-wide ones from Nemotron-3-Diarization
    (NeMo-Speech.cpp CLI). Off unless both config keys are set; any failure keeps the chunk labels."""
    from second_brain.adaptive.linking import link_speakers, parse_rttm

    command, model = cfg.get("speaker_linking_command"), cfg.get("speaker_linking_model")
    if not command or not model:
        return segments
    print("=== Speaker linking across chunks ===", flush=True)
    try:
        source = run_dir / "linking-input.wav"
        extract_wav(audio, 0.0, ffprobe_duration(audio), 16000, source, channel)
        rttm = run_dir / "linking.rttm"
        run([*command, "diarize", str(source), "--model", str(model), "--format", "rttm", "--output", str(rttm)])
        mapping, report = link_speakers(segments, parse_rttm(rttm.read_text(encoding="utf-8")))
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        manifest["fallbacks"].append({"from": "speaker linking", "to": "chunk-local labels",
                                      "reason": type(error).__name__})
        print(f"[linking] failed ({type(error).__name__}); keeping chunk-local labels", flush=True)
        return segments
    for segment in segments:
        if segment["speaker"] in mapping:
            segment["chunk_speaker"] = segment["speaker"]
            segment["speaker"] = mapping[segment["speaker"]]
    manifest["speaker_linking"] = report
    print(f"[linking] {report['labels']} chunk labels -> {report['speakers']} speakers "
          f"({report['kept_apart']} kept apart, {report['unlinked']} unlinked)", flush=True)
    return segments
```

`run()` raises `RuntimeError` on a nonzero exit and `subprocess.TimeoutExpired` (a `SubprocessError`) on timeout. `extract_wav` and `ffprobe_duration` already exist in this module.

The `linking-input.wav` file is job scratch audio. The runtime's existing `purge_audio` deletes every audio file under the job directory once the note is published, so no new cleanup is needed. Confirm that by reading `Runtime.purge_audio` in `src/second_brain/runtime.py`.

3. In `main()`, insert the call directly after `all_segments.sort(key=lambda s: (s["start"], s["end"]))` and **before** the `moss_all_segments.json` write:

```python
    all_segments = link_recording_speakers(audio, all_segments, cfg, run_dir, manifest,
                                           "R" if args.meeting else None)
```

For a meeting, MOSS's remote segments are labelled `rNNNN:Sx`, and the diarizer runs on the remote channel only. The owner's `owner` label is never linked.

- [ ] **Step 5: Run to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_speaker_linking -v`
Expected: all PASS.
Run: `python3 scripts/test.py`
Expected: both Python suites `OK`. Existing pipeline tests leave the new keys `null`, so they are unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/second_brain/adaptive/pipeline.py config/pipeline.example.json tests/fixtures/fake_nemo_speech.py tests/test_speaker_linking.py
git commit -m "feat(speakers): link speakers across chunks with Nemotron diarization when configured"
```

---

### Task 3: Meeting captures

**Files:**
- Test: `tests/test_speaker_linking.py`

Task 2 already passes `"R"` for meetings. This task proves the meeting path end to end.

- [ ] **Step 1: Write the test**

Append to `tests/test_speaker_linking.py`:

```python
class MeetingLinkingTests(unittest.TestCase):
    def test_remote_channel_is_linked_and_the_owner_is_untouched(self):
        from tests.test_meetings import make_capture
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = make_capture(root / "capture.caf")          # L owner tone, R remote tone, 10 s
            cfg = json.loads((ROOT / "config/pipeline.example.json").read_text())
            cfg.update(moss_command=[sys.executable, str(ROOT / "tests/fixtures/fake_moss_tones.py")],
                       scan_vault_for_novelty=False, long_silence_seconds=999, memory_gate_min_words=1,
                       work_root=str(root / "work"), speaker_linking_command=FAKE,
                       speaker_linking_model=str(root / "model.gguf"))
            config = root / "pipeline.json"
            config.write_text(json.dumps(cfg))
            manifest = run_pipeline(root, capture, root / "run", config, "--meeting")
            segments = [s for w in manifest["windows"] for s in w["moss_segments"]]
            self.assertEqual({s["speaker"] for s in segments if s.get("channel") == "L"}, {"owner"})
            self.assertEqual({s["speaker"] for s in segments if s.get("channel") == "R"}, {"spk1"})
            received = json.loads((root / "run/linking.rttm.input.json").read_text())
            self.assertEqual(received["channels"], 1)
```

Read `tests/test_meetings.py` first. Use its `make_capture` exactly as defined, and check that the R segment really is labelled `r0000:S1` before linking (`fake_moss_tones.py` labels every segment `S1`). Without `--block`, the fake diarizer reports a single `speaker_a` for the whole file, so the one remote label must become `spk1`.

- [ ] **Step 2: Run, verify it passes, and commit**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_speaker_linking -v` → all PASS. If it fails, the implementation from Task 2 is wrong for meetings: fix the code, not the test, and report what was wrong.

```bash
git add tests/test_speaker_linking.py
git commit -m "test(speakers): meeting captures link the remote channel only"
```

---

### Task 4: Downstream labels stay correct

**Files:**
- Test: `tests/test_speaker_linking.py`

Speaker review and notes key observations by label. Linking must yield one observation per person per recording, with the original chunk label kept as provenance.

- [ ] **Step 1: Write the test**

Append to `tests/test_speaker_linking.py`:

```python
class DownstreamLabelTests(unittest.TestCase):
    def test_one_review_observation_per_linked_person(self):
        from second_brain.speaker_audio import candidates
        manifest = {"windows": [{"id": "w0000", "moss_segments": [
            {"start": 0.0, "end": 4.0, "speaker": "spk1", "chunk_speaker": "c0000:S1", "text": "one two three"},
            {"start": 240.0, "end": 244.0, "speaker": "spk1", "chunk_speaker": "c0001:S2", "text": "four five six"},
            {"start": 244.0, "end": 248.0, "speaker": "spk2", "chunk_speaker": "c0001:S1", "text": "seven eight"},
        ]}]}
        observations = {o["label"]: o for o in candidates(manifest)}
        self.assertEqual(set(observations), {"spk1", "spk2"})
        self.assertEqual(len(observations["spk1"]["clips"]), 2)    # clips from both chunks
```

Read `src/second_brain/speaker_audio.py` `candidates()` first. It reads `final_segments` if present, else `moss_segments`. Confirm that the clip-eligibility rules (minimum duration, text with words, no overlap) accept these segments. If they don't, change the fixture text or times, not the assertion.

- [ ] **Step 2: Run, verify it passes, and commit**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_speaker_linking -v` → all PASS.

```bash
git add tests/test_speaker_linking.py
git commit -m "test(speakers): linked labels give one review observation per person"
```

---

### Task 5: Windows build and model, opt-in

**Files:**
- Modify: `config/engines.lock.json`, `windows/download-model.py`, `windows/setup-engines.ps1`, `windows/setup.ps1`
- Create: `windows/build-nemo-speech.ps1`

None of this runs in CI. Keep `windows/test-setup.ps1` passing, and do not add network or build steps to it.

- [ ] **Step 1: Pin the engine and model**

In `config/engines.lock.json`, add two entries (keep valid JSON):

```json
  "nemo_speech": {
    "url": "https://github.com/NVIDIA/NeMo-Speech.cpp.git",
    "revision": "083861c241061562b8ec29eb06d7f6cdc3af8775"
  },
  "diar_model": {
    "repo": "nvidia/Nemotron-3-Diarization",
    "revision": "f667ed73aee57d40cc39428eb768b4fd87a0a29e",
    "patterns": ["Nemotron-3-Diarization.q8_0.gguf", "README.md"],
    "sha256": {"Nemotron-3-Diarization.q8_0.gguf": "08456d9e22cd9a323c0364d98375f3746d6e68507ebb705cd46438c534c7a3a1"}
  }
```

- [ ] **Step 2: Verify downloads by hash**

In `windows/download-model.py`:
- Change `choices=['moss_model', 'vibe_model', 'speaker_model']` to `choices=['moss_model', 'vibe_model', 'speaker_model', 'diar_model']`.
- Add `import hashlib`, and append after `snapshot_download(...)`:

```python
for name, expected in model.get('sha256', {}).items():
    with open(Path(a.destination) / name, 'rb') as artifact:
        actual = hashlib.file_digest(artifact, 'sha256').hexdigest()
    if actual != expected:
        raise ValueError(f'{name}: SHA-256 {actual} does not match the lock')
```

- [ ] **Step 3: Build script**

Create `windows/build-nemo-speech.ps1`:

```powershell
param(
    [string]$EngineRoot = 'C:\second-brain-asr-engines'
)
# CPU-only NeMo-Speech.cpp CLI for Nemotron-3-Diarization speaker linking (opt-in).
. "$PSScriptRoot/setup-common.ps1"
Require-Command cmake
$lock = Get-EngineLock
$source = Join-Path $EngineRoot ('sources/nemo-speech-' + $lock.nemo_speech.revision.Substring(0,12))
# The repository keeps test media in Git LFS; the build needs none of it.
$env:GIT_LFS_SKIP_SMUDGE = '1'
Get-PinnedSource $lock.nemo_speech.url $lock.nemo_speech.revision $source
$build = Join-Path $source 'build-cpu'
# The upstream driver sets up MSVC, Ninja and vcpkg (SentencePiece) itself.
Invoke-Checked powershell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    (Join-Path $source 'scripts/windows/build.ps1'), '-Backend', 'cpu', '-AsrOnly', '-BuildDir', $build)
$exe = Join-Path $build 'bin/nemo-speech.exe'
if (-not (Test-Path $exe)) { throw "NeMo-Speech build did not produce $exe" }
Invoke-Checked $exe @('--help')
```

- [ ] **Step 4: Wire it into setup, opt-in**

In `windows/setup-engines.ps1`:
- Add `[switch]$SpeakerLinking` to `param(...)`.
- Before `Save-PipelineConfig $Config $cfg`, add:

```powershell
if ($SpeakerLinking) {
    & "$PSScriptRoot/build-nemo-speech.ps1" -EngineRoot $EngineRoot
    $nemoExe = Join-Path $EngineRoot ('sources/nemo-speech-' + $lock.nemo_speech.revision.Substring(0,12) + '/build-cpu/bin/nemo-speech.exe')
    $diarModel = Join-Path $EngineRoot ('models/nemotron-diar-' + $lock.diar_model.revision.Substring(0,12))
    Invoke-Checked $setupPython @("$PSScriptRoot/download-model.py", '--lock', $lockPath, '--model', 'diar_model', '--destination', $diarModel)
    Set-ConfigValue $cfg 'speaker_linking_command' @($nemoExe)
    Set-ConfigValue $cfg 'speaker_linking_model' (Join-Path $diarModel 'Nemotron-3-Diarization.q8_0.gguf')
}
```

In `windows/setup.ps1`, add `[switch]$SpeakerLinking` to `param(...)` and pass `-SpeakerLinking:$SpeakerLinking` on the existing `setup-engines.ps1` call.

`Set-ConfigValue` with an array must produce a JSON list. Read `Set-ConfigValue` and `Save-PipelineConfig` in `windows/setup-common.ps1`. If a one-element array would be collapsed to a string by `ConvertTo-Json`, wrap it so it serialises as a list (for example `,@($nemoExe)`), and say what you did.

- [ ] **Step 5: Verify and commit**

Run `python3 -c "import json; json.load(open('config/engines.lock.json'))"` and `python3 scripts/test.py`; both Python suites must print `OK`. On a machine without PowerShell, say that the `.ps1` changes were not executed.

```bash
git add config/engines.lock.json windows/download-model.py windows/build-nemo-speech.ps1 windows/setup-engines.ps1 windows/setup.ps1
git commit -m "feat(windows): opt-in NeMo-Speech.cpp build and Nemotron model for speaker linking"
```

---

### Task 6: Documentation

**Files:**
- Modify: `SYSTEM.md`, `README.md`, `src/second_brain/ARCHITECTURE.md`

- [ ] **Step 1: SYSTEM.md**

Find the diarization lines (`grep -n "Speaker labels are scoped by chunk" SYSTEM.md`) and add, matching the surrounding density:

```markdown
- With `speaker_linking_command`/`speaker_linking_model` set (Windows: `setup.ps1 -SpeakerLinking`),
  Nemotron-3-Diarization (NeMo-Speech.cpp, CPU, about 10% of real time) runs once per recording
  on a 16 kHz mono copy, the remote channel for meetings. Each MOSS chunk label joins the
  Nemotron speaker it overlaps most and becomes `spk<n>` for the whole recording; the original
  label stays in `chunk_speaker`. Labels MOSS separated inside one chunk never merge (Nemotron
  tracks at most 8 speakers), and a label whose best overlap covers less than 30% of its speech
  keeps its own identity. The manifest's `speaker_linking` records labels/linked/kept_apart/
  unlinked/speakers; a diarizer failure keeps chunk labels and adds a `fallbacks` entry.
```

Add a dated known-limits entry (2026-10-15):

```markdown
- Speaker linking, known limits (2026-10-15): measured on AMI (cpWER 37.8% whole recording vs
  MOSS 37.1% within chunks) and one ~10-person call, never on Omi chest-mic audio. Windows
  refined by VibeVoice 7B keep VibeVoice's window-local labels. Calls with more than 8 people
  can still show one person under two labels. The Windows CPU build of NeMo-Speech.cpp was not
  run before release.
```

- [ ] **Step 2: README.md**

In the pipeline configuration section, document `speaker_linking_command` and `speaker_linking_model` (null = off), and the `-SpeakerLinking` setup switch.

- [ ] **Step 3: ARCHITECTURE.md**

In step 4 of "Receipt → job → note", add one sentence: after MOSS, optional speaker linking relabels chunk-local speakers as recording-wide ones before dedupe and windowing.

- [ ] **Step 4: Verify and commit**

Run `python3 scripts/test.py` → both Python suites `OK`.

```bash
git add SYSTEM.md README.md src/second_brain/ARCHITECTURE.md
git commit -m "docs(speakers): recording-wide speaker linking"
```

---

### Task 7: Final verification and handoff

- [ ] **Step 1:** Run `python3 scripts/test.py` twice. Both Python suites must print `OK` each time.
- [ ] **Step 2:** Re-run the evaluation that justified this plan against the shipped code:

```bash
cd ~/diar-eval && python3 - <<'EOF'
import json, sys
sys.path.insert(0, "/Users/genepol/code/omi/src")
from second_brain.adaptive.linking import link_speakers, parse_rttm
for stem in ("ES2004a", "IS1009a", "TS3003a", "EN2002a", "meeting1"):
    _, report = link_speakers(json.load(open(f"out/moss/{stem}.segments.json")),
                              parse_rttm(open(f"out/nemotron-cpp/{stem}.rttm").read()))
    print(stem, report)
EOF
```

Expected `speakers` per meeting: ES2004a 4, IS1009a 6, TS3003a 7, EN2002a 4, meeting1 18 (the prototype's numbers at `min_share` 0.3).

- [ ] **Step 3:** State in the handoff what was not verified:
  - the Windows build and setup script
  - real Omi recordings
  - VibeVoice-refined windows, which keep local labels
  - whether voice-matching accuracy improves with whole-recording observations (expected, not measured)
