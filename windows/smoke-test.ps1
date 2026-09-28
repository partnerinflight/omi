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
    Write-Host "PASS: virtual service account, Session 0, synthetic upload, adaptive pipeline, vault publication, restart without duplication. Evidence: $root"
} finally {
    & "$PSScriptRoot\uninstall.ps1" -ServiceName $name
}
