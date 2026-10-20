# Windows installer — design

Date: 2026-10-20. Status: approved design; amended 2026-10-20 (local build, see Decisions). Phase 1 implemented.

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
- Delivery (amended 2026-10-20): the owner builds `SecondBrain-Setup-<version>.exe` on the Windows
  box with one script, `installer\build-installer.ps1`, into `dist\installer\`. Nothing is
  published; no GitHub Release is used. The voice-matching and 7B runtimes are inside Setup.exe
  (about 3.5 GB). GitHub's Windows CI runner runs the same script in a slim mode (no CUDA, no
  large runtimes) so breakage is caught; the build never runs on the owner's Mac.
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

`installer\build-installer.ps1`, run by the owner on the Windows box (prerequisites: Visual Studio
2022 Build Tools with the C++ workload, CMake 3.26+, Ninja, Git, .NET 10 SDK, CUDA Toolkit, Inno
Setup 6). It stages everything under `dist\installer\payload\` and compiles the wizard:

| Piece | Built from | Staged as |
|---|---|---|
| Service, tray | `dotnet publish --self-contained` (as `windows/build.ps1`) | `app\service`, `app\tray` |
| App Python | python-build-standalone CPython 3.12 (pinned, relocatable) with app wheels installed into it; no venv | `app\python` |
| MOSS CPU, MOSS CUDA | `windows/build-moss.ps1` twice; CUDA runtime DLLs next to the CUDA CLI | `engines\moss-cpu`, `engines\moss-cuda` |
| NeMo-Speech.cpp CPU | `windows/build-nemo-speech.ps1` | `engines\nemo-speech` |
| ffmpeg | pinned LGPL build, SHA-256 checked, licence included | `engines\ffmpeg` |
| Voice-matching runtime | standalone CPython + torch 2.8.0 CPU + speechbrain 1.0.3 | `runtimes\voice` |
| 7B runtime | standalone CPython + CUDA PyTorch + transformers 4.51.3 + VibeVoice (pinned) + accelerate; CUDA PyTorch also runs on CPU, so one runtime covers both | `runtimes\vibe` |
| Models | not built: Hugging Face, pinned revisions in `config/engines.lock.json` plus SHA-256 | downloaded at install |
| `release-manifest.json` | `scripts/make-manifest.py`: version, component sizes, model URLs/revisions/SHA-256 | `payload\` |

`-Slim` (used by CI): skips the CUDA MOSS build and both ML runtimes; the wizard then offers only
the components present in the payload.

Repository changes:
- New: `installer/build-installer.ps1`; `installer/SecondBrain.iss` (wizard); `installer/install-core.ps1`
  (done in phase 1); `installer/benchmark-7b.ps1`; `scripts/make-manifest.py`; a CI step running
  `build-installer.ps1 -Slim`.
- `windows/install.ps1` stays a thin wrapper over `install-core.ps1` (done in phase 1).
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
- CI, every push: `build-installer.ps1 -Slim`, then silent install (`/VERYSILENT` + a test answer
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
   (fixes both bugs for the existing path first). Done 2026-10-20 (Windows CI green).
2. Local build: `build-installer.ps1` staging the payload (engines, standalone Pythons, runtimes,
   manifest) with `-Slim` for CI.
3. Wizard: `SecondBrain.iss`, downloads, GPU detection, upgrade/rollback/uninstall, CI silent tests.
4. 7B CUDA offload: runner change, CUDA runtime, benchmark.
