# Real local Git/PowerShell boundary checks; no model download or service changes.
. "$PSScriptRoot/setup-common.ps1"
$root = Join-Path ([IO.Path]::GetTempPath()) ('second-brain-setup-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
try {
    $origin = Join-Path $root 'origin'
    Invoke-Checked git @('init', $origin)
    Invoke-Checked git @('-C', $origin, 'config', 'user.email', 'fixture@example.invalid')
    Invoke-Checked git @('-C', $origin, 'config', 'user.name', 'Setup fixture')
    Set-Content (Join-Path $origin 'CMakeLists.txt') 'fixture'
    Invoke-Checked git @('-C', $origin, 'add', '.')
    Invoke-Checked git @('-C', $origin, 'commit', '-m', 'fixture')
    $sha = & git -C $origin rev-parse HEAD
    $destination = Join-Path $root 'managed'
    Get-PinnedSource $origin $sha $destination
    Get-PinnedSource $origin $sha $destination
    Set-Content (Join-Path $destination 'CMakeLists.txt') 'user edit'
    $rejected = $false
    try { Get-PinnedSource $origin $sha $destination } catch {
        if ($_.Exception.Message -notlike '*local changes*') { throw }
        $rejected = $true
    }
    if (-not $rejected -or (Get-Content (Join-Path $destination 'CMakeLists.txt')) -ne 'user edit') { throw 'Setup did not preserve dirty source.' }
    $config = Join-Path $root 'pipeline.json'
    Set-Content $config '{"hermes_scoring_enabled":true,"custom":42}'
    $cfg = Get-Content $config -Raw | ConvertFrom-Json
    Set-ConfigValue $cfg 'moss_model' 'new-path'
    Save-PipelineConfig $config $cfg
    $saved = Get-Content $config -Raw | ConvertFrom-Json
    if ($saved.custom -ne 42 -or -not $saved.hermes_scoring_enabled -or $saved.moss_model -ne 'new-path') { throw 'Setup lost configuration.' }
    if (@(Get-ChildItem "$config.before-setup-*.bak").Count -ne 1) { throw 'Config backup missing.' }
    $rejected = $false
    try { Invoke-Checked ([Environment]::ProcessPath) @('-NoProfile', '-Command', 'exit 7') } catch {
        if ($_.Exception.Message -notlike '*exit code 7*') { throw }
        $rejected = $true
    }
    if (-not $rejected) { throw 'Native failure was ignored.' }
    Write-Host 'PASS: pinned checkout repeatability, dirty-source protection, config backup/preservation, native failure propagation.'
} finally { Remove-Item $root -Recurse -Force }
