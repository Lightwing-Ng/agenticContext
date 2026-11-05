"""Verify reversible publication, external preservation gates, and source pins.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest


_SCRIPT = Path(__file__).parents[1] / "scripts" / "publish_wechat_compaction.py"
_SPEC = importlib.util.spec_from_file_location("publish_wechat_compaction", _SCRIPT)
publisher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(publisher)
compactor = publisher.compactor


def _simulated_swap(left, right):
    left, right = Path(left), Path(right)
    temporary = right.with_name(right.name + ".test-swap")
    os.replace(left, temporary)
    os.replace(right, left)
    os.replace(temporary, right)


def _external_backup(source, directory):
    """Represent the separately tested archive tool's immutable verification receipt."""
    archive = directory / (source.name + ".zst")
    archive.write_bytes(b"Synthetic externally verified stream: " + source.name.encode())
    receipt = archive.with_name(archive.name + ".receipt.json")
    receipt.write_text(json.dumps({
        "status": "byte_exact_preservation_verified_upload_pending",
        "source": str(source), "source_sha256": compactor.sha256(source),
        "decompressed_sha256": compactor.sha256(source),
        "decompressed_bytes": source.stat().st_size,
        "archive": str(archive), "archive_bytes": archive.stat().st_size,
        "archive_sha256": compactor.sha256(archive),
        "full_roundtrip_verified": True, "source_unchanged": True, "source_deleted": False,
    }))
    return receipt


@pytest.fixture
def publication(tmp_path, monkeypatch):
    runtime, stage, external = (tmp_path / name for name in ("runtime", "stage", "external"))
    for path in (runtime, stage, external):
        path.mkdir()
    history = runtime / "history.sqlite3"
    with closing(sqlite3.connect(history)) as connection, connection:
        connection.executescript(
            "CREATE TABLE partitions(id TEXT PRIMARY KEY,account_key TEXT,account_id TEXT);"
            "INSERT INTO partitions VALUES('source','account','owner');"
            "CREATE TABLE message_storage(id TEXT PRIMARY KEY,text TEXT);"
            "INSERT INTO message_storage VALUES('original','Retain exact original private text');"
            "CREATE VIEW messages AS SELECT id,text FROM message_storage;"
        )
    sidecar = runtime / "resource_references.sqlite3"
    with closing(sqlite3.connect(sidecar)) as connection, connection:
        connection.executescript(
            "CREATE TABLE archive_info(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            "CREATE TABLE messages(id TEXT PRIMARY KEY,text TEXT);"
            "INSERT INTO messages VALUES('media','Retain original metadata');"
            "CREATE TABLE conversations(id TEXT PRIMARY KEY);"
        )
        partition = hashlib.sha256(json.dumps([("source", "account", "owner")], separators=(",", ":")).encode()).hexdigest()
        connection.executemany("INSERT INTO archive_info VALUES(?,?)", [
            ("schema_version", "1"),
            ("history_file_identity", json.dumps(compactor.identity(history))),
            ("history_partitions_sha256", partition),
        ])
    receipts, backups, reports = [], [], []
    for database in (history, sidecar):
        receipt = stage / (database.name + ".json")
        report = compactor.prepare(database, stage / database.name, receipt)
        receipts.append(receipt)
        reports.append(report)
        backups.append(_external_backup(database, external))
    compactor.rebind_sidecar(receipts[1], receipts[0])
    monkeypatch.setattr(publisher, "_ensure_platform", lambda: None)
    monkeypatch.setattr(publisher, "_assert_no_writers", lambda path: [])
    monkeypatch.setattr(publisher, "_swap", _simulated_swap)
    if os.name == "nt":
        def sync_test_files(left, right):
            # The macOS exchange/directory primitives are modeled by this fixture.
            for path in (left, right):
                with Path(path).open("r+b") as stream:
                    os.fsync(stream.fileno())

        monkeypatch.setattr(publisher, "_sync_pair", sync_test_files)
    return {"history": history, "sidecar": sidecar, "receipts": receipts,
            "backups": backups, "reports": reports, "operation": external / "operation.json"}


def _publish(fixture):
    return publisher.publish(list(reversed(fixture["receipts"])), fixture["backups"], fixture["operation"])


