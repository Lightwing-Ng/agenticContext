"""Native Windows launcher and fail-closed gate tests. Code version: v1.0.0-codex.1."""

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
        ["pwsh", "-NoProfile", "-File", str(PROJECT_ROOT / "scripts/test.ps1"), str(probe), "-q"],
        env=_windows_environment(), capture_output=True, text=True, timeout=60,
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
    (tests / "test_agent_optimization.mjs").write_text(
        "import test from 'node:test';\ntest('probe', () => {});\n", encoding="utf-8"
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
        ["pwsh", "-NoProfile", "-File", str(scripts / "check.ps1")],
        env=environment, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Pytest did not produce a fresh coverage report." in result.stdout + result.stderr
    assert "Quality gate passed." not in result.stdout
