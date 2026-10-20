import importlib.util
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import subprocess
import unittest
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "second-brain-morning-gate.py"
spec = importlib.util.spec_from_file_location("morning_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class GateTests(unittest.TestCase):
    def test_housekeeping_runs_hourly_but_only_six_am_wakes_agent(self):
        calls = []
        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(stdout='{"success":true,"changed":false}')
        for month in (1, 7):
            for hour in (0, 5, 6, 7, 23):
                now = datetime(2030, month, 5, hour, tzinfo=ZoneInfo("America/Los_Angeles"))
                result = gate.run_gate(now, runner)
                self.assertEqual(result["wakeAgent"], hour == 6)
                if hour == 6:
                    self.assertEqual(result["context"]["scheduled_time"], "06:00")
        self.assertEqual(len(calls), 10)
        self.assertTrue(all(call[1]["check"] for call in calls))

    def test_housekeeping_errors_do_not_claim_a_successful_gate(self):
        def failure(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "scrub")
        with self.assertRaises(subprocess.CalledProcessError):
            gate.run_gate(runner=failure)
        with self.assertRaises(RuntimeError):
            gate.run_gate(runner=lambda *a, **kw: SimpleNamespace(stdout='{"success":false}'))


if __name__ == "__main__":
    unittest.main()
