from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from . import test_checklist as fixtures
from sbr_plugin.todos import TodoError


class TodoTests(unittest.TestCase):
    setUp = fixtures.ChecklistTests.setUp
    tearDown = fixtures.ChecklistTests.tearDown
    state = fixtures.ChecklistTests.state
    def migrate(self):
        self.service.sync(["evt-a", "evt-b"])
        return self.vault / "ToDos" / "Tasks.md"

    def test_due_dates_and_manual_edits_survive_reimport(self):
        source = self.vault / "Decisions" / "one.md"
        source.write_text("- Call clinic (owner: Eugene; due: 2030-01-02) %% router:evt-a %%\n", encoding="utf-8")
        self.service.sync(["evt-a"])
        path = self.vault / "ToDos" / "Tasks.md"
        self.assertIn("due: 2030-01-02", path.read_text())
        edited = path.read_text().replace("Call clinic", "Call my doctor").replace("2030-01-02", "2030-01-03")
        path.write_text(edited, encoding="utf-8")
        self.service.sync(["evt-a"])
        self.assertEqual(path.read_text(), edited)

    def test_button_checks_markdown_and_reopens_without_editing_intake(self):
        source = (self.vault / "Decisions" / "one.md").read_bytes()
        path = self.migrate()
        asyncio.run(self.service.display(router_ids=["evt-a"], request_id="new"))
        data = self.sender.calls[-1]["reply_markup"][0][0]["callback_data"]
        result = self.service.callback(data=data, user_id=42, chat_id=-1001, message_id=321)
        self.assertTrue(result.ok)
        self.assertIn("- [x] Call the clinic", path.read_text())
        result = self.service.callback(data=result.markup[0][0]["callback_data"], user_id=42, chat_id=-1001, message_id=321)
        self.assertTrue(result.ok)
        self.assertIn("- [ ] Call the clinic", path.read_text())
        self.assertEqual((self.vault / "Decisions" / "one.md").read_bytes(), source)

    def test_manual_completion_is_excluded_and_manual_task_is_adopted(self):
        path = self.migrate()
        path.write_text(path.read_text().replace("[ ] Call", "[x] Call") + "- [ ] My own task (due: 2030-02-03)\n", encoding="utf-8")
        result = self.service.sync([])
        self.assertEqual(len(result["tasks"]), 3)
        self.assertEqual(self.state()["items"]["evt-a"]["status"], "done")
        result = asyncio.run(self.service.display(router_ids=["evt-a"], request_id="manual"))
        self.assertEqual(result["status"], "empty")
        self.assertFalse(self.sender.calls)

    def test_deleted_task_is_not_reimported_or_actionable(self):
        path = self.migrate()
        path.write_text("\n".join(line for line in path.read_text().splitlines() if "evt-a" not in line), encoding="utf-8")
        self.service.sync(["evt-a"])
        self.assertNotIn("evt-a", path.read_text())
        with self.assertRaises(TodoError):
            self.service.change(action="done", router_id="evt-a")
        path.unlink()
        with self.assertRaises(TodoError):
            self.service.sync([])

    def test_markdown_survives_cache_write_failure_and_recovers(self):
        path = self.migrate()
        with patch.object(self.service.store, "write", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.service.change(action="done", router_id="evt-a")
        self.assertIn("[x] Call", path.read_text())
        self.service.sync([])
        self.assertEqual(self.state()["items"]["evt-a"]["status"], "done")

    def test_conflict_and_concurrent_edit_fail_closed(self):
        path = self.migrate()
        conflict = path.with_name("Tasks.sync-conflict-20300101.md")
        conflict.write_text("conflict", encoding="utf-8")
        with self.assertRaises(TodoError):
            self.service.change(action="done", router_id="evt-a")
        conflict.unlink()
        with self.assertRaises(TodoError):
            self.service.todos.write("outdated contents", "overwrite")
        self.assertIn("[ ] Call", path.read_text())

    def test_old_button_rejected_after_migration_and_manual_edit(self):
        asyncio.run(self.service.display(router_ids=["evt-a"], request_id="old"))
        data = self.sender.calls[-1]["reply_markup"][0][0]["callback_data"]
        path = self.migrate()
        self.assertFalse(self.service.callback(data=data, user_id=42, chat_id=-1001, message_id=321).ok)
        asyncio.run(self.service.display(router_ids=["evt-a"], request_id="new"))
        data = self.sender.calls[-1]["reply_markup"][0][0]["callback_data"]
        path.write_text(path.read_text().replace("Call the clinic", "Call another clinic"), encoding="utf-8")
        self.assertFalse(self.service.callback(data=data, user_id=42, chat_id=-1001, message_id=322).ok)

    def test_existing_done_state_migrates_and_snooze_roundtrips(self):
        self.service.change(action="done", router_id="evt-a")
        path = self.migrate()
        self.assertIn("[x] Call", path.read_text())
        self.service.change(action="snooze", router_id="evt-b", until="2099-01-02")
        self.service.sync([])
        self.assertEqual(self.state()["items"]["evt-b"]["until"], "2099-01-02")
        self.service.change(action="reopen", router_id="evt-b")
        self.assertNotIn("snoozed-until", path.read_text())

    def test_missing_vault_is_not_created_by_empty_sync(self):
        import shutil
        shutil.rmtree(self.vault)
        with self.assertRaises(TodoError):
            self.service.sync([])
        self.assertFalse(self.vault.exists())

    def test_scrub_moves_tasks_without_invalidating_buttons_or_cache(self):
        from sbr_plugin.maintenance import scrub
        path = self.migrate()
        asyncio.run(self.service.display(router_ids=["evt-a"], request_id="housekeeping"))
        data = self.sender.calls[-1]["reply_markup"][0][0]["callback_data"]
        done = self.service.callback(data=data, user_id=42, chat_id=-1001, message_id=321)
        self.assertTrue(done.ok)
        before_revision = self.state()["items"]["evt-a"]["revision"]
        self.assertEqual(scrub(self.vault)["moved_to_completed"], 1)
        self.assertGreater(path.read_text().index("[x] Call"), path.read_text().index("## Completed"))
        self.assertEqual(self.state()["items"]["evt-a"]["revision"], before_revision)
        reopen = self.service.callback(data=done.markup[0][0]["callback_data"], user_id=42, chat_id=-1001, message_id=321)
        self.assertTrue(reopen.ok)
        self.assertEqual(scrub(self.vault)["moved_to_tasks"], 1)
        self.assertLess(path.read_text().index("[ ] Call"), path.read_text().index("## Completed"))
        self.assertFalse(scrub(self.vault)["changed"])
