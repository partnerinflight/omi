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

    def test_bridge_never_exports_a_lecture(self):
        # Lectures stay in the vault's lectures folder only (owner decision, 2026-10-21).
        from second_brain.bridge import Bridge
        job_id = "e" * 64
        write_json(self.cfg.data_dir / "jobs" / job_id / "publication-manifest.json", {"windows": [
            {"id": "w0000", "start": 0, "end": 30, "final_transcript": "Entropy is defined.", "memory_keep": True,
             "route_to_knowledge_router": True}]})
        bridge = Bridge(self.cfg.data_dir, self.cfg.vault_path, "http://127.0.0.1:9", "token")
        self.assertEqual(bridge.sources({job_id: {"sha256": "0" * 64, "metadata": json.dumps(
            {"kind": "lecture", "first_utc": "2026-10-21T15:00:00Z"})}}, set()), [])


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

    def test_a_status_refresh_reads_and_logs_a_broken_pipeline_config_once(self):
        runtime = self.runtime(no_hermes=False)
        runtime.cfg.pipeline_config.write_text("{broken")
        with self.assertLogs("second_brain", "ERROR") as logs:
            runtime.status()
        self.assertEqual(len([r for r in logs.records if "Pipeline config unreadable" in r.getMessage()]), 1)
        status = json.loads(runtime.cfg.status_file.read_text())
        self.assertEqual((status["router"]["enabled"], status["lecture_summaries"]["enabled"]), (False, False))

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

    def attempts(self, runtime):
        with runtime.summaries.connect() as db:
            return db.execute("SELECT state,attempts,error FROM routes").fetchone()

    def test_an_interrupted_summary_stays_pending_without_spending_an_attempt(self):
        from second_brain import lectures, router
        runtime = self.runtime(no_hermes=False)
        runtime.summaries.enqueue(JOB, "2026-10-21T15:00:00Z")
        root = runtime.cfg.data_dir / "jobs" / JOB
        calls = []

        def stop_after_first_section(cfg, system, user):
            calls.append(system)
            runtime.stop.set()  # the service begins stopping while Hermes answers
            return {"summary": "Section 1."}

        with patch.object(router, "chat", stop_after_first_section):
            asyncio.run(asyncio.wait_for(runtime.summarize_lectures(), 10))
        self.assertEqual(calls, [lectures.SECTION_PROMPT], "the second section is never requested")
        self.assertEqual(tuple(self.attempts(runtime)), ("pending", 0, None))
        self.assertEqual(json.loads((root / "lecture-summary.partial.json").read_text()), {"w0000": "Section 1."})
        self.assertFalse((root / "lecture-summary.json").exists())
        self.assertFalse((runtime.cfg.vault_path / "Omi" / "Lectures").exists())
        # After the restart only the remaining section and the combination are requested.
        runtime.stop.clear()
        fake = FakeSummarizer()
        with patch.object(router, "chat", fake):
            runtime.summarize_lecture({}, runtime.summaries.claim())
        self.assertEqual([system for system, _ in fake.calls], [lectures.SECTION_PROMPT, lectures.COMBINE_PROMPT])
        self.assertIn("Any questions?", fake.calls[0][1])
        frozen = json.loads((root / "lecture-summary.json").read_text())
        self.assertEqual([s["summary"] for s in frozen["sections"]], ["Section 1.", "Section 1."])
        self.assertFalse((root / "lecture-summary.partial.json").exists(), "the partial is removed once frozen")

    def test_a_failed_section_is_retried_from_that_section(self):
        from second_brain import lectures, router
        runtime = self.runtime()
        item = {"id": JOB, "job": JOB, "observed_at": "2026-10-21T15:00:00Z"}
        seen = []

        def fail_on_second_section(cfg, system, user):
            seen.append(user)
            if len(seen) == 2:
                raise OSError("Hermes down")
            return {"summary": "Section 1."}

        with patch.object(router, "chat", fail_on_second_section):
            with self.assertRaises(OSError):
                runtime.summarize_lecture({}, item)
        fake = FakeSummarizer()
        with patch.object(router, "chat", fake):
            self.assertEqual(runtime.summarize_lecture({}, item), {"notes": 1})
        self.assertEqual([system for system, _ in fake.calls], [lectures.SECTION_PROMPT, lectures.COMBINE_PROMPT])
        self.assertIn("Any questions?", fake.calls[0][1])
        self.assertNotIn("Today we define entropy.", "".join(user for _, user in fake.calls))

    def test_summaries_wait_without_spending_attempts_until_hermes_is_usable(self):
        from second_brain import router
        runtime = self.runtime()  # no_hermes
        runtime.summaries.enqueue(JOB, "2026-10-21T15:00:00Z")

        async def run_until(condition):
            runtime.stop = asyncio.Event()  # one per event loop
            task = asyncio.create_task(runtime.summarize_lectures())
            for _ in range(100):
                if condition():
                    break
                await asyncio.sleep(0.05)
            runtime.stop.set()
            await asyncio.wait_for(task, 10)

        polls = iter(range(5))
        asyncio.run(run_until(lambda: next(polls, None) is None))  # a few polls with Hermes off
        self.assertEqual(tuple(self.attempts(runtime)), ("pending", 0, None))
        # Enabling Hermes later summarizes the lecture queued while it was off.
        runtime.cfg = dataclasses.replace(runtime.cfg, no_hermes=False)
        with patch.object(router, "chat", FakeSummarizer()):
            asyncio.run(run_until(lambda: runtime.summaries.snapshot()["done"]))
        self.assertEqual(runtime.summaries.snapshot()["done"], 1)
        self.assertTrue((runtime.cfg.vault_path / "Omi" / "Lectures" / f"{JOB} Summary.md").is_file())

    def test_a_published_lecture_is_always_queued_and_counts_excluded_windows_as_filtered(self):
        runtime = self.runtime()  # no_hermes: the summary waits in the queue until Hermes is usable
        audio = runtime.cfg.incoming_dir / "lecture.opus"
        audio.write_bytes(b"lecture audio")
        runtime.queue.enqueue(audio, {"device": "112233445566", "start_seq": 1, "mode": "lecture",
                                      "kind": "lecture", "first_utc": "2026-10-21T15:00:00Z"})
        job = runtime.queue.claim()
        root = runtime.cfg.data_dir / "jobs" / job["id"]
        write_json(root / "manifest.json", lecture_manifest())  # a replay: no pipeline run
        result = asyncio.run(runtime.process(job))
        # Only the [AudioBook] window is excluded; the gate's memory_keep=False never filters a lecture.
        self.assertEqual((result["notes"], result["filtered"], result["windows"]), (1, 1, 4))
        self.assertEqual(runtime.summaries.snapshot()["pending"], 1)
        self.assertEqual(runtime.summaries.claim()["job"], job["id"])

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
        # Hermes off: the summary is queued and waits without spending attempts.
        self.assertEqual(self.runtime.summaries.snapshot()["pending"], 1)
        with self.runtime.summaries.connect() as db:
            self.assertEqual(db.execute("SELECT attempts FROM routes").fetchone()[0], 0)
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


if __name__ == "__main__":
    unittest.main()
