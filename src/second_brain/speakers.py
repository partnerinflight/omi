"""Persistent human-confirmed voice profiles and private tray review mailbox."""

from __future__ import annotations
from contextlib import contextmanager
import copy
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid
from .io import write_json, atomic_write
from .speaker_audio import useful_clip


def review_clips(row):
    """Clips worth listening to; rows without any are hidden unless a person is already assigned."""
    return [c for c in json.loads(row["clips"]) if useful_clip(c)]


def unit(value):
    if not isinstance(value, list) or not 2 <= len(value) <= 4096:
        raise ValueError("Invalid voice embedding")
    numbers = [float(x) for x in value]
    norm = math.sqrt(sum(x * x for x in numbers))
    if not math.isfinite(norm) or norm < 1e-8:
        raise ValueError("Invalid voice embedding")
    return [x / norm for x in numbers]


def match(vectors, model, references, threshold=0.50, margin=0.05):
    """Every query clip must agree; only manually confirmed references may enter."""
    if len(vectors) < 2 or not model:
        return None, None
    winners = []
    scores = []
    for vector in vectors:
        by_person = {}
        for person, reference_model, reference in references:
            if model != reference_model or len(vector) != len(reference):
                continue
            score = sum(a * b for a, b in zip(vector, reference))
            by_person[person] = max(by_person.get(person, -1), score)
        ranking = sorted(by_person.items(), key=lambda item: item[1], reverse=True)
        if not ranking:
            return None, None
        person, score = ranking[0]
        runner_up = ranking[1][1] if len(ranking) > 1 else -1
        if score < threshold or score - runner_up < margin:
            return None, score
        winners.append(person)
        scores.append(score)
    return (winners[0], min(scores)) if len(set(winners)) == 1 else (None, None)


