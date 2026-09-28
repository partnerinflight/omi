param([string]$InstallDir = "$env:ProgramFiles\SecondBrain", [string]$DataRoot = "$env:ProgramData\SecondBrain", [switch]$Unregister)
$ErrorActionPreference = 'Stop'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
if ($Unregister) { Remove-ItemProperty $runKey -Name SecondBrainTray -ErrorAction SilentlyContinue; return }
$exe = "$InstallDir\tray\SecondBrain.Tray.exe"
if (-not (Test-Path $exe)) { throw "Tray app missing: $exe" }
New-Item $runKey -Force | Out-Null
New-ItemProperty $runKey -Name SecondBrainTray -PropertyType String -Value ('"' + $exe + '" "' + "$DataRoot\status\status.json" + '"') -Force | Out-Null
Start-Process -FilePath $exe -ArgumentList ('"' + "$DataRoot\status\status.json" + '"')
