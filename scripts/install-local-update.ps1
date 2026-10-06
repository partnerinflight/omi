$ErrorActionPreference = 'Stop'
$wheel = Join-Path $PSScriptRoot '..\dist\router-update\second_brain_local-0.2.1-py3-none-any.whl'
$receiverWheel = Join-Path $PSScriptRoot '..\dist\router-update\omi_local-0.2.0-py3-none-any.whl'
$python = 'C:\Program Files\SecondBrain\python\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $wheel)) { throw 'Build the router-update wheel first.' }
if (-not (Test-Path -LiteralPath $receiverWheel)) { throw 'Build the receiver wheel first.' }
Stop-Service SecondBrain
try {
    & $python -I -m pip install --no-deps --force-reinstall $wheel $receiverWheel
    if ($LASTEXITCODE -ne 0) { throw 'Wheel installation failed.' }
    & $python -I -c "from second_brain.router import EXTRACT_PROMPT; assert 'two shoes' in EXTRACT_PROMPT"
    if ($LASTEXITCODE -ne 0) { throw 'Installed router verification failed.' }
    & $python -I -c "import inspect; from second_brain.speakers import match; assert 'exemplars' in inspect.signature(match).parameters"
    if ($LASTEXITCODE -ne 0) { throw 'Installed speaker matcher verification failed.' }
} finally {
    Start-Service SecondBrain
}
Get-Service SecondBrain
