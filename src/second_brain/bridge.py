"""Outbound shadow export to the selfhosted server (docs/mobile/architecture-and-sync.md).

Read-only over the pipeline's own durable state: completed jobs, frozen publication
manifests, router ledgers and the speaker database. Nothing here is on the receive,
ACK, scoring or publication path; a server outage only delays export.

There is no separate spool: every tick reconciles current local state against the
records the server has acknowledged (bridge.sqlite3), so a crash, a lost response or
a long outage replays exactly what is missing. The server deduplicates by id and hash.
"""

from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import urllib.error
import urllib.request

from .router import event_id, safe_name

BATCH = 100
EXCERPT_CHARS = 4000  # minimal evidence; the vault note keeps the full transcript
TIMEOUT_SECONDS = 30


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def published_windows(manifest: dict, meeting: bool) -> list[dict]:
    """Windows the vault writer publishes (vault.publish / publish_meeting); filtered chatter stays local."""
    if meeting:
        return [w for w in manifest["windows"] if not w.get("speaker_filter") and w.get("final_transcript", "").strip()]
    return [w for w in manifest["windows"] if w.get("memory_keep") is True and w.get("route_to_knowledge_router") is True]


class Bridge:
    def __init__(self, data_dir: Path, vault: Path, url: str, token: str, opener=urllib.request.urlopen):
        self.data_dir, self.vault = data_dir, vault
        self.url, self.token, self.opener = url.rstrip("/"), token, opener
        self.path = data_dir / "bridge.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS exported (kind TEXT NOT NULL, id TEXT NOT NULL, hash TEXT NOT NULL,
                    PRIMARY KEY (kind, id));
                CREATE TABLE IF NOT EXISTS done_files (name TEXT PRIMARY KEY);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def read_only(self, name: str):
        db = sqlite3.connect(f"file:{self.data_dir / name}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    # --- Collect ---------------------------------------------------------------------------

    def jobs(self) -> dict[str, dict]:
        if not (self.data_dir / "queue.sqlite3").exists():
            return {}
        with self.read_only("queue.sqlite3") as db:
            return {r["id"]: dict(r) for r in db.execute("SELECT id,sha256,metadata FROM jobs WHERE state='complete'")}

    def sources(self, jobs: dict, done: set) -> list[tuple]:
        """Immutable once complete, so a job already exported is never read again."""
        out = []
        for job_id, job in jobs.items():
            if f"job:{job_id}" in done:
                continue
            path = self.data_dir / "jobs" / job_id / "publication-manifest.json"
            if not path.is_file():
                continue  # completed before publication manifests existed
            manifest = json.loads(path.read_text(encoding="utf-8"))
            metadata = json.loads(job["metadata"])
            meeting = metadata.get("source") == "meeting"
            windows = [{
                "id": w["id"], "start": w["start"], "end": w["end"],
                "excerpt": w["final_transcript"].strip()[:EXCERPT_CHARS],
                "conversation_type": w.get("memory_gate", {}).get("conversation_type"),
                "speakers": w.get("speaker_identities", {}),
            } for w in published_windows(manifest, meeting)]
            out.append((f"job:{job_id}", "source", job_id, {
                "kind": "meeting" if meeting else "omi",
                "recorded_at": metadata.get("first_utc"),
                "source_sha256": job["sha256"],
                "window_count": len(manifest["windows"]),
                "windows": windows,
            }))
        return out

    def items(self, jobs: dict, done: set) -> list[tuple]:
        """Router extractions as proposals. IDs reproduce router.event_id, so they match the
        markers on items the router wrote to the vault (needed for the later todo migration).
        ponytail: exports every extracted item, including ones the router's reconciliation
        dropped as duplicates; read the ledger's verdicts if the phone needs that distinction."""
        folder = self.vault / "System" / "Router" / "Ledger"
        out = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            if f"ledger:{path.stem}" in done:
                continue
            ledger = json.loads(path.read_text(encoding="utf-8"))
            shash, source_id, x = ledger["source_hash"], ledger["source_id"], ledger["extraction"]
            job = jobs.get(source_id.rsplit("-", 1)[0])
            observed_at = (json.loads(job["metadata"]).get("first_utc") if job else None) or "unknown"
            day = safe_name(observed_at[:10])  # as router.route keys ideas and daily notes
            found = []
            for f in x["facts"]:
                found.append(("fact", f"{f['entity_type']}:{f['entity_name']}", f["fact"], f))
            for d in x["decisions"]:
                found.append(("decision", (d.get("observed_at") or observed_at)[:10], d["text"], d))
            for t in x["tasks"]:
                found.append(("task", t["owner"] or "", t["text"], t))
            found += [("idea", day, i["text"], i) for i in x["ideas"]]
            found += [("daily", day, t, {}) for t in x["daily_summary"]]
            records = [(f"ledger:{path.stem}", "item", event_id(shash, kind, key, text), {
                "kind": kind, "text": text, "source_id": source_id, "source_hash": shash,
                "observed_at": fields.get("observed_at") or observed_at,
                **{k: fields[k] for k in ("entity_type", "entity_name", "owner", "due", "confidence") if k in fields},
            }) for kind, key, text, fields in found]
            # A ledger with no items still needs marking done; an empty marker record does it.
            out += records or [(f"ledger:{path.stem}", None, None, None)]
        return out

    def speakers(self) -> list[tuple]:
        """Mutable (assignments, renames, forgets), so read in full and diffed every tick.
        No voice vectors and no clips leave the machine in this milestone."""
        if not (self.data_dir / "speakers.sqlite3").exists():
            return []
        with self.read_only("speakers.sqlite3") as db:
            people = db.execute("SELECT id,name FROM people").fetchall()
            observations = db.execute(
                "SELECT id,job,label,display,recorded,person,manual,score,embedding_status,published,importance,clips "
                "FROM observations").fetchall()
        out = [(None, "person", p["id"], {"name": p["name"]}) for p in people]
        for o in observations:
            provenance = ("confirmed" if o["manual"] else "inferred") if o["person"] else ("cleared" if o["manual"] else None)
            out.append((None, "observation", o["id"], {
                "source_id": o["job"], "label": o["label"], "display": o["display"], "recorded_at": o["recorded"],
                "person_id": o["person"], "provenance": provenance,
                "score": round(o["score"], 3) if o["score"] is not None else None,
                "embedding_status": o["embedding_status"], "published": o["published"], "importance": o["importance"],
                "clip_count": len(json.loads(o["clips"])),
            }))
        return out

    # --- Send ------------------------------------------------------------------------------

    def post(self, records: list[dict], status: dict) -> dict:
        body = json.dumps({"records": records, "status": status}).encode()
        request = urllib.request.Request(self.url + "/v1/worker/events", data=body, method="POST", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            error.close()
            raise

    def tick(self, status: dict | None = None) -> dict:
        """Export everything not yet acknowledged. Raises on network/server failure; the
        acknowledged state only advances after a successful response, so the next tick resumes."""
        with self.connect() as db:
            exported = {(k, i): h for k, i, h in db.execute("SELECT kind,id,hash FROM exported")}
            done = {r[0] for r in db.execute("SELECT name FROM done_files")}
        jobs = self.jobs()
        found = self.sources(jobs, done) + self.items(jobs, done) + self.speakers()
        live = {(kind, rid) for _, kind, rid, _ in found if kind}
        pending, files = [], {}
        for file, kind, rid, data in found:
            if kind:
                h = digest(data)
                if exported.get((kind, rid)) != h:
                    pending.append(({"kind": kind, "id": rid, "hash": h, "data": data}, file))
            if file:
                files.setdefault(file, 0)
        # A person or observation gone from the speaker database is forgotten: tombstone it.
        # Sources and items are immutable evidence; a missing file (vault offline) never deletes them.
        for (kind, rid), h in exported.items():
            if kind in ("person", "observation") and (kind, rid) not in live and h != "deleted":
                pending.append(({"kind": kind, "id": rid, "hash": "deleted", "data": None}, None))
        for _, file in pending:
            if file:
                files[file] += 1
        sent = 0
        for start in range(0, max(len(pending), 1), BATCH):
            batch = pending[start:start + BATCH]
            self.post([r for r, _ in batch], status or {})
            with self.connect() as db:
                db.executemany("INSERT OR REPLACE INTO exported VALUES(?,?,?)",
                               [(r["kind"], r["id"], r["hash"]) for r, _ in batch])
                for _, file in batch:
                    if file:
                        files[file] -= 1
                db.executemany("INSERT OR IGNORE INTO done_files VALUES(?)",
                               [(f,) for f, left in files.items() if left == 0])
            sent += len(batch)
        return {"sent": sent}


def load_token(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("Bridge token file is empty")
    return token


def safe_error(error: Exception) -> str:
    """Status text: never the URL, token, response body or any content."""
    if isinstance(error, urllib.error.HTTPError):
        return f"server returned {error.code}"
    return type(error).__name__
