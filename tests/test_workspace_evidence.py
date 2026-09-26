"""Focused content-fingerprint boundaries for optional reference materials.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.core.workspace import evidence


def git(workspace: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text(
        "/forPrompts/\n/docs/forPrompts/\n/tests/fixtures/ignored/\n",
        encoding="utf-8",
    )
    (root / "source.py").write_text("value = 1\n", encoding="utf-8")
    return root


def reference_file(workspace: Path, directory: str) -> Path:
    path = workspace / directory / "reference.txt"
    path.parent.mkdir(parents=True)
    path.write_text("first\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("directory", ["forPrompts", "docs/forPrompts"])
def test_ignored_untracked_references_are_not_opened(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    reference = reference_file(workspace, directory)
    real_open = Path.open

    def open_without_cloud_download(path: Path, *args, **kwargs):
        if path == reference:
            pytest.fail("A reference cloud placeholder must not be opened.")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_without_cloud_download)
    first, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert complete
    os.utime(reference, (1, 1))
    reference.rename(reference.with_name("renamed.txt"))
    second, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert complete
    assert first == second


@pytest.mark.parametrize("directory", ["forPrompts", "docs/forPrompts"])
def test_tracked_reference_content_and_deletion_remain_in_fingerprint(
    workspace: Path, directory: str
) -> None:
    reference = reference_file(workspace, directory)
    git(workspace, "add", "-f", reference.relative_to(workspace).as_posix())
    first, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert complete
    reference.write_text("other\n", encoding="utf-8")
    second, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert complete
    reference.unlink()
    third, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert complete
    assert first != second != third


@pytest.mark.parametrize(
    "directory",
    ["tests/fixtures/forPrompts", "tests/fixtures/ignored", "forPrompts"],
)
def test_other_ignored_inputs_and_nonignored_references_remain_fingerprinted(
    workspace: Path, directory: str
) -> None:
    if directory == "forPrompts":
        (workspace / ".gitignore").write_text("", encoding="utf-8")
    reference = reference_file(workspace, directory)
    first, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert complete
    reference.write_text("other\n", encoding="utf-8")
    second, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert complete
    assert first != second


def test_non_git_reference_directory_remains_fingerprinted(tmp_path: Path) -> None:
    reference = reference_file(tmp_path, "forPrompts")
    (tmp_path / ".gitignore").write_text("/forPrompts/\n", encoding="utf-8")
    first, complete = evidence._workspace_mutation_fingerprint(tmp_path)
    assert complete
    reference.write_text("other\n", encoding="utf-8")
    second, complete = evidence._workspace_mutation_fingerprint(tmp_path)
    assert complete
    assert first != second


@pytest.mark.parametrize("failure", ["missing_git", "error", "timeout"])
def test_git_query_failure_preserves_full_scan(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    reference = reference_file(workspace, "forPrompts")
    if failure == "missing_git":
        monkeypatch.setattr(evidence, "_trusted_system_executable", lambda *a, **k: None)
    elif failure == "error":
        monkeypatch.setattr(
            evidence.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=128)
        )
    else:
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        monkeypatch.setattr(evidence.subprocess, "run", timeout)
    first, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert complete
    reference.write_text("other\n", encoding="utf-8")
    second, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert complete
    assert first != second


@pytest.mark.parametrize("directory", ["forPrompts", "docs/forPrompts", "docs"])
def test_linked_reference_directory_is_not_excluded(
    workspace: Path, tmp_path: Path, directory: str
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "reference.txt").write_text("first\n", encoding="utf-8")
    link = workspace / directory
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this host.")
    _, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert complete is False


def test_reference_tracked_during_scan_invalidates_exclusion(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = reference_file(workspace, "forPrompts")
    real_exclusions = evidence._ignored_reference_artifact_directories
    calls = 0

    def exclusions_with_tracking_change(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            git(workspace, "add", "-f", reference.relative_to(workspace).as_posix())
        return real_exclusions(*args, **kwargs)

    monkeypatch.setattr(
        evidence, "_ignored_reference_artifact_directories", exclusions_with_tracking_change
    )
    _, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert calls == 2
    assert complete is False


def test_git_queries_share_fingerprint_deadline_and_discard_output(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference_file(workspace, "forPrompts")
    observed_timeouts = []

    def bounded_query(command, **kwargs):
        observed_timeouts.append(kwargs["timeout"])
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        return SimpleNamespace(returncode=0 if "check-ignore" in command else 1)

    monkeypatch.setattr(evidence.subprocess, "run", bounded_query)
    monkeypatch.setattr(evidence.time, "monotonic", lambda: 10.0)
    result = evidence._ignored_reference_artifact_directories(
        workspace, deadline=10.5, should_stop=lambda: False
    )
    assert result == frozenset({Path("forPrompts")})
    assert observed_timeouts == [0.5, 0.5]
    assert not evidence._ignored_reference_artifact_directories(
        workspace, deadline=10.0, should_stop=lambda: False
    )
    assert observed_timeouts == [0.5, 0.5]


def test_reference_churn_during_directory_recheck_remains_excluded(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference_file(workspace, "forPrompts")
    real_stat = os.stat
    root_stats = 0

    def stat_with_reference_churn(path, *args, **kwargs):
        nonlocal root_stats
        if Path(path) == workspace:
            root_stats += 1
            if root_stats == 2:
                os.utime(workspace, (1, 1))
                (workspace / "forPrompts" / "new.txt").write_text("new\n", encoding="utf-8")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(evidence.os, "stat", stat_with_reference_churn)
    _, complete = evidence._workspace_mutation_fingerprint(workspace)
    assert root_stats >= 2
    assert complete
