# agenticContext Windows dependency setup.
# Code version: v1.0.3-codex.1

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
    Write-Host "Using Python: $Python $($PythonArgs -join ' ')"
    & $Python @PythonArgs -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python @PythonArgs -m pip install -r (Join-Path $ProjectRoot "requirements-dev.txt")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $SkipPlaywrightInstall = if ($env:AGENTIC_CONTEXT_SKIP_PLAYWRIGHT_INSTALL) {
        $env:AGENTIC_CONTEXT_SKIP_PLAYWRIGHT_INSTALL
    } else {
        $env:CACHELIKES_SKIP_PLAYWRIGHT_INSTALL
    }
    if ($SkipPlaywrightInstall -ne "1") {
        & $Python @PythonArgs -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    Write-Host "Environment is ready."
    Write-Host "Run tests with: .\scripts\test.ps1"
    Write-Host "Run the quality gate with: .\scripts\check.ps1"
    Write-Host "Run the app with: .\scripts\run_app.ps1"
} finally {
    Pop-Location
}
