# agenticContext Windows quality gate.
# Code version: v1.3.0-codex.1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "resolve_python.ps1")
$Python = $env:AGENTIC_CONTEXT_RESOLVED_PYTHON
[string[]]$PythonArgs = if ($env:AGENTIC_CONTEXT_RESOLVED_PYTHON_ARGS) {
    $env:AGENTIC_CONTEXT_RESOLVED_PYTHON_ARGS -split ' '
} else {
    @()
}
$TestMarkExpression = if ($env:AGENTIC_CONTEXT_TEST_MARK_EXPRESSION) {
    $env:AGENTIC_CONTEXT_TEST_MARK_EXPRESSION
} elseif ($env:CACHELIKES_TEST_MARK_EXPRESSION) {
    $env:CACHELIKES_TEST_MARK_EXPRESSION
} else {
    "not live"
}

$CoverageMinimum = if ($env:AGENTIC_CONTEXT_COVERAGE_MINIMUM) {
    $env:AGENTIC_CONTEXT_COVERAGE_MINIMUM
} elseif ($env:CACHELIKES_COVERAGE_MINIMUM) {
    $env:CACHELIKES_COVERAGE_MINIMUM
} else {
    "55"
}

if ($CoverageMinimum -notmatch '^\d+$' -or [int]$CoverageMinimum -lt 0 -or [int]$CoverageMinimum -gt 100) {
    Write-Error "Coverage minimum must be an integer from 0 to 100."
    exit 1
}

Push-Location $ProjectRoot
try {
    New-Item -ItemType Directory -Force -Path "test-results" | Out-Null
    $env:COVERAGE_FILE = (Join-Path $ProjectRoot "test-results/.coverage")
    $env:PYTHONDONTWRITEBYTECODE = "1"

    Write-Host "Quality gate configuration: Python=$Python $($PythonArgs -join ' '), branch coverage minimum=${CoverageMinimum}%"

    Write-Host "[1/5] Python static checks"
    & $Python @PythonArgs -m ruff check main.py app tests scripts
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[2/5] Local documentation checks"
    & $Python @PythonArgs scripts/check_docs.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[3/5] JavaScript syntax checks"
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        Write-Error "Node.js is required for JavaScript syntax checks."
        exit 1
    }

    $jsFiles = @(
        Get-ChildItem -Path "app/web/static" -Recurse -Filter "*.js" -File |
            Sort-Object FullName
    )
    if ($jsFiles.Count -eq 0) {
        Write-Error "No first-party JavaScript files were found for syntax checks."
        exit 1
    }

    foreach ($scriptFile in $jsFiles) {
        & node --check $scriptFile.FullName
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    Write-Host "[4/5] JavaScript unit tests"
    & node --test tests/test_agent_optimization.mjs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[5/5] Python tests with branch coverage"
    $CoverageStartedAt = [DateTime]::UtcNow
    & $Python @PythonArgs -m pytest `
        -q `
        -p no:cacheprovider `
        -m $TestMarkExpression `
        --cov=app `
        --cov-branch `
        --cov-report=term-missing `
        --cov-report="json:test-results/coverage.json" `
        --cov-fail-under="$CoverageMinimum"
    $pytestExitCode = $LASTEXITCODE
    if ($pytestExitCode -ne 0) { exit $pytestExitCode }
    $CoverageReport = Get-Item "test-results/coverage.json" -ErrorAction SilentlyContinue
    if (-not $CoverageReport -or $CoverageReport.LastWriteTimeUtc -lt $CoverageStartedAt) {
        throw "Pytest did not produce a fresh coverage report."
    }

    Write-Host "Quality gate passed."
    exit 0
} finally {
    Pop-Location
}
