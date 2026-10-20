"""The installer payload is pinned, complete, and described by release-manifest.json."""

from __future__ import annotations
import fnmatch
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "config/engines.lock.json").read_text(encoding="utf-8"))
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class PinTests(unittest.TestCase):
    def test_downloads_are_pinned_by_url_size_and_hash(self):
        for key in ("python_standalone", "ffmpeg"):
            pin = LOCK[key]
            self.assertTrue(pin["url"].startswith("https://github.com/"), key)
            self.assertRegex(pin["sha256"], HEX64, key)
            self.assertGreater(pin["size"], 1_000_000, key)
        self.assertIn("x86_64-pc-windows-msvc-install_only", LOCK["python_standalone"]["url"])
        self.assertIn("lgpl", LOCK["ffmpeg"]["url"])          # GPL builds cannot be redistributed freely

    def test_every_model_weight_has_a_hash(self):
        weights = {"moss_model": ["moss-transcribe-q5_k.gguf"],
                   "speaker_model": ["classifier.ckpt", "embedding_model.ckpt"],
                   "vibe_model": [f"model-0000{i}-of-00008.safetensors" for i in range(1, 9)],
                   "diar_model": ["Nemotron-3-Diarization.q8_0.gguf"]}
        for key, files in weights.items():
            hashes = LOCK[key]["sha256"]
            for name in files:
                self.assertRegex(hashes[name], HEX64, f"{key}/{name}")
                self.assertTrue(any(fnmatch.fnmatch(name, p) for p in LOCK[key]["patterns"]), f"{key}/{name}")

    def test_runtime_requirements_are_exact_pins(self):
        for name in ("requirements-voice.txt", "requirements-vibe.txt"):
            lines = [l.strip() for l in (ROOT / "installer" / name).read_text().splitlines()
                     if l.strip() and not l.startswith("#")]
            packages = [l for l in lines if not l.startswith("--")]
            self.assertTrue(packages, name)
            for line in packages:
                self.assertRegex(line, r"^[A-Za-z0-9_.\-\[\]]+==[0-9][0-9A-Za-z.+]*$", f"{name}: {line}")
        vibe = (ROOT / "installer/requirements-vibe.txt").read_text()
        self.assertIn("download.pytorch.org/whl/cu126", vibe)    # GTX 1660 Super (sm_75) is supported
        self.assertIn("torch==2.8.0", vibe)
        self.assertIn("download.pytorch.org/whl/cpu", (ROOT / "installer/requirements-voice.txt").read_text())


def load_make_manifest():
    spec = importlib.util.spec_from_file_location("make_manifest", ROOT / "scripts/make-manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_payload(root: Path, full: bool) -> Path:
    files = ["app/service/SecondBrain.Service.exe", "app/tray/SecondBrain.Tray.exe", "app/python/python.exe",
             "engines/moss-cpu/moss-transcribe.exe", "engines/ffmpeg/bin/ffmpeg.exe",
             "engines/ffmpeg/bin/ffprobe.exe", "scripts/install-core.ps1", "config/engines.lock.json",
             "licenses/README.txt"]
    if full:
        files += ["engines/moss-cuda/moss-transcribe.exe", "engines/nemo-speech/nemo-speech.exe",
                  "runtimes/voice/python/python.exe", "runtimes/vibe/python/python.exe"]
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
    return root


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "payload"
        self.mm = load_make_manifest()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_full_payload_lists_every_component_and_its_models(self):
        manifest = self.mm.make_manifest(LOCK, fake_payload(self.root, full=True), "1.2.3")
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertEqual({k for k, v in manifest["components"].items() if v["present"]},
                         {"core", "gpu", "voice", "linking", "refinement"})
        models = {m["key"]: m for m in manifest["models"]}
        self.assertEqual(models["moss_model"]["component"], "core")
        self.assertEqual(models["vibe_model"]["component"], "refinement")
        weights = {f["path"]: f["sha256"] for f in models["vibe_model"]["files"]}
        self.assertEqual(len(weights), 8)
        self.assertEqual(models["moss_model"]["revision"], LOCK["moss_model"]["revision"])
        self.assertGreater(manifest["components"]["core"]["size_bytes"], 0)

    def test_a_slim_payload_marks_the_large_components_absent(self):
        manifest = self.mm.make_manifest(LOCK, fake_payload(self.root, full=False), "ci")
        present = {k for k, v in manifest["components"].items() if v["present"]}
        self.assertEqual(present, {"core"})
        self.assertEqual({m["key"] for m in manifest["models"]}, {"moss_model"})   # only needed models

    def test_a_missing_core_file_is_refused(self):
        fake_payload(self.root, full=False)
        (self.root / "engines/ffmpeg/bin/ffprobe.exe").unlink()
        problems = self.mm.verify_payload(self.root)
        self.assertEqual(problems, ["missing core file: engines/ffmpeg/bin/ffprobe.exe"])

    def test_a_half_built_optional_component_is_refused(self):
        fake_payload(self.root, full=True)
        (self.root / "runtimes/vibe/python/python.exe").unlink()
        (self.root / "runtimes/vibe/python/readme.txt").write_text("partial")
        self.assertEqual(self.mm.verify_payload(self.root),
                         ["incomplete refinement component: runtimes/vibe/python/python.exe missing"])

    def test_cli_writes_the_manifest_and_fails_on_problems(self):
        fake_payload(self.root, full=False)
        out = self.root / "release-manifest.json"
        cmd = [sys.executable, str(ROOT / "scripts/make-manifest.py"), "--lock", str(ROOT / "config/engines.lock.json"),
               "--payload", str(self.root), "--version", "9.9.9", "--out", str(out)]
        ok = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(out.read_text())["version"], "9.9.9")
        (self.root / "app/python/python.exe").unlink()
        out.unlink()
        bad = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("missing core file: app/python/python.exe", bad.stderr)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
