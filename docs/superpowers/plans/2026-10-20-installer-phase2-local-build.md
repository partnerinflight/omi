# Installer Phase 2: Local Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One script, `installer\build-installer.ps1`, that the owner runs on the Windows box. It stages everything the installer needs into `dist\installer\payload\`: the service, the tray, a ready-to-run Python, both MOSS builds, NeMo-Speech, ffmpeg, the voice-matching and 7B runtimes, licences and a verified `release-manifest.json`. A `-Slim` mode runs in GitHub's Windows CI.

**Architecture:**
- **Pins.** Every external download (standalone CPython, ffmpeg) and every model file gets a pinned URL or revision and a SHA-256 in `config/engines.lock.json`. Python package versions for the two ML runtimes are pinned in `installer/requirements-*.txt`.
- **Build script.** PowerShell reuses the existing `windows/build.ps1`, `build-moss.ps1` and `build-nemo-speech.ps1`, and downloads through a cache that checks hashes.
- **Manifest.** `scripts/make-manifest.py` (pure Python, unit-tested on the Mac) records what the payload contains and which models each component needs. It also refuses an incomplete payload.
- **Not this phase.** Phase 2 does not compile `Setup.exe` (phase 3) and never runs on the owner's Mac.

**Tech Stack:** PowerShell 7, Python 3.12 stdlib, unittest, python-build-standalone, BtbN FFmpeg builds, pip, GitHub Actions `windows-latest`.

Spec: `docs/superpowers/specs/2026-10-20-windows-installer-design.md` §2 (amended 2026-10-20: local build) and "Implementation phases" item 2.

## Pins (looked up 2026-10-20)

- **Standalone CPython:** `cpython-3.12.15+20261013-x86_64-pc-windows-msvc-install_only.tar.gz`, 46,390,009 bytes.
  - URL: `https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.12.15%2B20261013-x86_64-pc-windows-msvc-install_only.tar.gz`
  - SHA-256: `bbc902d4d187ac4914915bf538196a064b49109a33a104965b5164ced5b80a1e`
- **ffmpeg (LGPL, shared):** `ffmpeg-n9.0.2-29-g6dfaba896f-win64-lgpl-shared-9.0.zip`, 77,233,322 bytes.
  - URL: `https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-20-13-08/ffmpeg-n9.0.2-29-g6dfaba896f-win64-lgpl-shared-9.0.zip`
  - SHA-256: `c39750c8dfe849e4389843e5f4af51e8062a6d6a3e8b74c1fd7e469594ef68e2`
  - BtbN prunes old autobuilds. Once the cache holds the file, the build never needs the URL again, and `-FfmpegZip <path>` accepts a local copy.
- **Model weight SHA-256** (Hugging Face LFS object IDs at the pinned revisions):
  - `moss_model`: `moss-transcribe-q5_k.gguf` = `6fe985fd9a9cd043728f7be49659d10de6901872dad73b49e3c30e1340582e79`. This matches a hash computed independently from the downloaded file on 2026-10-15.
  - `speaker_model`:
    - `classifier.ckpt` = `fd9e3634fe68bd0a427c95e354c0c677374f62b3f434e45b78599950d860d535`
    - `embedding_model.ckpt` = `0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2`
  - `vibe_model`:
    - `model-00001-of-00008.safetensors` = `3685d210ad49c49521e71a0cc5418ea94b20761c801dc8fc95938c4ec1ab27b3`
    - `model-00002-of-00008.safetensors` = `d0252f5e9bdf7e65bb0ae051c49148fd5d77e531057556eb955fc6dc72f4f0bc`
    - `model-00003-of-00008.safetensors` = `eeeb0c24e4a3746f16512c0d4aef84732bf8bb4dc5452782e4db356eb5fc7ce7`
    - `model-00004-of-00008.safetensors` = `02fec24aaf3e59cbc653b55d94665d6393543f2550364c2c8bc9e75e2a2ca3c0`
    - `model-00005-of-00008.safetensors` = `a78637bdc2b44f28601f7ee12503e28f5e9faedde35ff5bcdc2e7356c9a6458e`
    - `model-00006-of-00008.safetensors` = `5bc9dfef14c62989192ab39356da45a9ccc9f0ef82f7f7127e48c17b5bd5a2f8`
    - `model-00007-of-00008.safetensors` = `bb6da42b3547124eb67645fc6fb92523e93310a107872643f7df6dd75f85b4d7`
    - `model-00008-of-00008.safetensors` = `b1499fbe8bc0454eeafc1177db144679940f2456eae188299214c629d4d618b4`
  - `diar_model`: already pinned.
  - Small text files (`README.md`, `config.json`, …) have no LFS hash. They are fixed by the immutable revision commit.

