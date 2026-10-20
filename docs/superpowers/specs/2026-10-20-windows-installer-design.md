# Windows installer — design

Date: 2026-10-20. Status: approved design, not yet implemented.

## Problem

A fresh Windows install needs six developer tools installed by hand (Python 3.12, .NET 10
SDK, Git, CMake, ffmpeg, Visual Studio Build Tools), compiles MOSS and NeMo-Speech.cpp from
source, and runs several PowerShell scripts with long argument lists. Upgrades are manual
(uninstall, then reinstall with the same arguments). Two upgrade bugs exist today:

1. `windows/install.ps1` regenerates `service.json` from a fixed list, so `meetings_enabled`,
   `meetings_vault_folder` and `owner_name` are lost on every reinstall.
2. `install.ps1` grants the service write access to `Omi\Conversations` and the router folders
   but not to `meetings_vault_folder` (`Omi\Meetings`), so meeting notes cannot be published.

## Decisions (owner, 2026-10-17 to 2026-10-20)

- Audience: the owner's own PCs only. Unsigned; one SmartScreen warning is acceptable.
- Delivery: one `SecondBrain-Setup-<version>.exe` built by CI and attached to a GitHub Release
  of the public repository. Publishing a release is an outward action: the first one is made
  only with explicit owner confirmation.
- Models download during install (pinned revision + SHA-256); runtime stays offline.
- GPU: MOSS ships both CPU and CUDA builds; the installer picks CUDA when an NVIDIA GPU is present.
- Components, all on by default: voice matching, speaker linking, VibeVoice 7B refinement.
- 7B refinement uses a CUDA PyTorch runtime with automatic CPU offload on small GPUs (the owner's
  GTX 1660 Super has 6 GB; the 7B needs ~14 GB at fp16). An install-time benchmark keeps
  whichever of CUDA-offload or CPU is faster.
- Technology: Inno Setup wizard (not MSI, not a bare bootstrap script).

## 1. User experience

Wizard pages:
1. Welcome — detects an existing installation and offers "Upgrade (keeps all data)".
2. Components — voice matching, speaker linking, 7B refinement (ticked; sizes shown).
3. Folders — Obsidian vault; recordings folder (default `C:\ProgramData\SecondBrain\incoming`);
   install folder (default `C:\Program Files\SecondBrain`).
4. Pairing key — import an existing key file, or generate a new one (with re-pairing
   instructions for the Omi and the Mac app). Upgrades reuse the installed key and skip the page.
5. Meetings — enable Mac meeting capture; owner name for the mic channel.
6. Install — copy files; download and verify selected runtimes/models with progress; detect the
   GPU; for 7B, run the benchmark and show both timings; install/start the service; run
   preflight; register the tray for the installing user.
7. Finish — the receiver address for the Mac app; a "Run diagnostics" shortcut.

Upgrade: run a newer `Setup.exe`. It stops the service, replaces application files, keeps
`ProgramData\SecondBrain` (config, key, data, speaker database, recordings) and the engine
root, re-verifies existing models by hash and downloads only new or changed ones, merges new
config keys, restarts the service.

Uninstall (Windows Settings): removes the application, service and tray registration; keeps data
and models unless the owner ticks "Also delete my data and models".

## 2. Build

`.github/workflows/release.yml`, triggered by a `v*` tag, on `windows-latest`:

