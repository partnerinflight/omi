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
# Windows' own bsdtar: Git's GNU tar (often on PATH) reads 'C:\...' as a remote host.
$tar = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not (Test-Path $tar)) { throw "Windows tar.exe not found at $tar (Windows 10 1803+ ships it)." }
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
    # Stage beside the destination: Move-Item cannot move a folder across volumes (e.g. %TEMP% on C:).
    $parent = Split-Path $Destination -Parent
    New-Item -ItemType Directory -Force $parent | Out-Null
    $staging = Join-Path $parent ('.sb-python-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force $staging | Out-Null
    try {
        Invoke-Checked $tar @('-xzf', $archive, '-C', $staging) | Out-Host   # archive root is python\
        Move-Item (Join-Path $staging 'python') $Destination
    } finally { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }
    $python = Join-Path $Destination 'python.exe'
    Invoke-Checked $python @('-I', '-c', 'import sys; assert sys.version_info[:2] == (3, 12), sys.version') | Out-Host
    return $python
}

function Copy-Tree([string]$From, [string]$To) {
    New-Item -ItemType Directory -Force $To | Out-Null
    Copy-Item (Join-Path $From '*') $To -Recurse -Force
}

function Copy-Files([string]$From, [string[]]$Filters, [string]$To) {
    New-Item -ItemType Directory -Force $To | Out-Null
    foreach ($filter in $Filters) {
        Get-ChildItem $From -File -Filter $filter | Copy-Item -Destination $To -Force
    }
}

# MOSS ignores /build/ but not /build-cuda/; without this the next cached build-moss.ps1 run
# sees an untracked build-cuda\ and refuses the checkout as locally modified.
function Set-MossBuildIgnored([string]$Source) {
    $info = Join-Path $Source '.git/info'
    if (-not (Test-Path (Join-Path $Source '.git'))) { return }   # not cloned yet
    $exclude = Join-Path $info 'exclude'
    if (-not (Test-Path $exclude) -or -not (Select-String -Path $exclude -SimpleMatch '/build-cuda/' -Quiet)) {
        New-Item -ItemType Directory -Force $info | Out-Null
        Add-Content $exclude '/build-cuda/'
    }
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
if ($wheels.Count -eq 0) { throw "windows/build.ps1 produced no wheels in $bundle\python" }
Invoke-Checked $appPython (@('-I', '-m', 'pip', 'install', '--no-warn-script-location') + $wheels)
Invoke-Checked $appPython @('-I', '-c', 'import second_brain.runtime, omi_local.server; print(''app packages OK'')')
# build.ps1 copies install-core.ps1, the setup/build helpers, download-model.py and config into dist\windows.
Copy-Files $bundle @('*.ps1', 'download-model.py') (Join-Path $payload 'scripts')
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
$mossSource = Join-Path $engineRoot ('sources/moss-' + $lock.moss.revision.Substring(0,12))
Set-MossBuildIgnored $mossSource
& (Join-Path $repo 'windows/build-moss.ps1') -EngineRoot $engineRoot
Set-MossBuildIgnored $mossSource
# Static build: the CLI is self-contained; copy it alone, not CMake's intermediate files.
Copy-Files (Join-Path $mossSource 'build/Release') @('moss-transcribe.exe') (Join-Path $payload 'engines\moss-cpu')
New-Item -ItemType Directory -Force (Join-Path $payload 'licenses\moss') | Out-Null
Copy-Item (Join-Path $mossSource 'LICENSE') (Join-Path $payload 'licenses\moss') -Force
Copy-Item (Join-Path $mossSource 'third_party/ggml/LICENSE') (Join-Path $payload 'licenses\moss\ggml-LICENSE') -Force

if (-not $Slim) {
    Write-Host '==> MOSS (CUDA)'
    & (Join-Path $repo 'windows/build-moss.ps1') -EngineRoot $engineRoot -Cuda
    # build-moss.ps1 copies the CUDA runtime DLLs next to the CLI.
    Copy-Files (Join-Path $mossSource 'build-cuda/Release') @('moss-transcribe.exe', '*.dll') (Join-Path $payload 'engines\moss-cuda')

    Write-Host '==> NeMo-Speech (speaker linking)'
    & (Join-Path $repo 'windows/build-nemo-speech.ps1') -EngineRoot $engineRoot
    $nemoSource = Join-Path $engineRoot ('sources/nemo-speech-' + $lock.nemo_speech.revision.Substring(0,12))
    Copy-Files (Join-Path $nemoSource 'build-cpu/bin') @('nemo-speech.exe', '*.dll') (Join-Path $payload 'engines\nemo-speech')
    New-Item -ItemType Directory -Force (Join-Path $payload 'licenses\nemo-speech') | Out-Null
    foreach ($name in @('LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md')) {
        Copy-Item (Join-Path $nemoSource $name) (Join-Path $payload 'licenses\nemo-speech') -Force
    }

    Write-Host '==> voice-matching runtime'
    $voicePython = New-StandalonePython (Join-Path $payload 'runtimes\voice\python')
    Invoke-Checked $voicePython @('-I', '-m', 'pip', 'install', '--no-warn-script-location', '-r', (Join-Path $PSScriptRoot 'requirements-voice.txt'))
    Invoke-Checked $voicePython @('-I', '-c', 'import torch, speechbrain; print(''voice runtime OK'', torch.__version__)')

    Write-Host '==> VibeVoice 7B runtime (CUDA PyTorch)'
    $vibeSource = Join-Path $engineRoot ('sources/VibeVoice-' + $lock.vibe.revision.Substring(0,12))
    Get-PinnedSource $lock.vibe.url $lock.vibe.revision $vibeSource
    $vibePython = New-StandalonePython (Join-Path $payload 'runtimes\vibe\python')
    Invoke-Checked $vibePython @('-I', '-m', 'pip', 'install', '--no-warn-script-location', '-r', (Join-Path $PSScriptRoot 'requirements-vibe.txt'), $vibeSource)
    Invoke-Checked $vibePython @('-I', '-c', 'from vibevoice.modular.modeling_vibevoice_asr import VibeVoiceASRForConditionalGeneration as M; import torch, accelerate; assert callable(M.streaming_generate); print(''VibeVoice runtime OK'', torch.__version__, ''cuda build:'', torch.version.cuda)')
    New-Item -ItemType Directory -Force (Join-Path $payload 'licenses\vibevoice') | Out-Null
    Copy-Item (Join-Path $vibeSource 'LICENSE') (Join-Path $payload 'licenses\vibevoice') -Force
}

Write-Host '==> licences and manifest'
New-Item -ItemType Directory -Force (Join-Path $payload 'licenses') | Out-Null
Copy-Item (Join-Path $repo 'LICENSE') (Join-Path $payload 'licenses\SecondBrain-LICENSE') -Force
Copy-Item (Join-Path $payload 'app\python\LICENSE.txt') (Join-Path $payload 'licenses\python-LICENSE.txt') -Force
Set-Content (Join-Path $payload 'licenses\README.txt') "Third-party licences for software shipped in this installer. Python packages keep theirs in each runtime's site-packages. Models are downloaded at install time under their own licences (see config\engines.lock.json)." -Encoding UTF8
Invoke-Checked $appPython @('-I', (Join-Path $repo 'scripts/make-manifest.py'), '--lock', (Join-Path $repo 'config/engines.lock.json'),
    '--payload', $payload, '--version', $Version, '--out', (Join-Path $payload 'release-manifest.json'))
Write-Host "Payload staged in $payload$(if ($Slim) { ' (slim)' })."
