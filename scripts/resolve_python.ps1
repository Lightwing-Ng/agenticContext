# agenticContext Python resolver.
# Code version: v1.1.1-codex.1

$ErrorActionPreference = "Stop"
$env:AGENTIC_CONTEXT_RESOLVED_PYTHON = $null
$env:AGENTIC_CONTEXT_RESOLVED_PYTHON_ARGS = $null

function Test-SupportedPython([string]$Executable, [string[]]$Arguments = @()) {
    try {
        $version = & $Executable @Arguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        return $LASTEXITCODE -eq 0 -and ([version]$version -ge [version]'3.13')
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
    throw "AGENTIC_CONTEXT_PYTHON must point to Python 3.13 or newer."
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
