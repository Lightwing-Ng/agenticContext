"""Version v1.0.1: Keep optional cloud resource indices out of text-only reads."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


@pytest.fixture(scope="module")
def archive_module():
    # The repository reader uses only the standard library. Do not load providers.
    source = Path(__file__).resolve().parents[1] / "app/core/wechat_archive.py"
    specification = importlib.util.spec_from_file_location(
        "app.core.wechat_cloud_storage_target", source
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture
def indexed_archive(tmp_path, archive_module):
    root = tmp_path / "local_store"
    history = archive_module.wechat_archive_path(root)
    history.parent.mkdir(parents=True)
    owner = "wxid_cloud_storage_test"
    partition = "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    account = "account:" + owner
    peer = "cloud-storage-test@chatroom"
    conversation = "wechat:" + archive_module._identifier(partition, peer)
    timestamp = "2026-10-31T12:00:00+08:00"
    conversation_row = (
        conversation,
        partition,
        account,
        peer,
        "A preserved group",
        "group",
        1,
        100.0,
        100.0,
        timestamp,
        timestamp,
        "Preserved ordinary text.",
    )
    main_message = (
        "a" * 64,
        conversation,
        1,
        "wxid_peer",
        "Peer",
        "received",
        "Preserved ordinary text.",
        100.0,
        timestamp,
        "31 Oct 2026 12:00:00",
        "source_local",
        "{}",
        "[]",
        "preserved ordinary text.",
    )
    with sqlite3.connect(history) as connection:
        connection.executescript(archive_module._SCHEMA)
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                partition,
                account,
                owner,
                "Source account",
                "Verified archive ID",
                "iPhone archive",
                "Native receipt",
                "Native payload",
            ),
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            conversation_row,
        )
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", main_message
        )
        fingerprint = archive_module.wechat_history_partition_fingerprint(connection)
    resource = history.with_name("resource_references.sqlite3")
    with sqlite3.connect(resource) as connection:
        connection.executescript(archive_module._SCHEMA)
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (*conversation_row[:7], 200.0, 200.0, timestamp, timestamp, "[Image]"),
        )
        provenance = {"resource_reference": {"account_id": owner, "peer_id": peer}}
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "b" * 64,
                conversation,
                2,
                "wxid_peer",
                "Peer",
                "received",
                "[Image]",
                200.0,
                timestamp,
                "31 Oct 2026 12:00:00",
                "source_local",
                json.dumps(provenance),
                "[]",
                "[image]",
            ),
        )
        connection.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                (
                    "history_file_identity",
                    json.dumps(archive_module._file_identity(history)),
                ),
                ("history_partitions_sha256", fingerprint),
            ],
        )
    return root, history, resource, conversation, account


def _watch_resource_access(monkeypatch, archive_module, resource, *, deny_attach):
    attempts = []
    connect = sqlite3.connect
    open_path = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == resource:
            raise AssertionError("A text query opened the cloud resource file.")
        return open_path(path, *args, **kwargs)

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)

        def authorize(action, first, second, database, origin):
            if action == sqlite3.SQLITE_ATTACH:
                attempts.append(first)
                if deny_attach:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(archive_module.sqlite3, "connect", tracked_connect)
    return attempts


def _cloud_stat(monkeypatch, resource, enabled):
    original = Path.stat

    class CloudState:
        def __init__(self, state):
            self.state = state
            self.st_flags = getattr(state, "st_flags", 0) | 0x40000000

        def __getattr__(self, name):
            return getattr(self.state, name)

    def inspect(path, *args, **kwargs):
        state = original(path, *args, **kwargs)
        return CloudState(state) if path == resource and enabled() else state

    monkeypatch.setattr(Path, "stat", inspect)


def test_cloud_resource_is_not_opened_by_conversation_or_text_reads(
    monkeypatch, archive_module, indexed_archive
):
    root, history, resource, conversation, account = indexed_archive
    original_history = history.read_bytes()
    original_resource_identity = archive_module._file_identity(resource)
    _cloud_stat(monkeypatch, resource, lambda: True)
    attempts = _watch_resource_access(
        monkeypatch, archive_module, resource, deny_attach=True
    )
    repository = archive_module.WechatArchiveRepository(root)
    listing = repository.query(account=account)
    detail = repository.query(account=account, session=conversation)

    assert attempts == []
    assert (
        listing["resource_index_status"] == detail["resource_index_status"] == "absent"
    )
    assert listing["message_count"] == detail["message_count"] == 1
    assert listing["conversation_count"] == 1
    assert detail["messages"][0]["text"] == "Preserved ordinary text."
    assert detail["messages"][0]["id"] == "a" * 64
    assert history.read_bytes() == original_history
    assert archive_module._file_identity(resource) == original_resource_identity


def test_a_downloaded_resource_index_is_used_without_restarting_the_repository(
    monkeypatch, archive_module, indexed_archive
):
    root, _, resource, conversation, account = indexed_archive
    cloud_only = [True]
    _cloud_stat(monkeypatch, resource, lambda: cloud_only[0])
    attempts = _watch_resource_access(
        monkeypatch, archive_module, resource, deny_attach=False
    )
    repository = archive_module.WechatArchiveRepository(root)
    deferred = repository.query(account=account, session=conversation)
    assert deferred["resource_index_status"] == "absent"
    assert deferred["message_count"] == 1
    assert attempts == []

    cloud_only[0] = False
    resident = repository.query(account=account, session=conversation)
    assert resident["resource_index_status"] == "ready"
    assert resident["message_count"] == 2
    assert [row["id"] for row in resident["messages"]] == ["a" * 64, "b" * 64]
    assert len(attempts) == 1
