from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "sbr_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
plugin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["sbr_plugin"] = plugin
spec.loader.exec_module(plugin)

from sbr_plugin.checklist import ChecklistError, ChecklistService, index_tasks
from sbr_plugin.transport import AmbiguousSendError, SendReceipt


class FakeSender:
    def __init__(self, ambiguous: bool = False):
        self.calls = []
        self.ambiguous = ambiguous

    def preflight(self):
        return None

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        if self.ambiguous:
            raise AmbiguousSendError("unknown outcome")
        return SendReceipt(chat_id=kwargs["chat_id"], message_id=320 + len(self.calls))


class ChecklistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "Decisions").mkdir(parents=True)
        (self.vault / "Projects").mkdir()
        (self.vault / "Decisions" / "one.md").write_text(
            "- Call the clinic <!-- router:evt-a -->\n"
            "- Submit the form %% router:evt-b %%\n",
            encoding="utf-8",
        )
        (self.vault / "Projects" / "_Tasks.md").write_text("", encoding="utf-8")
        self.sender = FakeSender()
        self.service = ChecklistService(
            vault=str(self.vault), chat_id=-1001, owner_user_id=42,
            timezone_name="America/Los_Angeles", sender=self.sender,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        return json.loads((self.vault / "System" / "Reminders" / "state.json").read_text())

    def test_indexes_legacy_html_and_native_obsidian_markers(self):
        indexed = index_tasks(self.vault)
        self.assertEqual(set(indexed), {"evt-a", "evt-b"})
        self.assertEqual(indexed["evt-a"][0].source, "Decisions/one.md")

    def test_rendering_confirmed_receipt_and_duplicate_request(self):
        first = asyncio.run(self.service.display(router_ids=["evt-a", "evt-b"], request_id="daily:2030-01-02"))
        second = asyncio.run(self.service.display(router_ids=["evt-a", "evt-b"], request_id="daily:2030-01-02"))
        self.assertTrue(first["confirmed"])
        self.assertEqual(second["status"], "confirmed")
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.sender.calls), 2)
        sent = self.sender.calls[0]
        self.assertEqual(sent["text"], "1. Call the clinic")
        self.assertEqual(self.sender.calls[1]["text"], "2. Submit the form")
        self.assertTrue(all(len(card["reply_markup"]) == 1 for card in self.sender.calls))
        self.assertLess(len(sent["reply_markup"][0][0]["callback_data"].encode()), 64)

    def test_done_reopen_authorization_binding_and_idempotence(self):
        asyncio.run(self.service.display(router_ids=["evt-a"], request_id="interactive:x"))
        data = self.sender.calls[0]["reply_markup"][0][0]["callback_data"]
        denied_user = self.service.callback(data=data, user_id=99, chat_id=-1001, message_id=321)
        denied_chat = self.service.callback(data=data, user_id=42, chat_id=-1002, message_id=321)
        stale_message = self.service.callback(data=data, user_id=42, chat_id=-1001, message_id=999)
        self.assertFalse(denied_user.ok or denied_chat.ok or stale_message.ok)
        done = self.service.callback(data=data, user_id=42, chat_id=-1001, message_id=321)
        repeated = self.service.callback(data=data, user_id=42, chat_id=-1001, message_id=321)
        self.assertTrue(done.ok and repeated.ok)
        self.assertIn("already recorded", repeated.message)
        self.assertIn("☑ Done", done.markup[0][0]["text"])
        self.assertEqual(self.state()["items"]["evt-a"]["status"], "done")
        reopen_data = done.markup[0][0]["callback_data"]
        reopened = self.service.callback(data=reopen_data, user_id=42, chat_id=-1001, message_id=321)
        self.assertTrue(reopened.ok)
        self.assertEqual(self.state()["items"]["evt-a"]["status"], "open")
        old_done = self.service.callback(data=data, user_id=42, chat_id=-1001, message_id=321)
        self.assertFalse(old_done.ok)
        self.assertEqual(self.state()["items"]["evt-a"]["status"], "open")
        new_done_data = reopened.markup[0][0]["callback_data"]
        new_done = self.service.callback(data=new_done_data, user_id=42, chat_id=-1001, message_id=321)
        self.assertTrue(new_done.ok)
        old_reopen = self.service.callback(data=reopen_data, user_id=42, chat_id=-1001, message_id=321)
        self.assertFalse(old_reopen.ok)
        self.assertEqual(self.state()["items"]["evt-a"]["status"], "done")

    def test_text_actions_share_writer_and_preserve_unrelated_state(self):
        state_path = self.vault / "System" / "Reminders" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "version": 1,
            "items": {"evt-a": {"status": "open", "until": None, "custom": {"keep": True}}},
            "unrelated": {"keep": True},
        }))
        self.service.change(action="done", router_id="evt-a")
        self.service.change(action="dismiss", router_id="evt-b")
        state = self.state()
        self.assertEqual(state["unrelated"], {"keep": True})
        self.assertEqual(state["items"]["evt-a"]["custom"], {"keep": True})
        self.assertEqual(state["items"]["evt-a"]["status"], "done")
        self.assertEqual(state["items"]["evt-b"]["status"], "dismissed")

    def test_unknown_and_ambiguous_ids_are_rejected(self):
        with self.assertRaises(ChecklistError):
            self.service.change(action="done", router_id="missing")
        with (self.vault / "Projects" / "_Tasks.md").open("w", encoding="utf-8") as f:
            f.write("- Duplicate <!-- router:evt-a -->\n")
        with self.assertRaises(ChecklistError):
            self.service.change(action="done", router_id="evt-a")

    def test_corrupt_state_fails_closed(self):
        state_path = self.vault / "System" / "Reminders" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ChecklistError):
            self.service.change(action="done", router_id="evt-a")
        self.assertEqual(state_path.read_text(encoding="utf-8"), "{broken")

    def test_malformed_item_status_until_and_record_fail_closed(self):
        state_path = self.vault / "System" / "Reminders" / "state.json"
        state_path.parent.mkdir(parents=True)
        bad_items = [
            {"evt-a": "not-an-object"},
            {"evt-a": {"status": "mystery", "until": None}},
            {"evt-a": {"status": "done", "until": "2030-01-01"}},
            {"evt-a": {"status": "snoozed", "until": "not-a-date"}},
        ]
        for items in bad_items:
            state_path.write_text(json.dumps({"version": 1, "items": items}), encoding="utf-8")
            with self.assertRaises(ChecklistError):
                self.service.change(action="done", router_id="evt-a")

    def test_display_filters_caps_three_and_bounds_text(self):
        long_text = "X" * 5000
        extra = ["evt-c", "evt-d", "evt-e", "evt-f", "evt-g"]
        (self.vault / "Projects" / "_Tasks.md").write_text(
            f"- {long_text} <!-- router:evt-c -->\n"
            + "".join(f"- Task {rid} <!-- router:{rid} -->\n" for rid in extra[1:]),
            encoding="utf-8",
        )
        state_path = self.vault / "System" / "Reminders" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "version": 1,
            "items": {
                "evt-a": {"status": "done", "until": None},
                "evt-b": {"status": "dismissed", "until": None},
                "evt-c": {"status": "snoozed", "until": "2999-01-01"},
            },
        }))
        result = asyncio.run(self.service.display(
            router_ids=["evt-a", "evt-b", *extra], request_id="bounded"
        ))
        self.assertTrue(result["confirmed"])
        self.assertEqual(len(self.sender.calls), 3)
        self.assertTrue(all(len(card["reply_markup"]) == 1 for card in self.sender.calls))
        self.assertLessEqual(len(self.sender.calls[0]["text"]), 3500)
        self.assertNotIn(long_text, self.sender.calls[0]["text"])

    def test_long_first_item_does_not_hide_other_button_targets(self):
        (self.vault / "Projects" / "_Tasks.md").write_text(
            "- " + "X" * 5000 + " <!-- router:long -->\n",
            encoding="utf-8",
        )
        asyncio.run(self.service.display(
            router_ids=["long", "evt-a", "evt-b"], request_id="long-list"
        ))
        text = self.sender.calls[0]["text"]
        self.assertLessEqual(len(text), 3500)
        self.assertEqual(self.sender.calls[1]["text"], "2. Call the clinic")
        self.assertEqual(self.sender.calls[2]["text"], "3. Submit the form")

    def test_filtered_empty_sends_nothing(self):
        state_path = self.vault / "System" / "Reminders" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "version": 1,
            "items": {"evt-a": {"status": "done", "until": None}},
        }))
        result = asyncio.run(self.service.display(router_ids=["evt-a"], request_id="filtered"))
        self.assertEqual(result["status"], "empty")
        self.assertEqual(self.sender.calls, [])

    def test_source_snapshot_change_rejects_old_callback(self):
        asyncio.run(self.service.display(router_ids=["evt-a"], request_id="snapshot"))
        data = self.sender.calls[0]["reply_markup"][0][0]["callback_data"]
        (self.vault / "Decisions" / "one.md").write_text(
            "- Call a different clinic <!-- router:evt-a -->\n"
            "- Submit the form %% router:evt-b %%\n",
            encoding="utf-8",
        )
        result = self.service.callback(data=data, user_id=42, chat_id=-1001, message_id=321)
        self.assertFalse(result.ok)
        self.assertNotIn("evt-a", self.state()["items"])

    def test_source_and_state_symlink_escape_are_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "leak.md").write_text("- Outside <!-- router:outside -->\n")
        shutil.rmtree(self.vault / "Decisions")
        (self.vault / "Decisions").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ChecklistError):
            index_tasks(self.vault)

        (self.vault / "Decisions").unlink()
        (self.vault / "Decisions").mkdir()
        (self.vault / "Decisions" / "one.md").write_text("- Safe <!-- router:evt-a -->\n")
        (self.vault / "System").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ChecklistError):
            self.service.change(action="done", router_id="evt-a")

    def test_concurrent_writers_preserve_every_item_and_unrelated_data(self):
        ids = [f"evt-{i}" for i in range(20)]
        (self.vault / "Projects" / "_Tasks.md").write_text(
            "".join(f"- Task {i} %% router:{rid} %%\n" for i, rid in enumerate(ids)), encoding="utf-8"
        )
        state_path = self.vault / "System" / "Reminders" / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"version": 1, "items": {}, "unrelated": [1, 2, 3]}))
        errors = []
        threads = [threading.Thread(target=lambda rid=rid: self._change_catching(rid, errors)) for rid in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        state = self.state()
        self.assertEqual(state["unrelated"], [1, 2, 3])
        self.assertTrue(all(state["items"][rid]["status"] == "done" for rid in ids))

    def _change_catching(self, router_id, errors):
        try:
            self.service.change(action="done", router_id=router_id)
        except Exception as exc:
            errors.append(exc)

    def test_ambiguous_send_is_reserved_and_never_retried(self):
        sender = FakeSender(ambiguous=True)
        service = ChecklistService(
            vault=str(self.vault), chat_id=-1001, owner_user_id=42,
            timezone_name="UTC", sender=sender,
        )
        first = asyncio.run(service.display(router_ids=["evt-a"], request_id="cron:2030-01-02"))
        second = asyncio.run(service.display(router_ids=["evt-a"], request_id="cron:2030-01-02"))
        self.assertEqual(first["status"], "ambiguous")
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["status"], "ambiguous")
        self.assertEqual(len(sender.calls), 1)

    def test_empty_selection_sends_nothing(self):
        result = asyncio.run(self.service.display(router_ids=[], request_id="empty"))
        self.assertEqual(result["status"], "empty")
        self.assertEqual(self.sender.calls, [])

    def test_each_card_is_bound_to_its_own_task_and_message(self):
        result = asyncio.run(self.service.display(router_ids=["evt-a", "evt-b"], request_id="cards"))
        self.assertEqual(result["message_ids"], [321, 322])
        first = self.sender.calls[0]["reply_markup"][0][0]["callback_data"]
        second = self.sender.calls[1]["reply_markup"][0][0]["callback_data"]
        self.assertFalse(self.service.callback(data=first, user_id=42, chat_id=-1001, message_id=322).ok)
        done = self.service.callback(data=second, user_id=42, chat_id=-1001, message_id=322)
        self.assertTrue(done.ok)
        self.assertEqual(len(done.markup), 1)
        self.assertNotIn("evt-a", self.state()["items"])
        self.assertEqual(self.state()["items"]["evt-b"]["status"], "done")

    def test_partial_send_keeps_confirmed_card_actionable_without_duplicate_send(self):
        class PartialSender(FakeSender):
            async def send(inner, **kwargs):
                if inner.calls:
                    inner.calls.append(kwargs)
                    raise AmbiguousSendError("second card uncertain")
                return await super().send(**kwargs)

        self.service.sender = sender = PartialSender()
        result = asyncio.run(self.service.display(router_ids=["evt-a", "evt-b"], request_id="partial"))
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["confirmed_count"], 1)
        first = sender.calls[0]["reply_markup"][0][0]["callback_data"]
        second = sender.calls[1]["reply_markup"][0][0]["callback_data"]
        self.assertTrue(self.service.callback(data=first, user_id=42, chat_id=-1001, message_id=321).ok)
        self.assertFalse(self.service.callback(data=second, user_id=42, chat_id=-1001, message_id=322).ok)
        again = asyncio.run(self.service.display(router_ids=["evt-a", "evt-b"], request_id="partial"))
        self.assertTrue(again["duplicate"])
        self.assertFalse(again["confirmed"])
        self.assertEqual(len(sender.calls), 2)

    def test_actual_hermes_plugin_doctor_registration(self):
        try:
            from hermes_cli.plugin_dev import doctor_plugin
        except ModuleNotFoundError as exc:
            if exc.name != "hermes_cli":
                raise
            self.skipTest("Hermes checkout required for real plugin registration validation")

        report = doctor_plugin(ROOT)
        self.assertTrue(report.ok, report.format_text())
        self.assertEqual(report.registered_tools, ("secondbrain_checklist",))

    def test_callback_edit_failure_reports_state_was_saved(self):
        class FakeContext:
            def __init__(inner):
                inner.factory = None

            def get_config(inner, key, default=None):
                return {
                    "vault": str(self.vault), "chat_id": "-1001", "owner_user_id": "42",
                    "timezone": "UTC",
                }.get(key, default)

            def register_tool(inner, **kwargs):
                inner.tool = kwargs

            def register_platform_handler(inner, platform, factory):
                self.assertEqual(platform, "telegram")
                inner.factory = factory

        class FakeApplication:
            def add_handler(inner, handler):
                inner.handler = handler

        class FakeQuery:
            data = "sbr:abcdefghijklmnop:d:0:0"
            from_user = type("User", (), {"id": 42})()
            message = type("Message", (), {"chat_id": -1001, "message_id": 321})()

            def __init__(inner):
                inner.answers = []

            async def edit_message_reply_markup(inner, **kwargs):
                raise RuntimeError("edit failed")

            async def answer(inner, text, show_alert=False):
                inner.answers.append((text, show_alert))

        state_path = self.vault / "System" / "Reminders" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "version": 1,
            "items": {},
            "secondbrain_checklist": {"deliveries": {"request": {
                "status": "confirmed", "token": "abcdefghijklmnop", "chat_id": -1001,
                "owner_user_id": 42, "message_id": 321, "router_ids": ["evt-a"],
                "snapshots": [{"text": "Call the clinic", "source": "Decisions/one.md"}],
            }}},
        }), encoding="utf-8")
        ctx = FakeContext()
        plugin.register(ctx)
        app = FakeApplication()
        ctx.factory(app, object())
        query = FakeQuery()
        update = type("Update", (), {"callback_query": query})()
        asyncio.run(app.handler.callback(update, None))
        self.assertEqual(self.state()["items"]["evt-a"]["status"], "done")
        self.assertIn("state saved", query.answers[0][0])
        self.assertTrue(query.answers[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
