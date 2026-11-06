"""Group quote events resolve original servers before interpreting room roles.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_conversation_events as events
from app.core.wechat_archive import _identifier
from app.core.wechat_contacts import canonical_contact_id
from tests import test_wechat_conversation_events as fixtures
from tests import test_wechat_conversation_quote_events as quotes
from tests.test_wechat_contacts import bind, insert_contact


event_store = fixtures.event_store
ROOM = "23525319954@chatroom"
LEGACY_AUTHOR = "makeping2007"
REPLY_AUTHOR = "wxid_group_reply_author"
HISTORICAL_LABEL = "Historical quoted author"


def _group_quote(fixture, mutation=None):
    html_id, native_id = quotes._add_quote(fixture)
    with sqlite3.connect(fixture.registry) as connection:
        for sender in (LEGACY_AUTHOR, REPLY_AUTHOR):
            contact = insert_contact(connection, sender)
            bind(connection, contact, source=fixtures.HTML, sender="alias-" + sender)
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "UPDATE conversations SET kind='group',peer_id=? WHERE id=?",
            (ROOM, fixtures.SESSION),
        )
        for original_html, original_native in (fixture.ids[0], fixture.ids[2]):
            connection.execute(
                "UPDATE messages SET sender_id=?,sender_label='Current remark' WHERE id=?",
                ("alias-" + LEGACY_AUTHOR, original_html),
            )
            connection.execute(
                "UPDATE messages SET sender_id=?,sender_label='Current remark' WHERE id=?",
                (LEGACY_AUTHOR, original_native),
            )
        for row in connection.execute("SELECT * FROM messages").fetchall():
            row = dict(row)
            proof = json.loads(row["provenance"])
            native = proof["source_snapshot"] == 2
            if native:
                proof["locator"]["table"] = (
                    "Chat_" + hashlib.md5(ROOM.encode()).hexdigest()
                )
            if row["id"] in {html_id, native_id}:
                row["sender_id"] = REPLY_AUTHOR if native else "alias-" + REPLY_AUTHOR
                row["sender_label"] = "Current reply author"
                row["direction"] = "received"
                if native:
                    reference = proof["quoted_reference"]
                    reference.update(
                        source_sender_id=ROOM,
                        source_chat_id=REPLY_AUTHOR,
                        source_sender_label=HISTORICAL_LABEL,
                    )
                    if mutation == "wrong_room":
                        reference["source_sender_id"] = "999999@chatroom"
                    elif mutation == "unrelated_chat":
                        reference["source_chat_id"] = "wxid_unrelated_person"
                    elif mutation == "missing_server":
                        reference["source_ids"] = {}
                    elif mutation == "unknown_server":
                        reference["source_ids"] = {"svrid": "999999999999"}
                    elif mutation == "explicit_wrong_author":
                        reference.update(
                            source_sender_id=REPLY_AUTHOR, source_chat_id=ROOM
                        )
                    elif mutation == "explicit_original_author":
                        reference.update(
                            source_sender_id=LEGACY_AUTHOR, source_chat_id=ROOM
                        )
                    elif mutation == "chat_is_original_author":
                        reference["source_chat_id"] = LEGACY_AUTHOR
                    elif mutation == "invalid_room_table":
                        proof["locator"]["table"] = (
                            "Chat_" + hashlib.md5(b"999999@chatroom").hexdigest()
                        )
                    elif mutation == "different_snapshot":
                        snapshot = connection.execute(
                            "SELECT source_id,metadata FROM source_snapshots WHERE id=2"
                        ).fetchone()
                        connection.execute(
                            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
                            (3, "unrelated-native-snapshot", *snapshot),
                        )
                        proof["source_snapshot"] = 3
                    elif mutation == "reply_precedes_original":
                        row["ordinal"] = 99
                    proof["text_components"][1].update(deepcopy(reference))
                    if mutation == "conflicting_component_chat":
                        proof["text_components"][1]["source_chat_id"] = LEGACY_AUTHOR
                else:
                    envelope = "「" + HISTORICAL_LABEL + "：" + quotes.QUOTED + "」"
                    proof["quoted_components"][0]["text"] = envelope
                    row["text"] = envelope + "\n\n" + quotes.REPLY
                    if mutation == "different_current_direction":
                        row["direction"] = "sent"
                    elif mutation == "different_current_author":
                        row["sender_id"] = "alias-" + LEGACY_AUTHOR
                    elif mutation == "different_current_clock":
                        row["timestamp_value"] = "2020-01-01T08:00:05"
                        proof["source_time"] = "2020-01-01 08:00:05"
            connection.execute(
                "UPDATE messages SET sender_id=?,sender_label=?,direction=?,text=?,"
                "search_text=?,provenance=?,ordinal=?,timestamp_value=? WHERE id=?",
                (
                    row["sender_id"],
                    row["sender_label"],
                    row["direction"],
                    row["text"],
                    row["text"].casefold(),
                    json.dumps(proof),
                    row["ordinal"],
                    row["timestamp_value"],
                    row["id"],
                ),
            )
        if mutation in {"ambiguous_html_original", "duplicate_native_reply"}:
            source_id = (
                fixture.ids[0][0]
                if mutation == "ambiguous_html_original"
                else native_id
            )
            additional = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (source_id,)
                ).fetchone()
            )
            proof = json.loads(additional["provenance"])
            if mutation == "ambiguous_html_original":
                additional.update(
                    id=hashlib.sha256(b"another-html-original").hexdigest(), ordinal=4
                )
                proof.update(source_order=4)
            else:
                proof.update(
                    record_id="duplicate-native-quote", server_id="922337203685477598"
                )
                additional.update(
                    id=_identifier(fixtures.NATIVE, proof["record_id"]), ordinal=106
                )
            additional["provenance"] = json.dumps(proof)
            quotes._insert(connection, additional)
    return html_id, native_id


@pytest.mark.parametrize(
    "role", [None, "explicit_original_author", "chat_is_original_author"]
)
def test_group_quote_prefers_ipad_and_resolves_legacy_original(event_store, role):
    html_id, native_id = _group_quote(event_store, role)
    before = event_store.repository.path.read_bytes(), event_store.registry.read_bytes()
    connection, result = fixtures._run(event_store)
    try:
        assert result["status"] == "ready"
        assert result["preferred_by_id"] == {
            **dict(event_store.ids),
            html_id: native_id,
        }
        members = result["members_by_preferred"][native_id]
        assert {item["quoted_original_id"] for item in members} == {
            event_store.ids[0][1]
        }
        assert {item["quoted_sender_id"] for item in members} == {LEGACY_AUTHOR}
        assert {item["quoted_contact_id"] for item in members} == {
            canonical_contact_id(LEGACY_AUTHOR, authority="native_message_sender")
        }
        assert {item["contact_id"] for item in members} == {
            canonical_contact_id(REPLY_AUTHOR, authority="native_message_sender")
        }
        table = events.canonical_message_table(connection, "main.messages", result)
        retained = {row[0] for row in connection.execute("SELECT id FROM " + table)}
        assert native_id in retained and html_id not in retained
        assert event_store.ids[0][1] in retained
    finally:
        connection.close()
    assert before == (
        event_store.repository.path.read_bytes(),
        event_store.registry.read_bytes(),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_room",
        "unrelated_chat",
        "missing_server",
        "unknown_server",
        "explicit_wrong_author",
        "invalid_room_table",
        "different_snapshot",
        "reply_precedes_original",
        "conflicting_component_chat",
        "different_current_direction",
        "different_current_author",
        "different_current_clock",
        "ambiguous_html_original",
        "duplicate_native_reply",
    ],
)
def test_group_quote_never_uses_a_label_instead_of_original_identity(
    event_store, mutation
):
    html_id, native_id = _group_quote(event_store, mutation)
    before = event_store.repository.path.read_bytes(), event_store.registry.read_bytes()
    connection, result = fixtures._run(event_store)
    try:
        assert result["status"] == "ready"
        assert html_id not in result["preferred_by_id"]
        assert native_id not in result["preferred_by_id"]
        table = events.canonical_message_table(connection, "main.messages", result)
        retained = {row[0] for row in connection.execute("SELECT id FROM " + table)}
        assert {html_id, native_id} <= retained
    finally:
        connection.close()
    assert before == (
        event_store.repository.path.read_bytes(),
        event_store.registry.read_bytes(),
    )
