param([string]$Python = "python")
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
function Run([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}
Push-Location $repo
try {
    Run dotnet @('run', '--project', 'windows/SecondBrain.Status.Tests', '-c', 'Release')
    Run dotnet @('publish', 'windows/SecondBrain.Service', '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true', '-o', 'dist/windows/service')
    Run dotnet @('publish', 'windows/SecondBrain.Tray', '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true', '-o', 'dist/windows/tray')
    Get-ChildItem 'dist/windows/python/*.whl' -ErrorAction SilentlyContinue | Remove-Item
    Run $Python @('-m', 'pip', 'wheel', '--no-deps', '--wheel-dir', 'dist/windows/python', '.', './omi/firmware/scripts/omi-local')
    Copy-Item (Join-Path $PSScriptRoot 'install.ps1') 'dist/windows/install.ps1' -Force
    Copy-Item (Join-Path $PSScriptRoot 'uninstall.ps1') 'dist/windows/uninstall.ps1' -Force
    Copy-Item (Join-Path $PSScriptRoot 'register-tray.ps1') 'dist/windows/register-tray.ps1' -Force
    New-Item -ItemType Directory -Force 'dist/windows/config' | Out-Null
    Copy-Item 'config/*.example.json' 'dist/windows/config/' -Force
    Copy-Item 'config/engines.lock.json' 'dist/windows/config/' -Force
    Copy-Item (Join-Path $PSScriptRoot 'setup-engines.ps1') 'dist/windows/setup-engines.ps1' -Force
    Copy-Item (Join-Path $PSScriptRoot 'setup-speakers.ps1') 'dist/windows/setup-speakers.ps1' -Force
    foreach ($file in @('setup-common.ps1', 'build-moss.ps1', 'download-model.py', 'run-audio.ps1')) {
        Copy-Item (Join-Path $PSScriptRoot $file) "dist/windows/$file" -Force
    }
    Copy-Item 'README.md' 'dist/windows/README.md' -Force
    Copy-Item 'SYSTEM.md' 'dist/windows/SYSTEM.md' -Force
    New-Item -ItemType Directory -Force 'dist/windows/docs' | Out-Null
    Copy-Item 'docs/*.md' 'dist/windows/docs/' -Force
    Write-Host 'Built dist/windows. No service has been installed or started.'
} finally { Pop-Location }
