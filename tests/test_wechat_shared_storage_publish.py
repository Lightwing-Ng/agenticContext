"""Verify reversible publication and source-bound sidecar identity updates.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_shared_storage_publish as publisher
from app.core.wechat_archive import (
    _file_identity,
    wechat_archive_path,
    wechat_history_partition_fingerprint,
)
from tests import test_wechat_shared_storage as storage_fixtures


shared_archive = storage_fixtures.shared_archive
private_archive = storage_fixtures.private_archive
private_batch_archive = storage_fixtures.private_batch_archive


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _messages(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection, connection:
        return connection.execute("SELECT * FROM messages ORDER BY id").fetchall()


def _sidecar_info(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection, connection:
        return dict(connection.execute("SELECT key,value FROM archive_info"))


def _sidecar(path, history, *, stale=False):
    with closing(sqlite3.connect(history)) as source, source:
        fingerprint = wechat_history_partition_fingerprint(source)
    identity = _file_identity(history)
    if stale:
        identity = {**identity, "inode": identity["inode"] + 1}
    info = {
        "schema_version": "1",
        "history_file_identity": json.dumps(identity, sort_keys=True),
        "history_partitions_sha256": fingerprint,
        "synthetic_evidence": "Preserve every unrelated sidecar value exactly.",
    }
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            "CREATE TABLE archive_info(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            "CREATE TABLE events(id TEXT PRIMARY KEY,evidence TEXT NOT NULL);"
        )
        connection.executemany("INSERT INTO archive_info VALUES(?,?)", info.items())
        connection.execute("INSERT INTO events VALUES('original-event','Exact original sidecar evidence')")
    return info


@pytest.fixture
def publication(shared_archive, tmp_path, monkeypatch):
    source, conversations, identifiers = shared_archive
    source.commit()
    store = tmp_path / "store"
    history = wechat_archive_path(store)
    history.parent.mkdir(parents=True)
    with closing(sqlite3.connect(history)) as output, output:
        source.backup(output)
    # Tiny synthetic databases exercise publication independently of the APFS backend.
    monkeypatch.setattr(publisher, "_clonefile", shutil.copyfile)
    return SimpleNamespace(
        store=store,
        history=history,
        conversations=conversations,
        identifiers=identifiers,
        backup=tmp_path / "rollback-evidence",
        original_digest=_digest(history),
        original_messages=_messages(history),
    )


def _publish(publication):
    return publisher.publish_shared_group_bodies(
        publication.store,
        publication.conversations,
        backup_directory=publication.backup,
    )


def test_private_publication_keeps_exact_rollback_and_complementary_source_records(
    private_archive, tmp_path, monkeypatch
):
    source, conversations, _ = private_archive
    source.commit()
    store = tmp_path / "private-store"
    history = wechat_archive_path(store)
    history.parent.mkdir(parents=True)
    with closing(sqlite3.connect(history)) as output, output:
        source.backup(output)
    original_digest = _digest(history)
    original_messages = _messages(history)
    backup = tmp_path / "private-rollback"
    monkeypatch.setattr(publisher, "_clonefile", shutil.copyfile)

    report = publisher.publish_shared_conversation_bodies(
        store, conversations, backup_directory=backup
    )

    assert report["changed"]
    assert report["observations"] == 5
    assert sorted(report["conversation_counts"].values()) == [2, 3]
    assert report["shared_payloads"] == 1
    assert report["shared_observations"] == 2
    assert report["payload_bytes_saved"] > 0
    assert report["filesystem_bytes_reclaimed"] == 0
    assert _messages(history) == original_messages
    assert _digest(backup / history.name) == original_digest
    receipt = json.loads((backup / "receipt.json").read_text())
    assert receipt["backups"][history.name] == original_digest
    repeated = publisher.publish_shared_conversation_bodies(
        store, conversations, backup_directory=tmp_path / "unused-private-rollback"
    )
    assert not repeated["changed"]
    assert repeated["observations_to_rewrite"] == 0
    assert not (tmp_path / "unused-private-rollback").exists()


def test_batch_private_publication_clones_once_and_checks_one_integrity_result(
    private_batch_archive, tmp_path, monkeypatch
):
    source, scopes, _group_conversations, common = private_batch_archive
    source.commit()
    store = tmp_path / "batch-private-store"
    history = wechat_archive_path(store)
    history.parent.mkdir(parents=True)
    with closing(sqlite3.connect(history)) as output, output:
        source.backup(output)
    original_digest = _digest(history)
    original_messages = _messages(history)
    backup = tmp_path / "batch-private-rollback"
    clones = []
    integrity_queries = []
    hashed = []
    staged = []
    original_connect = sqlite3.connect
    original_sha256 = publisher._sha256
    original_stage = publisher._stage

    def tracked_clone(origin, destination):
        clones.append((Path(origin), Path(destination)))
        return shutil.copyfile(origin, destination)

    def tracked_sha256(path):
        hashed.append(Path(path))
        return original_sha256(path)

    def tracked_stage(path):
        staged.append(Path(path))
        return original_stage(path)

    class TrackedConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement.strip().casefold().startswith("pragma quick_check"):
                integrity_queries.append(statement)
            return super().execute(statement, *args, **kwargs)

    def tracked_connect(*args, **kwargs):
        kwargs.setdefault("factory", TrackedConnection)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(publisher, "_clonefile", tracked_clone)
    monkeypatch.setattr(publisher, "_sha256", tracked_sha256)
    monkeypatch.setattr(publisher, "_stage", tracked_stage)
    monkeypatch.setattr(publisher.sqlite3, "connect", tracked_connect)
    report = publisher.publish_shared_conversation_scopes(
        store, scopes, backup_directory=backup
    )

    assert report["changed"]
    assert report["observations"] == 8
    assert len(report["scopes"]) == 2
    assert report["incremental_payload_bytes_saved"] == 3 * (
        len(common.encode()) + len(common.casefold().encode())
    )
    assert len(clones) == 2
    assert all(origin == history for origin, _destination in clones)
    assert hashed == [history, backup / history.name]
    assert staged == [history]
    assert len(integrity_queries) == 1
    assert _messages(history) == original_messages
    assert _digest(backup / history.name) == original_digest
    receipt = json.loads((backup / "receipt.json").read_text())
    assert receipt["analysis"]["incremental_payload_bytes_saved"] == report["incremental_payload_bytes_saved"]
    assert not list(history.parent.glob(".wechat-share-*"))


def test_partial_batch_compaction_failure_never_publishes_a_partial_private_union(
    private_batch_archive, tmp_path, monkeypatch
):
    from app.core.wechat_shared_storage import compact_shared_conversation_bodies

    source, scopes, _group_conversations, _common = private_batch_archive
    source.commit()
    store = tmp_path / "failed-batch-store"
    history = wechat_archive_path(store)
    history.parent.mkdir(parents=True)
    with closing(sqlite3.connect(history)) as output, output:
        source.backup(output)
    original_digest = _digest(history)
    original_messages = _messages(history)
    backup = tmp_path / "failed-batch-rollback"

    def fail_after_first_scope(connection, incoming_scopes):
        compact_shared_conversation_bodies(connection, incoming_scopes[0])
        raise ValueError("Injected second private scope failure")

    monkeypatch.setattr(publisher, "_clonefile", shutil.copyfile)
    monkeypatch.setattr(publisher, "compact_shared_conversation_scopes", fail_after_first_scope)
    with pytest.raises(ValueError, match="Injected second private scope failure"):
        publisher.publish_shared_conversation_scopes(store, scopes, backup_directory=backup)

    assert _digest(history) == original_digest
    assert _messages(history) == original_messages
    assert _digest(backup / history.name) == original_digest
    with closing(sqlite3.connect(history)) as connection, connection:
        assert connection.execute("SELECT type FROM sqlite_master WHERE name='messages'").fetchone()[0] == "table"
    assert not list(history.parent.glob(".wechat-share-*"))


def test_publication_retains_exact_original_and_all_source_observations(publication):
    result = _publish(publication)

    assert result["changed"]
    assert result["observations"] == 5
    assert sorted(result["conversation_counts"].values()) == [2, 3]
    assert result["shared_payloads"] == 1
    assert result["shared_observations"] == 2
    assert result["payload_bytes_saved"] > 0
    assert result["filesystem_bytes_reclaimed"] == 0
    assert _messages(publication.history) == publication.original_messages
    assert _digest(publication.backup / publication.history.name) == publication.original_digest
    receipt = json.loads((publication.backup / "receipt.json").read_text())
    assert receipt["backups"][publication.history.name] == publication.original_digest
    assert _messages(publication.backup / publication.history.name) == publication.original_messages
    assert all("absent" in status for status in result["sidecars"].values())
    assert not list(publication.history.parent.glob(".wechat-share-*"))


def test_publication_rerun_does_not_clone_rewrite_or_create_another_backup(publication, monkeypatch):
    sidecar = publication.history.with_name("resource_references.sqlite3")
    _sidecar(sidecar, publication.history)
    _publish(publication)
    history_digest, sidecar_digest = _digest(publication.history), _digest(sidecar)
    history_identity, sidecar_identity = _file_identity(publication.history), _file_identity(sidecar)
    unused_backup = publication.backup.with_name("unused-rerun-backup")

    def forbidden_clone(*args, **kwargs):
        pytest.fail("An already compacted group must not clone any database.")

    monkeypatch.setattr(publisher, "_clonefile", forbidden_clone)
    result = publisher.publish_shared_group_bodies(
        publication.store,
        publication.conversations,
        backup_directory=unused_backup,
    )

    assert result["changed"] is False
    assert result["observations_to_rewrite"] == 0
    assert result["shared_payloads"] == 1
    assert result["shared_observations"] == 2
    assert not unused_backup.exists()
    assert _digest(publication.history) == history_digest
    assert _digest(sidecar) == sidecar_digest
    assert _file_identity(publication.history) == history_identity
    assert _file_identity(sidecar) == sidecar_identity
    assert not list(publication.history.parent.glob(".wechat-share-*"))


@pytest.mark.parametrize("fresh_name", ["resource_references.sqlite3", "media_events.sqlite3"])
def test_only_fresh_local_sidecar_pins_are_rebound(publication, fresh_name):
    stale_name = next(
        name
        for name in ("resource_references.sqlite3", "media_events.sqlite3")
        if name != fresh_name
    )
    fresh = publication.history.with_name(fresh_name)
    stale = publication.history.with_name(stale_name)
    original_info = _sidecar(fresh, publication.history)
    _sidecar(stale, publication.history, stale=True)
    fresh_digest, stale_digest = _digest(fresh), _digest(stale)
    result = _publish(publication)

    rebound = _sidecar_info(fresh)
    assert json.loads(rebound.pop("history_file_identity")) == _file_identity(publication.history)
    assert rebound == {key: value for key, value in original_info.items() if key != "history_file_identity"}
    assert _digest(publication.backup / fresh_name) == fresh_digest
    assert _digest(stale) == stale_digest
    assert not (publication.backup / stale_name).exists()
    assert "rebound" in result["sidecars"][fresh_name]
    assert "already stale" in result["sidecars"][stale_name]
    with closing(sqlite3.connect(fresh)) as connection, connection:
        assert connection.execute("SELECT * FROM events").fetchall() == [
            ("original-event", "Exact original sidecar evidence")
        ]


def test_cloud_only_sidecar_is_skipped_without_sqlite_access(publication, monkeypatch):
    sidecar = publication.history.with_name("resource_references.sqlite3")
    sidecar.write_bytes(b"Synthetic cloud placeholder; must never be opened as SQLite.")
    original_digest = _digest(sidecar)
    original_stat = Path.stat

    def placeholder_stat(path, *args, **kwargs):
        actual = original_stat(path, *args, **kwargs)
        if path == sidecar:
            return SimpleNamespace(st_mode=actual.st_mode, st_flags=0x40000000)
        return actual

    monkeypatch.setattr(Path, "stat", placeholder_stat)
    result = _publish(publication)
    assert "cloud-only" in result["sidecars"][sidecar.name]
    assert not (publication.backup / sidecar.name).exists()
    assert _digest(sidecar) == original_digest
    assert _messages(publication.history) == publication.original_messages


def test_stage_scope_corruption_aborts_before_live_publication(publication, monkeypatch):
    original_stage = publisher._stage

    def damaged_stage(path):
        staged = original_stage(path)
        if path == publication.history:
            with closing(sqlite3.connect(staged)) as connection, connection:
                connection.execute("UPDATE messages SET text=text || ' changed stage evidence'")
        return staged

    monkeypatch.setattr(publisher, "_stage", damaged_stage)
    with pytest.raises(ValueError, match="staged group scope"):
        _publish(publication)
    assert _digest(publication.history) == publication.original_digest
    assert _digest(publication.backup / publication.history.name) == publication.original_digest
    assert not list(publication.history.parent.glob(".wechat-share-*"))


def test_live_source_drift_is_preserved_and_not_overwritten(publication, monkeypatch):
    original_sync = publisher._sync
    drifted = {}

    def source_drift(path):
        original_sync(path)
        if not drifted:
            with closing(sqlite3.connect(publication.history)) as connection, connection:
                connection.execute("INSERT INTO archive_info VALUES('concurrent-writer','Retain this new evidence')")
            drifted["digest"] = _digest(publication.history)

    monkeypatch.setattr(publisher, "_sync", source_drift)
    with pytest.raises(ValueError, match="changed before"):
        _publish(publication)
    assert _digest(publication.history) == drifted["digest"]
    assert _messages(publication.history) == publication.original_messages
    assert _digest(publication.backup / publication.history.name) == publication.original_digest
    with closing(sqlite3.connect(publication.history)) as connection, connection:
        assert connection.execute(
            "SELECT value FROM archive_info WHERE key='concurrent-writer'"
        ).fetchone()[0] == "Retain this new evidence"
        assert connection.execute(
            "SELECT type FROM sqlite_master WHERE name='messages'"
        ).fetchone()[0] == "table"


def test_partial_publication_failure_restores_history_and_all_fresh_sidecar_pins(publication, monkeypatch):
    names = ("resource_references.sqlite3", "media_events.sqlite3")
    originals = {}
    for name in names:
        path = publication.history.with_name(name)
        info = _sidecar(path, publication.history)
        originals[name] = (_digest(path), info)
    original_replace = publisher.os.replace
    failed = False

    def fail_final_sidecar(source, destination):
        nonlocal failed
        if Path(destination) == publication.history.with_name("media_events.sqlite3") and not failed:
            failed = True
            raise OSError("Injected sidecar publication failure")
        return original_replace(source, destination)

    monkeypatch.setattr(publisher.os, "replace", fail_final_sidecar)
    with pytest.raises(OSError, match="Injected sidecar publication failure"):
        _publish(publication)

    assert failed
    assert _messages(publication.history) == publication.original_messages
    assert _digest(publication.backup / publication.history.name) == publication.original_digest
    with closing(sqlite3.connect(publication.history)) as connection, connection:
        assert connection.execute("SELECT type FROM sqlite_master WHERE name='messages'").fetchone()[0] == "table"
    for name, (digest, original_info) in originals.items():
        assert _digest(publication.backup / name) == digest
        current = _sidecar_info(publication.history.with_name(name))
        assert json.loads(current.pop("history_file_identity")) == _file_identity(publication.history)
        assert current == {key: value for key, value in original_info.items() if key != "history_file_identity"}
    assert not list(publication.history.parent.glob(".wechat-share-*"))
