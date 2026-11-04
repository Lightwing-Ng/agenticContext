"""Keep native message provenance inside a verified shared conversation container.

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
from tests import test_wechat_contact_analysis as contact_fixtures


CID = contact_fixtures.CID
TARGET = contact_fixtures.TARGET
contact_analysis_store = contact_fixtures.contact_analysis_store


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.fixture
def routed_contact_store(contact_analysis_store):
    """Model the native delta routed into an independently verified HTML peer."""
    fixture = contact_analysis_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        native = dict(
            connection.execute(
                "SELECT m.*,c.partition_id,c.peer_id,c.account_key,p.account_id "
                "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                "JOIN partitions p ON p.id=c.partition_id "
                "WHERE c.kind='direct' AND m.sender_id=? AND c.partition_id LIKE 'ios-%' "
                "ORDER BY m.id LIMIT 1",
                (TARGET,),
            ).fetchone()
        )
        owner = native["account_id"]
        account_hash = hashlib.md5(owner.encode()).hexdigest()
        peer_hash = hashlib.md5(native["peer_id"].encode()).hexdigest()
        source_id = native["partition_id"]
        container_source = (
            "html-" + hashlib.sha256(b"verified-container").hexdigest()[:24]
        )
        conversation = "wechat:" + _identifier(container_source, native["peer_id"])
        metadata = {
            "source_id": source_id,
            "source_account_id": owner,
            "account_hash": account_hash,
            "account_hash_verified": True,
            "source_label": "iPad archive",
            "source_file": "Frozen iPad text",
            "snapshot_id": "synthetic-ipad-routed-delta",
        }
        snapshot = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            (metadata["snapshot_id"], source_id, _json(metadata)),
        ).lastrowid
        provenance = json.loads(native["provenance"])
        provenance.update(
            source_snapshot=snapshot,
            message_type=1,
            text_extraction_kind="plain_text",
            locator={
                "database_relative_path": f"Documents/{account_hash}/DB/msg.sqlite",
                "database": "msg.sqlite",
                "table": "Chat_" + peer_hash,
                "rowid": 41,
            },
        )
        text = "ROUTED-NATIVE-ONLY-original statement"
        connection.execute("DELETE FROM messages WHERE id<>?", (native["id"],))
        connection.execute("DELETE FROM conversations")
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                container_source,
                native["account_key"],
                owner,
                "Verified owner export",
                "Corroborated export ID",
                "HTML export",
                "Original export.zip",
                "",
            ),
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                conversation,
                container_source,
                native["account_key"],
                native["peer_id"],
                "Existing canonical peer conversation",
                "direct",
                1,
                native["time_sort"],
                native["time_sort"],
                native["timestamp_value"],
                native["timestamp_value"],
                text,
            ),
        )
        connection.execute(
            "UPDATE messages SET conversation_id=?,text=?,search_text=?,provenance=? WHERE id=?",
            (conversation, text, text.casefold(), _json(provenance), native["id"]),
        )
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute(
            "DELETE FROM sender_bindings WHERE source_id<>? OR sender_id<>?",
            (source_id, TARGET),
        )
        connection.execute("DELETE FROM message_contact_bindings")
    return SimpleNamespace(
        store=fixture.store,
        history=fixture.history,
        registry=fixture.registry,
        message_id=native["id"],
        source_id=source_id,
        container_source=container_source,
        conversation_id=conversation,
        owner=owner,
        account_key=native["account_key"],
        snapshot=snapshot,
        snapshot_id=metadata["snapshot_id"],
        text=text,
        peer=native["peer_id"],
    )


def test_routed_native_text_uses_its_own_source_binding_and_retains_the_container(
    routed_contact_store,
):
    fixture = routed_contact_store
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    result = analysis.query_contact_analysis(fixture.store, CID)
    assert result["status"] == "ready"
    assert result["total_count"] == 1
    assert result["counts"] == {
        "messages": 1,
        "conversations": 1,
        "source_accounts": 1,
        "sources": 1,
    }
    row = result["items"][0]
    assert row["id"] == row["parent_message_id"] == fixture.message_id
    assert row["conversation_id"] == fixture.conversation_id
    assert row["source_id"] == fixture.source_id
    assert row["source_id"] != fixture.container_source
    assert row["source_snapshot_id"] == fixture.snapshot_id
    assert row["account_id"] == fixture.owner
    assert row["contact_id"] == CID
    assert row["content_text"] == fixture.text
    assert result["accounts"][0]["source_id"] == fixture.source_id
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_routed_native_reference_stays_compact_and_warm_queries_do_not_rescan(
    routed_contact_store, monkeypatch
):
    fixture = routed_contact_store
    first = analysis.query_contact_analysis(fixture.store, CID)
    assert first["total_count"] == 1
    cache_path = analysis.contact_analysis_cache_path(fixture.store, CID)
    with sqlite3.connect(cache_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM refs").fetchone()[0] == 1
        assert fixture.text not in "\n".join(connection.iterdump())
        assert (
            connection.execute("SELECT source_id FROM refs").fetchone()[0]
            == fixture.source_id
        )
    assert fixture.text.encode() not in cache_path.read_bytes()

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("A warm routed query must not rebuild source references.")

    monkeypatch.setattr(analysis, "_candidate_batches", fail_scan)
    monkeypatch.setattr(analysis, "_candidate_forwarded_batches", fail_scan)
    for query in ("", "original statement"):
        warm = analysis.query_contact_analysis(fixture.store, CID, query=query)
        assert warm["status"] == "ready"
        assert warm["total_count"] == 1
        assert warm["source_fingerprint"] == first["source_fingerprint"]
        assert warm["items"][0]["source_id"] == fixture.source_id


@pytest.mark.parametrize(
    "tamper",
    (
        "container_owner",
        "container_account_key",
        "container_partition_account_key",
        "container_peer",
        "native_path",
        "native_path_traversal",
        "native_database",
        "native_table",
        "message_id",
        "record_id",
        "metadata_owner",
        "metadata_source_id",
        "unverified_metadata",
    ),
)
def test_routed_native_source_rejects_scope_or_identity_tampering(
    routed_contact_store, tamper
):
    fixture = routed_contact_store
    with sqlite3.connect(fixture.history) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.message_id,)
            ).fetchone()[0]
        )
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?", (fixture.snapshot,)
            ).fetchone()[0]
        )
        if tamper == "container_owner":
            connection.execute(
                "UPDATE partitions SET account_id='wxid_other_owner' WHERE id=?",
                (fixture.container_source,),
            )
        elif tamper == "container_account_key":
            connection.execute(
                "UPDATE conversations SET account_key='account:wxid_other_owner' WHERE id=?",
                (fixture.conversation_id,),
            )
        elif tamper == "container_partition_account_key":
            connection.execute(
                "UPDATE partitions SET account_key='account:wxid_other_owner' WHERE id=?",
                (fixture.container_source,),
            )
        elif tamper == "container_peer":
            connection.execute(
                "UPDATE conversations SET peer_id='wxid_other_peer' WHERE id=?",
                (fixture.conversation_id,),
            )
        elif tamper == "native_path":
            provenance["locator"]["database_relative_path"] = (
                "Documents/" + "0" * 32 + "/DB/msg.sqlite"
            )
        elif tamper == "native_path_traversal":
            provenance["locator"]["database_relative_path"] = (
                "Documents/"
                + hashlib.md5(fixture.owner.encode()).hexdigest()
                + "/DB/.."
            )
        elif tamper == "native_database":
            provenance["locator"]["database"] = "another.sqlite"
        elif tamper == "native_table":
            provenance["locator"]["table"] = "Chat_" + "0" * 32
        elif tamper == "message_id":
            connection.execute(
                "UPDATE messages SET id=? WHERE id=?",
                (
                    _identifier("wrong-source", provenance["record_id"]),
                    fixture.message_id,
                ),
            )
        elif tamper == "record_id":
            provenance["record_id"] = "changed-extraction-record"
        elif tamper == "metadata_owner":
            metadata["source_account_id"] = "wxid_other_owner"
        elif tamper == "metadata_source_id":
            metadata["source_id"] = fixture.container_source
        elif tamper == "unverified_metadata":
            metadata["account_hash_verified"] = False
        connection.execute("UPDATE messages SET provenance=?", (_json(provenance),))
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (_json(metadata), fixture.snapshot),
        )
    with pytest.raises(ValueError):
        analysis.query_contact_analysis(fixture.store, CID)
    assert not analysis.contact_analysis_cache_path(fixture.store, CID).exists()


def test_routed_native_variant_keeps_its_source_and_exact_variant_identity(
    routed_contact_store,
):
    fixture = routed_contact_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.message_id,)
            ).fetchone()
        )
        provenance = json.loads(row["provenance"])
        observation = {
            **provenance,
            "content_sha256": hashlib.sha256(row["text"].encode()).hexdigest(),
            "sender_id": row["sender_id"] or "",
            "sender_label": row["sender_label"],
            "direction": row["direction"],
            "timestamp_value": row["timestamp_value"],
            "time_basis": row["time_basis"],
            "flags": json.loads(row["quality_flags"]),
        }
        observation_id = _identifier(
            fixture.source_id, provenance["record_id"], _json(observation)
        )
        variant_id = _identifier(
            fixture.source_id, provenance["record_id"], "variant", observation_id
        )
        connection.execute(
            "UPDATE messages SET id=? WHERE id=?", (variant_id, fixture.message_id)
        )
    result = analysis.query_contact_analysis(fixture.store, CID)
    assert result["total_count"] == 1
    assert result["items"][0]["id"] == variant_id
    assert result["items"][0]["source_id"] == fixture.source_id
