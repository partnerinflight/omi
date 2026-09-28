"""Run the host suite through the shared manifest's script entry point."""

import unittest
from pathlib import Path


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
