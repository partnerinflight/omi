"""Milestone 5 inventory. The import and cutover against a real server are in
server/tests/test_api.py; this covers what is read from Tasks.md and what is refused."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from . import test_checklist as fixtures
from sbr_plugin import migrate
from sbr_plugin.maintenance import scrub
from sbr_plugin.todos import TodoError, TodoFile

REPO = Path(__file__).resolve().parents[5]


class MigrateTests(unittest.TestCase):
    setUp = fixtures.ChecklistTests.setUp
    tearDown = fixtures.ChecklistTests.tearDown

    def tasks(self, body: str) -> Path:
        self.service.sync(["evt-a", "evt-b"])  # adopts the intake and records todos_imported
        path = self.vault / "ToDos" / "Tasks.md"
        path.write_text("# ToDos\n\nSome prose about how I use this list.\n\n## Tasks\n\n" + body, encoding="utf-8")
        return path

    def test_inventory_maps_statuses_notes_and_tombstones(self):
        self.tasks(
            "- [ ] Call the clinic %% router:evt-a %%\n"
            "  ask about the referral\n"
            "    - [ ] bring insurance card %% router:sub-1 %%\n"
            "- [x] Done thing %% router:m1 %%\n"
            "- [-] Not doing %% router:m2 %%\n"
            "- [ ] Later %% snoozed-until:2030-01-02 %% %% router:m3 %%\n"
            "- [ ] Typed without an id\n")
        found = migrate.inventory(self.vault)
        by = {t["legacy_id"]: t for t in found["todos"]}
        self.assertEqual(by["evt-a"]["notes"], "  ask about the referral")
        self.assertEqual([by[i]["state"] for i in ("m1", "m2", "m3")], ["completed", "dismissed", "open"])
        self.assertEqual(by["m3"]["snoozed_until"], "2030-01-02")
        self.assertEqual((by["evt-b"]["deleted"], by["evt-b"]["title"]), (True, "Submit the form"))  # removed by hand
        r = found["report"]
        self.assertEqual((r["tasks"], r["tombstones"], r["nested_imported_flat"], r["unmarked_rows_not_imported"],
                          r["prose_lines_not_imported"]), (5, 1, ["sub-1"], 1, 1))
        self.assertEqual(migrate.inventory(self.vault)["source_hash"], found["source_hash"])  # read-only, stable

    def test_inventory_refuses_what_the_plugin_refuses(self):
        path = self.tasks("- [ ] One %% router:x1 %%\n- [ ] Two %% router:x1 %%\n")
        with self.assertRaises(TodoError):
            migrate.inventory(self.vault)
        path.write_text("# ToDos\n\n## Tasks\n\n- [ ] One %% router:x1 %%\n", encoding="utf-8")
        (path.parent / "Tasks.sync-conflict-20261010-1.md").write_text("x", encoding="utf-8")
        with self.assertRaises(TodoError):
            migrate.inventory(self.vault)

    def test_projection_format_round_trips_through_the_plugin(self):
        sys.path.insert(0, str(REPO / "src"))
        from second_brain.bridge import render_todos
        todos = [{"id": "u1", "legacy_id": "evt-a", "title": "Call the clinic", "state": "open", "snoozed_until": "2030-01-02",
                  "notes": "  ask about the referral"},
                 {"id": "u2", "legacy_id": None, "title": "Made in the app", "state": "completed", "snoozed_until": None, "notes": None},
                 {"id": "u3", "legacy_id": "gone", "title": "Deleted", "state": "dismissed", "deleted_at": "2026-10-10"}]
        path = self.vault / "ToDos" / "Tasks.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_todos(todos, managed=False))
        rows = TodoFile(self.vault).rows()
        self.assertEqual({k: (v["status"], v["until"]) for k, v in rows.items()},
                         {"evt-a": ("snoozed", "2030-01-02"), "u2": ("done", None)})
        self.assertEqual(migrate.inventory(self.vault)["todos"][0]["notes"], "  ask about the referral")
        self.assertTrue(scrub(self.vault)["success"])  # handed-back file is ordinary again

        path.write_bytes(render_todos(todos))
        before = path.read_bytes()
        self.assertEqual(scrub(self.vault)["skipped"], "managed by the Second Brain app")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