## Payload layout (the contract phase 3 consumes)

```
dist\installer\payload\
  app\service\SecondBrain.Service.exe …     dotnet publish, self-contained
  app\tray\SecondBrain.Tray.exe …
  app\python\python.exe …                    standalone CPython + app wheels and dependencies
  engines\moss-cpu\moss-transcribe.exe …
  engines\moss-cuda\moss-transcribe.exe …    full build only (+ CUDA runtime DLLs)
  engines\nemo-speech\nemo-speech.exe …      full build only (+ ggml DLLs)
  engines\ffmpeg\bin\ffmpeg.exe, ffprobe.exe, *.dll
  runtimes\voice\python\python.exe …         full build only: torch CPU + speechbrain
  runtimes\vibe\python\python.exe …          full build only: CUDA torch + transformers + VibeVoice
  scripts\…                                  install-core.ps1 and helpers (copied from dist\windows)
  config\…                                   *.example.json, engines.lock.json
  licenses\…                                 third-party licences
  release-manifest.json
```

The components follow from the layout:
- **core** is always present.
- **gpu** is `engines\moss-cuda`.
- **voice** is `runtimes\voice`.
- **linking** is `engines\nemo-speech`.
- **refinement** is `runtimes\vibe`.

Models per component: core → `moss_model`, voice → `speaker_model`, linking → `diar_model`, refinement → `vibe_model`.

---

## File map

| File | Responsibility |
|---|---|
| `config/engines.lock.json` (modify) | `python_standalone`, `ffmpeg` pins; `sha256` maps for moss/speaker/vibe models |
| `installer/requirements-voice.txt`, `installer/requirements-vibe.txt` (create) | Pinned packages for the two ML runtimes |
| `scripts/make-manifest.py` (create) | Build `release-manifest.json` from lock + payload; refuse incomplete payloads |
| `tests/test_installer_manifest.py` (create) | Lock-pin rules; manifest contents; payload verification |
| `installer/build-installer.ps1` (create) | Stage the payload, full or `-Slim` |
| `.github/workflows/ci.yml` (modify) | Windows job runs `build-installer.ps1 -Slim` |
| `docs/install-windows.md`, `README.md` (modify) | How the owner builds the installer |

Run from the repo root.
- **Single module:** `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_installer_manifest -v`.
- **Full suite:** `python3 scripts/test.py`. Its trailing `swift test` step fails in a sandboxed shell; that is pre-existing. Both Python suites must print `OK`.
- **PowerShell:** cannot run on the Mac. `build-installer.ps1` is verified by the Windows CI job (Task 5) and by the owner's full run on the Windows box.

---

### Task 1: Pins

**Files:**
- Modify: `config/engines.lock.json`
- Create: `installer/requirements-voice.txt`, `installer/requirements-vibe.txt`
- Test: `tests/test_installer_manifest.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_installer_manifest.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_installer_manifest -v`
Expected: FAIL. The tests hit `KeyError: 'python_standalone'`, `KeyError: 'sha256'` and `FileNotFoundError` for the requirements files.

- [ ] **Step 3: Add the pins**

In `config/engines.lock.json`:
- Add `"sha256"` maps to `moss_model`, `speaker_model` and `vibe_model`, holding exactly the hashes listed under "Pins" above. `diar_model` already has one.
- Add two top-level entries. Keep the file valid JSON and keep the existing entries unchanged:

```json
  "python_standalone": {
    "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.12.15%2B20261013-x86_64-pc-windows-msvc-install_only.tar.gz",
    "sha256": "bbc902d4d187ac4914915bf538196a064b49109a33a104965b5164ced5b80a1e",
    "size": 46390009
  },
  "ffmpeg": {
    "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-20-13-08/ffmpeg-n9.0.2-29-g6dfaba896f-win64-lgpl-shared-9.0.zip",
    "sha256": "c39750c8dfe849e4389843e5f4af51e8062a6d6a3e8b74c1fd7e469594ef68e2",
    "size": 77233322
  }
```

