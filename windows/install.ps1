#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$PipelineConfig,
    [Parameter(Mandatory=$true)][string]$SecretFile,
    [Parameter(Mandatory=$true)][string]$FfmpegDir,
    [string]$Vault = 'C:\Users\Eugene\SecondBrain',
    [string]$InstallDir = "$env:ProgramFiles\SecondBrain",
    [string]$DataRoot = "$env:ProgramData\SecondBrain",
    [string]$IncomingDir = '',
    [string]$Bundle = $PSScriptRoot,
    [ValidatePattern("^[A-Za-z][A-Za-z0-9]{0,48}$")][string]$ServiceName = "SecondBrain",
    [ValidateRange(1,65535)][int]$Port = 7331,
    [switch]$SkipVibe7,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
function Run([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}
if (-not $NoStart -and (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) { throw "Port $Port is in use. Stop the standalone receiver before installing, or use -NoStart." }
if (Get-Service $ServiceName -ErrorAction SilentlyContinue) { throw 'SecondBrain already exists. Use uninstall.ps1 (keeps all data), then reinstall.' }
foreach ($path in @($Python, $PipelineConfig, $SecretFile, $Vault, (Join-Path $FfmpegDir 'ffmpeg.exe'), (Join-Path $FfmpegDir 'ffprobe.exe'), (Join-Path $Bundle 'service\SecondBrain.Service.exe'))) {
    if (-not (Test-Path $path)) { throw "Required path not found: $path" }
}
if (-not [IO.Path]::IsPathRooted($Python) -or $Python -like '*WindowsApps*') { throw 'Use an absolute machine-wide Python 3.12+ executable, not a Windows Store alias.' }
if (-not [IO.Path]::IsPathRooted($Vault)) { throw 'Vault must be a local absolute path; mapped drives are unavailable before login.' }
if (-not $IncomingDir) { $IncomingDir = Join-Path $DataRoot 'incoming' }
$paths = @($InstallDir, $DataRoot, "$DataRoot\config", "$DataRoot\data", "$DataRoot\status", $IncomingDir, "$Vault\Omi\Conversations")
foreach ($path in $paths) { New-Item -ItemType Directory -Force -Path $path | Out-Null }
Run icacls.exe @($DataRoot, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
foreach ($component in @('service', 'tray')) {
    New-Item -ItemType Directory -Force "$InstallDir\$component" | Out-Null
    Copy-Item "$Bundle\$component\*" "$InstallDir\$component\" -Recurse -Force
}
Run $Python @('-m', 'venv', "$InstallDir\python")
$workerPython = "$InstallDir\python\Scripts\python.exe"
$wheels = @(Get-ChildItem "$Bundle\python\*.whl" | ForEach-Object FullName)
if ($wheels.Count -lt 2) { throw 'Bundle is missing Python wheels. Run windows/build.ps1 first.' }
Run $workerPython (@('-m', 'pip', 'install') + $wheels)
$key = (Get-Content $SecretFile -Raw).Trim()
if ($key -notmatch '^[0-9a-fA-F]{64}$') { throw 'Existing receiver pairing key must contain 64 hexadecimal characters.' }
$targetKey = "$DataRoot\config\upload-secret.hex"
if ((Test-Path $targetKey) -and (Get-Content $targetKey -Raw).Trim() -ne $key) { throw 'A different pairing key is already installed; refusing to replace it.' }
[IO.File]::WriteAllText($targetKey, $key + "`n")
$pipelineTarget = "$DataRoot\config\pipeline.json"
if ([IO.Path]::GetFullPath($PipelineConfig) -ne [IO.Path]::GetFullPath($pipelineTarget)) { Copy-Item $PipelineConfig $pipelineTarget -Force }
$cfg = [ordered]@{
    data_dir = "$DataRoot\data"; incoming_dir = [IO.Path]::GetFullPath($IncomingDir)
    secret_file = $targetKey; pipeline_config = $pipelineTarget
    vault_path = $Vault; vault_folder = 'Omi/Conversations'
    status_file = "$DataRoot\status\status.json"; host = '0.0.0.0'; port = $Port
    poll_seconds = 2; retry_seconds = 60; max_attempts = 5; job_timeout_seconds = 14400
    skip_vibe7 = [bool]$SkipVibe7; no_hermes = $false; ffmpeg_dir = $FfmpegDir
}
$cfg | ConvertTo-Json | Set-Content "$DataRoot\config\service.json" -Encoding UTF8
Run $workerPython @('-m', 'second_brain.cli', 'check', '--config', "$DataRoot\config\service.json")
$binary = '"' + "$InstallDir\service\SecondBrain.Service.exe" + '" --python "' + $workerPython + '" --config "' + "$DataRoot\config\service.json" + '" --service-name ' + $ServiceName
if (-not [System.Diagnostics.EventLog]::SourceExists($ServiceName)) { [System.Diagnostics.EventLog]::CreateEventSource($ServiceName, 'Application') }
# Virtual service account: no stored user password and no interactive login.
Run sc.exe @('create', $ServiceName, 'binPath=', $binary, 'start=', 'delayed-auto', 'obj=', ('NT SERVICE\' + $ServiceName), 'DisplayName=', 'Second Brain')
try {
    Run sc.exe @('description', $ServiceName, 'Omi receiver, adaptive transcription and Obsidian delivery')
    Run sc.exe @('failure', $ServiceName, 'reset=', '86400', 'actions=', 'restart/15000/restart/60000/restart/300000')
    $sid = (New-Object Security.Principal.NTAccount(('NT SERVICE\' + $ServiceName))).Translate([Security.Principal.SecurityIdentifier]).Value
    $principal = '*' + $sid
    # Private state/logs/key: service and administrators only. Tray receives coarse status only.
    Run icacls.exe @($DataRoot, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', "${principal}:(OI)(CI)RX")
    foreach ($path in @("$DataRoot\data", $IncomingDir)) { Run icacls.exe @($path, '/grant:r', "${principal}:(OI)(CI)M", '/T', '/Q') }
    Run icacls.exe @("$DataRoot\status", '/grant:r', "${principal}:(OI)(CI)M", '*S-1-5-32-545:(OI)(CI)RX', '/T', '/Q')
    Run icacls.exe @($InstallDir, '/grant:r', "${principal}:(OI)(CI)RX", '/T', '/Q')
    Run icacls.exe @($Vault, '/grant', "${principal}:(OI)(CI)RX", '/T', '/Q')
    Run icacls.exe @("$Vault\Omi\Conversations", '/grant', "${principal}:(OI)(CI)M", '/T', '/Q')
    $pipeline = Get-Content $pipelineTarget -Raw | ConvertFrom-Json
    $pythonBase = (& $Python -c 'import sys; print(sys.base_prefix)').Trim()
    $enginePaths = @($pipeline.moss_cpp_engine_dir, $pipeline.moss_model, $FfmpegDir, $pythonBase)
    if (-not $SkipVibe7) {
        $vibePython = if ($pipeline.vibe_python) { $pipeline.vibe_python } else { Join-Path $pipeline.vibe_repo '.venv\Scripts\python.exe' }
        $vibeRoots = (& $vibePython -c 'import sys,json; print(json.dumps([sys.prefix,sys.base_prefix]))') | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'VibeVoice interpreter is not runnable' }
        foreach ($path in $vibeRoots) {
            if ([IO.Path]::GetPathRoot($path) -eq $path) { throw 'Refusing an interpreter configured at the drive root' }
        }
        $enginePaths += @($pipeline.vibe_repo, $pipeline.vibe_7b_model) + $vibeRoots
    }
    foreach ($path in $enginePaths) {
        if (-not (Test-Path $path)) { throw "Engine/model path unavailable: $path" }
        Run icacls.exe @($path, '/grant', "${principal}:(OI)(CI)RX", '/T', '/Q')
    }
    # Pin caches to machine state; normal service operation never downloads models.
    New-ItemProperty ('HKLM:\SYSTEM\CurrentControlSet\Services\' + $ServiceName) -Name Environment -PropertyType MultiString -Value @("HF_HOME=$DataRoot\data\model-cache", 'HF_HUB_OFFLINE=1', 'TRANSFORMERS_OFFLINE=1') -Force | Out-Null
    if (-not (Get-NetFirewallRule -DisplayName ($ServiceName + ' Omi receiver') -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName ($ServiceName + ' Omi receiver') -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress LocalSubnet | Out-Null
    }
    if (-not $NoStart) { Start-Service $ServiceName }
    Write-Host 'Installed SecondBrain with automatic delayed startup and crash recovery.'
    Write-Host 'Run register-tray.ps1 as your normal logged-in user to enable the tray UI.'
    Write-Host "The existing standalone receiver must be stopped before this service can bind port $Port."
} catch {
    Stop-Service $ServiceName -ErrorAction SilentlyContinue
    & sc.exe delete $ServiceName | Out-Null
    throw
}
