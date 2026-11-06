"""Verify source-preserving workstation relocation. Code version: v1.2.1-codex.0."""

from __future__ import annotations

import importlib.util
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def tool(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/preserve_wechat_workstations.py"
    spec = importlib.util.spec_from_file_location("preserve_wechat_workstations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "open_handles", lambda paths: {})
    return module


def write(root, relative, content=b"immutable evidence"):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def test_dry_run_keeps_pinned_artifacts_and_runtime_databases(tool, tmp_path):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    artifact = "repair_audits/native/artifact.json"
    write(root, artifact)
    write(root, "storage_backups/run/history.sqlite3")
    write(root, "history.sqlite3")
    write(root, "contact_activity/published.sqlite3")
    write(root, "contact_activity/.contact-analysis-old.sqlite3")
    write(root, "text_observations.json", json.dumps({
        "schema_version": 1, "observations": [{"source": {"path": artifact}}],
    }).encode())
    report = tool.preserve(root, target)
    assert report["entries"][artifact]["defer_reason"] == "runtime_pinned_artifact"
    assert "history.sqlite3" not in report["entries"]
    assert "contact_activity/published.sqlite3" not in report["entries"]
    assert "contact_activity/.contact-analysis-old.sqlite3" in report["entries"]
    assert not target.exists()
    assert not (root / tool.LOCATOR).exists()


def test_rename_preserves_inode_and_checksums_and_is_idempotent(tool, tmp_path):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "storage_backups/run/history.sqlite3")
    inode, mtime = source.stat().st_ino, source.stat().st_mtime_ns
    report = tool.preserve(root, target, apply=True)
    moved = target / source.relative_to(root)
    assert not source.exists()
    assert (moved.stat().st_ino, moved.stat().st_mtime_ns) == (inode, mtime)
    entry = report["moved_entries"]["storage_backups/run/history.sqlite3"]
    assert entry["status"] == "moved_verified"
    assert entry["sha256"] == tool.hash_local(moved, tool.identity(moved))
    again = tool.preserve(root, target, apply=True)
    assert again["moved_entries"] == report["moved_entries"]
    locator = json.loads((root / tool.LOCATOR).read_text())
    assert locator["cloud_upload_verified"] is False
    assert locator["locations"]["storage_backups/run/history.sqlite3"]["path"] == str(moved)


def test_placeholder_is_never_opened_hashed_or_moved(tool, tmp_path, monkeypatch):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "contacts 2.sqlite3")
    identity = tool.identity
    def metadata(path):
        value = identity(path)
        if path == source:
            value["flags"] |= tool.DATALESS
        return value
    monkeypatch.setattr(tool, "identity", metadata)
    monkeypatch.setattr(tool, "hash_local", lambda *args: pytest.fail("A placeholder was read"))
    report = tool.preserve(root, target, apply=True)
    assert source.exists()
    assert report["entries"][source.name]["defer_reason"] == "cloud_placeholder_requires_separate_preservation"
    assert not (target / source.name).exists()


@pytest.mark.parametrize("process,lock,allow,expected", [
    ("python3", "", True, False), ("fileproviderd", "W", True, False),
    ("fileproviderd", "", False, False), ("fileproviderd", "", True, True),
    ("fileproviderd", " ", True, True),
])
def test_open_handles_require_exact_provider_exception(tool, process, lock, allow, expected):
    handles = [{"process": process, "lock": lock}]
    assert tool.permitted_handles(handles, allow) is expected


def test_existing_destination_is_never_overwritten(tool, tmp_path):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "imports/source/evidence.json", b"source")
    destination = write(target, "imports/source/evidence.json", b"different")
    with pytest.raises(FileExistsError):
        tool.preserve(root, target, apply=True)
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"different"


def test_source_changes_before_hash_blocks_rename(tool, tmp_path, monkeypatch):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "recovery/receipt.json")
    original = tool.hash_local
    def changed(path, expected):
        source.write_bytes(b"changed evidence")
        return original(path, expected)
    monkeypatch.setattr(tool, "hash_local", changed)
    with pytest.raises(ValueError, match="changed"):
        tool.preserve(root, target, apply=True)
    assert source.exists()
    assert not (target / source.relative_to(root)).exists()


@pytest.mark.parametrize("legacy_metadata", [False, True])
def test_interrupted_rename_resumes_from_sealed_receipt(tool, tmp_path, legacy_metadata):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "recovery/receipt.json")
    before = tool.identity(source)
    if legacy_metadata:
        before.pop("file_attributes", None)
        before.pop("reparse_tag", None)
    checksum = tool.hash_local(source, before)
    target.mkdir()
    moved = target / "recovery/receipt.json"
    moved.parent.mkdir()
    os.rename(source, moved)
    tool.atomic_json(target / tool.RECEIPT, {
        "version": tool.VERSION, "source_root": str(root), "destination_root": str(target),
        "entries": {"recovery/receipt.json": {"status": "sealed_before_rename",
                    "source_identity": before, "sha256": checksum}},
    })
    report = tool.preserve(root, target, apply=True)
    assert report["moved_entries"]["recovery/receipt.json"]["status"] == "moved_verified"
    assert not source.exists()


def test_symlink_in_candidate_tree_is_rejected(tool, tmp_path):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "storage_backups").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        tool.preserve(root, target, apply=True)


def test_open_database_protects_its_closed_journal(tool, tmp_path, monkeypatch):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "contact_activity/.contact-analysis-live.sqlite3")
    journal = write(root, "contact_activity/.contact-analysis-live.sqlite3-journal")
    monkeypatch.setattr(tool, "open_handles", lambda paths: {
        str(source): [{"process": "python3", "pid": 123, "fd": "4"}],
    })
    report = tool.preserve(root, target, apply=True)
    assert source.exists() and journal.exists()
    assert report["entries"][journal.relative_to(root).as_posix()]["defer_reason"] == "database_family_requires_review"
    assert not report["moved_entries"]


def test_missing_preserved_destination_is_not_reported_as_success(tool, tmp_path):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "recovery/receipt.json")
    tool.preserve(root, target, apply=True)
    (target / source.relative_to(root)).unlink()
    with pytest.raises(FileNotFoundError):
        tool.preserve(root, target, apply=True)


def test_size_limit_defers_large_files_and_keeps_database_family_together(tool, tmp_path):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    large = write(root, "storage_backups/run/history.sqlite3", b"a" * 32)
    journal = write(root, "storage_backups/run/history.sqlite3-journal", b"journal")
    small = write(root, "recovery/receipt.json", b"receipt")
    report = tool.preserve(root, target, apply=True, max_file_bytes=16)
    assert large.exists() and journal.exists()
    assert not small.exists()
    assert report["entries"]["storage_backups/run/history.sqlite3"]["defer_reason"] == "requires_compressed_preservation"
    assert report["entries"]["storage_backups/run/history.sqlite3-journal"]["defer_reason"] == "database_family_requires_review"
    unlimited = tool.preserve(root, target, apply=True)
    assert not large.exists() and not journal.exists()
    assert len(unlimited["moved_entries"]) == 3


def test_cli_forwards_optional_size_limit(tool, tmp_path, monkeypatch, capsys):
    observed = {}
    def inspect(root, destination, **kwargs):
        observed.update(kwargs)
        return {"dry_run": True}
    monkeypatch.setattr(tool, "preserve", inspect)
    monkeypatch.setattr(tool.sys, "argv", [
        "preserve_wechat_workstations", "--archive-root", str(tmp_path / "wechat"),
        "--destination", str(tmp_path / "workstation"), "--max-file-bytes", "104857600",
    ])
    tool.main()
    assert observed["max_file_bytes"] == 104857600
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_missing_allocation_metadata_retains_flags_and_never_reads_payloads(tool, tmp_path, monkeypatch):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    resident = write(root, "storage_backups/run/history.sqlite3")
    placeholder = write(root, "contacts 2.sqlite3")
    lstat = Path.lstat

    class NoAllocation:
        def __init__(self, metadata, dataless):
            self.metadata = metadata
            self.st_flags = getattr(metadata, "st_flags", 0) | (tool.DATALESS if dataless else 0)

        def __getattr__(self, name):
            if name == "st_blocks":
                raise AttributeError("Windows does not expose st_blocks")
            return getattr(self.metadata, name)

    def missing_blocks(path, *args, **kwargs):
        state = lstat(path, *args, **kwargs)
        return NoAllocation(state, path == placeholder) if path in {resident, placeholder} else state

    monkeypatch.setattr(Path, "lstat", missing_blocks)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Metadata preview opened a payload"))
    report = tool.preserve(root, target)
    assert report["dry_run"] and not target.exists()
    assert report["cloud_upload_verified"] is False
    assert report["local_release_attempted"] is False
    entries = report["entries"]
    for relative, path in (("storage_backups/run/history.sqlite3", resident), ("contacts 2.sqlite3", placeholder)):
        recorded = entries[relative]["source_identity"]
        actual = lstat(path)
        assert recorded["allocated_bytes"] is None
        assert recorded["bytes"] == actual.st_size
        assert recorded["inode"] == actual.st_ino and recorded["mtime_ns"] == actual.st_mtime_ns
    assert entries["contacts 2.sqlite3"]["source_identity"]["flags"] & tool.DATALESS
    assert entries["contacts 2.sqlite3"]["defer_reason"] == "cloud_placeholder_requires_separate_preservation"
    assert entries["storage_backups/run/history.sqlite3"]["defer_reason"] is None


