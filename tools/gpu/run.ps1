# Examples: ./run.ps1; ./run.ps1 my_script.py; ./run.ps1 -m pip --version
$ErrorActionPreference = 'Stop'
$gpuPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $gpuPython)) {
    throw 'GPU environment not found. Run tools/gpu/setup.ps1 first.'
}
if ($args.Count -eq 0) {
    & $gpuPython (Join-Path $PSScriptRoot 'verify.py')
} else {
    & $gpuPython @args
}
exit $LASTEXITCODE
