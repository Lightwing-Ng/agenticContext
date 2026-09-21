"""Regression tests for the one-way shadow cloud backup.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from app.core.config import CrawlConfig
from app.core.job_lock import CacheTaskLock
from app.core.shadow_backup import (
    MACOS_DIRECTORY_PICKER_APPLESCRIPT,
    ShadowBackupError,
    ShadowBackupService,
    choose_settings_directory,
    sync_shadow_backup,
)


def test_macos_directory_picker_uses_finder_and_restores_browser_focus(tmp_path: Path) -> None:
    selected_path = tmp_path / "selected"
    selected_path.mkdir()
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=f"{selected_path}\n",
        stderr="",
    )

    with patch("app.core.shadow_backup.is_windows_host", return_value=False), patch(
        "app.core.shadow_backup.subprocess.run",
        return_value=completed,
    ) as run:
        result = choose_settings_directory(tmp_path, "Select project folder")

    assert result == selected_path.resolve()
    run.assert_called_once_with(
        [
            "/usr/bin/osascript",
            "-e",
            MACOS_DIRECTORY_PICKER_APPLESCRIPT,
            "Select project folder",
            tmp_path.as_posix(),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert 'tell application "Finder"' in MACOS_DIRECTORY_PICKER_APPLESCRIPT
    assert "activate" in MACOS_DIRECTORY_PICKER_APPLESCRIPT
    assert "restorePreviousApplication" in MACOS_DIRECTORY_PICKER_APPLESCRIPT
    assert 'currentFrontmostProcessName is "Finder"' in MACOS_DIRECTORY_PICKER_APPLESCRIPT
    assert 'tell application "Terminal"' not in MACOS_DIRECTORY_PICKER_APPLESCRIPT


def test_macos_directory_picker_treats_user_cancel_as_no_selection(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="execution error: User canceled. (-128)\n",
    )

    with patch("app.core.shadow_backup.is_windows_host", return_value=False), patch(
        "app.core.shadow_backup.subprocess.run",
        return_value=completed,
    ):
        assert choose_settings_directory(tmp_path, "Select project folder") is None


def test_shadow_backup_copies_changes_and_optionally_mirrors_deletions(tmp_path: Path) -> None:
    source_root = tmp_path / "local_store"
    destination_root = tmp_path / "OneDrive" / "AICaches"
    source_file = source_root / "x" / "demo" / "media.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"first-cache-version")
    (source_root / ".cache_task.lock").write_text("runtime lock", encoding="utf-8")

    first_result = sync_shadow_backup(source_root, destination_root, mirror_deletions=False)

    mirrored_file = destination_root / "x" / "demo" / "media.jpg"
    assert first_result.copied_files == 1
    assert first_result.unchanged_files == 0
    assert mirrored_file.read_bytes() == b"first-cache-version"
    assert not (destination_root / ".cache_task.lock").exists()

    second_result = sync_shadow_backup(source_root, destination_root, mirror_deletions=False)

    assert second_result.copied_files == 0
    assert second_result.unchanged_files == 1

    source_file.write_bytes(b"second-cache-version")
    cloud_only_file = destination_root / "legacy" / "old.jpg"
    cloud_only_file.parent.mkdir(parents=True)
    cloud_only_file.write_bytes(b"keep-until-mirroring")
    changed_result = sync_shadow_backup(source_root, destination_root, mirror_deletions=False)

    assert changed_result.copied_files == 1
    assert mirrored_file.read_bytes() == b"second-cache-version"
    assert cloud_only_file.exists()

    mirrored_result = sync_shadow_backup(source_root, destination_root, mirror_deletions=True)

    assert mirrored_result.deleted_files == 1
    assert mirrored_result.deleted_directories == 1
    assert not cloud_only_file.exists()
    assert not cloud_only_file.parent.exists()


def test_shadow_backup_rejects_overlapping_source_and_destination(tmp_path: Path) -> None:
    source_root = tmp_path / "local_store"
    source_root.mkdir()

    with pytest.raises(ShadowBackupError, match="separate"):
        sync_shadow_backup(source_root, source_root / "AICaches", mirror_deletions=False)


def test_shadow_backup_keeps_cloud_type_conflicts_without_mirror_deletions(tmp_path: Path) -> None:
    source_root = tmp_path / "local_store"
    destination_root = tmp_path / "OneDrive" / "AICaches"
    source_file = source_root / "x" / "demo.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"source")
    conflicting_destination = destination_root / "x" / "demo.jpg"
    conflicting_destination.mkdir(parents=True)

    with pytest.raises(ShadowBackupError, match="directory where a source file"):
        sync_shadow_backup(source_root, destination_root, mirror_deletions=False)

    result = sync_shadow_backup(source_root, destination_root, mirror_deletions=True)

    assert result.copied_files == 1
    assert conflicting_destination.read_bytes() == b"source"


def test_shadow_backup_service_runs_a_manual_sync_with_the_shared_task_lock(tmp_path: Path) -> None:
    source_root = tmp_path / "local_store"
    destination_root = tmp_path / "OneDrive" / "AICaches"
    source_file = source_root / "media" / "grok" / "asset.png"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"asset")
    service = ShadowBackupService(source_root, task_lock=CacheTaskLock(tmp_path / "cache-task.lock"))
    config = CrawlConfig(
        shadow_backup_enabled=True,
        shadow_backup_destination=destination_root,
    )

    service.start(config)
    assert service._worker is not None
    service._worker.join(timeout=1)

    snapshot = service.snapshot()
    assert not snapshot["running"]
    assert snapshot["phase"] == "finished"
    assert snapshot["copied_files"] == 1
    assert (destination_root / "media" / "grok" / "asset.png").read_bytes() == b"asset"