@pytest.fixture
def windows_filesystem(tool, monkeypatch):
    state = SimpleNamespace(rename_calls=[], create_collision=False, fail_sync=False, binary_opens=[])
    binary = getattr(os, "O_BINARY", 0x40000000)

    class WindowsOS:
        name = "nt"
        O_BINARY = binary

        def __getattr__(self, name):
            return getattr(os, name)

        def open(self, path, flags, *args, **kwargs):
            if Path(path).is_dir():
                raise OSError(errno.EACCES, "Windows CRT cannot open this directory")
            assert flags & binary, "Preservation checksums require binary source descriptors"
            state.binary_opens.append(Path(path))
            return os.open(path, flags if os.name == "nt" else flags & ~binary, *args, **kwargs)

        def rename(self, source, destination):
            state.rename_calls.append((source, destination))
            if state.create_collision:
                destination.write_bytes(b"Independent concurrent destination")
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(destination)
            return os.rename(source, destination)

        def fsync(self, descriptor):
            if state.fail_sync:
                raise OSError(errno.EIO, "Synthetic preservation file sync failure")
            return os.fsync(descriptor)

    monkeypatch.setattr(tool, "os", WindowsOS())
    monkeypatch.setattr(tool, "sys", SimpleNamespace(platform="win32"))
    return state


@pytest.mark.parametrize("change", ["none", "destination_collision", "source_drift"])
def test_windows_preservation_keeps_binary_bytes_and_refuses_collision_or_drift(
    tool, tmp_path, windows_filesystem, monkeypatch, change,
):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    body = b"Exact original\r\nbytes\x1aafter-marker\x00\xff"
    source = write(root, "recovery/evidence.bin", body)
    destination = target / "recovery/evidence.bin"
    original = source.stat()
    windows_filesystem.create_collision = change == "destination_collision"
    if change == "source_drift":
        calls = []

        def drift(paths):
            calls.append(list(paths))
            if len(calls) == 2:
                source.write_bytes(b"Concurrent source evidence")
            return {}

        monkeypatch.setattr(tool, "open_handles", drift)
    if change == "none":
        result = tool.preserve(root, target, apply=True)
        assert destination.read_bytes() == body and not source.exists()
        assert (destination.stat().st_ino, destination.stat().st_mtime_ns) == (original.st_ino, original.st_mtime_ns)
        assert result["cloud_upload_verified"] is result["local_release_attempted"] is False
        assert windows_filesystem.binary_opens == [source, destination]
    elif change == "destination_collision":
        with pytest.raises(FileExistsError):
            tool.preserve(root, target, apply=True)
        assert source.read_bytes() == body
        assert destination.read_bytes() == b"Independent concurrent destination"
    else:
        with pytest.raises(ValueError, match="changed after planning"):
            tool.preserve(root, target, apply=True)
        assert source.read_bytes() == b"Concurrent source evidence"
        assert not destination.exists()
        assert windows_filesystem.rename_calls == []


def test_windows_directory_sync_exception_does_not_suppress_file_sync_failures(tool, tmp_path, windows_filesystem):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "recovery/evidence.bin", b"Retain source evidence")
    windows_filesystem.fail_sync = True
    with pytest.raises(OSError, match="Synthetic preservation file sync failure"):
        tool.preserve(root, target, apply=True)
    assert source.read_bytes() == b"Retain source evidence"
    assert not (target / "recovery/evidence.bin").exists()
    assert windows_filesystem.rename_calls == []


@pytest.mark.parametrize("attributes", [0x1000, 0x40000, 0x400000, 0x400])
@pytest.mark.parametrize("kind", ["payload", "control"])
def test_native_placeholder_attributes_are_retained_and_refused_before_reads(
    tool, tmp_path, monkeypatch, attributes, kind,
):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    relative = "storage_backups/run/history.sqlite3" if kind == "payload" else "text_observations.json"
    source = write(root, relative, b"Private synthetic source bytes")
    lstat = Path.lstat

    class NativeAttributes:
        st_file_attributes = attributes
        st_reparse_tag = 0x9000001A

        def __init__(self, metadata):
            self.metadata = metadata

        def __getattr__(self, name):
            return getattr(self.metadata, name)

    def native_metadata(path, *args, **kwargs):
        value = lstat(path, *args, **kwargs)
        return NativeAttributes(value) if path == source else value

    monkeypatch.setattr(Path, "lstat", native_metadata)
    metadata = tool.identity(source)
    assert metadata["file_attributes"] == attributes
    assert metadata["reparse_tag"] == 0x9000001A
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("A native placeholder was opened"))
    if kind == "control":
        with pytest.raises(ValueError, match="bounded local JSON"):
            tool.local_json(source)
    else:
        checksum = tool.hash_local
        monkeypatch.setattr(tool, "hash_local", lambda *args: pytest.fail("A native placeholder was hashed"))
        report = tool.preserve(root, target)
        expected_reason = ("native_reparse_requires_separate_preservation" if attributes == 0x400
                           else "cloud_placeholder_requires_separate_preservation")
        assert report["entries"][relative]["defer_reason"] == expected_reason
        with pytest.raises(ValueError, match="unavailable"):
            checksum(source, metadata)
        assert not target.exists()


