"""Proven cross-source session events preserve original rows and uncertain repeats.

Code version: v1.0.0-codex.0
"""

from datetime import datetime
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_conversation_events as events
from app.core.wechat_archive import _SCHEMA, _identifier, WechatArchiveRepository
from app.core.wechat_contacts import (
    canonical_contact_id,
    contact_registry_path,
    initialize_contact_registry,
)
from tests.test_wechat_contacts import bind, insert_contact


OWNER = "wxid_event_owner"
PEER = "wxid_event_peer"
HTML = "html-synthetic-events"
NATIVE = "ios-" + hashlib.md5(OWNER.encode()).hexdigest()
SESSION = "wechat:" + hashlib.sha256(b"event-session").hexdigest()


@pytest.fixture
def event_store(tmp_path):
    store = tmp_path / "store"
    repository = WechatArchiveRepository(store)
    repository.path.parent.mkdir(parents=True)
    with sqlite3.connect(repository.path) as connection:
        connection.executescript(_SCHEMA)
        connection.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,source_id TEXT,metadata TEXT)"
        )
        connection.executemany(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    HTML,
                    "account:" + OWNER,
                    OWNER,
                    "Export",
                    "Corroborated export ID",
                    "HTML export",
                    "Synthetic.zip",
                    "",
                ),
                (
                    NATIVE,
                    "account:" + OWNER,
                    OWNER,
                    "Native",
                    "Verified archive ID",
                    "iPad archive",
                    "Synthetic iPad",
                    "",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            [
                (
                    1,
                    "html-snapshot",
                    HTML,
                    json.dumps(
                        {
                            "source_id": HTML,
                            "source_account_id": OWNER,
                            "owner_verified": True,
                            "owner_evidence": {"native_username": OWNER},
                        }
                    ),
                ),
                (
                    2,
                    "ipad-snapshot",
                    NATIVE,
                    json.dumps(
                        {
                            "source_id": NATIVE,
                            "source_account_id": OWNER,
                            "account_hash_verified": True,
                            "account_hash": hashlib.md5(OWNER.encode()).hexdigest(),
                            "source_label": "iPad archive",
                        }
                    ),
                ),
            ],
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                SESSION,
                HTML,
                "account:" + OWNER,
                PEER,
                "Synthetic conversation",
                "direct",
                6,
                1,
                3,
                "",
                "",
                "",
            ),
        )
        ids = []
        for index in range(3):
            sender = PEER if index % 2 == 0 else OWNER
            direction = "received" if sender == PEER else "sent"
            wall = f"2020-01-01T08:00:0{index}"
            body = f"Private event body {index}; preserve  two spaces"
            html_id = hashlib.sha256(("html-" + str(index)).encode()).hexdigest()
            record = "native-" + str(index)
            native_id = _identifier(NATIVE, record)
            provenance = {
                "record_id": "html-record-" + str(index),
                "source_snapshot": 1,
                "source_time": wall.replace("T", " "),
                "source_order": index + 1,
                "text_extraction_kind": "plain_text",
                "primary_text_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "locator": {
                    "export_msgtype": "1",
                    "export_msgid": str((3, 1, 2)[index]),
                },
            }
            native = {
                "record_id": record,
                "source_snapshot": 2,
                "message_type": 1,
                "text_extraction_kind": "plain_text",
                "server_id": "92233720368547758" + str(index),
                "unix_timestamp": int(
                    datetime.fromisoformat(wall + "+08:00").timestamp()
                ),
                "source_time": None,
                "locator": {
                    "database": "message.sqlite",
                    "database_relative_path": f"Documents/{hashlib.md5(OWNER.encode()).hexdigest()}/DB/message.sqlite",
                    "table": "Chat_" + hashlib.md5(PEER.encode()).hexdigest(),
                    "local_id": 100 + index,
                },
            }
            for identifier, ordinal, source_sender, clock, proof, basis in [
                (
                    html_id,
                    index + 1,
                    "alias-" + sender,
                    wall,
                    provenance,
                    "Source clock; timezone unknown",
                ),
                (native_id, index + 101, sender, wall + "+08:00", native, "UTC+08:00"),
            ]:
                connection.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        SESSION,
                        ordinal,
                        source_sender,
                        "Source label",
                        direction,
                        body,
                        index + 1,
                        clock,
                        clock,
                        basis,
                        json.dumps(proof),
                        "[]",
                        body.casefold(),
                    ),
                )
            ids.append((html_id, native_id))
    registry = contact_registry_path(store)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        for sender in (OWNER, PEER):
            contact = insert_contact(connection, sender)
            bind(connection, contact, source=HTML, sender="alias-" + sender)
    return SimpleNamespace(
        store=store, repository=repository, registry=registry, ids=ids
    )


