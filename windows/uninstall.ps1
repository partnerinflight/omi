#Requires -RunAsAdministrator
param([ValidatePattern('^[A-Za-z][A-Za-z0-9]{0,48}$')][string]$ServiceName = 'SecondBrain')
$ErrorActionPreference = 'Stop'
if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    Stop-Service $ServiceName
    & sc.exe delete $ServiceName
    if ($LASTEXITCODE -ne 0) { throw 'Service removal failed' }
}
Get-NetFirewallRule -DisplayName ($ServiceName + ' Omi receiver') -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host 'Service removed. Recordings, keys, models, job history, generated notes and application files are retained.'
Write-Host 'Run register-tray.ps1 -Unregister as the normal user to remove tray autostart.'
