<#
.SYNOPSIS
    Build and verify the whole system from scratch.

.DESCRIPTION
    Runs every step in dependency order and stops at the first failure. This is
    the script a new developer runs after cloning, and the one CI would run.

    Embedded Qdrant is single-process, so nothing else may hold the index while
    this runs. Stop the Streamlit app first.

.EXAMPLE
    .\scripts\run_all.ps1
    .\scripts\run_all.ps1 -SkipEval      # faster; skips the ~10 minute evaluation
    .\scripts\run_all.ps1 -Fresh         # rebuild the database and index from scratch
#>

[CmdletBinding()]
param(
    [switch]$SkipEval,
    [switch]$SkipTests,
    [switch]$Fresh
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Error "Virtual environment not found at $python. Run: py -3.13 -m venv .venv"
}

Set-Location $root
$started = Get-Date

function Invoke-Step {
    param([string]$Name, [string[]]$Arguments)

    Write-Host ''
    Write-Host ('=' * 78) -ForegroundColor DarkGray
    Write-Host "  $Name" -ForegroundColor Cyan
    Write-Host ('=' * 78) -ForegroundColor DarkGray

    $stepStart = Get-Date
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
    $elapsed = ((Get-Date) - $stepStart).TotalSeconds
    Write-Host ("  OK - {0:N1}s" -f $elapsed) -ForegroundColor Green
}

Invoke-Step 'Environment check' @('scripts\check_environment.py')
Invoke-Step 'Database schema'   @('scripts\setup_database.py')

$dataArgs = @('scripts\generate_synthetic_data.py')
Invoke-Step 'Synthetic data' $dataArgs

Invoke-Step 'Data validation' @('scripts\validate_data.py')

$indexArgs = @('scripts\build_index.py')
if ($Fresh) { $indexArgs += '--rebuild' }
Invoke-Step 'Document index' $indexArgs

Invoke-Step 'Index validation' @('scripts\build_index.py', '--validate')

if (-not $SkipTests) {
    Invoke-Step 'Test suite' @('-m', 'pytest', '-q')
}

if (-not $SkipEval) {
    Invoke-Step 'Retrieval evaluation (held-out)' @(
        'scripts\evaluate_retrieval.py', '--holdout', '--k', '8'
    )
}

$total = ((Get-Date) - $started).TotalMinutes
Write-Host ''
Write-Host ('=' * 78) -ForegroundColor DarkGray
Write-Host ("  All steps passed in {0:N1} minutes" -f $total) -ForegroundColor Green
Write-Host ('=' * 78) -ForegroundColor DarkGray
Write-Host ''
Write-Host '  Next:' -ForegroundColor Cyan
Write-Host '    .venv\Scripts\python scripts\copilot.py "what is the refund policy for annual plans?"'
Write-Host '    .venv\Scripts\streamlit run app\streamlit_app.py'
Write-Host ''
