"""Verified transcript chains keep one native original and all receiving evidence.

Code version: v1.4.2-codex.0
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_export_merge as export_merge
from app.core.wechat_archive import _identifier
from app.core.wechat_contacts import canonical_contact_id
from app.core.wechat_export_merge import _json, merge_wechat_export
from app.core.wechat_text import native_components_text
from tests import test_wechat_native_html_precedence as source_fixtures
from tests.test_wechat_html_forwarded import _render


CID = source_fixtures.CID
TARGET = source_fixtures.grouping.TARGET
RECEIVER = "wxid_receiving_peer"
native_html_store = source_fixtures.native_html_store
statement_store = source_fixtures.statement_store


def _insert(connection, table, row):
    connection.execute(
        "INSERT INTO "
        + table
        + "("
        + ",".join(row)
        + ") VALUES("
        + ",".join("?" for _ in row)
        + ")",
        tuple(row.values()),
    )


@pytest.fixture
def forwarded_origin_store(native_html_store, monkeypatch):
    """Publish a whole native/HTML transcript with a resource-only position gap."""
    fixture = native_html_store
    # These disposable provenance fixtures exercise merge proofs, not the native
    # APFS syscall. The publication suite retains the real no-fallback boundary.
    monkeypatch.setattr(export_merge, "_clonefile", shutil.copyfile)
    parent_clock = datetime.fromisoformat("2021-10-20T16:19:53+08:00")
    native_parent = _identifier(fixture.native_source, "native-forward-envelope")
    sha = hashlib.sha256(b"Synthetic source-preserving forwarded HTML").hexdigest()
    html_source = "html-" + sha[:24]
    member = {
        "member_path": "Synthetic/Receiver.html",
        "sha256": hashlib.sha256(b"Synthetic whole forwarded member").hexdigest(),
        "size": 8192,
        "crc32": "12345678",
        "zip_filename": "Synthetic/Receiver.html",
        "header_offset": 0,
        "compressed_size": 2048,
        "filename_utf8_recovered": False,
    }
    base_locator = {
        "member_path": member["member_path"],
        "page": 0,
        "message_ordinal": 1,
        "page_message_ordinal": 1,
    }
    locator = {
        **base_locator,
        "archive_sha256": sha,
        "member": member,
        "export_msgid": "413",
        "export_msgid_namespace": "MesLocalID",
        "export_msgtype": "49",
    }
    native_parts, html_parts, native_paths, html_paths = [], [], [], []
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        originals = [
            dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (identifier,)
                ).fetchone()
            )
            for identifier in fixture.native_ids
        ]
        for identifier in fixture.html_ids:
            connection.execute("DELETE FROM messages WHERE id=?", (identifier,))
        owner_originals = []
        for index, offset in enumerate((-1, 6), 1):
            stamp = datetime.fromisoformat(originals[0]["timestamp_value"]) + timedelta(
                seconds=offset
            )
            owner_record = "owner-original-anchor-" + str(index)
            owner_row = deepcopy(originals[0])
            owner_proof = json.loads(owner_row["provenance"])
            owner_proof.update(
                record_id=owner_record,
                server_id=str(8000000000000000000 + index),
                unix_timestamp=int(stamp.timestamp()),
            )
            owner_row.update(
                id=_identifier(fixture.native_source, owner_record),
                ordinal=100 if index == 1 else 107,
                sender_id=fixture.owner,
                direction="sent",
                text="Owner original anchor " + str(index),
                search_text="owner original anchor " + str(index),
                timestamp_value=stamp.isoformat(),
                timestamp_label=stamp.strftime("%d %b %Y %H:%M:%S"),
                time_sort=stamp.timestamp(),
                provenance=json.dumps(owner_proof),
            )
            _insert(connection, "messages", owner_row)
            owner_originals.append(owner_row)
        for index, original in enumerate(
            [owner_originals[0], *originals, owner_originals[1]], 1
        ):
            if index == 4:
                native_parts.append(
                    {
                        "kind": "forwarded_resource",
                        "text": "",
                        "resource_kind": "image",
                        "source_sender_id": TARGET,
                    }
                )
            stamp = datetime.fromisoformat(original["timestamp_value"])
            data_id = hashlib.md5(f"Synthetic component {index}".encode()).hexdigest()
            native_parts.append(
                {
                    "kind": "forwarded_text",
                    "text": original["text"],
                    "source_sender_id": original["sender_id"],
                    "source_sender_label": "Original author",
                    "source_create_time": str(int(stamp.timestamp())),
                    "source_timestamp": stamp.strftime("%Y-%m-%d %H:%M"),
                    "source_data_id": data_id,
                }
            )
            if original["sender_id"] == TARGET:
                native_paths.append(["native", len(native_parts)])
                html_paths.append([len(html_parts) + 1])
            html_parts.append(
                {
                    "export_msgid": "413_" + data_id,
                    "type": "",
                    "ordinal": index + 1,
                    "component_ordinal": index,
                    "sender_export_id": "synthetic-target-export-id",
                    "sender_native_id": original["sender_id"],
                    "sender_label": "Original author",
                    "avatar_path": f"Receiver_files/Portrait/{original['sender_id']}.jpg",
                    "raw_timestamp": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "text": original["text"],
                    "text_kind": "forwarded_rendered_text",
                    "source_locator": {
                        **base_locator,
                        "message_ordinal": index + 1,
                        "page_message_ordinal": index + 1,
                    },
                    "flags": ["original_nested_parent_unknown"],
                    "text_completion_parent_context": {"immediate_parent_known": False},
                    "forwarded_components": [],
                }
            )
        unmatched_clock = parent_clock - timedelta(days=1)
        unmatched_data_id = hashlib.md5(
            b"Synthetic unmatched owner component"
        ).hexdigest()
        native_parts.append(
            {
                "kind": "forwarded_text",
                "text": "Unmatched owner's original words",
                "source_sender_id": fixture.owner,
                "source_sender_label": "Owner",
                "source_create_time": str(int(unmatched_clock.timestamp())),
                "source_timestamp": unmatched_clock.strftime("%Y-%m-%d %H:%M"),
                "source_data_id": unmatched_data_id,
            }
        )
        html_parts.append(
            {
                **deepcopy(html_parts[-1]),
                "export_msgid": "413_" + unmatched_data_id,
                "ordinal": len(html_parts) + 2,
                "component_ordinal": len(html_parts) + 1,
                "sender_native_id": fixture.owner,
                "sender_export_id": "synthetic-owner-export-id",
                "sender_label": "Owner",
                "avatar_path": f"Receiver_files/Portrait/{fixture.owner}.jpg",
                "raw_timestamp": unmatched_clock.strftime("%Y-%m-%d %H:%M:%S"),
                "text": "Unmatched owner's original words",
                "source_locator": {
                    **base_locator,
                    "message_ordinal": len(html_parts) + 2,
                    "page_message_ordinal": len(html_parts) + 2,
                },
            }
        )
        original_conversation = dict(
            connection.execute(
                "SELECT * FROM conversations WHERE id=?",
                (originals[0]["conversation_id"],),
            ).fetchone()
        )
        native_conversation = {
            **original_conversation,
            "id": "wechat:" + _identifier(fixture.native_source, RECEIVER),
            "peer_id": RECEIVER,
            "title": "Receiving native conversation",
            "kind": "direct",
            "message_count": 1,
        }
        connection.execute(
            "UPDATE conversations SET message_count=8 WHERE id=?",
            (original_conversation["id"],),
        )
        _insert(connection, "conversations", native_conversation)
        provenance = json.loads(originals[0]["provenance"])
        provenance.update(
            record_id="native-forward-envelope",
            server_id="8123456789012345678",
            message_type=49,
            appmsg_subtype=19,
            text_extraction_kind="forwarded_text",
            text_components=native_parts,
            source_time=parent_clock.isoformat(),
            unix_timestamp=int(parent_clock.timestamp()),
            locator={
                "local_id": 407,
                "database": "message_4.sqlite",
                "database_relative_path": f"Documents/{hashlib.md5(fixture.owner.encode()).hexdigest()}/DB/message_4.sqlite",
                "table": "Chat_" + hashlib.md5(RECEIVER.encode()).hexdigest(),
            },
        )
        parent = {
            **originals[0],
            "id": native_parent,
            "conversation_id": native_conversation["id"],
            "ordinal": 1,
            "sender_id": fixture.owner,
            "direction": "sent",
            "text": native_components_text(native_parts),
            "search_text": native_components_text(native_parts).casefold(),
            "timestamp_value": parent_clock.isoformat(),
            "timestamp_label": "20 Oct 2021 16:19:53",
            "time_sort": parent_clock.timestamp(),
            "provenance": json.dumps(provenance),
        }
        _insert(connection, "messages", parent)
    source = {
        "source_id": html_source,
        "source_sha256": sha,
        "source_file": "Synthetic forwarded export.zip",
        "source_label": "HTML export",
        "source_account_id": fixture.owner,
        "owner_verified": True,
        "owner_evidence": {
            "native_username": fixture.owner,
            "native_contact_id": canonical_contact_id(
                fixture.owner, authority="native_account_owner"
            ),
        },
        "selected_text_members": [member],
        "source_conversation_catalog": [
            {
                "member_path": member["member_path"],
                "index_avatar_native_id": RECEIVER,
                "conversation_kind": "direct",
                "declared_async_counts": {"data_path": "Receiver_files/Data"},
            }
        ],
    }
    primary_text = "\n\n".join(part["text"] for part in html_parts)
    text = _render(
        {
            "text": primary_text,
            "text_kind": "forwarded_text",
            "quoted_components": [],
            "forwarded_components": html_parts,
        }
    )
    record_id = hashlib.sha256(
        ("html-text-v1\0" + sha + "\0" + _json(locator)).encode()
    ).hexdigest()
    connection.close()
    merge_wechat_export(
        fixture.store,
        source=source,
        snapshot_id=sha,
        records=[
            {
                "extraction_record_id": record_id,
                "conversation_key": RECEIVER,
                "peer_id": RECEIVER,
                "title": "Receiving HTML conversation",
                "kind": "direct",
                "sender_id": "synthetic-owner-export-id",
                "sender_label": "Owner",
                "direction": "sent",
                "text": text,
                "sent_at_raw": parent_clock.strftime("%Y-%m-%d %H:%M:%S"),
                "provenance": {
                    "locator": locator,
                    "sender_native_id": fixture.owner,
                    "export_msgid_namespace": "MesLocalID",
                    "text_extraction_kind": "forwarded_text",
                    "primary_text_sha256": hashlib.sha256(
                        primary_text.encode()
                    ).hexdigest(),
                    "quoted_components": [],
                    "forwarded_components": html_parts,
                },
                "flags": [],
            }
        ],
    )
    owner_id = canonical_contact_id(fixture.owner, authority="native_account_owner")
    with sqlite3.connect(fixture.registry) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO contacts VALUES(?,?,?,?,?,?)",
            (owner_id, "wechat", "", fixture.owner, "wechat_account", "verified"),
        )
        connection.execute(
            "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
            (
                html_source,
                "synthetic-owner-export-id",
                "",
                "",
                owner_id,
                "corroborated_export_username",
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
            (
                fixture.native_source,
                fixture.owner,
                "",
                "",
                owner_id,
                "native_account_owner",
                "{}",
            ),
        )
    return SimpleNamespace(
        **{
            **vars(fixture),
            "original_ids": fixture.native_ids,
            "native_parent": native_parent,
            "html_parent": _identifier(html_source, record_id),
            "html_source": html_source,
            "native_paths": native_paths,
            "html_paths": html_paths,
            "owner_originals": [row["id"] for row in owner_originals],
            "original_conversation": originals[0]["conversation_id"],
            "owner_id": owner_id,
        }
    )


def _update_proof(fixture, parent, mutate):
    with sqlite3.connect(fixture.history) as connection:
        proof = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (parent,)
            ).fetchone()[0]
        )
        mutate(proof)
        if parent == fixture.html_parent:
            primary_text = "\n\n".join(
                part["text"] for part in proof["forwarded_components"]
            )
            proof["primary_text_sha256"] = hashlib.sha256(
                primary_text.encode()
            ).hexdigest()
            text = _render(
                {
                    "text": primary_text,
                    "text_kind": "forwarded_text",
                    "quoted_components": [],
                    "forwarded_components": proof["forwarded_components"],
                }
            )
            connection.execute(
                "UPDATE messages SET text=?,search_text=? WHERE id=?",
                (text, text.casefold(), parent),
            )
        elif parent == fixture.native_parent:
            text = native_components_text(proof["text_components"])
            connection.execute(
                "UPDATE messages SET text=?,search_text=? WHERE id=?",
                (text, text.casefold(), parent),
            )
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), parent)
        )


def _export(fixture, page):
    return list(
        analysis.export_contact_analysis_items(
            fixture.store,
            CID,
            source_fingerprint=page["source_fingerprint"],
            source_occurrences=True,
        )
    )


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_complete_forwarded_chain_has_one_ipad_original_per_statement(
    forwarded_origin_store, sort
):
    fixture = forwarded_origin_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID, sort=sort)
    assert page["total_count"] == 6
    assert page["observation_count"] == 18
    assert {item["id"] for item in page["items"]} == set(fixture.original_ids)
    for item in page["items"]:
        assert not item["is_embedded"]
        assert item["conversation_id"] == fixture.original_conversation
        assert item["source_label"] == "iPad archive"
        assert item["observation_count"] == 3
        assert not item["time_conflict"] and not item["time_uncertain"]
    exported = _export(fixture, page)
    assert len(exported) == 18
    for index, path in enumerate(fixture.native_paths, 1):
        native = next(item for item in exported if item["component_path"] == path)
        html = next(
            item
            for item in exported
            if item["component_path"] == fixture.html_paths[index - 1]
        )
        primary = next(
            item for item in exported if item["id"] == fixture.original_ids[index - 1]
        )
        assert native["statement_id"] == html["statement_id"] == primary["statement_id"]
        assert native["parent_message_id"] == fixture.native_parent
        assert html["parent_message_id"] == fixture.html_parent
        assert native["original_conversation_unknown"]
        assert html["original_conversation_unknown"]
        assert native[
            "anchor_id"
        ] == "wechat_message_" + fixture.native_parent + "_forwarded_" + str(path[-1])
        assert html[
            "anchor_id"
        ] == "wechat_message_" + fixture.html_parent + "_forwarded_" + str(
            fixture.html_paths[index - 1][-1]
        )
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_unmatched_owner_words_do_not_acquire_an_original_conversation(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store
    page = analysis.query_contact_analysis(
        fixture.store, fixture.owner_id, query="Unmatched owner's original words"
    )
    assert page["total_count"] == 1 and page["observation_count"] == 2
    assert all(item["original_conversation_unknown"] for item in page["items"])
    assert all(item["is_embedded"] for item in page["items"])


@pytest.mark.parametrize(
    "damage", ["data_id", "duplicate_id", "author", "body", "clock", "order"]
)
def test_html_transcript_bridge_requires_the_complete_component_sequence(
    forwarded_origin_store, damage
):
    fixture = forwarded_origin_store

    def mutate(proof):
        parts = proof["forwarded_components"]
        first = fixture.html_paths[0][0] - 1
        if damage == "data_id":
            parts[first]["export_msgid"] = "413_" + "f" * 32
        elif damage == "duplicate_id":
            parts[first]["export_msgid"] = parts[first + 1]["export_msgid"]
        elif damage == "author":
            parts[first]["sender_native_id"] = fixture.owner
            parts[first]["avatar_path"] = f"Receiver_files/Portrait/{fixture.owner}.jpg"
        elif damage == "body":
            parts[first]["text"] += " changed"
        elif damage == "clock":
            parts[first]["raw_timestamp"] = "2020-08-12 13:46:45"
        else:
            parts.reverse()
            for index, part in enumerate(parts, 1):
                part.update(ordinal=index + 1, component_ordinal=index)
                part["source_locator"].update(
                    message_ordinal=index + 1, page_message_ordinal=index + 1
                )

    _update_proof(fixture, fixture.html_parent, mutate)
    page = analysis.query_contact_analysis(fixture.store, CID)
    exported = _export(fixture, page)
    html = [
        item for item in exported if item["parent_message_id"] == fixture.html_parent
    ]
    assert html
    native_originals = {
        item["statement_id"] for item in exported if item["id"] in fixture.original_ids
    }
    assert all(item["statement_id"] not in native_originals for item in html)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_ids", {"svrid": "8999999999999999999"}),
        ("source_chat_id", "wxid_conflicting_original_peer"),
        ("source_create_time", "0"),
    ],
)
def test_explicit_native_conflict_cannot_be_overridden_by_neighbor_matches(
    forwarded_origin_store, field, value
):
    fixture = forwarded_origin_store
    _update_proof(
        fixture,
        fixture.native_parent,
        lambda p: p["text_components"][fixture.native_paths[0][-1] - 1].update(
            {field: value}
        ),
    )
    page = analysis.query_contact_analysis(fixture.store, CID)
    exported = _export(fixture, page)
    first = next(item for item in exported if item["id"] == fixture.original_ids[0])
    receiving = next(
        item
        for item in exported
        if item["parent_message_id"] == fixture.native_parent
        and item["component_path"] == fixture.native_paths[0]
    )
    assert receiving["statement_id"] != first["statement_id"]


def test_two_same_author_body_clock_events_are_not_resolved_by_a_forward(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        duplicate = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.original_ids[0],)
            ).fetchone()
        )
        duplicate["id"] = "d" * 64
        proof = json.loads(duplicate["provenance"])
        proof["server_id"] = "8999999999999999999"
        duplicate["provenance"] = json.dumps(proof)
        _insert(connection, "messages", duplicate)
    page = analysis.query_contact_analysis(fixture.store, CID)
    exported = _export(fixture, page)
    originals = {
        item["statement_id"]
        for item in exported
        if item["id"] in {fixture.original_ids[0], duplicate["id"]}
    }
    assert len(originals) == 2
    receiving = next(
        item
        for item in exported
        if item["parent_message_id"] == fixture.native_parent
        and item["component_path"] == fixture.native_paths[0]
    )
    assert receiving["statement_id"] not in originals


@pytest.mark.parametrize("original_type", [49, None, True, "1"])
def test_explicit_conflicting_forwarded_type_never_inherits_a_plain_text_origin(
    forwarded_origin_store, original_type
):
    fixture = forwarded_origin_store
    _update_proof(
        fixture,
        fixture.native_parent,
        lambda proof: proof["text_components"][fixture.native_paths[0][-1] - 1].update(
            original_message_type=original_type
        ),
    )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID)
    exported = _export(fixture, page)
    originals = {
        item["statement_id"] for item in exported if item["id"] in fixture.original_ids
    }
    receiving = [item for item in exported if item["is_embedded"]]
    assert len(originals) == 6
    assert receiving and all(
        item["statement_id"] not in originals for item in receiving
    )
    # Invalid field types are rejected by the established native metadata parser.
    # Valid but contradictory types retain their source references without linking.
    expected = 12 if original_type is True or original_type == "1" else 18
    assert page["total_count"] == page["observation_count"] == expected
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def _reindex_html(parts):
    for index, part in enumerate(parts, 1):
        part.update(ordinal=index + 1, component_ordinal=index)
        part["source_locator"].update(
            message_ordinal=index + 1, page_message_ordinal=index + 1
        )


def test_a_consistently_reversed_transcript_keeps_the_same_original_events(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store
    _update_proof(
        fixture, fixture.native_parent, lambda proof: proof["text_components"].reverse()
    )

    def reverse_html(proof):
        proof["forwarded_components"].reverse()
        _reindex_html(proof["forwarded_components"])

    _update_proof(fixture, fixture.html_parent, reverse_html)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 6 and page["observation_count"] == 18
    assert {item["id"] for item in page["items"]} == set(fixture.original_ids)


def test_one_party_only_is_insufficient_to_invent_an_original_private_thread(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store

    def remove_native_owner_anchors(proof):
        proof["text_components"] = [
            part
            for part in proof["text_components"]
            if not part.get("text", "").startswith("Owner original anchor ")
        ]

    def remove_html_owner_anchors(proof):
        proof["forwarded_components"] = [
            part
            for part in proof["forwarded_components"]
            if not part.get("text", "").startswith("Owner original anchor ")
        ]
        _reindex_html(proof["forwarded_components"])

    _update_proof(fixture, fixture.native_parent, remove_native_owner_anchors)
    _update_proof(fixture, fixture.html_parent, remove_html_owner_anchors)
    page = analysis.query_contact_analysis(fixture.store, CID)
    exported = _export(fixture, page)
    originals = {
        item["statement_id"] for item in exported if item["id"] in fixture.original_ids
    }
    assert page["total_count"] == 12 and page["observation_count"] == 18
    receiving = [item for item in exported if item["is_embedded"]]
    assert receiving and all(
        item["statement_id"] not in originals for item in receiving
    )


@pytest.mark.parametrize("change", ["receiving_peer", "parent_clock", "direction"])
def test_html_parent_scope_must_agree_before_component_ids_can_bridge(
    forwarded_origin_store, change
):
    fixture = forwarded_origin_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM messages WHERE id=?", (fixture.html_parent,)
        ).fetchone()
        proof = json.loads(row["provenance"])
        if change == "receiving_peer":
            peer = "wxid_different_receiving_peer"
            connection.execute(
                "UPDATE conversations SET peer_id=? WHERE id=?",
                (peer, row["conversation_id"]),
            )
            metadata = json.loads(
                connection.execute(
                    "SELECT metadata FROM source_snapshots WHERE id=?",
                    (proof["source_snapshot"],),
                ).fetchone()[0]
            )
            metadata["source_conversation_catalog"][0]["index_avatar_native_id"] = peer
            connection.execute(
                "UPDATE source_snapshots SET metadata=? WHERE id=?",
                (json.dumps(metadata), proof["source_snapshot"]),
            )
        elif change == "direction":
            connection.execute(
                "UPDATE messages SET direction='received' WHERE id=?",
                (fixture.html_parent,),
            )
        else:
            stamp = datetime.fromisoformat(row["timestamp_value"]) + timedelta(
                seconds=1
            )
            proof["source_time"] = stamp.strftime("%Y-%m-%d %H:%M:%S")
            connection.execute(
                "UPDATE messages SET timestamp_value=?,provenance=? WHERE id=?",
                (stamp.isoformat(), json.dumps(proof), fixture.html_parent),
            )
    page = analysis.query_contact_analysis(fixture.store, CID)
    exported = _export(fixture, page)
    originals = {
        item["statement_id"] for item in exported if item["id"] in fixture.original_ids
    }
    html = [
        item for item in exported if item["parent_message_id"] == fixture.html_parent
    ]
    assert html and all(item["statement_id"] not in originals for item in html)


def test_nested_transcript_links_preserve_each_receiving_component_anchor(
    forwarded_origin_store,
):
    fixture = forwarded_origin_store

    def nest_native(proof):
        children = proof["text_components"][:3]
        proof["text_components"] = [
            {
                "kind": "forwarded_group",
                "text": native_components_text(children),
                "text_components": children,
            },
            *proof["text_components"][3:],
        ]

    def nest_html(proof):
        children = proof["forwarded_components"][:3]
        wrapper = {
            **deepcopy(children[0]),
            "export_msgid": "413_nested",
            "text_kind": "forwarded_text",
            "text": "\n\n".join(child["text"] for child in children),
            "forwarded_components": children,
        }
        proof["forwarded_components"] = [wrapper, *proof["forwarded_components"][3:]]
        _reindex_html(proof["forwarded_components"])

    _update_proof(fixture, fixture.native_parent, nest_native)
    _update_proof(fixture, fixture.html_parent, nest_html)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 6 and page["observation_count"] == 18
    exported = _export(fixture, page)
    native = next(
        item for item in exported if item["component_path"] == ["native", 1, 2]
    )
    html = next(item for item in exported if item["component_path"] == [1, 2])
    original = next(item for item in exported if item["id"] == fixture.original_ids[0])
    assert native["statement_id"] == html["statement_id"] == original["statement_id"]
    for item in (native, html):
        assert (
            item["anchor_id"]
            == "wechat_message_"
            + item["parent_message_id"]
            + "_forwarded_1_forwarded_2"
        )
        assert item["original_conversation_unknown"]
        location = analysis.message_source_location(fixture.store, item["id"])
        assert location["anchor_id"] == item["anchor_id"]
        assert location["conversation_id"] == item["conversation_id"]
