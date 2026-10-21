# Lecture mode — service implementation plan (2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recordings the receiver tagged `"mode": "lecture"` become lecture jobs: transcribed verbatim with MOSS only, published whole as `<job>.md` in `lectures_vault_folder`, and summarized by Hermes into a linked `<job> Summary.md`. Lectures never reach the knowledge router or daily notes.

**Architecture:** A new `second_brain.kinds` module owns the job kind (`omi`, `meeting`, `lecture`). Discovery stores it in each job's metadata (`with_kind`); every reader calls `job_kind(metadata)`, which falls back to deriving the kind for jobs queued before kinds were stored. Lecture jobs run the pipeline with `--lecture` (MOSS tier forced, Hermes scoring off, meeting dedupe refused; windows and `final_transcript`/`final_segments` unchanged) and publish through `vault.publish_lecture`. When Hermes is usable, the runtime enqueues the job in a `SummaryQueue` (a `RouterQueue` in its own `data/lectures.sqlite3`, so it has the same backoff, retry and snapshot). A separate `summarize_lectures` loop, shaped like `route_published`, maps every section through `router.chat`, reduces the section summaries, freezes the result in `jobs/<id>/lecture-summary.json`, then writes the summary note. The queue snapshot and the tray's `JobInfo` carry `kind`, and lecture jobs show a badge.

**Tech Stack:** Python 3 stdlib, `unittest`, ffmpeg (already required by the suite), C#/.NET for the tray (Windows only). Spec: `docs/superpowers/specs/2026-10-21-lecture-mode-design.md` sections 3 and 4.

**Prerequisite:** Plan 1 (`2026-10-21-lecture-mode-receiver.md`) is merged on this branch. The end-to-end tests in Tasks 7 and 9 upload a real mode-marker record (`P.RECORD_MODE_MAGIC`) and rely on the receiver writing `"mode"` into the sidecar.

Run every command from the repo root with `PYTHONPATH=src:omi/firmware/scripts/omi-local` set (the value `scripts/test.py` uses), for example `export PYTHONPATH=src:omi/firmware/scripts/omi-local`. On Windows use `python` instead of `python3` and `;` as the separator. Full suite: `python3 scripts/test.py`.

---

### Task 1: The job kind helper

**Files:**
- Create: `src/second_brain/kinds.py`
- Create: `tests/test_lectures.py`

- [ ] **Step 1: Write the failing tests** — create `tests/test_lectures.py`:

```python
"""Lecture mode: Omi recordings tagged "lecture" become one transcript note and one summary note
(docs/superpowers/specs/2026-10-21-lecture-mode-design.md §3)."""

from __future__ import annotations
import asyncio
import dataclasses
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from second_brain.io import write_json
from tests.helpers import configuration

ROOT = Path(__file__).resolve().parents[1]


class KindTests(unittest.TestCase):
    def test_receipts_map_to_kinds(self):
        from second_brain.kinds import job_kind
        self.assertEqual(job_kind({}), "omi")                       # receivers before lecture mode
        self.assertEqual(job_kind({"mode": "normal"}), "omi")
        self.assertEqual(job_kind({"mode": "lecture"}), "lecture")
        self.assertEqual(job_kind({"mode": "podcast"}), "omi")      # an unknown mode is normal
        self.assertEqual(job_kind({"source": "meeting"}), "meeting")
        self.assertEqual(job_kind({"source": "meeting", "mode": "lecture"}), "meeting")

    def test_a_stored_kind_wins_and_an_invalid_one_is_ignored(self):
        from second_brain.kinds import job_kind
        self.assertEqual(job_kind({"kind": "lecture"}), "lecture")
        self.assertEqual(job_kind({"kind": "podcast", "mode": "lecture"}), "lecture")

    def test_with_kind_stores_the_derived_kind_and_never_trusts_a_sidecar_kind(self):
        from second_brain.kinds import with_kind
        sidecar = {"device": "d", "kind": "meeting", "mode": "lecture"}
        self.assertEqual(with_kind(sidecar), {"device": "d", "kind": "lecture", "mode": "lecture"})
        self.assertEqual(sidecar["kind"], "meeting", "input is not mutated")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_lectures -v`
Expected: 3 ERRORs — `ModuleNotFoundError: No module named 'second_brain.kinds'`.

- [ ] **Step 3: Implement** — create `src/second_brain/kinds.py`:

```python
"""Job kinds: `omi` (an ordinary Omi recording), `meeting` (a Mac meeting capture) or `lecture`
(an Omi recording made in lecture mode). Discovery stores the kind in each job's metadata;
jobs queued before kinds were stored derive it from their receipt fields."""

from __future__ import annotations

KINDS = ("omi", "meeting", "lecture")


def _from_receipt(metadata: dict) -> str:
    if metadata.get("source") == "meeting":
        return "meeting"
    # The receiver writes "mode" for Omi recordings; a missing or unknown mode is normal.
    return "lecture" if metadata.get("mode") == "lecture" else "omi"


def job_kind(metadata: dict) -> str:
    """The job's kind: the stored one, else derived the way discovery derives it."""
    kind = metadata.get("kind")
    return kind if kind in KINDS else _from_receipt(metadata)


def with_kind(metadata: dict) -> dict:
    """Receipt metadata with its kind stored. A kind already present in a sidecar is never trusted."""
    return {**metadata, "kind": _from_receipt(metadata)}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_lectures -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/kinds.py tests/test_lectures.py
git commit -m "feat(service): one job_kind helper for omi, meeting and lecture jobs"
```

---

### Task 2: Replace the scattered `source == "meeting"` checks

`speaker_audio.py:32` is not a metadata check (it skips segments on mic channel `L`, which only meeting captures have) and stays as is.

**Files:**
- Modify: `src/second_brain/runtime.py` (imports :14-18, `discover` :90-93, `dedupe_index_or_none` :179-187, `process` :195, :224-229, :274, :277)
- Modify: `src/second_brain/queue.py` (:47)
- Modify: `src/second_brain/meetings.py` (`recording_span` :28-31)
- Modify: `src/second_brain/speakers.py` (imports :13-14, `ingest` :148-149, :189)
- Modify: `src/second_brain/bridge.py` (imports :23-25, `published_windows` :38-42, `sources` :99-108)
- Modify: `tests/test_dedupe.py` (:769-770)
- Test: `tests/test_lectures.py`

- [ ] **Step 1: Write the failing tests** — add to `tests/test_lectures.py` before `if __name__`:

```python
class LectureKindWiringTests(unittest.TestCase):
    def setUp(self):
        from second_brain.runtime import Runtime
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = dataclasses.replace(configuration(self.root), meetings_enabled=True)
        self.runtime = Runtime(self.cfg)

    def receipt(self, name, **metadata):
        audio = self.cfg.incoming_dir / f"{name}.opus"
        audio.write_bytes(name.encode() * 10)
        write_json(self.cfg.incoming_dir / ".ready" / f"{name}.json", {"audio": str(audio), "metadata": {
            "device": "112233445566", "start_seq": len(name), "first_utc": "2026-10-21T15:00:00Z", **metadata}})

    def test_discovery_stores_each_jobs_kind(self):
        self.receipt("lecture", mode="lecture")
        self.receipt("legacy")  # receivers before lecture mode write no mode
        self.receipt("normal", mode="normal")
        self.runtime.discover()
        kinds = {}
        while job := self.runtime.queue.claim():
            kinds[Path(job["audio"]).stem] = json.loads(job["metadata"])["kind"]
        self.assertEqual(kinds, {"lecture": "lecture", "legacy": "omi", "normal": "omi"})

    def test_a_lecture_never_waits_for_or_dedupes_against_meetings(self):
        from second_brain.meetings import overlapping
        meetings = self.cfg.incoming_dir / "meetings"
        for cid, state in (("ab" * 16, "closed"), ("cd" * 16, "open")):
            write_json(meetings / ".captures" / f"{cid}.json", {
                "capture_id": cid, "client": "mac", "state": state, "start_ms": 1_200_000,
                "end_ms": 1_500_000, "app": "us.zoom.xos", "updated": time.time()})
        (meetings / f"{'ab' * 16}.caf").write_bytes(b"audio")
        span = {"first_timestamp": 1000, "audio_seconds": 600}
        self.assertEqual(len(overlapping(meetings, span).closed), 1)  # an Omi recording here would be deduped
        found = overlapping(meetings, {**span, "mode": "lecture"})
        self.assertEqual((found.closed, found.open), ([], []))
        audio = self.cfg.incoming_dir / "lecture.opus"
        audio.write_bytes(b"audio")
        self.runtime.queue.enqueue(audio, {"device": "d", "start_seq": 1, "kind": "lecture", **span})
        job = self.runtime.queue.claim()
        self.assertFalse(self.runtime.defer_for_meeting(job))
        self.assertIsNone(self.runtime.dedupe_index_or_none(job, self.cfg.data_dir / "jobs" / job["id"], "lecture"))

    def test_lecture_speakers_are_reviewable_even_if_the_gate_dropped_the_window(self):
        from second_brain.speakers import Speakers
        speakers = Speakers(self.root / "data", self.root / "review")
        clip = self.root / "data" / "jobs" / "job" / "c.wav"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"RIFF")
        manifest = {
            "windows": [{"id": "w0000", "memory_keep": False, "scores": {"importance": 20}, "final_segments": [
                {"start": 0.0, "end": 5.0, "speaker": "c0000:S1", "text": "Entropy measures disorder."}]}],
            "speaker_observations": [{"label": "c0000:S1", "clips": [
                {"start": 0.0, "end": 5.0, "text": "Entropy measures disorder.", "quality": "clean turn",
                 "path": str(clip)}]}],
        }
        # No stored kind: a job queued before kinds were stored still counts as a lecture.
        speakers.ingest({"id": "job", "metadata": json.dumps({"mode": "lecture", "first_utc": "x"})}, manifest)
        with speakers.connect() as db:
            self.assertEqual(db.execute("SELECT published FROM observations").fetchone()[0], 1)

    def test_bridge_exports_a_lecture_with_every_spoken_window(self):
        from second_brain.bridge import Bridge
        job_id = "e" * 64
        write_json(self.cfg.data_dir / "jobs" / job_id / "publication-manifest.json", {"windows": [
            {"id": "w0000", "start": 0, "end": 30, "final_transcript": "Entropy is defined.", "memory_keep": False},
            {"id": "w0001", "start": 30, "end": 60, "final_transcript": "  ", "memory_keep": False}]})
        bridge = Bridge(self.cfg.data_dir, self.cfg.vault_path, "http://127.0.0.1:9", "token")
        [(key, record, ident, data)] = bridge.sources({job_id: {"sha256": "0" * 64, "metadata": json.dumps(
            {"kind": "lecture", "first_utc": "2026-10-21T15:00:00Z"})}}, set())
        self.assertEqual((key, record, ident, data["kind"]), (f"job:{job_id}", "source", job_id, "lecture"))
        self.assertEqual([w["id"] for w in data["windows"]], ["w0000"])
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_lectures.LectureKindWiringTests -v`
Expected: 4 FAIL/ERROR — `KeyError: 'kind'` (discovery), `AssertionError: Tuples differ` on the lecture's captures, `AssertionError: 0 != 1` (speakers), `AssertionError` on `'omi' != 'lecture'` (bridge).

