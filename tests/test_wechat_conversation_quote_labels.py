"""Historical quote labels require the same proved original and reply event.

Code version: v1.0.1-codex.0
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


event_store = fixtures.event_store
HISTORICAL_LABEL = "Historical remark"
CURRENT_LABEL = "Current remark"


def _historical_quote(fixture, mutation=None):
    inherited = {
        "unknown_target_server",
        "wrong_quoted_author",
        "different_current_author",
        "different_current_direction",
        "different_reply",
        "ambiguous_html_target",
        "unproven_target",
        "reply_precedes_native_target",
    }
    html_id, native_id = quotes._add_quote(
        fixture, mutation if mutation in inherited else None
    )
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "UPDATE messages SET sender_label=? WHERE id IN (?,?)",
            (CURRENT_LABEL, *fixture.ids[0]),
        )
        html = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (html_id,)
            ).fetchone()
        )
        native = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (native_id,)
            ).fetchone()
        )
        html_proof = json.loads(html["provenance"])
        native_proof = json.loads(native["provenance"])
        old_envelope = html_proof["quoted_components"][0]["text"]
        envelope = old_envelope.replace("Source label", HISTORICAL_LABEL, 1)
        html_proof["quoted_components"][0]["text"] = envelope
        html["text"] = envelope + html["text"][len(old_envelope) :]
        native_label = (
            "Another historical remark"
            if mutation == "different_native_envelope_label"
            else HISTORICAL_LABEL
        )
        native_proof["quoted_reference"]["source_sender_label"] = native_label
        native_proof["text_components"][1]["source_sender_label"] = native_label
        if mutation == "blank_native_envelope_label":
            native_proof["quoted_reference"]["source_sender_label"] = ""
            native_proof["text_components"][1]["source_sender_label"] = ""
        elif mutation == "missing_native_envelope_label":
            native_proof["quoted_reference"].pop("source_sender_label")
            native_proof["text_components"][1].pop("source_sender_label")
        elif mutation == "conflicting_native_envelope_labels":
            native_proof["text_components"][1]["source_sender_label"] = (
                "Contradictory component remark"
            )
        elif mutation == "different_current_clock":
            html["timestamp_value"] = "2020-01-01T08:00:05"
            html["timestamp_label"] = "1 Jan 2020 08:00:05"
            html_proof["source_time"] = "2020-01-01 08:00:05"
        elif mutation == "native_reply_server_conflict":
            additional = deepcopy(native)
            proof = deepcopy(native_proof)
            proof.update(
                record_id="another-native-quoted-reply",
                server_id="922337203685477598",
            )
            proof["locator"]["local_id"] = 198
            additional.update(
                id=_identifier(fixtures.NATIVE, proof["record_id"]),
                ordinal=106,
                provenance=json.dumps(proof),
            )
            quotes._insert(connection, additional)
        elif mutation == "native_quote_from_different_snapshot":
            snapshot = connection.execute(
                "SELECT source_id,metadata FROM source_snapshots WHERE id=2"
            ).fetchone()
            connection.execute(
                "INSERT INTO source_snapshots VALUES(?,?,?,?)",
                (3, "another-ipad-snapshot", *snapshot),
            )
            native_proof["source_snapshot"] = 3
        elif mutation == "duplicate_html_reply":
            additional = deepcopy(html)
            proof = deepcopy(html_proof)
            proof.update(record_id="another-html-quoted-reply", source_order=6)
            proof["locator"]["export_msgid"] = "100"
            additional.update(
                id=hashlib.sha256(b"another-html-quoted-reply").hexdigest(),
                ordinal=6,
                provenance=json.dumps(proof),
            )
            quotes._insert(connection, additional)
        for row, proof in ((html, html_proof), (native, native_proof)):
            connection.execute(
                "UPDATE messages SET text=?,search_text=?,provenance=?,"
                "timestamp_value=?,timestamp_label=? WHERE id=?",
                (
                    row["text"],
                    row["text"].casefold(),
                    json.dumps(proof),
                    row["timestamp_value"],
                    row["timestamp_label"],
                    row["id"],
                ),
            )
    return html_id, native_id


def test_historical_quote_label_uses_proved_native_original(event_store):
    html_id, native_id = _historical_quote(event_store)
    before = event_store.repository.path.read_bytes(), event_store.registry.read_bytes()
    connection, result = fixtures._run(event_store)
    try:
        assert result["status"] == "ready"
        assert result["preferred_by_id"] == {
            **dict(event_store.ids),
            html_id: native_id,
        }
        assert result["display_count"] == 4
        members = result["members_by_preferred"][native_id]
        assert {item["id"] for item in members} == {html_id, native_id}
        assert {item["quoted_original_id"] for item in members} == {
            event_store.ids[0][1]
        }
        assert {item["quoted_contact_id"] for item in members} == {
            canonical_contact_id(fixtures.PEER, authority="native_message_sender")
        }
        assert {item["contact_id"] for item in members} == {
            canonical_contact_id(fixtures.OWNER, authority="native_message_sender")
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
    cache = events._cache_path(event_store.repository, fixtures.SESSION).read_text()
    assert quotes.REPLY not in cache and quotes.QUOTED not in cache
    assert hashlib.sha256(quotes.REPLY.encode()).hexdigest() not in cache


@pytest.mark.parametrize(
    "mutation",
    [
        "different_native_envelope_label",
        "blank_native_envelope_label",
        "missing_native_envelope_label",
        "conflicting_native_envelope_labels",
        "unknown_target_server",
        "wrong_quoted_author",
        "different_current_author",
        "different_current_direction",
        "different_reply",
        "different_current_clock",
        "ambiguous_html_target",
        "unproven_target",
        "reply_precedes_native_target",
        "native_reply_server_conflict",
        "native_quote_from_different_snapshot",
        "duplicate_html_reply",
    ],
)
def test_historical_label_cannot_replace_event_identity(event_store, mutation):
    html_id, native_id = _historical_quote(event_store, mutation)
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
