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
from .io import InstanceLock, utc_from_ms, write_json
from .kinds import job_kind, with_kind
from .meetings import overlapping, recording_span
from .queue import Queue
from .receiver import MeetingStore, ReceiverFactory
from .router import RouterQueue, route
from .lectures import SummaryQueue, summarize
from .vault import publish, publish_lecture, publish_lecture_summary, publish_meeting
from .speakers import Speakers
from .clarifications import Clarifications
from . import bridge

log = logging.getLogger("second_brain")
_READ = object()  # routing()/summarizing(): read the pipeline config themselves
STAGES = {"segmentation", "transcribing", "scoring", "refining", "routing", "publishing", "speakers"}
MEETING_WAIT_SECONDS = 300  # recheck an Omi job whose meeting is still uploading
AUDIO_SUFFIXES = {".opus", ".m4a", ".caf", ".wav", ".ogg", ".mp3", ".flac"}


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
        self.speakers.rematch()
        self.router = RouterQueue(cfg.data_dir / "router.sqlite3")
        self.summaries = SummaryQueue(cfg.data_dir / "lectures.sqlite3")
        self.clarifications = Clarifications(cfg.data_dir, cfg.review_dir, cfg.vault_path)
        self.bridge_status = {"enabled": bool(cfg.bridge_url), "last_success": None, "error": None}

    def pipeline_or_none(self):
        """The pipeline config, or None (logged) when it is unreadable."""
        try:
            return self.cfg.pipeline()
        except (OSError, ValueError):
            log.exception("Pipeline config unreadable; routing and lecture summaries paused")
            return None

    def routing(self, pipeline=_READ):
        """The pipeline config when the knowledge router should run, else None. Pass an already
        read config (or None) to avoid reading and logging it twice."""
        if pipeline is _READ:
            pipeline = self.pipeline_or_none()
        if pipeline is None or not pipeline.get("router_enabled") or self.cfg.no_hermes or not pipeline.get("hermes_url"):
            return None
        return pipeline

    def summarizing(self, pipeline=_READ):
        """The pipeline config when lecture summaries can run, else None. Unlike routing this
        needs no router_enabled: lecture content never reaches the knowledge router."""
        if pipeline is _READ:
            pipeline = self.pipeline_or_none()
        if pipeline is None or self.cfg.no_hermes or not pipeline.get("hermes_url"):
            return None
        return pipeline

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
                metadata = with_kind(value["metadata"])
                if metadata["kind"] == "meeting":
                    # Speaker review, routing and notes read the start time from first_utc.
                    metadata = {**metadata, "first_utc": utc_from_ms(metadata["start_ms"])}
                if not self.queue.known(audio):
                    self.queue.enqueue(audio, metadata)
            except (OSError, ValueError, KeyError, TypeError, OverflowError) as e:
                self.last_scan_error = f"Recording discovery failed ({type(e).__name__}); check private service log"
                log.exception("Could not discover recording receipt %s", receipt.name)

    def purge_audio(self, job_id, audio):
        """Delete a completed recording's audio once its note is published (delete_audio_after_processing).
        Transcripts, manifests and logs stay; failed or pending jobs are never passed here."""
        if not self.cfg.delete_audio_after_processing:
            return
        incoming = self.cfg.incoming_dir.resolve()
        source = Path(audio).resolve()
        # Omi recordings only. Meeting captures stay: Omi dedupe correlates later jobs against
        # meeting audio (adaptive/dedupe.py).
        if source.is_relative_to(incoming) and not source.is_relative_to(incoming / "meetings"):
            # Receipt and sidecar first: the receiver republishes receipts from sidecars on startup.
            for receipt in (incoming / ".ready").glob("*.json"):
                try:
                    if Path(read_json(receipt)["audio"]).resolve() == source:
                        receipt.unlink(missing_ok=True)
                except (OSError, ValueError, KeyError):
                    continue
            source.with_suffix(".json").unlink(missing_ok=True)
            source.unlink(missing_ok=True)
        work = self.cfg.data_dir / "jobs" / job_id
        if work.is_dir():
            for path in work.rglob("*"):
                if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES:
                    path.unlink(missing_ok=True)

    def purge_audio_logged(self, job_id, audio):
        try:
            self.purge_audio(job_id, audio)
        except OSError:
            log.exception("Could not delete audio for completed job %s; the startup sweep retries", job_id)

    def purge_completed_audio(self):
        for job in self.queue.completed():
            self.purge_audio_logged(job["id"], job["audio"])

    def meeting_overlaps(self, job):
        """Meeting captures overlapping this Omi recording, or None when dedupe does not apply."""
        if not self.cfg.meetings_enabled:
            return None
        return overlapping(self.cfg.incoming_dir / "meetings", json.loads(job["metadata"]))

    def defer_for_meeting(self, job):
        """True when an overlapping capture is still open: the job waits rather than publishing
        speech the meeting note will also contain. Fails open: a broken check never blocks a job."""
        if (self.cfg.data_dir / "jobs" / job["id"] / "manifest.json").exists():
            return False                          # a replay ignores meetings; waiting is pointless
        try:
            found = self.meeting_overlaps(job)
        except Exception:
            log.exception("Meeting overlap check failed for job %s; processing without waiting", job["id"])
            return False
        if found and found.expired:
            log.info("Job %s: %d overlapping capture(s) open over 24 h; not waiting for them",
                     job["id"], len(found.expired))
        if not found or not found.open:
            return False
        log.info("Job %s waits for %d uploading meeting capture(s)", job["id"], len(found.open))
        try:
            self.queue.defer(job, MEETING_WAIT_SECONDS, "waiting-for-meeting")
        except Exception:
            log.exception("Could not defer job %s for its meeting; processing it now", job["id"])
            return False
        return True

    def log_dedupe_result(self, job_id, result):
        """Private log only: a failed alignment means the Omi note may duplicate the meeting note."""
        if result.get("alignment_failed"):
            log.warning("Job %s: meeting dedupe could not align (Omi clock may be off); nothing deduped", job_id)

    def meeting_index(self, job, root: Path):
        """Write the dedupe index for the pipeline, or None when there is nothing to compare."""
        found = self.meeting_overlaps(job)
        if not found or not found.closed:
            return None
        span = recording_span(json.loads(job["metadata"]))
        path = root / "meeting-dedupe.json"
        write_json(path, {"recording_epoch": int(span[0]), "captures": found.closed})
        return str(path)

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

    async def process(self, job):
        cfg = self.cfg
        root = cfg.data_dir / "jobs" / job["id"]
        root.mkdir(parents=True, exist_ok=True)
        manifest_path = root / "manifest.json"
        metadata = json.loads(job["metadata"])
        kind = job_kind(metadata)
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
            if kind == "meeting":
                cmd.append("--meeting")
            elif kind == "lecture":
                cmd.append("--lecture")
            # Only on a fresh run: a replay from a saved manifest never re-runs dedupe.
            index = self.dedupe_index_or_none(job, root, kind)
            if index:
                cmd += ["--meeting-dedupe", index]
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            if cfg.ffmpeg_dir:
                env["PATH"] = cfg.ffmpeg_dir + os.pathsep + env.get("PATH", "")
            kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x08000000}
            with (attempt / "pipeline.log").open("wb") as output:
                # Only the worker reads the supervisor's control pipe. Inheriting
                # its pending Windows read can stall a child's Python startup.
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=output, stderr=output, env=env, **kwargs
                )
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
            write_json(publication, self.speakers.render(job["id"], manifest, cfg.owner_name if kind == "meeting" else None))
        manifest = read_json(publication)
        self.queue.progress(job["id"], "publishing")
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
        if kind == "lecture":
            # Lectures stay in the lectures folder: never routed, so never in daily notes either.
            # Always queued, so enabling Hermes later still summarizes it; summarize_lectures holds
            # the item without spending attempts while Hermes is off. Idempotent on crash-replay.
            if notes:
                self.summaries.enqueue(job["id"], metadata.get("first_utc", "unknown-time"))
        elif self.routing():
            # Idempotent, so a crash-replay of this job enqueues nothing twice.
            kept = [w for w in manifest["windows"]
                    if w.get("memory_keep") is True and w.get("route_to_knowledge_router") is True]
            self.router.enqueue(job["id"], kept, metadata.get("first_utc") or "unknown")
        return {
            "notes": len(notes),
            "warnings": len(manifest.get("fallbacks", [])),
            # A lecture ignores the memory gate: only speaker exclusions ([AudioBook]) filter windows.
            "filtered": sum(bool(w.get("speaker_filter")) for w in manifest["windows"]) if kind == "lecture"
            else sum(w.get("memory_keep") is not True for w in manifest["windows"]),
            "windows": len(manifest["windows"]),
            **({"deduped": manifest["dedupe"]["dropped_count"],
                "alignment_failed": manifest["dedupe"]["alignment_failed"]}
               if "dedupe" in manifest else {}),
        }

    async def worker(self):
        while not self.stop.is_set():
            await asyncio.to_thread(self.discover)
            job = self.queue.claim()
            if job and await asyncio.to_thread(self.defer_for_meeting, job):
                job = None
            if job:
                try:
                    result = await self.process(job)
                    self.queue.complete(job["id"], result)
                    self.log_dedupe_result(job["id"], result)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("Processing failed for job %s", job["id"])
                    # Never copy model output, transcripts, filesystem paths or credentials into public status.
                    stage = (self.queue.snapshot()["current"] or {}).get("stage", "processing")
                    error = f"{stage}: {type(e).__name__}; see private job log"
                    self.queue.fail(job, error, self.cfg.retry_seconds, self.cfg.max_attempts)
                else:
                    # Only after the completion is recorded; a deletion error never fails a finished job.
                    await asyncio.to_thread(self.purge_audio_logged, job["id"], job["audio"])
            else:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=self.cfg.poll_seconds)
                except asyncio.TimeoutError:
                    pass

    async def route_published(self):
        """Send published conversations to the knowledge router, one at a time, separately from
        transcription so a Hermes outage only delays routing."""
        while not self.stop.is_set():
            item = None
            try:
                pipeline = await asyncio.to_thread(self.routing)
                item = self.router.claim() if pipeline else None
                if item:
                    result = await asyncio.to_thread(
                        route, pipeline, self.cfg.vault_path, item["id"], item["text"], item["observed_at"], self.clarifications)
                    self.router.done(item["id"], result)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Routing failed for %s", item["id"] if item else "the router")
                if item:
                    self.router.fail(item, f"{type(e).__name__}; see private service log")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(self.cfg.poll_seconds, 1))
            except asyncio.TimeoutError:
                pass

    def summarize_lecture(self, pipeline, item):
        """Summarize one published lecture. Hermes's result is frozen before the note is written,
        so a crash replay writes byte-identical content and never asks Hermes again."""
        root = self.cfg.data_dir / "jobs" / item["job"]
        frozen = root / "lecture-summary.json"
        partial = root / "lecture-summary.partial.json"  # finished sections; never published
        if not frozen.exists():
            write_json(frozen, summarize(pipeline, read_json(root / "publication-manifest.json"),
                                         partial, self.stop.is_set))
        partial.unlink(missing_ok=True)
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
                if isinstance(e, InterruptedError) and self.stop.is_set():
                    break  # the service is stopping: the item stays pending and keeps its attempts
                log.exception("Lecture summary failed for %s", item["id"] if item else "the summarizer")
                if item:
                    self.summaries.fail(item, f"{type(e).__name__}; see private service log")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(self.cfg.poll_seconds, 1))
            except asyncio.TimeoutError:
                pass

    async def export(self):
        """Phone commands and shadow export to the server. Never raises: an unreachable or broken server must not
        stop the service (run() propagates task failures to SCM recovery)."""
        exporter = None

        def tick():
            nonlocal exporter
            if exporter is None:
                exporter = bridge.Bridge(self.cfg.data_dir, self.cfg.vault_path, self.cfg.bridge_url,
                                         bridge.load_token(Path(self.cfg.bridge_token_file)),
                                         review_dir=self.cfg.review_dir)
            # Phone commands first, so the export that follows already shows their effect.
            exporter.apply_commands(self.speakers, self.clarifications)
            conflicts = exporter.project() + (exporter.project_todos() == "conflict")
            self.bridge_status["export_conflicts"] = conflicts
            # Counts only: the server sees worker freshness and backlog, never content.
            return exporter.tick({"jobs": self.queue.snapshot()["counts"], "routes_pending": self.router.snapshot()["pending"],
                                  "export_conflicts": conflicts})

        while not self.stop.is_set():
            try:
                result = await asyncio.to_thread(tick)
                self.bridge_status.update(last_success=time.time(), error=None, sent=result["sent"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                error = bridge.safe_error(e)
                if error != self.bridge_status["error"]:  # one traceback per outage, not one a minute
                    log.exception("Bridge export failed; retrying every %s s", self.cfg.bridge_seconds)
                self.bridge_status["error"] = error
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.cfg.bridge_seconds)
            except asyncio.TimeoutError:
                pass

    def status(self, state="running"):
        pipeline = self.pipeline_or_none()  # once per refresh, so a broken config logs once
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
            clarifications=self.clarifications.count(),
            router=dict(self.router.snapshot(), enabled=self.routing(pipeline) is not None),
            lecture_summaries=dict(self.summaries.snapshot(), enabled=self.summarizing(pipeline) is not None),
            bridge=self.bridge_status,
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
                await asyncio.to_thread(self.speakers.tick, self.clarifications.command)
                await asyncio.to_thread(self.clarifications.catalog)
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
            meetings = MeetingStore(self.cfg.incoming_dir / "meetings")
            meetings.recover()
            # Finish deletions interrupted by a crash, and apply the policy to jobs completed before it existed.
            await asyncio.to_thread(self.purge_completed_audio)
            secret = load_or_create_secret(self.cfg.secret_file, create=False)
            self.server = UploadServer(
                secret, self.cfg.incoming_dir, self.cfg.host, self.cfg.port, writer_factory=factory,
                file_store=meetings,
            )
            await self.server.start()
            tasks = [
                asyncio.create_task(self.worker()),
                asyncio.create_task(self.heartbeat()),
                asyncio.create_task(self.speaker_review()),
                asyncio.create_task(self.route_published()),
                asyncio.create_task(self.summarize_lectures()),
                *([asyncio.create_task(self.export())] if self.cfg.bridge_url else []),
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
