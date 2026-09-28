"""Process-crash checkpoints and failures must not advance the durable cursor."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from omi_local.cli import SessionWriter
from omi_local.state import StateStore, DeviceState
from omi_local.oggopus import iter_pages
from tests.fake_device import make_record

class CheckpointTests(unittest.TestCase):
    def test_failed_checkpoint_truncates_uncommitted_audio_on_resume(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); store = StateStore(root); state = store.get('test')
            writer = SessionWriter(root, 'test', state, store)
            writer.add(0, make_record(0, 1700000000)); writer.fsync()
            committed = StateStore(root).get('test'); size = committed.open_file.bytes_written
            with patch.object(store, 'put', side_effect=OSError('disk full')):
                with self.assertRaises(OSError): writer.add(1, make_record(1, 1700000000))
            writer.finish(final=False)
            self.assertEqual(StateStore(root).get('test').downloaded_through, 1)
            reopened = StateStore(root); resumed = SessionWriter(root, 'test', reopened.get('test'), reopened)
            self.assertEqual(Path(committed.open_file.path).stat().st_size, size)
            resumed.add(1, make_record(1, 1700000000)); resumed.finish(final=True)
            pages = list(iter_pages(Path(committed.open_file.path).read_bytes()))
            self.assertTrue(all(p.crc_ok for p in pages))
            self.assertEqual(sum(len(p.packets) for p in pages) - 2, 10)

    def test_audio_fsync_failure_does_not_save_advanced_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); store = StateStore(root); writer = SessionWriter(root, 'test', store.get('test'), store)
            writer.add(0, make_record(0, 1700000000))
            import os
            real = os.fsync
            def fail_audio(fd):
                if fd == writer._fp.fileno(): raise OSError('audio disk error')
                return real(fd)
            with patch('os.fsync', side_effect=fail_audio):
                with self.assertRaises(OSError): writer.add(1, make_record(1, 1700000000))
            writer.finish(final=False)
            self.assertEqual(StateStore(root).get('test').downloaded_through, 1)

    def test_device_state_updates_preserve_other_devices(self):
        with tempfile.TemporaryDirectory() as d:
            a = StateStore(Path(d)); b = StateStore(Path(d))
            a.put(DeviceState('a', downloaded_through=10)); b.put(DeviceState('b', downloaded_through=20))
            final = StateStore(Path(d))
            self.assertEqual(final.get('a').downloaded_through, 10)
            self.assertEqual(final.get('b').downloaded_through, 20)
