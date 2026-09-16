# agenticContext Python resolver.
# Code version: v1.2.0-codex.1

param(
    [ValidateSet("version", "runtime", "test", "quality")]
    [string]$Mode = "version"
)

$ErrorActionPreference = "Stop"
$env:AGENTIC_CONTEXT_RESOLVED_PYTHON = $null
$env:AGENTIC_CONTEXT_RESOLVED_PYTHON_ARGS = $null

function Test-SupportedPython([string]$Executable, [string[]]$Arguments = @()) {
    try {
        $version = & $Executable @Arguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -ne 0 -or ([version]$version -lt [version]'3.13')) {
            return $false
        }
        if ($Mode -eq "version") {
            return $true
        }
        $checker = Join-Path $PSScriptRoot "check_python_requirements.py"
        $requirements = Join-Path (Split-Path -Parent $PSScriptRoot) "requirements.txt"
        $checkerCode = 'import runpy, sys; checker = sys.argv[1]; sys.argv = sys.argv[1:]; runpy.run_path(checker, run_name="__main__")'
        $requirementOutput = & $Executable @Arguments -c $checkerCode $checker $Mode $requirements 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Warning (
                "Skipping Python with incompatible $Mode dependencies: " +
                "$Executable ($($requirementOutput -join '; '))"
            )
            return $false
        }
        return $true
    } catch {
        return $false
    }
}

$ExplicitPython = if ($env:AGENTIC_CONTEXT_PYTHON) {
    $env:AGENTIC_CONTEXT_PYTHON
} else {
    $env:CACHELIKES_PYTHON
}

if ($ExplicitPython) {
    if (Test-SupportedPython $ExplicitPython) {
        $env:AGENTIC_CONTEXT_RESOLVED_PYTHON = $ExplicitPython
        return
    }
    throw "AGENTIC_CONTEXT_PYTHON must point to Python 3.13 or newer with compatible $Mode dependencies."
}

$python = Get-Command py -ErrorAction SilentlyContinue
if ($python -and (Test-SupportedPython $python.Source @("-3"))) {
    $env:AGENTIC_CONTEXT_RESOLVED_PYTHON = $python.Source
    $env:AGENTIC_CONTEXT_RESOLVED_PYTHON_ARGS = "-3"
    return
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($python -and (Test-SupportedPython $python.Source)) {
    $env:AGENTIC_CONTEXT_RESOLVED_PYTHON = $python.Source
    return
}

throw "Install Python 3.13 or newer, or set AGENTIC_CONTEXT_PYTHON to a supported interpreter."
