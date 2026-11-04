"""Resource notices follow the selected source without hiding relevant failures.

Code version: v1.0.0-codex.0
"""

import hashlib
import json
import sqlite3

import pytest

from app.core.wechat_archive import (
    WechatArchiveRepository,
    _SCHEMA,
    _file_identity,
    _identifier,
    wechat_history_partition_fingerprint,
)
from tests import test_wechat_media_event_global_timeline as media


global_media_store = media.global_media_store
placeholder_store = media.placeholder_store


def _native_catalog(fixture, state):
    path = fixture.history.with_name("resource_references.sqlite3")
    if state == "invalid":
        path.write_text("Invalid optional native catalog")
        return
    pin = _file_identity(fixture.history)
    if state == "stale":
        pin = {**pin, "size": pin["size"] + 1}
    with sqlite3.connect(fixture.history) as primary, sqlite3.connect(path) as index:
        fingerprint = wechat_history_partition_fingerprint(primary)
        index.executescript(_SCHEMA)
        index.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("history_file_identity", json.dumps(pin)),
                ("history_partitions_sha256", fingerprint),
                ("report", json.dumps({"accounts": []})),
            ],
        )


@pytest.mark.parametrize("native_state", ["ready", "stale", "invalid"])
def test_html_session_retains_complete_media_without_native_catalog_notices(
    global_media_store, native_state
):
    fixture = global_media_store
    _native_catalog(fixture, native_state)
    repository = WechatArchiveRepository(fixture.store)
    spec = fixture.specs[0]
    pages = [
        repository.query(session=spec.conversation, page=page, page_size=400)
        for page in (1, 2)
    ]
    assert all(page["resource_index_status"] == "ready" for page in pages)
    assert all(page["resource_index_accounts"] is None for page in pages)
    assert all(page["count"] == 738 for page in pages)
    rows = [row for page in pages for row in page["messages"]]
    assert len(rows) == len({row["id"] for row in rows}) == 738
    assert sum(row["text"] == "[Image]" for row in rows) == 720


@pytest.mark.parametrize("html_state", ["absent", "stale", "invalid"])
def test_html_session_reports_its_own_missing_or_broken_metadata(
    global_media_store, html_state
):
    fixture = global_media_store
    _native_catalog(fixture, "stale")
    fixture.overlay.unlink()
    if html_state == "absent":
        fixture.index.unlink()
    else:
        with sqlite3.connect(fixture.index) as index:
            if html_state == "stale":
                pin = {**_file_identity(fixture.history), "size": 0}
                index.execute(
                    "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
                    (json.dumps(pin),),
                )
            else:
                index.execute("UPDATE archive_info SET value='0' WHERE key='published'")
    page = WechatArchiveRepository(fixture.store).query(
        session=fixture.specs[0].conversation
    )
    assert page["resource_index_status"] == html_state
    assert page["resource_index_accounts"] is None
    assert page["count"] == 18


def test_invalid_legacy_overlay_is_not_hidden_by_ready_html_metadata(global_media_store):
    fixture = global_media_store
    _native_catalog(fixture, "stale")
    fixture.overlay.write_text("Invalid optional HTML overlay")
    page = WechatArchiveRepository(fixture.store).query(
        session=fixture.specs[0].conversation
    )
    assert page["resource_index_status"] == "invalid"
    assert page["count"] == 738


def test_media_only_html_scope_uses_its_verified_source_notice(global_media_store):
    fixture = global_media_store
    spec = fixture.specs[0]
    fixture.overlay.unlink()
    with sqlite3.connect(fixture.history) as primary:
        primary.execute("DELETE FROM messages WHERE conversation_id=?", (spec.conversation,))
        primary.execute("DELETE FROM conversations WHERE id=?", (spec.conversation,))
    with sqlite3.connect(fixture.index) as index:
        index.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture.history)),),
        )
    _native_catalog(fixture, "stale")
    page = WechatArchiveRepository(fixture.store).query(session=spec.conversation)
    assert page["resource_index_status"] == "ready"
    assert page["resource_index_accounts"] is None
    assert page["count"] == 720
    assert page["current"]["partition_id"] == spec.source


def test_native_session_and_global_view_keep_applicable_catalog_failure(global_media_store):
    fixture = global_media_store
    owner = "wxid_notice_native_owner"
    partition_id = "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    conversation_id = "wechat:" + _identifier(partition_id, "wxid_notice_peer")
    with sqlite3.connect(fixture.history) as primary:
        primary.row_factory = sqlite3.Row
        partition = dict(primary.execute("SELECT * FROM partitions LIMIT 1").fetchone())
        conversation = dict(primary.execute("SELECT * FROM conversations LIMIT 1").fetchone())
        partition.update(id=partition_id, account_id=owner, account_key="account:" + owner)
        conversation.update(
            id=conversation_id,
            partition_id=partition_id,
            account_key="account:" + owner,
            peer_id="wxid_notice_peer",
        )
        media._insert(primary, "partitions", partition)
        media._insert(primary, "conversations", conversation)
    with sqlite3.connect(fixture.index) as index:
        index.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture.history)),),
        )
    _native_catalog(fixture, "stale")
    repository = WechatArchiveRepository(fixture.store)
    assert repository.query(session=conversation_id)["resource_index_status"] == "stale"
    assert repository.query()["resource_index_status"] == "stale"
    assert repository.query(session=fixture.specs[0].conversation)["resource_index_status"] == "ready"
