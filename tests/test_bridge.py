"""Bridge export over real loopback HTTP against synthetic manifests, ledgers and speaker data.
The real server contract is exercised in server/tests/test_worker.py."""

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import uuid
import wave

from second_brain.bridge import STALE, Bridge
from second_brain.clarifications import Clarifications
from second_brain.io import write_json
from second_brain.queue import Queue
from second_brain.router import event_id, source_hash
from second_brain.router_markers import marker
from second_brain.speakers import Speakers

TOKEN = "producer-token"


class FakeServer(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.records, self.requests, self.mode = {}, [], "ok"  # ok | down | lose_response
        self.commands, self.results, self.clips, self.lose_next_result = [], {}, {}, False
        self.memories = []
        self.epoch = "e1"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value=None):
        self.send_response(code)
        self.end_headers()
        if value is not None:
            self.wfile.write(json.dumps(value).encode())

    def allowed(self):
        s = self.server
        if s.mode == "down" or self.headers["Authorization"] != f"Bearer {TOKEN}":
            self.reply(503 if s.mode == "down" else 401)
            return False
        return True

    def body(self):
        return self.rfile.read(int(self.headers["Content-Length"] or 0))

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == "/v1/worker/memories":
            return self.reply(200, {"memories": self.server.memories})
        # /v1/worker/commands: pending ones, in order
        self.reply(200, {"commands": [c for c in self.server.commands if c["id"] not in self.server.results]})

    def do_PUT(self):  # /v1/worker/clips/{observation}/{n}
        if self.allowed():
            audio = self.body()
            assert hashlib.sha256(audio).hexdigest() == self.headers["X-Content-SHA256"]
            self.server.clips[self.path.rsplit("/", 2)[-2] + ":" + self.path.rsplit("/", 1)[-1]] = audio
            self.reply(204)

    def do_POST(self):
        s = self.server
        if not self.allowed():
            return
        body = json.loads(self.body())
        if self.path.endswith("/result"):
            command = self.path.split("/")[-2]
            s.results.setdefault(command, body)  # the first result stands, as on the real server
            lost, s.lose_next_result = s.lose_next_result, False
            return self.reply(500 if lost else 200, {})
        s.requests.append(body)
        for r in body["records"]:
            s.records[(r["kind"], r["id"])] = r
        # 500 after storing: committed, but the worker never hears it
        self.reply(500 if s.mode == "lose_response" else 200, {"accepted": len(body["records"]), "epoch": s.epoch})


class BridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data, self.vault = self.root / "data", self.root / "vault"
        self.server = FakeServer()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.job = self.completed_job()
        self.bridge = Bridge(self.data, self.vault, f"http://127.0.0.1:{self.server.server_port}", TOKEN,
                             review_dir=self.root / "review")
        self.clarifications = Clarifications(self.data, self.root / "review", self.vault)

    def completed_job(self):
        queue = Queue(self.data / "queue.sqlite3")
        audio = self.root / "rec.opus"
        audio.write_bytes(b"synthetic")
        job_id = queue.enqueue(audio, {"device": "dev", "start_seq": 1, "first_utc": "2026-10-09T08:00:00Z"})
        queue.complete(job_id, {})
        write_json(self.data / "jobs" / job_id / "publication-manifest.json", {"windows": [
            {"id": "w1", "start": 0, "end": 30, "final_transcript": " Buy oat milk tomorrow. ", "memory_keep": True,
             "route_to_knowledge_router": True, "memory_gate": {"conversation_type": "planning"},
             "speaker_identities": {"c:S1": {"name": "Alex"}}},
            {"id": "w2", "start": 30, "end": 60, "final_transcript": "private chatter", "memory_keep": False},
        ]})
        shash = source_hash(f"{job_id}-w1", "Buy oat milk tomorrow.")
        write_json(self.vault / "System/Router/Ledger" / f"{shash}.json", {
            "source_hash": shash, "source_id": f"{job_id}-w1", "extraction": {
                "facts": [{"entity_type": "person", "entity_name": "Alex", "fact": "Prefers oat milk", "confidence": 0.9,
                           "observed_at": None}],
                "decisions": [], "ideas": [], "daily_summary": ["Planned groceries"], "clarifications": [],
                "tasks": [{"text": "Buy oat milk", "owner": "", "due": "2026-10-10", "confidence": 0.8}]}})
        self.shash = shash
        speakers = Speakers(self.data, self.root / "review")
        clip = self.data / "jobs" / job_id / "0.wav"
        with wave.open(str(clip), "wb") as f:
            f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            f.writeframes(b"\0" * 96000)
        speakers.ingest({"id": job_id, "metadata": json.dumps({"first_utc": "2026-10-09T08:00:00Z"})}, {
            "windows": [], "speaker_embedding_status": "ready", "speaker_observations": [{"label": "c:S1", "model": "m",
                "clips": [{"path": str(clip), "start": 0, "end": 3, "text": "secret clip words", "quality": "clean turn",
                           "eligible": True, "embedding": [1, 0]}]}]})
        self.speakers = speakers
        return job_id

    def sent(self):
        return [r for body in self.server.requests for r in body["records"]]

    def test_exports_minimal_records_once(self):
        self.bridge.tick({"jobs": {"complete": 1}})
        by = {(r["kind"], r["id"]): r["data"] for r in self.sent()}
        source = by[("source", self.job)]
        self.assertEqual([w["id"] for w in source["windows"]], ["w1"])  # filtered window never leaves
        self.assertEqual(source["windows"][0]["excerpt"], "Buy oat milk tomorrow.")
        # Item ids match the markers router.route writes beside vault items.
        task = event_id(self.shash, "task", "", "Buy oat milk")
        self.assertEqual(by[("item", task)]["due"], "2026-10-10")
        self.assertIn(("item", event_id(self.shash, "fact", "person:Alex", "Prefers oat milk")), by)
        self.assertIn(("item", event_id(self.shash, "daily", "2026-10-09", "Planned groceries")), by)
        [obs] = [d for (k, _), d in by.items() if k == "observation"]
        self.assertEqual((obs["source_id"], obs["provenance"], obs["clip_count"]), (self.job, None, 1))
        payload = json.dumps(self.server.requests)
        for private in ("private chatter", "secret clip words", '"embedding":', '"vectors"', str(self.data)):
            self.assertNotIn(private, payload)
        self.assertEqual(self.server.requests[0]["status"], {"jobs": {"complete": 1}})

        # Replay: nothing new, only the heartbeat.
        self.bridge.tick()
        self.assertEqual(self.server.requests[-1]["records"], [])

    def test_outage_and_lost_response_resend_identically(self):
        self.server.mode = "down"
        with self.assertRaises(Exception):
            self.bridge.tick()
        self.server.mode = "lose_response"
        with self.assertRaises(Exception):
            self.bridge.tick()
        first = self.server.requests[-1]["records"]
        self.server.mode = "ok"
        self.bridge.tick()
        self.assertEqual(self.server.requests[-1]["records"], first)  # same ids and hashes; server dedupes
        self.bridge.tick()
        self.assertEqual(self.server.requests[-1]["records"], [])

    def test_speaker_changes_and_forget_tombstone(self):
        self.bridge.tick()
        [obs_id] = [i for k, i in self.server.records if k == "observation"]
        self.speakers.command({"id": str(uuid.uuid4()), "action": "assign", "observation": obs_id, "name": "Alex"})
        self.bridge.tick()
        changed = self.server.requests[-1]["records"]
        person = next(r for r in changed if r["kind"] == "person")
        self.assertEqual(person["data"], {"name": "Alex"})
        obs = next(r for r in changed if r["kind"] == "observation")["data"]
        self.assertEqual((obs["person_id"], obs["provenance"]), (person["id"], "confirmed"))

        self.speakers.command({"id": str(uuid.uuid4()), "action": "forget", "person": person["id"]})
        self.bridge.tick()
        changed = {(r["kind"], r["id"]): r for r in self.server.requests[-1]["records"]}
        self.assertIsNone(changed[("person", person["id"])]["data"])
        self.assertEqual(changed[("observation", obs_id)]["data"]["provenance"], "cleared")
        self.bridge.tick()
        self.assertEqual(self.server.requests[-1]["records"], [])  # tombstone sent once

    def test_restored_server_gets_everything_again(self):
        self.bridge.tick()
        first = {(r["kind"], r["id"]) for r in self.sent()}
        self.server.records.clear()  # the backup predates everything
        self.server.epoch = "e2"
        self.server.requests.clear()
        self.bridge.tick()
        self.assertEqual(set(self.server.records), first)
        self.server.requests.clear()
        self.bridge.tick()
        self.assertEqual(self.sent(), [])  # once, not every cycle

    def test_unreadable_vault_never_deletes_evidence(self):
        self.bridge.tick()
        shutil.rmtree(self.vault / "System")
        self.bridge.tick()
        self.assertEqual(self.server.requests[-1]["records"], [])

    def test_wrong_token_is_an_error_not_an_ack(self):
        bad = Bridge(self.data, self.vault, self.bridge.url, "wrong")
        with self.assertRaises(Exception):
            bad.tick()
        self.bridge.tick()
        self.assertTrue(any(r["kind"] == "source" for r in self.server.requests[-1]["records"]))

    # --- Milestone 3: phone commands and clips ---

    def observation(self):
        [(_, obs_id)] = [k for k in self.server.records if k[0] == "observation"]
        return obs_id, self.server.records[("observation", obs_id)]

    def command(self, **payload):
        command = {"id": str(uuid.uuid4()), "payload": payload}
        self.server.commands.append(command)
        return command["id"]

    def person_of(self, obs_id):
        with self.speakers.connect() as db:
            return db.execute("SELECT person,manual FROM observations WHERE id=?", (obs_id,)).fetchone()

    def test_phone_assign_applies_once_after_lost_result(self):
        self.bridge.tick()
        obs_id, record = self.observation()
        command = self.command(action="assign", observation=obs_id, name="Alex", base_hash=record["hash"])
        self.server.lose_next_result = True  # applied locally, result never acknowledged
        with self.assertRaises(Exception):
            self.bridge.apply_commands(self.speakers, self.clarifications)
        self.server.results.clear()  # the server never committed it
        self.assertEqual(self.bridge.apply_commands(self.speakers, self.clarifications), 1)
        self.assertEqual(self.server.results[command], {"ok": True, "error": None})
        with self.speakers.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM people").fetchone()[0], 1)
        self.assertEqual(self.person_of(obs_id)["manual"], 1)
        self.bridge.tick()
        self.assertEqual(self.server.records[("observation", obs_id)]["data"]["provenance"], "confirmed")

    def test_tray_edit_makes_phone_command_stale(self):
        self.bridge.tick()
        obs_id, record = self.observation()
        self.speakers.command({"id": str(uuid.uuid4()), "action": "assign", "observation": obs_id, "name": "Bob"})
        command = self.command(action="assign", observation=obs_id, name="Alex", base_hash=record["hash"])
        self.bridge.apply_commands(self.speakers, self.clarifications)
        self.assertEqual(self.server.results[command], {"ok": False, "error": STALE})
        with self.speakers.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM people").fetchall()[0][0], "Bob")

    def test_invalid_command_fails_terminally_and_later_ones_still_apply(self):
        self.bridge.tick()
        obs_id, record = self.observation()
        bad = self.command(action="assign", observation="missing", name="Alex")
        good = self.command(action="discard", observation=obs_id, base_hash=record["hash"])
        self.assertEqual(self.bridge.apply_commands(self.speakers, self.clarifications), 2)
        self.assertFalse(self.server.results[bad]["ok"])
        self.assertTrue(self.server.results[good]["ok"])
        self.assertFalse(any((self.root / "review/clips").glob(f"{obs_id}-*.wav")))  # discard drops audio

    def test_clips_upload_only_while_unconfirmed(self):
        self.bridge.tick()
        obs_id, _ = self.observation()
        self.assertEqual(list(self.server.clips), [f"{obs_id}:0"])
        self.assertEqual(self.server.clips[f"{obs_id}:0"], (self.root / "review/clips" / f"{obs_id}-0.wav").read_bytes())
        self.server.clips.clear()
        self.bridge.tick()
        self.assertEqual(self.server.clips, {})  # unchanged file is not resent

    def test_phone_clarification_updates_vault_note_once(self):
        eid = "0123456789abcdef"
        note = self.vault / "People/Alex.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"# Alex\n- Met Speaker 2 at lunch {marker(eid)}\n", encoding="utf-8")
        self.clarifications.add(eid, note, "Met Speaker 2 at lunch", ["Who is Speaker 2?"], "lunch transcript")
        self.bridge.tick()
        self.assertEqual(self.server.records[("clarification", eid)]["data"]["questions"], ["Who is Speaker 2?"])
        command = self.command(action="clarify", item=eid, answer="Met Sam at lunch")
        self.server.lose_next_result = True
        with self.assertRaises(Exception):
            self.bridge.apply_commands(self.speakers, self.clarifications)
        self.server.results.clear()
        self.bridge.apply_commands(self.speakers, self.clarifications)  # replay after the note was written
        self.assertTrue(self.server.results[command]["ok"])
        self.assertEqual(note.read_text(encoding="utf-8"), f"# Alex\n- Met Sam at lunch {marker(eid)}\n")
        self.bridge.tick()
        self.assertEqual(self.server.records[("clarification", eid)]["data"]["state"], "resolved")

    # --- Milestone 4: vault projection of confirmed memories ---

    def memory(self, mid, text, **kw):
        return {"id": mid, "kind": "fact", "text": text, "entity_type": "person", "entity_name": "Alex",
                "observed_at": "2026-10-09T08:00:00Z", **kw}

    def test_projection_writes_overlay_once_and_leaves_router_notes_alone(self):
        self.server.memories = [self.memory("b2", "Likes hiking"), self.memory("a1", "Prefers oat milk"),
                                {"id": "d1", "kind": "decision", "text": "Move standup to 10", "observed_at": None}]
        self.assertEqual(self.bridge.project(), 0)
        alex = self.vault / "Mobile/People/Alex.md"
        text = alex.read_text(encoding="utf-8")
        self.assertLess(text.index("Prefers oat milk"), text.index("Likes hiking"))  # observed_at, then id
        self.assertIn("- 2026-10-09 — Prefers oat milk %% memory:a1 %%", text)
        self.assertIn("- unknown — Move standup to 10 %% memory:d1 %%", (self.vault / "Mobile/Decisions.md").read_text(encoding="utf-8"))
        self.assertFalse((self.vault / "People").exists())  # router notes are never written by projection
        before = alex.stat().st_mtime_ns
        self.bridge.project()
        self.assertEqual(alex.stat().st_mtime_ns, before)  # unchanged content is not rewritten

    def test_hand_edited_overlay_keeps_both_versions_until_resolved(self):
        self.server.memories = [self.memory("a1", "Prefers oat milk")]
        self.bridge.project()
        alex = self.vault / "Mobile/People/Alex.md"
        alex.write_text(alex.read_text(encoding="utf-8") + "My own note\n", encoding="utf-8")
        self.server.memories = [self.memory("a1", "Prefers soy milk")]
        self.assertEqual(self.bridge.project(), 1)
        self.assertIn("My own note", alex.read_text(encoding="utf-8"))  # human edit intact
        conflict = self.vault / "Mobile/People/Alex (conflict from app).md"
        self.assertIn("Prefers soy milk", conflict.read_text(encoding="utf-8"))
        self.assertEqual(self.bridge.project(), 1)  # stays visible, nothing overwritten
        alex.unlink()  # the user resolves it by removing their copy
        self.assertEqual(self.bridge.project(), 0)
        self.assertIn("Prefers soy milk", alex.read_text(encoding="utf-8"))
        self.assertFalse(conflict.exists())

    def test_archived_memories_leave_the_overlay(self):
        self.server.memories = [self.memory("a1", "Prefers oat milk"), self.memory("b2", "Likes hiking")]
        self.bridge.project()
        self.server.memories = [self.memory("b2", "Likes hiking")]
        self.bridge.project()
        self.assertNotIn("oat milk", (self.vault / "Mobile/People/Alex.md").read_text(encoding="utf-8"))
        self.server.memories = []
        self.assertEqual(self.bridge.project(), 0)
        self.assertFalse((self.vault / "Mobile/People/Alex.md").exists())

    def test_items_carry_their_vault_note(self):
        self.bridge.tick()
        notes = {r["data"]["kind"]: r["data"]["note"] for (k, _), r in self.server.records.items() if k == "item"}
        self.assertEqual(notes, {"fact": "People/Alex.md", "task": "Projects/_Tasks.md", "daily": "Daily/2026-10-09.md"})


class BridgeConfigTests(unittest.TestCase):
    def test_bridge_requires_https_and_absolute_token_file(self):
        from second_brain.config import Config
        with tempfile.TemporaryDirectory() as tmp:
            base = {k: str(Path(tmp) / k) for k in
                    ["data_dir", "incoming_dir", "secret_file", "pipeline_config", "vault_path", "status_file"]}
            token = str(Path(tmp) / "bridge.token")
            for url, file in [("http://brain.example", token), ("https://brain.example", "bridge.token"),
                              ("https://user@brain.example", token), ("ftp://brain.example", token)]:
                write_json(Path(tmp) / "c.json", {**base, "bridge_url": url, "bridge_token_file": file})
                with self.assertRaises(ValueError, msg=url):
                    Config.load(Path(tmp) / "c.json")
            for url in ["https://brain.example", "http://127.0.0.1:8000", ""]:
                write_json(Path(tmp) / "c.json", {**base, "bridge_url": url, "bridge_token_file": token})
                self.assertEqual(Config.load(Path(tmp) / "c.json").bridge_url, url)


if __name__ == "__main__":
    unittest.main()