@pytest.mark.parametrize("helper", ["_view_content", "_info"])
@pytest.mark.parametrize("read_error", [False, True])
def test_preflight_readers_close_before_publication_or_error(
    publication, monkeypatch, helper, read_error
):
    readonly = compactor.readonly
    readers = []

    class TrackedReader:
        def __init__(self, path):
            self.connection = readonly(path)
            self.closed = False
            readers.append(self)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *error):
            return self.connection.__exit__(*error)

        def execute(self, *args):
            if read_error:
                raise sqlite3.DatabaseError("Synthetic preflight read failure")
            return self.connection.execute(*args)

        def close(self):
            self.connection.close()
            self.closed = True

    monkeypatch.setattr(compactor, "readonly", TrackedReader)
    try:
        if read_error:
            with pytest.raises(sqlite3.DatabaseError, match="Synthetic preflight"):
                getattr(publisher, helper)(publication["history"])
        else:
            getattr(publisher, helper)(publication["history"])
        assert len(readers) == 1 and readers[0].closed
        assert not publication["operation"].exists()
    finally:
        for reader in readers:
            reader.close()


def test_non_macos_publication_refuses_before_creating_output(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "sys", SimpleNamespace(platform="win32"))
    output = tmp_path / "unused-operation.json"
    with pytest.raises(ValueError, match="requires macOS"):
        publisher.publish([], [], output)
    assert not output.exists()


def test_publication_preserves_original_inodes_and_exact_view_output(publication):
    result = _publish(publication)
    assert result["status"] == "published_originals_retained"
    assert result["files"][0]["source"] == str(publication["history"])
    assert result["files"][0]["complete_view_content"]["messages"]["rows"] == 1
    for row in result["files"]:
        assert compactor.identity(row["source"]) == row["new_identity"]
        assert compactor.identity(row["candidate"]) == row["old_identity"]
        assert compactor.sha256(row["candidate"]) == row["old_sha256"]
    info = publisher._info(publication["sidecar"])
    assert json.loads(info["history_file_identity"]) == compactor.identity(publication["history"])
    assert all(path.exists() for path in publication["backups"])


def test_failure_after_both_swaps_restores_original_inodes_and_sidecar_pin(publication, monkeypatch):
    sync = publisher._sync_pair
    calls = 0

    def fail_once(left, right):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Injected durability failure after the second swap")
        sync(left, right)

    monkeypatch.setattr(publisher, "_sync_pair", fail_once)
    with pytest.raises(OSError, match="Injected"):
        _publish(publication)
    receipt = json.loads(publication["operation"].read_text())
    assert receipt["status"] == "failed_originals_restored"
    assert all(row["state"] == "rolled_back_original_inode_restored" for row in receipt["files"])
    for row in receipt["files"]:
        assert compactor.identity(row["source"]) == row["old_identity"]
        assert compactor.sha256(row["source"]) == row["old_sha256"]
    assert json.loads(publisher._info(publication["sidecar"])["history_file_identity"]) == compactor.identity(publication["history"])


@pytest.mark.parametrize("failure", ["backup_bytes", "source_hash", "missing_backup", "missing_current_sidecar"])
def test_preservation_and_pin_failures_block_all_swaps(publication, monkeypatch, failure):
    swaps = []
    monkeypatch.setattr(publisher, "_swap", lambda left, right: swaps.append((left, right)))
    if failure == "backup_bytes":
        backup = json.loads(publication["backups"][0].read_text())
        Path(backup["archive"]).write_bytes(b"Changed preserved artifact")
    elif failure == "source_hash":
        path = publication["backups"][0]
        backup = json.loads(path.read_text())
        backup["source_sha256"] = "0" * 64
        path.write_text(json.dumps(backup))
    elif failure == "missing_backup":
        publication["backups"].pop()
    else:
        publication["receipts"].pop()
    with pytest.raises(ValueError):
        _publish(publication)
    assert not swaps
    assert not publication["operation"].exists()


def test_writer_or_view_content_mismatch_blocks_swaps(publication, monkeypatch):
    monkeypatch.setattr(publisher, "_assert_no_writers", lambda path: (_ for _ in ()).throw(ValueError("Active writer")))
    with pytest.raises(ValueError, match="Active writer"):
        _publish(publication)
    monkeypatch.setattr(publisher, "_assert_no_writers", lambda path: [])
    monkeypatch.setattr(publisher, "_view_content", lambda path: {"path_specific_mismatch": str(path)})
    with pytest.raises(ValueError, match="view output"):
        _publish(publication)
    assert not publication["operation"].exists()


