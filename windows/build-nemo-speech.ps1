param(
    [string]$EngineRoot = 'C:\second-brain-asr-engines'
)
# CPU-only NeMo-Speech.cpp CLI for Nemotron-3-Diarization speaker linking (opt-in).
# Prerequisites (see the pinned source's docs/development/windows-build.md): Visual Studio
# 2022 Build Tools with the C++ workload, CMake >= 3.26, Ninja and Git. The upstream driver
# imports the MSVC environment and checks for git, cl, cmake and ninja itself, so CMake and
# Ninja may come from Visual Studio rather than the PATH of this shell.
. "$PSScriptRoot/setup-common.ps1"
$lock = Get-EngineLock
$source = Join-Path $EngineRoot ('sources/nemo-speech-' + $lock.nemo_speech.revision.Substring(0,12))
# The repository keeps Mandarin TTS data in Git LFS; this ASR + diarization build needs none.
$lfsSkip = $env:GIT_LFS_SKIP_SMUDGE
$env:GIT_LFS_SKIP_SMUDGE = '1'
try { Get-PinnedSource $lock.nemo_speech.url $lock.nemo_speech.revision $source }
finally { $env:GIT_LFS_SKIP_SMUDGE = $lfsSkip }
$build = Join-Path $source 'build-cpu'
# -AsrOnly selects the upstream 'asr' profile: CLI, ASR and diarization, no TTS.
# The driver bootstraps its pinned vcpkg (SentencePiece) under %LOCALAPPDATA%\NeMoSpeech
# and links it statically; ggml DLLs land next to nemo-speech.exe in <BuildDir>\bin.
Invoke-Checked powershell @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    (Join-Path $source 'scripts/windows/build.ps1'), '-Backend', 'cpu', '-AsrOnly', '-BuildDir', $build)
$exe = Join-Path $build 'bin/nemo-speech.exe'
if (-not (Test-Path $exe)) { throw "NeMo-Speech build did not produce $exe" }
# The service runs the CLI by absolute path; its DLLs must sit beside it, not on PATH.
if (-not (Get-ChildItem (Split-Path $exe) -Filter 'ggml*.dll')) { throw "ggml DLLs missing next to $exe" }
Invoke-Checked $exe @('--help')
