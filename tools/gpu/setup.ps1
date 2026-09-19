# Rebuild the isolated Windows/Python 3.11 environment from exact package versions.
$ErrorActionPreference = 'Stop'
$gpuPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required. Install uv first, then rerun this script.'
}
if (-not (Test-Path -LiteralPath $gpuPython)) {
    & uv venv (Join-Path $PSScriptRoot '.venv') --python 3.11
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& uv pip sync --python $gpuPython --link-mode copy `
    --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/xpu `
    --index-strategy unsafe-best-match (Join-Path $PSScriptRoot 'requirements-lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'GPU dependency installation failed.' }
& uv pip check --python $gpuPython
if ($LASTEXITCODE -ne 0) { throw 'GPU dependency compatibility check failed.' }
& $gpuPython -m ipykernel install --user --name leobox-intel-arc --display-name 'Leobox Intel Arc (Python 3.11)'
if ($LASTEXITCODE -ne 0) { throw 'Notebook kernel registration failed.' }
& $gpuPython (Join-Path $PSScriptRoot 'verify.py')
exit $LASTEXITCODE
