param(
    [string]$EngineRoot = 'C:\second-brain-asr-engines',
    # NVIDIA GPU build; needs the CUDA Toolkit (CUDA_PATH). 'native' targets the GPU in this PC.
    [switch]$Cuda,
    [string]$CudaArchitectures = 'native'
)
. "$PSScriptRoot/setup-common.ps1"
Require-Command cmake
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
if (-not (Test-Path $vswhere)) { throw 'Install Visual Studio 2022 or 2026 Build Tools with Desktop development with C++.' }
$version = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationVersion
if ($LASTEXITCODE -ne 0 -or -not $version) { throw 'No Visual Studio C++ toolchain found.' }
$major = ([version]$version.Trim()).Major
$generator = switch ($major) {
    17 { 'Visual Studio 17 2022' }
    18 { 'Visual Studio 18 2026' }
    default { throw "Unsupported Visual Studio version: $version" }
}
$lock = Get-EngineLock
$source = Join-Path $EngineRoot ('sources/moss-' + $lock.moss.revision.Substring(0,12))
Get-PinnedSource $lock.moss.url $lock.moss.revision $source
$build = Join-Path $source $(if ($Cuda) { 'build-cuda' } else { 'build' })
$gpuArgs = @()
if ($Cuda) {
    if (-not $env:CUDA_PATH -or -not (Test-Path (Join-Path $env:CUDA_PATH 'bin/nvcc.exe'))) { throw 'Install the NVIDIA CUDA Toolkit (CUDA_PATH must point to it).' }
    $gpuArgs = @('-DMT_GGML_CUDA=ON', "-DCMAKE_CUDA_ARCHITECTURES=$CudaArchitectures", '-T', "cuda=$env:CUDA_PATH")
}
# Visual Studio Desktop development with C++ must be installed. Static dependencies
# put the CLI in one predictable directory without a DLL search-path dependency.
Invoke-Checked cmake (@('-S', $source, '-B', $build, '-G', $generator, '-A', 'x64', '-DMT_BUILD_CLI=ON', '-DMT_BUILD_TESTS=OFF', '-DBUILD_SHARED_LIBS=OFF') + $gpuArgs)
Invoke-Checked cmake @('--build', $build, '--config', 'Release', '--target', 'moss-transcribe-cli', '--parallel', '4')
$exe = Join-Path $build 'Release/moss-transcribe.exe'
if (-not (Test-Path $exe)) { throw "MOSS build did not produce $exe" }
if ($Cuda) {
    # The service must not depend on PATH: ship the CUDA runtime next to the CLI.
    foreach ($dll in @('cudart64_*.dll', 'cublas64_*.dll', 'cublasLt64_*.dll')) {
        $found = Get-ChildItem (Join-Path $env:CUDA_PATH 'bin') -Filter $dll | Select-Object -First 1
        if (-not $found) { throw "CUDA runtime $dll not found in CUDA_PATH (bin folder)" }
        Copy-Item $found.FullName (Split-Path $exe) -Force
    }
}
Invoke-Checked $exe @('version')
# Caller uses the same pinned destination, avoiding parsing compiler stdout.
