"""Behavioral checks for the POSIX test and quality entrypoints.

Code version: v1.1.0-codex.1
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(os.name == "nt", reason="Exercises POSIX executable scripts.")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _environment():
    environment = os.environ.copy()
    for key in (
        "AGENTIC_CONTEXT_PYTHON", "CACHELIKES_PYTHON", "PYTEST_ADDOPTS",
        "AGENTIC_CONTEXT_TEST_MARK_EXPRESSION", "CACHELIKES_TEST_MARK_EXPRESSION",
    ):
        environment.pop(key, None)
    environment["AGENTIC_CONTEXT_PYTHON"] = sys.executable
    return environment


@pytest.mark.parametrize("version,accepted", [
    ((3, 12), False), ((3, 13), True), ((3, 14), True), ((3, 15), True), ((4, 0), True),
])
def test_resolver_enforces_only_the_minimum(tmp_path, version, accepted):
    """Exercise the actual version predicate with present and future version tuples."""
    interpreter = tmp_path / "python probe"
    interpreter.write_text(
        f"#!{sys.executable}\nimport sys\nsys.version_info = {version!r}\nexec(sys.argv[2])\n"
    )
    interpreter.chmod(0o755)
    environment = _environment()
    environment["AGENTIC_CONTEXT_PYTHON"] = str(interpreter)
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; resolve_python_bin', "resolver",
         str(PROJECT_ROOT / "scripts/resolve_python.sh")],
        env=environment, capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode == 0) == accepted, result.stderr
    assert result.stdout.strip() == (str(interpreter) if accepted else "")


@pytest.mark.parametrize("marker,expected", [(None, "offline"), ("live", "live")])
def test_test_entrypoint_selects_safe_defaults_and_explicit_markers(tmp_path, marker, expected):
    """Selection must happen before a live test can execute."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("test.sh", "resolve_python.sh"):
        shutil.copy2(PROJECT_ROOT / "scripts" / name, scripts / name)
    shutil.copy2(PROJECT_ROOT / "pytest.ini", tmp_path / "pytest.ini")
    (tmp_path / "test_probe.py").write_text(
        "import pytest\nfrom pathlib import Path\n"
        "def test_offline():\n    Path('selected').write_text('offline')\n"
        "@pytest.mark.live\ndef test_live():\n    Path('selected').write_text('live')\n"
    )
    environment = _environment()
    if marker:
        environment["AGENTIC_CONTEXT_TEST_MARK_EXPRESSION"] = marker
    result = subprocess.run(
        [str(scripts / "test.sh"), "test_probe.py"], env=environment,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 deselected" in result.stdout
    assert (tmp_path / "selected").read_text() == expected


@pytest.mark.parametrize("stale_report", [False, True])
def test_quality_gate_rejects_noop_python(tmp_path, stale_report):
    """A zero exit code without current pytest coverage must never pass the gate."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("check.sh", "test.sh", "resolve_python.sh"):
        shutil.copy2(PROJECT_ROOT / "scripts" / name, scripts / name)
    static = tmp_path / "app/web/static"
    static.mkdir(parents=True)
    (static / "probe.js").write_text("void 0;\n")
    interpreter = tmp_path / "noop-python"
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o755)
    if stale_report:
        report = tmp_path / "test-results/coverage.json"
        report.parent.mkdir()
        report.write_text("{}")
        os.utime(report, (1, 1))
    environment = _environment()
    environment["AGENTIC_CONTEXT_PYTHON"] = str(interpreter)
    environment["PATH"] = str(tmp_path) + os.pathsep + environment["PATH"]
    result = subprocess.run(
        [str(scripts / "check.sh")], env=environment,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "Pytest did not produce a fresh coverage report." in result.stdout + result.stderr
    assert "Quality gate passed." not in result.stdout


def test_runtime_resolver_skips_unprepared_host_but_respects_override(tmp_path):
    """Dependency readiness must govern startup without overriding an explicit choice."""
    missing = tmp_path / "python3"
    ready = tmp_path / "python"
    for path, available in ((missing, False), (ready, True)):
        path.write_text(
            f"#!{sys.executable}\nimport sys\n"
            f"if 'import flask' in sys.argv[2]: raise SystemExit({0 if available else 1})\n"
            "exec(sys.argv[2])\n"
        )
        path.chmod(0o755)
    environment = _environment()
    environment.pop("AGENTIC_CONTEXT_PYTHON")
    environment["PATH"] = str(tmp_path) + os.pathsep + environment["PATH"]
    command = ["bash", "-c", 'source "$1"; resolve_python_bin runtime', "resolver",
               str(PROJECT_ROOT / "scripts/resolve_python.sh")]
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout.strip() == str(ready)
    environment["AGENTIC_CONTEXT_PYTHON"] = str(missing)
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert result.stdout == ""


def test_app_entrypoint_checks_runtime_dependencies_before_main(tmp_path):
    """An unprepared explicit interpreter must produce guidance instead of launching main."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("run_app.sh", "resolve_python.sh"):
        shutil.copy2(PROJECT_ROOT / "scripts" / name, scripts / name)
    interpreter = tmp_path / "python-probe"
    marker = tmp_path / "launched"
    interpreter.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        f"if sys.argv[1] != '-c': Path({str(marker)!r}).touch(); raise SystemExit(0)\n"
        "if 'import flask' in sys.argv[2]: raise SystemExit(1)\nexec(sys.argv[2])\n"
    )
    interpreter.chmod(0o755)
    environment = _environment()
    environment["AGENTIC_CONTEXT_PYTHON"] = str(interpreter)
    result = subprocess.run([str(scripts / "run_app.sh")], env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "requirements.txt" in result.stderr
    assert not marker.exists()