- [ ] **Step 3: Implement**

In `src/second_brain/runtime.py`, add the import after `from .io import ...` (line 13):

```python
from .kinds import job_kind, with_kind
```

Replace `discover`'s lines 90-93:

```python
                metadata = with_kind(value["metadata"])
                if metadata["kind"] == "meeting":
                    # Speaker review, routing and notes read the start time from first_utc.
                    metadata = {**metadata, "first_utc": utc_from_ms(metadata["start_ms"])}
```

Replace `dedupe_index_or_none` (lines 179-187):

```python
    def dedupe_index_or_none(self, job, root: Path, kind):
        """The dedupe index path, or None. Only Omi recordings are deduped (never meetings or
        lectures), and dedupe never fails a job: any error means no dedupe."""
        if kind != "omi":
            return None
        try:
            return self.meeting_index(job, root)
        except Exception:
            log.exception("Meeting dedupe index failed for job %s; processing without dedupe", job["id"])
            return None
```

In `process`, replace line 195 `meeting = metadata.get("source") == "meeting"` with:

```python
        kind = job_kind(metadata)
```

and replace lines 224-227:

```python
            if kind == "meeting":
                cmd.append("--meeting")
            # Only on a fresh run: a replay from a saved manifest never re-runs dedupe.
            index = self.dedupe_index_or_none(job, root, kind)
```

Replace line 274 and line 277:

```python
            write_json(publication, self.speakers.render(job["id"], manifest, cfg.owner_name if kind == "meeting" else None))
```

```python
        if kind == "meeting":
```

In `src/second_brain/queue.py` add `from .kinds import job_kind` after `from pathlib import Path` (line 9) and replace line 47:

```python
        if job_kind(metadata) == "meeting":
```

In `src/second_brain/meetings.py` add `from .kinds import job_kind` after `import time` (line 12) and replace lines 30-31:

```python
    if job_kind(metadata) != "omi":
        return None                              # meetings and lectures are never deduped
```

In `src/second_brain/speakers.py` add `from .kinds import job_kind` after line 13 and replace lines 148-149:

```python
        # Every meeting and lecture becomes a note, so its speakers are worth naming whatever the gate said.
        always_published = job_kind(metadata) != "omi"
```

and in line 189 replace `if meeting` with `if always_published`.

In `src/second_brain/bridge.py` add `from .kinds import job_kind` after line 23, replace `published_windows` (lines 38-42):

```python
def published_windows(manifest: dict, kind: str) -> list[dict]:
    """Windows the vault writer publishes (vault.publish / publish_meeting / publish_lecture);
    filtered chatter stays local."""
    if kind in ("meeting", "lecture"):
        return [w for w in manifest["windows"] if not w.get("speaker_filter") and w.get("final_transcript", "").strip()]
    return [w for w in manifest["windows"] if w.get("memory_keep") is True and w.get("route_to_knowledge_router") is True]
```

and in `sources` replace lines 100 and 106-108:

```python
            kind = job_kind(metadata)
```

```python
            } for w in published_windows(manifest, kind)]
            out.append((f"job:{job_id}", "source", job_id, {
                "kind": kind,
```

In `tests/test_dedupe.py` lines 769-770, pass kinds instead of booleans:

