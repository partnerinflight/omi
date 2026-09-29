param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$PipelineConfig,
    [string]$EngineRoot = 'C:\second-brain-asr-engines\speaker-recognition',
    [string]$Revision = ''
)
. "$PSScriptRoot/setup-common.ps1"
if (-not $Revision) { $Revision = (Get-EngineLock).speaker_model.revision }
if ($Revision -notmatch '^[0-9a-f]{40}$') { throw 'Use a full model commit SHA for -Revision.' }
function Run([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}
if (-not (Test-Path $PipelineConfig)) { throw 'Copy pipeline.example.json to a private configuration first.' }
New-Item -ItemType Directory -Force $EngineRoot | Out-Null
Run $Python @('-m', 'venv', "$EngineRoot\python")
$encoder = "$EngineRoot\python\Scripts\python.exe"
# Separate CPU environment so ASR/CUDA dependencies are not changed.
Run $encoder @('-m', 'pip', 'install', 'torch==2.8.0', 'torchaudio==2.8.0', '--index-url', 'https://download.pytorch.org/whl/cpu')
Run $encoder @('-m', 'pip', 'install', 'speechbrain==1.0.3', 'huggingface-hub==0.34.6')
$model = "$EngineRoot\model"
Run $encoder @('-c', 'import sys; from huggingface_hub import snapshot_download; snapshot_download(''speechbrain/spkrec-ecapa-voxceleb'', revision=sys.argv[2], local_dir=sys.argv[1], allow_patterns=[''hyperparams.yaml'', ''*.ckpt'', ''label_encoder.txt'', ''README.md''])', $model, $Revision)
$cfg = Get-Content $PipelineConfig -Raw | ConvertFrom-Json
$cfg | Add-Member -NotePropertyName speaker_python -NotePropertyValue $encoder -Force
$cfg | Add-Member -NotePropertyName speaker_model -NotePropertyValue $model -Force
Save-PipelineConfig $PipelineConfig $cfg
Write-Host 'Speaker encoder downloaded and configured. Install/reinstall the service to grant it model access.'
Write-Host 'No recordings or voice profiles were uploaded. Runtime inference uses these local weights offline.'
