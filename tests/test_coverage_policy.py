"""Behavioral checks for critical coverage policy enforcement.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.check_coverage import check_coverage


@pytest.fixture
def policy():
    return {"groups": [{
        "name": "Critical storage",
        "paths": ["app/core/storage.py"],
        "minimum_line_percent": 70,
        "minimum_branch_percent": 50,
    }]}


@pytest.fixture
def report():
    return {
        "meta": {"branch_coverage": True},
        "files": {"app/core/storage.py": {"summary": {
            "num_statements": 10, "covered_lines": 7,
            "num_branches": 10, "covered_branches": 5,
        }}},
    }


def test_exact_thresholds_pass_without_rounding_up(report, policy):
    measurements, violations = check_coverage(report, policy)
    assert violations == []
    assert "statements 70.00%" in measurements[0]
    assert "branches 50.00%" in measurements[0]


@pytest.mark.parametrize("field,value", [("covered_lines", 6), ("covered_branches", 4)])
def test_each_coverage_floor_can_independently_fail(report, policy, field, value):
    report["files"]["app/core/storage.py"]["summary"][field] = value
    _, violations = check_coverage(report, policy)
    assert violations == ["Critical coverage floor not met: Critical storage"]


def test_missing_required_path_fails_even_with_another_fully_covered_module(report, policy):
    report["files"]["app/core/unrelated.py"] = report["files"].pop("app/core/storage.py")
    with pytest.raises(ValueError, match="Required coverage measurement is missing"):
        check_coverage(report, policy)


@pytest.mark.parametrize("metadata", [None, {}, {"branch_coverage": False}])
def test_missing_branch_metadata_cannot_pass(report, policy, metadata):
    report["meta"] = metadata
    with pytest.raises(ValueError, match="branch coverage metadata"):
        check_coverage(report, policy)


@pytest.mark.parametrize("field", ["num_statements", "num_branches"])
def test_empty_denominators_cannot_pass(report, policy, field):
    summary = report["files"]["app/core/storage.py"]["summary"]
    summary[field] = 0
    summary["covered_lines" if field == "num_statements" else "covered_branches"] = 0
    with pytest.raises(ValueError, match="no executable statements|no measured branches"):
        check_coverage(report, policy)


def test_group_coverage_uses_weighted_counts_and_normalizes_windows_paths(report, policy):
    policy["groups"][0]["paths"].append("app/core/large.py")
    report["files"]["app\\core\\large.py"] = {"summary": {
        "num_statements": 90, "covered_lines": 90,
        "num_branches": 90, "covered_branches": 90,
    }}
    measurements, violations = check_coverage(report, policy)
    assert violations == []
    assert "statements 97.00%" in measurements[0]
    assert "branches 95.00%" in measurements[0]


@pytest.mark.parametrize("change", [
    lambda r, p: r["files"]["app/core/storage.py"]["summary"].pop("covered_branches"),
    lambda r, p: r["files"]["app/core/storage.py"]["summary"].update(covered_lines=11),
    lambda r, p: p.update(groups=[]),
    lambda r, p: p["groups"][0].update(minimum_branch_percent=0),
    lambda r, p: p["groups"][0]["paths"].append("app/core/storage.py"),
    lambda r, p: p["groups"][0].update(paths=["../outside.py"]),
])
def test_incomplete_or_invalid_policy_inputs_fail_closed(report, policy, change):
    change(report, policy)
    with pytest.raises(ValueError):
        check_coverage(report, policy)


def test_cli_returns_failure_for_low_coverage_and_invalid_json(tmp_path, report, policy):
    report_path = tmp_path / "coverage.json"
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    low_report = deepcopy(report)
    low_report["files"]["app/core/storage.py"]["summary"]["covered_lines"] = 6
    checker = Path(__file__).resolve().parents[1] / "scripts/check_coverage.py"
    for contents, expected in (
        (json.dumps(report), ""),
        (json.dumps(low_report), "Critical coverage floor not met"),
        ("{broken", "Critical coverage validation failed"),
    ):
        report_path.write_text(contents, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(checker), str(report_path), "--policy", str(policy_path)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == int(bool(expected)), result.stdout + result.stderr
        assert expected in result.stderr
