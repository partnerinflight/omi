param(
    [Parameter(Mandatory=$true)][string]$Config,
    [string]$Python = 'python',
    [string]$EngineRoot = 'C:\second-brain-asr-engines',
    [string]$MossBinaryDir = '',
    # Build MOSS for an NVIDIA GPU (CUDA Toolkit required) and run it there.
    [switch]$Cuda,
    [switch]$SkipVibe7
)
. "$PSScriptRoot/setup-common.ps1"
if (-not (Test-Path $Config)) { throw 'Copy config/pipeline.example.json to a private config first.' }
if (-not [IO.Path]::IsPathRooted($EngineRoot)) { throw 'EngineRoot must be an absolute machine-local path.' }
$cfg = Get-Content $Config -Raw | ConvertFrom-Json
$lock = Get-EngineLock
$lockPath = Join-Path $PSScriptRoot 'config/engines.lock.json'
if (-not (Test-Path $lockPath)) { $lockPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'config/engines.lock.json' }
New-Item -ItemType Directory -Force $EngineRoot | Out-Null
$setupEnv = Join-Path $EngineRoot 'setup-python'
Invoke-Checked $Python @('-m', 'venv', $setupEnv)
$setupPython = Join-Path $setupEnv 'Scripts/python.exe'
Invoke-Checked $setupPython @('-m', 'pip', 'install', 'huggingface-hub==0.34.6')

if ($MossBinaryDir) {
    $MossBinaryDir = (Resolve-Path $MossBinaryDir).Path
    Invoke-Checked (Join-Path $MossBinaryDir 'moss-transcribe.exe') @('version')
} else {
    & "$PSScriptRoot/build-moss.ps1" -EngineRoot $EngineRoot -Cuda:$Cuda
    $MossBinaryDir = Join-Path $EngineRoot ('sources/moss-' + $lock.moss.revision.Substring(0,12) + $(if ($Cuda) { '/build-cuda/Release' } else { '/build/Release' }))
}
# moss-transcribe falls back to CPU when the requested device is unavailable.
Set-ConfigValue $cfg 'moss_device' $(if ($Cuda) { 'cuda' } else { 'cpu' })
$mossModel = Join-Path $EngineRoot ('models/moss-' + $lock.moss_model.revision.Substring(0,12))
Invoke-Checked $setupPython @("$PSScriptRoot/download-model.py", '--lock', $lockPath, '--model', 'moss_model', '--destination', $mossModel)
Set-ConfigValue $cfg 'moss_cpp_engine_dir' $MossBinaryDir
Set-ConfigValue $cfg 'moss_model' (Join-Path $mossModel 'moss-transcribe-q5_k.gguf')
Invoke-Checked (Join-Path $MossBinaryDir 'moss-transcribe.exe') @('info', $cfg.moss_model)

if (-not $SkipVibe7) {
    $vibe = Join-Path $EngineRoot ('sources/VibeVoice-' + $lock.vibe.revision.Substring(0,12))
    Get-PinnedSource $lock.vibe.url $lock.vibe.revision $vibe
    $venv = Join-Path $vibe '.venv'
    Invoke-Checked $Python @('-m', 'venv', $venv)
    $vibePython = Join-Path $venv 'Scripts/python.exe'
    Invoke-Checked $vibePython @('-m', 'pip', 'install', 'torch==2.8.0', 'torchaudio==2.8.0', '--index-url', 'https://download.pytorch.org/whl/cpu')
    Invoke-Checked $vibePython @('-m', 'pip', 'install', $vibe, 'transformers==4.51.3', 'huggingface-hub==0.34.6', 'soundfile', 'librosa')
    Invoke-Checked $vibePython @('-c', 'from vibevoice.modular.modeling_vibevoice_asr import VibeVoiceASRForConditionalGeneration as M; from vibevoice.processor.vibevoice_asr_processor import VibeVoiceASRProcessor; assert callable(M.streaming_generate); print("VibeVoice streaming API OK")')
    $vibeModel = Join-Path $EngineRoot ('models/vibe-' + $lock.vibe_model.revision.Substring(0,12))
    Invoke-Checked $setupPython @("$PSScriptRoot/download-model.py", '--lock', $lockPath, '--model', 'vibe_model', '--destination', $vibeModel)
    Set-ConfigValue $cfg 'vibe_repo' $vibe
    Set-ConfigValue $cfg 'vibe_python' $vibePython
    Set-ConfigValue $cfg 'vibe_7b_model' $vibeModel
    Set-ConfigValue $cfg 'vibe_7b_device' 'cpu'
    Set-ConfigValue $cfg 'vibe_7b_dtype' 'float32'
}
Save-PipelineConfig $Config $cfg
Write-Host 'Engine setup complete. Config backed up and updated. CPU environments selected; see docs/install-windows.md for GPU setup.'
