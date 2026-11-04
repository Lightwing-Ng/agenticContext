"""Human-reviewed quote occurrences require unchanged, scoped source evidence.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core.wechat_reviewed_quote_events import (
    MATCH_BASIS,
    reviewed_quotes_digest,
    reviewed_quotes_path,
)
from tests import test_wechat_conversation_events as events
from tests import test_wechat_incomplete_quote_events as quotes


event_store = quotes.event_store
unidentified_store = quotes.unidentified_store
incomplete_store = quotes.incomplete_store


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def reviewed_store(incomplete_store):
    fixture = incomplete_store
    html, native = fixture.unidentified_ids
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM messages WHERE id=?", (native,)
        ).fetchone()
        proof = json.loads(row["provenance"])
        proof["quoted_reference"]["original_message_type"] = 49
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), native)
        )
        pins = []
        for identifier in (html, native):
            row = connection.execute(
                "SELECT * FROM messages WHERE id=?", (identifier,)
            ).fetchone()
            snapshot = connection.execute(
                "SELECT source_id,snapshot_id FROM source_snapshots WHERE id=?",
                (json.loads(row["provenance"])["source_snapshot"],),
            ).fetchone()
            pins.append(
                {
                    "message_id": identifier,
                    "text_sha256": _sha(row["text"]),
                    "provenance_sha256": _sha(row["provenance"]),
                    "source_id": snapshot["source_id"],
                    "source_snapshot_id": snapshot["snapshot_id"],
                }
            )
    review = {
        "authority": "user_confirmed_quoted_reply_copy",
        "conversation_id": events.SESSION,
        "account_key": "account:" + events.OWNER,
        "preferred_message_id": html,
        "members": pins,
        "evidence_id": _sha("The user selected this complete exported quote"),
    }
    fixture.quote_registry = reviewed_quotes_path(fixture.store)
    fixture.review = {"schema_version": 1, "quote_reviews": [review]}
    fixture.quote_registry.write_text(json.dumps(fixture.review))
    return fixture


def _run(fixture):
    connection, result = events._run(fixture)
    connection.close()
    return result


def test_reviewed_occurrence_keeps_complete_quote_and_native_uncertainty(
    reviewed_store,
):
    fixture = reviewed_store
    html, native = fixture.unidentified_ids
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    result = _run(fixture)
    assert result["status"] == "ready"
    assert result["preferred_by_id"][native] == html
    contexts = result["members_by_preferred"][html]
    assert {item["id"] for item in contexts} == {html, native}
    assert all(item["quote_match_basis"] == MATCH_BASIS for item in contexts)
    native_context = next(item for item in contexts if item["id"] == native)
    assert native_context["quoted_content_unavailable"] is True
    assert native_context["quoted_reference_type"] == 49
    assert all("quoted_original_id" not in item for item in contexts)
    assert all("quoted_contact_id" not in item for item in contexts)
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )
    assert _run(fixture) == result


def test_removing_review_restores_both_observations_and_invalidates_cache(
    reviewed_store,
):
    fixture = reviewed_store
    _, native = fixture.unidentified_ids
    first = _run(fixture)
    fixture.quote_registry.unlink()
    second = _run(fixture)
    assert native in first["preferred_by_id"]
    assert native not in second["preferred_by_id"]
    assert first["fingerprint"] != second["fingerprint"]
    assert second["preferred_by_id"] == dict(fixture.ids)


def test_reviewed_complete_envelopes_need_no_invented_original(reviewed_store):
    fixture = reviewed_store
    html, native = fixture.unidentified_ids
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM messages WHERE id=?", (native,)
        ).fetchone()
        proof = json.loads(row["provenance"])
        proof["quoted_reference"]["original_message_type"] = 1
        proof["text_components"].append(
            {
                "kind": "quoted_text",
                "text": "[Photo]",
                **proof["quoted_reference"],
            }
        )
        body, provenance = quotes.REPLY + "\n\n[Photo]", json.dumps(proof)
        connection.execute(
            "UPDATE messages SET text=?,provenance=? WHERE id=?",
            (body, provenance, native),
        )
    fixture.review["quote_reviews"][0]["members"][1].update(
        text_sha256=_sha(body), provenance_sha256=_sha(provenance)
    )
    fixture.quote_registry.write_text(json.dumps(fixture.review))
    result = _run(fixture)
    assert result["preferred_by_id"][native] == html
    context = next(
        item for item in result["members_by_preferred"][html] if item["id"] == native
    )
    assert context["quoted_content_unavailable"] is False
    assert context["quoted_reference_type"] == 1
    assert "quoted_original_id" not in context


@pytest.mark.parametrize(
    "damage",
    [
        "text_pin",
        "provenance_pin",
        "source",
        "snapshot",
        "session",
        "account",
        "preferred_native",
        "authority",
        "duplicate_member",
        "missing_member",
        "speaker",
        "reply",
        "sender",
        "server",
        "ambiguous_html",
    ],
)
def test_stale_or_ambiguous_review_preserves_unrelated_proofs(reviewed_store, damage):
    fixture = reviewed_store
    html, native = fixture.unidentified_ids
    registry = deepcopy(fixture.review)
    review = registry["quote_reviews"][0]
    if damage == "text_pin":
        review["members"][0]["text_sha256"] = "0" * 64
    elif damage == "provenance_pin":
        review["members"][1]["provenance_sha256"] = "0" * 64
    elif damage == "source":
        review["members"][0]["source_id"] = "html-other"
    elif damage == "snapshot":
        review["members"][1]["source_snapshot_id"] = "other-snapshot"
    elif damage == "session":
        review["conversation_id"] = "wechat:" + "1" * 64
    elif damage == "account":
        review["account_key"] = "account:wxid_unrelatedowner"
    elif damage == "preferred_native":
        review["preferred_message_id"] = native
    elif damage == "authority":
        review["authority"] = "similar_text"
    elif damage == "duplicate_member":
        review["members"].append(review["members"][0])
    elif damage == "missing_member":
        review["members"][1]["message_id"] = "0" * 64
    else:
        with sqlite3.connect(fixture.repository.path) as connection:
            connection.row_factory = sqlite3.Row
            row = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (native,)
                ).fetchone()
            )
            proof = json.loads(row["provenance"])
            if damage == "speaker":
                proof["quoted_reference"]["source_sender_label"] = "Conflicting speaker"
            elif damage == "reply":
                row["text"] = "Different reply"
                proof["text_components"][0]["text"] = row["text"]
            elif damage == "sender":
                row["sender_id"] = "wxid_anotherreplyauthor"
            elif damage == "server":
                proof["server_id"] = "0"
            elif damage == "ambiguous_html":
                duplicate = dict(
                    connection.execute(
                        "SELECT * FROM messages WHERE id=?", (html,)
                    ).fetchone()
                )
                duplicate["id"] = _sha("Unreviewed ambiguous HTML event")
                connection.execute(
                    "INSERT INTO messages ("
                    + ",".join(duplicate)
                    + ") VALUES ("
                    + ",".join("?" for _ in duplicate)
                    + ")",
                    tuple(duplicate.values()),
                )
            row["provenance"] = json.dumps(proof)
            connection.execute(
                "UPDATE messages SET text=?,sender_id=?,provenance=? WHERE id=?",
                (row["text"], row["sender_id"], row["provenance"], native),
            )
            review["members"][1]["text_sha256"] = _sha(row["text"])
            review["members"][1]["provenance_sha256"] = _sha(row["provenance"])
    fixture.quote_registry.write_text(json.dumps(registry))
    before = fixture.repository.path.read_bytes()
    result = _run(fixture)
    assert native not in result["preferred_by_id"]
    assert result["preferred_by_id"] == dict(fixture.ids)
    assert before == fixture.repository.path.read_bytes()


def test_malformed_or_unavailable_registry_fails_closed_without_hydration(
    reviewed_store,
):
    fixture = reviewed_store
    fixture.quote_registry.write_text(
        '{"schema_version":1,"schema_version":1,"quote_reviews":[]}'
    )
    assert _run(fixture)["preferred_by_id"] == dict(fixture.ids)
    digest = reviewed_quotes_digest(fixture.store)
    fixture.quote_registry.write_text("x" * (1024 * 1024 + 1))
    assert reviewed_quotes_digest(fixture.store) != digest
    assert _run(fixture)["preferred_by_id"] == dict(fixture.ids)
