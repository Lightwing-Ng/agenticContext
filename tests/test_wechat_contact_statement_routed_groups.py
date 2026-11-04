"""Native statement groups retain verified source routing and reject weak scopes.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_archive import _identifier
from tests import test_wechat_contact_statement_groups as grouping


CID = grouping.CID
BODY = grouping.BODY
statement_store = grouping.statement_store


@pytest.fixture
def routed_group_store(statement_store):
    """Keep one native observation in a same-owner HTML conversation container."""
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        original = dict(connection.execute(
            analysis._ROW_SELECT + "WHERE m.id=?", (fixture.rows[0]["id"],)
        ).fetchone())
        owner = original["account_id"]
        account_hash = hashlib.md5(owner.encode()).hexdigest()
        source_id = original["source_id"]
        peer = original["peer_id"]
        container = "html-" + hashlib.sha256(b"group-container").hexdigest()[:24]
        conversation = "wechat:" + _identifier(container, peer)
        metadata = {
            "source_id": source_id,
            "source_account_id": owner,
            "account_hash": account_hash,
            "account_hash_verified": True,
            "source_label": "iPad archive",
            "source_file": "Original native checkpoint",
        }
        connection.execute(
            "CREATE TABLE IF NOT EXISTS source_snapshots("
            "id INTEGER PRIMARY KEY,snapshot_id TEXT,source_id TEXT,metadata TEXT)"
        )
        snapshot = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            ("group-native-routed", source_id, json.dumps(metadata)),
        ).lastrowid
        provenance = json.loads(original["provenance"])
        provenance.update(
            source_snapshot=snapshot,
            locator={
                "database_relative_path": f"Documents/{account_hash}/DB/msg.sqlite",
                "database": "msg.sqlite",
                "table": "Chat_" + hashlib.md5(peer.encode()).hexdigest(),
                "rowid": 17,
            },
        )
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                container, original["account_key"], owner, "Owner HTML export",
                "Corroborated export ID", "HTML export", "Original export.zip", "",
            ),
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conversation, container, original["account_key"], peer,
                "Canonical shared group", "group", 1,
                original["time_sort"], original["time_sort"],
                original["timestamp_value"], original["timestamp_value"], BODY,
            ),
        )
        connection.execute(
            "UPDATE messages SET conversation_id=?,provenance=? WHERE id=?",
            (conversation, json.dumps(provenance), original["id"]),
        )
    return SimpleNamespace(
        **vars(fixture), routed_message=original["id"], source_id=source_id,
        container_source=container, container_conversation=conversation,
        snapshot=snapshot,
    )


def test_routed_native_group_observations_share_one_statement(routed_group_store):
    fixture = routed_group_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID, page_size=1, page=2)
    assert page["total_count"] == 1
    assert page["current_page"] == page["total_pages"] == 1
    assert page["observation_count"] == 2
    assert page["counts"] == {
        "messages": 1, "conversations": 1, "source_accounts": 2, "sources": 2,
    }
    item, = page["items"]
    assert item["content_text"] == BODY
    assert item["observation_count"] == item["source_account_count"] == 2
    routed = next(
        observation for observation in item["observations"]
        if observation["id"] == fixture.routed_message
    )
    assert routed["source_id"] == fixture.source_id
    assert routed["source_id"] != fixture.container_source
    assert routed["conversation_id"] == fixture.container_conversation
    assert routed["source_file"] == "Original native checkpoint"
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_routed_members_group_after_legacy_reference_migration(
    routed_group_store, monkeypatch
):
    fixture = routed_group_store
    grouping._legacy_cache(fixture)

    def no_scan(*args, **kwargs):
        raise AssertionError("A valid routed reference cache must migrate directly.")

    monkeypatch.setattr(analysis, "_candidate_batches", no_scan)
    page = analysis.query_contact_analysis(fixture.store, CID, query="Synthetic")
    assert page["total_count"] == 1
    assert page["observation_count"] == 2
    assert page["items"][0]["source_account_count"] == 2


@pytest.mark.parametrize("field,value", [
    ("identity_status", "Source account unconfirmed"),
    ("account_key", "account:wxid_different_owner"),
])
def test_unverified_native_partition_scope_does_not_aggregate(
    statement_store, field, value
):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            f"UPDATE partitions SET {field}=? WHERE id=?",
            (value, fixture.rows[0]["source_id"]),
        )
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == page["observation_count"] == 2
    assert all(item["observation_count"] == 1 for item in page["items"])


@pytest.mark.parametrize("tamper", ["owner", "table", "container_account"])
def test_tampered_routed_native_proof_cannot_publish_groups(
    routed_group_store, tamper
):
    fixture = routed_group_store
    with sqlite3.connect(fixture.history) as connection:
        if tamper == "owner":
            metadata = json.loads(connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?",
                (fixture.snapshot,),
            ).fetchone()[0])
            metadata["source_account_id"] = "wxid_different_owner"
            connection.execute(
                "UPDATE source_snapshots SET metadata=? WHERE id=?",
                (json.dumps(metadata), fixture.snapshot),
            )
        elif tamper == "table":
            provenance = json.loads(connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.routed_message,)
            ).fetchone()[0])
            provenance["locator"]["table"] = "Chat_" + "0" * 32
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(provenance), fixture.routed_message),
            )
        else:
            connection.execute(
                "UPDATE partitions SET account_key='account:wxid_different_owner' WHERE id=?",
                (fixture.container_source,),
            )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    with pytest.raises(ValueError, match="snapshot crosses its source partition"):
        analysis.query_contact_analysis(fixture.store, CID)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    assert not analysis.contact_analysis_cache_path(fixture.store, CID).exists()
