"""Contract tests at the real boundaries: uvicorn over HTTP, Postgres 14 in Docker.

Run: cd server && uv run python -m unittest discover -s tests -t .
"""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import psycopg
import uvicorn

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from second_brain.bridge import Bridge  # noqa: E402
from second_brain.io import write_json  # noqa: E402
from second_brain.queue import Queue  # noqa: E402
from second_brain.clarifications import Clarifications  # noqa: E402
from second_brain.speakers import Speakers  # noqa: E402
import json  # noqa: E402
import wave  # noqa: E402

PG_IMAGE = "postgres:14-alpine"  # matches HA_Selfhost's shared postgres-db


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_port = free_port()
        cls.container = f"sb-server-test-{uuid.uuid4().hex[:8]}"
        subprocess.run(["docker", "run", "-d", "--rm", "--name", cls.container, "-e", "POSTGRES_PASSWORD=test",
                        "-p", f"127.0.0.1:{pg_port}:5432", PG_IMAGE], check=True, capture_output=True)
        os.environ["SB_DATABASE_URL"] = f"postgresql://postgres:test@127.0.0.1:{pg_port}/postgres"
        os.environ["SB_DB_TIMEOUT"] = "1"  # pool wait before 503, short for the exhaustion test
        for _ in range(120):
            try:
                psycopg.connect(os.environ["SB_DATABASE_URL"]).close()
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            raise RuntimeError("postgres did not start")

        from sb_server.db import migrate
        assert migrate() == ["001_init.sql", "002_worker.sql", "003_commands.sql", "004_memories.sql",
                         "005_todo_authority.sql"]
        assert migrate() == []  # idempotent

        port = free_port()
        cls.server = uvicorn.Server(uvicorn.Config("sb_server.app:app", port=port, log_level="warning"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        while not cls.server.started:
            time.sleep(0.05)
        cls.base = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        subprocess.run(["docker", "rm", "-f", cls.container], capture_output=True)

    def http(self, method, path, token=None, headers=None, **kw):
        headers = {**({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
        return httpx.request(method, self.base + path, headers=headers, timeout=10, **kw)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "sb_server", *args], check=True, capture_output=True, text=True,
                              env=os.environ, cwd=os.path.dirname(os.path.dirname(__file__))).stdout

    def pairing_code(self, name="test phone"):
        return self.cli("pair", name).split()[0]

    def paired(self):
        r = self.http("POST", "/v1/pair", json={"code": self.pairing_code()})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def op(self, token, entity_id, action, payload, base=0, op_id=None):
        op = {"operation_id": str(op_id or uuid.uuid4()), "entity_type": "todo", "entity_id": str(entity_id),
              "base_revision": base, "action": action, "payload": payload}
        r = self.http("POST", "/v1/operations", token, json={"operations": [op]})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["results"][0]

    def test_pairing_code_is_single_use_and_endpoints_require_auth(self):
        code = self.pairing_code()
        self.assertEqual(self.http("POST", "/v1/pair", json={"code": code.lower()}).status_code, 200)
        self.assertEqual(self.http("POST", "/v1/pair", json={"code": code}).status_code, 401)
        self.assertEqual(self.http("GET", "/v1/bootstrap").status_code, 401)
        self.assertEqual(self.http("GET", "/v1/bootstrap", "not-a-token").status_code, 401)

    def test_operation_replay_returns_same_result_and_reuse_is_rejected(self):
        token, todo, op_id = self.paired()["access_token"], uuid.uuid4(), uuid.uuid4()
        first = self.op(token, todo, "create", {"title": "Buy milk"}, op_id=op_id)
        self.assertEqual((first["outcome"], first["entity"]["revision"]), ("applied", 1))
        cursor = self.http("GET", "/v1/bootstrap", token).json()["cursor"]

        self.assertEqual(self.op(token, todo, "create", {"title": "Buy milk"}, op_id=op_id), first)
        reused = self.op(token, todo, "create", {"title": "Different"}, op_id=op_id)
        self.assertEqual((reused["outcome"], reused["error"]), ("rejected", "operation_id_reused"))
        # Neither retry wrote a change.
        self.assertEqual(self.http("GET", "/v1/changes", token, params={"cursor": cursor}).json()["changes"], [])

    def test_stale_revision_conflicts_and_returns_current_entity(self):
        token, todo = self.paired()["access_token"], uuid.uuid4()
        self.op(token, todo, "create", {"title": "Call plumber"})
        self.assertEqual(self.op(token, todo, "set_state", {"state": "completed"}, base=1)["outcome"], "applied")
        stale = self.op(token, todo, "set_title", {"title": "Call electrician"}, base=1)
        self.assertEqual((stale["outcome"], stale["error"]), ("conflict", "stale_revision"))
        self.assertEqual((stale["entity"]["state"], stale["entity"]["revision"]), ("completed", 2))

    def test_invalid_payloads_are_rejected_without_changes(self):
        token, todo = self.paired()["access_token"], uuid.uuid4()
        self.assertEqual(self.op(token, todo, "create", {"title": "  "})["error"], "invalid_title")
        self.op(token, todo, "create", {"title": "ok"})
        self.assertEqual(self.op(token, todo, "set_state", {"state": "toggle"}, base=1)["error"], "invalid_state")
        self.assertEqual(self.op(token, uuid.uuid4(), "set_state", {"state": "open"}, base=1)["error"], "not_found")

    def test_changes_follow_bootstrap_cursor_and_epoch_change_expires_it(self):
        token = self.paired()["access_token"]
        cursor = self.http("GET", "/v1/bootstrap", token).json()["cursor"]
        ids = [uuid.uuid4() for _ in range(3)]
        for i in ids:
            self.op(token, i, "create", {"title": str(i)})

        page = self.http("GET", "/v1/changes", token, params={"cursor": cursor, "limit": 2}).json()
        self.assertTrue(page["has_more"])
        rest = self.http("GET", "/v1/changes", token, params={"cursor": page["cursor"]}).json()
        self.assertFalse(rest["has_more"])
        seen = [c["entity"]["id"] for c in page["changes"] + rest["changes"]]
        self.assertEqual(seen, [str(i) for i in ids])

        with psycopg.connect(os.environ["SB_DATABASE_URL"], autocommit=True) as conn:
            conn.execute("UPDATE meta SET epoch = gen_random_uuid()")  # what `new-epoch` does after a restore
        self.assertEqual(self.http("GET", "/v1/changes", token, params={"cursor": rest["cursor"]}).status_code, 410)

    def test_refresh_rotates_and_reuse_revokes_device(self):
        tokens = self.paired()
        r = self.http("POST", "/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        self.assertEqual(r.status_code, 200)
        fresh = r.json()
        self.assertEqual(self.http("GET", "/v1/bootstrap", fresh["access_token"]).status_code, 200)

        # Replaying the rotated refresh token signals theft: the whole device is revoked.
        self.assertEqual(self.http("POST", "/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code, 401)
        self.assertEqual(self.http("GET", "/v1/bootstrap", fresh["access_token"]).status_code, 401)
        self.assertEqual(self.http("POST", "/v1/auth/refresh", json={"refresh_token": fresh["refresh_token"]}).status_code, 401)

    def test_revoke_blocks_device(self):
        token = self.paired()["access_token"]
        self.assertEqual(self.http("POST", "/v1/auth/revoke", token).status_code, 204)
        self.assertEqual(self.http("GET", "/v1/bootstrap", token).status_code, 401)

    def test_concurrent_edits_on_one_revision_apply_once_and_log_stays_ordered(self):
        token, todo = self.paired()["access_token"], uuid.uuid4()
        self.op(token, todo, "create", {"title": "race"})
        cursor = self.http("GET", "/v1/bootstrap", token).json()["cursor"]
        with ThreadPoolExecutor(8) as pool:
            outcomes = list(pool.map(lambda n: self.op(token, todo, "set_title", {"title": f"t{n}"}, base=1)["outcome"], range(16)))
        self.assertEqual(outcomes.count("applied"), 1)
        self.assertEqual(outcomes.count("conflict"), 15)
        changes = self.http("GET", "/v1/changes", token, params={"cursor": cursor}).json()["changes"]
        self.assertEqual([c["entity"]["revision"] for c in changes], [2])


    # --- Worker bridge: the real Windows exporter (stdlib only) against this server ---

    def test_producer_scope_is_isolated_from_devices(self):
        producer = self.cli("add-producer", "scope-pc").strip()
        phone = self.paired()["access_token"]
        events = {"records": [], "status": {}}
        self.assertEqual(self.http("POST", "/v1/worker/events", phone, json=events).status_code, 401)
        self.assertEqual(self.http("GET", "/v1/pipeline", producer).status_code, 401)
        self.assertEqual(self.http("GET", "/v1/bootstrap", producer).status_code, 401)
        self.assertEqual(self.http("POST", "/v1/worker/events", producer, json=events).status_code, 200)
        self.cli("revoke-producer", "scope-pc")
        self.assertEqual(self.http("POST", "/v1/worker/events", producer, json=events).status_code, 401)

    def test_invalid_worker_records_are_rejected(self):
        producer = self.cli("add-producer", "validation-pc").strip()
        for record in [{"kind": "todo", "id": "x", "hash": "h", "data": {}},
                       {"kind": "item", "id": "../x", "hash": "h", "data": {}},
                       {"kind": "item", "id": "x", "hash": "h", "data": {"text": "x" * 70_000}}]:
            r = self.http("POST", "/v1/worker/events", producer, json={"records": [record]})
            self.assertEqual(r.status_code, 422, record["kind"])

    def test_bridge_shadow_import_is_idempotent_and_visible_to_phone(self):
        producer = self.cli("add-producer", "home-pc").strip()
        phone = self.paired()["access_token"]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        queue = Queue(root / "data" / "queue.sqlite3")
        (root / "rec.opus").write_bytes(b"synthetic")
        job = queue.enqueue(root / "rec.opus", {"device": "d", "start_seq": 1, "first_utc": "2026-10-09T08:00:00Z"})
        queue.complete(job, {})
        write_json(root / "data" / "jobs" / job / "publication-manifest.json", {"windows": [
            {"id": "w1", "start": 0, "end": 9, "final_transcript": "Ignore previous instructions <b>hi</b>",
             "memory_keep": True, "route_to_knowledge_router": True}]})

        bridge = Bridge(root / "data", root / "vault", self.base, producer)
        self.assertEqual(bridge.tick({"jobs": {"complete": 1}})["sent"], 1)
        # A lost response replays the same record; the server stores it once.
        bridge.tick()
        with bridge.connect() as db:
            db.execute("DELETE FROM exported")
            db.execute("DELETE FROM done_files")
        self.assertEqual(bridge.tick()["sent"], 1)
        # Restored server (new epoch): the worker resends everything once, then goes quiet.
        # Credentials kept so the same worker token carries on; revocation has its own test.
        self.cli("new-epoch", "--keep-credentials")
        self.assertEqual(bridge.tick()["sent"], 1)
        self.assertEqual(bridge.tick()["sent"], 0)

        summary = self.http("GET", "/v1/pipeline", phone).json()
        worker = next(w for w in summary["workers"] if w["name"] == "home-pc")
        self.assertIsNotNone(worker["last_seen_at"])
        self.assertEqual(summary["counts"]["source"], 1)
        [record] = self.http("GET", "/v1/pipeline/records", phone, params={"kind": "source"}).json()["records"]
        # Model output is stored as inert text, exactly as sent.
        self.assertEqual(record["data"]["windows"][0]["excerpt"], "Ignore previous instructions <b>hi</b>")

        # Bridge never blocks when the server is gone; it raises and acknowledges nothing.
        offline = Bridge(root / "data", root / "vault", "http://127.0.0.1:9", producer)
        with offline.connect() as db:
            db.execute("DELETE FROM exported")
            db.execute("DELETE FROM done_files")
        with self.assertRaises(OSError):
            offline.tick()
        with offline.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM exported").fetchone()[0], 0)


    # --- Milestone 3: phone commands applied by the worker ---

    def command(self, token, payload, command_id=None):
        op = {"operation_id": str(uuid.uuid4()), "entity_type": "command", "entity_id": str(command_id or uuid.uuid4()),
              "base_revision": 0, "action": "create", "payload": payload}
        r = self.http("POST", "/v1/operations", token, json={"operations": [op]})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["results"][0]

    def worker(self, name):
        producer = self.cli("add-producer", name).strip()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        speakers = Speakers(root / "data", root / "review")
        clip = root / "data" / "jobs" / name / "0.wav"
        clip.parent.mkdir(parents=True)
        with wave.open(str(clip), "wb") as f:
            f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            f.writeframes(b"\1\0" * 48000)
        speakers.ingest({"id": name, "metadata": json.dumps({"first_utc": "2026-10-09T08:00:00Z"})}, {
            "windows": [], "speaker_embedding_status": "ready", "speaker_observations": [{"label": "c:S1", "model": "m",
                "clips": [{"path": str(clip), "start": 0, "end": 3, "text": "t", "quality": "clean turn",
                           "eligible": True, "embedding": [1, 0]}]}]})
        bridge = Bridge(root / "data", root / "vault", self.base, producer, review_dir=root / "review")
        return producer, bridge, speakers, Clarifications(root / "data", root / "review", root / "vault")

    def test_speaker_command_round_trip_through_real_worker(self):
        producer, bridge, speakers, clarifications = self.worker("speaker-pc")
        phone = self.paired()["access_token"]
        bridge.apply_commands(speakers, clarifications)  # drain commands other tests left in the shared database
        bridge.tick()
        [obs] = [r for r in self.http("GET", "/v1/pipeline/records", phone, params={"kind": "observation"}).json()["records"]
                 if r["data"]["source_id"] == "speaker-pc"]
        audio = self.http("GET", f"/v1/clips/{obs['id']}/0", phone)
        self.assertEqual((audio.status_code, audio.headers["content-type"]), (200, "audio/wav"))
        self.assertEqual(self.http("GET", f"/v1/clips/{obs['id']}/0", producer).status_code, 401)
        cursor = self.http("GET", "/v1/bootstrap", phone).json()["cursor"]

        # Windows is offline: the server accepts and the phone sees it waiting for the worker.
        saved = self.command(phone, {"action": "assign", "observation": obs["id"], "name": "Alex", "base_hash": obs["hash"]})
        self.assertEqual((saved["outcome"], saved["entity"]["state"]), ("applied", "pending_worker"))
        self.assertIsNotNone(self.http("GET", "/v1/pipeline", phone).json()["oldest_pending_command_at"])
        self.assertEqual(self.http("GET", "/v1/worker/commands", phone).status_code, 401)  # device cannot act as worker

        # The worker comes back, applies once, and the result reaches the phone through /v1/changes.
        self.assertEqual(bridge.apply_commands(speakers, clarifications), 1)
        self.assertEqual(bridge.apply_commands(speakers, clarifications), 0)
        bridge.tick()
        changes = self.http("GET", "/v1/changes", phone, params={"cursor": cursor}).json()["changes"]
        self.assertEqual([c["entity"]["state"] for c in changes if c["entity_type"] == "command"],
                         ["pending_worker", "applied_worker"])
        records = self.http("GET", "/v1/pipeline/records", phone, params={"kind": "observation"}).json()["records"]
        self.assertEqual(next(r for r in records if r["id"] == obs["id"])["data"]["provenance"], "confirmed")
        self.assertEqual(self.http("GET", f"/v1/clips/{obs['id']}/0", phone).status_code, 410)  # dropped once named

        # A retried result after a lost response does not change the outcome.
        late = self.http("POST", f"/v1/worker/commands/{saved['entity']['id']}/result", producer,
                         json={"ok": False, "error": "late"}).json()
        self.assertEqual(late["state"], "applied_worker")

        # Forget, then a job replay: the old reference is not restored.
        person = next(r for r in self.http("GET", "/v1/pipeline/records", phone, params={"kind": "person"}).json()["records"]
                      if r["data"]["name"] == "Alex")
        self.command(phone, {"action": "forget", "person": person["id"], "base_hash": person["hash"]})
        bridge.apply_commands(speakers, clarifications)
        speakers.ingest({"id": "speaker-pc", "metadata": json.dumps({})}, {"speaker_observations": [{"label": "c:S1", "clips": []}]})
        bridge.tick()
        records = self.http("GET", "/v1/pipeline/records", phone, params={"kind": "observation"}).json()["records"]
        self.assertEqual(next(r for r in records if r["id"] == obs["id"])["data"]["provenance"], "cleared")

        # A stale precondition fails terminally and is visible as failed_worker.
        stale = self.command(phone, {"action": "clear", "observation": obs["id"], "base_hash": obs["hash"]})
        bridge.apply_commands(speakers, clarifications)
        [entity] = [c["entity"] for c in self.http("GET", "/v1/changes", phone, params={"cursor": cursor}).json()["changes"]
                    if c["entity"]["id"] == stale["entity"]["id"] and c["entity"]["revision"] == 2]
        self.assertEqual(entity["state"], "failed_worker")

    def test_invalid_commands_are_rejected_at_the_server(self):
        phone = self.paired()["access_token"]
        for payload in [{"action": "assign", "observation": "o1"},  # neither person nor name
                        {"action": "assign", "observation": "o1", "person": "p", "name": "x"},
                        {"action": "rename", "person": "p", "name": "\n"},
                        {"action": "forget", "person": "../p"},
                        {"action": "clarify", "item": "abc", "answer": "x" * 4001},
                        {"action": "train_model"},
                        {"action": "clear", "observation": "o1", "extra": 1}]:
            result = self.command(phone, payload)
            self.assertEqual(result["outcome"], "rejected", payload)
        command_id = uuid.uuid4()
        self.command(phone, {"action": "forget", "person": "p"}, command_id)
        self.assertEqual(self.command(phone, {"action": "forget", "person": "p"}, command_id)["error"], "already_exists")

    def test_clip_upload_validates_hash_and_owner(self):
        producer, bridge, *_ = self.worker("clip-pc")
        bridge.tick()
        headers = {"Authorization": f"Bearer {producer}", "X-Content-SHA256": "0" * 64}
        r = httpx.put(self.base + "/v1/worker/clips/unknown/0", content=b"x", headers=headers)
        self.assertEqual(r.status_code, 422)  # hash mismatch is checked before anything is stored
        import hashlib
        headers["X-Content-SHA256"] = hashlib.sha256(b"x").hexdigest()
        self.assertEqual(httpx.put(self.base + "/v1/worker/clips/unknown/0", content=b"x", headers=headers).status_code, 404)


    # --- Milestone 4: memories ---

    def memory_op(self, token, mid, action, payload, base):
        op = {"operation_id": str(uuid.uuid4()), "entity_type": "memory", "entity_id": mid,
              "base_revision": base, "action": action, "payload": payload}
        r = self.http("POST", "/v1/operations", token, json={"operations": [op]})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["results"][0]

    def test_memories_import_edit_replay_and_project(self):
        producer = self.cli("add-producer", "memory-pc").strip()
        phone = self.paired()["access_token"]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        queue = Queue(root / "data" / "queue.sqlite3")
        (root / "rec.opus").write_bytes(b"memory test")
        job = queue.enqueue(root / "rec.opus", {"device": "m", "start_seq": 7, "first_utc": "2026-10-09T08:00:00Z"})
        queue.complete(job, {})
        write_json(root / "data" / "jobs" / job / "publication-manifest.json", {"windows": [
            {"id": "w1", "start": 0, "end": 9, "final_transcript": "Alex said she prefers oat milk.",
             "memory_keep": True, "route_to_knowledge_router": True}]})
        from second_brain.router import event_id, source_hash
        shash = source_hash(f"{job}-w1", "Alex said she prefers oat milk.")
        write_json(root / "vault" / "System/Router/Ledger" / f"{shash}.json", {
            "source_hash": shash, "source_id": f"{job}-w1", "extraction": {
                "facts": [{"entity_type": "person", "entity_name": "Alex", "fact": "Prefers oat milk", "confidence": 0.9,
                           "observed_at": None}],
                "decisions": [], "ideas": [{"text": "Oat milk subscription", "confidence": 0.6}],
                "daily_summary": ["Groceries"], "clarifications": [],
                "tasks": [{"text": "Buy oat milk", "owner": "", "due": "", "confidence": 0.8}]}})
        bridge = Bridge(root / "data", root / "vault", self.base, producer)
        cursor = self.http("GET", "/v1/bootstrap", phone).json()["cursor"]
        bridge.tick()

        fact_id = event_id(shash, "fact", "person:Alex", "Prefers oat milk")
        mine = {m["id"]: m for m in self.http("GET", "/v1/bootstrap", phone).json()["memories"]}
        fact = mine[fact_id]
        self.assertEqual((fact["state"], fact["entity_name"], fact["confidence"]), ("proposed", "Alex", 0.9))
        self.assertEqual(fact["evidence"]["excerpt"], "Alex said she prefers oat milk.")  # cached for offline detail
        self.assertEqual(fact["evidence"]["note"], "People/Alex.md")
        idea = next(m for m in mine.values() if m["kind"] == "idea" and m["text"] == "Oat milk subscription")
        self.assertFalse(any(m["text"] in ("Buy oat milk", "Groceries") for m in mine.values()))  # tasks/daily are not memories
        new = self.http("GET", "/v1/changes", phone, params={"cursor": cursor}).json()["changes"]
        self.assertEqual({c["entity"]["id"] for c in new if c["entity_type"] == "memory"}, {fact_id, idea["id"]})

        # Correct, confirm; a stale edit conflicts; bad text is rejected.
        edited = self.memory_op(phone, fact_id, "set_text", {"text": "Prefers oat milk, not soy"}, 1)
        self.assertEqual((edited["outcome"], edited["entity"]["original_text"]), ("applied", "Prefers oat milk"))
        self.assertEqual(self.memory_op(phone, fact_id, "set_state", {"state": "confirmed"}, 2)["outcome"], "applied")
        self.assertEqual(self.memory_op(phone, fact_id, "set_text", {"text": "x"}, 2)["error"], "stale_revision")
        for bad in ("", "two\nlines", "hidden %% comment", "x" * 2001):
            self.assertEqual(self.memory_op(phone, fact_id, "set_text", {"text": bad}, 3)["error"], "invalid_text")
        self.assertEqual(self.memory_op(phone, idea["id"], "set_state", {"state": "archived"}, 1)["outcome"], "applied")
        self.assertEqual(self.memory_op(phone, "nope", "set_state", {"state": "archived"}, 1)["error"], "not_found")
        self.assertEqual(self.op(phone, "not-a-uuid", "create", {"title": "x"})["error"], "invalid_entity_id")

        # Extraction replay (everything resent) changes nothing the user decided.
        with bridge.connect() as db:
            db.execute("DELETE FROM exported")
            db.execute("DELETE FROM done_files")
        bridge.tick()
        mine = {m["id"]: m for m in self.http("GET", "/v1/bootstrap", phone).json()["memories"]}
        self.assertEqual((mine[fact_id]["text"], mine[fact_id]["state"], mine[fact_id]["revision"]),
                         ("Prefers oat milk, not soy", "confirmed", 3))
        self.assertEqual(mine[idea["id"]]["state"], "archived")

        # Only confirmed memories reach the vault overlay; devices cannot read the worker route.
        self.assertEqual(self.http("GET", "/v1/worker/memories", phone).status_code, 401)
        self.assertEqual(bridge.project(), 0)
        note = (root / "vault" / "Mobile/People/Alex.md").read_text(encoding="utf-8")
        self.assertIn(f"Prefers oat milk, not soy %% memory:{fact_id} %%", note)
        self.assertNotIn("subscription", note)
        self.assertFalse((root / "vault" / "Mobile/Ideas.md").exists())


    # --- Milestone 5: todo migration, cutover, Hermes server mode, rollback ---

    def test_todo_migration_cutover_and_rollback(self):
        import asyncio
        import importlib.util
        from datetime import date as day, timedelta
        root = Path(__file__).resolve().parents[2] / "integrations/hermes/plugins/secondbrain-checklist"
        spec = importlib.util.spec_from_file_location("sbr_m5", root / "__init__.py", submodule_search_locations=[str(root)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules["sbr_m5"] = plugin
        spec.loader.exec_module(plugin)
        from sbr_m5 import migrate as legacy
        from sbr_m5.checklist import ChecklistService
        from sbr_m5.server_todos import ServerTodos
        from sbr_m5.maintenance import scrub
        from sbr_m5.todos import TodoFile
        from sbr_m5.transport import SendReceipt

        class Sender:
            calls = []

            def preflight(self):
                pass

            async def send(self, **kw):
                self.calls.append(kw)
                return SendReceipt(chat_id=kw["chat_id"], message_id=500 + len(self.calls))

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        vault = Path(tmp.name) / "vault"
        (vault / "Decisions").mkdir(parents=True)
        (vault / "Projects").mkdir()
        (vault / "Decisions/one.md").write_text("- Call the clinic %% router:evt-a %%\n- Later intake %% router:evt-c %%\n")
        (vault / "Projects/_Tasks.md").write_text("")
        sender = Sender()
        hermes = lambda todos=None: ChecklistService(vault=str(vault), chat_id=-1001, owner_user_id=42,  # noqa: E731
                                                     timezone_name="America/Los_Angeles", sender=sender, todos=todos)
        hermes().sync(["evt-a"])
        tasks = vault / "ToDos/Tasks.md"
        tasks.write_text(tasks.read_text() + "  bring the referral\n- [x] Old done %% router:m1 %%\n\nMy own prose.\n")
        old_button = asyncio.run(hermes().display(router_ids=["evt-a"], request_id="before"))
        self.assertTrue(old_button["confirmed"])

        producer, phone = self.cli("add-producer", "migration-pc").strip(), self.paired()["access_token"]
        dry = legacy.inventory(vault)
        self.assertEqual((dry["report"]["tasks"], dry["report"]["prose_lines_not_imported"]), (2, 3))  # incl. the 2 header lines
        first = legacy.apply(vault, self.base, producer, "todos-test")["result"]
        self.assertEqual(first, {"created": 2, "updated": 0, "unchanged": 0})
        self.assertEqual(legacy.apply(vault, self.base, producer, "todos-test")["result"]["unchanged"], 2)  # repeat: no effect
        todos = {t["legacy_id"]: t for t in self.http("GET", "/v1/bootstrap", phone).json()["todos"] if t["legacy_id"]}
        clinic = todos["evt-a"]
        self.assertEqual((clinic["notes"], todos["m1"]["state"]), ("  bring the referral", "completed"))
        # Before cutover the app cannot change imported todos; Tasks.md still owns them.
        self.assertEqual(self.op(phone, clinic["id"], "set_state", {"state": "completed"}, base=1)["error"], "legacy_authority")
        hermes().change(action="done", router_id="evt-a")  # a legacy edit before the final delta
        self.assertEqual(legacy.apply(vault, self.base, producer, "todos-test")["result"]["updated"], 1)
        hermes().change(action="reopen", router_id="evt-a")
        legacy.apply(vault, self.base, producer, "todos-test")  # final delta, under the writer lock

        (Path(tmp.name) / "data").mkdir()
        bridge = Bridge(Path(tmp.name) / "data", vault, self.base, producer)
        self.assertEqual(bridge.project_todos(), "legacy")  # nothing is written before cutover
        self.cli("authority", "cutover")
        self.assertEqual(self.http("POST", "/v1/migration/todos", producer, json={
            "migration_id": "late", "source_hash": "0" * 64, "todos": []}).status_code, 409)  # stale old writer refused

        integration = self.cli("add-integration", "hermes").strip()
        server_mode = lambda: hermes(ServerTodos(vault, self.base, integration))  # noqa: E731
        # Buttons sent before cutover are refused rather than editing the wrong store.
        data = old_button and sender.calls[0]["reply_markup"][0][0]["callback_data"]
        self.assertFalse(server_mode().callback(data=data, user_id=42, chat_id=-1001, message_id=501).ok)
        sent = asyncio.run(server_mode().display(router_ids=["evt-a"], request_id="after"))
        self.assertTrue(sent["confirmed"])
        data = sender.calls[-1]["reply_markup"][0][0]["callback_data"]
        self.assertTrue(server_mode().callback(data=data, user_id=42, chat_id=-1001, message_id=500 + len(sender.calls)).ok)
        bootstrap = lambda: {t["legacy_id"] or t["id"]: t for t in self.http("GET", "/v1/bootstrap", phone).json()["todos"]}  # noqa: E731
        self.assertEqual(bootstrap()["evt-a"]["state"], "completed")  # Telegram Done went to the server
        until = (day.today() + timedelta(days=3)).isoformat()
        server_mode().change(action="snooze", router_id="m1", until=until)
        self.assertEqual(bootstrap()["m1"]["snoozed_until"], until)
        self.assertEqual(server_mode().sync(["evt-c"])["added"], 1)  # intake promotion creates a server todo
        self.assertEqual(server_mode().sync(["evt-c"])["added"], 0)
        made = uuid.uuid4()
        self.op(phone, made, "create", {"title": "Made on the phone"})

        # Tasks.md changed after the final import: nothing is overwritten until it is re-imported.
        original = tasks.read_bytes()
        tasks.write_bytes(original + b"- [ ] Typed after the import\n")
        self.assertEqual(bridge.project_todos(), "conflict")
        self.assertIn(b"Typed after the import", tasks.read_bytes())
        self.assertFalse((vault / "ToDos/Tasks (before app cutover).md").exists())  # only the imported bytes are archived
        tasks.write_bytes(original)

        # The vault projection: the original file, prose and all, is kept; Hermes' format is written.
        self.assertEqual(bridge.project_todos(), "projected")
        self.assertIn("My own prose.", (vault / "ToDos/Tasks (before app cutover).md").read_text())
        projected = tasks.read_text()
        self.assertIn("managed_by: second-brain-app", projected)
        self.assertIn(f"- [ ] Old done %% snoozed-until:{until} %% %% router:m1 %%", projected)
        self.assertIn(f"Made on the phone %% router:{made} %%", projected)
        self.assertEqual(scrub(vault)["changed"], False)  # housekeeping stays off

        # Rollback: stop app writers, hand the file back, and Hermes in file mode sees every app change.
        self.cli("authority", "rollback")
        self.assertEqual(bridge.project_todos(), "handed_back")
        self.assertNotIn("managed_by", tasks.read_text())
        rows = TodoFile(vault).rows()
        self.assertEqual((rows["evt-a"]["status"], rows["m1"]["status"], rows[str(made)]["text"]),
                         ("done", "snoozed", "Made on the phone"))
        self.assertEqual(bridge.project_todos(), "legacy")  # handed back once, then left alone
        again = legacy.apply(vault, self.base, producer, "todos-test")["result"]
        self.assertEqual((again["created"], again["updated"]), (0, 0))  # nothing duplicated, nothing lost
        self.assertEqual(sum(t["title"] == "Made on the phone" for t in bootstrap().values()), 1)


    def test_oversized_bodies_get_413_before_auth(self):
        big = b"{" + b" " * 300_000 + b"}"
        # No token at all: the cap answers first, not the 401.
        self.assertEqual(self.http("POST", "/v1/pair", content=big,
                                   headers={"Content-Type": "application/json"}).status_code, 413)
        self.assertEqual(self.http("POST", "/v1/operations", content=b"x" * 1_200_000).status_code, 413)

        def chunks():  # no Content-Length: Transfer-Encoding: chunked
            for _ in range(40):
                yield b" " * 10_000
        r = httpx.post(self.base + "/v1/pair", content=chunks(), headers={"Content-Type": "application/json"}, timeout=10)
        self.assertEqual(r.status_code, 413)
        # Normal requests still work.
        self.assertEqual(self.http("POST", "/v1/pair", json={"code": "WRONGCODE1"}).status_code, 401)

    def test_route_caps_allow_real_payloads(self):
        from sb_server.limits import cap_for
        self.assertEqual(cap_for("PUT", "/v1/worker/clips/obs-1/0"), 2_100_000)
        self.assertEqual(cap_for("POST", "/v1/worker/events"), 7_000_000)
        self.assertEqual(cap_for("POST", "/v1/migration/todos"), 4_000_000)
        self.assertEqual(cap_for("POST", "/v1/operations"), 1_100_000)
        self.assertEqual(cap_for("POST", "/v1/pair"), 256_000)
        self.assertEqual(cap_for("GET", "/v1/bootstrap"), 256_000)

    def test_api_docs_are_not_served(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.http("GET", path).status_code, 404, path)

    def test_exhausted_pool_answers_503_not_a_hang(self):
        from sb_server import db
        self.assertEqual(self.http("GET", "/v1/health").status_code, 200)  # pool exists
        held = [db.pool().getconn() for _ in range(db.pool().max_size)]
        try:
            started = time.monotonic()
            r = self.http("GET", "/v1/health")
            self.assertEqual((r.status_code, r.headers.get("retry-after")), (503, "5"))
            self.assertLess(time.monotonic() - started, 5)
        finally:
            for conn in held:
                db.pool().putconn(conn)
        self.assertEqual(self.http("GET", "/v1/health").status_code, 200)

    def test_pairing_limiter_is_per_ip_with_a_global_ceiling(self):
        from sb_server import app as api
        api.pairing_failures.reset()
        attacker, owner = {"X-Forwarded-For": "203.0.113.7"}, {"X-Forwarded-For": "198.51.100.2"}
        for _ in range(10):
            self.assertEqual(self.http("POST", "/v1/pair", json={"code": "WRONGCODE1"}, headers=attacker).status_code, 401)
        r = self.http("POST", "/v1/pair", json={"code": self.pairing_code()}, headers=attacker)
        self.assertEqual((r.status_code, r.headers.get("retry-after")), (429, "600"))
        # Another address still pairs.
        self.assertEqual(self.http("POST", "/v1/pair", json={"code": self.pairing_code()}, headers=owner).status_code, 200)
        api.pairing_failures.reset()

    def test_limiter_counts_concurrent_attempts_and_global_ceiling(self):
        from sb_server.auth import FailureLimiter
        lim = FailureLimiter(per_key=10, window=600, total=100, total_window=3600)
        slots = [lim.reserve("a") for _ in range(12)]
        self.assertEqual(sum(s is not None for s in slots), 10)  # in-flight attempts count
        lim.release("a", slots[0])  # a success frees its slot
        self.assertIsNotNone(lim.reserve("a"))
        lim = FailureLimiter(per_key=10, window=600, total=3, total_window=3600)
        self.assertEqual([lim.reserve(k) is not None for k in "abcd"], [True, True, True, False])

    def test_devices_list_and_revoke_cut_off_a_live_session(self):
        tokens = self.http("POST", "/v1/pair", json={"code": self.pairing_code("stolen phone")}).json()
        listing = self.cli("devices")
        self.assertIn("stolen phone", listing)
        self.assertIn("device", listing)
        self.cli("revoke-device", "stolen phone")
        self.assertEqual(self.http("GET", "/v1/bootstrap", tokens["access_token"]).status_code, 401)
        self.assertEqual(self.http("POST", "/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code, 401)
        self.assertIn("revoked", self.cli("devices"))

    def test_expired_codes_and_tokens_are_refused(self):
        code = self.pairing_code()
        with psycopg.connect(os.environ["SB_DATABASE_URL"], autocommit=True) as c:
            c.execute("UPDATE pairing_codes SET expires_at = now() - interval '1 second' WHERE used_at IS NULL")
        self.assertEqual(self.http("POST", "/v1/pair", json={"code": code}).status_code, 401)
        token = self.paired()["access_token"]
        with psycopg.connect(os.environ["SB_DATABASE_URL"], autocommit=True) as c:
            c.execute("UPDATE tokens SET expires_at = now() - interval '1 second' WHERE kind = 'access'")
        self.assertEqual(self.http("GET", "/v1/bootstrap", token).status_code, 401)

    def test_new_epoch_revokes_every_credential_unless_kept(self):
        phone = self.paired()
        worker = self.cli("add-producer", "epoch-pc").strip()
        out = self.cli("new-epoch")
        self.assertTrue(out.startswith("epoch: "))
        self.assertIn("revoked:", out)
        self.assertEqual(self.http("GET", "/v1/bootstrap", phone["access_token"]).status_code, 401)
        self.assertEqual(self.http("POST", "/v1/auth/refresh", json={"refresh_token": phone["refresh_token"]}).status_code, 401)
        self.assertEqual(self.http("GET", "/v1/worker/commands", worker).status_code, 401)
        # Worker data from a revoked producer is no longer shown.
        fresh = self.paired()["access_token"]
        self.assertNotIn("epoch-pc", [w["name"] for w in self.http("GET", "/v1/pipeline", fresh).json()["workers"]])
        kept = self.paired()
        self.cli("new-epoch", "--keep-credentials")
        self.assertEqual(self.http("GET", "/v1/bootstrap", kept["access_token"]).status_code, 200)

if __name__ == "__main__":
    unittest.main()