def test_existing_cloud_only_sidecar_blocks_primary_publication_before_any_swap(publication, monkeypatch):
    cold = publication["history"].with_name("media_events.sqlite3")
    cold.write_bytes(b"Synthetic placeholder metadata; never read as SQLite")
    original_stat = Path.stat

    def cloud_stat(path, *args, **kwargs):
        state = original_stat(path, *args, **kwargs)
        if path == cold:
            values = {name: getattr(state, name) for name in dir(state) if name.startswith("st_")}
            values["st_flags"] = getattr(state, "st_flags", 0) | 0x40000000
            return SimpleNamespace(**values)
        return state

    swaps = []
    monkeypatch.setattr(Path, "stat", cloud_stat)
    monkeypatch.setattr(publisher, "_swap", lambda left, right: swaps.append((left, right)))
    with pytest.raises(ValueError, match="Cloud-only"):
        _publish(publication)
    assert not swaps
    assert not publication["operation"].exists()


def test_truly_missing_optional_sidecars_do_not_block_primary_publication(publication):
    history = publication["history"]
    assert not os.path.lexists(history.with_name("media_events.sqlite3"))
    assert not os.path.lexists(history.with_name("resource_revalidated.sqlite3"))
    result = _publish(publication)
    assert result["status"] == "published_originals_retained"


@pytest.mark.parametrize("omit_current_sidecar", [False, True])
def test_verified_device_refresh_preserves_historical_current_pin_gate(publication, monkeypatch, omit_current_sidecar):
    old_identity = compactor.identity(publication["history"])
    identity = compactor.identity
    monkeypatch.setattr(compactor, "identity", lambda path: {**identity(path), "dev": identity(path)["dev"] + 1})
    output = publication["operation"].parent / "mount-refreshed"
    output.mkdir()
    result = compactor.refresh_mount(publication["receipts"], output)
    publication["receipts"] = [Path(item["receipt"]) for item in result["receipts"]]
    stale = publication["history"].with_name("media_events.sqlite3")
    with closing(sqlite3.connect(stale)) as connection, connection:
        connection.execute("CREATE TABLE archive_info(key TEXT PRIMARY KEY,value TEXT)")
        connection.execute("INSERT INTO archive_info VALUES('history_file_identity',?)", (json.dumps({**old_identity, "inode": old_identity["inode"] + 100}),))
    stale_sha = compactor.sha256(stale)
    if omit_current_sidecar:
        publication["receipts"].pop()
        with pytest.raises(ValueError, match="every currently pinned"):
            _publish(publication)
        assert not publication["operation"].exists()
    else:
        assert _publish(publication)["status"] == "published_originals_retained"
        assert json.loads(publisher._info(publication["sidecar"])["history_file_identity"]) == compactor.identity(publication["history"])
    assert compactor.sha256(stale) == stale_sha


def test_invalid_mount_identity_evidence_blocks_publication_before_swap(publication, monkeypatch):
    path = publication["receipts"][0]
    report = json.loads(path.read_text())
    report["mount_identity_refresh"] = {
        "previous_source_identity": {**report["source_identity"], "dev": report["source_identity"]["dev"] - 1, "inode": report["source_identity"]["inode"] + 1},
        "source_sha256": report["source_sha256"], "verified_source_bytes": True,
        "prior_receipt_sha256": "0" * 64,
    }
    path.write_text(json.dumps(report))
    monkeypatch.setattr(publisher, "_swap", lambda *args: pytest.fail("Invalid mount evidence must never reach a swap."))
    with pytest.raises(ValueError, match="mount identity"):
        _publish(publication)
    assert not publication["operation"].exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="renamex_np is a macOS filesystem API.")
def test_native_atomic_swap_and_reverse_preserve_both_original_inode_identities(tmp_path):
    left, right = tmp_path / "left.sqlite3", tmp_path / "right.sqlite3"
    left.write_bytes(b"Original independent left evidence")
    right.write_bytes(b"Original independent right evidence")
    original_left, original_right = compactor.identity(left), compactor.identity(right)
    publisher._swap(left, right)
    assert compactor.identity(left) == original_right
    assert compactor.identity(right) == original_left
    publisher._swap(left, right)
    assert compactor.identity(left) == original_left
    assert compactor.identity(right) == original_right
