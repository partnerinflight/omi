param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$SecretFile,
    [Parameter(Mandatory=$true)][string]$FfmpegDir,
    [Parameter(Mandatory=$true)][string]$IncomingDir,
    [string]$Vault = 'C:\Users\Eugene\SecondBrain',
    [string]$PipelineConfig = 'C:\SecondBrainConfig\pipeline.json',
    [string]$EngineRoot = 'C:\second-brain-asr-engines',
    [string]$MossBinaryDir = '',
    [string]$ReviewUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name,
    [switch]$Cuda,
    [switch]$SkipVibe7,
    [switch]$SkipSpeakers,
    [switch]$PrepareOnly
)
. "$PSScriptRoot/setup-common.ps1"
$repo = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path "$repo/pyproject.toml")) { throw 'Run this entry point from a source checkout. Bundles use setup-engines.ps1 then install.ps1.' }
if (-not [IO.Path]::IsPathRooted($Python) -or $Python -like '*WindowsApps*') { throw 'Use an absolute machine-wide Python 3.12+ executable.' }
Invoke-Checked $Python @('-c', 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"')
foreach ($path in @($Python, $SecretFile, $Vault, "$FfmpegDir/ffmpeg.exe", "$FfmpegDir/ffprobe.exe")) {
    if (-not (Test-Path $path)) { throw "Required path not found: $path" }
}
foreach ($name in @('dotnet', 'git')) { Require-Command $name }
if (-not $MossBinaryDir) { Require-Command cmake }
if (-not $PrepareOnly) {
    $admin = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run setup in Administrator PowerShell to install the service; use -PrepareOnly to build without installing.' }
    if (Get-Service SecondBrain -ErrorAction SilentlyContinue) { throw 'Service already installed. See docs/install-windows.md for a data-preserving upgrade; setup will not replace a running service.' }
    if (Get-NetTCPConnection -State Listen -LocalPort 7331 -ErrorAction SilentlyContinue) { throw 'Stop the old receiver on port 7331 first.' }
}
$oldPath = $env:PATH
try {
    $env:PATH = "$FfmpegDir;$oldPath"
    if (-not (Test-Path $PipelineConfig)) {
        New-Item -ItemType Directory -Force (Split-Path $PipelineConfig -Parent) | Out-Null
        Copy-Item "$repo/config/pipeline.example.json" $PipelineConfig
    }
    & "$PSScriptRoot/setup-engines.ps1" -Python $Python -Config $PipelineConfig -EngineRoot $EngineRoot -MossBinaryDir $MossBinaryDir -Cuda:$Cuda -SkipVibe7:$SkipVibe7
    if (-not $SkipSpeakers) {
        & "$PSScriptRoot/setup-speakers.ps1" -Python $Python -PipelineConfig $PipelineConfig -EngineRoot "$EngineRoot/speaker-recognition"
    }
    Invoke-Checked $Python @("$repo/scripts/test.py")
    & "$PSScriptRoot/build.ps1" -Python $Python
    Invoke-Checked $Python @("$repo/scripts/test-installed.py", "$repo/dist/windows/python")
    if (-not $PrepareOnly) {
        & "$repo/dist/windows/install.ps1" -Python $Python -PipelineConfig $PipelineConfig -SecretFile $SecretFile -FfmpegDir $FfmpegDir -Vault $Vault -IncomingDir $IncomingDir -ReviewUser $ReviewUser -SkipVibe7:$SkipVibe7
    }
} finally { $env:PATH = $oldPath }
Write-Host 'Prepared bundle and engine configuration. If installed, run dist/windows/register-tray.ps1 as your normal user.'
