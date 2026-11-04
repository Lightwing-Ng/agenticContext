"""Missing HTML identities require a bounded, independently verified event sequence.

Code version: v1.0.0-codex.0
"""

from datetime import datetime
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_conversation_events as events
from app.core.chat_history_browser import query_chat_history
from app.core.wechat_contact_analysis import message_source_location
from app.core.wechat_archive import _identifier
from tests import test_wechat_conversation_events as fixtures


event_store = fixtures.event_store
ROOM = "123456789@chatroom"
SENDER = "wxid_unidentifiedsender"


@pytest.fixture
def unidentified_store(event_store):
    fixture = event_store
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "UPDATE conversations SET peer_id=?,kind='group',message_count=8",
            (ROOM,),
        )
        for index, pair in enumerate(fixture.ids):
            order = (1, 3, 4)[index]
            local_id = (100, 102, 103)[index]
            wall = f"2020-01-01T08:00:0{order - 1}"
            for native, identifier in enumerate(pair):
                row = connection.execute(
                    "SELECT * FROM messages WHERE id=?", (identifier,)
                ).fetchone()
                provenance = json.loads(row["provenance"])
                if native:
                    provenance["locator"].update(
                        local_id=local_id,
                        table="Chat_" + hashlib.md5(ROOM.encode()).hexdigest(),
                    )
                    clock = wall + "+08:00"
                    provenance["unix_timestamp"] = int(
                        datetime.fromisoformat(clock).timestamp()
                    )
                else:
                    provenance["locator"].update(
                        export_msgid=str(local_id),
                        export_msgid_namespace="MesLocalID",
                    )
                    provenance["source_time"] = wall.replace("T", " ")
                    provenance["source_order"] = order
                    clock = wall
                connection.execute(
                    "UPDATE messages SET ordinal=?,timestamp_value=?,provenance=? WHERE id=?",
                    (
                        100 + order if native else order,
                        clock,
                        json.dumps(provenance),
                        identifier,
                    ),
                )
        html = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.ids[0][0],)
            ).fetchone()
        )
        native = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.ids[0][1],)
            ).fetchone()
        )
        body = "Unknown sender sample; preserve  two spaces\n[Smirk]"
        html.update(
            id=hashlib.sha256(b"unidentified-html-event").hexdigest(),
            ordinal=2,
            sender_id=None,
            sender_label=SENDER,
            text=body,
            timestamp_value="2020-01-01T08:00:01",
        )
        proof = json.loads(html["provenance"])
        proof.update(
            record_id="unidentified-html-record",
            source_order=2,
            source_time="2020-01-01 08:00:01",
            primary_text_sha256=hashlib.sha256(body.encode()).hexdigest(),
            sender_export_id="",
            sender_native_id="",
            avatar_path_metadata="Synthetic_files/Portrait/DefaultAvatar.png",
        )
        proof["locator"].update(export_msgid="101")
        html["provenance"] = json.dumps(proof)
        native.update(
            id=_identifier(fixtures.NATIVE, "unidentified-native-record"),
            ordinal=102,
            sender_id=SENDER,
            sender_label="Native source label",
            text=body,
            timestamp_value="2020-01-01T08:00:01+08:00",
        )
        proof = json.loads(native["provenance"])
        proof.update(
            record_id="unidentified-native-record",
            server_id="1234567890123456789",
            unix_timestamp=int(
                datetime.fromisoformat(native["timestamp_value"]).timestamp()
            ),
        )
        proof["locator"].update(local_id=101)
        native["provenance"] = json.dumps(proof)
        for row in (html, native):
            row["search_text"] = body.casefold()
            connection.execute(
                "INSERT INTO messages ("
                + ",".join(row)
                + ") VALUES ("
                + ",".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
    fixture.unidentified_ids = html["id"], native["id"]
    return fixture


def test_verified_group_sequence_collapses_missing_identity_without_promoting_it(
    unidentified_store,
):
    fixture = unidentified_store
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    connection, result = fixtures._run(fixture)
    try:
        html_id, native_id = fixture.unidentified_ids
        assert result["status"] == "ready"
        assert result["preferred_by_id"] == {**dict(fixture.ids), html_id: native_id}
        assert result["original_count"] == 8
        assert result["display_count"] == 4
        contexts = result["members_by_preferred"][native_id]
        html = next(item for item in contexts if item["id"] == html_id)
        assert html["sender_id"] == html["contact_id"] == ""
        assert html["sender_label"] == SENDER
        assert html["event_match_basis"]
        table = events.canonical_message_table(connection, "main.messages", result)
        assert connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 4
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM main.messages WHERE id IN (?,?)",
                (html_id, native_id),
            ).fetchone()[0]
            == 2
        )
    finally:
        connection.close()
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


