from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from omi_local.server import UploadServer, load_or_create_secret
from .config import Config, read_json
from .io import InstanceLock, write_json
from .queue import Queue
from .receiver import ReceiverFactory
from .vault import publish
from .speakers import Speakers

log = logging.getLogger("second_brain")
STAGES = {"segmentation", "transcribing", "scoring", "refining", "routing", "publishing", "speakers"}


async def kill_tree(proc):
    if proc.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(proc.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await proc.wait()


class Runtime:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.queue = Queue(cfg.data_dir / "queue.sqlite3")
        self.stop = asyncio.Event()
        self.server = None
        self.receiver_error = None
        self.last_scan_error = None
        self.started = time.time()
        self.speakers = Speakers(cfg.data_dir, cfg.review_dir, cfg.speaker_match_threshold, cfg.speaker_match_margin)

    def discover(self):
        self.last_scan_error = None
        for receipt in (self.cfg.incoming_dir / ".ready").glob("*.json"):
            try:
                value = read_json(receipt)
                audio = Path(value["audio"]).resolve()
                if not audio.is_relative_to(self.cfg.incoming_dir.resolve()):
                    raise ValueError("Receipt path escapes incoming directory")
                if not self.queue.known(audio):
                    self.queue.enqueue(audio, value["metadata"])
            except (OSError, ValueError, KeyError) as e:
                self.last_scan_error = f"Recording discovery failed ({type(e).__name__}); check private service log"
                log.exception("Could not discover recording receipt %s", receipt.name)

    async def process(self, job):
        cfg = self.cfg
        root = cfg.data_dir / "jobs" / job["id"]
        root.mkdir(parents=True, exist_ok=True)
        manifest_path = root / "manifest.json"
        if not manifest_path.exists():
            with Path(job["audio"]).open("rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            if digest != job["sha256"]:
                raise ValueError("Source audio changed after it was queued")
            attempt = root / f"attempt-{job['attempts']}"
            attempt.mkdir(exist_ok=True)
            pipeline_cfg = cfg.pipeline()
            pipeline_cfg["work_root"] = str(attempt)
            write_json(attempt / "config.json", pipeline_cfg)
            cmd = [
                sys.executable,
                "-u",
                "-m",
                "second_brain.adaptive.pipeline",
                "--audio",
                job["audio"],
                "--config",
                str(attempt / "config.json"),
                "--output-dir",
                str(attempt),
                "--progress-file",
                str(attempt / "progress.json"),
            ]
            if cfg.skip_vibe7:
                cmd.append("--skip-vibe7")
            if cfg.no_hermes:
                cmd.append("--no-hermes")
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            if cfg.ffmpeg_dir:
                env["PATH"] = cfg.ffmpeg_dir + os.pathsep + env.get("PATH", "")
            kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x08000000}
            with (attempt / "pipeline.log").open("wb") as output:
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=output, stderr=output, env=env, **kwargs)
                deadline = time.monotonic() + cfg.job_timeout_seconds
                try:
                    while proc.returncode is None:
                        if self.stop.is_set():
                            raise InterruptedError("Service is stopping")
                        if time.monotonic() > deadline:
                            raise TimeoutError("Audio processing exceeded its configured time limit")
                        try:
                            stage = read_json(attempt / "progress.json")["stage"]
                            if stage in STAGES:
                                self.queue.progress(job["id"], stage)
                        except (OSError, ValueError, KeyError):
                            pass
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=0.5)
                        except asyncio.TimeoutError:
                            pass
                    if proc.returncode:
                        raise RuntimeError(
                            f"Audio pipeline exited with code {proc.returncode}; see private pipeline.log"
                        )
                finally:
                    await kill_tree(proc)
            manifest = read_json(attempt / "manifest.json")
            if not isinstance(manifest.get("windows"), list):
                raise ValueError("Pipeline returned an invalid manifest")
            write_json(manifest_path, manifest)
        manifest = read_json(manifest_path)
        await asyncio.to_thread(self.speakers.ingest, job, manifest)
        # Freeze names once for publication so later name corrections cannot
        # turn a crash-replay into an edited-note conflict.
        publication = root / "publication-manifest.json"
        if not publication.exists():
            write_json(publication, self.speakers.render(job["id"], manifest))
        manifest = read_json(publication)
        self.queue.progress(job["id"], "publishing")
        notes = await asyncio.to_thread(publish, cfg.vault_path, cfg.vault_folder, job, manifest)
        return {
            "notes": len(notes),
            "warnings": len(manifest.get("fallbacks", [])),
            "filtered": sum(w.get("memory_keep") is not True for w in manifest["windows"]),
            "windows": len(manifest["windows"]),
        }

    async def worker(self):
        while not self.stop.is_set():
            await asyncio.to_thread(self.discover)
            job = self.queue.claim()
            if job:
                try:
                    result = await self.process(job)
                    self.queue.complete(job["id"], result)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("Processing failed for job %s", job["id"])
                    # Never copy model output, transcripts, filesystem paths or credentials into public status.
                    stage = (self.queue.snapshot()["current"] or {}).get("stage", "processing")
                    error = f"{stage}: {type(e).__name__}; see private job log"
                    self.queue.fail(job, error, self.cfg.retry_seconds, self.cfg.max_attempts)
            else:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=self.cfg.poll_seconds)
                except asyncio.TimeoutError:
                    pass

    def status(self, state="running"):
        result = self.queue.snapshot()
        result.update(
            version=1,
            service=state,
            heartbeat=time.time(),
            started=self.started,
            receiver={
                "listening": self.server is not None,
                "port": self.server.bound_port if self.server else self.cfg.port,
                "active_uploads": len(self.server._busy) if self.server else 0,
                "sessions_ok": self.server.sessions_ok if self.server else 0,
                "sessions_failed": self.server.sessions_failed if self.server else 0,
                "error": self.receiver_error,
            },
            discovery_error=self.last_scan_error,
            speakers=self.speakers.summary(),
        )
        write_json(self.cfg.status_file, result)

    async def heartbeat(self):
        while not self.stop.is_set():
            await asyncio.to_thread(self.status)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def speaker_review(self):
        while not self.stop.is_set():
            try:
                await asyncio.to_thread(self.speakers.tick)
            except OSError:
                log.exception("Speaker review mailbox unavailable")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def run(self):
        with InstanceLock(self.cfg.data_dir / "worker.lock"):
            self.queue.recover()
            factory = ReceiverFactory(self.cfg.incoming_dir)
            factory.recover_receipts()
            secret = load_or_create_secret(self.cfg.secret_file, create=False)
            self.server = UploadServer(
                secret, self.cfg.incoming_dir, self.cfg.host, self.cfg.port, writer_factory=factory
            )
            await self.server.start()
            tasks = [
                asyncio.create_task(self.worker()),
                asyncio.create_task(self.heartbeat()),
                asyncio.create_task(self.speaker_review()),
            ]
            waiter = asyncio.create_task(self.stop.wait())
            try:
                done, _ = await asyncio.wait([*tasks, waiter], return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task  # propagate unexpected worker/status failure to SCM recovery
            finally:
                self.stop.set()
                await self.server.close()
                await asyncio.gather(*tasks, return_exceptions=True)
                waiter.cancel()
                self.server = None
                await asyncio.to_thread(self.status, "stopped")