```python
            self.assertIsNone(self.runtime.dedupe_index_or_none(job, root, "omi"))
        self.assertIsNone(self.runtime.dedupe_index_or_none(job, root, "meeting"))
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_lectures tests.test_dedupe tests.test_meetings tests.test_bridge tests.test_speakers -v`
Expected: all PASS (meeting behavior is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/runtime.py src/second_brain/queue.py src/second_brain/meetings.py \
  src/second_brain/speakers.py src/second_brain/bridge.py tests/test_dedupe.py tests/test_lectures.py
git commit -m "refactor(service): read the job kind through job_kind everywhere"
```

---

### Task 3: `kind` in the public queue snapshot

**Files:**
- Modify: `src/second_brain/queue.py` (`snapshot` :137-150)
- Test: `tests/test_lectures.py`

- [ ] **Step 1: Write the failing test** — add before `if __name__`:

```python
class QueueKindTests(unittest.TestCase):
    def test_public_snapshot_carries_the_kind_but_never_the_metadata(self):
        from second_brain.queue import Queue
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = Queue(root / "queue.sqlite3")
            expected = {"a": "lecture", "b": "meeting", "c": "omi"}
            for name, extra in (("a", {"mode": "lecture"}), ("b", {"source": "meeting", "capture_id": "ab" * 16}),
                                ("c", {})):
                audio = root / f"{name}.opus"
                audio.write_bytes(name.encode())
                queue.enqueue(audio, {"device": "private-device-name", "start_seq": 1, **extra})
            job = queue.claim()
            snapshot = queue.snapshot()
            self.assertEqual(sorted(j["kind"] for j in snapshot["recent"]), ["lecture", "meeting", "omi"])
            self.assertEqual(snapshot["current"]["kind"], expected[Path(job["audio"]).stem])
            self.assertNotIn("metadata", snapshot["recent"][0])
            self.assertNotIn("private-device-name", json.dumps(snapshot))
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_lectures.QueueKindTests -v`
Expected: ERROR — `KeyError: 'kind'`.

- [ ] **Step 3: Implement** — in `src/second_brain/queue.py` add above `class Queue` (after the imports):

```python
def public(row) -> dict:
    """A job row for public status: its kind replaces the private metadata it is derived from."""
    value = dict(row)
    value["kind"] = job_kind(json.loads(value.pop("metadata")))
    return value
```

and replace `snapshot` (lines 137-150):

```python
    def snapshot(self):
        with self.connect() as db:
            counts = {r["state"]: r["n"] for r in db.execute("SELECT state,count(*) n FROM jobs GROUP BY state")}
            current = db.execute(
                "SELECT id,stage,attempts,updated,metadata FROM jobs WHERE state='processing' LIMIT 1"
            ).fetchone()
            events = [dict(r) for r in db.execute("SELECT time,kind,job,detail FROM events ORDER BY id DESC LIMIT 10")]
            recent = [
                public(r)
                for r in db.execute(
                    "SELECT id,state,stage,attempts,error,updated,metadata FROM jobs ORDER BY updated DESC LIMIT 10"
                )
            ]
            return {"counts": counts, "current": public(current) if current else None, "recent": recent, "events": events}
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_lectures tests.test_service tests.test_dedupe -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/queue.py tests/test_lectures.py
git commit -m "feat(service): public job status carries the job kind"
```

---

### Task 4: Pipeline `--lecture`

**Files:**
- Modify: `src/second_brain/adaptive/pipeline.py` (`main` arguments :1235-1244, tier choice :1372-1377)
- Test: `tests/test_lectures.py`

- [ ] **Step 1: Write the failing tests** — add before `if __name__`:

```python
def make_recording(path: Path, seconds: int = 10) -> Path:
    """Mono Opus like an Omi recording: one steady tone, so nothing is a long silence."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:sample_rate=16000:duration={seconds}",
         "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", str(path)],
        check=True,
    )
    return path


def lecture_pipeline_config(root: Path) -> Path:
    cfg = json.loads((ROOT / "config/pipeline.example.json").read_text())
    cfg.update(
        moss_command=[sys.executable, str(ROOT / "tests/fixtures/fake_moss.py")],
        scan_vault_for_novelty=False, long_silence_seconds=999, memory_gate_min_words=1,
        conversation_max_seconds=6.0,  # fake_moss's two 5 s turns become two sections
        work_root=str(root / "work"),
        # Outside lecture mode these send every window to VibeVoice 7B (a missing interpreter, so the
        # run would fail) and to Hermes scoring (a closed port, so every window would log a fallback).
        vibe7_rescue_uncertainty_threshold=0, vibe_python=str(root / "no-such-python"),
        hermes_scoring_enabled=True, hermes_url="http://127.0.0.1:9/v1/chat/completions",
    )
    path = root / "pipeline.json"
    path.write_text(json.dumps(cfg))
    return path


def run_pipeline(root: Path, *flags):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
    return subprocess.run(
        [sys.executable, "-m", "second_brain.adaptive.pipeline", "--audio", str(root / "recording.opus"),
         "--config", str(lecture_pipeline_config(root)), "--output-dir", str(root / "run"), *flags],
        capture_output=True, text=True, env=env,
    )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class LecturePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        make_recording(cls.root / "recording.opus")
        proc = run_pipeline(cls.root, "--lecture")  # no --no-hermes: --lecture alone must stop scoring
        if proc.returncode:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
        cls.manifest = json.loads((cls.root / "run" / "manifest.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_windows_become_timestamped_sections(self):
        self.assertEqual([(w["id"], w["start"]) for w in self.manifest["windows"]], [("w0000", 0.0), ("w0001", 5.0)])

    def test_lectures_stay_on_moss_and_never_reach_vibevoice(self):
        self.assertEqual({w["asr_tier"] for w in self.manifest["windows"]}, {"moss"})
        self.assertEqual({w["final_engine"] for w in self.manifest["windows"]}, {"moss"})
        self.assertFalse((self.root / "run" / "refined" / "vibe7-jobs.json").exists())

    def test_lectures_are_never_scored_by_hermes(self):
        self.assertFalse(self.manifest["config"]["hermes_scoring_enabled"])
        self.assertEqual([w["hermes_scores"] for w in self.manifest["windows"]], [None, None])
        self.assertEqual([f for f in self.manifest["fallbacks"] if f["from"] == "hermes"], [])

    def test_publication_fields_are_still_set(self):
        for w in self.manifest["windows"]:
            self.assertTrue(w["final_transcript"].strip())
            self.assertEqual(w["final_segments"], w["moss_segments"])
        self.assertNotIn("dedupe", self.manifest)


class LecturePipelineArgumentTests(unittest.TestCase):
    def test_lecture_refuses_meeting_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for flags in (["--lecture", "--meeting"], ["--lecture", "--meeting-dedupe", str(root / "index.json")]):
                proc = run_pipeline(root, *flags)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("--lecture cannot be combined", proc.stderr)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_lectures.LecturePipelineTests tests.test_lectures.LecturePipelineArgumentTests -v`
Expected: `LecturePipelineTests` ERROR in `setUpClass` with `error: unrecognized arguments: --lecture`; `test_lecture_refuses_meeting_flags` FAIL (stderr says `unrecognized arguments`, not `--lecture cannot be combined`).

- [ ] **Step 3: Implement** — in `src/second_brain/adaptive/pipeline.py` replace lines 1236-1244:

```python
    ap.add_argument("--meeting-dedupe", help="JSON index of overlapping meeting captures (service-written)")
    ap.add_argument("--lecture", action="store_true",
                    help="verbatim lecture: MOSS only, no Hermes scoring, no meeting dedupe")
    args = ap.parse_args()
    if args.lecture and (args.meeting or args.meeting_dedupe):
        ap.error("--lecture cannot be combined with --meeting or --meeting-dedupe")

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    global PROGRESS_FILE, COMMAND_TIMEOUT
    PROGRESS_FILE = Path(args.progress_file) if args.progress_file else None
    COMMAND_TIMEOUT = float(cfg.get("command_timeout_seconds", 14400))
    # A lecture is published whole, so per-window Hermes scores would decide nothing.
    if args.no_hermes or args.lecture:
        cfg["hermes_scoring_enabled"] = False
```

and replace the tier choice (lines 1372-1377):

```python
        # Meetings: 7B refinement works on a mono mix. Lectures: MOSS text stays verbatim, and a
        # 90-minute lecture stays well inside the attempt deadline.
        tier = (
            "vibe7"
            if not (args.meeting or args.lecture)
            and should_escalate_to_vibe7(cfg, importance, uncertainty, heuristic, preliminary_memory_gate)
            else "moss"
        )
```

The memory gate still runs and is recorded in the manifest; publication and the runtime ignore it for lectures (Tasks 6-7).

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_lectures tests.test_meetings.MeetingPipelineTests tests.test_adaptive_policy -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/adaptive/pipeline.py tests/test_lectures.py
git commit -m "feat(pipeline): --lecture keeps MOSS text verbatim without Hermes scoring or dedupe"
```

---

### Task 5: `lectures_vault_folder` config, example and installer

**Files:**
- Modify: `src/second_brain/config.py` (fields :45-48, validation :78-79)
- Modify: `config/service.example.json` (after `"meetings_vault_folder"`)
- Modify: `installer/install-core.ps1` (defaults :63-67, ACL grant after :118)
- Modify: `windows/smoke-test.ps1` (after :79)
- Test: `tests/test_lectures.py`

- [ ] **Step 1: Write the failing tests** — add before `if __name__`:

```python
class LectureConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_lecture_folder_defaults_and_loads(self):
        from second_brain.config import Config
        from tests.test_meetings import write_config
        self.assertEqual(Config.load(write_config(self.root)).lectures_vault_folder, "Omi/Lectures")
        self.assertEqual(Config.load(write_config(self.root, lectures_vault_folder="School/Lectures"))
                         .lectures_vault_folder, "School/Lectures")

    def test_unsafe_lecture_folder_is_rejected(self):
        from second_brain.config import Config
        from tests.test_meetings import write_config
        for folder in ("../x", "/abs", "a/./b", "C:x", ""):
            with self.assertRaisesRegex(ValueError, "lectures_vault_folder must be a safe relative folder"):
                Config.load(write_config(self.root, lectures_vault_folder=folder))

    def test_example_service_config_lists_the_lecture_folder(self):
        from second_brain.config import Config
        example = json.loads((ROOT / "config/service.example.json").read_text())
        self.assertEqual(example["lectures_vault_folder"], "Omi/Lectures")
        self.assertLessEqual(example.keys(), set(Config.__dataclass_fields__))
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_lectures.LectureConfigTests -v`
Expected: 3 FAIL/ERROR — `AttributeError: 'Config' object has no attribute 'lectures_vault_folder'`; `AssertionError` because the raised message is `Unknown service configuration keys: lectures_vault_folder`; `KeyError: 'lectures_vault_folder'`.

- [ ] **Step 3: Implement** — in `src/second_brain/config.py` after line 48 (`owner_name`):

```python
    # Omi lecture recordings (docs/superpowers/specs/2026-10-21-lecture-mode-design.md §3).
    lectures_vault_folder: str = "Omi/Lectures"
```

and after line 79 (`check_folder("meetings_vault_folder", ...)`):

```python
        check_folder("lectures_vault_folder", cfg.lectures_vault_folder)
```

In `config/service.example.json` after the `"meetings_vault_folder": "Omi/Meetings",` line:

```json
  "lectures_vault_folder": "Omi/Lectures",
```

In `installer/install-core.ps1` replace the first defaults line (line 64):

```powershell
        vault_folder = 'Omi/Conversations'; lectures_vault_folder = 'Omi/Lectures'; host = '0.0.0.0'
```

and after line 118 (the meetings `icacls` grant):

```powershell
    # Lecture transcript and summary notes publish here (lectures_vault_folder).
    $lecturesFolder = if ($serviceConfig.lectures_vault_folder) { $serviceConfig.lectures_vault_folder } else { 'Omi/Lectures' }
    $lecturesPath = Join-Path $Vault ($lecturesFolder -replace '/', '\')
    New-Item -ItemType Directory -Force $lecturesPath | Out-Null
    Run icacls.exe @($lecturesPath, '/grant', "${principal}:(OI)(CI)M", '/T', '/Q')
```

In `windows/smoke-test.ps1` after line 79 (`if (-not $writable) { throw 'The service cannot write meeting notes ...' }`):

```powershell
    $lecturesWritable = (Get-Acl "$root\vault\Omi\Lectures").Access | Where-Object {
        $_.IdentityReference.Value -in @($account, $sid) -and
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        ($_.FileSystemRights -band $modify) -eq $modify
    }
    if (-not $lecturesWritable) { throw 'The service cannot write lecture notes (no Modify on Omi\Lectures)' }
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_lectures tests.test_meetings.MeetingConfigTests tests.test_install_config -v`
Expected: all PASS. The PowerShell changes run only in Windows CI (`windows/smoke-test.ps1`); they cannot be executed on macOS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/config.py config/service.example.json installer/install-core.ps1 windows/smoke-test.ps1 tests/test_lectures.py
git commit -m "feat(service): lectures_vault_folder setting, installer default and write grant"
```

---

### Task 6: The lecture transcript note

**Files:**
- Modify: `src/second_brain/vault.py` (new `spoken_windows` and `publish_lecture` after `_clock` :114-116; `publish_meeting` :126-127 uses `spoken_windows`)
- Test: `tests/test_lectures.py`

- [ ] **Step 1: Write the failing tests** — add before `if __name__`:

```python
JOB = "e" * 64


def lecture_job(audio=str(Path(tempfile.gettempdir()) / "incoming" / "lecture.opus")):
    return {"id": JOB, "audio": audio, "sha256": "0" * 64, "metadata": json.dumps({
        "device": "112233445566", "start_seq": 3, "mode": "lecture", "kind": "lecture",
        "first_utc": "2026-10-21T15:00:00Z"})}


def lecture_manifest():
    return {"windows": [
        {"id": "w0000", "start": 0.0, "end": 65.0, "memory_keep": False,
         "final_transcript": "[0.00-5.00] Speaker 1 (c0000:S1): Today we define entropy.",
         "speaker_identities": {}},
        {"id": "w0001", "start": 3700.0, "end": 3720.0, "memory_keep": False,
         "final_transcript": "[3700.00-3720.00] Ana (c0015:S1): Any questions?",
         "speaker_identities": {"c0015:S1": {"id": "p1", "name": "Ana", "source": "voice match"}}},
        {"id": "w0002", "start": 3720.0, "end": 3800.0, "memory_keep": False,
         "speaker_filter": {"reason": "audiobook_speaker", "labels": ["c0015:S2"]},
         "final_transcript": "[3720.00-3800.00] [AudioBook] Narrator (c0015:S2): Chapter one."},
        {"id": "w0003", "start": 3800.0, "end": 3810.0, "memory_keep": False, "final_transcript": "  "},
    ]}


class LectureNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()

    def publish(self, manifest=None, audio_retained=False):
        from second_brain.vault import publish_lecture
        return publish_lecture(self.vault, "Omi/Lectures", lecture_job(), manifest or lecture_manifest(), audio_retained)

    def test_one_note_with_every_spoken_section_and_a_summary_link(self):
        written = self.publish()
        self.assertEqual([Path(p) for p in written], [Path("Omi/Lectures") / f"{JOB}.md"])
        text = (self.vault / written[0]).read_text(encoding="utf-8")
        self.assertIn('type: "omi-lecture"', text)
        self.assertIn('recorded_at: "2026-10-21T15:00:00Z"', text)
        self.assertIn(f'summary: "[[{JOB} Summary]]"', text)
        self.assertIn(f"Summary: [[{JOB} Summary]]", text)
        self.assertIn('speakers: ["Ana"]', text)
        self.assertIn("Audio deleted after processing", text)
        self.assertIn("## Transcript", text)
        # The memory gate dropped every window; a lecture publishes them anyway.
        self.assertLess(text.index("### 0:00"), text.index("Today we define entropy."))
        self.assertLess(text.index("### 1:01:40"), text.index("Ana (c0015:S1): Any questions?"))
        self.assertNotIn("Chapter one.", text)  # [AudioBook] exclusions still apply
        self.assertNotIn("### 1:02:00", text)
        self.assertNotIn("### 1:03:20", text)   # a window without speech gets no section

    def test_a_retained_recording_is_linked(self):
        text = (self.vault / self.publish(audio_retained=True)[0]).read_text(encoding="utf-8")
        self.assertIn(f"[Source audio]({Path(lecture_job()['audio']).as_uri()})", text)

    def test_republishing_is_idempotent_and_edits_are_preserved(self):
        from second_brain.vault import NoteConflict
        path = self.vault / self.publish()[0]
        self.assertEqual(self.publish(), [str(path.relative_to(self.vault))])
        path.write_text(path.read_text() + "\nmy notes\n")
        with self.assertRaises(NoteConflict):
            self.publish()
        self.assertTrue(path.read_text().endswith("my notes\n"))

    def test_a_lecture_without_speech_publishes_nothing(self):
        self.assertEqual(self.publish({"windows": []}), [])
        self.assertEqual(self.publish({"windows": lecture_manifest()["windows"][2:]}), [])
        self.assertFalse((self.vault / "Omi" / "Lectures").exists())
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_lectures.LectureNoteTests -v`
Expected: 4 ERRORs — `ImportError: cannot import name 'publish_lecture' from 'second_brain.vault'`.

- [ ] **Step 3: Implement** — in `src/second_brain/vault.py` add after `_clock` (line 116):

```python
def spoken_windows(manifest: dict) -> list[dict]:
    """Windows with speech that no speaker exclusion vetoed. Meetings and lectures publish all of
    them: the memory gate never filters either."""
    return [w for w in manifest["windows"]
            if not w.get("speaker_filter") and w.get("final_transcript", "").strip()]
```

In `publish_meeting` replace lines 126-127:

```python
    windows = spoken_windows(manifest)
```

Append at the end of the file:

```python
def publish_lecture(vault: Path, folder: str, job: dict, manifest: dict, audio_retained: bool = True) -> list[str]:
    """The whole lecture in one note, one section per window, linked to the summary note that
    publish_lecture_summary writes later. A lecture with no speech publishes nothing."""
    root, dest = _destination(vault, folder)
    windows = spoken_windows(manifest)
    if not windows:
        return []
    metadata = json.loads(job["metadata"])
    identities = {k: v for w in windows for k, v in w.get("speaker_identities", {}).items()}
    summary = f"[[{job['id']} Summary]]"
    front = {
        "type": "omi-lecture",
        "source_id": job["id"],
        "recorded_at": metadata.get("first_utc", "unknown-time"),
        "device": metadata["device"],
        "source_sha256": job["sha256"],
        **({"audio": Path(job["audio"]).as_uri()} if audio_retained else {}),
        "summary": summary,
        "speakers": sorted({v["name"] for v in identities.values()}),
        "speaker_identities": identities,
    }
    lines = _frontmatter(front) + [
        f"# Lecture · {front['recorded_at']}",
        "",
        f"Summary: {summary}",
        "",
        # The recording is deleted after processing unless retention is configured; never link a missing file.
        f"[Source audio]({front['audio']})" if audio_retained else "Audio deleted after processing",
        "",
        "Unnamed speaker labels are local to this recording.",
        "",
        "## Transcript",
        "",
    ]
    for w in windows:
        lines += [f"### {_clock(w['start'])}", "", w["final_transcript"].strip(), ""]
    path = dest / f"{job['id']}.md"
    _write_note(path, dest, "\n".join(lines).encode("utf-8"))
    return [str(path.relative_to(root))]
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_lectures tests.test_meetings.MeetingNoteTests -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/vault.py tests/test_lectures.py
git commit -m "feat(vault): lecture transcript note with every spoken section"
```

---

### Task 7: Run lecture jobs end to end

**Files:**
- Modify: `src/second_brain/runtime.py` (import :18, `process` cmd :224-225, publishing :277-296)
- Modify: `tests/helpers.py` (new `mode_record`, `lecture_records` after `records` :48-54)
- Test: `tests/test_lectures.py`

- [ ] **Step 1: Write the failing test** — in `tests/helpers.py` add after `records` (line 54):

```python
def mode_record(mode: int, timestamp: int = 1700000000) -> bytes:
    """A recording-mode marker record (receiver plan 1): mode 0 = normal, 1 = lecture."""
    return struct.pack(">I", timestamp) + (P.RECORD_MODE_MAGIC + bytes([mode])).ljust(P.AUDIO_PAYLOAD_BYTES, b"\0")


def lecture_records(count=250):
    """A lecture-mode marker, then `count` audio records and an end marker."""
    return [mode_record(1)] + records(count)
```

Add to `tests/test_lectures.py` before `if __name__`:

```python
@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class LectureEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task = None

    async def asyncTearDown(self):
        if self.task:
            self.runtime.stop.set()
            await asyncio.wait_for(self.task, 10)
        self.tmp.cleanup()

    async def start(self, hermes_url=None):
        from second_brain.runtime import Runtime
        cfg = configuration(self.root)
        if hermes_url:
            pipeline = json.loads(cfg.pipeline_config.read_text())
            # router_enabled too: if lecture text reached the router, Decisions/Daily notes would appear.
            pipeline.update(router_enabled=True, hermes_url=hermes_url)
            write_json(cfg.pipeline_config, pipeline)
            cfg = dataclasses.replace(cfg, no_hermes=False)  # helpers default to no Hermes
        self.cfg = cfg
        self.runtime = Runtime(cfg)
        self.task = asyncio.create_task(self.runtime.run())
        for _ in range(100):
            if self.runtime.server and self.runtime.server._server:
                break
            if self.task.done():
                await self.task
            await asyncio.sleep(0.01)

    async def wait_complete(self):
        for _ in range(600):
            counts = self.runtime.queue.snapshot()["counts"]
            if counts.get("complete") or counts.get("failed"):
                break
            await asyncio.sleep(0.05)
        logs = "\n".join(p.read_text()[-3000:] for p in (self.root / "data/jobs").rglob("pipeline.log"))
        self.assertEqual(counts.get("complete"), 1, str(self.runtime.queue.snapshot()) + logs)

    async def test_uploaded_lecture_becomes_one_transcript_note(self):
        from tests.helpers import lecture_records, upload
        from omi_local import upload_protocol as U
        await self.start()
        self.assertEqual(await upload(self.runtime.server.bound_port, lecture_records()), U.MSG_BYE)
        await self.wait_complete()
        folder = self.cfg.vault_path / "Omi" / "Lectures"
        [note] = list(folder.glob("*.md"))
        text = note.read_text(encoding="utf-8")
        self.assertIn('type: "omi-lecture"', text)
        self.assertIn(f"[[{note.stem} Summary]]", text)
        self.assertIn("### 0:00", text)
        self.assertIn("c0000:S1", text)
        self.assertIn("c0000:S2", text)
        self.assertIn("project launch", text)
        # Exactly one note in the whole vault: nothing in Conversations, no router or daily notes.
        self.assertEqual(list(self.cfg.vault_path.rglob("*.md")), [note])
        self.assertEqual(self.runtime.router.snapshot()["pending"] + self.runtime.router.snapshot()["done"], 0)
        [job] = self.runtime.queue.snapshot()["recent"]
        self.assertEqual(job["kind"], "lecture")
        with self.runtime.queue.connect() as db:
            result = json.loads(db.execute("SELECT result FROM jobs").fetchone()[0])
        self.assertEqual((result["notes"], result["filtered"]), (1, 0))
        # Lecture audio is deleted after publication, like other Omi audio.
        self.assertIn("Audio deleted after processing", text)
        self.assertEqual(list(self.cfg.incoming_dir.glob("*.opus")), [])
        # Crash replay republishes from the saved manifest without another ASR run.
        with self.runtime.queue.connect() as db:
            db.execute("UPDATE jobs SET state='processing'")
        self.runtime.queue.recover()
        await self.wait_complete()
        self.assertEqual(note.read_text(encoding="utf-8"), text)
        self.assertEqual(len(list((self.root / "data/jobs").rglob("pipeline.log"))), 1)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_lectures.LectureEndToEndTests -v`
Expected: FAIL — `ValueError: not enough values to unpack (expected 1, got 0)` at `[note] = ...` (the job published to `Omi/Conversations` through the gate instead).

- [ ] **Step 3: Implement** — in `src/second_brain/runtime.py` replace line 18:

```python
from .vault import publish, publish_lecture, publish_meeting
```

In `process` replace the `--meeting` lines (from Task 2):

```python
            if kind == "meeting":
                cmd.append("--meeting")
            elif kind == "lecture":
                cmd.append("--lecture")
```

Replace the publishing block from `if kind == "meeting":` (the `publish_meeting` call) through the end of `process`:

```python
        if kind == "meeting":
            notes = await asyncio.to_thread(publish_meeting, cfg.vault_path, cfg.meetings_vault_folder, job, manifest)
        elif kind == "lecture":
            notes = await asyncio.to_thread(
                publish_lecture, cfg.vault_path, cfg.lectures_vault_folder, job, manifest,
                not cfg.delete_audio_after_processing,
            )
        else:
            notes = await asyncio.to_thread(
                publish, cfg.vault_path, cfg.vault_folder, job, manifest, not cfg.delete_audio_after_processing
            )
        # Lectures stay in the lectures folder: never routed, so never in daily notes either.
        if kind != "lecture" and self.routing():
            # Idempotent, so a crash-replay of this job enqueues nothing twice.
            kept = [w for w in manifest["windows"]
                    if w.get("memory_keep") is True and w.get("route_to_knowledge_router") is True]
            self.router.enqueue(job["id"], kept, metadata.get("first_utc") or "unknown")
        return {
            "notes": len(notes),
            "warnings": len(manifest.get("fallbacks", [])),
            # A lecture is published whole: nothing is filtered.
            "filtered": 0 if kind == "lecture" else sum(w.get("memory_keep") is not True for w in manifest["windows"]),
            "windows": len(manifest["windows"]),
            **({"deduped": manifest["dedupe"]["dropped_count"],
                "alignment_failed": manifest["dedupe"]["alignment_failed"]}
               if "dedupe" in manifest else {}),
        }
```

`purge_audio` needs no change: lecture recordings live in `incoming/`, outside `incoming/meetings/`.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_lectures tests.test_service tests.test_meetings tests.test_router -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/runtime.py tests/helpers.py tests/test_lectures.py
git commit -m "feat(service): lecture jobs publish one unfiltered transcript note and are never routed"
```

---

### Task 8: Summarize a lecture (map, reduce, summary note, queue)

**Files:**
- Create: `src/second_brain/lectures.py`
- Modify: `src/second_brain/vault.py` (append `publish_lecture_summary`)
- Test: `tests/test_lectures.py`

- [ ] **Step 1: Write the failing tests** — add before `if __name__`:

```python
class FakeSummarizer:
    """Stands in for router.chat: numbered section summaries, then one combined summary."""

    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, cfg, system, user):
        from second_brain import lectures
        if self.error:
            raise self.error
        self.calls.append((system, user))
        if system == lectures.SECTION_PROMPT:
            return {"summary": f"Section {len(self.calls)}."}
        return {"summary": "Combined."}


class LectureSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()

    def test_sections_are_summarized_then_combined(self):
        from second_brain import lectures, router
        fake = FakeSummarizer()
        with patch.object(router, "chat", fake):
            summary = lectures.summarize({}, lecture_manifest())
        self.assertEqual([system for system, _ in fake.calls],
                         [lectures.SECTION_PROMPT, lectures.SECTION_PROMPT, lectures.COMBINE_PROMPT])
        self.assertIn("Today we define entropy.", fake.calls[0][1])
        self.assertNotIn("Chapter one.", "".join(user for _, user in fake.calls))  # excluded speaker
        self.assertIn("[0:00] Section 1.", fake.calls[2][1])
        self.assertIn("[1:01:40] Section 2.", fake.calls[2][1])
        self.assertEqual(summary, {"version": 1, "summary": "Combined.", "sections": [
            {"id": "w0000", "start": 0.0, "end": 65.0, "summary": "Section 1."},
            {"id": "w0001", "start": 3700.0, "end": 3720.0, "summary": "Section 2."}]})

    def test_an_empty_hermes_reply_is_an_error_not_an_empty_summary(self):
        from second_brain import lectures, router
        for reply in ({}, {"summary": "  "}, {"summary": ["not", "text"]}):
            with patch.object(router, "chat", lambda cfg, system, user: reply):
                with self.assertRaisesRegex(ValueError, "Hermes returned no summary"):
                    lectures.summarize({}, lecture_manifest())
        with self.assertRaisesRegex(ValueError, "no transcript"):
            lectures.summarize({}, {"windows": []})

    def test_summary_note_links_back_and_preserves_edits(self):
        from second_brain.vault import NoteConflict, publish_lecture_summary
        summary = {"version": 1, "summary": "Combined.", "sections": [
            {"id": "w0000", "start": 0.0, "end": 65.0, "summary": "Section 1."}]}
        written = publish_lecture_summary(self.vault, "Omi/Lectures", JOB, "2026-10-21T15:00:00Z", summary)
        self.assertEqual([Path(p) for p in written], [Path("Omi/Lectures") / f"{JOB} Summary.md"])
        path = self.vault / written[0]
        text = path.read_text(encoding="utf-8")
        self.assertIn('type: "omi-lecture-summary"', text)
        self.assertIn(f'transcript: "[[{JOB}]]"', text)
        self.assertIn(f"Transcript: [[{JOB}]]", text)
        self.assertLess(text.index("## Summary"), text.index("Combined."))
        self.assertLess(text.index("### 0:00"), text.index("Section 1."))
        self.assertEqual(publish_lecture_summary(self.vault, "Omi/Lectures", JOB, "2026-10-21T15:00:00Z", summary),
                         written)
        path.write_text(text + "\nmy notes\n")
        with self.assertRaises(NoteConflict):
            publish_lecture_summary(self.vault, "Omi/Lectures", JOB, "2026-10-21T15:00:00Z", summary)
        with self.assertRaisesRegex(ValueError, "Invalid job ID"):
            publish_lecture_summary(self.vault, "Omi/Lectures", "../x", "2026-10-21T15:00:00Z", summary)

    def test_summary_queue_is_idempotent_and_backs_off_like_routing(self):
        from second_brain.lectures import SummaryQueue
        queue = SummaryQueue(self.root / "lectures.sqlite3")
        self.assertEqual(queue.enqueue(JOB, "2026-10-21T15:00:00Z"), 1)
        self.assertEqual(queue.enqueue(JOB, "2026-10-21T15:00:00Z"), 0, "idempotent")
        item = queue.claim()
        self.assertEqual((item["id"], item["job"], item["observed_at"]), (JOB, JOB, "2026-10-21T15:00:00Z"))
        queue.fail(item, "URLError; see private service log")
        self.assertIsNone(queue.claim(), "backs off")
        self.assertEqual(queue.snapshot()["pending"], 1)
        self.assertEqual(queue.snapshot()["last_error"], "URLError; see private service log")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_lectures.LectureSummaryTests -v`
Expected: 4 ERRORs — `ImportError: cannot import name 'lectures' from 'second_brain'` and `cannot import name 'publish_lecture_summary'`.

- [ ] **Step 3: Implement** — append to `src/second_brain/vault.py`:

```python
def publish_lecture_summary(vault: Path, folder: str, job_id: str, recorded_at: str, summary: dict) -> list[str]:
    """The lecture's summary note, rendered from the frozen lecture-summary.json and linked back
    to the transcript note."""
    if not job_id.isalnum():
        raise ValueError("Invalid job ID")
    root, dest = _destination(vault, folder)
    transcript = f"[[{job_id}]]"
    front = {"type": "omi-lecture-summary", "source_id": job_id, "recorded_at": recorded_at,
             "transcript": transcript}
    lines = _frontmatter(front) + [
        f"# Lecture summary · {recorded_at}",
        "",
        f"Transcript: {transcript}",
        "",
        "Written by Hermes from the transcript; check it against the transcript before relying on it.",
        "",
        "## Summary",
        "",
        summary["summary"].strip(),
        "",
        "## Sections",
        "",
    ]
    for section in summary["sections"]:
        lines += [f"### {_clock(section['start'])}", "", section["summary"].strip(), ""]
    path = dest / f"{job_id} Summary.md"
    _write_note(path, dest, "\n".join(lines).encode("utf-8"))
    return [str(path.relative_to(root))]
```

Create `src/second_brain/lectures.py`:

```python
"""Lecture summaries: summarize each transcript section, then combine the section summaries
(docs/superpowers/specs/2026-10-21-lecture-mode-design.md §3). Hermes output is untrusted text;
it only ever lands in the lecture's own summary note."""

from __future__ import annotations
import time
from . import router
from .router import RouterQueue
from .vault import _clock, spoken_windows

SECTION_PROMPT = """You summarize one section of a lecture transcript for the listener's study notes.

Return ONLY valid JSON. Do not use tools. Do not read or write files. Do not browse.
Use only what the transcript says; do not add outside facts. Speaker labels may be anonymous.

Use exactly this schema:
{"summary": "3-6 sentences: the concepts, definitions, examples and announcements in this section"}
"""

COMBINE_PROMPT = """You combine the section summaries of one lecture into study notes.

Return ONLY valid JSON. Do not use tools. Do not read or write files. Do not browse.
Use only the supplied section summaries; do not add outside facts.

Use exactly this schema:
{"summary": "Markdown: a one-paragraph overview, then a bulleted list of the key points, then any assignments, deadlines or announcements (leave that part out when there are none)"}
"""


def _summary(reply: dict) -> str:
    text = reply.get("summary")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Hermes returned no summary")
    return text.strip()


def summarize(cfg: dict, manifest: dict) -> dict:
    """Map every spoken section, then reduce. Any Hermes failure raises, so the queue retries the
    whole summary; nothing partial is ever published."""
    sections = []
    for w in spoken_windows(manifest):
        user = f"SECTION: {_clock(w['start'])}–{_clock(w['end'])}\n\nTRANSCRIPT:\n{w['final_transcript'].strip()}\n"
        sections.append({"id": w["id"], "start": w["start"], "end": w["end"],
                         "summary": _summary(router.chat(cfg, SECTION_PROMPT, user))})
    if not sections:
        raise ValueError("Lecture has no transcript to summarize")
    user = "SECTION SUMMARIES:\n\n" + "\n\n".join(f"[{_clock(s['start'])}] {s['summary']}" for s in sections) + "\n"
    return {"version": 1, "summary": _summary(router.chat(cfg, COMBINE_PROMPT, user)), "sections": sections}


class SummaryQueue(RouterQueue):
    """Lecture jobs waiting for a summary: RouterQueue's table, backoff and retry in its own
    database. `text` stays empty; the transcript is read from the job directory."""

    def enqueue(self, job: str, observed_at: str) -> int:
        now = time.time()
        with self.connect() as db:
            return db.execute(
                "INSERT OR IGNORE INTO routes(id,job,text,observed_at,created,updated) VALUES(?,?,?,?,?,?)",
                (job, job, "", observed_at, now, now)).rowcount
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_lectures tests.test_router -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/lectures.py src/second_brain/vault.py tests/test_lectures.py
git commit -m "feat(service): lecture summaries through Hermes, map then reduce"
```

---

### Task 9: Summarize published lectures in the service

**Files:**
- Modify: `src/second_brain/runtime.py` (imports :17-18, `__init__` :62, new `summarizing` after `routing` :66-75, `process` lecture branch, new `summarize_lecture`/`summarize_lectures` after `route_published` :326-348, `status` :403, `run` tasks :447)
- Modify: `src/second_brain/cli.py` (`retry` :29-34)
- Test: `tests/test_lectures.py`

- [ ] **Step 1: Write the failing tests** — add these methods to `LectureSummaryTests`:

```python
    def runtime(self, **changes):
        from second_brain.runtime import Runtime
        runtime = Runtime(dataclasses.replace(configuration(self.root), **changes))
        write_json(runtime.cfg.data_dir / "jobs" / JOB / "publication-manifest.json", lecture_manifest())
        return runtime

    def test_summaries_need_hermes_but_not_the_router(self):
        self.assertIsNone(self.runtime().summarizing(), "no_hermes")
        runtime = self.runtime(no_hermes=False)
        self.assertEqual(runtime.summarizing()["hermes_url"], "http://YOUR-PI-IP:8642/v1/chat/completions")
        pipeline = json.loads(runtime.cfg.pipeline_config.read_text())
        write_json(runtime.cfg.pipeline_config, dict(pipeline, hermes_url=""))
        self.assertIsNone(runtime.summarizing(), "no hermes_url")

    def test_summary_is_frozen_before_the_note_and_replays_identically(self):
        from second_brain import router
        runtime = self.runtime()
        item = {"id": JOB, "job": JOB, "observed_at": "2026-10-21T15:00:00Z"}
        with patch.object(router, "chat", FakeSummarizer()):
            self.assertEqual(runtime.summarize_lecture({}, item), {"notes": 1})
        frozen = runtime.cfg.data_dir / "jobs" / JOB / "lecture-summary.json"
        note = runtime.cfg.vault_path / "Omi" / "Lectures" / f"{JOB} Summary.md"
        text = note.read_text(encoding="utf-8")
        self.assertEqual(json.loads(frozen.read_text())["summary"], "Combined.")
        note.unlink()  # a crash before the note was written
        outage = FakeSummarizer(error=OSError("Hermes down"))
        with patch.object(router, "chat", outage):
            runtime.summarize_lecture({}, item)
        self.assertEqual(note.read_text(encoding="utf-8"), text)
        self.assertEqual(outage.calls, [], "a replay never asks Hermes again")

    def test_a_hermes_outage_only_delays_the_summary(self):
        from second_brain import router
        runtime = self.runtime()
        runtime.summaries.enqueue(JOB, "2026-10-21T15:00:00Z")
        item = runtime.summaries.claim()
        with patch.object(router, "chat", FakeSummarizer(error=OSError("Hermes down"))):
            with self.assertRaises(OSError):
                runtime.summarize_lecture({}, item)
        self.assertFalse((runtime.cfg.data_dir / "jobs" / JOB / "lecture-summary.json").exists())
        self.assertFalse((runtime.cfg.vault_path / "Omi" / "Lectures").exists())
        runtime.summaries.fail(item, "OSError; see private service log")
        with runtime.summaries.connect() as db:
            db.execute("UPDATE routes SET next_attempt=0")
        item = runtime.summaries.claim()
        with patch.object(router, "chat", FakeSummarizer()):
            runtime.summaries.done(item["id"], runtime.summarize_lecture({}, item))
        self.assertEqual(runtime.summaries.snapshot()["done"], 1)
        self.assertTrue((runtime.cfg.vault_path / "Omi" / "Lectures" / f"{JOB} Summary.md").is_file())

    def test_cli_retry_requeues_failed_summaries(self):
        from second_brain.lectures import SummaryQueue
        from tests.test_meetings import write_config
        path = write_config(self.root)
        queue = SummaryQueue(self.root / "data" / "lectures.sqlite3")
        queue.enqueue(JOB, "2026-10-21T15:00:00Z")
        with queue.connect() as db:
            db.execute("UPDATE routes SET state='failed'")
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            [str(ROOT / "src"), str(ROOT / "omi/firmware/scripts/omi-local")]))
        out = subprocess.run([sys.executable, "-m", "second_brain.cli", "retry", "--config", str(path)],
                             capture_output=True, text=True, env=env, check=True).stdout
        self.assertIn("Requeued 1 failed lecture summaries", out)
        self.assertEqual(queue.snapshot()["pending"], 1)
