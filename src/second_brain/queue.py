"""SQLite is the job authority. Publication is replayable after any crash."""

from __future__ import annotations
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Queue:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, audio TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
                    metadata TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'queued', error TEXT, created REAL NOT NULL,
                    updated REAL NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, time REAL NOT NULL, kind TEXT NOT NULL, job TEXT, detail TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def known(self, path: Path):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM jobs WHERE audio=?", (str(path.resolve()),)).fetchone() is not None

    def enqueue(self, path: Path, metadata: dict):
        with path.open("rb") as audio:
            digest = hashlib.file_digest(audio, "sha256").hexdigest()
        identity = f"{metadata['device']}:{metadata['start_seq']}:{digest}"
        key = hashlib.sha256(identity.encode()).hexdigest()
        now = time.time()
        with self.connect() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO jobs(id,audio,sha256,metadata,created,updated) VALUES(?,?,?,?,?,?)",
                (key, str(path.resolve()), digest, json.dumps(metadata), now, now),
            )
            if inserted.rowcount:
                db.execute(
                    "INSERT INTO events(time,kind,job,detail) VALUES(?,'received',?,'Recording queued')", (now, key)
                )
        return key

    def recover(self):
        with self.connect() as db:
            # Interrupted work can be retried even if interruption used the last attempt.
            db.execute(
                "UPDATE jobs SET state='pending',stage='recovered',next_attempt=0,attempts=max(0,attempts-1) WHERE state='processing'"
            )

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE state='pending' AND next_attempt<=? ORDER BY created LIMIT 1", (time.time(),)
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET state='processing',stage='starting',attempts=attempts+1,updated=? WHERE id=?",
                (time.time(), row["id"]),
            )
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def progress(self, key, stage):
        with self.connect() as db:
            db.execute("UPDATE jobs SET stage=?,updated=? WHERE id=?", (stage, time.time(), key))

    def complete(self, key, result):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state='complete',stage='complete',error=NULL,result=?,updated=? WHERE id=?",
                (json.dumps(result), time.time(), key),
            )
            db.execute(
                "INSERT INTO events(time,kind,job,detail) VALUES(?,'complete',?,?)",
                (time.time(), key, json.dumps(result)),
            )

    def fail(self, job, error, retry_seconds, max_attempts):
        state = "failed" if job["attempts"] >= max_attempts else "pending"
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state=?,stage=?,error=?,next_attempt=?,updated=? WHERE id=?",
                (
                    state,
                    "failed" if state == "failed" else "retry_wait",
                    error,
                    time.time() + min(3600, retry_seconds * 2 ** min(job["attempts"] - 1, 6)),
                    time.time(),
                    job["id"],
                ),
            )
            db.execute(
                "INSERT INTO events(time,kind,job,detail) VALUES(?,'failed',?,?)", (time.time(), job["id"], error)
            )

    def completed(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,audio FROM jobs WHERE state='complete'")]

    def retry_failed(self):
        with self.connect() as db:
            return db.execute(
                "UPDATE jobs SET state='pending',stage='queued',attempts=0,next_attempt=0,error=NULL WHERE state='failed'"
            ).rowcount

    def snapshot(self):
        with self.connect() as db:
            counts = {r["state"]: r["n"] for r in db.execute("SELECT state,count(*) n FROM jobs GROUP BY state")}
            current = db.execute(
                "SELECT id,stage,attempts,updated FROM jobs WHERE state='processing' LIMIT 1"
            ).fetchone()
            events = [dict(r) for r in db.execute("SELECT time,kind,job,detail FROM events ORDER BY id DESC LIMIT 10")]
            recent = [
                dict(r)
                for r in db.execute(
                    "SELECT id,state,stage,attempts,error,updated FROM jobs ORDER BY updated DESC LIMIT 10"
                )
            ]
            return {"counts": counts, "current": dict(current) if current else None, "recent": recent, "events": events}
