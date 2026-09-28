param([string]$Config = ".\config.json")
$ErrorActionPreference = "Stop"

if (-not (Test-Path $Config)) {
    throw "Missing $Config. Run: Copy-Item config.example.json config.json"
}

$cfg = Get-Content $Config -Raw | ConvertFrom-Json

function Require-Command($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "Required command '$name' was not found on PATH."
    }
}

Require-Command python
Require-Command git
Require-Command ffmpeg
Require-Command ffprobe

Write-Host "System Python:"
python --version

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    python -m pip install --user uv
    $userScripts = python -c "import site, pathlib; print(pathlib.Path(site.USER_BASE) / 'Scripts')"
    if (Test-Path $userScripts) { $env:Path = "$userScripts;$env:Path" }
}
Require-Command uv

Write-Host ""
Write-Host "=== MOSS C++ ==="
$source = $cfg.moss_cpp_source_dir
$dest = $cfg.moss_cpp_engine_dir
if (-not (Test-Path (Join-Path $source "moss-transcribe.exe"))) {
    throw "Working MOSS binary not found at $source"
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item (Join-Path $source "*") $dest -Recurse -Force
$mossExe = Join-Path $dest "moss-transcribe.exe"
& $mossExe version
if ($LASTEXITCODE -ne 0) {
    throw "Copied MOSS runtime failed to start. Exit code: $LASTEXITCODE"
}
if (-not (Test-Path $cfg.moss_model)) {
    throw "MOSS model not found: $($cfg.moss_model)"
}

Write-Host ""
Write-Host "=== Microsoft VibeVoice repo ==="
$vibeRepo = $cfg.vibe_repo
$vibeParent = Split-Path -Parent $vibeRepo
New-Item -ItemType Directory -Force -Path $vibeParent | Out-Null

if (-not (Test-Path $vibeRepo)) {
    git clone https://github.com/microsoft/VibeVoice.git $vibeRepo
} else {
    git -C $vibeRepo pull --ff-only
}

$vibeVenv = Join-Path $vibeRepo ".venv"
$vibePython = Join-Path $vibeVenv "Scripts\python.exe"
if (-not (Test-Path $vibePython)) {
    uv venv --python 3.12 $vibeVenv
}

uv pip install --python $vibePython -e $vibeRepo
uv pip install --python $vibePython "huggingface-hub>=0.34.0,<1.0" soundfile librosa

& $vibePython -c "import torch; from vibevoice.modular.modeling_vibevoice_asr import VibeVoiceASRForConditionalGeneration; print('Torch:', torch.__version__); print('CUDA:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE'); print('VibeVoice repo import: OK')"
if ($LASTEXITCODE -ne 0) {
    throw "VibeVoice preflight failed."
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "MOSS:      $dest"
Write-Host "VibeVoice: $vibeRepo"
