import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from second_brain.io import atomic_write


class AtomicWriteTests(unittest.TestCase):
    def test_writes_and_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a" / "note.md"
            atomic_write(path, b"one")
            atomic_write(path, b"two")
            self.assertEqual(path.read_bytes(), b"two")
            self.assertEqual([p.name for p in path.parent.iterdir()], ["note.md"])  # no temp left behind

    def test_folder_without_create_permission_fails_at_once_on_windows(self):
        # tempfile.mkstemp treats PermissionError on Windows as a name clash and retries up to
        # os.TMP_MAX (2**31 - 1) times, so a vault folder the service account may not write hung
        # the bridge silently. A denied create must raise on the first attempt.
        calls = []
        real_open = os.open

        def denied(path, flags, mode=0o777, *a, **kw):
            if str(path).endswith(".tmp"):
                calls.append(path)
                if len(calls) > 50:
                    raise AssertionError("retrying a denied create")
                raise PermissionError(13, "Access is denied", str(path))
            return real_open(path, flags, mode, *a, **kw)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Tasks.md"  # before faking os.name: pathlib checks it on creation
            with patch.object(os, "name", "nt"), patch.object(os, "open", denied), self.assertRaises(PermissionError):
                atomic_write(target, b"x")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
