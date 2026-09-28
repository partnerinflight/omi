"""Exercise durable-state writes with Windows handle permissions and I/O errors."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from omi_local.state import StateStore

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

    def test_open_and_flush_failures_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StateStore(Path(tmp));store.put(store.get('test'))
            for operation in ['os.open','os.fsync']:
                with self.subTest(operation=operation),patch(operation,side_effect=OSError('disk failure')):
                    with self.assertRaises(OSError): store.fsync()