`windows/download-model.py` (phase-0 setup) already verifies `sha256` maps when present, so these new maps also harden the existing source-checkout setup. Confirm by reading it.

Create `installer/requirements-voice.txt`:

```
# Voice matching runtime (speaker_audio / speaker_encoder.py): CPU PyTorch + SpeechBrain.
--index-url https://download.pytorch.org/whl/cpu
--extra-index-url https://pypi.org/simple
torch==2.8.0
torchaudio==2.8.0
speechbrain==1.0.3
huggingface-hub==0.34.6
```

Create `installer/requirements-vibe.txt`:

```
# VibeVoice 7B runtime: CUDA 12.6 PyTorch (also runs on CPU), plus offload support (accelerate).
# The VibeVoice package itself is installed from the pinned checkout in config/engines.lock.json.
--index-url https://download.pytorch.org/whl/cu126
--extra-index-url https://pypi.org/simple
torch==2.8.0
torchaudio==2.8.0
transformers==4.51.3
accelerate==1.6.0
huggingface-hub==0.34.6
soundfile==0.13.1
librosa==0.11.0
```

Cross-check these versions against today's `windows/setup-engines.ps1` and `windows/setup-speakers.ps1`, which install torch 2.8.0, speechbrain 1.0.3, transformers 4.51.3 and huggingface-hub 0.34.6. They must agree. Only accelerate, soundfile and librosa are newly pinned: setup-engines installs `soundfile` and `librosa` unpinned, and accelerate is new for offload.

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_installer_manifest -v`
Expected: 3 tests PASS. Also run `python3 -c "import json; json.load(open('config/engines.lock.json'))"` and `python3 scripts/test.py`; both Python suites must print `OK`.

- [ ] **Step 5: Commit**

```bash
git add config/engines.lock.json installer/requirements-voice.txt installer/requirements-vibe.txt tests/test_installer_manifest.py
git commit -m "feat(installer): pin standalone Python, ffmpeg, runtime packages and model hashes"
```

---

### Task 2: Manifest and payload verification

**Files:**
- Create: `scripts/make-manifest.py`
- Test: `tests/test_installer_manifest.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_installer_manifest.py` (above `if __name__`):

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_installer_manifest -v`
Expected: the 5 new tests ERROR with `FileNotFoundError` for `scripts/make-manifest.py`.

- [ ] **Step 3: Implement**

Create `scripts/make-manifest.py`:

