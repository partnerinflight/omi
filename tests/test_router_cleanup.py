import unittest
from second_brain.router_cleanup import clean_note
from second_brain.router import append_item
from pathlib import Path
import tempfile


class CleanupTests(unittest.TestCase):
    def test_new_bullets_do_not_publish_encoded_spaces_or_hard_breaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "day.md"
            append_item(path, "&#x20;Readable text.\\\n&#x20;More text.&#x20;", "abc")
            self.assertIn("- Readable text. More text. <!-- router:abc -->", path.read_text())

    def test_preserves_curated_sections_and_markers_and_is_idempotent(self):
        original = ("# Day\nHuman prose\n## Router Inbox\n"
                    "- Keep this.  \n  Source: `abc` · confidence 0.9 <!-- router:abc123 -->\n"
                    "- Trivial bargain.\\\n&#x20;Source: `def` <!-- router:def456 -->\n"
                    "- Human bullet without marker.\n## Curated\n- Human choice.\n")
        cleaned, removed = clean_note(original, {"def456"})
        self.assertEqual(removed, ["def456"])
        self.assertEqual(cleaned, "# Day\nHuman prose\n## Router Inbox\n"
                         "- Keep this. <!-- router:abc123 -->\n"
                         "- Human bullet without marker.\n## Curated\n- Human choice.\n")
        self.assertEqual(clean_note(cleaned, {"def456"}), (cleaned, []))

    def test_preserves_unknown_continuations(self):
        note = "## Router Inbox\n- Human edit <!-- router:abc -->\n  More context.\n"
        self.assertEqual(clean_note(note, set()), (note, []))
