# Install from one checkout

All Second Brain application code is in this repository. External toolchains,
third-party engines and model weights are dependencies, not files copied from
an old personal project. Setup downloads engines/models into `EngineRoot` outside
Git; inference uses local files. Internet is needed during initial setup.

## Prerequisites (Windows x64)

Install machine-wide Python 3.12 (recommended for model wheel compatibility),
.NET 10 SDK, Git, current CMake, ffmpeg/ffprobe, and Visual Studio 2022 or 2026 Build Tools with
**Desktop development with C++**. Reopen PowerShell after installing them.
The C++ toolchain/CMake are unnecessary if supplying a working `-MossBinaryDir`.
Use local disk paths available before login; do not use Windows Store Python or
mapped network drives. A configured Hermes server is optional and external.

## Fresh service installation

Stop the old standalone receiver. Run in Administrator PowerShell from the repo:

```powershell
.\windows\setup.ps1 `
  -Python 'C:\Program Files\Python312\python.exe' `
  -SecretFile 'C:\Users\Eugene\.omi-local\upload-secret.hex' `
  -FfmpegDir 'C:\ffmpeg\bin' `
  -IncomingDir 'D:\SecondBrain-Audio\incoming' `
  -Vault 'C:\Users\Eugene\SecondBrain' `
  -ReviewUser "$env:COMPUTERNAME\Eugene"
```

Replace paths/account with your actual values. Reuse the existing receiver's key
and incoming directory. Setup preserves an existing pipeline config, backs it up
before changing engine paths, builds MOSS at a pinned revision, downloads its
Q5_K model, prepares VibeVoice and its checkpoint, prepares the speaker encoder,
runs tests, builds/packages, verifies installed wheels, and installs the service.
No cloud Omi account or original ZIP is needed. The service owns vault publication.

Optional switches:

- `-SkipVibe7`: MOSS transcription/diarization plus speaker recognition; skips
  the large 7B download/environment and disables refinement in installed service.
- `-SkipSpeakers`: skips encoder setup; existing configured encoder paths are
  preserved. To disable an existing encoder, explicitly clear both speaker fields.
- `-MossBinaryDir 'C:\existing\moss\bin'`: reuse your existing executable/DLLs.
- `-PrepareOnly`: prepare engines/build/test without installing a service.
- `-EngineRoot`, `-PipelineConfig`: change machine-local destinations.

Full VibeVoice setup downloads multi-GB weights and uses CPU/float32 by default;
this is not a throughput guarantee. For NVIDIA execution, separately install a
compatible CUDA PyTorch build in its configured venv and set `vibe_7b_device` /
`vibe_7b_dtype` appropriately before installing/restarting. Setup intentionally
uses CPU so it does not guess your GPU/driver. Re-running full setup selects CPU.

Register the tray from normal, non-administrator PowerShell:

```powershell
.\dist\windows\register-tray.ps1
```

## Existing installations and individual steps

Do not run fresh setup over an installed service; it refuses to replace it.
To update application code while preserving engine configuration/data:

```powershell
python -m pip install .\omi\firmware\scripts\omi-local .
python scripts\test.py
.\windows\build.ps1
python scripts\test-installed.py dist\windows\python
```

Exit the tray, run `windows\uninstall.ps1` as administrator (retains all state),
then `dist\windows\install.ps1` with the **same** key, vault, incoming and installed
pipeline config paths; include `-SkipVibe7` if that was your configuration. See
[README](../README.md) for full installer arguments. Restart/register the tray.

To provision missing engines without reinstalling application code:

```powershell
.\windows\setup-engines.ps1 -Python 'C:\Program Files\Python312\python.exe' `
  -Config 'C:\SecondBrainConfig\pipeline.json'
.\windows\setup-speakers.ps1 -Python 'C:\Program Files\Python312\python.exe' `
  -PipelineConfig 'C:\SecondBrainConfig\pipeline.json'
```

Engine setup writes new paths; reinstall the service with that config to grant
its account read access. Copying a repo example over an installed config would
lose local settings. Backups are private too; they may contain endpoint details.

The distributed bundle contains the same engine setup scripts, source/model
manifest and examples. It uses `setup-engines.ps1`, `setup-speakers.ps1`, then
`install.ps1`; the combined `setup.ps1` is for source checkouts only.

## Process one file without the service

After installing the application packages and engines:

```powershell
.\windows\run-audio.ps1 `
  -Python 'C:\Program Files\SecondBrain\python\Scripts\python.exe' `
  -Audio 'D:\SecondBrain-Audio\incoming\example.opus' `
  -Config 'C:\ProgramData\SecondBrain\config\pipeline.json' `
  -OutputDir 'D:\SecondBrain-Audio\test-results\run-001' `
  -FfmpegDir 'C:\ffmpeg\bin' -SkipVibe7 -NoHermes
```

Use a new output directory. Equivalent portable entry point:
`python -m second_brain.adaptive.pipeline --audio <file> --config <pipeline.json>
--output-dir <new-directory> --skip-vibe7 --no-hermes`.
This produces manifest/report/transcripts/clips; it does **not** publish notes or
consume receiver checkpoints. The service adds queue, identity store and vault
publication. `check` validates paths/configuration, not recognition accuracy.

## Dependency and verification boundaries

[engines.lock.json](../config/engines.lock.json) pins MOSS/VibeVoice source and all
three model snapshots. MOSS submodules follow its pinned Git tree. Git source
checkouts are managed separately and dirty/unmanaged directories are refused.
Torch/torchaudio, SpeechBrain, Transformers and Hugging Face Hub are pinned in
setup scripts. Other transitive Python dependencies still resolve through pip;
this is not a fully offline or completely reproducible dependency lock.

MOSS and VibeVoice retain their upstream licenses in downloaded source; model
cards accompany weights. The MOSS runtime is built from
[localai-org/moss-transcribe.cpp](https://github.com/localai-org/moss-transcribe.cpp),
its GGUF from [mudler](https://huggingface.co/mudler/moss-transcribe.cpp-gguf), and
VibeVoice from [Microsoft](https://github.com/microsoft/VibeVoice).

`test-installed.py` installs only the main wheel into a clean venv, resolves the
receiver dependency, checks imports/entry points, then runs synthetic audio
through installed pipeline/gate/clip code using a deterministic ASR substitute.
It catches missing package code; it does not validate model accuracy. CI also
builds the actual pinned MOSS CLI on Windows. Real model/GPU performance and the
user's machine permissions remain separate checks; see [validation](validation.md).
