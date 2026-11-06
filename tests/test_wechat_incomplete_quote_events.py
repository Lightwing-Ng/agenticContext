"""Complete exporter quotes outrank incomplete native copies without source loss.

Code version: v1.0.0-codex.0
"""

import hashlib
import json
import sqlite3

import pytest

from app.core.chat_history_browser import query_chat_history
from app.core.wechat_contact_analysis import message_source_location
from app.core.wechat_contacts import canonical_contact_id
from app.core.wechat_display_names import display_names_path
from tests import test_wechat_conversation_events as fixtures
from tests import test_wechat_unidentified_sender_events as sources


event_store = sources.event_store
unidentified_store = sources.unidentified_store
REPLY = "Reply whose quoted image is absent from the text-only archive"
SPEAKER = "Original quoted author"


@pytest.fixture
def incomplete_store(unidentified_store):
    fixture = unidentified_store
    html_id, native_id = fixture.unidentified_ids
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        for native, identifier in enumerate((html_id, native_id)):
            row = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (identifier,)
                ).fetchone()
            )
            proof = json.loads(row["provenance"])
            proof["text_extraction_kind"] = "quoted_reply"
            if native:
                row.update(
                    sender_id=fixtures.PEER, sender_label="Native personal remark"
                )
                row["text"] = REPLY
                proof.update(
                    message_type=49,
                    appmsg_subtype=57,
                    text_components=[{"kind": "reply_text", "text": REPLY}],
                    quoted_reference={
                        "original_message_type": 3,
                        "source_sender_id": sources.ROOM,
                        "source_chat_id": "wxid_originalphotoauthor",
                        "source_sender_label": SPEAKER,
                        "source_ids": {"svrid": "987654321012345678"},
                        "source_timestamp": "",
                    },
                )
            else:
                row.update(
                    sender_id="alias-" + fixtures.PEER, sender_label="Historical remark"
                )
                envelope = "「" + SPEAKER + "：[Photo]」"
                row["text"] = envelope + "\n\n" + REPLY
                proof.update(
                    primary_text_sha256=hashlib.sha256(REPLY.encode()).hexdigest(),
                    export_msgid_namespace="MesLocalID",
                    quoted_components=[{"kind": "rendered_quote", "text": envelope}],
                )
                proof["locator"].update(export_msgtype="49", export_msgid="2001")
            connection.execute(
                "UPDATE messages SET sender_id=?,sender_label=?,text=?,search_text=?,"
                "provenance=? WHERE id=?",
                (
                    row["sender_id"],
                    row["sender_label"],
                    row["text"],
                    row["text"].casefold(),
                    json.dumps(proof),
                    identifier,
                ),
            )
    return fixture


def test_complete_photo_quote_is_kept_and_old_native_anchor_still_works(
    incomplete_store,
):
    fixture = incomplete_store
    html_id, native_id = fixture.unidentified_ids
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    connection, result = fixtures._run(fixture)
    connection.close()
    assert result["status"] == "ready"
    assert result["preferred_by_id"] == {**dict(fixture.ids), native_id: html_id}
    assert result["original_count"] == 8
    assert result["display_count"] == 4
    contexts = result["members_by_preferred"][html_id]
    assert {row["id"] for row in contexts} == {html_id, native_id}
    assert all("quoted_original_id" not in row for row in contexts)
    assert all("quoted_contact_id" not in row for row in contexts)
    page = query_chat_history(
        fixture.store, source="wechat", session=fixtures.SESSION, query=REPLY
    )
    assert page.total_count == 1
    assert page.items[0].stable_id == html_id
    assert page.items[0].messaging["quoted_reply"]["text"] == "[Photo]"
    assert message_source_location(fixture.store, native_id)["id"] == html_id
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


def test_personal_viewing_remark_applies_to_preferred_complete_quote(incomplete_store):
    fixture = incomplete_store
    html_id, _ = fixture.unidentified_ids
    contact = canonical_contact_id(fixtures.PEER, authority="native_message_sender")
    preference = display_names_path(fixture.store)
    preference.write_text(
        json.dumps(
            {"schema_version": 1, "contacts": {contact: "Personal viewing remark"}}
        )
    )
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    page = query_chat_history(
        fixture.store, source="wechat", session=fixtures.SESSION, query=REPLY
    )
    assert page.total_count == 1
    item = page.items[0]
    assert item.stable_id == html_id
    assert item.messaging["viewer_display_name"] == "Personal viewing remark"
    assert item.author_label == "Historical remark"
    assert item.messaging["contact"]["id"] == contact
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


@pytest.mark.parametrize(
    "damage", ["speaker", "type", "server", "reply", "anchors", "ambiguous_html"]
)
def test_uncertain_incomplete_quote_is_not_collapsed(incomplete_store, damage):
    fixture = incomplete_store
    html_id, native_id = fixture.unidentified_ids
    if damage == "anchors":
        with sqlite3.connect(fixture.registry) as connection:
            connection.execute(
                "DELETE FROM sender_bindings WHERE sender_id=?",
                ("alias-" + fixtures.OWNER,),
            )
    else:
        with sqlite3.connect(fixture.repository.path) as connection:
            connection.row_factory = sqlite3.Row
            row = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (native_id,)
                ).fetchone()
            )
            proof = json.loads(row["provenance"])
            if damage == "speaker":
                proof["quoted_reference"]["source_sender_label"] = (
                    "Different quoted author"
                )
            elif damage == "type":
                proof["quoted_reference"]["original_message_type"] = 49
            elif damage == "server":
                proof["quoted_reference"]["source_ids"]["svrid"] = "0"
            elif damage == "reply":
                row["text"] = "Different reply"
            elif damage == "ambiguous_html":
                duplicate = dict(
                    connection.execute(
                        "SELECT * FROM messages WHERE id=?", (html_id,)
                    ).fetchone()
                )
                duplicate["id"] = hashlib.sha256(b"ambiguous-full-quote").hexdigest()
                duplicate["sender_id"] = "unknown-export-alias"
                duplicate["sender_label"] = "Unresolved name"
                duplicate["ordinal"] = 99
                connection.execute(
                    "INSERT INTO messages ("
                    + ",".join(duplicate)
                    + ") VALUES ("
                    + ",".join("?" for _ in duplicate)
                    + ")",
                    tuple(duplicate.values()),
                )
            connection.execute(
                "UPDATE messages SET text=?,provenance=? WHERE id=?",
                (row["text"], json.dumps(proof), native_id),
            )
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    connection, result = fixtures._run(fixture)
    connection.close()
    assert native_id not in result["preferred_by_id"]
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )
