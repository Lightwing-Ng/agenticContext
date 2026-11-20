"""Quoted reply source runs retain roles, literal payloads, and native events.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_archive import _identifier
from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_history_browser import (
    _html_quoted_reply_presentation,
    _native_quoted_reply_presentation,
)
from app.core.wechat_text import native_components_text
from tests import test_wechat_native_html_precedence as sources
from tests.test_wechat_html_group_frames import synthetic_group_registry


CID = sources.CID
statement_store = sources.statement_store
native_html_store = sources.native_html_store


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _row(connection, message_id):
    return dict(
        connection.execute(
            "SELECT * FROM messages WHERE id=?", (message_id,)
        ).fetchone()
    )


def _update(connection, row, proof, text):
    connection.execute(
        "UPDATE messages SET text=?,search_text=?,provenance=? WHERE id=?",
        (text, text.casefold(), json.dumps(proof), row["id"]),
    )


@pytest.fixture
def quoted_source_store(native_html_store):
    """Keep six independent native events with differently rendered HTML envelopes."""
    fixture = native_html_store
    replies, quoted = {}, {}
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        for index, (native_id, html_id) in enumerate(
            zip(fixture.native_ids, fixture.html_ids, strict=True), 1
        ):
            reply = f"  Synthetic own reply {index}\nKeep  two spaces.  \n"
            quote = f"Earlier statement {index}\nA literal quoted second line."
            reference = {
                "original_message_type": 1,
                "source_sender_id": fixture.owner,
                "source_chat_id": sources.grouping.TARGET,
                "source_sender_label": "Native contact remark " + str(index),
                "source_timestamp": "2020-08-12 13:44:00",
                "source_ids": {"svrid": str(6000000000000000000 + index)},
            }
            native = _row(connection, native_id)
            native_proof = json.loads(native["provenance"])
            native_proof.update(
                message_type=49,
                appmsg_subtype=57,
                text_extraction_kind="quoted_reply",
                quoted_reference=reference,
                text_components=[
                    {"kind": "reply_text", "text": reply},
                    {"kind": "quoted_text", "text": quote, **reference},
                ],
            )
            _update(
                connection,
                native,
                native_proof,
                native_components_text(native_proof["text_components"]),
            )
            html = _row(connection, html_id)
            html_proof = json.loads(html["provenance"])
            envelope = f"「HTML-local remark {index}：{quote}」"
            html_proof.update(
                text_extraction_kind="quoted_reply",
                primary_text_sha256=_sha(reply),
                quoted_components=[{"kind": "rendered_quote", "text": envelope}],
            )
            html_proof["locator"]["export_msgtype"] = "49"
            _update(connection, html, html_proof, envelope + "\n\n" + reply)
            replies[native_id] = replies[html_id] = reply
            quoted[native_id] = quoted[html_id] = quote
    return SimpleNamespace(**vars(fixture), replies=replies, quoted=quoted)


def _query(fixture, **kwargs):
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    result = analysis.query_contact_analysis(fixture.store, CID, **kwargs)
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    return result


@pytest.mark.parametrize("sort", ["oldest", "newest"])
def test_three_anchor_quote_runs_prefer_ipad_without_normalizing_own_reply(
    quoted_source_store, sort
):
    fixture = quoted_source_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        native = _row(connection, fixture.native_ids[0])
        html = _row(connection, fixture.html_ids[0])
        assert native["text"] != html["text"]
        native_quote = _native_quoted_reply_presentation(
            json.loads(native["provenance"])
        )
        html_proof = json.loads(html["provenance"])
        html_quote = _html_quoted_reply_presentation(html["text"], html_proof)
        assert (
            native_quote["quoted_reply"]["speaker"]
            != html_quote["quoted_reply"]["speaker"]
        )
        assert (
            native_quote["quoted_reply"]["text"] == html_quote["quoted_reply"]["text"]
        )
        assert html_proof["primary_text_sha256"] == _sha(html_quote["display_text"])
        assert html_proof["primary_text_sha256"] != _sha(html["text"])
    page = _query(fixture, sort=sort)
    assert page["total_count"] == 6
    assert page["observation_count"] == 12
    assert {item["id"] for item in page["items"]} == set(fixture.native_ids)
    assert all(item["source_label"] == "iPad archive" for item in page["items"])
    assert all(
        item["content_text"] == fixture.replies[item["id"]] for item in page["items"]
    )
    assert all(item["observation_count"] == 2 for item in page["items"])


@pytest.mark.parametrize(
    "change",
    [
        "own_reply",
        "own_whitespace",
        "quoted_payload",
        "native_subtype_missing",
        "native_original_not_plain",
        "native_quote_unavailable",
        "native_unix_missing",
        "html_author",
    ],
)
def test_unproven_quote_pair_stays_independent(quoted_source_store, change):
    fixture = quoted_source_store
    native_id, html_id = fixture.native_ids[-1], fixture.html_ids[-1]
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        native, html = _row(connection, native_id), _row(connection, html_id)
        native_proof, html_proof = (
            json.loads(native["provenance"]),
            json.loads(html["provenance"]),
        )
        html_text = html["text"]
        if change in {"own_reply", "own_whitespace"}:
            reply = fixture.replies[html_id] + ("!" if change == "own_reply" else " ")
            html_proof["primary_text_sha256"] = _sha(reply)
            html_text = html_proof["quoted_components"][0]["text"] + "\n\n" + reply
        elif change == "quoted_payload":
            envelope = "「HTML-local remark：A different original quoted statement.」"
            html_proof["quoted_components"][0]["text"] = envelope
            html_text = envelope + "\n\n" + fixture.replies[html_id]
        elif change == "native_subtype_missing":
            native_proof["appmsg_subtype"] = None
        elif change == "native_original_not_plain":
            native_proof["quoted_reference"]["original_message_type"] = 49
            native_proof["text_components"][1]["original_message_type"] = 49
        elif change == "native_quote_unavailable":
            native_proof["text_components"] = native_proof["text_components"][:1]
        elif change == "native_unix_missing":
            native_proof.pop("unix_timestamp")
        else:
            html_proof["sender_native_id"] = "wxid_unrelated_person"
        _update(connection, html, html_proof, html_text)
        _update(
            connection,
            native,
            native_proof,
            native_components_text(native_proof["text_components"]),
        )
    page = _query(fixture)
    assert page["total_count"] == 7
    assert page["observation_count"] == 12
    for item in page["items"]:
        if item["id"] in {native_id, html_id}:
            assert item["observation_count"] == 1


def test_contradictory_native_quote_roles_do_not_acquire_event_identity(
    quoted_source_store,
):
    fixture = quoted_source_store
    native_id, html_id = fixture.native_ids[-1], fixture.html_ids[-1]
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = _row(connection, native_id)
        proof = json.loads(row["provenance"])
        proof["text_components"][1]["original_message_type"] = 49
        _update(
            connection, row, proof, native_components_text(proof["text_components"])
        )
    # Contradictory native roles stay raw rather than becoming an accepted own reply.
    page = _query(fixture)
    assert page["total_count"] == 6
    assert page["observation_count"] == 11
    html = next(item for item in page["items"] if item["id"] == html_id)
    assert html["observation_count"] == 1
    assert native_id not in {
        origin["id"] for item in page["items"] for origin in item["observations"]
    }
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        preserved = _row(connection, native_id)
    assert (
        json.loads(preserved["provenance"])["text_components"][1][
            "original_message_type"
        ]
        == 49
    )


@pytest.mark.parametrize("plain_side", ["native", "html"])
def test_plain_and_quoted_roles_never_share_an_event_key(
    quoted_source_store, plain_side
):
    fixture = quoted_source_store
    target = fixture.native_ids[-1] if plain_side == "native" else fixture.html_ids[-1]
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = _row(connection, target)
        proof = json.loads(row["provenance"])
        proof["text_extraction_kind"] = "plain_text"
        for key in (
            "quoted_components",
            "quoted_reference",
            "text_components",
            "appmsg_subtype",
        ):
            proof.pop(key, None)
        if plain_side == "native":
            proof["message_type"] = 1
        else:
            proof["locator"]["export_msgtype"] = "1"
            proof["primary_text_sha256"] = _sha(fixture.replies[target])
        _update(connection, row, proof, fixture.replies[target])
    page = _query(fixture)
    assert page["total_count"] == 7
    assert page["observation_count"] == 12


def test_two_quote_anchors_do_not_authorize_a_source_alignment(quoted_source_store):
    fixture = quoted_source_store
    with sqlite3.connect(fixture.history) as connection:
        connection.executemany(
            "DELETE FROM messages WHERE id=?",
            [
                (identifier,)
                for identifier in fixture.native_ids[2:] + fixture.html_ids[2:]
            ],
        )
    page = _query(fixture)
    assert page["total_count"] == page["observation_count"] == 4


def test_distinct_native_server_events_block_the_shared_quote_signature(
    quoted_source_store,
):
    fixture = quoted_source_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        duplicate = _row(connection, fixture.native_ids[-1])
        proof = deepcopy(json.loads(duplicate["provenance"]))
        duplicate["id"] = _identifier(
            fixture.native_source, "different-native-quote-event"
        )
        duplicate["ordinal"] += 1
        proof["record_id"] = "different-native-quote-event"
        proof["server_id"] = "7999999999999999999"
        proof["locator"].update(local_id=77, row_id=77)
        duplicate["provenance"] = json.dumps(proof)
        sources._insert_message(connection, duplicate)
    page = _query(fixture)
    assert page["total_count"] == 8
    assert page["observation_count"] == 13
    tail = {fixture.native_ids[-1], fixture.html_ids[-1], duplicate["id"]}
    assert all(
        item["observation_count"] == 1 for item in page["items"] if item["id"] in tail
    )


def test_quote_source_frame_must_still_prove_the_html_owner(quoted_source_store):
    fixture = quoted_source_store
    with sqlite3.connect(fixture.history) as connection:
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?",
                (fixture.html_snapshot,),
            ).fetchone()[0]
        )
        metadata["owner_verified"] = False
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), fixture.html_snapshot),
        )
    page = _query(fixture)
    assert page["total_count"] == page["observation_count"] == 12


def test_quote_source_alignment_keeps_the_receiving_context_boundary(
    quoted_source_store,
):
    fixture = quoted_source_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        conversation = dict(
            connection.execute(
                "SELECT * FROM conversations WHERE id=?", (fixture.html_conversation,)
            ).fetchone()
        )
        conversation["id"] = _identifier("different-quoted-receiving-context")
        conversation["peer_id"] = "wxid_different_private_peer"
        columns, marks = ",".join(conversation), ",".join("?" for _ in conversation)
        connection.execute(
            f"INSERT INTO conversations({columns}) VALUES({marks})",
            tuple(conversation.values()),
        )
        connection.execute(
            "UPDATE messages SET conversation_id=? WHERE id=?",
            (conversation["id"], fixture.html_ids[-1]),
        )
    page = _query(fixture)
    assert page["total_count"] == 7
    assert page["observation_count"] == 12


GROUP = "98000000004@chatroom"


def _cross_owner_group(fixture):
    """Use a literal group index identity with independent verified source owners."""
    html_owner = fixture.rows[1]["account_id"]
    assert html_owner != fixture.owner
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "UPDATE partitions SET account_id=?,account_key=? WHERE id=?",
            (html_owner, "account:" + html_owner, fixture.html_source),
        )
        connection.execute(
            "UPDATE conversations SET kind='group',peer_id=? WHERE id=?",
            (GROUP, fixture.rows[0]["conversation_id"]),
        )
        connection.execute(
            "UPDATE conversations SET kind='group',peer_id=?,account_key=? WHERE id=?",
            (GROUP, "account:" + html_owner, fixture.html_conversation),
        )
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?",
                (fixture.html_snapshot,),
            ).fetchone()[0]
        )
        metadata["source_account_id"] = html_owner
        metadata["owner_evidence"]["native_username"] = html_owner
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), fixture.html_snapshot),
        )
        for native_id in fixture.native_ids:
            row = _row(connection, native_id)
            proof = json.loads(row["provenance"])
            proof["locator"]["table"] = (
                "Chat_" + hashlib.md5(GROUP.encode()).hexdigest()
            )
            if proof.get("text_extraction_kind") == "quoted_reply":
                proof["quoted_reference"]["source_chat_id"] = GROUP
                proof["text_components"][1]["source_chat_id"] = GROUP
            _update(connection, row, proof, row["text"])
        for html_id in fixture.html_ids:
            row = _row(connection, html_id)
            proof = json.loads(row["provenance"])
            proof["reviewed_session_resolution"] = {
                "basis": "Exact source index and physical group member",
                "main_html_sha256": _sha("Synthetic group main HTML"),
                "index_member_path": "Synthetic/group.html",
                "physical_member_path": "Synthetic/group.html",
                "declared_data_path": "group_files/Data",
                "native_peer_evidence": None,
            }
            proof["locator"]["member_path"] = "Synthetic/group_files/Data/msg-0.js"
            proof["locator"]["member"] = {
                "member_path": "Synthetic/group_files/Data/msg-0.js",
                "sha256": _sha("Synthetic shared source group fragment"),
                "size": 4_096,
            }
            _update(connection, row, proof, row["text"])
    return SimpleNamespace(**vars(fixture), html_owner=html_owner)


@pytest.fixture
def cross_owner_group_store(native_html_store):
    return _cross_owner_group(native_html_store)


@pytest.fixture
def cross_owner_quoted_group_store(quoted_source_store):
    return _cross_owner_group(quoted_source_store)


@pytest.mark.parametrize(
    "fixture_name", ["cross_owner_group_store", "cross_owner_quoted_group_store"]
)
def test_global_group_anchor_runs_preserve_separate_verified_viewer_accounts(
    request, fixture_name
):
    fixture = request.getfixturevalue(fixture_name)
    page = _query(fixture)
    assert page["total_count"] == 6
    assert page["observation_count"] == 12
    assert page["counts"]["source_accounts"] == page["counts"]["sources"] == 2
    assert {item["id"] for item in page["items"]} == set(fixture.native_ids)
    for item in page["items"]:
        assert item["source_label"] == "iPad archive"
        assert item["observation_count"] == 2
        assert {origin["account_key"] for origin in item["observations"]} == {
            "account:" + fixture.owner,
            "account:" + fixture.html_owner,
        }


@pytest.mark.parametrize(
    "change",
    [
        "missing_review_frame",
        "missing_index_hash",
        "malformed_index_hash",
        "different_index_path",
        "different_declared_data_path",
        "unsafe_physical_path",
        "archive_hash_mismatch",
        "missing_member_frame",
        "missing_member_hash",
        "malformed_member_hash",
        "different_member_path",
        "export_namespace",
    ],
)
def test_global_group_requires_the_bound_export_index_and_fragment(
    cross_owner_group_store, change
):
    fixture = cross_owner_group_store
    html_id = fixture.html_ids[-1]
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = _row(connection, html_id)
        proof = json.loads(row["provenance"])
        frame, locator = proof["reviewed_session_resolution"], proof["locator"]
        if change == "missing_review_frame":
            proof.pop("reviewed_session_resolution")
        elif change == "missing_index_hash":
            frame.pop("main_html_sha256")
        elif change == "malformed_index_hash":
            frame["main_html_sha256"] = "x" * 64
        elif change == "different_index_path":
            frame["index_member_path"] = "Synthetic/unrelated.html"
        elif change == "different_declared_data_path":
            frame["declared_data_path"] = "unrelated_files/Data"
        elif change == "unsafe_physical_path":
            frame["physical_member_path"] = "../group.html"
        elif change == "archive_hash_mismatch":
            locator["archive_sha256"] = "f" * 64
        elif change == "missing_member_frame":
            locator.pop("member")
        elif change == "missing_member_hash":
            locator["member"].pop("sha256")
        elif change == "malformed_member_hash":
            locator["member"]["sha256"] = "x" * 64
        elif change == "different_member_path":
            locator["member"]["member_path"] = "Synthetic/unrelated_files/Data/msg-0.js"
        else:
            proof["export_msgid_namespace"] = "Unrelated device-local IDs"
            locator["export_msgid_namespace"] = "Unrelated device-local IDs"
        _update(connection, row, proof, row["text"])
    page = _query(fixture)
    assert page["total_count"] == 7
    assert page["observation_count"] == 12
    assert (
        next(item for item in page["items"] if item["id"] == html_id)[
            "observation_count"
        ]
        == 1
    )


@pytest.mark.parametrize("change", ["owner_verified", "owner_username", "source_owner"])
def test_global_group_keeps_independent_owner_verification(
    cross_owner_group_store, change
):
    fixture = cross_owner_group_store
    with sqlite3.connect(fixture.history) as connection:
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?",
                (fixture.html_snapshot,),
            ).fetchone()[0]
        )
        if change == "owner_verified":
            metadata["owner_verified"] = False
        elif change == "owner_username":
            metadata["owner_evidence"]["native_username"] = "wxid_unrelated_owner"
        else:
            metadata["source_account_id"] = "wxid_unrelated_owner"
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (json.dumps(metadata), fixture.html_snapshot),
        )
    page = _query(fixture)
    assert page["total_count"] == page["observation_count"] == 12


def test_another_literal_group_id_cannot_share_a_source_run(cross_owner_group_store):
    fixture = cross_owner_group_store
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE conversations SET peer_id=? WHERE id=?",
            ("98000000005@chatroom", fixture.html_conversation),
        )
    page = _query(fixture)
    assert page["total_count"] == page["observation_count"] == 12


def test_global_group_still_requires_three_independent_anchors(cross_owner_group_store):
    fixture = cross_owner_group_store
    with sqlite3.connect(fixture.history) as connection:
        connection.executemany(
            "DELETE FROM messages WHERE id=?",
            [
                (identifier,)
                for identifier in fixture.native_ids[2:] + fixture.html_ids[2:]
            ],
        )
    page = _query(fixture)
    assert page["total_count"] == page["observation_count"] == 4


def test_private_conversations_never_use_the_global_group_scope(
    cross_owner_group_store,
):
    fixture = cross_owner_group_store
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE conversations SET kind='direct',peer_id=? WHERE id IN (?,?)",
            (
                sources.grouping.TARGET,
                fixture.rows[0]["conversation_id"],
                fixture.html_conversation,
            ),
        )
    page = _query(fixture)
    assert page["total_count"] == page["observation_count"] == 12


def test_global_group_does_not_choose_between_distinct_native_server_events(
    cross_owner_group_store,
):
    fixture = cross_owner_group_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        duplicate = _row(connection, fixture.native_ids[-1])
        proof = deepcopy(json.loads(duplicate["provenance"]))
        duplicate["id"] = _identifier(
            fixture.native_source, "different-native-group-event"
        )
        duplicate["ordinal"] += 1
        proof["record_id"] = "different-native-group-event"
        proof["server_id"] = "7999999999999999999"
        proof["locator"].update(local_id=77, row_id=77)
        duplicate["provenance"] = json.dumps(proof)
        sources._insert_message(connection, duplicate)
    page = _query(fixture)
    assert page["total_count"] == 8
    assert page["observation_count"] == 13


@pytest.fixture
def recovered_catalog_group_store(cross_owner_group_store):
    fixture = cross_owner_group_store
    with sqlite3.connect(fixture.history) as connection:
        raw = connection.execute(
            "SELECT metadata FROM source_snapshots WHERE id=?",
            (fixture.html_snapshot,),
        ).fetchone()[0]
        metadata = json.loads(raw)
        snapshot, registry = synthetic_group_registry(
            {
                "archive": metadata["source_sha256"],
                "metadata": metadata,
                "numeric": fixture.html_snapshot,
            }
        )
        connection.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=?",
            (snapshot["metadata"], fixture.html_snapshot),
        )
        connection.row_factory = sqlite3.Row
        for html_id in fixture.html_ids:
            row = _row(connection, html_id)
            proof = json.loads(row["provenance"])
            proof.pop("reviewed_session_resolution")
            _update(connection, row, proof, row["text"])
    path = wechat_archive_path(fixture.store).parent / "html_group_source_frames.json"
    return SimpleNamespace(
        **vars(fixture), frame_registry=registry, frame_registry_path=path
    )


def test_recovered_snapshot_catalog_frames_invalidate_the_cached_independent_view(
    recovered_catalog_group_store,
):
    fixture = recovered_catalog_group_store
    without = _query(fixture)
    assert without["total_count"] == without["observation_count"] == 12
    fixture.frame_registry_path.write_text(
        json.dumps(fixture.frame_registry), encoding="utf-8"
    )
    recovered = _query(fixture)
    assert recovered["source_fingerprint"] != without["source_fingerprint"]
    assert recovered["total_count"] == 6
    assert recovered["observation_count"] == 12
    assert {item["id"] for item in recovered["items"]} == set(fixture.native_ids)
    assert all(item["source_label"] == "iPad archive" for item in recovered["items"])


@pytest.mark.parametrize(
    "change", ["fragment_hash", "fragment_path", "contradictory_reviewed_frame"]
)
def test_recovered_catalog_frame_cannot_override_the_actual_source_member(
    recovered_catalog_group_store, change
):
    fixture = recovered_catalog_group_store
    fixture.frame_registry_path.write_text(
        json.dumps(fixture.frame_registry), encoding="utf-8"
    )
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = _row(connection, fixture.html_ids[-1])
        proof = json.loads(row["provenance"])
        if change == "fragment_hash":
            proof["locator"]["member"]["sha256"] = "f" * 64
        elif change == "fragment_path":
            path = "Synthetic/group_files/Data/msg-99.js"
            proof["locator"]["member_path"] = path
            proof["locator"]["member"]["member_path"] = path
        else:
            proof["reviewed_session_resolution"] = {
                "index_member_path": "Synthetic/unrelated.html"
            }
        _update(connection, row, proof, row["text"])
    page = _query(fixture)
    assert page["total_count"] == 7
    assert page["observation_count"] == 12
    item = next(item for item in page["items"] if item["id"] == fixture.html_ids[-1])
    assert item["observation_count"] == 1
