"""Prove complete test partitions and fail-closed branch-coverage aggregation.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from coverage import CoverageData
import pytest

from scripts import quality_shards as shards


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = {"GITHUB_SHA": "synthetic-commit", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "RUNNER_OS": "Linux"}
ARGUMENTS = ["-q", "-p", "no:cacheprovider", "-m", "not live", "--cov=app", "--cov-branch", "--cov-report=term-missing", "--cov-report=json:test-results/coverage.json", "--cov-fail-under=0"]
MODULE_ARCS = {(-1, 1), (1, -1)}
TRUE_ARCS = MODULE_ARCS | {(-1, 2), (2, 3), (3, -1)}
FALSE_ARCS = MODULE_ARCS | {(-1, 2), (2, 4), (4, -1)}


@pytest.fixture
def project(tmp_path):
    tests, app = tmp_path / "tests", tmp_path / "app"
    tests.mkdir()
    app.mkdir()
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "probe.py").write_text("def choose(flag):\n    if flag:\n        return 1\n    return 0\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests\npython_files = test_*.py\n", encoding="utf-8")
    (tests / "conftest.py").write_text('collect_ignore_glob = ["* 2.py"]\n', encoding="utf-8")
    for category in ("e2e", "unit"):
        for index in range(shards.SHARD_COUNT):
            (tests / f"test_{category}_{index}.py").write_text(
                "from app.probe import choose\ndef test_probe():\n    assert choose(True) == 1\n",
                encoding="utf-8",
            )
    return tmp_path


def _write_manifest(bundle, manifest):
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _bundles(project, arcs=None):
    inventory = shards.test_inventory(project)
    partition = shards.partition(inventory)
    artifacts = project / "downloaded"
    artifacts.mkdir()
    for index in range(1, shards.SHARD_COUNT + 1):
        bundle = artifacts / f"artifact-{index}"
        bundle.mkdir()
        data = CoverageData(basename=str(bundle / "coverage.data"))
        data.add_arcs({"app/probe.py": arcs if arcs is not None else TRUE_ARCS if index == 1 else FALSE_ARCS})
        data.write()
        (bundle / "coverage.json").write_text(json.dumps({"meta": {"branch_coverage": True}}), encoding="utf-8")
        manifest = {
            "version": 1, "status": "passed", "context": CONTEXT,
            "shard_index": index, "shard_count": 8, "marker": "not live",
            "inventory_sha256": hashlib.sha256(shards._encoded(inventory)).hexdigest(),
            "selected_files": partition[index - 1],
            "coverage_sha256": shards._digest(bundle / "coverage.data"),
            "report_sha256": shards._digest(bundle / "coverage.json"),
            "started_at_ns": 1, "finished_at_ns": 2,
        }
        _write_manifest(bundle, manifest)
    return artifacts


def test_partition_covers_real_and_synthetic_inventory_exactly_once(project):
    assert shards.SHARD_COUNT == 8
    for root in (project, ROOT):
        inventory = shards.test_inventory(root)
        partition = shards.partition(inventory)
        assert partition == shards.partition(list(reversed(inventory)))
        flattened = [path for files in partition for path in files]
        assert len(flattened) == len(set(flattened)) == len(inventory)
        assert set(flattened) == {item["path"] for item in inventory}
    assert all(any("_e2e" in path for path in group) for group in shards.partition(shards.test_inventory(project)))
    assert all(any("_unit" in path for path in group) for group in shards.partition(shards.test_inventory(project)))


def test_browser_setup_allowance_balances_one_large_module_and_many_small_modules():
    """Repeated small browser modules must not disappear behind one large source file."""
    inventory = [
        {"path": "tests/test_large_e2e.py", "bytes": 512 * 1024},
        *({"path": f"tests/test_small_{index:02d}_e2e.py", "bytes": 4096} for index in range(64)),
    ]
    partition = shards.partition(inventory)
    sizes = {item["path"]: item["bytes"] for item in inventory}
    costs = [sum(sizes[path] + shards.BROWSER_SETUP_BYTES for path in group) for group in partition]
    assert min(map(len, partition)) >= 2
    assert max(map(len, partition)) <= 9
    assert max(costs) - min(costs) <= shards.BROWSER_SETUP_BYTES + 4096
    assert set(path for group in partition for path in group) == set(sizes)


def test_an_incomplete_inventory_cannot_create_empty_quality_shards():
    inventory = [{"path": f"tests/test_unit_{index}.py", "bytes": 1} for index in range(7)]
    with pytest.raises(ValueError, match="eight quality shards"):
        shards.partition(inventory)


def test_discovery_preserves_collision_exclusion_and_rejects_configuration_drift(project):
    (project / "tests/test_probe 2.py").write_text("raise AssertionError('Ignored collision copy')\n", encoding="utf-8")
    assert len(shards.test_inventory(project)) == 16
    (project / "pytest.ini").write_text("[pytest]\ntestpaths = elsewhere\npython_files = *.py\n", encoding="utf-8")
    with pytest.raises(ValueError, match="discovery contract"):
        shards.test_inventory(project)


@pytest.mark.parametrize("environment", [
    {shards.SHARD_ENV: "1"}, {shards.COUNT_ENV: "8"},
    {shards.SHARD_ENV: "", shards.COUNT_ENV: "8"},
    {shards.SHARD_ENV: "0", shards.COUNT_ENV: "8"},
    {shards.SHARD_ENV: "9", shards.COUNT_ENV: "8"},
    {shards.SHARD_ENV: "1", shards.COUNT_ENV: "4"},
])
def test_partial_or_invalid_shard_settings_fail(environment):
    with pytest.raises(ValueError):
        shards.shard_from_environment(environment)


@pytest.mark.parametrize("index,expected", [(None, "55"), ("1", "0"), ("8", "0")])
def test_only_explicit_complete_shard_settings_lower_the_default_minimum(index, expected):
    environment = {key: value for key, value in os.environ.items() if key not in (shards.SHARD_ENV, shards.COUNT_ENV)}
    if index is not None:
        environment.update({shards.SHARD_ENV: index, shards.COUNT_ENV: "8"})
    result = subprocess.run([sys.executable, str(ROOT / "scripts/quality_shards.py"), "minimum", "55"], env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("extra", [["-k", "subset"], ["tests/test_probe.py"], ["--deselect=tests/test_probe.py::test_probe"], ["--collect-only"]])
def test_shards_reject_additional_collection_filters(extra, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    with pytest.raises(ValueError, match="ordinary quality-gate"):
        shards._validate_pytest_arguments([*ARGUMENTS, *extra])


@pytest.mark.parametrize("field,value", [
    ("status", "failed"), ("shard_index", 2), ("shard_count", 3),
    ("marker", "integration"), ("selected_files", []),
    ("inventory_sha256", "old-inventory"), ("finished_at_ns", 0),
])
def test_incomplete_or_duplicate_receipts_cannot_pass(project, field, value):
    artifacts = _bundles(project)
    bundle = artifacts / "artifact-1"
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest[field] = value
    _write_manifest(bundle, manifest)
    with pytest.raises(ValueError):
        shards.verified_coverage_files(project, artifacts, CONTEXT)


@pytest.mark.parametrize("key", shards.CONTEXT_KEYS)
def test_artifacts_from_another_commit_run_attempt_or_platform_fail(project, key):
    artifacts = _bundles(project)
    expected = {**CONTEXT, key: "another-context"}
    with pytest.raises(ValueError, match="stale"):
        shards.verified_coverage_files(project, artifacts, expected)


def test_seven_successful_artifacts_cannot_satisfy_the_eight_shard_gate(project):
    artifacts = _bundles(project)
    bundle = artifacts / "artifact-8"
    for path in bundle.iterdir():
        path.unlink()
    bundle.rmdir()
    assert len(list(artifacts.iterdir())) == 7
    with pytest.raises(ValueError, match="Exactly eight successful"):
        shards.verified_coverage_files(project, artifacts, CONTEXT)


@pytest.mark.parametrize("damage", ["extra", "missing_receipt", "changed_data", "changed_report", "line_only", "changed_test"])
def test_artifact_completeness_and_integrity_are_required(project, damage):
    artifacts = _bundles(project)
    bundle = artifacts / "artifact-1"
    if damage == "extra":
        (artifacts / "artifact-9").mkdir()
    elif damage == "missing_receipt":
        (bundle / "manifest.json").unlink()
    elif damage == "changed_data":
        with (bundle / "coverage.data").open("ab") as stream:
            stream.write(b"changed")
    elif damage == "changed_report":
        (bundle / "coverage.json").write_text("{}", encoding="utf-8")
    elif damage == "line_only":
        (bundle / "coverage.data").unlink()
        data = CoverageData(basename=str(bundle / "coverage.data"))
        data.add_lines({"app/probe.py": [1]})
        data.write()
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["coverage_sha256"] = shards._digest(bundle / "coverage.data")
        _write_manifest(bundle, manifest)
    else:
        (project / "tests/test_unit_0.py").write_text("def test_new(): pass\n", encoding="utf-8")
    with pytest.raises((OSError, ValueError)):
        shards.verified_coverage_files(project, artifacts, CONTEXT)


@pytest.mark.parametrize("covered,expected", [(True, 0), (False, 2)])
def test_global_threshold_uses_combined_branch_arcs_not_partial_percentages(project, monkeypatch, covered, expected):
    artifacts = _bundles(project, None if covered else MODULE_ARCS)
    monkeypatch.chdir(project)
    for key, value in CONTEXT.items():
        monkeypatch.setenv(key, value)
    assert shards.aggregate(project, artifacts, 55, "success") == expected
    data = CoverageData(basename=str(project / "test-results/.coverage"))
    data.read()
    measured = data.measured_files()
    assert {name.replace("\\", "/") for name in measured} == {"app/probe.py"}
    assert len(measured) == 1
    assert set(data.arcs(next(iter(measured)))) == (
        TRUE_ARCS | FALSE_ARCS if covered else MODULE_ARCS
    )
    report = json.loads((project / "test-results/coverage.json").read_text())
    assert report["meta"]["branch_coverage"] is True
    assert (report["totals"]["percent_covered"] >= 55) == covered
    with pytest.raises(ValueError, match="Every quality-shard job"):
        shards.aggregate(project, artifacts, 55, "failure")


def test_real_shard_produces_a_success_receipt_only_after_fresh_branch_data(project, monkeypatch):
    for key, value in {**CONTEXT, shards.SHARD_ENV: "1", shards.COUNT_ENV: "8"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    assert shards.run_shard(project, ARGUMENTS) == 0
    bundle = project / "test-results/quality-shard-1"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["status"] == "passed"
    assert manifest["selected_files"] == shards.partition(shards.test_inventory(project))[0]
    assert manifest["coverage_sha256"] == shards._digest(bundle / "coverage.data")
    assert json.loads((bundle / "coverage.json").read_text())["meta"]["branch_coverage"]
    with pytest.raises(ValueError, match="fresh checkout"):
        shards.run_shard(project, ARGUMENTS)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_failed_or_noop_pytest_cannot_publish_a_success_manifest(project, monkeypatch, exit_code):
    for key, value in {**CONTEXT, shards.SHARD_ENV: "1", shards.COUNT_ENV: "8"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setattr(shards.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], exit_code))
    if exit_code:
        assert shards.run_shard(project, ARGUMENTS) == exit_code
    else:
        with pytest.raises(ValueError, match="fresh coverage"):
            shards.run_shard(project, ARGUMENTS)
    assert not (project / "test-results/quality-shard-1/manifest.json").exists()
