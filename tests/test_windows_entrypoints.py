"""Native Windows launcher and fail-closed gate tests. Code version: v1.2.0-codex.1."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Exercises the native Windows py launcher and PowerShell."
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _powershell() -> str:
    """Resolve PowerShell 7 first, then the Windows 5.1 host."""
    for name in ("pwsh", "powershell"):
        executable = shutil.which(name)
        if executable:
            return executable
    pytest.skip("Neither pwsh nor powershell is available on PATH.")


def _windows_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "AGENTIC_CONTEXT_PYTHON", "CACHELIKES_PYTHON",
        "AGENTIC_CONTEXT_RESOLVED_PYTHON", "AGENTIC_CONTEXT_RESOLVED_PYTHON_ARGS",
        "AGENTIC_CONTEXT_TEST_MARK_EXPRESSION", "CACHELIKES_TEST_MARK_EXPRESSION",
    ):
        environment.pop(name, None)
    return environment


@pytest.mark.parametrize("passes", [True, False])
def test_windows_test_entrypoint_executes_pytest_and_preserves_exit_code(tmp_path, passes):
    """A launcher returning zero without running the requested module must not pass."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(f"def test_probe():\n    assert {passes}\n", encoding="utf-8")
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(PROJECT_ROOT / "scripts/test.ps1"),
            str(probe),
        ],
        env=_windows_environment(),
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == (0 if passes else 1), result.stdout + result.stderr
    assert ("1 passed" if passes else "1 failed") in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize("stale_report", [False, True])
def test_windows_gate_rejects_success_without_fresh_coverage(tmp_path, stale_report):
    """Reject both absent coverage and a report left over from a previous run."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("check.ps1", "resolve_python.ps1"):
        shutil.copyfile(PROJECT_ROOT / "scripts" / name, scripts / name)
    static = tmp_path / "app/web/static"
    static.mkdir(parents=True)
    (static / "probe.js").write_text("void 0;\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in (
        "test_agent_optimization.mjs",
        "test_beta_engines.mjs",
        "test_select_controller.mjs",
    ):
        (tests / name).write_text(
            "import test from 'node:test';\ntest('probe', () => {});\n",
            encoding="utf-8",
        )
    fake_python = tmp_path / "fake-python.cmd"
    fake_python.write_text('@echo off\nif "%~1"=="-c" echo 3.13\nexit /b 0\n', encoding="utf-8")
    if stale_report:
        report = tmp_path / "test-results/coverage.json"
        report.parent.mkdir()
        report.write_text("{}", encoding="utf-8")
        os.utime(report, (1, 1))
    environment = _windows_environment()
    environment["AGENTIC_CONTEXT_PYTHON"] = str(fake_python)
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-File", str(scripts / "check.ps1")],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Pytest did not produce a fresh coverage report." in result.stdout + result.stderr
    assert "Quality gate passed." not in result.stdout


@pytest.mark.parametrize("marker,expected", [(None, "offline"), ("live", "live")])
def test_windows_test_selection_and_stale_launcher_arguments(tmp_path, marker, expected):
    """Direct interpreters must discard launcher arguments and honor safe marker selection."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("test.ps1", "resolve_python.ps1"):
        shutil.copy2(PROJECT_ROOT / "scripts" / name, scripts / name)
    shutil.copy2(PROJECT_ROOT / "pytest.ini", tmp_path / "pytest.ini")
    (tmp_path / "test_probe.py").write_text(
        "import pytest\nfrom pathlib import Path\n"
        "def test_offline():\n    Path('selected').write_text('offline')\n"
        "@pytest.mark.live\ndef test_live():\n    Path('selected').write_text('live')\n",
        encoding="utf-8",
    )
    environment = _windows_environment()
    environment["AGENTIC_CONTEXT_PYTHON"] = sys.executable
    environment["AGENTIC_CONTEXT_RESOLVED_PYTHON_ARGS"] = "-invalid-stale-argument"
    if marker:
        environment["AGENTIC_CONTEXT_TEST_MARK_EXPRESSION"] = marker
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(scripts / "test.ps1"),
            "test_probe.py",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 deselected" in result.stdout
    assert (tmp_path / "selected").read_text() == expected


@pytest.mark.parametrize("stage", ["upgrade", "requirements", "playwright"])
def test_windows_setup_stops_at_the_first_failed_install(tmp_path, stage):
    """Never report a ready environment after any native dependency command fails."""
    interpreter = tmp_path / "install-probe.cmd"
    condition = {
        "upgrade": 'if "%~4"=="--upgrade" exit /b 23',
        "requirements": 'if "%~4"=="-r" exit /b 23',
        "playwright": 'if "%~2"=="playwright" exit /b 23',
    }[stage]
    interpreter.write_text(
        '@echo off\nif "%~1"=="-c" (\n echo 3.13\n exit /b 0\n)\n'
        + condition + "\nexit /b 0\n", encoding="utf-8",
    )
    environment = _windows_environment()
    environment["AGENTIC_CONTEXT_PYTHON"] = str(interpreter)
    environment["AGENTIC_CONTEXT_SKIP_PLAYWRIGHT_INSTALL"] = "0"
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(PROJECT_ROOT / "scripts/setup_python.ps1"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 23, result.stdout + result.stderr
    assert "Environment is ready." not in result.stdout


@pytest.mark.parametrize("version,accepted", [
    ("3.12", False), ("3.13", True), ("3.14", True), ("3.15", True), ("4.0", True),
    ("invalid", False),
])
def test_windows_resolver_minimum_version(tmp_path, version, accepted):
    interpreter = tmp_path / "version-probe.cmd"
    interpreter.write_text(f"@echo off\necho {version}\nexit /b 0\n", encoding="utf-8")
    environment = _windows_environment()
    environment["AGENTIC_CONTEXT_PYTHON"] = str(interpreter)
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(PROJECT_ROOT / "scripts/resolve_python.ps1"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
    )
    assert (result.returncode == 0) == accepted, result.stdout + result.stderr
