"""Outbound shadow export to the selfhosted server (docs/mobile/architecture-and-sync.md).

Export reads the pipeline's own durable state: completed jobs, frozen publication
manifests, router ledgers, clarifications and the speaker database. Phone commands
(milestone 3) are applied through the same local commands the tray uses. Nothing here
is on the receive, ACK, scoring or publication path; a server outage only delays it.

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
import uuid

from .io import atomic_write
from .router import ENTITY_FOLDERS, event_id, safe_name
from .speaker_audio import useful_clip

BATCH = 100
EXCERPT_CHARS = 4000  # minimal evidence; the vault note keeps the full transcript
TIMEOUT_SECONDS = 30
MAX_CLIP_BYTES = 2_000_000  # matches the server limit
STALE = "Changed on Windows since you loaded it; refresh and try again."


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def published_windows(manifest: dict, meeting: bool) -> list[dict]:
    """Windows the vault writer publishes (vault.publish / publish_meeting); filtered chatter stays local."""
    if meeting:
        return [w for w in manifest["windows"] if not w.get("speaker_filter") and w.get("final_transcript", "").strip()]
    return [w for w in manifest["windows"] if w.get("memory_keep") is True and w.get("route_to_knowledge_router") is True]


class Bridge:
    def __init__(self, data_dir: Path, vault: Path, url: str, token: str, opener=urllib.request.urlopen,
                 review_dir: Path | None = None):
        self.data_dir, self.vault = data_dir, vault
        self.review = review_dir or data_dir.parent / "review"  # Config.review_dir
        self.url, self.token, self.opener = url.rstrip("/"), token, opener
        self.path = data_dir / "bridge.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS exported (kind TEXT NOT NULL, id TEXT NOT NULL, hash TEXT NOT NULL,
                    PRIMARY KEY (kind, id));
                CREATE TABLE IF NOT EXISTS done_files (name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS projected (path TEXT PRIMARY KEY, hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
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
                found.append(("fact", f"{f['entity_type']}:{f['entity_name']}", f["fact"], f,
                              f"{ENTITY_FOLDERS[f['entity_type']]}/{safe_name(f['entity_name'])}.md"))
            for d in x["decisions"]:
                when = (d.get("observed_at") or observed_at)[:10]
                found.append(("decision", when, d["text"], d, f"Decisions/{safe_name(when)}.md"))
            for t in x["tasks"]:
                found.append(("task", t["owner"] or "", t["text"], t, "Projects/_Tasks.md"))
            found += [("idea", day, i["text"], i, f"Ideas/{day}.md") for i in x["ideas"]]
            found += [("daily", day, t, {}, f"Daily/{day}.md") for t in x["daily_summary"]]
            records = [(f"ledger:{path.stem}", "item", event_id(shash, kind, key, text), {
                "kind": kind, "text": text, "source_id": source_id, "source_hash": shash,
                "note": note,  # vault-relative, where router.route wrote it
                "observed_at": fields.get("observed_at") or observed_at,
                **{k: fields[k] for k in ("entity_type", "entity_name", "owner", "due", "confidence") if k in fields},
            }) for kind, key, text, fields, note in found]
            # A ledger with no items still needs marking done; an empty marker record does it.
            out += records or [(f"ledger:{path.stem}", None, None, None)]
        return out

    def clarifications(self) -> list[tuple]:
        """Clarification ids are router event ids, so they match the vault item they ask about."""
        if not (self.data_dir / "clarifications.sqlite3").exists():
            return []
        with self.read_only("clarifications.sqlite3") as db:
            rows = db.execute("SELECT id,path,text,questions,context,state,answer FROM items").fetchall()
        return [(None, "clarification", r["id"], {
            "note": r["path"], "text": r["text"], "questions": json.loads(r["questions"]),
            "context": r["context"][:EXCERPT_CHARS], "state": r["state"], "answer": r["answer"],
        }) for r in rows]

    def speaker_records(self) -> dict[tuple, dict]:
        """Mutable (assignments, renames, forgets), so read in full and diffed every tick.
        Voice vectors never leave the machine; clips go separately, only while unconfirmed."""
        if not (self.data_dir / "speakers.sqlite3").exists():
            return {}
        with self.read_only("speakers.sqlite3") as db:
            people = db.execute("SELECT id,name FROM people").fetchall()
            observations = db.execute(
                "SELECT id,job,label,display,recorded,person,manual,score,embedding_status,published,importance,clips "
                "FROM observations").fetchall()
        out = {("person", p["id"]): {"name": p["name"]} for p in people}
        for o in observations:
            provenance = ("confirmed" if o["manual"] else "inferred") if o["person"] else ("cleared" if o["manual"] else None)
            clips = json.loads(o["clips"])
            out[("observation", o["id"])] = {
                "source_id": o["job"], "label": o["label"], "display": o["display"], "recorded_at": o["recorded"],
                "person_id": o["person"], "provenance": provenance,
                "score": round(o["score"], 3) if o["score"] is not None else None,
                "embedding_status": o["embedding_status"], "published": o["published"], "importance": o["importance"],
                "clip_count": len(clips),
                # Index n is the n in the clip file name (Speakers.ingest); same filter as the tray.
                "clips": [n for n, c in enumerate(clips) if useful_clip(c)],
            }
        return out

    # --- Send ------------------------------------------------------------------------------

    def send(self, method: str, path: str, value=None, body: bytes | None = None, headers: dict | None = None):
        if value is not None:
            body, headers = json.dumps(value).encode(), {"Content-Type": "application/json"}
        request = urllib.request.Request(self.url + path, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}", **(headers or {})})
        try:
            with self.opener(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            error.close()
            raise

    def post(self, records: list[dict], status: dict) -> dict:
        return self.send("POST", "/v1/worker/events", {"records": records, "status": status})

    # --- Commands from the phone ------------------------------------------------------------

    def apply_commands(self, speakers, clarifications, pages: int = 10) -> int:
        """Apply pending phone commands in server order, one at a time. A transient error
        (database busy, network) raises and stops here, so later commands never overtake it."""
        applied = 0
        for _ in range(pages):
            commands = self.send("GET", "/v1/worker/commands")["commands"]
            if not commands:
                break
            for command in commands:
                result = self.apply(command, speakers, clarifications)
                self.send("POST", f"/v1/worker/commands/{command['id']}/result", result)
                applied += 1
        return applied

    def apply(self, command: dict, speakers, clarifications) -> dict:
        p, key = command["payload"], str(uuid.UUID(command["id"]))
        try:
            if p["action"] in ("clarify", "clarify_dismiss"):
                # Idempotent on replay: a resolved item with the same answer reports ok.
                response = clarifications.command(
                    {"id": key, "action": p["action"], "observation": p["item"], "name": p.get("answer")})
            else:
                with speakers.connect() as db:
                    saved = db.execute("SELECT response FROM commands WHERE id=?", (key,)).fetchone()
                if saved:  # applied before a crash or a lost result upload: report, never reapply
                    response = json.loads(saved["response"])
                else:
                    # Optimistic concurrency against tray edits: the phone names the state it saw.
                    target = ("person", p["person"]) if p["action"] in ("rename", "forget") else ("observation", p["observation"])
                    current = self.speaker_records().get(target)
                    if "base_hash" in p and (current is None or digest(current) != p["base_hash"]):
                        response = {"ok": False, "error": STALE}
                    else:
                        response = speakers.command({"id": key, **{k: p[k] for k in ("action", "observation", "person", "name")
                                                                   if k in p}})
        except (ValueError, KeyError, TypeError):
            response = {"ok": False, "error": "Invalid command"}
        ok = response.get("ok") is True
        return {"ok": ok, "error": None if ok else str(response.get("error") or "Failed")[:500]}

    # --- Vault projection of curated memories -------------------------------------------------

    def project(self) -> int:
        """Write confirmed memories into managed overlay notes under Mobile/. Router notes, curated
        prose and published conversations are never modified. A note is replaced only while it
        still holds exactly what was last written there; a human edit stops that note and the new
        version goes beside it, so both survive. Returns the number of conflicted notes."""
        memories = self.send("GET", "/v1/worker/memories")["memories"]
        groups: dict[str, list] = {}
        for m in memories:
            if m["kind"] == "fact" and m.get("entity_type") in ENTITY_FOLDERS and m.get("entity_name"):
                title = m["entity_name"]
                path = f"{PROJECTION}/{ENTITY_FOLDERS[m['entity_type']]}/{safe_name(title)}.md"
            else:
                title = {"fact": "Facts", "decision": "Decisions", "idea": "Ideas"}[m["kind"]]
                path = f"{PROJECTION}/{title}.md"
            groups.setdefault(path, [title]).append(m)
        with self.connect() as db:
            ledger = dict(db.execute("SELECT path,hash FROM projected"))
        conflicts = 0
        for path in sorted(set(groups) | set(ledger)):
            content = render(groups[path][0], groups[path][1:]) if path in groups else None
            conflicts += not self.write_projection(path, content, ledger.get(path))
        return conflicts

    def write_projection(self, relative: str, content: bytes | None, last: str | None) -> bool:
        target = self.vault / relative
        conflict = target.with_name(target.stem + " (conflict from app).md")
        try:
            current = target.read_bytes()
        except FileNotFoundError:
            current = None
        new = hashlib.sha256(content).hexdigest() if content is not None else None
        ours = current is None or hashlib.sha256(current).hexdigest() in (last, new)
        if not ours:
            # Edited by hand: keep it, and offer the app's version next to it until resolved.
            if content is not None and (not conflict.exists() or conflict.read_bytes() != content):
                atomic_write(conflict, content)
            return False
        if content is None:
            target.unlink(missing_ok=True)  # nothing curated left and the note was untouched
        elif current != content:
            atomic_write(target, content)
        conflict.unlink(missing_ok=True)
        with self.connect() as db:
            if content is None:
                db.execute("DELETE FROM projected WHERE path=?", (relative,))
            else:
                db.execute("INSERT OR REPLACE INTO projected VALUES(?,?)", (relative, new))
        return True

    def project_todos(self) -> str:
        """After cutover, ToDos/Tasks.md is a projection of server todos in the format the Hermes
        plugin reads (so a rollback finds every app change, with its id). The first write moves
        the hand-kept file, prose included, to "Tasks (before app cutover).md" if it still has
        the bytes the final import read. A hand edit afterwards stops the projection (the app
        version goes beside it). After a rollback the file is handed back once, without the
        managed header, and left alone."""
        found = self.send("GET", "/v1/worker/todos")
        relative = "ToDos/Tasks.md"
        target = self.vault / relative
        with self.connect() as db:
            last = db.execute("SELECT hash FROM projected WHERE path=?", (relative,)).fetchone()
        last = last[0] if last else None
        if any(target.parent.glob("Tasks.sync-conflict-*.md")):
            return "conflict"  # Syncthing has two versions; a person must pick one first
        if found["authority"] != "server":
            if last is None:
                return "legacy"
            handed = self.write_projection(relative, render_todos(found["todos"], managed=False), last)
            if handed:
                with self.connect() as db:
                    db.execute("DELETE FROM projected WHERE path=?", (relative,))
            return "handed_back" if handed else "conflict"
        if last is None:
            try:
                current = target.read_bytes()
            except FileNotFoundError:
                current = None
            if current is not None:
                if hashlib.sha256(current).hexdigest() != found["source_hash"]:
                    return "conflict"  # changed after the final import; re-run the import first
                archive = target.with_name("Tasks (before app cutover).md")
                if not archive.exists():
                    atomic_write(archive, current)
            last = found["source_hash"]
        return "projected" if self.write_projection(relative, render_todos(found["todos"]), last) else "conflict"

    # --- Review clips -------------------------------------------------------------------------

    def upload_clips(self, records: dict, exported: dict) -> int:
        """Clips of speakers still waiting for a name. Re-sent only when the file changes; the
        server verifies the hash and drops clips once the speaker is confirmed or discarded."""
        sent = 0
        for (kind, rid), data in records.items():
            if kind != "observation" or data["provenance"] == "confirmed" or data["published"] == 0:
                continue
            for n in data["clips"]:
                path = self.review / "clips" / f"{rid}-{n}.wav"
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue  # the phone shows it as unavailable
                version = f"{stat.st_size}:{stat.st_mtime_ns}"
                if exported.get(("clip", f"{rid}:{n}")) == version or stat.st_size > MAX_CLIP_BYTES:
                    continue
                audio = path.read_bytes()
                self.send("PUT", f"/v1/worker/clips/{rid}/{n}", body=audio, headers={
                    "Content-Type": "audio/wav", "X-Content-SHA256": hashlib.sha256(audio).hexdigest()})
                with self.connect() as db:
                    db.execute("INSERT OR REPLACE INTO exported VALUES('clip',?,?)", (f"{rid}:{n}", version))
                sent += 1
        return sent

    def tick(self, status: dict | None = None) -> dict:
        """Export everything not yet acknowledged. Raises on network/server failure; the
        acknowledged state only advances after a successful response, so the next tick resumes."""
        with self.connect() as db:
            exported = {(k, i): h for k, i, h in db.execute("SELECT kind,id,hash FROM exported")}
            done = {r[0] for r in db.execute("SELECT name FROM done_files")}
        jobs = self.jobs()
        speakers = self.speaker_records()
        found = (self.sources(jobs, done) + self.items(jobs, done) + self.clarifications()
                 + [(None, kind, rid, data) for (kind, rid), data in speakers.items()])
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
            epoch = self.post([r for r, _ in batch], status or {}).get("epoch")
            with self.connect() as db:
                known = db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
                if epoch and known and known[0] != epoch:
                    # Restored from a backup: what it acknowledged since may be gone. Forget every
                    # acknowledgment (the server deduplicates) and start over once.
                    db.executescript("DELETE FROM exported; DELETE FROM done_files;")
                if epoch:
                    db.execute("INSERT OR REPLACE INTO meta VALUES('epoch',?)", (epoch,))
            if epoch and known and known[0] != epoch:
                return self.tick(status)
            with self.connect() as db:
                db.executemany("INSERT OR REPLACE INTO exported VALUES(?,?,?)",
                               [(r["kind"], r["id"], r["hash"]) for r, _ in batch])
                for _, file in batch:
                    if file:
                        files[file] -= 1
                db.executemany("INSERT OR IGNORE INTO done_files VALUES(?)",
                               [(f,) for f, left in files.items() if left == 0])
            sent += len(batch)
        return {"sent": sent, "clips": self.upload_clips(speakers, exported)}


PROJECTION = "Mobile"
MARKER_PREFIX = "%% memory:"


def render(title: str, memories: list[dict]) -> bytes:
    """Deterministic: the same curated set always renders the same bytes."""
    lines = ["---", "type: second-brain-memories", "managed_by: second-brain-app", "---", f"# {title}", "",
             "Confirmed in the Second Brain app. Edit memories there; edits made here stop updates to this note.", ""]
    for m in sorted(memories, key=lambda m: (m.get("observed_at") or "", m["id"])):
        when = (m.get("observed_at") or "unknown")[:10]
        text = " ".join(m["text"].split())
        lines.append(f"- {when} — {text} {MARKER_PREFIX}{m['id']} %%")
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_todos(todos: list[dict], managed: bool = True) -> bytes:
    """The Hermes plugin's ToDos format (integrations/hermes/.../todos.py): one row per todo with
    its %% router:ID %% marker, [x] done, [-] dismissed, snooze before the marker, notes indented."""
    def row(t):
        check = {"completed": "x", "dismissed": "-"}.get(t["state"], " ")
        snooze = f" %% snoozed-until:{t['snoozed_until']} %%" if t.get("snoozed_until") and t["state"] == "open" else ""
        title = " ".join(t["title"].split()).replace("%%", "%").replace("<!--", "<!-")
        lines = [f"- [{check}] {title}{snooze} %% router:{t.get('legacy_id') or t['id']} %%"]
        for note in (t.get("notes") or "").splitlines():
            lines.append(note if note.startswith((" ", "\t")) else "  " + note)
        return lines
    live = [t for t in todos if not t.get("deleted_at")]
    head = ["---", "managed_by: second-brain-app", "---"] if managed else []
    lines = head + ["# ToDos", "",
                    "Managed by the Second Brain app; check items there or in Telegram. Edits here are not synced."
                    if managed else "Check items here or use Done in Telegram. Due dates are included only when known.",
                    "", "## Tasks", ""]
    for t in live:
        if t["state"] != "completed":
            lines += row(t)
    lines += ["", "## Completed", ""]
    for t in live:
        if t["state"] == "completed":
            lines += row(t)
    return ("\n".join(lines) + "\n").encode("utf-8")


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