```

Add above `LectureEndToEndTests`:

```python
SECTION_REPLY = "The project launch moves to Friday."
COMBINED_REPLY = "Overview: launch planning for Friday."


class LectureHermes(http.server.BaseHTTPRequestHandler):
    """Answers both summary prompts; any other prompt (the router's) would write a decision."""
    seen = []

    def do_POST(self):
        from second_brain import lectures
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = body["messages"][0]["content"]
        LectureHermes.seen.append(system)
        if system == lectures.SECTION_PROMPT:
            reply = {"summary": SECTION_REPLY}
        elif system == lectures.COMBINE_PROMPT:
            reply = {"summary": COMBINED_REPLY}
        else:
            reply = {"decisions": [{"text": "Routed lecture content.", "confidence": 1.0, "observed_at": None}],
                     "items": []}
        data = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass
```

Add this method to `LectureEndToEndTests`:

```python
    async def test_lecture_summary_note_is_linked_and_nothing_is_routed(self):
        from second_brain import lectures
        from tests.helpers import lecture_records, upload
        from omi_local import upload_protocol as U
        server = http.server.HTTPServer(("127.0.0.1", 0), LectureHermes)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        LectureHermes.seen = []
        await self.start(f"http://127.0.0.1:{server.server_port}/v1/chat/completions")
        self.assertEqual(await upload(self.runtime.server.bound_port, lecture_records()), U.MSG_BYE)
        await self.wait_complete()
        for _ in range(200):
            if self.runtime.summaries.snapshot()["done"]:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.runtime.summaries.snapshot()["done"], 1, self.runtime.summaries.snapshot())
        job_id = self.runtime.queue.snapshot()["recent"][0]["id"]
        folder = self.cfg.vault_path / "Omi" / "Lectures"
        self.assertEqual(sorted(p.name for p in self.cfg.vault_path.rglob("*.md")),
                         [f"{job_id} Summary.md", f"{job_id}.md"])
        text = (folder / f"{job_id} Summary.md").read_text(encoding="utf-8")
        self.assertIn('type: "omi-lecture-summary"', text)
        self.assertIn(f"Transcript: [[{job_id}]]", text)
        self.assertIn(SECTION_REPLY, text)
        self.assertIn(COMBINED_REPLY, text)
        self.assertTrue((self.cfg.data_dir / "jobs" / job_id / "lecture-summary.json").is_file())
        # Only the two summary prompts reached Hermes: the router never saw the lecture.
        self.assertEqual(LectureHermes.seen, [lectures.SECTION_PROMPT, lectures.COMBINE_PROMPT])
        self.assertEqual(self.runtime.router.snapshot()["pending"] + self.runtime.router.snapshot()["done"], 0)
        self.assertFalse((self.cfg.vault_path / "Daily").exists())
        self.runtime.status()
        status = json.loads(self.cfg.status_file.read_text())
        self.assertEqual((status["lecture_summaries"]["enabled"], status["lecture_summaries"]["done"]), (True, 1))
        # Crash replay of the job re-enqueues nothing and leaves both notes byte-identical.
        with self.runtime.queue.connect() as db:
            db.execute("UPDATE jobs SET state='processing'")
        self.runtime.queue.recover()
        await self.wait_complete()
        self.assertEqual((folder / f"{job_id} Summary.md").read_text(encoding="utf-8"), text)
        self.assertEqual(self.runtime.summaries.snapshot()["done"], 1)
        self.assertEqual(len(LectureHermes.seen), 2)
