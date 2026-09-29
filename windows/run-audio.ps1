param(
    [Parameter(Mandatory=$true)][string]$Audio,
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$OutputDir,
    [string]$Python = 'C:\Program Files\SecondBrain\python\Scripts\python.exe',
    [string]$FfmpegDir = '',
    [switch]$SkipVibe7,
    [switch]$NoHermes
)
. "$PSScriptRoot/setup-common.ps1"
foreach ($path in @($Audio, $Config)) { if (-not (Test-Path $path)) { throw "Required path not found: $path" } }
if (Test-Path $OutputDir) { throw 'Use a new output directory; existing results will not be overwritten.' }
$oldPath = $env:PATH
try {
    if ($FfmpegDir) { $env:PATH = "$FfmpegDir;$oldPath" }
    $arguments = @('-m', 'second_brain.adaptive.pipeline', '--audio', (Resolve-Path $Audio).Path, '--config', (Resolve-Path $Config).Path, '--output-dir', [IO.Path]::GetFullPath($OutputDir))
    if ($SkipVibe7) { $arguments += '--skip-vibe7' }
    if ($NoHermes) { $arguments += '--no-hermes' }
    Invoke-Checked $Python $arguments
} finally { $env:PATH = $oldPath }
Write-Host 'Standalone processing complete. Review manifest.json and report.md; this command does not publish vault notes.'
