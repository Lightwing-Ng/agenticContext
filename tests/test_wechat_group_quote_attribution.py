"""Room-valued native quotes require original evidence before contact attribution.

Code version: v1.0.2-codex.0
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import sqlite3
from unittest.mock import patch

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_quoted_clock_recovery as recovery
from app.core.wechat_archive import _identifier
from app.core.wechat_text import native_components_text
from tests import test_wechat_quoted_clock_analysis as fixtures


quoted_clock_store = fixtures.quoted_clock_store


def _preserved(fixture):
    return fixture.history.read_bytes(), fixture.registry.read_bytes()


def _change_source(fixture, *, known_clock=False, original="present", nested=False, receiving_room=None):
    with sqlite3.connect(fixture.history) as source:
        source.row_factory = sqlite3.Row
        parent = dict(source.execute("SELECT * FROM messages WHERE id=?", (fixture.parent,)).fetchone())
        proof = json.loads(parent["provenance"])
        if known_clock:
            clock = str(int(datetime.fromisoformat(fixtures.ORIGINAL_TIME).timestamp()))
            proof["quoted_reference"]["source_timestamp"] = clock
            proof["text_components"][1]["source_timestamp"] = clock
        if nested:
            child = {
                "kind": "forwarded_quote",
                "text": native_components_text(proof["text_components"]),
                "text_components": deepcopy(proof["text_components"]),
                "quoted_reference": deepcopy(proof["quoted_reference"]),
                "appmsg_subtype": 57,
                "source_sender_id": fixtures.TARGET,
                "source_chat_id": fixtures.ROOM,
                "source_create_time": str(int(datetime.fromisoformat(fixtures.REPLY_TIME).timestamp())),
            }
            proof.update(
                appmsg_subtype=19,
                text_extraction_kind="forwarded_text",
                text_components=[child],
            )
            proof.pop("quoted_reference")
            fixture.quote = fixture.parent + "_forwarded_1_quoted_2"
            fixture.reply_reference = fixture.parent + "_forwarded_1"
        else:
            fixture.reply_reference = fixture.parent
        if receiving_room is not None:
            conversation = "wechat:" + _identifier(fixture.source_id, receiving_room)
            source.execute(
                "INSERT INTO conversations SELECT ?,partition_id,account_key,?,title,kind,"
                "message_count,first_sort,last_sort,first_time,last_time,latest_text "
                "FROM conversations WHERE id=?",
                (conversation, receiving_room, parent["conversation_id"]),
            )
            proof["locator"]["table"] = "Chat_" + hashlib.md5(receiving_room.encode()).hexdigest()
            source.execute(
                "UPDATE messages SET conversation_id=? WHERE id=?", (conversation, fixture.parent)
            )
        text = native_components_text(proof["text_components"])
        source.execute(
            "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
            (text, text.casefold(), json.dumps(proof), fixture.parent),
        )
        if original == "missing":
            source.execute("DELETE FROM messages WHERE id=?", (fixture.original,))
        elif original == "ambiguous":
            row = dict(source.execute("SELECT * FROM messages WHERE id=?", (fixture.original,)).fetchone())
            original_proof = json.loads(row["provenance"])
            original_proof["record_id"] = "duplicate-original-event"
            row.update(
                id=_identifier(fixture.source_id, original_proof["record_id"]),
                ordinal=row["ordinal"] + 10,
                provenance=json.dumps(original_proof),
            )
            source.execute(
                "INSERT INTO messages VALUES(" + ",".join("?" for _ in row) + ")",
                tuple(row.values()),
            )
        for (conversation,) in source.execute("SELECT id FROM conversations").fetchall():
            source.execute(
                "UPDATE conversations SET message_count=(SELECT COUNT(*) FROM messages WHERE conversation_id=?) WHERE id=?",
                (conversation, conversation),
            )
    fixture.preserved = _preserved(fixture)


def _legacy_cache(fixture, *, embedded_version=None):
    if embedded_version is None:
        return fixtures._old_index(fixture)
    with patch.object(analysis, "NATIVE_EMBEDDED_VERSION", embedded_version):
        return fixtures._old_index(fixture)


def _quote_proof(fixture):
    with sqlite3.connect(fixtures._cache(fixture)) as connection:
        connection.row_factory = sqlite3.Row
        proof = connection.execute(
            "SELECT * FROM quoted_clock_evidence WHERE reference_id=?", (fixture.quote,)
        ).fetchone()
        return dict(proof) if proof else None


def _assert_reply_retained_without_quote(fixture, result):
    assert result["status"] == "ready"
    assert fixture.reply_reference in {item["id"] for item in result["items"]}
    assert fixtures.BODY not in {item["content_text"] for item in result["items"]}
    assert fixtures.REPLY in {item["content_text"] for item in result["items"]}
    assert fixtures._ref(fixtures._cache(fixture), fixture.quote) is None
    assert fixtures._ref(fixtures._cache(fixture), fixture.reply_reference) is not None
    raw = list(analysis.export_contact_analysis_items(
        fixture.store, fixture.target,
        source_fingerprint=result["source_fingerprint"], source_occurrences=True,
    ))
    assert fixture.quote not in {item["id"] for item in raw}
    assert {item["sender_id"] for item in raw} == {fixtures.TARGET}
    assert _preserved(fixture) == fixture.preserved


@pytest.mark.parametrize("known_clock", [False, True])
def test_room_quote_cannot_borrow_reply_author_when_original_has_another_author(
    quoted_clock_store, monkeypatch, known_clock,
):
    fixture = quoted_clock_store(original_author=fixtures.OTHER)
    _change_source(fixture, known_clock=known_clock)
    old = _legacy_cache(fixture)
    assert old["total_count"] == 2
    assert fixtures._ref(fixtures._cache(fixture), fixture.quote) is not None
    fixtures._no_source_rescan(monkeypatch)

    result = analysis.query_contact_analysis(fixture.store, fixture.target)

    _assert_reply_retained_without_quote(fixture, result)
    proof = _quote_proof(fixture)
    assert proof["status"] == "wrong_author"
    assert proof["original_message_id"] == fixture.original
    assert proof["original_sender_id"] == fixtures.OTHER
    assert json.loads(proof["reference_before_json"])["id"] == fixture.quote
    assert result["total_count"] == 1


@pytest.mark.parametrize("original", ["missing", "ambiguous"])
@pytest.mark.parametrize("known_clock", [False, True])
def test_unproved_room_quote_is_excluded_but_raw_quote_text_is_preserved(
    quoted_clock_store, monkeypatch, original, known_clock,
):
    fixture = quoted_clock_store(original_author=fixtures.OTHER)
    _change_source(fixture, original=original, known_clock=known_clock)
    _legacy_cache(fixture)
    fixtures._no_source_rescan(monkeypatch)

    result = analysis.query_contact_analysis(fixture.store, fixture.target)

    _assert_reply_retained_without_quote(fixture, result)
    proof = _quote_proof(fixture)
    assert proof is not None and proof["status"] != "recovered"
    assert json.loads(proof["reference_before_json"])["id"] == fixture.quote
    with sqlite3.connect(fixture.history) as source:
        assert fixtures.BODY in source.execute(
            "SELECT text FROM messages WHERE id=?", (fixture.parent,)
        ).fetchone()[0]


@pytest.mark.parametrize("known_clock", [False, True])
def test_exact_original_with_same_author_keeps_quote_and_its_independent_source_clock(
    quoted_clock_store, monkeypatch, known_clock,
):
    fixture = quoted_clock_store()
    _change_source(fixture, known_clock=known_clock)
    _legacy_cache(fixture)
    before = fixtures._ref(fixtures._cache(fixture), fixture.quote)
    fixtures._no_source_rescan(monkeypatch)

    result = analysis.query_contact_analysis(fixture.store, fixture.target)

    assert result["status"] == "ready"
    retained = fixtures._ref(fixtures._cache(fixture), fixture.quote)
    assert retained is not None
    assert retained["timestamp_value"] == fixtures.ORIGINAL_TIME
    assert retained["source_clock"] == before["source_clock"]
    proof = _quote_proof(fixture)
    assert proof["status"] == "recovered"
    assert proof["original_message_id"] == fixture.original
    assert proof["original_sender_id"] == fixtures.TARGET
    assert _preserved(fixture) == fixture.preserved


@pytest.mark.parametrize("budget", ["MAX_REFERENCES", "MAX_SOURCE_BYTES", "MAX_SCOPED_ROWS"])
def test_bounded_original_proof_cannot_leave_an_ambiguous_quote_attributed_to_reply_author(
    quoted_clock_store, monkeypatch, budget,
):
    fixture = quoted_clock_store(original_author=fixtures.OTHER)
    _change_source(fixture, known_clock=True)
    _legacy_cache(fixture)
    fixtures._no_source_rescan(monkeypatch)
    monkeypatch.setattr(recovery, budget, 1 if budget == "MAX_SOURCE_BYTES" else 0)

    result = analysis.query_contact_analysis(fixture.store, fixture.target)

    _assert_reply_retained_without_quote(fixture, result)
    proof = _quote_proof(fixture)
    assert proof is not None and proof["status"] != "recovered"
    assert json.loads(proof["reference_before_json"])["id"] == fixture.quote


@pytest.mark.parametrize("known_clock", [False, True])
def test_nested_room_quote_is_excluded_without_removing_the_forwarded_authors_reply(
    quoted_clock_store, monkeypatch, known_clock,
):
    fixture = quoted_clock_store(original_author=fixtures.OTHER)
    _change_source(fixture, nested=True, known_clock=known_clock)
    _legacy_cache(fixture)
    fixtures._no_source_rescan(monkeypatch)

    result = analysis.query_contact_analysis(fixture.store, fixture.target)

    _assert_reply_retained_without_quote(fixture, result)
    assert _quote_proof(fixture)["status"] == "wrong_author"


@pytest.mark.parametrize("known_clock", [False, True])
def test_nested_quote_cannot_borrow_the_receiving_room_to_claim_a_reply_author(
    quoted_clock_store, monkeypatch, known_clock,
):
    fixture = quoted_clock_store(original_author=fixtures.OTHER)
    _change_source(
        fixture, nested=True, known_clock=known_clock,
        receiving_room="33333333333@chatroom",
    )
    _legacy_cache(fixture)
    fixtures._no_source_rescan(monkeypatch)

    result = analysis.query_contact_analysis(fixture.store, fixture.target)

    _assert_reply_retained_without_quote(fixture, result)
    assert _quote_proof(fixture)["status"] != "recovered"


def test_compatible_old_projection_cache_is_repaired_without_rebuilding_accepted_sources(
    quoted_clock_store, monkeypatch,
):
    fixture = quoted_clock_store(original_author=fixtures.OTHER)
    _change_source(fixture, known_clock=True)
    _legacy_cache(fixture, embedded_version="v1.2.1-codex.0")
    assert fixtures._ref(fixtures._cache(fixture), fixture.quote) is not None
    fixtures._no_source_rescan(monkeypatch)

    result = analysis.query_contact_analysis(fixture.store, fixture.target)

    _assert_reply_retained_without_quote(fixture, result)
    with sqlite3.connect(fixtures._cache(fixture)) as connection:
        assert connection.execute(
            "SELECT value FROM info WHERE key='native_embedded_version'"
        ).fetchone()[0] == analysis.NATIVE_EMBEDDED_VERSION
    proof = _quote_proof(fixture)
    assert proof["original_sender_id"] == fixtures.OTHER
    assert hashlib.sha256(fixtures.BODY.encode()).hexdigest() == proof["body_sha256"]


def test_one_oversized_conversation_cannot_cancel_an_independent_exact_author_proof(
    quoted_clock_store, monkeypatch,
):
    fixture = quoted_clock_store(original_author=fixtures.OTHER)
    _change_source(fixture, known_clock=True)
    independent_room = "33333333333@chatroom"
    independent_conversation = "wechat:" + _identifier(fixture.source_id, independent_room)
    independent_original = _identifier(fixture.source_id, "independent-original")
    independent_parent = _identifier(fixture.source_id, "independent-reply")
    independent_quote = independent_parent + "_quoted_2"
    original_server = "3365480705523997095"
    with sqlite3.connect(fixture.history) as source:
        source.row_factory = sqlite3.Row
        original = dict(source.execute("SELECT * FROM messages WHERE id=?", (fixture.original,)).fetchone())
        parent = dict(source.execute("SELECT * FROM messages WHERE id=?", (fixture.parent,)).fetchone())
        oversized_conversation = parent["conversation_id"]
        source.execute(
            "INSERT INTO conversations SELECT ?,partition_id,account_key,?,title,kind,"
            "message_count,first_sort,last_sort,first_time,last_time,latest_text "
            "FROM conversations WHERE id=?",
            (independent_conversation, independent_room, parent["conversation_id"]),
        )
        for number, (name, row, identifier) in enumerate(
            (("independent-original", original, independent_original),
             ("independent-reply", parent, independent_parent)),
            100,
        ):
            proof = json.loads(row["provenance"])
            proof["record_id"] = name
            proof["server_id"] = original_server if identifier == independent_original else "4001703475893116386"
            proof["locator"]["table"] = "Chat_" + hashlib.md5(independent_room.encode()).hexdigest()
            proof["locator"]["local_id"] = number
            if identifier == independent_parent:
                for reference in (proof["quoted_reference"], proof["text_components"][1]):
                    reference["source_sender_id"] = independent_room
                    reference["source_ids"] = {"svrid": original_server}
            row.update(
                id=identifier,
                conversation_id=independent_conversation,
                ordinal=number,
                provenance=json.dumps(proof),
            )
            source.execute(
                "INSERT INTO messages VALUES(" + ",".join("?" for _ in row) + ")",
                tuple(row.values()),
            )
        source.execute(
            "UPDATE conversations SET message_count=11 WHERE id=?", (oversized_conversation,)
        )
        source.execute(
            "UPDATE conversations SET message_count=2 WHERE id=?", (independent_conversation,)
        )
    fixture.preserved = _preserved(fixture)
    _legacy_cache(fixture)
    fixtures._no_source_rescan(monkeypatch)
    monkeypatch.setattr(recovery, "MAX_SCOPED_ROWS", 10)

    result = analysis.query_contact_analysis(fixture.store, fixture.target)

    _assert_reply_retained_without_quote(fixture, result)
    cache = fixtures._cache(fixture)
    with sqlite3.connect(cache) as connection:
        small_proof = connection.execute(
            "SELECT status,original_message_id,original_sender_id,body_sha256 "
            "FROM quoted_clock_evidence WHERE reference_id=?", (independent_quote,),
        ).fetchone()
        assert small_proof == (
            "wrong_author", independent_original, fixtures.OTHER,
            hashlib.sha256(fixtures.BODY.encode()).hexdigest(),
        )
        assert connection.execute(
            "SELECT status FROM quoted_clock_evidence WHERE reference_id=?", (fixture.quote,),
        ).fetchone() == ("unverified_author",)
        assert connection.execute(
            "SELECT id FROM refs WHERE id=?", (independent_quote,),
        ).fetchone() is None
        assert connection.execute(
            "SELECT id FROM refs WHERE id=?", (independent_parent,),
        ).fetchone() == (independent_parent,)
    assert _preserved(fixture) == fixture.preserved
