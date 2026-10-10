"""Bridge export over real loopback HTTP against synthetic manifests, ledgers and speaker data.
The real server contract is exercised in server/tests/test_worker.py."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
import uuid
import wave

from second_brain.bridge import Bridge
from second_brain.io import write_json
from second_brain.queue import Queue
from second_brain.router import event_id, source_hash
from second_brain.speakers import Speakers

TOKEN = "producer-token"


class FakeServer(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.records, self.requests, self.mode = {}, [], "ok"  # ok | down | lose_response


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        s = self.server
        if s.mode == "down" or self.headers["Authorization"] != f"Bearer {TOKEN}":
            self.send_response(503 if s.mode == "down" else 401)
            self.end_headers()
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        s.requests.append(body)
        for r in body["records"]:
            s.records[(r["kind"], r["id"])] = r
        self.send_response(500 if s.mode == "lose_response" else 200)  # committed, but the worker never hears it
        self.end_headers()
        self.wfile.write(json.dumps({"accepted": len(body["records"])}).encode())


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
        self.bridge = Bridge(self.data, self.vault, f"http://127.0.0.1:{self.server.server_port}", TOKEN)

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
        for private in ("private chatter", "secret clip words", '"embedding":', '"vectors"', "0.wav"):
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