```

In `test_uploaded_lecture_becomes_one_transcript_note` (Task 7) add after the `[note] = list(folder.glob("*.md"))` line:

```python
        self.assertEqual(self.runtime.summaries.snapshot()["pending"], 0)  # Hermes off: no summary queued
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_lectures.LectureSummaryTests tests.test_lectures.LectureEndToEndTests -v`
Expected: the runtime tests ERROR with `AttributeError: 'Runtime' object has no attribute 'summarizing'` / `'summarize_lecture'` / `'summaries'` (including the line added to the Task 7 test), and the CLI test FAILS because its output has no `Requeued 1 failed lecture summaries` line.

- [ ] **Step 3: Implement** — in `src/second_brain/runtime.py`, after line 17 (`from .router import RouterQueue, route`) add:

```python
from .lectures import SummaryQueue, summarize
```

and change the vault import (from Task 7) to:

```python
from .vault import publish, publish_lecture, publish_lecture_summary, publish_meeting
```

In `__init__` after line 62 (`self.router = RouterQueue(...)`):

```python
        self.summaries = SummaryQueue(cfg.data_dir / "lectures.sqlite3")
```

After `routing` (line 75) add:

```python
    def summarizing(self):
        """The pipeline config when lecture summaries can run, else None. Unlike routing this
        needs no router_enabled: lecture content never reaches the knowledge router."""
        try:
            pipeline = self.cfg.pipeline()
        except (OSError, ValueError):
            log.exception("Pipeline config unreadable; lecture summaries paused")
            return None
        if self.cfg.no_hermes or not pipeline.get("hermes_url"):
            return None
        return pipeline
