"""OpenClaw preservation, live SQLite, and protected path regressions.

Code version: v1.1.0-codex.0
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

import pytest

from app.core import openclaw_archive
from app.core.openclaw_archive import create_openclaw_archive
from app.core.owner_only_files import (
    OwnerOnlyFileError,
    owner_only_directory_problem,
    owner_only_problem,
)


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
        assert owner_only_problem(path) == ""
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "\\" not in entry["path"]
        assert entry["source_path"] == str(source / entry["path"])
    for directory in (archive, *[path for path in archive.rglob("*") if path.is_dir()]):
        assert owner_only_directory_problem(directory) == ""
        if os.name == "posix":
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for metadata in (archive / "manifest.json", tmp_path / "store/openclaw_archive/latest.json"):
        assert owner_only_problem(metadata) == ""
        if os.name == "posix":
            assert stat.S_IMODE(metadata.stat().st_mode) == 0o600


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
        with closing(sqlite3.connect(archived)) as restored:
            assert restored.execute("SELECT text FROM messages").fetchall() == [("preserved",)]
        assert owner_only_problem(archived) == ""
        if os.name == "posix":
            assert stat.S_IMODE(archived.stat().st_mode) == 0o600
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


def test_binary_copy_preserves_crlf_ctrl_z_and_non_text_bytes_on_windows(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    payload = b"first\r\nsecond\x1aafter-eof\x00\xff\r\n"
    source.write_bytes(payload)
    destination = tmp_path / "preserved.bin"
    marker = getattr(os, "O_BINARY", 0x40000000)
    open_file = os.open
    fdopen = os.fdopen
    text_descriptors = set()

    class CrtTextInput:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size=-1):
            return self.handle.read(size).split(b"\x1a", 1)[0].replace(b"\r\n", b"\n")

    def windows_open(path, flags, *args, **kwargs):
        descriptor = open_file(path, flags & ~marker if os.name != "nt" else flags, *args, **kwargs)
        if Path(path) == source and not flags & marker:
            text_descriptors.add(descriptor)
        return descriptor

    def windows_fdopen(descriptor, *args, **kwargs):
        handle = fdopen(descriptor, *args, **kwargs)
        return CrtTextInput(handle) if descriptor in text_descriptors else handle

    monkeypatch.setattr(openclaw_archive.os, "O_BINARY", marker, raising=False)
    monkeypatch.setattr(openclaw_archive.os, "open", windows_open)
    monkeypatch.setattr(openclaw_archive.os, "fdopen", windows_fdopen)
    entry = openclaw_archive._copy_file(source, destination)
    assert destination.read_bytes() == source.read_bytes() == payload
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert owner_only_problem(destination) == ""


def test_sqlite_source_is_closed_when_destination_connection_fails(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "copy.sqlite3"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE evidence (body TEXT)")
        connection.commit()
    before = source.read_bytes()
    connect = sqlite3.connect
    opened = []

    def fail_destination(database, *args, **kwargs):
        if database == destination:
            raise sqlite3.OperationalError("Synthetic destination refusal")
        connection = connect(database, *args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(openclaw_archive.sqlite3, "connect", fail_destination)
    with pytest.raises(sqlite3.OperationalError, match="Synthetic destination refusal"):
        openclaw_archive._backup_sqlite(source, destination)
    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert source.read_bytes() == before


@pytest.mark.parametrize("sqlite_source", [False, True])
def test_unverified_file_boundary_does_not_copy_private_bytes_or_replace_latest(
    tmp_path, monkeypatch, sqlite_source,
):
    source = tmp_path / "source"
    source.mkdir()
    filename = "history.sqlite3" if sqlite_source else "history.jsonl"
    original = source / filename
    if sqlite_source:
        with closing(sqlite3.connect(original)) as connection:
            connection.execute("CREATE TABLE evidence (body TEXT)")
            connection.execute("INSERT INTO evidence VALUES ('private source transcript')")
            connection.commit()
    else:
        original.write_bytes(b"private source transcript")
    body = original.read_bytes()
    first = create_openclaw_archive(tmp_path / "store", source)
    pointer = tmp_path / "store/openclaw_archive/latest.json"
    before_pointer = pointer.read_bytes()
    ensure = openclaw_archive.ensure_owner_only

    def reject_history(path):
        if path.name == filename:
            raise OwnerOnlyFileError("Synthetic native boundary refusal")
        return ensure(path)

    monkeypatch.setattr(openclaw_archive, "ensure_owner_only", reject_history)
    with pytest.raises(OwnerOnlyFileError, match="Synthetic native boundary refusal"):
        create_openclaw_archive(tmp_path / "store", source)
    assert pointer.read_bytes() == before_pointer
    if sqlite_source:
        with closing(sqlite3.connect(Path(first["state_directory"]) / filename)) as preserved:
            assert preserved.execute("SELECT body FROM evidence").fetchall() == [("private source transcript",)]
    else:
        assert (Path(first["state_directory"]) / filename).read_bytes() == body
    assert original.read_bytes() == body
    manifests = [json.loads(path.read_text()) for path in pointer.parent.glob("*/manifest.json")]
    incomplete = next(manifest for manifest in manifests if manifest["status"] == "incomplete")
    assert (Path(incomplete["state_directory"]) / filename).read_bytes() == b""
    assert owner_only_problem(Path(incomplete["manifest_path"])) == ""
