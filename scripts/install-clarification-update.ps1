$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$wheel = Join-Path $repo 'dist\router-update\second_brain_local-0.2.1-py3-none-any.whl'
$tray = Join-Path $repo 'dist\windows\tray'
$installed = 'C:\Program Files\SecondBrain\tray'
$python = 'C:\Program Files\SecondBrain\python\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $wheel)) { throw 'Build the service wheel first.' }
if (-not (Test-Path -LiteralPath (Join-Path $tray 'SecondBrain.Tray.exe'))) { throw 'Build the tray first.' }
Start-Transcript -Path (Join-Path $repo 'dist\clarification-install.log') -Force
try {
    $backup = Join-Path $repo ('dist\tray-backup-' + (Get-Date -Format yyyyMMddHHmmss))
    Copy-Item -LiteralPath $installed -Destination $backup -Recurse
    Get-Process SecondBrain.Tray -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq (Join-Path $installed 'SecondBrain.Tray.exe') } | Stop-Process
    Stop-Service SecondBrain
    try {
        & $python -I -m pip install --no-deps --force-reinstall $wheel
        if ($LASTEXITCODE -ne 0) { throw 'Service wheel installation failed.' }
        & $python -I -c "from second_brain.clarifications import Clarifications; from second_brain.router import EXTRACT_PROMPT; assert 'clarifications' in EXTRACT_PROMPT"
        if ($LASTEXITCODE -ne 0) { throw 'Installed clarification verification failed.' }
        Copy-Item -Path (Join-Path $tray '*') -Destination $installed -Recurse -Force
    } finally { Start-Service SecondBrain }
    Get-Service SecondBrain
} finally { Stop-Transcript }
