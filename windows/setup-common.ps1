$ErrorActionPreference = 'Stop'
function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}
function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "Required command '$Name' is missing. See docs/install-windows.md." }
}
function Get-EngineLock {
    $path = Join-Path $PSScriptRoot 'config/engines.lock.json'
    if (-not (Test-Path $path)) { $path = Join-Path (Split-Path $PSScriptRoot -Parent) 'config/engines.lock.json' }
    return Get-Content $path -Raw | ConvertFrom-Json
}
function Get-PinnedSource([string]$Url, [string]$Revision, [string]$Destination) {
    Require-Command git
    if ($Revision -notmatch '^[0-9a-f]{40}$') { throw 'Engine revision must be a full commit SHA.' }
    $newClone = -not (Test-Path $Destination)
    if ($newClone) {
        New-Item -ItemType Directory -Force (Split-Path $Destination -Parent) | Out-Null
        Invoke-Checked git @('clone', '--no-checkout', $Url, $Destination)
    }
    $remote = & git -C $Destination remote get-url origin
    if ($LASTEXITCODE -ne 0 -or $remote.Trim() -ne $Url) { throw "Unexpected source repository at $Destination; refusing to modify it." }
    $gitDir = & git -C $Destination rev-parse --absolute-git-dir
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate engine Git metadata.' }
    $marker = Join-Path $gitDir 'second-brain-source'
    if (-not $newClone) {
        if (-not (Test-Path $marker)) { throw "Unmanaged engine checkout: $Destination" }
        $dirty = & git -C $Destination status --porcelain
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect engine checkout.' }
        if ($dirty) { throw "Engine checkout has local changes: $Destination" }
    }
    Invoke-Checked git @('-C', $Destination, 'fetch', '--depth', '1', 'origin', $Revision)
    Invoke-Checked git @('-C', $Destination, 'checkout', '--detach', $Revision)
    Invoke-Checked git @('-C', $Destination, 'submodule', 'update', '--init', '--recursive', '--depth', '1')
    # Keep ownership marker inside .git, outside the source worktree.
    $gitDir = & git -C $Destination rev-parse --absolute-git-dir
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate engine Git metadata.' }
    Set-Content (Join-Path $gitDir 'second-brain-source') $Revision
}
function Save-PipelineConfig([string]$Path, $Config) {
    $backup = "$Path.before-setup-$(Get-Date -Format yyyyMMdd-HHmmss-ffff).bak"
    Copy-Item $Path $backup
    $tmp = "$Path.tmp"
    $Config | ConvertTo-Json -Depth 30 | Set-Content $tmp -Encoding UTF8
    Move-Item $tmp $Path -Force
}
function Set-ConfigValue($Config, [string]$Name, $Value) {
    $Config | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
}
