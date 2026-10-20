"""service.json survives reinstall: installer-managed keys update, everything else is kept."""

from __future__ import annotations
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class MergeTests(unittest.TestCase):
    def merge(self, existing, managed, defaults):
        from second_brain.install_config import merge_service_config
        return merge_service_config(existing, managed, defaults)

    def test_installer_managed_keys_always_win(self):
        merged = self.merge({"port": 7331, "vault_path": "C:/old"}, {"port": 17331, "vault_path": "C:/new"}, {})
        self.assertEqual((merged["port"], merged["vault_path"]), (17331, "C:/new"))

    def test_settings_the_installer_does_not_manage_are_kept(self):
        existing = {"meetings_enabled": True, "owner_name": "Eugene", "bridge_url": "https://sb.example",
                    "speaker_match_threshold": 0.45}
        merged = self.merge(existing, {"port": 7331}, {})
        for key, value in existing.items():
            self.assertEqual(merged[key], value, key)

    def test_defaults_fill_only_missing_keys(self):
        merged = self.merge({"poll_seconds": 5}, {}, {"poll_seconds": 2, "retry_seconds": 60})
        self.assertEqual((merged["poll_seconds"], merged["retry_seconds"]), (5, 60))

    def test_a_fresh_install_is_defaults_plus_managed(self):
        merged = self.merge(None, {"port": 7331}, {"poll_seconds": 2})
        self.assertEqual(merged, {"poll_seconds": 2, "port": 7331})

    def test_inputs_are_not_mutated(self):
        existing, managed, defaults = {"a": 1}, {"b": 2}, {"c": 3}
        self.merge(existing, managed, defaults)
        self.assertEqual((existing, managed, defaults), ({"a": 1}, {"b": 2}, {"c": 3}))


def service_paths(root: Path) -> dict:
    for name in ("data", "incoming", "vault", "status"):
        (root / name).mkdir(exist_ok=True)
    (root / "secret.hex").write_text("0" * 64)
    (root / "pipeline.json").write_text("{}")
    return {"data_dir": str(root / "data"), "incoming_dir": str(root / "incoming"),
            "secret_file": str(root / "secret.hex"), "pipeline_config": str(root / "pipeline.json"),
            "vault_path": str(root / "vault"), "status_file": str(root / "status" / "status.json"),
            "port": 7331, "ffmpeg_dir": "", "skip_vibe7": False}


def run_cli(*args):
    env = {"PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, "-m", "second_brain.install_config", *args],
                          capture_output=True, text=True, env=env)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "service.json"
        self.installer = self.root / "installer.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_installer(self, managed, defaults=None):
        self.installer.write_text(json.dumps({"managed": managed, "defaults": defaults or {}}))

    def test_reinstall_keeps_meetings_and_bridge_settings(self):
        managed = service_paths(self.root)
        self.target.write_text(json.dumps({**managed, "meetings_enabled": True, "owner_name": "Eugene",
                                           "poll_seconds": 5}), encoding="utf-8-sig")   # BOM: PowerShell 5.1
        self.write_installer({**managed, "port": 17331}, {"poll_seconds": 2, "vault_folder": "Omi/Conversations"})
        proc = run_cli("--target", str(self.target), "--installer", str(self.installer))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        saved = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual((saved["meetings_enabled"], saved["owner_name"]), (True, "Eugene"))
        self.assertEqual((saved["port"], saved["poll_seconds"], saved["vault_folder"]), (17331, 5, "Omi/Conversations"))

    def test_a_fresh_install_creates_the_file(self):
        self.write_installer(service_paths(self.root), {"poll_seconds": 2})
        proc = run_cli("--target", str(self.target), "--installer", str(self.installer))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(self.target.read_text())["poll_seconds"], 2)

    def test_an_invalid_result_is_refused_and_the_old_file_is_untouched(self):
        managed = service_paths(self.root)
        original = json.dumps({**managed, "no_such_setting": 1})
        self.target.write_text(original)
        self.write_installer(managed)
        proc = run_cli("--target", str(self.target), "--installer", str(self.installer))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Unknown service configuration keys: no_such_setting", proc.stderr)
        self.assertEqual(self.target.read_text(), original)
        self.assertEqual(sorted(p.name for p in self.root.glob("service.json*")), ["service.json"])

    def test_an_unreadable_existing_file_is_refused_rather_than_replaced(self):
        self.target.write_text("{not json")
        self.write_installer(service_paths(self.root))
        proc = run_cli("--target", str(self.target), "--installer", str(self.installer))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.target.read_text(), "{not json")


if __name__ == "__main__":
    unittest.main()
