"""Verify immediate failure output and unchanged pytest exit codes.

Code version: v1.0.1-codex.1
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHASE_SUITE = '''"""Disposable diagnostic cases. Code version: v1.0.0-codex.1."""
import os
from pathlib import Path
import pytest

@pytest.fixture
def broken_setup():
    raise RuntimeError("SETUP_DIAGNOSTIC")

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("TEARDOWN_DIAGNOSTIC")

def test_setup(broken_setup):
    pass

def test_call():
    assert False, "CALL_DIAGNOSTIC"

def test_teardown(broken_teardown):
    pass

def test_reports_have_already_reached_the_output_file():
    text = Path(os.environ["REPORT_OUTPUT_FILE"]).read_text(encoding="utf-8")
    enabled = os.environ["AGENTIC_CONTEXT_TEST_REPORT_FAILURES"] == "1"
    for phase in ("setup", "call", "teardown"):
        assert (f"phase={phase}" in text) is enabled
        if enabled:
            assert f"test_cases.py::test_{phase}" in text
            assert f"{phase.upper()}_DIAGNOSTIC" in text
    Path(os.environ["REPORT_PROOF_FILE"]).write_text("observed before session finish", encoding="utf-8")
'''


def run_disposable_suite(tmp_path: Path, source: str, enabled: bool) -> tuple[int, str]:
    suite = tmp_path / "test_cases.py"
    output = tmp_path / "pytest-output.log"
    suite.write_text(source, encoding="utf-8")
    configuration = tmp_path / "pytest.ini"
    configuration.write_text("[pytest]\naddopts =\n", encoding="utf-8")
    environment = {
        **os.environ,
        "AGENTIC_CONTEXT_TEST_REPORT_FAILURES": "1" if enabled else "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "REPORT_OUTPUT_FILE": str(output),
        "REPORT_PROOF_FILE": str(tmp_path / "flush-proof.txt"),
    }
    environment.pop("PYTEST_ADDOPTS", None)
    with output.open("w", encoding="utf-8") as stream:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=short", "--color=no",
             "-c", str(configuration), "--rootdir", str(tmp_path),
             "-p", "no:cacheprovider", "-p", "tests.conftest", str(suite)],
            cwd=PROJECT_ROOT, env=environment,
            stdout=stream, stderr=subprocess.STDOUT, check=False, timeout=30,
        )
    return result.returncode, output.read_text(encoding="utf-8")


@pytest.mark.parametrize("enabled", [True, False])
def test_phase_reports_are_flushed_before_finish_without_changing_failure_exit(tmp_path, enabled):
    returncode, output = run_disposable_suite(tmp_path, PHASE_SUITE, enabled)
    assert returncode == pytest.ExitCode.TESTS_FAILED, output
    assert (tmp_path / "flush-proof.txt").read_text(encoding="utf-8") == "observed before session finish"
    reports = re.findall(r"^\[pytest-failure\] nodeid=(.+) phase=(setup|call|teardown)$", output, re.MULTILINE)
    if enabled:
        assert [(node.rsplit("::", 1)[-1], phase) for node, phase in reports] == [
            ("test_setup", "setup"), ("test_call", "call"), ("test_teardown", "teardown"),
        ]
        assert output.index("phase=teardown") < output.index("short test summary info")
    else:
        assert reports == []


def test_collection_failure_is_reported_before_interruption_summary(tmp_path):
    returncode, output = run_disposable_suite(
        tmp_path, '# Code version: v1.0.0-codex.1\nraise RuntimeError("COLLECTION_DIAGNOSTIC")\n', True,
    )
    assert returncode == pytest.ExitCode.INTERRUPTED, output
    assert re.search(r"^\[pytest-failure\] nodeid=.*test_cases.py phase=collect$", output, re.MULTILINE)
    assert "COLLECTION_DIAGNOSTIC" in output
    assert output.index("phase=collect") < output.index("short test summary info")
