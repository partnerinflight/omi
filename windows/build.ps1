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
    Run $Python @('-m', 'pip', 'wheel', '--no-deps', '--wheel-dir', 'dist/windows/python', '.', './omi/firmware/scripts/omi-local')
    Copy-Item (Join-Path $PSScriptRoot 'install.ps1') 'dist/windows/install.ps1' -Force
    Copy-Item (Join-Path $PSScriptRoot 'uninstall.ps1') 'dist/windows/uninstall.ps1' -Force
    Copy-Item (Join-Path $PSScriptRoot 'register-tray.ps1') 'dist/windows/register-tray.ps1' -Force
    Copy-Item 'config' 'dist/windows/config' -Recurse -Force
    Write-Host 'Built dist/windows. No service has been installed or started.'
} finally { Pop-Location }
