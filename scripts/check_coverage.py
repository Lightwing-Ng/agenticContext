"""Enforce independent statement and branch floors for critical application paths.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import sys


def _path(value: object) -> str:
    """Normalize portable report paths without accepting ambiguous aliases."""
    if not isinstance(value, str) or not value:
        raise ValueError("Coverage paths must be nonempty strings.")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or ":" in normalized:
        raise ValueError(f"Coverage path must be repository-relative: {value}")
    return str(path)


def _count(summary: dict, field: str, path: str) -> int:
    value = summary.get(field)
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid {field} for {path}.")
    return value


def _minimum(group: dict, field: str) -> float:
    value = group.get(field)
    if type(value) not in (int, float) or not 0 < value <= 100:
        raise ValueError(f"{field} must be greater than 0 and at most 100.")
    return value


def check_coverage(report: dict, policy: dict) -> tuple[list[str], list[str]]:
    """Return measured groups and violations, rejecting incomplete coverage input."""
    if not isinstance(report, dict) or not isinstance(policy, dict):
        raise ValueError("Coverage report and policy must be JSON objects.")
    metadata = report.get("meta")
    if not isinstance(metadata, dict) or metadata.get("branch_coverage") is not True:
        raise ValueError("Coverage report must include branch coverage metadata.")
    report_files = report.get("files")
    if not isinstance(report_files, dict) or not report_files:
        raise ValueError("Coverage report has no file measurements.")
    files = {}
    for raw_path, measurement in report_files.items():
        path = _path(raw_path)
        if path in files:
            raise ValueError(f"Duplicate normalized coverage path: {path}")
        files[path] = measurement

    groups = policy.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("Coverage policy must require at least one group.")
    measurements, violations = [], []
    names, required_paths = set(), set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Coverage groups must be objects.")
        name = group.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Coverage group names must be nonempty and unique.")
        names.add(name)
        paths = group.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError(f"Coverage group has no required paths: {name}")
        minimum_lines = _minimum(group, "minimum_line_percent")
        minimum_branches = _minimum(group, "minimum_branch_percent")
        totals = dict(num_statements=0, covered_lines=0, num_branches=0, covered_branches=0)
        for raw_path in paths:
            path = _path(raw_path)
            if path in required_paths:
                raise ValueError(f"Duplicate required coverage path: {path}")
            required_paths.add(path)
            measurement = files.get(path)
            if not isinstance(measurement, dict) or not isinstance(measurement.get("summary"), dict):
                raise ValueError(f"Required coverage measurement is missing: {path}")
            summary = measurement["summary"]
            counts = {field: _count(summary, field, path) for field in totals}
            if counts["num_statements"] == 0:
                raise ValueError(f"Required path has no executable statements: {path}")
            if counts["covered_lines"] > counts["num_statements"] or counts["covered_branches"] > counts["num_branches"]:
                raise ValueError(f"Covered counts exceed measured counts: {path}")
            for field, count in counts.items():
                totals[field] += count
        if totals["num_branches"] == 0:
            raise ValueError(f"Required group has no measured branches: {name}")
        line_percent = 100 * totals["covered_lines"] / totals["num_statements"]
        branch_percent = 100 * totals["covered_branches"] / totals["num_branches"]
        measurements.append(
            f"{name}: statements {line_percent:.2f}% (minimum {minimum_lines:g}%), "
            f"branches {branch_percent:.2f}% (minimum {minimum_branches:g}%)."
        )
        if line_percent < minimum_lines or branch_percent < minimum_branches:
            violations.append(f"Critical coverage floor not met: {name}")
    return measurements, violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--policy", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        report = json.loads(arguments.report.read_text(encoding="utf-8"))
        policy = json.loads(arguments.policy.read_text(encoding="utf-8"))
        measurements, violations = check_coverage(report, policy)
    except (OSError, ValueError) as error:
        print(f"Critical coverage validation failed: {error}", file=sys.stderr)
        return 1
    for measurement in measurements:
        print(measurement)
    for violation in violations:
        print(violation, file=sys.stderr)
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main())