```

In `process` replace the routing block from Task 7:

```python
        # Lectures stay in the lectures folder: never routed, so never in daily notes either.
        if kind != "lecture" and self.routing():
```

with:

```python
        if kind == "lecture":
            # Lectures stay in the lectures folder: never routed, so never in daily notes either.
            # Idempotent, so a crash-replay of this job enqueues nothing twice.
            if notes and self.summarizing():
                self.summaries.enqueue(job["id"], metadata.get("first_utc", "unknown-time"))
        elif self.routing():
```

(the indented router enqueue lines below it are unchanged).

After `route_published` (line 348) add:

```python
    def summarize_lecture(self, pipeline, item):
        """Summarize one published lecture. Hermes's result is frozen before the note is written,
        so a crash replay writes byte-identical content and never asks Hermes again."""
        root = self.cfg.data_dir / "jobs" / item["job"]
        frozen = root / "lecture-summary.json"
        if not frozen.exists():
            write_json(frozen, summarize(pipeline, read_json(root / "publication-manifest.json")))
        notes = publish_lecture_summary(self.cfg.vault_path, self.cfg.lectures_vault_folder, item["job"],
                                        item["observed_at"], read_json(frozen))
        return {"notes": len(notes)}

    async def summarize_lectures(self):
        """Summarize published lectures one at a time, separately from transcription, so a Hermes
        outage only delays the summary note."""
        while not self.stop.is_set():
            item = None
            try:
                pipeline = await asyncio.to_thread(self.summarizing)
                item = self.summaries.claim() if pipeline else None
                if item:
                    result = await asyncio.to_thread(self.summarize_lecture, pipeline, item)
                    self.summaries.done(item["id"], result)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Lecture summary failed for %s", item["id"] if item else "the summarizer")
                if item:
                    self.summaries.fail(item, f"{type(e).__name__}; see private service log")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(self.cfg.poll_seconds, 1))
            except asyncio.TimeoutError:
                pass