@pytest.mark.parametrize(
    "damage",
    [
        "label",
        "local_id",
        "native_sender",
        "avatar",
        "missing_id_namespace",
        "known_sender",
        "contact_anchors",
    ],
)
def test_uncertain_html_sender_stays_separate(unidentified_store, damage):
    fixture = unidentified_store
    html_id, native_id = fixture.unidentified_ids
    if damage == "contact_anchors":
        with sqlite3.connect(fixture.registry) as connection:
            connection.execute("DELETE FROM sender_bindings")
    else:
        with sqlite3.connect(fixture.repository.path) as connection:
            connection.row_factory = sqlite3.Row
            row = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (html_id,)
                ).fetchone()
            )
            proof = json.loads(row["provenance"])
            if damage == "label":
                row["sender_label"] = "Unverified remark"
            elif damage == "local_id":
                proof["locator"]["export_msgid"] = "999"
            elif damage == "native_sender":
                connection.execute(
                    "UPDATE messages SET sender_id=? WHERE id=?",
                    ("wxid_other_sender", native_id),
                )
            elif damage == "avatar":
                proof["avatar_path_metadata"] = "Synthetic_files/Portrait/Other.jpg"
            elif damage == "missing_id_namespace":
                proof["locator"].pop("export_msgid_namespace")
            elif damage == "known_sender":
                row["sender_id"] = "unresolved_export_alias"
            connection.execute(
                "UPDATE messages SET sender_id=?,sender_label=?,provenance=? WHERE id=?",
                (row["sender_id"], row["sender_label"], json.dumps(proof), html_id),
            )
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    connection, result = fixtures._run(fixture)
    connection.close()
    assert html_id not in result["preferred_by_id"]
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


def test_viewer_search_and_old_anchor_reuse_the_preferred_native_occurrence(
    unidentified_store,
):
    fixture = unidentified_store
    html_id, native_id = fixture.unidentified_ids
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    page = query_chat_history(
        fixture.store,
        source="wechat",
        session=fixtures.SESSION,
        query="Unknown sender sample",
    )
    assert page.total_count == 1
    assert page.items[0].stable_id == native_id
    observations = page.items[0].messaging["source_observations"]
    assert {row["id"] for row in observations} == {html_id, native_id}
    html = next(row for row in observations if row["id"] == html_id)
    assert html["contact_id"] == html["sender_id"] == ""
    location = message_source_location(fixture.store, html_id)
    assert location["id"] == native_id
    assert location["anchor_id"] == "wechat_message_" + native_id
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


def test_other_unresolved_html_repeat_vetoes_the_sequence_bridge(unidentified_store):
    fixture = unidentified_store
    html_id, _ = fixture.unidentified_ids
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        duplicate = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (html_id,)
            ).fetchone()
        )
        duplicate.update(
            id=hashlib.sha256(b"other-unknown-source-repeat").hexdigest(),
            ordinal=200,
            sender_label="An unresolved remark",
            sender_id="an-unresolved-export-alias",
        )
        proof = json.loads(duplicate["provenance"])
        proof.update(source_order=200, record_id="other-unidentified-html-record")
        proof["locator"]["export_msgid"] = "999"
        duplicate["provenance"] = json.dumps(proof)
        connection.execute(
            "INSERT INTO messages ("
            + ",".join(duplicate)
            + ") VALUES ("
            + ",".join("?" for _ in duplicate)
            + ")",
            tuple(duplicate.values()),
        )
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    connection, result = fixtures._run(fixture)
    connection.close()
    assert html_id not in result["preferred_by_id"]
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )
