#Requires -RunAsAdministrator
param([string]$Python = 'python', [string]$Bundle = '', [string]$FfmpegDir = '')
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
if (-not $Bundle) { $Bundle = Join-Path $repo 'dist\windows' }
if (-not $FfmpegDir) { $FfmpegDir = Split-Path (Get-Command ffmpeg.exe).Source }
$root = Join-Path $env:ProgramData ('SecondBrainSmoke-' + [Guid]::NewGuid().ToString('N'))
$name = 'SecondBrainSmoke'
if (Get-Service $name -ErrorAction SilentlyContinue) { throw 'An existing smoke service is present; refusing to replace it.' }
$pythonPath = (Get-Command $Python).Source
$env:PYTHONPATH = "$repo\src;$repo\omi\firmware\scripts\omi-local;$repo"
& $pythonPath -m tests.windows_smoke_fixture prepare --root $root
if ($LASTEXITCODE -ne 0) { throw 'Fixture preparation failed' }
try {
    & "$PSScriptRoot\install.ps1" -Python $pythonPath -PipelineConfig "$root\pipeline.json" -SecretFile "$root\secret.hex" -Vault "$root\vault" -IncomingDir "$root\incoming" -DataRoot "$root\machine" -InstallDir "$root\app" -Bundle $Bundle -FfmpegDir $FfmpegDir -SkipVibe7 -ServiceName $name -Port 17331
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 500
        $statusPath = "$root\machine\status\status.json"
        if (Test-Path $statusPath) { $status = Get-Content $statusPath -Raw | ConvertFrom-Json } else { $status = $null }
    } until (($status -and $status.service -eq 'running') -or (Get-Date) -gt $deadline)
    if (-not $status -or $status.service -ne 'running') { throw "Service failed to publish a heartbeat; inspect $root" }
    & $pythonPath -m tests.windows_smoke_fixture upload --port 17331
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic upload failed' }
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 500
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
    } until ($status.counts.complete -eq 1 -or (Get-Date) -gt $deadline)
    if ($status.counts.complete -ne 1) { throw "Service did not publish the synthetic note; inspect $root" }
    $notes = @(Get-ChildItem "$root\vault" -Recurse -Filter *.md)
    if ($notes.Count -ne 1) { throw 'Expected one note' }
    & $pythonPath -m tests.windows_smoke_fixture review --root $root
    if ($LASTEXITCODE -ne 0) { throw 'Speaker review/PCM playback fixture failed' }
    $previousStart = $status.started
    Restart-Service $name
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 500
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
    } until (($status.service -eq 'running' -and $status.started -gt $previousStart) -or (Get-Date) -gt $deadline)
    if ($status.service -ne 'running' -or $status.started -le $previousStart) { throw 'Service did not publish a fresh heartbeat after restart' }
    if (@(Get-ChildItem "$root\vault" -Recurse -Filter *.md).Count -ne 1) { throw 'Restart duplicated a note' }
    & $pythonPath -m tests.windows_smoke_fixture verify-review --root $root
    if ($LASTEXITCODE -ne 0) { throw 'Speaker identity did not survive restart' }
    $svc = Get-CimInstance Win32_Service -Filter "Name='$name'"
    $process = Get-Process -Id $svc.ProcessId
    if ($process.SessionId -ne 0) { throw 'Service is not running in Session 0' }
    # Reinstall must keep settings the installer does not own, and the service must be able to
    # write meeting notes (installer/install-core.ps1, src/second_brain/install_config.py).
    $serviceJson = "$root\machine\config\service.json"
    $edited = Get-Content $serviceJson -Raw | ConvertFrom-Json
    $edited | Add-Member -NotePropertyName meetings_enabled -NotePropertyValue $true -Force
    $edited | Add-Member -NotePropertyName owner_name -NotePropertyValue 'Smoke Owner' -Force
    $edited | Add-Member -NotePropertyName poll_seconds -NotePropertyValue 3 -Force
    $edited | ConvertTo-Json -Depth 10 | Set-Content $serviceJson -Encoding UTF8
    # The status file keeps the old heartbeat across uninstall; require one newer than this.
    $beforeReinstall = $status.started
    & "$PSScriptRoot\uninstall.ps1" -ServiceName $name
    # sc.exe delete only marks the service; install refuses while SCM still lists it.
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Service $name -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    # Same -SecretFile as the first install: the core refuses a different pairing key.
    & "$PSScriptRoot\install.ps1" -Python $pythonPath -PipelineConfig "$root\pipeline.json" -SecretFile "$root\secret.hex" -Vault "$root\vault" -IncomingDir "$root\incoming" -DataRoot "$root\machine" -InstallDir "$root\app" -Bundle $Bundle -FfmpegDir $FfmpegDir -SkipVibe7 -ServiceName $name -Port 17331
    $kept = Get-Content $serviceJson -Raw | ConvertFrom-Json
    if (-not $kept.meetings_enabled -or $kept.owner_name -ne 'Smoke Owner' -or $kept.poll_seconds -ne 3) {
        throw "Reinstall lost settings: $(Get-Content $serviceJson -Raw)"
    }
    # Get-Acl reports resolvable accounts by name and unresolvable ones as a SID string; accept
    # either form instead of translating every entry, which throws for orphaned SIDs.
    $account = 'NT SERVICE\' + $name
    $sid = (New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier]).Value
    $modify = [Security.AccessControl.FileSystemRights]::Modify
    $writable = (Get-Acl "$root\vault\Omi\Meetings").Access | Where-Object {
        $_.IdentityReference.Value -in @($account, $sid) -and
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        ($_.FileSystemRights -band $modify) -eq $modify
    }
    if (-not $writable) { throw 'The service cannot write meeting notes (no Modify on Omi\Meetings)' }
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 500
        $status = Get-Content $statusPath -Raw | ConvertFrom-Json
    } until (($status.service -eq 'running' -and $status.started -gt $beforeReinstall) -or (Get-Date) -gt $deadline)
    if ($status.service -ne 'running' -or $status.started -le $beforeReinstall) { throw 'Service did not publish a fresh heartbeat after reinstall' }
    Write-Host "PASS: virtual service account, Session 0, synthetic upload, adaptive pipeline, vault publication, restart without duplication, reinstall keeps settings, meetings folder writable. Evidence: $root"
} catch {
    # Synthetic fixture only: surface startup diagnostics before cleanup removes
    # the service. Never collect logs from the user's production service here.
    Get-WinEvent -FilterHashtable @{ LogName='Application'; StartTime=(Get-Date).AddMinutes(-10) } -ErrorAction SilentlyContinue |
        Where-Object { $_.ProviderName -in @($name, '.NET Runtime', 'Application Error') } |
        Select-Object -First 10 TimeCreated, ProviderName, Message | Format-List | Out-Host
    Get-WinEvent -FilterHashtable @{ LogName='System'; ProviderName='Service Control Manager'; StartTime=(Get-Date).AddMinutes(-10) } -ErrorAction SilentlyContinue |
        Where-Object { $_.Message -like "*$name*" -or $_.Message -like '*Second Brain*' } |
        Select-Object -First 10 TimeCreated, Message | Format-List | Out-Host
    Get-Content "$root\machine\data\service.log" -Tail 60 -ErrorAction SilentlyContinue | Out-Host
    Get-Content "$root\machine\status\status.json" -ErrorAction SilentlyContinue | Out-Host
    Get-ChildItem "$root\machine\data\jobs" -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -in @('pipeline.log', 'progress.json') } |
        ForEach-Object { Write-Host $_.FullName; Get-Content $_.FullName -Tail 60 | Out-Host }
    Get-CimInstance Win32_Process |
        Where-Object { $_.Name -in @('python.exe', 'ffmpeg.exe', 'ffprobe.exe') } |
        Select-Object ProcessId, ParentProcessId, Name, CommandLine | Format-List | Out-Host
    throw
} finally {
    & "$PSScriptRoot\uninstall.ps1" -ServiceName $name
}
