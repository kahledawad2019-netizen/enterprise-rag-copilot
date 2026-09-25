# Run the copilot locally with Ollama (Windows PowerShell).
#
#   .\run_local.ps1            # first run creates the virtualenv and installs
#   .\run_local.ps1 --pull     # also pull a missing embedding model
#
# Arguments are passed to run_local.py (see its --help).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$app = Join-Path $root "local-enterprise-copilot"
$python = Join-Path $app ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "==> Creating the virtualenv (first run only)"
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv venv --python 3.12 (Join-Path $app ".venv")
        uv pip install --python $python -e "$app[dev]" "uvicorn[standard]"
    } else {
        py -3.12 -m venv (Join-Path $app ".venv")
        & $python -m pip install --upgrade pip
        & $python -m pip install -e "$app[dev]" "uvicorn[standard]"
    }
}

& $python (Join-Path $root "run_local.py") @args
