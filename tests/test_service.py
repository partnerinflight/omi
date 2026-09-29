from __future__ import annotations
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from second_brain.io import InstanceLock, write_json
from second_brain.queue import Queue
from second_brain.runtime import Runtime, kill_tree
from second_brain.vault import NoteConflict, publish
from second_brain.adaptive.pipeline import parse_vibe_text
from second_brain.adaptive.runners.moss_cpp_runner import normalize_segments
from tests.helpers import configuration, records, upload, SECRET
from omi_local import upload_protocol as U


class QueueAndVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = configuration(self.root)
        self.audio = self.root / "incoming" / "sample.opus"
        self.audio.write_bytes(b"audio")
        self.queue = Queue(self.root / "queue.db")
        self.meta = {"device": "test", "start_seq": 0, "first_utc": "2026-09-28T17:00:00Z"}
        self.key = self.queue.enqueue(self.audio, self.meta)

    def test_queue_restart_and_idempotent_delivery(self):
        self.assertEqual(self.key, self.queue.enqueue(self.audio, self.meta))
        job = self.queue.claim()
        self.assertIsNone(self.queue.claim())
        self.queue.recover()
        recovered = self.queue.claim()
        self.assertEqual(job["id"], recovered["id"])
        manifest = {
            "windows": [
                dict(
                    id="w0000",
                    route_to_knowledge_router=True,
                    memory_keep=True,
                    final_transcript="S1: We decided to launch the project.",
                    start=0,
                    end=5,
                    final_engine="moss",
                ),
                dict(id="w0001", route_to_knowledge_router=False, memory_keep=False),
            ]
        }
        first = publish(self.cfg.vault_path, self.cfg.vault_folder, recovered, manifest)
        self.assertEqual(first, publish(self.cfg.vault_path, self.cfg.vault_folder, recovered, manifest))
        self.assertEqual(len(list(self.cfg.vault_path.rglob("*.md"))), 1)
        note = self.cfg.vault_path / first[0]
        note.write_text("human edit")
        with self.assertRaises(NoteConflict):
            publish(self.cfg.vault_path, self.cfg.vault_folder, recovered, manifest)
        self.assertEqual(note.read_text(), "human edit")

    def test_failed_jobs_are_visible_and_explicitly_retryable(self):
        job = self.queue.claim()
        self.queue.fail(job, "engine unavailable", 0.01, 1)
        self.assertEqual(self.queue.snapshot()["counts"]["failed"], 1)
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.queue.retry_failed(), 1)
        self.assertEqual(self.queue.claim()["attempts"], 1)

    def test_second_worker_cannot_take_same_data_directory(self):
        path = self.root / "instance.lock"
        for contents in (b"", b"0"):
            with self.subTest(contents=contents):
                path.write_bytes(contents)
                rejected = InstanceLock(path)
                with InstanceLock(path) as owner:
                    with self.assertRaisesRegex(RuntimeError, "Another Second Brain worker"):
                        with rejected:
                            self.fail("Second worker acquired an owned directory")
                    self.assertIsNone(rejected.file)
                    self.assertFalse(owner.file.closed)
                self.assertIsNone(owner.file)
                with rejected:
                    self.assertFalse(rejected.file.closed)

    def test_worker_lock_excludes_other_process_and_releases_on_exit(self):
        path = self.root / "instance.lock"
        code = """
import sys
from pathlib import Path
from second_brain.io import InstanceLock
try:
    with InstanceLock(Path(sys.argv[1])):
        print('acquired')
except RuntimeError:
    print('owned')
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        def attempt():
            return subprocess.check_output(
                [sys.executable, "-c", code, str(path)], env=env, text=True, timeout=15
            ).strip()
        with InstanceLock(path):
            self.assertEqual(attempt(), "owned")
        self.assertEqual(attempt(), "acquired")
        with InstanceLock(path):
            pass

    def test_vibe_padding_is_excluded_and_unknown_schema_is_error(self):
        ref = {
            "context_start": 10,
            "segments": [
                dict(approx_start=0, approx_end=2, speaker=0, text="outside"),
                dict(approx_start=5, approx_end=7, speaker=0, text="inside"),
            ],
        }
        self.assertEqual(parse_vibe_text(ref, 14, 18, "w1"), "w1:0: inside")
        with self.assertRaises(ValueError):
            normalize_segments({"unexpected": "schema"})


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class StdinControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_processing_completes_with_supervisor_pipe_open(self):
        from dataclasses import asdict

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = configuration(root)
            settings = root / "service.json"
            write_json(settings, {k: str(v) if isinstance(v, Path) else v for k, v in asdict(cfg).items()})
            env = os.environ.copy()
            repo = Path(__file__).resolve().parents[1]
            env["PYTHONPATH"] = os.pathsep.join([str(repo / "src"), str(repo / "omi/firmware/scripts/omi-local")])
            kwargs = {"start_new_session": True} if os.name != "nt" else {}
            with (root / "worker.log").open("wb") as output:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "second_brain.cli", "run", "--config", str(settings), "--stdin-control",
                    stdin=asyncio.subprocess.PIPE, stdout=output, stderr=output, env=env, **kwargs,
                )
                try:
                    async with asyncio.timeout(30):
                        while not cfg.status_file.exists():
                            self.assertIsNone(proc.returncode, (root / "worker.log").read_text())
                            await asyncio.sleep(0.05)
                        status = json.loads(cfg.status_file.read_text())
                        await upload(status["receiver"]["port"], records())
                        while True:
                            status = json.loads(cfg.status_file.read_text())
                            if status["counts"].get("complete"):
                                break
                            self.assertFalse(status["counts"].get("failed"), status)
                            await asyncio.sleep(0.05)
                    self.assertEqual(len(list(cfg.vault_path.rglob("*.md"))), 1)
                    proc.stdin.write(b"stop\n")
                    await proc.stdin.drain()
                    await asyncio.wait_for(proc.wait(), 10)
                    self.assertEqual(proc.returncode, 0)
                finally:
                    await kill_tree(proc)
                    proc.stdin.close()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = configuration(self.root)
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

    async def test_authenticated_upload_to_real_pipeline_and_vault(self):
        port = self.runtime.server.bound_port
        self.assertEqual(await upload(port, records(marker=False)), U.MSG_BYE)
        await asyncio.sleep(0.1)
        self.assertEqual(self.runtime.queue.snapshot()["counts"], {})  # unfinished recording stays resumable
        self.assertEqual(await upload(port, records()), U.MSG_BYE)
        for _ in range(300):
            state = self.runtime.queue.snapshot()
            if state["counts"].get("complete") or state["counts"].get("failed"):
                break
            await asyncio.sleep(0.05)
        logs = "\n".join(p.read_text()[-2500:] for p in (self.root / "data/jobs").rglob("pipeline.log"))
        self.assertEqual(state["counts"].get("complete"), 1, str(state) + logs)
        notes = list(self.cfg.vault_path.rglob("*.md"))
        self.assertEqual(len(notes), 1)
        text = notes[0].read_text()
        self.assertIn("c0000:S1", text)
        self.assertIn("c0000:S2", text)
        self.assertIn("Source audio", text)
        self.assertIn("project launch", text)
        # The tray can listen and name speakers without reading private jobs.
        import uuid
        import wave

        for _ in range(100):
            catalog = json.loads((self.cfg.review_dir / "catalog.json").read_text())
            if catalog["speakers"]:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(len(catalog["speakers"]), 2)
        speaker = catalog["speakers"][0]
        with wave.open(str(self.cfg.review_dir / "clips" / speaker["clips"][0]["file"])) as clip:
            self.assertEqual(clip.getframerate(), 16000)
            self.assertGreater(clip.getnframes(), 0)
        request_id = str(uuid.uuid4())
        write_json(
            self.cfg.review_dir / "requests" / f"{request_id}.json",
            dict(id=request_id, action="assign", observation=speaker["id"], name="Test Person"),
        )
        response = self.cfg.review_dir / "responses" / f"{request_id}.json"
        for _ in range(100):
            if response.exists():
                break
            await asyncio.sleep(0.02)
        self.assertTrue(json.loads(response.read_text())["ok"])
        self.assertNotIn("Test Person", json.dumps(self.runtime.queue.snapshot()))
        self.runtime.discover()
        self.assertEqual(self.runtime.queue.snapshot()["counts"]["complete"], 1)
        # Crash/restart publication replay uses the saved manifest, not another ASR run.
        with self.runtime.queue.connect() as db:
            db.execute("UPDATE jobs SET state='processing'")
        self.runtime.queue.recover()
        for _ in range(100):
            if self.runtime.queue.snapshot()["counts"].get("complete") == 1:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(len(list(self.cfg.vault_path.rglob("*.md"))), 1)
        self.assertEqual(len(list((self.root / "data/jobs").rglob("pipeline.log"))), 1)
        self.assertEqual(notes[0].read_text(), text)  # later naming doesn't overwrite an already published note

    async def test_bad_pairing_key_never_creates_a_job(self):
        self.assertEqual(await upload(self.runtime.server.bound_port, records(), bytes(reversed(SECRET))), U.MSG_REJECT)
        await asyncio.sleep(0.05)
        self.assertEqual(self.runtime.queue.snapshot()["counts"], {})
        self.assertFalse(list(self.cfg.vault_path.rglob("*.md")))

    async def test_failed_engine_retries_without_stopping_receiver(self):
        pipeline = json.loads(self.cfg.pipeline_config.read_text())
        pipeline["moss_command"] = ["missing-moss-executable"]
        write_json(self.cfg.pipeline_config, pipeline)
        await upload(self.runtime.server.bound_port, records())
        for _ in range(200):
            if self.runtime.queue.snapshot()["counts"].get("failed"):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.runtime.queue.snapshot()["counts"].get("failed"), 1)
        self.assertTrue(self.runtime.server._server.is_serving())
        self.assertFalse(list(self.cfg.vault_path.rglob("*.md")))

    async def test_rejected_duplicate_does_not_release_active_device(self):
        import secrets
        from omi_local import protocol as P
        from tests.helpers import receive, DEVICE

        reader, writer = await asyncio.open_connection("127.0.0.1", self.runtime.server.bound_port)
        try:
            cn = secrets.token_bytes(16)
            writer.write(U.frame(U.MSG_HELLO, U.encode_hello(DEVICE, cn)))
            await writer.drain()
            _, challenge = await receive(reader)
            info = P.Info(0, 0, 10000, 0, P.RECORD_SIZE, P.CODEC_ID_OPUS)
            writer.write(
                U.frame(U.MSG_AUTH, U.encode_auth(U.auth_tag(SECRET, U.LABEL_CLIENT, cn, challenge[:16]), info))
            )
            await writer.drain()
            kind, _ = await receive(reader)
            self.assertEqual(kind, U.MSG_START)
            self.assertEqual(await upload(self.runtime.server.bound_port, records()), U.MSG_REJECT)
            self.assertIn("11-22-33-44-55-66", self.runtime.server._busy)
            self.assertEqual(await upload(self.runtime.server.bound_port, records()), U.MSG_REJECT)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_model_timeout_keeps_receiver_alive(self):
        from dataclasses import replace
        import sys

        self.runtime.cfg = replace(self.cfg, job_timeout_seconds=0.1, max_attempts=1)
        pipeline = json.loads(self.cfg.pipeline_config.read_text())
        pipeline["moss_command"] = [sys.executable, "-c", "import time; time.sleep(60)"]
        write_json(self.cfg.pipeline_config, pipeline)
        await upload(self.runtime.server.bound_port, records())
        for _ in range(100):
            state = self.runtime.queue.snapshot()
            if state["counts"].get("failed"):
                break
            await asyncio.sleep(0.05)
        self.assertEqual(state["counts"].get("failed"), 1)
        self.assertIn("TimeoutError", state["recent"][0]["error"])
        self.assertTrue(self.runtime.server._server.is_serving())
