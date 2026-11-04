"""Assert child server identities without substituting forwarding envelope IDs.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_archive import _identifier
from app.core.wechat_forwarded_origins import link_forwarded_origins
from tests import test_wechat_native_html_precedence as fixtures


CID = fixtures.CID
TARGET = fixtures.grouping.TARGET
statement_store = fixtures.statement_store
native_html_store = fixtures.native_html_store


@pytest.fixture
def explicit_server_store(native_html_store):
    fixture = native_html_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        original = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.native_ids[0],)
            ).fetchone()
        )
        conversation = dict(
            connection.execute(
                "SELECT * FROM conversations WHERE id=?", (original["conversation_id"],)
            ).fetchone()
        )
        receiving_peer = "synthetic-receiving@chatroom"
        receiving = {
            **conversation,
            "id": "wechat:" + _identifier(fixture.native_source, receiving_peer),
            "peer_id": receiving_peer,
            "title": "Receiving group",
            "kind": "group",
            "message_count": 1,
        }
        columns, marks = ",".join(receiving), ",".join("?" for _ in receiving)
        connection.execute(
            f"INSERT INTO conversations({columns}) VALUES({marks})",
            tuple(receiving.values()),
        )
        proof = json.loads(original["provenance"])
        source_server = proof["server_id"]
        clock = datetime.fromisoformat(original["timestamp_value"]) + timedelta(days=1)
        parent_id = _identifier(fixture.native_source, "explicit-server-packet")
        component = {
            "kind": "forwarded_text",
            "text": original["text"],
            "source_sender_id": TARGET,
            "source_sender_label": "An independent remark",
            "source_timestamp": "2020-08-12 13:46:01",
            "source_create_time": "",
            "source_data_id": hashlib.md5(
                b"Synthetic child source identity"
            ).hexdigest(),
            "source_ids": {"dataitemsource/msgid": source_server},
        }
        proof.update(
            record_id="explicit-server-packet",
            server_id="8999999999999999999",
            message_type=49,
            appmsg_subtype=19,
            text_extraction_kind="forwarded_text",
            text_components=[component],
            source_time=clock.isoformat(),
            unix_timestamp=int(clock.timestamp()),
        )
        proof["locator"]["table"] = (
            "Chat_" + hashlib.md5(receiving_peer.encode()).hexdigest()
        )
        parent = {
            **original,
            "id": parent_id,
            "conversation_id": receiving["id"],
            "ordinal": 1,
            "sender_id": fixture.owner,
            "direction": "sent",
            "timestamp_value": clock.isoformat(),
            "time_sort": clock.timestamp(),
            "provenance": json.dumps(proof),
        }
        fixtures._insert_message(connection, parent)
    return SimpleNamespace(
        **vars(fixture),
        parent_id=parent_id,
        child_id=parent_id + "_forwarded_1",
        server=source_server,
        original_conversation=original["conversation_id"],
    )


def _change_child(fixture, **values):
    fixtures._change_provenance(
        fixture,
        fixture.parent_id,
        lambda proof: proof["text_components"][0].update(values),
    )


def _items(fixture):
    page = analysis.query_contact_analysis(fixture.store, CID)
    return page, {
        item["id"]: item
        for item in analysis.export_contact_analysis_items(
            fixture.store,
            CID,
            source_fingerprint=page["source_fingerprint"],
            source_occurrences=True,
        )
    }


@pytest.mark.parametrize(
    "field", ["dataitemsource/msgid", "svrid", "srcMsgSvrId", "dataitemsource/svrid"]
)
def test_exact_child_server_id_keeps_one_native_original_and_every_source(
    explicit_server_store, field
):
    fixture = explicit_server_store
    _change_child(fixture, source_ids={field: fixture.server})
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page, items = _items(fixture)
    child, original = items[fixture.child_id], items[fixture.native_ids[0]]
    assert page["total_count"] == 6
    assert page["observation_count"] == 13
    assert child["statement_id"] == original["statement_id"]
    assert child["timestamp_value"] == "2020-08-12T13:46:01"
    assert child["original_conversation_unknown"]
    assert child["parent_message_id"] == fixture.parent_id
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize(
    "values",
    [
        {"source_ids": {"dataitemsource/msgid": "7111111111111111111"}},
        {"source_ids": {}},
        {"source_ids": {"srcMsgLocalId": "7000000000000000001"}},
        {"source_ids": {"svrid": "7000000000000000001", "dataitemsource/msgid": "2"}},
        {"source_ids": {"svrid": "07000000000000000001"}},
        {"text": "A different literal body"},
        {"source_timestamp": "2020-08-12 13:46:02"},
        {"source_timestamp": "2020-08-12 13:46"},
        {"source_timestamp": "2020-08-12T13:46:01+07:00"},
        {"source_create_time": "1597211162"},
        {"source_create_time": "0"},
        {"source_create_time": "not-an-epoch"},
        {"source_chat_id": "different-original@chatroom"},
    ],
)
def test_contradictory_or_missing_child_evidence_stays_independent(
    explicit_server_store, values
):
    fixture = explicit_server_store
    _change_child(fixture, **values)
    _, items = _items(fixture)
    assert (
        items[fixture.child_id]["statement_id"]
        != (items[fixture.native_ids[0]]["statement_id"])
    )


def test_forwarding_parent_server_id_is_never_an_original_child_id(
    explicit_server_store,
):
    fixture = explicit_server_store
    _change_child(fixture, source_ids={})
    fixtures._change_provenance(
        fixture, fixture.parent_id, lambda proof: proof.update(server_id=fixture.server)
    )
    _, items = _items(fixture)
    assert (
        items[fixture.child_id]["statement_id"]
        != (items[fixture.native_ids[0]]["statement_id"])
    )


@pytest.mark.parametrize("field", ["svrid", "srcMsgSvrId", "dataitemsource/svrid"])
@pytest.mark.parametrize(
    "damage", [{"source_timestamp": "2020-08-12 13:46:02"}, {"source_create_time": "0"}]
)
def test_recognized_server_fields_do_not_override_child_clock_conflicts(
    explicit_server_store, field, damage
):
    fixture = explicit_server_store
    _change_child(fixture, source_ids={field: fixture.server}, **damage)
    _, items = _items(fixture)
    assert (
        items[fixture.child_id]["statement_id"]
        != (items[fixture.native_ids[0]]["statement_id"])
    )


@pytest.mark.parametrize("difference", ["body", "clock", "conversation"])
def test_ambiguous_native_server_identity_is_rejected_before_candidate_filtering(
    explicit_server_store, difference
):
    fixture = explicit_server_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        duplicate = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.native_ids[0],)
            ).fetchone()
        )
        duplicate["id"] = "d" * 64
        if difference == "body":
            duplicate["text"] = "Conflicting original text"
            duplicate["search_text"] = duplicate["text"].casefold()
        elif difference == "clock":
            proof = json.loads(duplicate["provenance"])
            proof["unix_timestamp"] += 1
            duplicate["provenance"] = json.dumps(proof)
            duplicate["timestamp_value"] = "2020-08-12T13:46:02+08:00"
        else:
            conversation = dict(
                connection.execute(
                    "SELECT * FROM conversations WHERE id=?",
                    (fixture.original_conversation,),
                ).fetchone()
            )
            conversation.update(
                id="wechat:" + "a" * 64, peer_id="other@chatroom", kind="group"
            )
            columns, marks = ",".join(conversation), ",".join("?" for _ in conversation)
            connection.execute(
                f"INSERT INTO conversations({columns}) VALUES({marks})",
                tuple(conversation.values()),
            )
            duplicate["conversation_id"] = conversation["id"]
        fixtures._insert_message(connection, duplicate)
    _, items = _items(fixture)
    assert (
        items[fixture.child_id]["statement_id"]
        != (items[fixture.native_ids[0]]["statement_id"])
    )


def test_a_different_native_author_cannot_inherit_the_child(explicit_server_store):
    fixture = explicit_server_store
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE messages SET sender_id='wxid_other_author' WHERE id=?",
            (fixture.native_ids[0],),
        )
    _, items = _items(fixture)
    assert fixture.native_ids[0] not in items
    assert (
        items[fixture.child_id]["statement_id"]
        != (items[fixture.html_ids[0]]["statement_id"])
    )


def test_another_source_account_cannot_supply_the_native_original(
    explicit_server_store,
):
    fixture = explicit_server_store
    other = fixture.rows[1]
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        copied = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.native_ids[0],)
            ).fetchone()
        )
        connection.execute("DELETE FROM messages WHERE id=?", (fixture.native_ids[0],))
        metadata = {
            "source_id": other["source_id"],
            "source_account_id": other["account_id"],
            "account_hash_verified": True,
            "account_hash": hashlib.md5(other["account_id"].encode()).hexdigest(),
            "source_label": "iPad archive",
        }
        snapshot = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            ("another-account-snapshot", other["source_id"], json.dumps(metadata)),
        ).lastrowid
        proof = json.loads(copied["provenance"])
        proof["source_snapshot"] = snapshot
        copied.update(id="d" * 64, conversation_id=other["conversation_id"])
        copied["provenance"] = json.dumps(proof)
        fixtures._insert_message(connection, copied)
    _, items = _items(fixture)
    assert (
        items[fixture.child_id]["statement_id"] != (items[copied["id"]]["statement_id"])
    )


def test_matching_explicit_epoch_and_original_chat_remain_valid(explicit_server_store):
    fixture = explicit_server_store
    _change_child(
        fixture,
        source_create_time="1597211161",
        source_chat_id=TARGET,
        source_ids={"dataitemsource/msgid": fixture.server, "svrid": fixture.server},
    )
    _, items = _items(fixture)
    assert (
        items[fixture.child_id]["statement_id"]
        == (items[fixture.native_ids[0]]["statement_id"])
    )


def test_warm_explicit_link_moves_the_whole_existing_group(explicit_server_store):
    fixture = explicit_server_store
    _items(fixture)
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    with (
        analysis._readonly(fixture.history) as source,
        sqlite3.connect(cache) as output,
    ):
        target = output.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?",
            (fixture.native_ids[0],),
        ).fetchone()[0]
        old = "b" * 64
        output.execute(
            "INSERT INTO statement_groups VALUES(?,?,?)",
            (old, fixture.child_id, "old-copy"),
        )
        output.execute(
            "UPDATE statement_members SET statement_id=? WHERE reference_id IN (?,?)",
            (old, fixture.child_id, fixture.html_ids[0]),
        )
        link_forwarded_origins(
            source,
            output,
            CID,
            row_select=analysis._ROW_SELECT,
            prepare_rows=analysis._requests,
            native_thread_key=analysis._native_conversation_key,
            native_statement_key=analysis._native_statement_key,
            link_originals=False,
        )
        assert set(
            output.execute(
                "SELECT statement_id FROM statement_members WHERE reference_id IN (?,?)",
                (fixture.child_id, fixture.html_ids[0]),
            )
        ) == {(target,)}
        assert not output.execute(
            "SELECT 1 FROM statement_groups WHERE id=?", (old,)
        ).fetchone()


def test_a_preexisting_conflicting_native_original_group_is_not_reassigned(
    explicit_server_store,
):
    fixture = explicit_server_store
    _items(fixture)
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    with (
        analysis._readonly(fixture.history) as source,
        sqlite3.connect(cache) as output,
    ):
        existing = output.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?",
            (fixture.native_ids[1],),
        ).fetchone()[0]
        output.execute(
            "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
            (existing, fixture.child_id),
        )
        link_forwarded_origins(
            source,
            output,
            CID,
            row_select=analysis._ROW_SELECT,
            prepare_rows=analysis._requests,
            native_thread_key=analysis._native_conversation_key,
            native_statement_key=analysis._native_statement_key,
            link_originals=False,
        )
        assert (
            output.execute(
                "SELECT statement_id FROM statement_members WHERE reference_id=?",
                (fixture.child_id,),
            ).fetchone()[0]
            == existing
        )
