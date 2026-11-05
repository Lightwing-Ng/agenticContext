"""Partition offline tests and verify complete, fresh branch-coverage artifacts.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import argparse
import ast
import configparser
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


SHARD_COUNT = 8
# Browser files each start disposable browser/server fixtures. CI showed that
# balancing only source bytes concentrated 28 such files in one timed-out shard.
# This fixed planning allowance represents setup work, not bytes read or runtime.
BROWSER_SETUP_BYTES = 64 * 1024
SHARD_ENV = "AGENTIC_CONTEXT_TEST_SHARD"
COUNT_ENV = "AGENTIC_CONTEXT_TEST_SHARD_COUNT"
CONTEXT_KEYS = ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_OS")
COVERAGE_CONFIG = "[run]\nbranch = True\nsource = app\nrelative_files = True\n"


@dataclass(frozen=True)
class Shard:
    index: int
    count: int = SHARD_COUNT


def shard_from_environment(environment: dict[str, str]) -> Shard | None:
    """Require the complete, explicit eight-shard contract before lowering coverage."""
    if SHARD_ENV not in environment and COUNT_ENV not in environment:
        return None
    index, count = environment.get(SHARD_ENV, ""), environment.get(COUNT_ENV, "")
    if not index.isdecimal() or count != str(SHARD_COUNT):
        raise ValueError("Set both test shard variables: index 1 through 8 and count 8.")
    if not 1 <= int(index) <= SHARD_COUNT:
        raise ValueError("The test shard index must be between 1 and 8.")
    return Shard(int(index))


def run_context(environment: dict[str, str]) -> dict[str, str]:
    context = {key: environment.get(key, "") for key in CONTEXT_KEYS}
    if not all(context.values()):
        raise ValueError("Sharded coverage requires commit, run, attempt, and platform identities.")
    return context


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_inventory(root: Path) -> list[dict[str, object]]:
    """Read names and bytes without importing tests or depending on collection success."""
    configuration = configparser.ConfigParser()
    configuration.read(root / "pytest.ini")
    if (
        configuration.get("pytest", "testpaths", fallback="").split() != ["tests"]
        or configuration.get("pytest", "python_files", fallback="").split()
        != ["test_*.py"]
    ):
        raise ValueError("Update shard discovery for the changed pytest file-discovery contract.")
    # Explicit file arguments bypass recursive discovery, so retain its collision-copy exclusion.
    conftest = ast.parse((root / "tests/conftest.py").read_text(encoding="utf-8"))
    ignores: list[str] = []
    for node in conftest.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {item.id for item in node.targets if isinstance(item, ast.Name)}
        if "collect_ignore" in names:
            raise ValueError("Update shard discovery before adding explicit collection exclusions.")
        if "collect_ignore_glob" in names:
            ignores = ast.literal_eval(node.value)
            if not isinstance(ignores, list) or any(not isinstance(item, str) for item in ignores):
                raise ValueError("Collection exclusions must be a literal list of patterns.")
    inventory = []
    for path in sorted((root / "tests").rglob("test_*.py")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or any(part.startswith(".") for part in path.relative_to(root).parts):
            raise ValueError("Test discovery must not follow symbolic links or hidden directories.")
        if any(path.match(pattern) for pattern in ignores):
            continue
        inventory.append({"path": relative, "bytes": path.stat().st_size, "sha256": _digest(path)})
    if not inventory:
        raise ValueError("The offline test inventory is empty.")
    return inventory


def partition(inventory: list[dict[str, object]]) -> list[list[str]]:
    """Balance browser setup and source work separately from other test files."""
    shards: list[list[str]] = [[] for _ in range(SHARD_COUNT)]
    for browser in (True, False):
        weights = [0] * SHARD_COUNT
        group = [item for item in inventory if ("_e2e" in str(item["path"])) == browser]
        for item in sorted(group, key=lambda row: (-int(row["bytes"]), str(row["path"]))):
            target = min(range(SHARD_COUNT), key=lambda index: (weights[index], len(shards[index]), index))
            shards[target].append(str(item["path"]))
            weights[target] += int(item["bytes"]) + (BROWSER_SETUP_BYTES if browser else 0)
    for files in shards:
        files.sort()
    selected = [path for files in shards for path in files]
    if len(selected) != len(set(selected)) or set(selected) != {item["path"] for item in inventory}:
        raise ValueError("The shard partition must cover every discovered test file exactly once.")
    if any(not files for files in shards):
        raise ValueError("Each of the eight quality shards must contain tests.")
    return shards


def _validate_pytest_arguments(arguments: list[str]) -> str:
    """Reject a narrowed run that could otherwise publish an apparently complete receipt."""
    remaining = list(arguments)
    marker = ""
    flags = {"-q", "--cov=app", "--cov-branch", "--cov-report=term-missing", "--cov-report=json:test-results/coverage.json", "--cov-fail-under=0"}
    while remaining:
        argument = remaining.pop(0)
        if argument in flags:
            continue
        if argument == "-p" and remaining and remaining.pop(0) == "no:cacheprovider":
            continue
        if argument == "-m" and remaining and not marker:
            marker = remaining.pop(0)
            continue
        raise ValueError("Shard runs accept only the ordinary quality-gate pytest arguments.")
    required = flags - {"-q"}
    if not required <= set(arguments) or marker != "not live":
        raise ValueError("Quality shards require branch coverage and the unchanged 'not live' marker.")
    if os.environ.get("PYTEST_ADDOPTS", "").strip():
        raise ValueError("PYTEST_ADDOPTS must not narrow a quality shard.")
    return marker


def run_shard(root: Path, arguments: list[str]) -> int:
    shard = shard_from_environment(dict(os.environ))
    if shard is None:
        raise ValueError("The shard runner requires an explicit shard index and count.")
    context = run_context(dict(os.environ))
    marker = _validate_pytest_arguments(arguments)
    inventory = test_inventory(root)
    selected = partition(inventory)[shard.index - 1]
    bundle = root / "test-results" / f"quality-shard-{shard.index}"
    if bundle.exists():
        raise ValueError("Use a fresh checkout/output directory for each quality-shard attempt.")
    bundle.mkdir(parents=True)
    configuration = bundle / "coverage.ini"
    configuration.write_text(COVERAGE_CONFIG, encoding="utf-8")
    coverage_file = bundle / "coverage.data"
    report = root / "test-results/coverage.json"
    started = time.time_ns()
    environment = {**os.environ, "COVERAGE_FILE": str(coverage_file), "PYTHONDONTWRITEBYTECODE": "1"}
    # Nested launcher contract tests must exercise their default full-run behavior.
    environment.pop(SHARD_ENV, None)
    environment.pop(COUNT_ENV, None)
    print(f"Quality shard {shard.index}/{shard.count}: {len(selected)} test files", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *arguments, f"--cov-config={configuration}", *selected],
        cwd=root,
        env=environment,
        check=False,
    )
    if result.returncode:
        return result.returncode
    if not coverage_file.is_file() or not report.is_file() or report.stat().st_mtime_ns < started:
        raise ValueError("The successful shard did not produce fresh coverage data and JSON.")
    if test_inventory(root) != inventory:
        raise ValueError("Test files changed while their shard was running.")
    shutil.copyfile(report, bundle / "coverage.json")
    manifest = {
        "version": 1,
        "status": "passed",
        "context": context,
        "shard_index": shard.index,
        "shard_count": shard.count,
        "marker": marker,
        "inventory_sha256": hashlib.sha256(_encoded(inventory)).hexdigest(),
        "selected_files": selected,
        "coverage_sha256": _digest(coverage_file),
        "report_sha256": _digest(bundle / "coverage.json"),
        "started_at_ns": started,
        "finished_at_ns": time.time_ns(),
    }
    (bundle / "manifest.json").write_bytes(_encoded(manifest))
    return 0


def verified_coverage_files(root: Path, artifacts: Path, context: dict[str, str]) -> list[Path]:
    """Reject missing, duplicated, altered, or foreign artifacts before combining data."""
    from coverage import CoverageData

    inventory = test_inventory(root)
    shards = partition(inventory)
    fingerprint = hashlib.sha256(_encoded(inventory)).hexdigest()
    bundles = list(artifacts.iterdir()) if artifacts.is_dir() else []
    if len(bundles) != SHARD_COUNT or any(path.is_symlink() or not path.is_dir() for path in bundles):
        raise ValueError("Exactly eight successful quality-shard artifact directories are required.")
    files: dict[int, Path] = {}
    for bundle in bundles:
        manifest_file = bundle / "manifest.json"
        if manifest_file.is_symlink() or not manifest_file.is_file():
            raise ValueError("Every shard artifact requires a regular success receipt.")
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        index = manifest.get("shard_index")
        if type(index) is not int or not 1 <= index <= SHARD_COUNT or index in files:
            raise ValueError("Shard artifacts contain an invalid or duplicate index.")
        if (
            manifest.get("version") != 1
            or manifest.get("status") != "passed"
            or manifest.get("context") != context
            or manifest.get("shard_count") != SHARD_COUNT
            or manifest.get("marker") != "not live"
            or manifest.get("inventory_sha256") != fingerprint
            or manifest.get("selected_files") != shards[index - 1]
            or type(manifest.get("started_at_ns")) is not int
            or type(manifest.get("finished_at_ns")) is not int
            or manifest["finished_at_ns"] < manifest["started_at_ns"]
        ):
            raise ValueError("A shard receipt is stale, unsuccessful, or incomplete for this run.")
        data_file, report_file = bundle / "coverage.data", bundle / "coverage.json"
        if any(path.is_symlink() or not path.is_file() for path in (data_file, report_file)):
            raise ValueError("Coverage artifacts must contain regular data and report files.")
        if _digest(data_file) != manifest.get("coverage_sha256") or _digest(report_file) != manifest.get("report_sha256"):
            raise ValueError("Coverage artifact content no longer matches its success receipt.")
        report = json.loads(report_file.read_text(encoding="utf-8"))
        data = CoverageData(basename=str(data_file))
        data.read()
        if not report.get("meta", {}).get("branch_coverage") or not data.has_arcs() or not data.measured_files():
            raise ValueError("Each shard must supply nonempty branch-coverage data.")
        if any(
            not name.replace("\\", "/").startswith("app/")
            or ".." in name.replace("\\", "/").split("/")
            for name in data.measured_files()
        ):
            raise ValueError("Coverage data must use relative application source paths.")
        files[index] = data_file
    return [files[index] for index in range(1, SHARD_COUNT + 1)]


def aggregate(root: Path, artifacts: Path, minimum: int, shard_status: str) -> int:
    from coverage import Coverage

    if shard_status != "success":
        raise ValueError("Every quality-shard job must succeed before coverage aggregation.")
    context = run_context(dict(os.environ))
    files = verified_coverage_files(root, artifacts, context)
    output = root / "test-results"
    output.mkdir(exist_ok=True)
    configuration = output / "coverage-aggregate.ini"
    configuration.write_text(COVERAGE_CONFIG, encoding="utf-8")
    data_file = output / ".coverage"
    coverage = Coverage(data_file=str(data_file), config_file=str(configuration))
    coverage.combine(data_paths=[str(path) for path in files], strict=True, keep=True)
    coverage.save()
    coverage.json_report(outfile=str(output / "coverage.json"))
    return subprocess.run(
        [sys.executable, "-m", "coverage", "report", f"--rcfile={configuration}", f"--data-file={data_file}", f"--fail-under={minimum}"],
        cwd=root,
        check=False,
    ).returncode


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    minimum = commands.add_parser("minimum")
    minimum.add_argument("value", type=int, choices=range(101))
    run = commands.add_parser("run")
    run.add_argument("pytest_arguments", nargs=argparse.REMAINDER)
    combine = commands.add_parser("aggregate")
    combine.add_argument("--artifacts", type=Path, required=True)
    combine.add_argument("--minimum", type=int, choices=range(101), default=55)
    combine.add_argument("--shard-status", required=True)
    options = parser.parse_args(arguments)
    root = Path(__file__).resolve().parents[1]
    try:
        if options.command == "minimum":
            print(0 if shard_from_environment(dict(os.environ)) else options.value)
            return 0
        if options.command == "run":
            arguments = options.pytest_arguments
            if arguments[:1] == ["--"]:
                arguments = arguments[1:]
            return run_shard(root, arguments)
        return aggregate(root, options.artifacts, options.minimum, options.shard_status)
    except (OSError, ValueError, KeyError) as error:
        print(f"Quality shard validation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
