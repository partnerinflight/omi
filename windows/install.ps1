#Requires -RunAsAdministrator
# Same parameters as before; the implementation is shared with the installer wizard.
# Run from a built bundle (dist\windows) or from the repository's windows\ folder.
$core = Join-Path $PSScriptRoot 'install-core.ps1'
if (-not (Test-Path $core)) { $core = Join-Path (Split-Path $PSScriptRoot -Parent) 'installer\install-core.ps1' }
if (-not (Test-Path $core)) { throw "install-core.ps1 not found next to $PSCommandPath or in installer\" }
& $core @args
if (-not $?) { throw 'Installation failed' }