@pytest.mark.parametrize("flag", [0x400, 0x1000, 0x40000, 0x400000, "mac-dataless"])
@pytest.mark.parametrize("location", ["root", "source_parent", "destination_parent", "child", "nested_child", "activity"])
def test_unavailable_directory_never_allows_traversal_or_relocation(tool, tmp_path, monkeypatch, flag, location):
    root = tmp_path / "source-parent/wechat" if location == "source_parent" else tmp_path / "wechat"
    target = tmp_path / "destination-parent/workstation" if location == "destination_parent" else tmp_path / "workstation"
    relative = ("recovery/blocked/evidence.bin" if location == "nested_child"
                else "contact_activity/.contact-analysis-blocked.sqlite3" if location == "activity"
                else "recovery/evidence.bin")
    source = write(root, relative, b"Untouched synthetic source evidence")
    target.parent.mkdir(parents=True, exist_ok=True)
    blocked = {"root": root, "source_parent": root.parent, "destination_parent": target.parent,
               "child": root / "recovery", "nested_child": root / "recovery/blocked",
               "activity": root / "contact_activity"}[location]
    lstat, scandir = Path.lstat, os.scandir

    class FlaggedDirectory:
        def __init__(self, value):
            self.value = value
            self.st_flags = getattr(value, "st_flags", 0) | (tool.DATALESS if flag == "mac-dataless" else 0)
            self.st_file_attributes = 0x10 | (0 if flag == "mac-dataless" else flag)
            self.st_reparse_tag = 0xA0000003 if flag == 0x400 else 0

        def __getattr__(self, name):
            return getattr(self.value, name)

    def metadata(path, *args, **kwargs):
        if path != blocked and path.is_relative_to(blocked):
            pytest.fail("Metadata traversed an unavailable ancestor")
        value = lstat(path, *args, **kwargs)
        return FlaggedDirectory(value) if path == blocked else value

    def no_descent(path):
        if Path(path).is_relative_to(blocked):
            pytest.fail("Enumeration entered an unavailable directory")
        return scandir(path)

    monkeypatch.setattr(Path, "lstat", metadata)
    monkeypatch.setattr(tool.os, "scandir", no_descent)
    monkeypatch.setattr(tool, "hash_local", lambda *args: pytest.fail("An unavailable subtree was read"))
    monkeypatch.setattr(tool, "rename_exclusive", lambda *args: pytest.fail("An unavailable subtree was relocated"))
    if location in {"child", "nested_child", "activity"}:
        report = tool.preserve(root, target, apply=True)
        relative_directory = blocked.relative_to(root).as_posix()
        assert report["entries"][relative_directory]["defer_reason"] == "unavailable_directory_requires_review"
        assert report["deferred_subtrees"] == [relative_directory]
        assert report["enumeration_complete"] is False
        assert not report["moved_entries"]
    else:
        with pytest.raises(ValueError, match="unavailable|reparse"):
            tool.preserve(root, target, apply=True)
        assert not target.exists()
    assert lstat(source).st_size == len(b"Untouched synthetic source evidence")


def test_directory_enumeration_error_cannot_report_complete_or_relocate_evidence(tool, tmp_path, monkeypatch):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "recovery/evidence.bin", b"Untouched synthetic source evidence")

    def unreadable(_path, **options):
        if options.get("onerror"):
            options["onerror"](PermissionError("Synthetic unavailable subtree"))
        yield from ()

    monkeypatch.setattr(tool.os, "walk", unreadable)
    monkeypatch.setattr(tool, "hash_local", lambda *args: pytest.fail("An unverified namespace was read"))
    monkeypatch.setattr(tool, "rename_exclusive", lambda *args: pytest.fail("An unverified namespace was relocated"))
    with pytest.raises(PermissionError, match="Synthetic unavailable subtree"):
        tool.preserve(root, target, apply=True)
    assert source.exists() and not target.exists()
