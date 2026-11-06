"""Quoted replies reuse proven original events without confusing their authors.

Code version: v1.1.0-codex.0
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_conversation_events as events
from app.core.wechat_archive import _identifier
from app.core.wechat_contacts import canonical_contact_id
from tests import test_wechat_conversation_events as fixtures


event_store = fixtures.event_store
REPLY = "A current reply; preserve  two spaces"
QUOTED = "Private event body 0; preserve  two spaces"
WALL = "2020-01-01T08:00:04"


def _insert(connection, row):
    connection.execute(
        "INSERT INTO messages("
        + ",".join(row)
        + ") VALUES("
        + ",".join("?" for _ in row)
        + ")",
        tuple(row.values()),
    )


def _add_quote(fixture, mutation=None):
    html_id = hashlib.sha256(b"html-quoted-reply").hexdigest()
    native_id = _identifier(fixtures.NATIVE, "native-quoted-reply")
    reference = {
        "original_message_type": 1,
        "source_sender_id": fixtures.PEER,
        "source_sender_label": "Source label",
        "source_chat_id": fixtures.OWNER,
        "source_timestamp": "",
        "source_ids": {"svrid": "922337203685477580"},
    }
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        html = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.ids[1][0],)
            ).fetchone()
        )
        native = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.ids[1][1],)
            ).fetchone()
        )
        html_proof = json.loads(html["provenance"])
        native_proof = json.loads(native["provenance"])
        envelope = "「Source label：" + QUOTED + "」"
        html_proof.update(
            record_id="html-quoted-reply",
            source_order=5,
            source_time=WALL.replace("T", " "),
            text_extraction_kind="quoted_reply",
            primary_text_sha256=hashlib.sha256(REPLY.encode()).hexdigest(),
            export_msgid_namespace="MesLocalID",
            forwarded_components=[],
            quoted_components=[{"kind": "rendered_quote", "text": envelope}],
        )
        html_proof["locator"].update(
            export_msgtype="49",
            export_msgid="99",
            export_msgid_namespace="MesLocalID",
        )
        native_proof.update(
            record_id="native-quoted-reply",
            message_type=49,
            appmsg_subtype=57,
            text_extraction_kind="quoted_reply",
            server_id="922337203685477599",
            unix_timestamp=int(datetime.fromisoformat(WALL + "+08:00").timestamp()),
            quoted_reference=deepcopy(reference),
            text_components=[
                {"kind": "reply_text", "text": REPLY},
                {"kind": "quoted_text", "text": QUOTED, **deepcopy(reference)},
            ],
        )
        native_proof["locator"]["local_id"] = 199
        for row, identifier, ordinal, text, proof, clock in (
            (html, html_id, 5, envelope + "\n\n" + REPLY, html_proof, WALL),
            (
                native,
                native_id,
                105,
                REPLY + "\n\n" + QUOTED,
                native_proof,
                WALL + "+08:00",
            ),
        ):
            row.update(
                id=identifier,
                ordinal=ordinal,
                text=text,
                search_text=text.casefold(),
                time_sort=5,
                timestamp_value=clock,
                timestamp_label="1 Jan 2020 08:00:04",
            )
        if mutation == "missing_target_server":
            reference["source_ids"] = {}
        elif mutation == "unknown_target_server":
            reference["source_ids"] = {"svrid": "123456789"}
        elif mutation == "wrong_quoted_author":
            reference["source_sender_id"] = fixtures.OWNER
        elif mutation == "different_quoted_body":
            native_proof["text_components"][1]["text"] = "A different quoted body"
            native["text"] = REPLY + "\n\nA different quoted body"
        elif mutation == "different_rendered_speaker":
            envelope = "「Unrelated label：" + QUOTED + "」"
            html_proof["quoted_components"][0]["text"] = envelope
            html["text"] = envelope + "\n\n" + REPLY
        elif mutation == "different_current_author":
            html["sender_id"] = "alias-" + fixtures.PEER
        elif mutation == "different_current_direction":
            html["direction"] = "received"
        elif mutation == "different_reply":
            native_proof["text_components"][0]["text"] = "A distinct current reply"
            native["text"] = "A distinct current reply\n\n" + QUOTED
        elif mutation == "invalid_html_reply_hash":
            html_proof["primary_text_sha256"] = hashlib.sha256(
                b"Other reply"
            ).hexdigest()
        elif mutation == "native_storage_mismatch":
            native["text"] = "Stored reply disagrees with structured roles"
        elif mutation == "nontext_quoted_target":
            reference["original_message_type"] = 3
        elif mutation == "wrong_native_subtype":
            native_proof["appmsg_subtype"] = 19
        elif mutation == "reply_precedes_native_target":
            native["ordinal"] = 99
        elif mutation == "forwarded_quote":
            reference["original_message_type"] = 49
            reference["relationship"] = "forwarded_conversation"
            native_proof["text_components"][1]["kind"] = "quoted_forwarded_text"
        elif mutation == "unavailable_quote":
            reference["content_unavailable"] = True
            native_proof["text_components"] = native_proof["text_components"][:1]
            native["text"] = REPLY
        elif mutation == "unproven_target":
            target_text = "An original without a proven cross-source event"
            reference["source_ids"] = {"svrid": "900000000000000001"}
            native_proof["text_components"][1]["text"] = target_text
            native["text"] = REPLY + "\n\n" + target_text
            envelope = "「Source label：" + target_text + "」"
            html_proof["quoted_components"][0]["text"] = envelope
            html["text"] = envelope + "\n\n" + REPLY
            for original_id, target_native in zip(fixture.ids[0], (False, True)):
                target = dict(
                    connection.execute(
                        "SELECT * FROM messages WHERE id=?", (original_id,)
                    ).fetchone()
                )
                proof = json.loads(target["provenance"])
                if target_native:
                    proof.update(
                        record_id="unproven-native-target",
                        server_id=reference["source_ids"]["svrid"],
                        unix_timestamp=int(
                            datetime.fromisoformat(
                                "2020-01-01T08:00:03+08:00"
                            ).timestamp()
                        ),
                    )
                    target_id = _identifier(fixtures.NATIVE, proof["record_id"])
                    clock = "2020-01-01T08:00:03+08:00"
                else:
                    proof.update(
                        record_id="unproven-html-target",
                        source_order=4,
                        source_time="2020-01-01 08:00:02",
                        primary_text_sha256=hashlib.sha256(
                            target_text.encode()
                        ).hexdigest(),
                    )
                    target_id = hashlib.sha256(b"unproven-html-target").hexdigest()
                    clock = "2020-01-01T08:00:02"
                target.update(
                    id=target_id,
                    ordinal=104 if target_native else 4,
                    text=target_text,
                    search_text=target_text.casefold(),
                    time_sort=4,
                    timestamp_value=clock,
                    timestamp_label=clock,
                    provenance=json.dumps(proof),
                )
                _insert(connection, target)
        elif mutation == "ambiguous_html_target":
            additional = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (fixture.ids[0][0],)
                ).fetchone()
            )
            proof = json.loads(additional["provenance"])
            proof.update(source_order=4, source_time="2020-01-01 08:00:03")
            additional.update(
                id=hashlib.sha256(b"additional-html-quote-target").hexdigest(),
                ordinal=4,
                time_sort=4,
                timestamp_value="2020-01-01T08:00:03",
                timestamp_label="1 Jan 2020 08:00:03",
                provenance=json.dumps(proof),
            )
            _insert(connection, additional)
        elif mutation not in {None, "inconsistent_native_quote_reference"}:
            raise AssertionError("Unknown test mutation")
        native_proof["quoted_reference"] = deepcopy(reference)
        if len(native_proof["text_components"]) == 2:
            native_proof["text_components"][1].update(deepcopy(reference))
        if mutation == "inconsistent_native_quote_reference":
            native_proof["quoted_reference"]["source_sender_id"] = fixtures.OWNER
        for row, proof in ((html, html_proof), (native, native_proof)):
            row["search_text"] = row["text"].casefold()
            row["provenance"] = json.dumps(proof)
            _insert(connection, row)
        connection.execute(
            "UPDATE conversations SET message_count=(SELECT COUNT(*) FROM messages "
            "WHERE conversation_id=?),last_sort=5,last_time=? WHERE id=?",
            (fixtures.SESSION, WALL + "+08:00", fixtures.SESSION),
        )
    return html_id, native_id


def test_quoted_reply_prefers_ipad_and_preserves_current_author(event_store):
    html_id, native_id = _add_quote(event_store)
    before = event_store.repository.path.read_bytes(), event_store.registry.read_bytes()
    connection, result = fixtures._run(event_store)
    try:
        assert result["status"] == "ready"
        assert result["original_count"] == 8
        assert result["display_count"] == 4
        assert result["count_delta"] == 4
        assert result["preferred_by_id"] == {
            **dict(event_store.ids),
            html_id: native_id,
        }
        members = result["members_by_preferred"][native_id]
        assert {member["contact_id"] for member in members} == {
            canonical_contact_id(fixtures.OWNER, authority="native_message_sender")
        }
        assert {member["source_label"] for member in members} == {
            "HTML export",
            "iPad archive",
        }
        assert {member["id"] for member in members} == {html_id, native_id}
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
    assert REPLY not in cache and QUOTED not in cache


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_target_server",
        "unknown_target_server",
        "wrong_quoted_author",
        "different_quoted_body",
        "different_rendered_speaker",
        "different_current_author",
        "different_current_direction",
        "different_reply",
        "invalid_html_reply_hash",
        "native_storage_mismatch",
        "nontext_quoted_target",
        "wrong_native_subtype",
        "reply_precedes_native_target",
        "ambiguous_html_target",
        "unproven_target",
        "inconsistent_native_quote_reference",
        "forwarded_quote",
        "unavailable_quote",
    ],
)
def test_unproven_quoted_pairs_remain_independent(event_store, mutation):
    html_id, native_id = _add_quote(event_store, mutation)
    before = event_store.repository.path.read_bytes(), event_store.registry.read_bytes()
    connection, result = fixtures._run(event_store)
    try:
        assert result["status"] == "ready"
        assert result["preferred_by_id"] == dict(event_store.ids)
        assert result["count_delta"] == 3
        table = events.canonical_message_table(connection, "main.messages", result)
        retained = {row[0] for row in connection.execute("SELECT id FROM " + table)}
        assert {html_id, native_id} <= retained
    finally:
        connection.close()
    assert before == (
        event_store.repository.path.read_bytes(),
        event_store.registry.read_bytes(),
    )