```

In `status`, after the `router=...` line (line 403):

```python
            lecture_summaries=dict(self.summaries.snapshot(), enabled=self.summarizing() is not None),
```

In `run`, after `asyncio.create_task(self.route_published()),` (line 447):

```python
                asyncio.create_task(self.summarize_lectures()),
```

In `src/second_brain/cli.py` replace lines 31-34:

```python
        from .lectures import SummaryQueue
        from .router import RouterQueue

        print(f"Requeued {RouterQueue(cfg.data_dir / 'router.sqlite3').retry_failed()} failed routing items")
        print(f"Requeued {SummaryQueue(cfg.data_dir / 'lectures.sqlite3').retry_failed()} failed lecture summaries")
        return
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_lectures tests.test_router tests.test_service -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/second_brain/runtime.py src/second_brain/cli.py tests/test_lectures.py
git commit -m "feat(service): summarize published lectures with retry, separately from transcription"
```

---

### Task 10: Tray lecture badge

**Files:**
- Modify: `windows/SecondBrain.Status/ServiceStatus.cs` (`JobInfo` :11-14, `Parse` :87-89)
- Modify: `windows/SecondBrain.Tray/ActivityPage.xaml.cs` (:6, :26)
- Modify: `windows/SecondBrain.Tray/ActivityPage.xaml` (after :34)
- Test: `windows/SecondBrain.Status.Tests/StatusTests.cs` (`Live` :16-20, checks after :66)

- [ ] **Step 1: Write the failing test** — in `StatusTests.cs` `Live()`, add `["kind"] = "lecture",` to the `recent` job object (after `["attempts"] = 1, ["error"] = null, ["updated"] = Now.ToUnixTimeSeconds(),`). After line 66 (`Check.Equal("14fe9d78", live.Jobs[0].ShortId, "short job id");`) add:

```csharp
        Check.Equal("lecture", live.Jobs[0].Kind, "job kind");
        Check.Equal("Lecture", live.Jobs[0].Badge, "lecture badge");
        var meetingJob = Live();
        meetingJob["recent"]![0]!["kind"] = "meeting";
        Check.Equal("Meeting", Parse(meetingJob).Jobs[0].Badge, "meeting badge");
        var legacyJob = Live();
        legacyJob["recent"]![0]!.AsObject().Remove("kind");
        var legacy = Parse(legacyJob).Jobs[0];
        Check.Equal(("omi", ""), (legacy.Kind, legacy.Badge), "a service before job kinds shows no badge");
```

- [ ] **Step 2: Run to verify failure** (Windows box or Windows CI only; `dotnet` is not installed on the Mac)

Run: `dotnet run --project windows/SecondBrain.Status.Tests -c Release`
Expected: build error `CS1061: 'JobInfo' does not contain a definition for 'Kind'`.

- [ ] **Step 3: Implement** — in `ServiceStatus.cs` replace `JobInfo` (lines 11-14):

```csharp
public sealed record JobInfo(string Id, string State, string Stage, int Attempts, string? Error, DateTimeOffset Updated, string Kind = "omi")
{
    public string ShortId => Id.Length > 8 ? Id[..8] : Id;
    // Ordinary Omi recordings carry no badge. Kind is metadata only, never content.
    public string Badge => Kind switch { "lecture" => "Lecture", "meeting" => "Meeting", _ => "" };
}
```

and in `Parse` replace line 88:

```csharp
            .Select(j => new JobInfo(Text(j, "id") ?? "", Text(j, "state") ?? "", Text(j, "stage") ?? "", Int(j, "attempts"), Text(j, "error"), Time(j.GetProperty("updated")), Text(j, "kind") ?? "omi"))
```

In `ActivityPage.xaml.cs` replace line 6 and line 26:

```csharp
public sealed record JobRow(string Job, string Kind, string State, string Stage, int Attempts, string Error, string Updated);
```

```csharp
            JobsGrid.ItemsSource = status.Jobs.Select(j => new JobRow(j.ShortId, j.Badge, j.State, j.Stage, j.Attempts, j.Error ?? "", Formats.Time(j.Updated, now))).ToList();
```

In `ActivityPage.xaml` after line 34 (the `Job` column):

```xml
                    <DataGridTextColumn Header="Kind" Binding="{Binding Kind}" Width="80" />
```

- [ ] **Step 4: Run to verify pass** (Windows)

Run: `powershell -File windows/build.ps1`
Expected: `Status and review tests passed`, then the service and tray publish without errors. If no Windows machine is available, push the branch and confirm the Windows CI job (`.github/workflows/ci.yml`, which runs `windows/build.ps1`) is green; otherwise report this check as not performed.

- [ ] **Step 5: Commit**

```bash
git add windows/SecondBrain.Status/ServiceStatus.cs windows/SecondBrain.Tray/ActivityPage.xaml.cs \
  windows/SecondBrain.Tray/ActivityPage.xaml windows/SecondBrain.Status.Tests/StatusTests.cs
git commit -m "feat(tray): show lecture and meeting jobs with a badge"
```

---

### Task 11: Docs, including the `moss_max_new` check

No per-mode MOSS setting exists (`moss_max_new` and `coarse_max_seconds` apply to every recording, `pipeline.py` `transcribe_spans`), and fixtures cannot measure truncation, so this is a documented known gap rather than a code change.

**Files:**
- Modify: `README.md` (after the meetings bullet ending at :164)
- Modify: `src/second_brain/ARCHITECTURE.md` (receipt → job → note steps 2 and 5, :13-25 and :37-41)
- Modify: `SYSTEM.md` (receiver section after item 6 ending at :170; retention bullet :236-242; vault output bullet :346-350; file table after :423; known gaps after the "Reinstall settings" bullet ending at :518)
- Modify: `docs/install-windows.md` (:88-96)

Line numbers below refer to each file before this task's edits; locate each spot by the quoted text, or edit bottom-up.

- [ ] **Step 1: README** — insert after line 164 (`installer grants the service write access to \`meetings_vault_folder\`.`):

