"""Verify source-preserving workstation relocation. Code version: v1.0.1-codex.0."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest


@pytest.fixture
def native_tool():
    path = Path(__file__).resolve().parents[1] / "scripts/preserve_wechat_workstations.py"
    spec = importlib.util.spec_from_file_location("preserve_wechat_workstations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool(native_tool, monkeypatch):
    """Exercise portable orchestration without inventing native durability."""
    def rename(source, target):
        if os.path.lexists(target):
            raise FileExistsError(target)
        os.rename(source, target)

    monkeypatch.setattr(native_tool, "open_handles", lambda paths: {})
    monkeypatch.setattr(native_tool, "require_native_relocation", lambda: None)
    monkeypatch.setattr(native_tool, "rename_exclusive", rename)
    monkeypatch.setattr(native_tool, "sync_directory", lambda path: None)
    return native_tool


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


def test_interrupted_rename_resumes_from_sealed_receipt(tool, tmp_path):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "recovery/receipt.json")
    before = tool.identity(source)
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
    assert report["entries"][str(journal.relative_to(root))]["defer_reason"] == "database_family_requires_review"
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


def test_parent_traversal_cannot_hide_a_destination_inside_runtime(tool, tmp_path):
    root, outside = tmp_path / "wechat", tmp_path / "outside"
    source = write(root, "imports/evidence.json")
    outside.mkdir()
    target = outside / ".." / root.name / "imports/relocated"
    with pytest.raises(ValueError, match="Parent traversal"):
        tool.preserve(root, target, apply=True)
    assert source.read_bytes() == b"immutable evidence"
    assert not (root / "imports/relocated").exists()
    assert not (root / tool.LOCATOR).exists()


def test_source_parent_traversal_is_rejected_before_any_metadata_is_written(tool, tmp_path):
    root = tmp_path / "wechat"
    source = write(root, "recovery/evidence.json")
    alias = root / ".." / root.name
    destination = tmp_path / "workstation"
    with pytest.raises(ValueError, match="Parent traversal"):
        tool.preserve(alias, destination, apply=True)
    assert source.exists()
    assert not destination.exists()
    assert not (root / tool.LOCATOR).exists()


@pytest.mark.parametrize("nested", ["destination", "runtime"])
def test_case_variants_follow_actual_directory_separation(tool, tmp_path, nested):
    mixed = tmp_path / "MixedCase"
    mixed.mkdir()
    alias = tmp_path / "MIXEDCASE"
    aliased = alias.is_dir() and alias.samefile(mixed)
    if not aliased:
        alias.mkdir()
    if nested == "destination":
        root, destination = mixed, alias / "imports/relocated"
    else:
        root, destination = mixed / "wechat", alias
    source = write(root, "imports/evidence.json")
    if aliased:
        with pytest.raises(ValueError, match="separate from runtime"):
            tool.preserve(root, destination, apply=True)
        assert source.read_bytes() == b"immutable evidence"
        assert not (root / tool.LOCATOR).exists()
        assert not (destination / tool.RECEIPT).exists()
    else:
        tool.preserve(root, destination, apply=True)
        assert not source.exists()
        assert (destination / "imports/evidence.json").read_bytes() == b"immutable evidence"


def test_phased_destinations_retain_all_original_locations_and_receipts(tool, tmp_path):
    root = tmp_path / "wechat"
    first, second = tmp_path / "first", tmp_path / "second"
    write(root, "recovery/first.json", b"first")
    tool.preserve(root, first, apply=True)
    # Exercise a locator produced by the previously released script as well.
    locator = json.loads((root / tool.LOCATOR).read_text())
    locator["version"] = "v1.0.0-codex.0"
    locator.pop("source_root")
    for entry in locator["locations"].values():
        entry.pop("workstation_root")
        entry.pop("receipt")
    (root / tool.LOCATOR).write_text(json.dumps(locator))
    write(root, "recovery/second.json", b"second")
    tool.preserve(root, second, apply=True)
    updated = json.loads((root / tool.LOCATOR).read_text())
    assert set(updated["locations"]) == {"recovery/first.json", "recovery/second.json"}
    for relative, destination in [("recovery/first.json", first), ("recovery/second.json", second)]:
        entry = updated["locations"][relative]
        assert entry["path"] == str(destination / relative)
        assert entry["workstation_root"] == str(destination)
        assert entry["receipt"] == str(destination / tool.RECEIPT)
    assert (first / "recovery/first.json").read_bytes() == b"first"
    tool.preserve(root, tmp_path / "empty-phase", apply=True)
    assert json.loads((root / tool.LOCATOR).read_text())["locations"] == updated["locations"]


def test_conflicting_relative_location_blocks_all_moves_and_keeps_old_locator(tool, tmp_path):
    root, first, second = tmp_path / "wechat", tmp_path / "first", tmp_path / "second"
    write(root, "recovery/z.json", b"original")
    tool.preserve(root, first, apply=True)
    original_locator = (root / tool.LOCATOR).read_bytes()
    other = write(root, "imports/a.json", b"must stay")
    conflict = write(root, "recovery/z.json", b"new evidence at the same runtime path")
    with pytest.raises(ValueError, match="conflicting runtime path"):
        tool.preserve(root, second, apply=True)
    assert other.read_bytes() == b"must stay"
    assert conflict.read_bytes() == b"new evidence at the same runtime path"
    assert not second.exists()
    assert (root / tool.LOCATOR).read_bytes() == original_locator
    assert (first / "recovery/z.json").read_bytes() == b"original"


def test_legacy_receipt_remains_resumable_after_patch_version_bump(tool, tmp_path):
    root, target = tmp_path / "wechat", tmp_path / "workstation"
    write(root, "recovery/evidence.json")
    tool.preserve(root, target, apply=True)
    path = target / tool.RECEIPT
    receipt = json.loads(path.read_text())
    receipt["version"] = "v1.0.0-codex.0"
    path.write_text(json.dumps(receipt))
    assert tool.preserve(root, target, apply=True)["moved_entries"]["recovery/evidence.json"]["status"] == "moved_verified"


def test_filesystem_without_allocation_metadata_reports_unknown(tool, monkeypatch):
    state = SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4,
                            st_mode=stat.S_IFREG | 0o600, st_uid=0, st_gid=0, st_nlink=1)
    monkeypatch.setattr(Path, "lstat", lambda self: state)
    assert tool.identity(Path("synthetic"))["allocated_bytes"] is None


@pytest.mark.parametrize("platform,expected", [
    ("darwin", (b"source", b"target", 4)),
    ("linux", (-100, b"source", -100, b"target", 1)),
])
def test_native_exclusive_rename_dispatch_uses_no_replace_flag(native_tool, monkeypatch, platform, expected):
    calls = []
    def rename(*arguments):
        calls.append(arguments)
        return 0
    library = SimpleNamespace(renamex_np=rename, renameat2=rename)
    monkeypatch.setattr(native_tool, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(native_tool.ctypes, "CDLL", lambda *args, **kwargs: library)
    native_tool.rename_exclusive(Path("source"), Path("target"))
    assert calls == [expected]


def test_unsupported_apply_and_rename_fail_before_native_or_file_changes(native_tool, tmp_path, monkeypatch):
    root, destination = tmp_path / "wechat", tmp_path / "workstation"
    source = write(root, "recovery/evidence.json")
    monkeypatch.setattr(native_tool, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(native_tool.ctypes, "CDLL", lambda *args, **kwargs: pytest.fail("Unsupported native dispatch"))
    with pytest.raises(ValueError, match="only on macOS and Linux"):
        native_tool.rename_exclusive(source, destination)
    with pytest.raises(ValueError, match="only on macOS and Linux"):
        native_tool.preserve(root, destination, apply=True)
    assert source.exists()
    assert not destination.exists()
    assert not (root / native_tool.LOCATOR).exists()