| Piece | Built from | Shipped |
|---|---|---|
| Service, tray | `dotnet publish --self-contained` (as `windows/build.ps1`) | in Setup.exe |
| App Python | python-build-standalone CPython 3.12 (relocatable) with app wheels installed into it; no venv | in Setup.exe |
| MOSS CPU, MOSS CUDA | `windows/build-moss.ps1` twice; CUDA Toolkit installed on the runner; CUDA runtime DLLs next to the CUDA CLI | in Setup.exe |
| NeMo-Speech.cpp CPU | `windows/build-nemo-speech.ps1` | in Setup.exe |
| ffmpeg | pinned LGPL build, SHA-256 checked, licence included | in Setup.exe |
| Voice-matching runtime | standalone CPython + torch 2.8.0 CPU + speechbrain 1.0.3 | release asset `.zip` (~250 MB) |
| 7B runtime, CUDA | standalone CPython + CUDA PyTorch + transformers 4.51.3 + VibeVoice (pinned) + accelerate | release asset `.zip` (~2.5 GB) |
| 7B runtime, CPU | as today's CPU environment | release asset `.zip` (~350 MB) |
| Models | not built: Hugging Face, pinned revisions in `config/engines.lock.json` plus SHA-256 | downloaded at install |
| `release-manifest.json` | `scripts/make-manifest.py`: every download's URL, size, SHA-256 | in Setup.exe |

GitHub release assets are limited to 2 GB per file. If the CUDA 7B runtime exceeds that, split
it into numbered parts listed in the manifest. Setup.exe is about 700 MB.

Repository changes:
- New: `installer/SecondBrain.iss` (wizard); `installer/install-core.ps1` (all install/upgrade
  steps the wizard runs, non-interactive, parameterised); `installer/benchmark-7b.ps1`;
  `scripts/make-manifest.py`; `.github/workflows/release.yml`.
- `windows/install.ps1` becomes a thin wrapper over `install-core.ps1`, so the source-checkout
  path (`windows/setup.ps1`) and the installer share one implementation.
- Fixed in the shared core: config merge (existing values win; new keys get defaults; covers
  `service.json` and `pipeline.json`), and write grants for `meetings_vault_folder`.
- `src/second_brain/adaptive/runners/vibe_batch_runner.py`: new `vibe_7b_device` value
  `cuda-offload` → `device_map="auto"` with `max_memory` derived from free GPU memory, fp16
  (Turing has no bf16). `cuda` and `cpu` keep today's behaviour.

## 3. Errors, upgrades, testing

- Downloads resume and retry; a hash mismatch is never used. After retries the wizard names the
  failure and offers Retry / Skip (component disabled in config) / Cancel.
- Upgrade rollback: if a step fails after the old service stopped, the previous application
  folder is restored and the service restarted, so Omi uploads continue.
- Preflight (`second_brain.cli check`) runs before the service starts; failures are shown in
  plain words; the full log is kept at `C:\ProgramData\SecondBrain\install.log`.
- The pairing key, data, speaker database, recordings and models are never deleted by an
  upgrade. A different key than the installed one is refused, as today.
- Benchmark: a pinned 30 s sample through CUDA-offload and CPU; the faster becomes
  `vibe_7b_device`; a CUDA failure (out of memory, old driver) keeps CPU with the reason shown.

Testing:
- CI, every release build: build Setup.exe, then silent install (`/VERYSILENT` + a test answer
  file) → upgrade over it → uninstall keeping data → reinstall, on the Windows runner. Assert the
  service runs, a synthetic recording produces a note (reuse `windows/smoke-test.ps1`), and
  settings (including `meetings_enabled`) survive upgrade. Components point at small fixture
  models and runtimes via a test manifest; CI never downloads the real 20 GB.
- Unit tests: config merge, manifest generation, hash verification, meetings folder grant.
- Manual (owner's box): first real install, GPU detection, 7B benchmark result, real model hashes.

## Out of scope

Code signing, auto-update, macOS installer, multi-user installs, a quantised 7B, changing the
default service account model.

## Implementation phases (each its own plan)

1. Shared install core: `install-core.ps1`, config merge, meetings grant, `install.ps1` wrapper
   (fixes both bugs for the existing path first).
2. Release build: workflow, prebuilt engines and runtimes, standalone Python, manifest.
3. Wizard: `SecondBrain.iss`, downloads, GPU detection, upgrade/rollback/uninstall, CI silent tests.
4. 7B CUDA offload: runner change, CUDA runtime, benchmark.
