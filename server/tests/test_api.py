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
        for _ in range(120):
            try:
                psycopg.connect(os.environ["SB_DATABASE_URL"]).close()
                break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            raise RuntimeError("postgres did not start")

        from sb_server.db import migrate
        assert migrate() == ["001_init.sql", "002_worker.sql"]
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

    def http(self, method, path, token=None, **kw):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
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


if __name__ == "__main__":
    unittest.main()
