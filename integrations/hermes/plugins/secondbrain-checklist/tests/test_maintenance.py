import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("todo_organizer", Path(__file__).resolve().parents[1] / "maintenance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
organize = module.organize


class MaintenanceTests(unittest.TestCase):
    def test_moves_done_with_due_and_notes_to_bottom_and_is_idempotent(self):
        text = "# ToDos\n\n## Tasks\n\n- [x] Finished (due: 2030-01-02) %% router:a %%\n  My note\n  - [ ] Nested step\n- [ ] Pending %% router:b %%\n\n## Notes\n\nKeep this prose.\n"
        result, counts = organize(text)
        self.assertEqual(counts, {"moved_to_completed": 1, "moved_to_tasks": 0})
        self.assertLess(result.index("Pending"), result.index("## Notes"))
        self.assertLess(result.index("## Notes"), result.index("## Completed"))
        self.assertIn("- [x] Finished (due: 2030-01-02) %% router:a %%\n  My note\n  - [ ] Nested step\n", result)
        self.assertIn("Keep this prose.", result)
        again, counts = organize(result)
        self.assertEqual(again, result)
        self.assertEqual(sum(counts.values()), 0)

    def test_reopen_and_new_import_below_completed_move_up(self):
        text = "## Tasks\n- [ ] First\n\n## Completed\n- [x] Done\n- [ ] Reopened\n- [ ] New import\n"
        result, counts = organize(text)
        self.assertEqual(counts["moved_to_tasks"], 2)
        self.assertLess(result.index("Reopened"), result.index("## Completed"))
        self.assertLess(result.index("New import"), result.index("## Completed"))
        self.assertGreater(result.index("- [x] Done"), result.index("## Completed"))

    def test_manual_tasks_fenced_examples_and_dismissed_status(self):
        text = "## Tasks\n```md\n## Completed\n- [x] Example\n```\n- [-] Dismissed\n- [X] Manually completed\n- [ ] Active\n"
        result, counts = organize(text)
        self.assertEqual(counts["moved_to_completed"], 1)
        self.assertIn("```md\n## Completed\n- [x] Example\n```", result)
        self.assertLess(result.index("Dismissed"), result.rindex("## Completed"))
        self.assertGreater(result.index("Manually completed"), result.rindex("## Completed"))

    def test_preserves_crlf_and_existing_completed_prose(self):
        text = "## Completed\r\nArchived notes.\r\n- [x] Old\r\n\r\n## Tasks\r\n- [x] New\r\n- [ ] Keep\r\n"
        result, _ = organize(text)
        self.assertIn("Archived notes.\r\n- [x] Old\r\n", result)
        self.assertIn("## Completed\r\n", result)
        self.assertNotIn("\n", result.replace("\r\n", ""))
        self.assertEqual(organize(result)[0], result)

    def test_ambiguous_sections_and_unclosed_fences_are_rejected(self):
        for text in ("## Tasks\n## Tasks\n", "## Tasks\n## Completed\n## Completed\n", "## Tasks\n```\n", "# Custom list\n- [x] Done\n"):
            with self.assertRaises(ValueError):
                organize(text)

    def test_no_task_text_or_checkbox_status_is_invented(self):
        text = "## Tasks\n- [ ] Past due (due: 2020-01-01)\n- [x] Done"
        result, counts = organize(text)
        self.assertIn("- [ ] Past due (due: 2020-01-01)", result)
        self.assertTrue(result.endswith("- [x] Done"))
        self.assertEqual(counts["moved_to_completed"], 1)


if __name__ == "__main__":
    unittest.main()
