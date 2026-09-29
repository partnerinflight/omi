param([string]$EngineRoot = 'C:\second-brain-asr-engines')
. "$PSScriptRoot/setup-common.ps1"
Require-Command cmake
$lock = Get-EngineLock
$source = Join-Path $EngineRoot ('sources/moss-' + $lock.moss.revision.Substring(0,12))
Get-PinnedSource $lock.moss.url $lock.moss.revision $source
$build = Join-Path $source 'build'
# VS 2022 Desktop development with C++ must be installed. Static dependencies
# put the CLI in one predictable directory without a DLL search-path dependency.
Invoke-Checked cmake @('-S', $source, '-B', $build, '-G', 'Visual Studio 17 2022', '-A', 'x64', '-DMT_BUILD_CLI=ON', '-DMT_BUILD_TESTS=OFF', '-DBUILD_SHARED_LIBS=OFF')
Invoke-Checked cmake @('--build', $build, '--config', 'Release', '--target', 'moss-transcribe-cli', '--parallel', '4')
$exe = Join-Path $build 'Release/moss-transcribe.exe'
if (-not (Test-Path $exe)) { throw "MOSS build did not produce $exe" }
Invoke-Checked $exe @('version')
# Caller uses the same pinned destination, avoiding parsing compiler stdout.