```markdown
- **Lecture mode** (firmware triple-click, solid yellow LED). The receiver tags those recordings
  `"mode": "lecture"`. The service transcribes them with MOSS only (no VibeVoice refinement, no
  Hermes scoring, no meeting dedupe) and writes `<job>.md` under `lectures_vault_folder` (default
  `Omi/Lectures`): the whole transcript with one `### m:ss` section per conversation window. The
  memory gate never filters a lecture; `[AudioBook]` exclusions still apply, and a recording with
  no speech publishes nothing. When Hermes is usable (`hermes_url` set and `no_hermes` off; the
  knowledge router need not be enabled) the service also writes `<job> Summary.md`: Hermes
  summarizes each section, then combines them. The result is saved in the job's
  `lecture-summary.json` before the note, so replays are identical; a Hermes outage only delays
  the summary, and `retry` requeues summaries that exhausted their attempts. Lectures never reach
  the knowledge router or daily notes. Lecture audio is deleted after processing like other Omi
  audio. The installer grants the service write access to `lectures_vault_folder`.
```

- [ ] **Step 2: ARCHITECTURE.md** — append to step 2 (after line 25, `between ASR and windowing.`):

```markdown
   Discovery stores each job's kind (`omi`, `meeting`, `lecture`; `second_brain.kinds`) in its
   metadata; Omi recordings the receiver tagged `"mode": "lecture"` become lecture jobs, which run
   the pipeline with --lecture (MOSS only, no Hermes scoring, no dedupe).
```

and append to step 5 (after line 41, `publication; a crash in between safely repeats it.`):

```markdown
   Meetings and lectures publish every window with speech; speaker exclusions still apply.
   A lecture's summary is a separate queue (`data/lectures.sqlite3`, RouterQueue backoff): the
   Hermes map/reduce result is frozen in `jobs/<id>/lecture-summary.json`, then the summary note
   is written. Lectures are never routed.
```

- [ ] **Step 3: SYSTEM.md** — insert after line 170 (end of receiver item 6):

```markdown
7. Lecture mode (2026-10-21, spec `docs/superpowers/specs/2026-10-21-lecture-mode-design.md`):
   the receiver writes `"mode"` into each Omi sidecar, and discovery stores the job kind
   (`second_brain.kinds.job_kind`: `omi`, `meeting`, `lecture`). Lecture jobs run the pipeline
   with `--lecture`: MOSS tier forced, Hermes scoring off, `--meeting`/`--meeting-dedupe` refused,
   windows kept. They never wait for or dedupe against meetings. `vault.publish_lecture` writes
   `<lectures_vault_folder>/<job>.md` (every spoken window, `### m:ss` sections). When
   `summarizing()` allows it (no `no_hermes`, a `hermes_url`), the job is queued in
   `data/lectures.sqlite3`; `summarize_lectures` maps each section and reduces through
   `router.chat`, freezes `lecture-summary.json`, then writes `<job> Summary.md`. Lectures are
   never routed and produce no daily notes; the job result's `filtered` is 0. The public status
   carries each job's `kind` and `lecture_summaries` counts; the tray shows a badge.
```

In the retention bullet (lines 236-242), after `startup sweep completes interrupted deletions.` insert: `Lecture recordings are Omi recordings and are deleted the same way.`

After the vault output bullet (ending line 350, `Later speaker corrections update review/future notes, not already published notes.`) add:

```markdown
- Lecture output: `Omi/Lectures/<job-id>.md` (`type: omi-lecture`, link to the summary) and
  `Omi/Lectures/<job-id> Summary.md` (`type: omi-lecture-summary`, link back). Same atomic,
  conflict-raising writer; the summary renders only from the frozen `lecture-summary.json`.
```

After the table row on line 423 (`data/jobs/<id>/manifest.json`, `publication-manifest.json`) add:

```markdown
| `data/lectures.sqlite3`, `data/jobs/<id>/lecture-summary.json` | Lecture summary queue and frozen Hermes summary |
```

After the "Reinstall settings, fixed 2026-10-20" bullet (ending line 518) add:

```markdown
- Lecture mode, known limits (2026-10-21): verified only with fixtures (fake MOSS, fake Hermes).
  Unverified: real MOSS on long, dense lecture speech, in particular truncation by
  `moss_max_new` (2048 new tokens) per `coarse_max_seconds` (240 s) chunk. A truncated chunk
  shows as a `coarse/c*.json` whose last segment ends well before the chunk's end while the
  audio continues. There is no per-mode setting; if it truncates, raise `moss_max_new` or lower
  `coarse_max_seconds` in pipeline.json (both affect every recording). Summary readability is
  unjudged. Summaries are queued only when Hermes is usable at publication; enabling Hermes later
  does not summarize earlier lectures. The example `hermes_url` placeholder counts as usable, so
  with it and `no_hermes` off every summary fails visibly (`lecture_summaries.failed`) after its
  backoff. A lecture overlapping a meeting capture is not deduped; both notes keep the speech.
  Sections past one hour are headed `h:mm:ss`. Lecture windows still record the memory gate in
  the manifest; only the kind keeps them out of routing.
```

- [ ] **Step 4: install-windows.md** — in lines 93-96 add `lectures_vault_folder` to the defaults and the grant:

```markdown
`port`, `ffmpeg_dir` and `skip_vibe7` from its arguments, and fills `vault_folder`,
`lectures_vault_folder`, `host`, `poll_seconds`, `retry_seconds`, `max_attempts`,
`job_timeout_seconds` and `no_hermes` with defaults only when they are missing. The service is
granted write access to the configured `vault_folder`, `meetings_vault_folder` (also while
meetings are off) and `lectures_vault_folder`.
```

- [ ] **Step 5: Commit**

```bash
git add README.md src/second_brain/ARCHITECTURE.md SYSTEM.md docs/install-windows.md
git commit -m "docs(lecture): lecture jobs, summary queue, settings and known limits"
```

---

### Task 12: Full verification

- [ ] **Step 1:** Run: `python3 scripts/test.py`
Expected: both suites pass (repo `tests/` and receiver `omi/firmware/scripts/omi-local/tests`), plus Swift tests on macOS.

- [ ] **Step 2:** Run: `git status --short`
Expected: only the user's pre-existing changes (for example `.nodeterm/project.json`) remain uncommitted.

- [ ] **Step 3:** Report which checks were not performed: the Windows tray/status tests and smoke test (Task 10, Task 5 PowerShell) unless run on Windows or CI; real MOSS truncation and summary quality on a recorded lecture (needs the device and a real lecture, see the SYSTEM.md known gap).

---

## Self-review against the spec

- **Job kind (§3):** Task 1 (`job_kind`, `with_kind`), Task 2 (runtime, queue, meetings, speakers, bridge). `speaker_audio.py:32` checks the mic channel, not metadata, and is intentionally unchanged. Audio retention unchanged and tested (Task 7).
- **Pipeline `--lecture` (§3):** Task 4 forces MOSS, disables Hermes scoring, refuses dedupe flags, keeps windows and `final_transcript`/`final_segments`. The `moss_max_new` check is a SYSTEM.md known gap (Task 11) because no per-mode setting exists and fixtures cannot measure it.
- **Vault (§3):** config/example/installer/ACL in Task 5; transcript note in Task 6 (type, summary link, `## Transcript`, `### m:ss`, speaker-labelled lines from the frozen publication manifest, never gated, nothing for no speech); summary note in Task 8 (type, back link, map then reduce via `router.chat`); freezing, retry like routing, Hermes-disabled behavior in Task 9; never routed and no daily notes in Tasks 7 and 9.
- **Tray (§3):** `kind` in the queue snapshot (Task 3), `JobInfo.Kind`/`Badge` (Task 10), bridge export `lecture` (Task 2).
- **Tests (§3):** pipeline run (Task 4), note templates, idempotent republish, `NoteConflict` (Tasks 6, 8), summary freezing and replay with a fake Hermes (Tasks 8, 9), end to end with exactly one note pair, nothing in Conversations, nothing routed (Tasks 7, 9), config validation (Task 5).
- **Rollout (§4):** this plan follows plan 1 and ships before firmware `.20`; docs move with the code (Task 11).
- **Names used consistently:** `job_kind`, `with_kind`, `spoken_windows`, `publish_lecture`, `publish_lecture_summary`, `SummaryQueue`, `SECTION_PROMPT`, `COMBINE_PROMPT`, `summarize`, `summarizing`, `summarize_lecture`, `summarize_lectures`, `lectures_vault_folder`, `lecture-summary.json`, `data/lectures.sqlite3`, status key `lecture_summaries`.
- **Deliberate choices where the spec is silent or loose:**
  - The spec says "no speech publishes nothing, like meetings", but meetings publish a "No speech was transcribed." note. This plan follows "publishes nothing".
  - "Hermes disabled" means `no_hermes` is on or `hermes_url` is empty. `router_enabled` is not required.
  - Summaries are queued only when Hermes is usable at publication time, as routing does.
  - The pipeline test runs `--lecture` without `--no-hermes`, with scoring enabled against a closed port. That proves `--lecture` alone stops scoring, which is stronger than the spec's `--lecture --no-hermes`.
