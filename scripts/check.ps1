# agenticContext Windows quality gate.
# Code version: v1.8.0-codex.0

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "resolve_python.ps1") -Mode quality
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

    Write-Host "[1/6] Python environment checks"
    & $Python @PythonArgs scripts/check_python_requirements.py quality requirements.txt
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python @PythonArgs -m pip check
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python @PythonArgs -m pip_audit --local --progress-spinner off
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[2/6] Python static checks"
    & $Python @PythonArgs -m ruff check main.py app tests scripts
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[3/6] Local documentation checks"
    & $Python @PythonArgs scripts/check_docs.py
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[4/6] JavaScript syntax checks"
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

    Write-Host "[5/6] JavaScript unit tests"
    $jsTestFiles = @(
        Get-ChildItem -Path "tests" -Filter "test_*.mjs" -File |
            Sort-Object Name |
            ForEach-Object { $_.FullName }
    )
    if ($jsTestFiles.Count -eq 0) {
        Write-Error "No JavaScript unit test files were found."
        exit 1
    }
    & node --test @jsTestFiles
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "[6/6] Python tests with branch coverage"
    $ShardCoverageMinimum = $CoverageMinimum
    $IsSharded = $null -ne $env:AGENTIC_CONTEXT_TEST_SHARD -or $null -ne $env:AGENTIC_CONTEXT_TEST_SHARD_COUNT
    if ($IsSharded) {
        $ShardCoverageMinimum = & $Python @PythonArgs scripts/quality_shards.py minimum $CoverageMinimum
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    $CoverageStartedAt = [DateTime]::UtcNow
    [string[]]$PytestArguments = @(
        "-q", "-p", "no:cacheprovider", "-m", $TestMarkExpression,
        "--cov=app", "--cov-branch", "--cov-report=term-missing",
        "--cov-report=json:test-results/coverage.json", "--cov-fail-under=$ShardCoverageMinimum"
    )
    if ($IsSharded) {
        & $Python @PythonArgs scripts/quality_shards.py run -- @PytestArguments
    } else {
        & $Python @PythonArgs -m pytest @PytestArguments
    }
    $pytestExitCode = $LASTEXITCODE
    if ($pytestExitCode -ne 0) { exit $pytestExitCode }
    $CoverageReport = Get-Item "test-results/coverage.json" -ErrorAction SilentlyContinue
    if (-not $CoverageReport -or $CoverageReport.LastWriteTimeUtc -lt $CoverageStartedAt) {
        throw "Pytest did not produce a fresh coverage report."
    }
    if ($IsSharded) {
        Write-Host "Quality shard passed. Complete platform coverage aggregation is still required."
    } else {
        & $Python @PythonArgs scripts/check_coverage.py test-results/coverage.json --policy scripts/coverage_policy.json
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Write-Host "Quality gate passed."
    }
    exit 0
} finally {
    Pop-Location
}
