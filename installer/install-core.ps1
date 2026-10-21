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
    [string]$ReviewUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name,
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
$reviewSid = (New-Object Security.Principal.NTAccount($ReviewUser)).Translate([Security.Principal.SecurityIdentifier]).Value
$reviewPrincipal = '*' + $reviewSid
$paths = @($InstallDir, $DataRoot, "$DataRoot\config", "$DataRoot\data", "$DataRoot\status", "$DataRoot\review", "$DataRoot\review\requests", "$DataRoot\review\responses", "$DataRoot\review\clips", $IncomingDir)
foreach ($path in $paths) { New-Item -ItemType Directory -Force -Path $path | Out-Null }
Run icacls.exe @($DataRoot, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
foreach ($component in @('service', 'tray')) {
    New-Item -ItemType Directory -Force "$InstallDir\$component" | Out-Null
    Copy-Item "$Bundle\$component\*" "$InstallDir\$component\" -Recurse -Force
}
Run $Python @('-I', '-m', 'venv', "$InstallDir\python")
$workerPython = "$InstallDir\python\Scripts\python.exe"
$wheels = @(Get-ChildItem "$Bundle\python\*.whl" | ForEach-Object FullName)
if ($wheels.Count -lt 2) { throw 'Bundle is missing Python wheels. Run windows/build.ps1 first.' }
# Developer PYTHONPATH/egg-info must not make pip skip the service's wheels.
# Reinstall explicit wheels even when an earlier bundle used the same version.
Run $workerPython (@('-I', '-m', 'pip', 'install', '--force-reinstall') + $wheels)
$key = (Get-Content $SecretFile -Raw).Trim()
if ($key -notmatch '^[0-9a-fA-F]{64}$') { throw 'Existing receiver pairing key must contain 64 hexadecimal characters.' }
$targetKey = "$DataRoot\config\upload-secret.hex"
if ((Test-Path $targetKey) -and (Get-Content $targetKey -Raw).Trim() -ne $key) { throw 'A different pairing key is already installed; refusing to replace it.' }
[IO.File]::WriteAllText($targetKey, $key + "`n")
$pipelineTarget = "$DataRoot\config\pipeline.json"
if ([IO.Path]::GetFullPath($PipelineConfig) -ne [IO.Path]::GetFullPath($pipelineTarget)) { Copy-Item $PipelineConfig $pipelineTarget -Force }
# Reinstall keeps every setting the owner or a later feature added; only installer-owned keys
# change (src/second_brain/install_config.py). Defaults fill keys that are missing.
$installerKeys = [ordered]@{
    managed = [ordered]@{
        data_dir = "$DataRoot\data"; incoming_dir = [IO.Path]::GetFullPath($IncomingDir)
        secret_file = $targetKey; pipeline_config = $pipelineTarget
        vault_path = $Vault; status_file = "$DataRoot\status\status.json"; port = $Port
        ffmpeg_dir = $FfmpegDir; skip_vibe7 = [bool]$SkipVibe7
    }
    defaults = [ordered]@{
        vault_folder = 'Omi/Conversations'; lectures_vault_folder = 'Omi/Lectures'; host = '0.0.0.0'
        poll_seconds = 2; retry_seconds = 60; max_attempts = 5; job_timeout_seconds = 14400
        no_hermes = $false
    }
}
$installerFile = Join-Path $env:TEMP ('secondbrain-install-' + [guid]::NewGuid().ToString('N') + '.json')
try {
    $installerKeys | ConvertTo-Json -Depth 4 | Set-Content $installerFile -Encoding UTF8
    Run $workerPython @('-I', '-m', 'second_brain.install_config', '--target', "$DataRoot\config\service.json", '--installer', $installerFile)
} finally { Remove-Item $installerFile -ErrorAction SilentlyContinue }
$serviceConfig = Get-Content "$DataRoot\config\service.json" -Raw -Encoding UTF8 | ConvertFrom-Json
# Omi notes publish to the configured vault_folder, known only after the merge.
$conversationsFolder = if ($serviceConfig.vault_folder) { $serviceConfig.vault_folder } else { 'Omi/Conversations' }
$conversationsPath = Join-Path $Vault ($conversationsFolder -replace '/', '\')
New-Item -ItemType Directory -Force -Path $conversationsPath | Out-Null
# Surface the preflight's own reasons instead of a bare exit code.
$checkOutput = & $workerPython -I -m second_brain.cli check --config "$DataRoot\config\service.json"
if ($LASTEXITCODE -ne 0) {
    $report = try { ($checkOutput -join "`n") | ConvertFrom-Json } catch { $null }
    if ($report -and $report.errors) { throw ("Preflight check failed:`n" + (($report.errors | ForEach-Object { "  - $_" }) -join "`n")) }
    throw ("Preflight check failed with exit code ${LASTEXITCODE}:`n" + ($checkOutput -join "`n"))
}
$binary = '"' + "$InstallDir\service\SecondBrain.Service.exe" + '" --python "' + $workerPython + '" --config "' + "$DataRoot\config\service.json" + '" --service-name ' + $ServiceName
if (-not [System.Diagnostics.EventLog]::SourceExists($ServiceName)) { [System.Diagnostics.EventLog]::CreateEventSource($ServiceName, 'Application') }
# Virtual service account: no stored user password and no interactive login.
# binPath contains quotes, which Windows PowerShell 5.1 does not escape for native
# commands, so pass sc.exe a pre-escaped command line verbatim.
$createArgs = 'create ' + $ServiceName + ' binPath= "' + $binary.Replace('"', '\"') + '" start= delayed-auto obj= "NT SERVICE\' + $ServiceName + '" DisplayName= "Second Brain"'
$create = Start-Process sc.exe -ArgumentList $createArgs -NoNewWindow -Wait -PassThru
if ($create.ExitCode -ne 0) { throw "sc.exe create failed with exit code $($create.ExitCode)" }
try {
    Run sc.exe @('description', $ServiceName, 'Omi receiver, adaptive transcription and Obsidian delivery')
    Run sc.exe @('failure', $ServiceName, 'reset=', '86400', 'actions=', 'restart/15000/restart/60000/restart/300000')
    $sid = (New-Object Security.Principal.NTAccount(('NT SERVICE\' + $ServiceName))).Translate([Security.Principal.SecurityIdentifier]).Value
    $principal = '*' + $sid
    # Private state/logs/key: service and administrators only. Tray receives coarse status only.
    Run icacls.exe @($DataRoot, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', "${principal}:(OI)(CI)RX")
    foreach ($path in @("$DataRoot\data", $IncomingDir)) { Run icacls.exe @($path, '/grant:r', "${principal}:(OI)(CI)M", '/T', '/Q') }
    Run icacls.exe @("$DataRoot\status", '/grant:r', "${principal}:(OI)(CI)M", '*S-1-5-32-545:(OI)(CI)RX', '/T', '/Q')
    # Only the configured interactive user may listen/name speakers. Never grant
    # all Users access to transcripts, snippets or voice references.
    Run icacls.exe @("$DataRoot\review", '/reset', '/T', '/Q')
    Run icacls.exe @("$DataRoot\review", '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', "${principal}:(OI)(CI)M", "${reviewPrincipal}:(OI)(CI)RX")
    Run icacls.exe @("$DataRoot\review\requests", '/grant:r', "${reviewPrincipal}:(OI)(CI)M", '/T', '/Q')
    # Read-only grants rely on (OI)(CI) inheritance instead of /T, which would stamp
    # an explicit entry on every file in large Python/model trees.
    Run icacls.exe @($InstallDir, '/grant:r', "${principal}:(OI)(CI)RX", '/Q')
    Run icacls.exe @($Vault, '/grant', "${principal}:(OI)(CI)RX", '/Q')
    Run icacls.exe @($conversationsPath, '/grant', "${principal}:(OI)(CI)M", '/T', '/Q')
    # Meeting notes publish here (meetings_vault_folder). Granted even while meetings are off, so
    # turning them on later needs no reinstall. Config.load already rejects unsafe folder values.
    $meetingsFolder = if ($serviceConfig.meetings_vault_folder) { $serviceConfig.meetings_vault_folder } else { 'Omi/Meetings' }
    $meetingsPath = Join-Path $Vault ($meetingsFolder -replace '/', '\')
    New-Item -ItemType Directory -Force $meetingsPath | Out-Null
    Run icacls.exe @($meetingsPath, '/grant', "${principal}:(OI)(CI)M", '/T', '/Q')
    # Lecture transcript and summary notes publish here (lectures_vault_folder).
    $lecturesFolder = if ($serviceConfig.lectures_vault_folder) { $serviceConfig.lectures_vault_folder } else { 'Omi/Lectures' }
    $lecturesPath = Join-Path $Vault ($lecturesFolder -replace '/', '\')
    New-Item -ItemType Directory -Force $lecturesPath | Out-Null
    Run icacls.exe @($lecturesPath, '/grant', "${principal}:(OI)(CI)M", '/T', '/Q')
    # The knowledge router appends under "## Router Inbox" in these folders (router_enabled).
    foreach ($folder in @('People', 'Projects', 'Topics', 'Decisions', 'Ideas', 'Daily', 'System\Router')) {
        New-Item -ItemType Directory -Force "$Vault\$folder" | Out-Null
        Run icacls.exe @("$Vault\$folder", '/grant', "${principal}:(OI)(CI)M", '/T', '/Q')
    }
    $pipeline = Get-Content $pipelineTarget -Raw | ConvertFrom-Json
    $pythonBase = (& $Python -c 'import sys; print(sys.base_prefix)').Trim()
    $enginePaths = @($pipeline.moss_cpp_engine_dir, $pipeline.moss_model, $FfmpegDir, $pythonBase)
    if ($pipeline.speaker_python -and $pipeline.speaker_model) {
        $speakerRoots = (& $pipeline.speaker_python -c 'import sys,json; print(json.dumps([sys.prefix,sys.base_prefix]))') | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Speaker encoder interpreter is not runnable' }
        $enginePaths += @($pipeline.speaker_model) + $speakerRoots
    }
    if ($pipeline.speaker_linking_command) {
        # The NeMo-Speech exe directory holds its ggml DLLs; the Nemotron GGUF may live elsewhere.
        if (-not ($pipeline.speaker_linking_command -is [array])) { throw 'speaker_linking_command must be a JSON list' }
        if (-not $pipeline.speaker_linking_model) { throw 'speaker_linking_model is required with speaker_linking_command' }
        $enginePaths += @((Split-Path $pipeline.speaker_linking_command[0]), $pipeline.speaker_linking_model)
    }
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
        if ([IO.Path]::GetPathRoot($path) -eq $path) { throw 'Refusing read access to an entire drive for an engine' }
        if (-not (Test-Path $path)) { throw "Engine/model path unavailable: $path" }
    }
    # Engines often share a base interpreter or nest a venv in their repo; grant each tree once.
    $grantRoots = @()
    foreach ($path in ($enginePaths | ForEach-Object { [IO.Path]::GetFullPath($_).TrimEnd('\') } | Sort-Object -Unique)) {
        if (-not ($grantRoots | Where-Object { $path.StartsWith($_ + '\', [StringComparison]::OrdinalIgnoreCase) })) { $grantRoots += $path }
    }
    foreach ($path in $grantRoots) { Run icacls.exe @($path, '/grant', "${principal}:(OI)(CI)RX", '/Q') }
    # Pin caches to machine state; normal service operation never downloads models.
    New-ItemProperty ('HKLM:\SYSTEM\CurrentControlSet\Services\' + $ServiceName) -Name Environment -PropertyType MultiString -Value @("HF_HOME=$DataRoot\data\model-cache", 'HF_HUB_OFFLINE=1', 'TRANSFORMERS_OFFLINE=1') -Force | Out-Null
    if (-not (Get-NetFirewallRule -DisplayName ($ServiceName + ' Omi receiver') -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName ($ServiceName + ' Omi receiver') -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress LocalSubnet | Out-Null
    }
    if (-not $NoStart) { Start-Service $ServiceName }
    Write-Host 'Installed SecondBrain with automatic delayed startup and crash recovery.'
    Write-Host 'Run register-tray.ps1 as your normal logged-in user to enable the tray UI.'
    Write-Host "Speaker review access: $ReviewUser"
    Write-Host "The existing standalone receiver must be stopped before this service can bind port $Port."
} catch {
    Stop-Service $ServiceName -ErrorAction SilentlyContinue
    & sc.exe delete $ServiceName | Out-Null
    throw
}
