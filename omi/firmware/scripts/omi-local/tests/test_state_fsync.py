"""Exercise durable-state writes with Windows handle permissions and I/O errors."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from omi_local.state import StateStore, atomic_json

class StateFsyncTests(unittest.TestCase):
    def test_flush_uses_writable_handle_without_truncating_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StateStore(Path(tmp));store.put(store.get('test'))
            before=store.path.read_bytes()
            real_open=os.open;real_fsync=os.fsync;flags_by_fd={}
            def open_file(path, flags, *args, **kwargs):
                fd=real_open(path,flags,*args,**kwargs);flags_by_fd[fd]=flags;return fd
            def windows_fsync(fd):
                if not flags_by_fd[fd] & (os.O_WRONLY|os.O_RDWR):
                    raise OSError('Windows flush requires write access')
                real_fsync(fd)
            with patch('os.open',side_effect=open_file),patch('os.fsync',side_effect=windows_fsync):
                store.fsync()
            self.assertEqual(store.path.read_bytes(),before)

    def test_transient_windows_replace_errors_are_retried(self):
        # WinError 1450 (seen live on the receiver) and sharing violations come from
        # scanners/filter drivers and clear quickly; they must not abort an upload.
        class Transient(OSError):
            winerror = 1450
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'meta.json'
            real_replace = os.replace; calls = []
            def flaky(src, dst):
                calls.append(dst)
                if len(calls) < 3: raise Transient('Insufficient system resources')
                real_replace(src, dst)
            with patch('os.replace', side_effect=flaky), patch('time.sleep') as sleep:
                atomic_json(path, {'ok': True})
            self.assertEqual(len(calls), 3)
            self.assertEqual(json.loads(path.read_text()), {'ok': True})
            self.assertEqual(list(Path(tmp).glob('*.tmp')), [])
            self.assertLess(sum(c.args[0] for c in sleep.call_args_list), 5, 'far below the device ACK timeout')

    def test_persistent_or_other_replace_errors_still_fail(self):
        class Transient(OSError):
            winerror = 32
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'meta.json'
            for error, attempts in [(Transient('locked'), 'several'), (OSError('disk failure'), 'one')]:
                with self.subTest(error=str(error)):
                    with patch('os.replace', side_effect=error) as replace, patch('time.sleep'):
                        with self.assertRaises(OSError): atomic_json(path, {'ok': True})
                    self.assertEqual(replace.call_count > 1, attempts == 'several')
                    self.assertEqual(list(Path(tmp).glob('*.tmp')), [], 'temporary file cleaned up')

    def test_open_and_flush_failures_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StateStore(Path(tmp));store.put(store.get('test'))
            for operation in ['os.open','os.fsync']:
                with self.subTest(operation=operation),patch(operation,side_effect=OSError('disk failure')):
                    with self.assertRaises(OSError): store.fsync()