def _run(fixture):
    connection = sqlite3.connect(
        fixture.repository.path.as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    current = dict(
        connection.execute(
            "SELECT c.*,p.account_id,p.identity_status FROM conversations c JOIN partitions p ON p.id=c.partition_id WHERE c.id=?",
            (SESSION,),
        ).fetchone()
    )
    result = events.conversation_events(
        fixture.repository, connection, SESSION, current, "main.messages"
    )
    return connection, result


def test_unique_source_sequence_prefers_ipad_without_local_id_equivalence(event_store):
    fixture = event_store
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    connection, result = _run(fixture)
    try:
        assert result["status"] == "ready"
        assert result["original_count"] == 6
        assert result["display_count"] == result["count_delta"] == 3
        assert result["preferred_by_id"] == dict(fixture.ids)
        assert all(len(group) == 2 for group in result["members_by_preferred"].values())
        for index, (_, preferred) in enumerate(fixture.ids):
            group = result["members_by_preferred"][preferred]
            expected_sender = PEER if index % 2 == 0 else OWNER
            assert {item["contact_id"] for item in group} == {
                canonical_contact_id(expected_sender, authority="native_message_sender")
            }
            wall = f"2020-01-01T08:00:0{index}"
            assert {item["timestamp_value"] for item in group} == {
                wall,
                wall + "+08:00",
            }
        table = events.canonical_message_table(connection, "main.messages", result)
        assert connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 3
        assert {row[0] for row in connection.execute("SELECT id FROM " + table)} == {
            pair[1] for pair in fixture.ids
        }
    finally:
        connection.close()
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )
    cache = events._cache_path(fixture.repository, SESSION)
    assert "Private event body" not in cache.read_text()
    assert events.cache_only_summaries(fixture.repository) == {SESSION: 3}


def test_warm_cache_does_not_scan_source_messages(event_store, monkeypatch):
    connection, first = _run(event_store)
    connection.close()
    monkeypatch.setattr(
        events, "_scan", lambda *args: pytest.fail("Warm event cache must not rescan.")
    )
    connection, second = _run(event_store)
    connection.close()
    assert first == second


@pytest.mark.parametrize(
    "identifier", [922337203685477580712345, -922337203685477580712345]
)
def test_arbitrary_precision_signed_json_server_ids_remain_exact(
    event_store, identifier
):
    fixture = event_store
    with sqlite3.connect(fixture.repository.path) as connection:
        target = fixture.ids[0][1]
        proof = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (target,)
            ).fetchone()[0]
        )
        proof["server_id"] = identifier
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), target)
        )
    connection, result = _run(fixture)
    connection.close()
    assert result["preferred_by_id"] == dict(fixture.ids)


def test_explicit_html_timezone_cannot_disagree_with_native_instant(event_store):
    fixture = event_store
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.execute(
            "UPDATE messages SET timestamp_value=timestamp_value||'+00:00' WHERE id=?",
            (fixture.ids[0][0],),
        )
    connection, result = _run(fixture)
    connection.close()
    assert result["count_delta"] == 0


