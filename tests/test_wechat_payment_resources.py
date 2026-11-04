"""Restored payment text supersedes optional native app placeholders safely.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import shutil
import sqlite3
import sys

import pytest

from app.core import wechat_native_restore as restore
from app.core.wechat_archive import WechatArchiveRepository, _file_identity
from app.core.wechat_resource_index import (
    _existing_observations,
    build_resource_index,
    resource_index_path,
)
from tests import test_wechat_resource_index as resource_fixtures
from tests.test_wechat_native_restore import record, source
from tests.test_wechat_resource_index import (
    BACKUP,
    NOW,
    OWNER,
    SERVER,
    _table,
)


resources = resource_fixtures.resources
_LOCAL_ID = 13
_PAYMENT_TEXT = "WeChat transfer\nTransfer to fixture recipient\n¥8.00"


def _payment(fixture, *, server_id):
    metadata = source(OWNER)
    clock = NOW + 3
    observation = record(
        metadata,
        "native-payment-app-text",
        text=_PAYMENT_TEXT,
        message_type=49,
        text_extraction_kind="app_text",
        appmsg_subtype=2000,
        server_id=str(server_id),
        sent_at_utc_iso=datetime.fromtimestamp(clock, UTC).isoformat(),
        sent_at_raw=str(clock),
        unix_timestamp=clock,
        source_locator={
            "database": fixture.source.name,
            "table": _table("peer-direct"),
            "local_id": _LOCAL_ID,
            "row_id": _LOCAL_ID,
        },
    )
    with sqlite3.connect(fixture.source) as connection:
        connection.execute(
            f'UPDATE "{_table("peer-direct")}" SET MesSvrID=? WHERE MesLocalID=?',
            (server_id, _LOCAL_ID),
        )
    return metadata, observation


def _restore(fixture, monkeypatch, metadata, observation, *, snapshot=BACKUP):
    if sys.platform != "darwin":
        # Only synthetic test databases use a copy backend on non-APFS CI hosts.
        monkeypatch.setattr(restore, "_clonefile", shutil.copyfile)
    return restore.restore_wechat_text_subset(
        fixture.store,
        sources=[metadata],
        records=iter([observation]),
        snapshot_id=snapshot,
        apply=True,
    )


@pytest.mark.parametrize(
    "server_id,snapshot",
    [(SERVER - 3, "another-native-snapshot"), (0, BACKUP)],
    ids=["positive-server-across-snapshots", "exact-snapshot-local-id"],
)
def test_rebuilt_resource_index_excludes_restored_payment_by_native_identity(
    resources, monkeypatch, server_id, snapshot
):
    fixture = resources
    metadata, observation = _payment(fixture, server_id=server_id)
    source_before = fixture.source.read_bytes()
    result = _restore(
        fixture, monkeypatch, metadata, observation, snapshot=snapshot
    )
    assert result["published"] and result["inserted_messages"] == 1
    history_before = fixture.history.read_bytes()
    with sqlite3.connect(fixture.history) as connection:
        conversation = connection.execute(
            "SELECT id FROM conversations WHERE account_key=? AND peer_id=?",
            ("account:" + OWNER, "peer-direct"),
        ).fetchone()[0]
        snapshots = dict(
            connection.execute("SELECT id,snapshot_id FROM source_snapshots")
        )
        servers, locators = _existing_observations(
            connection, conversation, BACKUP, snapshots, metadata["source_id"]
        )
        if server_id:
            assert str(server_id) in servers
        locator = (fixture.source.name, _table("peer-direct"), _LOCAL_ID)
        assert (locator in locators) == (snapshot == BACKUP)
    report = build_resource_index(fixture.store, fixture.snapshot)
    assert report["messages"] == 8
    assert report["excluded_existing_records"] == 1
    assert report["media_payload_files_read"] == 0
    with sqlite3.connect(resource_index_path(fixture.store)) as connection:
        for (encoded,) in connection.execute("SELECT provenance FROM messages"):
            provenance = json.loads(encoded)
            assert not (
                provenance["resource_reference"]["peer_id"] == "peer-direct"
                and provenance["locator"]["local_id"] == _LOCAL_ID
            )
    page = WechatArchiveRepository(fixture.store).query(session=conversation)
    assert page["resource_index_status"] == "ready"
    assert [item["text"] for item in page["messages"]].count(_PAYMENT_TEXT) == 1
    assert "[App message]" not in [item["text"] for item in page["messages"]]
    assert fixture.history.read_bytes() == history_before
    assert fixture.source.read_bytes() == source_before


def test_atomic_payment_restore_invalidates_previously_published_resource_index(
    resources, monkeypatch
):
    fixture = resources
    metadata, observation = _payment(fixture, server_id=SERVER - 3)
    build_resource_index(fixture.store, fixture.snapshot)
    sidecar = resource_index_path(fixture.store)
    sidecar_before = sidecar.read_bytes()
    repository = WechatArchiveRepository(fixture.store)
    session = next(
        item
        for item in repository.query(account="account:" + OWNER)["conversations"]
        if item["peer_id"] == "peer-direct"
    )
    initial = repository.query(session=session["id"])
    assert initial["resource_index_status"] == "ready"
    assert [item["text"] for item in initial["messages"]].count("[App message]") == 1
    identity = _file_identity(fixture.history)
    result = _restore(fixture, monkeypatch, metadata, observation)
    assert result["published"] and result["inserted_messages"] == 1
    assert _file_identity(fixture.history)["inode"] != identity["inode"]
    page = repository.query(session=session["id"])
    assert page["resource_index_status"] == "stale"
    assert page["count"] == page["current"]["message_count"] == 2
    assert [item["text"] for item in page["messages"]].count(_PAYMENT_TEXT) == 1
    assert "[App message]" not in [item["text"] for item in page["messages"]]
    assert sidecar.read_bytes() == sidecar_before
