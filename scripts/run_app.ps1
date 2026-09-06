# agenticContext Windows launcher.
# Code version: v1.0.2-codex.1

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "resolve_python.ps1")
$Python = $env:AGENTIC_CONTEXT_RESOLVED_PYTHON
[string[]]$PythonArgs = if ($env:AGENTIC_CONTEXT_RESOLVED_PYTHON_ARGS) {
    $env:AGENTIC_CONTEXT_RESOLVED_PYTHON_ARGS -split ' '
} else {
    @()
}

Push-Location $ProjectRoot
try {
    & $Python @PythonArgs (Join-Path $ProjectRoot "main.py")
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