class Speakers:
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def __init__(self, data_dir: Path, review_dir: Path, threshold=0.50, margin=0.05):
        self.path = data_dir / "speakers.sqlite3"
        self.review = review_dir
        self.threshold, self.margin = threshold, margin
        data_dir.mkdir(parents=True, exist_ok=True)
        # The installer grants the review user read access here and write access
        # ONLY to requests. Raw audio/embeddings and the database stay private.
        review_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for folder in ("clips", "requests", "responses"):
            (review_dir / folder).mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS people(id TEXT PRIMARY KEY, name TEXT NOT NULL, folded TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS observations(
                    id TEXT PRIMARY KEY, job TEXT NOT NULL, label TEXT NOT NULL, display TEXT NOT NULL,
                    recorded TEXT NOT NULL, clips TEXT NOT NULL, vectors TEXT NOT NULL, model TEXT,
                    embedding_status TEXT NOT NULL, person TEXT, manual INTEGER NOT NULL DEFAULT 0,
                    score REAL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, response TEXT NOT NULL);
            """)

    def ingest(self, job, manifest):
        observations = manifest.get("speaker_observations", [])
        metadata = json.loads(job["metadata"])
        # Copy clips before committing rows; a replay repairs interrupted copies.
        for index, obs in enumerate(observations):
            key = hashlib.sha256((job["id"] + ":" + obs["label"]).encode()).hexdigest()
            with self.connect() as db:
                if db.execute("SELECT 1 FROM observations WHERE id=?", (key,)).fetchone():
                    continue
            clips, vectors = [], []
            for n, clip in enumerate(obs["clips"]):
                source = Path(clip["path"]).resolve()
                if not source.is_relative_to((self.path.parent / "jobs" / job["id"]).resolve()):
                    raise ValueError("Review clip escapes the job directory")
                name = f"{key}-{n}.wav"
                atomic_write(self.review / "clips" / name, source.read_bytes())
                clips.append({k: clip[k] for k in ("start", "end", "text", "quality")})
                clips[-1]["file"] = name
                if clip.get("eligible") and clip.get("embedding") is not None:
                    vectors.append(unit(clip["embedding"]))
            with self.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO observations(id,job,label,display,recorded,clips,vectors,model,embedding_status,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        job["id"],
                        obs["label"],
                        f"Speaker {index+1}",
                        metadata.get("first_utc", "Unknown time"),
                        json.dumps(clips),
                        json.dumps(vectors),
                        obs.get("model"),
                        (
                            f"{len(vectors)} clean reference clips"
                            if vectors
                            else (
                                manifest.get("speaker_embedding_status", "not configured")
                                if manifest.get("speaker_embedding_status") != "ready"
                                else "no usable voice sample"
                            )
                        ),
                        time.time(),
                    ),
                )
        with self.connect() as db:
            self._rematch(db, job["id"])

    def _rematch(self, db, job=None):
        references = []
        for row in db.execute("SELECT person,model,vectors FROM observations WHERE manual=1 AND person IS NOT NULL"):
            references.extend((row["person"], row["model"], v) for v in json.loads(row["vectors"]))
        query = "SELECT id,model,vectors FROM observations WHERE manual=0"
        rows = db.execute(query + " AND job=?", (job,)).fetchall() if job else db.execute(query).fetchall()
        for row in rows:
            person, score = match(json.loads(row["vectors"]), row["model"], references, self.threshold, self.margin)
            db.execute("UPDATE observations SET person=?,score=? WHERE id=?", (person, score, row["id"]))

    def rematch(self):
        with self.connect() as db:
            self._rematch(db)

    @staticmethod
    def name(value):
        if not isinstance(value, str):
            raise ValueError("Enter a name")
        name = value.strip()
        if not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name) or any(c in name for c in "[]<>\\|"):
            raise ValueError("Use a name of 1–80 characters without control characters or markup brackets")
        return name

    def command(self, request):
        key = str(uuid.UUID(request["id"]))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT response FROM commands WHERE id=?", (key,)).fetchone()
            if saved:
                return json.loads(saved["response"])
            db.execute("SAVEPOINT change")
            try:
                action = request["action"]
                if action in ("assign", "clear"):
                    observation = request["observation"]
                    if not db.execute("SELECT 1 FROM observations WHERE id=?", (observation,)).fetchone():
                        raise ValueError("Speaker no longer exists; refresh the list")
                    person = None
                    if action == "assign":
                        person = request.get("person")
                        if person:
                            if not db.execute("SELECT 1 FROM people WHERE id=?", (person,)).fetchone():
                                raise ValueError("Person no longer exists; refresh the list")
                        else:
                            name = self.name(request.get("name"))
                            existing = db.execute("SELECT id FROM people WHERE folded=?", (name.casefold(),)).fetchone()
                            person = existing["id"] if existing else uuid.uuid4().hex
                            db.execute("INSERT OR IGNORE INTO people VALUES(?,?,?)", (person, name, name.casefold()))
                    # Clear is an explicit override: don't immediately match it again.
                    db.execute("UPDATE observations SET person=?,manual=1,score=NULL WHERE id=?", (person, observation))
                elif action == "rename":
                    name = self.name(request.get("name"))
                    if not db.execute(
                        "UPDATE people SET name=?,folded=? WHERE id=?", (name, name.casefold(), request["person"])
                    ).rowcount:
                        raise ValueError("Person no longer exists")
                elif action == "forget":
                    db.execute(
                        "UPDATE observations SET person=NULL,manual=1,score=NULL WHERE person=?", (request["person"],)
                    )
                    db.execute("DELETE FROM people WHERE id=?", (request["person"],))
                else:
                    raise ValueError("Unknown speaker action")
                self._rematch(db)
                response = {"id": key, "ok": True}
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError, sqlite3.ProgrammingError) as error:
                db.execute("ROLLBACK TO change")
                response = {
                    "id": key,
                    "ok": False,
                    "error": str(error) if isinstance(error, ValueError) else "Invalid or conflicting speaker change",
                }
            db.execute("RELEASE change")
            db.execute("INSERT INTO commands VALUES(?,?)", (key, json.dumps(response)))
            return response

    def tick(self):
        for path in sorted((self.review / "requests").glob("*.json"))[:50]:
            try:
                key = str(uuid.UUID(path.stem))
                if path.is_symlink() or path.stat().st_size > 16384:
                    raise ValueError("Request too large")
                request = json.loads(path.read_text(encoding="utf-8-sig"))
                if request["id"] != key:
                    raise ValueError("Request ID mismatch")
                response = self.command(request)
            except (ValueError, KeyError, TypeError):
                response = {"id": path.stem, "ok": False, "error": "Invalid speaker request"}
            write_json(self.review / "responses" / path.name, response)
            path.unlink(missing_ok=True)
        self.catalog()

    def catalog(self):
        with self.connect() as db:
            people = [dict(row) for row in db.execute("SELECT id,name FROM people ORDER BY folded")]
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT o.id,job,label,display,recorded,clips,embedding_status,person,manual,score,p.name FROM observations o LEFT JOIN people p ON p.id=o.person ORDER BY created DESC"
                )
            ]
            for row in rows:
                row["clips"] = review_clips(row)
            rows = [row for row in rows if row["clips"] or row["person"]]
            for row in rows:
                row["state"] = (
                    "confirmed" if row["manual"] and row["person"] else "matched" if row["person"] else "unidentified"
                )
            write_json(
                self.review / "catalog.json",
                {"version": 1, "heartbeat": time.time(), "people": people, "speakers": rows},
            )

    def summary(self):
        with self.connect() as db:
            return {
                "unidentified": sum(
                    1 for row in db.execute("SELECT clips FROM observations WHERE person IS NULL") if review_clips(row)
                ),
                "people": db.execute("SELECT count(*) FROM people").fetchone()[0],
            }

    def render(self, job_id, manifest):
        result = copy.deepcopy(manifest)
        with self.connect() as db:
            names = {
                r["label"]: (r["name"] or r["display"], r["person"], r["manual"])
                for r in db.execute(
                    "SELECT o.label,o.display,o.person,o.manual,p.name FROM observations o LEFT JOIN people p ON p.id=o.person WHERE job=?",
                    (job_id,),
                )
            }
        for window in result["windows"]:
            if not window.get("final_segments"):
                continue
            rows, identities = [], {}
            for seg in window["final_segments"]:
                label = seg["speaker"]
                display, person, manual = names.get(label, (label, None, False))
                # Keep machine labels visible as provenance, never equate a label
                # from one chunk/file with a label from another.
                rows.append(f"[{seg['start']:.2f}-{seg['end']:.2f}] {display} ({label}): {seg['text'].strip()}")
                if person:
                    identities[label] = {
                        "id": person,
                        "name": display,
                        "source": "confirmed" if manual else "voice match",
                    }
            window["final_transcript"] = "\n".join(rows)
            window["speaker_identities"] = identities
        return result