```python
#!/usr/bin/env python3
"""Describe a staged installer payload (installer/build-installer.ps1) in release-manifest.json.

Components follow from what was built into the payload. Each component names the pinned models the
installer downloads for it (repository, revision, files, SHA-256 from config/engines.lock.json).
An incomplete payload is refused, so a half-built installer is never produced."""

from __future__ import annotations
import argparse
import datetime as dt
import fnmatch
import json
import sys
from pathlib import Path

CORE_FILES = ["app/service/SecondBrain.Service.exe", "app/tray/SecondBrain.Tray.exe", "app/python/python.exe",
              "engines/moss-cpu/moss-transcribe.exe", "engines/ffmpeg/bin/ffmpeg.exe",
              "engines/ffmpeg/bin/ffprobe.exe", "scripts/install-core.ps1", "config/engines.lock.json"]
# component -> (folder that marks it present, file that must exist when it is)
OPTIONAL = {
    "gpu": ("engines/moss-cuda", "engines/moss-cuda/moss-transcribe.exe"),
    "voice": ("runtimes/voice", "runtimes/voice/python/python.exe"),
    "linking": ("engines/nemo-speech", "engines/nemo-speech/nemo-speech.exe"),
    "refinement": ("runtimes/vibe", "runtimes/vibe/python/python.exe"),
}
MODELS = {"moss_model": "core", "speaker_model": "voice", "diar_model": "linking", "vibe_model": "refinement"}
CORE_FOLDERS = ["app", "engines/moss-cpu", "engines/ffmpeg", "scripts", "config", "licenses"]


def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def verify_payload(payload: Path) -> list[str]:
    problems = [f"missing core file: {name}" for name in CORE_FILES if not (payload / name).is_file()]
    for component, (folder, required) in OPTIONAL.items():
        if (payload / folder).exists() and not (payload / required).is_file():
            problems.append(f"incomplete {component} component: {required} missing")
    return problems


def present(payload: Path) -> dict:
    found = {"core": not any(p.startswith("missing core") for p in verify_payload(payload))}
    for component, (folder, required) in OPTIONAL.items():
        found[component] = (payload / required).is_file()
    return found


def make_manifest(lock: dict, payload: Path, version: str) -> dict:
    found = present(payload)
    sizes = {"core": sum(_size(payload / f) for f in CORE_FOLDERS)}
    for component, (folder, _) in OPTIONAL.items():
        sizes[component] = _size(payload / folder)
    models = []
    for key, component in MODELS.items():
        if not found[component]:
            continue
        pin = lock[key]
        hashes = pin.get("sha256", {})
        models.append({"key": key, "component": component, "repo": pin["repo"], "revision": pin["revision"],
                       "patterns": pin["patterns"],
                       "files": [{"path": name, "sha256": digest} for name, digest in sorted(hashes.items())
                                 if any(fnmatch.fnmatch(name, p) for p in pin["patterns"])]})
    return {"version": version,
            "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "components": {c: {"present": found[c], "size_bytes": sizes[c]} for c in ["core", *OPTIONAL]},
            "models": models}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    problems = verify_payload(args.payload)
    if problems:
        print("Payload is incomplete:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    lock = json.loads(args.lock.read_text(encoding="utf-8-sig"))
    manifest = make_manifest(lock, args.payload, args.version)
    args.out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({c: v["present"] for c, v in manifest["components"].items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify they pass**

Run: `PYTHONPATH=src:omi/firmware/scripts/omi-local python3 -m unittest tests.test_installer_manifest -v`
Expected: 8 tests PASS. Run `python3 scripts/test.py`; both Python suites must print `OK`.

- [ ] **Step 5: Commit**

```bash
git add scripts/make-manifest.py tests/test_installer_manifest.py
git commit -m "feat(installer): release manifest describes the payload and refuses incomplete builds"
```

---

### Task 3: The build script

**Files:**
- Create: `installer/build-installer.ps1`

PowerShell cannot run on the Mac. Write the script by reading the existing scripts it reuses: `windows/build.ps1`, `windows/build-moss.ps1`, `windows/build-nemo-speech.ps1`, `windows/setup-engines.ps1` (its VibeVoice install and import check) and `windows/setup-common.ps1` (`Invoke-Checked`, `Get-EngineLock`, `Get-PinnedSource`). It is verified by Task 5 (CI, `-Slim`) and by the owner's full run.

- [ ] **Step 1: Write the script**

Create `installer/build-installer.ps1`:

```powershell
param(
    [string]$Version = '0.0.0-dev',
    [string]$OutDir = '',
    # Downloads and engine checkouts persist here between builds (hash-checked on every use).
    [string]$CacheDir = (Join-Path $env:LOCALAPPDATA 'SecondBrainBuildCache'),
    [string]$FfmpegZip = '',
    # CI: no CUDA MOSS, no NeMo-Speech, no ML runtimes. The installer then offers only what exists.
    [switch]$Slim
)
# Stage everything the Second Brain installer ships into <OutDir>\payload. Run on the Windows box.
# Prerequisites: .NET 10 SDK, Git, CMake 3.26+, Visual Studio 2022 Build Tools (C++ workload);
# full builds also need Ninja (NeMo-Speech) and the CUDA Toolkit (CUDA_PATH).
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
. (Join-Path $repo 'windows/setup-common.ps1')
if (-not $OutDir) { $OutDir = Join-Path $repo 'dist\installer' }
$payload = Join-Path $OutDir 'payload'
$downloads = Join-Path $CacheDir 'downloads'
$engineRoot = Join-Path $CacheDir 'engines'
$lock = Get-EngineLock
foreach ($tool in @('dotnet', 'git', 'cmake')) { Require-Command $tool }
if (-not $Slim -and (-not $env:CUDA_PATH -or -not (Test-Path (Join-Path $env:CUDA_PATH 'bin/nvcc.exe')))) {
    throw 'Full builds need the NVIDIA CUDA Toolkit (CUDA_PATH). Use -Slim to build without the GPU engine and ML runtimes.'
}

