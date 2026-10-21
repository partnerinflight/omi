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


if __name__ == "__main__":
    unittest.main()
