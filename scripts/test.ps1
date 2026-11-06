# agenticContext Windows test entrypoint.
# Code version: v1.3.0-codex.0

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "resolve_python.ps1") -Mode test
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

Push-Location $ProjectRoot
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    if ($null -ne $env:AGENTIC_CONTEXT_TEST_SHARD -or $null -ne $env:AGENTIC_CONTEXT_TEST_SHARD_COUNT) {
        & $Python @PythonArgs scripts/quality_shards.py run -- -q -p no:cacheprovider -m $TestMarkExpression @args
    } else {
        & $Python @PythonArgs -m pytest -q -p no:cacheprovider -m $TestMarkExpression @args
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