function Get-Verified([string]$Url, [string]$Sha256, [string]$Name) {
    New-Item -ItemType Directory -Force $downloads | Out-Null
    $file = Join-Path $downloads $Name
    if (-not (Test-Path $file) -or (Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $Sha256) {
        Write-Host "==> downloading $Name"
        Invoke-WebRequest $Url -OutFile "$file.partial" -UseBasicParsing
        Move-Item "$file.partial" $file -Force
    }
    $actual = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $Sha256) { Remove-Item $file -Force; throw "$Name SHA-256 $actual does not match the lock" }
    return $file
}

function New-StandalonePython([string]$Destination) {
    $archive = Get-Verified $lock.python_standalone.url $lock.python_standalone.sha256 'python-standalone.tar.gz'
    $staging = Join-Path $env:TEMP ('sb-python-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force $staging | Out-Null
    try {
        Invoke-Checked tar @('-xzf', $archive, '-C', $staging)     # archive root is python\
        New-Item -ItemType Directory -Force (Split-Path $Destination -Parent) | Out-Null
        Move-Item (Join-Path $staging 'python') $Destination
    } finally { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }
    $python = Join-Path $Destination 'python.exe'
    Invoke-Checked $python @('-I', '-c', 'import sys; assert sys.version_info[:2] == (3, 12), sys.version')
    return $python
}

function Copy-Tree([string]$From, [string]$To) {
    New-Item -ItemType Directory -Force $To | Out-Null
    Copy-Item (Join-Path $From '*') $To -Recurse -Force
}

if (Test-Path $payload) { Remove-Item $payload -Recurse -Force }
New-Item -ItemType Directory -Force $payload | Out-Null

Write-Host '==> app: service, tray, Python packages'
$appPython = New-StandalonePython (Join-Path $payload 'app\python')
& (Join-Path $repo 'windows/build.ps1') -Python $appPython
$bundle = Join-Path $repo 'dist\windows'
Copy-Tree (Join-Path $bundle 'service') (Join-Path $payload 'app\service')
Copy-Tree (Join-Path $bundle 'tray') (Join-Path $payload 'app\tray')
$wheels = @(Get-ChildItem (Join-Path $bundle 'python\*.whl') | ForEach-Object FullName)
Invoke-Checked $appPython (@('-I', '-m', 'pip', 'install', '--no-warn-script-location') + $wheels)
Invoke-Checked $appPython @('-I', '-c', 'import second_brain.runtime, omi_local.server; print("app packages OK")')
New-Item -ItemType Directory -Force (Join-Path $payload 'scripts'), (Join-Path $payload 'config') | Out-Null
Get-ChildItem $bundle -Filter '*.ps1' | Copy-Item -Destination (Join-Path $payload 'scripts') -Force
Copy-Item (Join-Path $bundle 'download-model.py') (Join-Path $payload 'scripts') -Force
Copy-Tree (Join-Path $bundle 'config') (Join-Path $payload 'config')

Write-Host '==> ffmpeg'
if (-not $FfmpegZip) { $FfmpegZip = Get-Verified $lock.ffmpeg.url $lock.ffmpeg.sha256 'ffmpeg.zip' }
elseif ((Get-FileHash $FfmpegZip -Algorithm SHA256).Hash.ToLower() -ne $lock.ffmpeg.sha256) { throw '-FfmpegZip does not match the lock' }
$ffStage = Join-Path $env:TEMP ('sb-ffmpeg-' + [guid]::NewGuid().ToString('N'))
try {
    Expand-Archive $FfmpegZip $ffStage
    $inner = Get-ChildItem $ffStage -Directory | Select-Object -First 1   # ffmpeg-n9.0.2-…-win64-lgpl-shared-9.0\
    Copy-Tree (Join-Path $inner.FullName 'bin') (Join-Path $payload 'engines\ffmpeg\bin')
    New-Item -ItemType Directory -Force (Join-Path $payload 'licenses\ffmpeg') | Out-Null
    Copy-Item (Join-Path $inner.FullName 'LICENSE.txt') (Join-Path $payload 'licenses\ffmpeg') -Force
} finally { Remove-Item $ffStage -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host '==> MOSS (CPU)'
& (Join-Path $repo 'windows/build-moss.ps1') -EngineRoot $engineRoot
$mossSource = Join-Path $engineRoot ('sources/moss-' + $lock.moss.revision.Substring(0,12))
Copy-Tree (Join-Path $mossSource 'build/Release') (Join-Path $payload 'engines\moss-cpu')
New-Item -ItemType Directory -Force (Join-Path $payload 'licenses\moss') | Out-Null
Copy-Item (Join-Path $mossSource 'LICENSE') (Join-Path $payload 'licenses\moss') -Force

if (-not $Slim) {
    Write-Host '==> MOSS (CUDA)'
    & (Join-Path $repo 'windows/build-moss.ps1') -EngineRoot $engineRoot -Cuda
    Copy-Tree (Join-Path $mossSource 'build-cuda/Release') (Join-Path $payload 'engines\moss-cuda')

    Write-Host '==> NeMo-Speech (speaker linking)'
    & (Join-Path $repo 'windows/build-nemo-speech.ps1') -EngineRoot $engineRoot
    $nemoSource = Join-Path $engineRoot ('sources/nemo-speech-' + $lock.nemo_speech.revision.Substring(0,12))
    Copy-Tree (Join-Path $nemoSource 'build-cpu/bin') (Join-Path $payload 'engines\nemo-speech')
    New-Item -ItemType Directory -Force (Join-Path $payload 'licenses\nemo-speech') | Out-Null
    foreach ($name in @('LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md')) {
        Copy-Item (Join-Path $nemoSource $name) (Join-Path $payload 'licenses\nemo-speech') -Force
    }

    Write-Host '==> voice-matching runtime'
    $voicePython = New-StandalonePython (Join-Path $payload 'runtimes\voice\python')
    Invoke-Checked $voicePython @('-I', '-m', 'pip', 'install', '--no-warn-script-location', '-r', (Join-Path $PSScriptRoot 'requirements-voice.txt'))
    Invoke-Checked $voicePython @('-I', '-c', 'import torch, speechbrain; print("voice runtime OK", torch.__version__)')

    Write-Host '==> VibeVoice 7B runtime (CUDA PyTorch)'
    $vibeSource = Join-Path $engineRoot ('sources/VibeVoice-' + $lock.vibe.revision.Substring(0,12))
    Get-PinnedSource $lock.vibe.url $lock.vibe.revision $vibeSource
    $vibePython = New-StandalonePython (Join-Path $payload 'runtimes\vibe\python')
    Invoke-Checked $vibePython @('-I', '-m', 'pip', 'install', '--no-warn-script-location', '-r', (Join-Path $PSScriptRoot 'requirements-vibe.txt'), $vibeSource)
    Invoke-Checked $vibePython @('-I', '-c', 'from vibevoice.modular.modeling_vibevoice_asr import VibeVoiceASRForConditionalGeneration as M; import torch, accelerate; assert callable(M.streaming_generate); print("VibeVoice runtime OK", torch.__version__, "cuda build:", torch.version.cuda)')
}

Write-Host '==> licences and manifest'
New-Item -ItemType Directory -Force (Join-Path $payload 'licenses') | Out-Null
Copy-Item (Join-Path $repo 'LICENSE') (Join-Path $payload 'licenses\SecondBrain-LICENSE') -Force
Copy-Item (Join-Path $payload 'app\python\LICENSE.txt') (Join-Path $payload 'licenses\python-LICENSE.txt') -Force -ErrorAction SilentlyContinue
Set-Content (Join-Path $payload 'licenses\README.txt') "Third-party licences for software shipped in this installer. Models are downloaded at install time under their own licences (see config\engines.lock.json)." -Encoding UTF8
Invoke-Checked $appPython @((Join-Path $repo 'scripts/make-manifest.py'), '--lock', (Join-Path $repo 'config/engines.lock.json'),
    '--payload', $payload, '--version', $Version, '--out', (Join-Path $payload 'release-manifest.json'))
Write-Host "Payload staged in $payload$(if ($Slim) { ' (slim)' })."
```

- [ ] **Step 2: Check it by reading**

Check each of the following against the reused scripts, and report every adjustment with its evidence:
1. **Build output paths:** `build-moss.ps1` writes `build/Release/moss-transcribe.exe` (CPU) and `build-cuda/Release/…` (CUDA), and copies the CUDA DLLs next to the CUDA CLI. `build-nemo-speech.ps1` writes `build-cpu/bin`.
2. **`build.ps1 -Python`:** it accepts `-Python` and puts wheels in `dist/windows/python`. It also runs `dotnet run` for tests and publishes into `dist\windows`; check that `dist\windows` contains `download-model.py` and the `.ps1` helpers after `build.ps1`.
3. **Python archive:** python-build-standalone `install_only` archives unpack to a top-level `python\` folder containing `python.exe`. Check this against the project's documentation (an exact quote is enough) and adjust if it is wrong.
4. **ffmpeg archive:** BtbN zips contain one top-level folder with `bin\` and `LICENSE.txt`.
5. **pip:** the standalone Python includes pip (`python -m pip`).
6. **`$Slim`:** nothing under `if (-not $Slim)` is needed for core.
7. **PowerShell compatibility:** use only syntax valid in PowerShell 7 and 5.1. For example, the `$(if (...) {...})` in the last string is fine in both.

- [ ] **Step 3: Commit**

```bash
git add installer/build-installer.ps1
git commit -m "feat(installer): build-installer.ps1 stages the installer payload on the Windows box"
```

---

### Task 4: Documentation

**Files:**
- Modify: `docs/install-windows.md`, `README.md`

- [ ] **Step 1: Write it**

Add a section "Build the installer (Windows box)" to `docs/install-windows.md` covering:
- **Prerequisites:** .NET 10 SDK, Git, CMake 3.26+, Visual Studio 2022 Build Tools with the C++ workload, Ninja, the CUDA Toolkit. `-Slim` needs neither Ninja nor CUDA.
- **Command:** `.\installer\build-installer.ps1 -Version 1.0.0`.
- **Output:** `dist\installer\payload\` and its `release-manifest.json`.
- **Cache:** `%LOCALAPPDATA%\SecondBrainBuildCache`, hash-checked on every build. It also makes the build keep working if a pinned ffmpeg build disappears upstream (`-FfmpegZip`).
- **Expected time and size:**
  - The first full build compiles MOSS twice and NeMo-Speech, and downloads about 4 GB (mostly CUDA PyTorch).
  - The payload is about 6 GB uncompressed.
  - Turning the payload into `Setup.exe` is phase 3; this phase produces the payload only.

Add a one-line pointer to that section in `README.md`'s Windows installation part. Match the surrounding density.

- [ ] **Step 2: Commit**

```bash
git add docs/install-windows.md README.md
git commit -m "docs(installer): build the installer payload on the Windows box"
```

---

### Task 5: Prove the slim build in CI

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Add the step**

In the `windows` job, directly after the step `Build pinned MOSS runtime on Windows`, add:

```yaml
      - name: Stage the installer payload (slim)
        shell: pwsh
        run: ./installer/build-installer.ps1 -Slim -Version ci -CacheDir "$env:RUNNER_TEMP/sb-build-cache"
      - name: Payload runs outside the checkout
        shell: pwsh
        run: |
          $py = "dist/installer/payload/app/python/python.exe"
          & $py -I -m second_brain.cli --help
          if ($LASTEXITCODE -ne 0) { throw 'payload Python cannot start the service CLI' }
          & "dist/installer/payload/engines/ffmpeg/bin/ffmpeg.exe" -hide_banner -decoders | Select-String ' opus '
          if (-not $?) { throw 'payload ffmpeg has no Opus decoder' }
          Get-Content dist/installer/payload/release-manifest.json
```

Read `.github/workflows/ci.yml` first, and match its indentation and the shell other steps use. The earlier MOSS step uses `$env:RUNNER_TEMP/second-brain-engines`; the slim build uses its own cache, so it rebuilds MOSS. That costs CI time but proves the cold path. If the job's wall time exceeds 60 minutes, pass `-CacheDir` so `engines` resolves to the same folder as the MOSS step, and say so.

- [ ] **Step 2: Push and watch**

Push the branch, then watch the Windows job: `gh run list --branch feature/wifi-local-upload --limit 1`, then `gh run watch <id>`. It must pass both new steps. Report the run URL, the manifest it printed, and the step timings. If it fails, fix the cause in `build-installer.ps1` (not the CI step) and push again.

- [ ] **Step 3: Commit (before pushing)**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: stage the slim installer payload and check it runs"
```

---

### Task 6: Handoff

- [ ] Run `python3 scripts/test.py` twice; both Python suites must print `OK`.
- [ ] State plainly what was verified and what was not:
  - **Verified:** the slim payload on GitHub's Windows runner (CI).
  - **Not run:** the full build (CUDA MOSS, NeMo-Speech, the ML runtimes).
- [ ] Give the owner the exact command for the full build on the Windows box, and say which output to send back if it fails: the last 60 lines of the console.
