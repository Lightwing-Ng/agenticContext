"""OpenClaw preservation, live SQLite, and protected path regressions.

Code version: v1.0.1-codex.0
"""

import hashlib
import json
from pathlib import Path
import sqlite3
import stat

import pytest

from app.core.openclaw_archive import create_openclaw_archive


def test_private_self_contained_snapshot_preserves_files_and_explicit_exclusions(tmp_path):
    source = tmp_path / "source"
    (source / "agents/main/sessions").mkdir(parents=True)
    transcript = source / "agents/main/sessions/history.jsonl"
    transcript.write_text('{"type":"session"}\n', encoding="utf-8")
    (source / "media").mkdir()
    (source / "media/image.png").write_bytes(b"local media")
    for name in ("npm", "cache", "browser", "workspace/node_modules"):
        directory = source / name
        directory.mkdir(parents=True)
        (directory / "private.txt").write_text("excluded", encoding="utf-8")
    (source / "workspace/note.md").write_text("workspace evidence", encoding="utf-8")
    (source / "alias").symlink_to(source / "media", target_is_directory=True)
    original = transcript.read_bytes()

    result = create_openclaw_archive(tmp_path / "store", source)
    archive = Path(result["archive_directory"])
    state = archive / "state"
    pointer = json.loads((tmp_path / "store/openclaw_archive/latest.json").read_text())

    assert result["status"] == "complete"
    assert result["file_count"] == 3
    assert result["total_bytes"] == sum(entry["size"] for entry in result["files"])
    assert {entry["path"] for entry in result["exclusions"]} == {"npm", "cache", "browser", "workspace/node_modules"}
    assert result["symlinks"] == [{"path": "alias", "target": str(source / "media")}]
    assert not (state / "alias").exists()
    assert not (state / "browser").exists()
    assert pointer["archive_directory"] == str(archive)
    assert Path(pointer["manifest_path"]).is_file()
    assert transcript.read_bytes() == original
    for entry in result["files"]:
        path = state / entry["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(archive.stat().st_mode) == 0o700
    assert stat.S_IMODE((archive / "manifest.json").stat().st_mode) == 0o600


def test_online_sqlite_backup_includes_committed_wal_and_detects_nonstandard_extension(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "history.sqlite.migrated"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE messages (text TEXT)")
    connection.execute("INSERT INTO messages VALUES ('preserved')")
    connection.commit()
    try:
        result = create_openclaw_archive(tmp_path / "store", source)
        archived = Path(result["state_directory"]) / path.name
        with sqlite3.connect(archived) as restored:
            assert restored.execute("SELECT text FROM messages").fetchall() == [("preserved",)]
        assert result["sqlite_count"] == 1
        assert result["files"][0]["strategy"] == "sqlite-backup"
        assert result["files"][0]["quick_check"] == "ok"
        assert not Path(str(archived) + "-wal").exists()
        assert {entry["path"] for entry in result["exclusions"]} == {path.name + "-wal", path.name + "-shm"}
    finally:
        connection.close()


def test_repeated_snapshots_never_overwrite_previous_evidence(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "history.jsonl").write_text("first", encoding="utf-8")
    first = create_openclaw_archive(tmp_path / "store", source)
    (source / "history.jsonl").write_text("second", encoding="utf-8")
    second = create_openclaw_archive(tmp_path / "store", source)
    assert first["archive_directory"] != second["archive_directory"]
    assert (Path(first["state_directory"]) / "history.jsonl").read_text() == "first"
    assert (Path(second["state_directory"]) / "history.jsonl").read_text() == "second"


def test_symlink_source_and_destination_or_nested_destination_are_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        create_openclaw_archive(tmp_path / "store", alias)
    with pytest.raises(ValueError, match="overlap"):
        create_openclaw_archive(source / "nested", source)
    store = tmp_path / "store"
    store.mkdir()
    (store / "openclaw_archive").symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        create_openclaw_archive(store, source)


def test_failed_snapshot_leaves_latest_successful_archive_unchanged(tmp_path, monkeypatch):
    from app.core import openclaw_archive

    source = tmp_path / "source"
    source.mkdir()
    (source / "history.jsonl").write_text("original", encoding="utf-8")
    first = create_openclaw_archive(tmp_path / "store", source)
    pointer = tmp_path / "store/openclaw_archive/latest.json"
    original_pointer = pointer.read_bytes()

    def fail_copy(*_args):
        raise RuntimeError("source changed")

    monkeypatch.setattr(openclaw_archive, "_copy_file", fail_copy)
    with pytest.raises(RuntimeError, match="source changed"):
        create_openclaw_archive(tmp_path / "store", source)
    assert pointer.read_bytes() == original_pointer
    assert Path(first["manifest_path"]).is_file()


def test_dot_segments_cannot_hide_a_destination_inside_the_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "other").mkdir()
    destination = tmp_path / "other" / ".." / "source" / "store"

    with pytest.raises(ValueError, match="overlap"):
        create_openclaw_archive(destination, source)

    assert not (source / "store").exists()


def test_unreadable_subtree_never_replaces_a_verified_archive(tmp_path, monkeypatch):
    from app.core import openclaw_archive

    source = tmp_path / "source"
    source.mkdir()
    (source / "history.jsonl").write_text("preserved", encoding="utf-8")
    first = create_openclaw_archive(tmp_path / "store", source)
    pointer = tmp_path / "store/openclaw_archive/latest.json"
    previous_pointer = pointer.read_bytes()

    def unreadable_walk(_source, *, onerror, **_options):
        onerror(PermissionError("A source subtree could not be read."))
        yield

    monkeypatch.setattr(openclaw_archive.os, "walk", unreadable_walk)
    with pytest.raises(PermissionError, match="subtree"):
        create_openclaw_archive(tmp_path / "store", source)

    assert pointer.read_bytes() == previous_pointer
    assert Path(first["manifest_path"]).is_file()
