# agenticContext Windows test entrypoint.
# Code version: v1.1.0-codex.1

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

Push-Location $ProjectRoot
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    & $Python @PythonArgs -m pytest -q -p no:cacheprovider -m $TestMarkExpression @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