@pytest.mark.parametrize(
    "damage",
    [
        "zero_server",
        "float_server",
        "quote",
        "system",
        "clock",
        "body",
        "owner",
        "locator",
        "one_speaker",
        "order",
        "ambiguous",
        "conflicting_server",
        "same_server_conflicting_body",
    ],
)
def test_uncertain_or_conflicting_events_are_not_collapsed(event_store, damage):
    fixture = event_store
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        target = fixture.ids[0][1]
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (target,)
            ).fetchone()
        )
        proof = json.loads(row["provenance"])
        if damage in {"zero_server", "float_server"}:
            proof["server_id"] = (
                "0" if damage == "zero_server" else 9.223372036854776e18
            )
        elif damage in {"quote", "system"}:
            proof["text_extraction_kind"] = (
                "quoted_reply" if damage == "quote" else "system_text"
            )
        elif damage == "clock":
            proof["unix_timestamp"] += 1
        elif damage == "body":
            connection.execute(
                "UPDATE messages SET text=text||' ' WHERE id=?", (target,)
            )
        elif damage == "owner":
            metadata = json.loads(
                connection.execute(
                    "SELECT metadata FROM source_snapshots WHERE id=2"
                ).fetchone()[0]
            )
            metadata["source_account_id"] = "wxid_wrong_owner"
            connection.execute(
                "UPDATE source_snapshots SET metadata=? WHERE id=2",
                (json.dumps(metadata),),
            )
        elif damage == "locator":
            proof["locator"]["table"] = "Chat_wrong_peer"
        elif damage == "one_speaker":
            connection.execute(
                "UPDATE messages SET sender_id=CASE WHEN ordinal<100 THEN ? ELSE ? END,direction='received'",
                ("alias-" + PEER, PEER),
            )
        elif damage == "order":
            connection.execute("UPDATE messages SET ordinal=103 WHERE id=?", (target,))
        elif damage in {
            "ambiguous",
            "conflicting_server",
            "same_server_conflicting_body",
        }:
            duplicate = dict(row)
            duplicate["id"] = hashlib.sha256(b"another-source-occurrence").hexdigest()
            duplicate["ordinal"] = 1000
            if damage == "ambiguous":
                original = dict(
                    connection.execute(
                        "SELECT * FROM messages WHERE id=?", (fixture.ids[0][0],)
                    ).fetchone()
                )
                duplicate.update(original)
                duplicate["id"] = hashlib.sha256(b"another-html-occurrence").hexdigest()
            else:
                other_proof = json.loads(duplicate["provenance"])
                other_proof["record_id"] = "conflicting-native-record"
                if damage == "conflicting_server":
                    other_proof["server_id"] = "123456789"
                else:
                    duplicate["text"] += " conflicting body"
                duplicate["id"] = _identifier(NATIVE, other_proof["record_id"])
                duplicate["provenance"] = json.dumps(other_proof)
            connection.execute(
                "INSERT INTO messages("
                + ",".join(duplicate)
                + ") VALUES("
                + ",".join("?" for _ in duplicate)
                + ")",
                tuple(duplicate.values()),
            )
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), target)
        )
    connection, result = _run(fixture)
    connection.close()
    assert result["preferred_by_id"] == {}
    assert result["count_delta"] == 0


def test_registry_changes_invalidate_cache_and_cache_only_counts(event_store):
    connection, first = _run(event_store)
    connection.close()
    assert first["count_delta"] == 3
    with sqlite3.connect(event_store.registry) as connection:
        connection.execute(
            "DELETE FROM sender_bindings WHERE sender_id=?", ("alias-" + OWNER,)
        )
    assert events.cache_only_summaries(event_store.repository) == {}
    connection, second = _run(event_store)
    connection.close()
    assert second["count_delta"] == 0


def test_absent_registry_preserves_raw_counts_and_needs_no_temp_filter(event_store):
    event_store.registry.unlink()
    connection, result = _run(event_store)
    try:
        assert result["status"] == "unavailable"
        assert result["original_count"] == result["display_count"] == 6
        assert (
            events.canonical_message_table(connection, "main.messages", result)
            == "main.messages"
        )
    finally:
        connection.close()
