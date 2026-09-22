"""Regression tests for the one-way shadow cloud backup.

Code version: v1.3.0-codex.0
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import CrawlConfig
from app.core.job_lock import CacheTaskLock
from app.core.shadow_backup import (
    SettingsDirectoryBrowserError,
    ShadowBackupError,
    ShadowBackupService,
    browse_settings_directory,
    sync_shadow_backup,
)


def test_settings_directory_browser_lists_real_paths_and_symlink_state(tmp_path: Path) -> None:
    root = tmp_path / "Folder browser"
    empty = root / "空 目录 'quoted'"
    target = root / "target"
    empty.mkdir(parents=True)
    target.mkdir()
    (root / "not-a-folder.txt").write_text("content", encoding="utf-8")
    link = root / "target link"
    link.symlink_to(target, target_is_directory=True)
    broken_link = root / "deleted link"
    broken_link.symlink_to(root / "missing", target_is_directory=True)

    listing = browse_settings_directory(root, fallback_path=tmp_path)

    assert listing.path == root.resolve()
    assert listing.parent == tmp_path.resolve()
    assert [entry.name for entry in listing.directories] == [
        "deleted link",
        "target",
        "target link",
        "空 目录 'quoted'",
    ]
    entries = {entry.name: entry for entry in listing.directories}
    assert entries["target link"].is_symlink is True
    assert entries["target link"].path == target.resolve()
    assert entries["deleted link"].accessible is False
    assert "unavailable" in entries["deleted link"].reason
    assert "not-a-folder.txt" not in entries


def test_settings_directory_browser_recovers_missing_initial_path_to_existing_parent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "existing"
    root.mkdir()

    listing = browse_settings_directory(
        root / "deleted" / "child",
        fallback_path=tmp_path,
        recover_invalid=True,
    )

    assert listing.path == root.resolve()
    assert listing.recovered_from.endswith("deleted/child")


def test_settings_directory_browser_rejects_relative_navigation(tmp_path: Path) -> None:
    with pytest.raises(SettingsDirectoryBrowserError, match="absolute"):
        browse_settings_directory(Path("relative/path"), fallback_path=tmp_path)


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
