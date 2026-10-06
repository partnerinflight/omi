import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from second_brain.clarifications import Clarifications
from second_brain.router import route
from second_brain.speakers import Speakers


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.data, self.review = root/'vault', root/'data', root/'review'
        self.store = Clarifications(self.data, self.review, self.vault)
        self.content = 'Meet on Wednesday at 11 so Alex can attend.'
        extraction = dict(decisions=[dict(text=self.content, confidence=.9)],
                          clarifications=[dict(text=self.content, question='Who is this meeting with?')])
        with patch('second_brain.router.chat', return_value=extraction):
            route({}, self.vault, 'source', 'Speaker 1: Wednesday at eleven.', '2026-10-01', self.store)
        self.store.catalog()
        self.item = json.loads((self.review/'clarifications.json').read_text(encoding="utf-8"))['items'][0]
        self.path = self.vault / self.item['path']
        self.request = dict(id=str(uuid.uuid4()), action='clarify', observation=self.item['id'],
                            name='2026-10-01 — Meet with the design team on Wednesday at 11.')

    def test_real_mailbox_save_preserves_other_prose_and_replay(self):
        before = self.path.read_text(encoding="utf-8")
        self.path.write_text(before + '\n## My notes\nLeave this alone.\n', encoding="utf-8")
        speakers = Speakers(self.data, self.review)
        request_path = self.review/'requests'/f"{self.request['id']}.json"
        request_path.write_text(json.dumps(self.request))
        speakers.tick(self.store.command)
        self.assertTrue(json.loads((self.review/'responses'/request_path.name).read_text(encoding="utf-8"))['ok'])
        self.assertIn(self.request['name'], self.path.read_text(encoding="utf-8"))
        self.assertIn('Leave this alone.', self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.store.count(), 0)
        self.assertTrue(self.store.command(self.request)['ok'])
        self.assertEqual(len(list((self.data/'clarification-backups').glob('*.md'))), 1)

    def test_human_edit_is_not_overwritten_and_can_be_dismissed(self):
        changed = self.path.read_text(encoding="utf-8").replace(self.content, 'My corrected meeting.')
        self.path.write_text(changed, encoding="utf-8")
        self.assertFalse(self.store.command(self.request)['ok'])
        self.assertEqual(self.path.read_text(encoding="utf-8"), changed)
        self.assertTrue(self.store.command(dict(self.request, action='clarify_dismiss'))['ok'])
        self.assertEqual(self.store.count(), 0)

    def test_resume_after_note_write_before_database_commit(self):
        old = self.path.read_text(encoding="utf-8")
        with patch('second_brain.clarifications.atomic_write') as write:
            from second_brain.io import atomic_write
            def interrupt(path, data):
                atomic_write(path, data)
                if path == self.path.resolve():
                    raise OSError('simulated crash after publication')
            write.side_effect = interrupt
            with self.assertRaises(OSError): self.store.command(self.request)
        self.assertNotEqual(self.path.read_text(encoding="utf-8"), old)
        restarted = Clarifications(self.data, self.review, self.vault)
        self.assertTrue(restarted.command(self.request)['ok'])
        self.assertEqual(restarted.count(), 0)

    def test_unchanged_and_multiline_answers_rejected(self):
        for answer in (self.item['text'], '', 'One\n- another', '<!-- router:fake -->'):
            self.assertFalse(self.store.command(dict(self.request, name=answer))['ok'])
        self.assertEqual(self.store.count(), 1)

    def test_source_replay_does_not_reopen_or_duplicate_review(self):
        self.store.command(self.request)
        self.assertEqual(route({}, self.vault, 'source', 'Speaker 1: Wednesday at eleven.', '2026-10-01', self.store), {'skipped': 'already routed'})
        self.assertEqual(self.store.count(), 0)

    def test_missing_note_returns_actionable_error(self):
        self.path.unlink()
        result = self.store.command(self.request)
        self.assertFalse(result['ok'])
        self.assertIn('moved or removed', result['error'])
        self.assertEqual(self.store.count(), 1)

    def test_anonymous_reference_is_flagged_without_model_question(self):
        with patch('second_brain.router.chat', return_value={'daily_summary': ['Speaker 2 committed to lead the launch.']}):
            route({}, self.vault, 'other', 'Source', '2026-10-02', self.store)
        self.store.catalog()
        catalog = json.loads((self.review/'clarifications.json').read_text(encoding='utf-8'))
        self.assertEqual(len(catalog['items']), 2)
        self.assertIn('unidentified person', catalog['items'][1]['questions'][0])
