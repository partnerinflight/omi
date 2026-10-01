"""Knowledge router: the regression cases of the standalone router (v4.1), plus the service wiring."""

from __future__ import annotations
import asyncio
import dataclasses
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from second_brain import router as r
from second_brain.io import write_json
from second_brain.runtime import Runtime
from tests.helpers import configuration, records, upload
from omi_local import upload_protocol as U

WHEN = "2026-09-08T08:00:00-07:00"


def decision(text, confidence=1.0):
    return dict(text=text, confidence=confidence, observed_at=WHEN)


def fact(name, text, entity_type="project"):
    return dict(entity_type=entity_type, entity_name=name, fact=text, confidence=0.99, observed_at=WHEN)


def managed(title, item):
    return (f"# {title}\n\nCurated prose the router must never touch.\n\n## Router Inbox\n"
            f"- 2026-09-01T10:00:00-07:00 — {item}  \n"
            "  Source: `old.txt` · `abc123def456` · confidence 0.99 <!-- router:1111111111111111 -->\n")


class FakeHermes:
    """Answers each router prompt; records which prompts were sent."""

    def __init__(self, extraction, collapse=None, verdicts=None):
        self.extraction, self.collapse, self.verdicts, self.calls = extraction, collapse, verdicts, []

    def __call__(self, cfg, system, user):
        if system is r.EXTRACT_PROMPT:
            self.calls.append("extract")
            return self.extraction
        payload = json.loads(user)
        if system is r.COLLAPSE_PROMPT:
            self.calls.append("collapse")
            return self.collapse or {"items": [dict(index=c["index"], action="keep", reason="keep")
                                               for c in payload["candidates"]]}
        self.calls.append("global")
        return self.verdicts or {"items": [dict(index=c["index"], action="new", matched_index=None, reason="new")
                                           for c in payload["candidates"]]}


class RouteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name) / "vault"
        self.vault.mkdir()

    def route(self, hermes, existing=None, text="synthetic source", source="job-w0000"):
        for rel, content in (existing or {}).items():
            (self.vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.vault / rel).write_text(content, encoding="utf-8")
        with patch.object(r, "chat", hermes):
            return r.route({}, self.vault, source, text, WHEN)

    def test_exact_duplicate_decision_is_not_written(self):
        existing = {"Decisions/2026-09-01.md": managed("2026-09-01", "Run heavy speech processing on the Threadripper.")}
        hermes = FakeHermes({"decisions": [decision("Run heavy speech processing on the Threadripper.")]},
                            verdicts={"items": [dict(index=0, action="duplicate", matched_index=0, reason="same")]})
        counts = self.route(hermes, existing)
        self.assertFalse((self.vault / "Decisions/2026-09-08.md").exists())
        self.assertEqual(counts["decisions"]["duplicate"], 1)

    def test_cross_category_paraphrase_is_suppressed(self):
        existing = {"Decisions/2026-09-01.md": managed("2026-09-01", "Run speech processing on the Threadripper.")}
        hermes = FakeHermes({"facts": [fact("Second Brain", "Speech processing runs on the Threadripper.")]},
                            verdicts={"items": [dict(index=0, action="duplicate", matched_index=0, reason="same")]})
        self.route(hermes, existing)
        self.assertFalse((self.vault / "Projects/Second Brain.md").exists())

    def test_refinement_and_conflict_are_appended_with_annotations_and_prose_kept(self):
        note = managed("Second Brain", "Speech runs on the Threadripper.")
        existing = {"Projects/Second Brain.md": note}
        hermes = FakeHermes(
            {"facts": [fact("Second Brain", "Speech runs on the Threadripper's GPU."),
                       fact("Second Brain", "Speech runs on the Raspberry Pi.")]},
            verdicts={"items": [dict(index=0, action="refinement", matched_index=0, reason="more specific"),
                                dict(index=1, action="conflict", matched_index=0, reason="contradicts")]})
        self.route(hermes, existing)
        text = (self.vault / "Projects/Second Brain.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(note), "existing content, including human prose, is untouched")
        self.assertIn("[REFINEMENT of fact: Speech runs on the Threadripper.] Speech runs on the Threadripper's GPU.", text)
        self.assertIn("[CONFLICTS with fact: Speech runs on the Threadripper.] Speech runs on the Raspberry Pi.", text)

    def test_new_knowledge_lands_in_its_notes_with_a_ledger(self):
        hermes = FakeHermes({
            "facts": [fact("Daniel", "Daniel is allergic to shellfish.", "person")],
            "decisions": [decision("Launch DeepQuill at 39 dollars.")],
            "tasks": [dict(text="Book the dentist", owner="Eugene", due="Friday", confidence=0.9)],
            "ideas": [dict(text="Offer a family plan", confidence=0.7)],
            "daily_summary": ["Discussed DeepQuill pricing"],
        })
        counts = self.route(hermes)
        self.assertIn("Daniel is allergic to shellfish.", (self.vault / "People/Daniel.md").read_text(encoding="utf-8"))
        self.assertIn("Launch DeepQuill at 39 dollars.", (self.vault / "Decisions/2026-09-08.md").read_text(encoding="utf-8"))
        self.assertIn("Book the dentist (owner: Eugene; due: Friday)",
                      (self.vault / "Projects/_Tasks.md").read_text(encoding="utf-8"))
        self.assertIn("Offer a family plan", (self.vault / "Ideas/2026-09-08.md").read_text(encoding="utf-8"))
        self.assertIn("Discussed DeepQuill pricing", (self.vault / "Daily/2026-09-08.md").read_text(encoding="utf-8"))
        self.assertEqual((counts["facts"]["written"], counts["decisions"]["written"], counts["tasks"]), (1, 1, 1))
        ledger = json.loads(next((self.vault / "System/Router/Ledger").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(ledger["source_id"], "job-w0000")
        self.assertEqual(hermes.calls, ["extract", "collapse"], "no existing knowledge: no reconciliation call")

    def test_batch_duplicates_collapse_to_one_item(self):
        hermes = FakeHermes(
            {"facts": [fact("Second Brain", "Speech runs on the Threadripper.")],
             "decisions": [decision("Run speech on the Threadripper.")]},
            collapse={"items": [dict(index=0, action="duplicate", duplicate_of=1, reason="later"),
                                dict(index=1, action="keep", reason="decision")]})
        counts = self.route(hermes)
        # An invalid duplicate target (pointing forward) is kept conservatively, as in v4.1.
        self.assertEqual((counts["facts"]["written"], counts["decisions"]["written"]), (1, 1))
        hermes = FakeHermes(
            {"decisions": [decision("Run speech on the Threadripper."), decision("Speech runs on the Threadripper.")]},
            collapse={"items": [dict(index=0, action="keep", reason="first"),
                                dict(index=1, action="duplicate", duplicate_of=0, reason="same")]})
        counts = self.route(hermes, source="job-w0001")
        self.assertEqual(counts["decisions"]["batch_duplicate"], 1)

    def test_same_source_is_routed_once_and_one_candidate_needs_no_collapse_call(self):
        hermes = FakeHermes({"decisions": [decision("Launch at 39 dollars.")]})
        self.route(hermes)
        self.assertEqual(hermes.calls, ["extract"])
        self.assertEqual(self.route(hermes), {"skipped": "already routed"})
        self.assertEqual(hermes.calls, ["extract"], "a routed source costs no further Hermes calls")

    def test_malformed_hermes_items_are_dropped_not_fatal(self):
        extraction = r.parse_extraction({
            "facts": [fact("A", "ok"), dict(entity_type="pet", entity_name="Rex", fact="x", confidence=1),
                      dict(entity_type="person", entity_name="", fact="x", confidence=1)],
            "decisions": [decision("ok"), dict(text="too sure", confidence=7), "not an object"],
            "tasks": "not a list",
            "daily_summary": ["fine", 3, "  "],
        })
        self.assertEqual([len(extraction[k]) for k in ("facts", "decisions", "tasks", "ideas")], [1, 1, 0, 0])
        self.assertEqual(extraction["daily_summary"], ["fine"])
        self.assertEqual(r.normalize_global({"items": [dict(index=0, action="duplicate", matched_index=9)]}, 1, 2)[0]["action"],
                         "new", "an out-of-range match is treated as new")

    def test_entity_names_cannot_escape_their_folder(self):
        hermes = FakeHermes({"facts": [fact("../../System/evil", "x")]})
        self.route(hermes)
        self.assertTrue((self.vault / "Projects/..-..-System-evil.md").exists())


class QueueTests(unittest.TestCase):
    def test_failures_back_off_then_stop_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            q = r.RouterQueue(Path(tmp) / "router.sqlite3")
            self.assertEqual(q.enqueue("job", [dict(id="w0000", final_transcript="text")], WHEN), 1)
            self.assertEqual(q.enqueue("job", [dict(id="w0000", final_transcript="text")], WHEN), 0, "idempotent")
            for attempt in range(6):
                with q.connect() as db:
                    db.execute("UPDATE routes SET next_attempt=0")
                item = q.claim()
                self.assertIsNotNone(item, attempt)
                q.fail(item, "URLError; see private service log")
            self.assertEqual(q.snapshot()["failed"], 1)
            self.assertIsNone(q.claim())
            self.assertEqual(q.retry_failed(), 1)
            self.assertEqual(q.claim()["id"], "job-w0000")


class FakeHermesServer(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = body["messages"][0]["content"]
        FakeHermesServer.seen.append((self.headers.get("Authorization"), system[:40]))
        reply = ({"decisions": [decision("Launch the project next week.")]} if system == r.EXTRACT_PROMPT
                 else {"items": []})
        data = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class ServiceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_published_conversation_is_routed_into_the_vault(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), FakeHermesServer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = dataclasses.replace(configuration(root), no_hermes=False)  # helpers default to no Hermes
            (root / "hermes-key.txt").write_text("gateway-secret")
            pipeline = json.loads(cfg.pipeline_config.read_text())
            pipeline.update(router_enabled=True, hermes_api_key_file=str(root / "hermes-key.txt"),
                            hermes_url=f"http://127.0.0.1:{server.server_port}/v1/chat/completions")
            write_json(cfg.pipeline_config, pipeline)
            runtime = Runtime(cfg)
            task = asyncio.create_task(runtime.run())
            try:
                for _ in range(100):
                    if runtime.server and runtime.server._server:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(await upload(runtime.server.bound_port, records(count=250)), U.MSG_BYE)
                for _ in range(600):
                    if runtime.router.snapshot()["done"]:
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(runtime.router.snapshot()["done"], 1, runtime.router.snapshot())
                decisions = list((cfg.vault_path / "Decisions").glob("*.md"))
                self.assertEqual(len(decisions), 1)
                self.assertIn("Launch the project next week.", decisions[0].read_text(encoding="utf-8"))
                self.assertEqual(FakeHermesServer.seen[0][0], "Bearer gateway-secret")
                runtime.status()
                status = json.loads(cfg.status_file.read_text())
                self.assertEqual((status["router"]["enabled"], status["router"]["done"]), (True, 1))
            finally:
                runtime.stop.set()
                await asyncio.wait_for(task, 10)


if __name__ == "__main__":
    unittest.main()
