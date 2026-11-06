"""Forwarded projections reuse canonical text and never open referenced media.

Code version: v1.1.1-codex.0
"""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3
from unittest.mock import patch

import pytest

from app.core.wechat_archive import _identifier, wechat_archive_path
from app.core.wechat_forwarded import (
    project_forwarded_references,
    project_native_forwarded,
)
from app.core.wechat_reconcile import native_text_metadata, native_text_metadata_extends
from app.core.wechat_resource_index import (
    _SCHEMA as RESOURCE_SCHEMA,
    resource_index_path,
)
from app.core.wechat_text import extract_wechat_text, native_components_text
from tests.test_wechat_reconcile import merge, record, rows, source


AUTHORITY = "user_confirmation_with_native_sequence_corroboration"
PEER = "123456789@chatroom"


def reference_fixture(store, *, canonical_parent=False):
    metadata = source()
    target = record(
        metadata,
        "original",
        peer=PEER,
        text="Original <b>literal</b> body",
        sender_id=metadata["source_account_id"],
        sender_display_name="Original sender",
        direction="sent",
        server_id="111",
    )
    parent = record(
        metadata,
        "forwarded-parent",
        peer="private-recipient",
        text="Forwarded conversation",
        message_type=49,
        text_extraction_kind="forwarded_text",
        server_id="222",
    )
    merge(store, [metadata], [target, parent])
    target_id = _identifier(metadata["source_id"], target["extraction_record_id"])
    parent_id = _identifier(metadata["source_id"], parent["extraction_record_id"])
    if canonical_parent:
        replacement = _identifier("canonical_resource_occurrence")
        with closing(sqlite3.connect(wechat_archive_path(store))) as connection, connection:
            connection.execute(
                "UPDATE messages SET id=? WHERE id=?", (replacement, parent_id)
            )
        parent_id = replacement
    resource_id = _identifier("synthetic-image")
    locator = {
        "database": "message_4.sqlite",
        "table": "Chat_" + hashlib.md5(PEER.encode()).hexdigest(),
        "local_id": 12,
    }
    with closing(sqlite3.connect(wechat_archive_path(store))) as connection, connection:
        connection.row_factory = sqlite3.Row
        original = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (target_id,)
            ).fetchone()
        )
        conversation = tuple(
            connection.execute(
                "SELECT * FROM conversations WHERE id=?", (original["conversation_id"],)
            ).fetchone()
        )
    image_provenance = {
        "record_id": "native-image",
        "locator": locator,
        "message_type": 3,
        "unix_timestamp": target["unix_timestamp"] - 24,
        "text_extraction_kind": "resource_placeholder",
        "resource_reference": {
            "kind": "image",
            "account_id": metadata["source_account_id"],
            "peer_id": PEER,
        },
    }
    resource_path = resource_index_path(store)
    with closing(sqlite3.connect(resource_path)) as connection, connection:
        connection.executescript(RESOURCE_SCHEMA)
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", conversation
        )
        image = {
            **original,
            "id": resource_id,
            "text": "[Image]",
            "provenance": json.dumps(image_provenance),
        }
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(image.values()),
        )
        connection.execute(
            "INSERT INTO archive_info VALUES('history_fingerprint','stale-on-purpose')"
        )
    declaration = {
        "schema_version": 1,
        "authority": AUTHORITY,
        "items": [
            {
                "kind": "native_resource",
                "resource_id": resource_id,
                "account_id": metadata["source_account_id"],
                "peer_id": PEER,
                "message_type": 3,
                "unix_timestamp": image_provenance["unix_timestamp"],
                "source_locator": locator,
            },
            {
                "kind": "message",
                "message_id": target_id,
                "account_id": metadata["source_account_id"],
                "peer_id": PEER,
            },
        ],
    }
    set_references(store, parent_id, declaration)
    return metadata, parent_id, target_id, declaration


def set_references(store, parent_id, declaration):
    with closing(sqlite3.connect(wechat_archive_path(store))) as connection, connection:
        row = connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (parent_id,)
        ).fetchone()
        provenance = json.loads(row[0])
        provenance["forwarded_references"] = declaration
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(provenance), parent_id),
        )
    return provenance


@pytest.mark.parametrize("canonical_parent", [False, True])
def test_canonical_references_reuse_existing_text_and_stale_resource_metadata(
    tmp_path, canonical_parent
):
    metadata, parent_id, target_id, declaration = reference_fixture(
        tmp_path, canonical_parent=canonical_parent
    )
    before = wechat_archive_path(tmp_path).read_bytes()
    resource_before = resource_index_path(tmp_path).read_bytes()
    with patch(
        "app.core.wechat_mac_resources.WechatMacResourceResolver.resolve",
        side_effect=AssertionError("Media must not be opened"),
    ):
        result = project_forwarded_references(tmp_path, parent_message_id=parent_id)
    assert result["title"] == "Forwarded conversation"
    assert result["authority"] == AUTHORITY
    assert result["status"] == "canonical_references"
    image, text = result["items"]
    assert image.messaging["resource"]["kind"] == "image"
    assert image.messaging["resource"]["can_reveal"] is False
    assert image.content_text == ""
    assert text.content_text == "Original <b>literal</b> body"
    assert text.content_html == ""
    assert text.author_label == "Original sender"
    assert text.messaging["direction"] == "sent"
    assert text.messaging["sender_id"] == metadata["source_account_id"]
    assert text.messaging["canonical_message_id"] == target_id
    assert text.messaging["forwarded_relationship_authority"] == AUTHORITY
    assert text.last_seen_at and text.messaging["timestamp_label"]
    assert image.stable_id != text.stable_id != target_id
    assert "Original <b>literal</b> body" not in json.dumps(declaration)
    assert len(rows(tmp_path)) == 2
    assert wechat_archive_path(tmp_path).read_bytes() == before
    assert resource_index_path(tmp_path).read_bytes() == resource_before


def test_preview_requires_an_existing_parent_and_unchanged_native_identity(tmp_path):
    _, parent_id, _, declaration = reference_fixture(tmp_path)
    with closing(sqlite3.connect(wechat_archive_path(tmp_path))) as connection, connection:
        original = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (parent_id,)
            ).fetchone()[0]
        )
    assert project_forwarded_references(
        tmp_path, parent_message_id=parent_id, provenance=original
    )
    altered = {**original, "server_id": "999"}
    assert (
        project_forwarded_references(
            tmp_path, parent_message_id=parent_id, provenance=altered
        )
        is None
    )
    assert (
        project_forwarded_references(
            tmp_path, parent_message_id="f" * 64, provenance=original
        )
        is None
    )
    assert (
        native_text_metadata({"forwarded_references": declaration}, {})[
            "forwarded_references"
        ]
        == declaration
    )


@pytest.mark.parametrize(
    "failure",
    [
        "account",
        "peer",
        "missing",
        "self",
        "locator",
        "resource_time",
        "resource_peer",
        "resource_missing",
        "resource_partition",
    ],
)
def test_references_fail_closed_for_wrong_scope_identity_and_stale_evidence(
    tmp_path, failure
):
    _, parent_id, _, declaration = reference_fixture(tmp_path)
    changed = deepcopy(declaration)
    if failure == "account":
        changed["items"][1]["account_id"] = "wxid_other_account"
    elif failure == "peer":
        changed["items"][1]["peer_id"] = "different-chat"
    elif failure == "missing":
        changed["items"][1]["message_id"] = "f" * 64
    elif failure == "self":
        changed["items"][1]["message_id"] = parent_id
    elif failure == "locator":
        changed["items"][0]["source_locator"]["local_id"] += 1
    elif failure == "resource_time":
        changed["items"][0]["unix_timestamp"] += 1
    elif failure == "resource_peer":
        changed["items"][0]["peer_id"] = "other@chatroom"
    elif failure == "resource_missing":
        changed["items"][0]["resource_id"] = "f" * 64
    elif failure == "resource_partition":
        with closing(sqlite3.connect(resource_index_path(tmp_path))) as connection, connection:
            connection.execute(
                "UPDATE conversations SET partition_id='ios-wrong-account'"
            )
    set_references(tmp_path, parent_id, changed)
    assert project_forwarded_references(tmp_path, parent_message_id=parent_id) is None


def test_references_cannot_read_a_canonical_target_from_another_account(tmp_path):
    _, parent_id, _, declaration = reference_fixture(tmp_path)
    other = source("wxid_account_b")
    observation = record(
        other, "other-original", peer=PEER, text="Other account private text"
    )
    merge(tmp_path, [other], [observation])
    declaration["items"][1]["message_id"] = _identifier(
        other["source_id"], observation["extraction_record_id"]
    )
    set_references(tmp_path, parent_id, declaration)
    assert project_forwarded_references(tmp_path, parent_message_id=parent_id) is None


@pytest.mark.parametrize(
    "failure", ["authority", "body", "id", "type", "path", "many", "version"]
)
def test_reference_schema_rejects_unsupported_or_body_carrying_declarations(
    tmp_path, failure
):
    _, _, _, reference = reference_fixture(tmp_path)
    if failure == "authority":
        reference["authority"] = "native_source_ids"
    elif failure == "body":
        reference["items"][1]["text"] = "A duplicated body"
    elif failure == "id":
        reference["items"][1]["message_id"] = 123
    elif failure == "type":
        reference["items"][0]["message_type"] = True
    elif failure == "path":
        reference["items"][0]["source_locator"]["database"] = "../../private.sqlite"
    elif failure == "many":
        reference["items"] *= 51
    elif failure == "version":
        reference["schema_version"] = True
    with pytest.raises(ValueError):
        native_text_metadata({"forwarded_references": reference}, {})


def test_native_reconciliation_retains_body_free_references_and_replays(tmp_path):
    metadata, _, _, reference = reference_fixture(tmp_path / "source")
    observation = record(
        metadata,
        "new-parent",
        text="Forwarded conversation",
        message_type=49,
        text_extraction_kind="forwarded_text",
        forwarded_references=reference,
    )
    store = tmp_path / "new-store"
    result = merge(store, [metadata], [observation])
    assert result["inserted_messages"] == 1
    provenance = json.loads(rows(store)[0][11])
    assert provenance["forwarded_references"] == reference
    before = wechat_archive_path(store).read_bytes()
    assert not merge(store, [metadata], [observation])["changed"]
    assert wechat_archive_path(store).read_bytes() == before
    crossing = deepcopy(observation)
    crossing["forwarded_references"]["items"][0]["account_id"] = "wxid_other"
    with pytest.raises(ValueError, match="verified native account"):
        merge(store, [metadata], [crossing])


@pytest.mark.parametrize("quoted", [False, True])
def test_native_forwarded_children_keep_their_own_speakers_text_and_clocks(quoted):
    owner = "wxid_account_a"
    components = [
        {
            "kind": "forwarded_text",
            "text": "Literal <img src=x> body",
            "source_sender_id": owner,
            "source_sender_label": "Original self",
            "source_create_time": "1793262652",
            "forwarded_item_path": [1],
        },
        {
            "kind": "forwarded_text",
            "text": "Different author, no own clock",
            "source_sender_id": "wxid_other",
            "source_sender_label": "Other participant",
            "forwarded_item_path": [2],
        },
    ]
    provenance = {
        "message_type": 49,
        "text_extraction_kind": "forwarded_text",
        "appmsg_subtype": 19,
        "text_components": components,
        "unix_timestamp": 1793264247,
    }
    if quoted:
        provenance.update(
            {
                "text_extraction_kind": "quoted_reply",
                "appmsg_subtype": 57,
                "text_components": [
                    {"kind": "reply_text", "text": "Outer reply"},
                    {
                        "kind": "quoted_forwarded_text",
                        "text": "Original aggregate",
                        "source_sender_label": "Outer forwarder",
                        "text_components": components,
                    },
                ],
            }
        )
    before = deepcopy(provenance)
    result = project_native_forwarded(provenance, parent_id="a" * 64, account_id=owner)
    first, second = result["items"]
    assert result["authority"] == "native_forwarded_envelope"
    assert first.author_label == "Original self"
    assert first.messaging["direction"] == "sent"
    assert first.content_text == components[0]["text"]
    assert first.last_seen_at == "2026-10-29T16:30:52+08:00"
    assert first.messaging["timestamp_label"] == "29 Oct 2026 16:30:52"
    assert second.author_label == "Other participant"
    assert second.messaging["direction"] == "received"
    assert second.last_seen_at == second.messaging["timestamp_label"] == ""
    assert first.messaging["contact"]["identity_status"] == "unresolved"
    assert provenance == before


@pytest.mark.parametrize("kind", ["plain_text", "app_text", "system_text"])
def test_native_projector_never_splits_ordinary_text_based_on_components(kind):
    provenance = {
        "message_type": 49,
        "text_extraction_kind": kind,
        "text_components": [{"kind": "forwarded_text", "text": "Not sufficient proof"}],
    }
    assert (
        project_native_forwarded(
            provenance, parent_id="a" * 64, account_id="wxid_account_a"
        )
        is None
    )


def test_native_unavailable_quote_and_unknown_html_trees_are_not_invented():
    provenance = {
        "message_type": 49,
        "text_extraction_kind": "quoted_reply",
        "text_components": [{"kind": "reply_text", "text": "Recovered reply"}],
        "quoted_reference": {"content_unavailable": True},
    }
    assert (
        project_native_forwarded(
            provenance, parent_id="a" * 64, account_id="wxid_account_a"
        )
        is None
    )
    html = {
        "message_type": 49,
        "text_extraction_kind": "forwarded_text",
        "forwarded_components": [{"text": "An HTML child"}],
    }
    assert (
        project_native_forwarded(html, parent_id="a" * 64, account_id="wxid_account_a")
        is None
    )


def test_native_mixed_tree_survives_reconcile_without_changing_primary_text(tmp_path):
    owner = source()
    xml = (
        "<msg><appmsg><type>19</type><title>Source bundle</title><recorditem>"
        '<recordinfo><datalist><dataitem datatype="1"><datadesc>Before</datadesc></dataitem>'
        '<dataitem datatype="2"><sourcename>Image sender</sourcename>'
        "<srcMsgCreateTime>1628330403</srcMsgCreateTime><dataitemsource>"
        "<fromusr>wxid_image</fromusr></dataitemsource><datadesc>Not a caption</datadesc></dataitem>"
        '<dataitem datatype="17"><sourcename>Forwarding participant</sourcename>'
        "<srcMsgCreateTime>1628330503</srcMsgCreateTime><recordinfo><datalist>"
        '<dataitem datatype="1"><sourcename>Original participant</sourcename>'
        "<datadesc>Inside</datadesc></dataitem>"
        '<dataitem datatype="8"><datatitle>Never a text utterance.pdf</datatitle></dataitem>'
        "</datalist></recordinfo></dataitem>"
        '<dataitem datatype="1"><datadesc>After</datadesc></dataitem>'
        "</datalist></recordinfo></recorditem></appmsg></msg>"
    )
    parsed = extract_wechat_text(49, xml)
    assert parsed["text"] == "Before\n\nInside\n\nAfter"
    merge(tmp_path, [owner], [record(owner, message_type=49, **parsed)])
    history = wechat_archive_path(tmp_path)
    before = history.read_bytes()
    from app.core.chat_history_browser import query_chat_history

    session = query_chat_history(tmp_path, source="wechat").sessions[0]
    page = query_chat_history(tmp_path, source="wechat", session=session.stable_id)
    assert len(page.items) == 1
    parent = page.items[0]
    assert parent.content_text == parsed["text"]
    bundle = parent.messaging["forwarded_record"]
    assert bundle["title"] == "Source bundle"
    assert len(bundle["items"]) == 4
    image, group = bundle["items"][1:3]
    assert image.author_label == "Image sender"
    assert image.messaging["resource"]["kind"] == "image"
    assert image.messaging["resource"]["can_reveal"] is False
    assert image.content_text == image.messaging["display_text"] == ""
    assert group.author_label == "Forwarding participant"
    assert group.messaging["display_text"] == ""
    original, file = group.messaging["forwarded_record"]["items"]
    assert original.author_label == "Original participant"
    assert original.last_seen_at == "" and group.last_seen_at
    assert file.messaging["resource"]["label"] == "File"
    assert file.last_seen_at == ""
    assert "Never a text" not in str(parent.messaging)
    assert history.read_bytes() == before


def test_legacy_flattened_paths_recover_groups_without_guessing_wrapper_identity():
    components = [
        {"kind": "forwarded_text", "text": "First", "forwarded_item_path": [1]},
        {
            "kind": "forwarded_text",
            "text": "Nested",
            "forwarded_item_path": [2, 1],
            "source_sender_label": "Actual inner author",
            "source_create_time": "1628330403",
        },
        {"kind": "forwarded_text", "text": "Last", "forwarded_item_path": [3]},
    ]
    proof = {
        "message_type": 49,
        "text_extraction_kind": "forwarded_text",
        "text_components": components,
    }
    before = deepcopy(proof)
    result = project_native_forwarded(
        proof, parent_id="a" * 64, account_id="wxid_owner"
    )
    first, group, last = result["items"]
    assert [first.content_text, group.content_text, last.content_text] == [
        "First",
        "Nested",
        "Last",
    ]
    assert group.author_label == "Unknown sender"
    assert group.last_seen_at == "" and group.messaging["direction"] == "unknown"
    inner = group.messaging["forwarded_record"]["items"][0]
    assert inner.author_label == "Actual inner author" and inner.last_seen_at
    assert proof == before


def test_explicit_forwarded_quote_reuses_native_reply_and_original_speaker_roles():
    raw = (
        "<msg><appmsg><type>19</type><recorditem><recordinfo><datalist>"
        '<dataitem datatype="1"><sourcename>Replying participant</sourcename>'
        "<appmsg><type>57</type><title>Exact reply，😀</title><refermsg><type>1</type>"
        "<displayname>Earlier participant</displayname><content>Earlier statement</content>"
        "</refermsg></appmsg></dataitem></datalist></recordinfo></recorditem></appmsg></msg>"
    )
    parsed = extract_wechat_text(49, raw)
    proof = {**parsed, "message_type": 49}
    child = project_native_forwarded(
        proof, parent_id="a" * 64, account_id="wxid_owner"
    )["items"][0]
    assert child.author_label == "Replying participant"
    assert child.messaging["display_text"] == "Exact reply，😀"
    assert child.messaging["quoted_reply"]["speaker"] == "Earlier participant"
    assert child.messaging["quoted_reply"]["text"] == "Earlier statement"
    assert child.content_text == "Exact reply，😀\n\nEarlier statement"


@pytest.mark.parametrize(
    "component",
    [
        {
            "kind": "forwarded_resource",
            "text": "Invented caption",
            "resource_kind": "image",
        },
        {"kind": "forwarded_resource", "text": "", "resource_kind": []},
        {
            "kind": "forwarded_group",
            "text": "Wrong",
            "text_components": [{"kind": "forwarded_text", "text": "Actual"}],
        },
        {
            "kind": "forwarded_quote",
            "text": "Reply",
            "text_components": [{"kind": "reply_text", "text": "Reply"}],
            "quoted_reference": {},
            "appmsg_subtype": 19,
        },
    ],
)
def test_native_metadata_rejects_resource_bodies_and_conflicting_new_tree_roles(
    component,
):
    with pytest.raises(ValueError):
        native_text_metadata({"text_components": [component]}, {})


def test_native_metadata_accepts_bounded_nested_tree_and_empty_metadata_separators():
    leaf = {"kind": "forwarded_text", "text": "Complete text"}
    component = leaf
    for _ in range(8):
        component = {
            "kind": "forwarded_group",
            "text": "Complete text",
            "text_components": [component],
        }
    resource = {"kind": "forwarded_resource", "text": "", "resource_kind": "unknown"}
    proof = {"text_components": [resource, component, resource]}
    assert native_text_metadata(proof, {}) == proof
    assert native_components_text(proof["text_components"]) == leaf["text"]


def metadata_extension_fixture():
    old = {
        "appmsg_subtype": 19,
        "text_components": [
            {
                "kind": "forwarded_text",
                "text": "First",
                "forwarded_item_path": [1],
                "source_sender_id": "wxid_first",
                "source_sender_label": "First source label",
            },
            {
                "kind": "forwarded_text",
                "text": "Inside",
                "forwarded_item_path": [3, 1],
                "source_sender_id": "wxid_inside",
                "source_create_time": "1628330403",
            },
        ],
    }
    resource = {
        "kind": "forwarded_resource",
        "text": "",
        "resource_kind": "image",
        "forwarded_item_path": [2],
    }
    new = {
        "appmsg_subtype": 19,
        "text_components": [
            deepcopy(old["text_components"][0]),
            resource,
            {
                "kind": "forwarded_group",
                "text": "Inside",
                "forwarded_item_path": [3],
                "source_sender_label": "Explicit wrapper source",
                "text_components": [deepcopy(old["text_components"][1])],
            },
        ],
    }
    return old, new


def test_flat_metadata_extension_only_adds_resources_and_explicit_path_boundaries():
    previous, candidate = metadata_extension_fixture()
    before = deepcopy((previous, candidate))
    assert native_text_metadata_extends(previous, candidate)
    assert native_components_text(
        previous["text_components"]
    ) == native_components_text(candidate["text_components"])
    assert (previous, candidate) == before
    assert native_text_metadata_extends(previous, deepcopy(previous))


@pytest.mark.parametrize(
    "change",
    [
        "text",
        "sender",
        "label",
        "clock",
        "added_sender",
        "removed_leaf",
        "extra_leaf",
        "order",
        "path",
        "group_path",
        "resource_body",
        "quote_reference",
        "subtype",
        "other_field",
    ],
)
def test_metadata_extension_rejects_changed_text_identity_clocks_or_ambiguous_trees(
    change,
):
    previous, candidate = metadata_extension_fixture()
    first = candidate["text_components"][0]
    group = candidate["text_components"][2]
    if change == "text":
        first["text"] = "Changed"
    elif change == "sender":
        first["source_sender_id"] = "wxid_other"
    elif change == "label":
        first["source_sender_label"] = "Matching nickname is not identity"
    elif change == "clock":
        group["text_components"][0]["source_create_time"] = "1628330404"
    elif change == "added_sender":
        first["source_timestamp"] = "Newly asserted clock"
    elif change == "removed_leaf":
        candidate["text_components"].pop(0)
    elif change == "extra_leaf":
        candidate["text_components"].append(
            {
                "kind": "forwarded_text",
                "text": "Extra utterance",
                "forwarded_item_path": [4],
            }
        )
    elif change == "order":
        candidate["text_components"].reverse()
    elif change == "path":
        first["forwarded_item_path"] = [4]
    elif change == "group_path":
        group["forwarded_item_path"] = [4]
    elif change == "resource_body":
        candidate["text_components"][1]["text"] = "A filename is not an utterance"
    elif change == "quote_reference":
        candidate["quoted_reference"] = {"source_sender_id": "wxid_earlier"}
    elif change == "subtype":
        candidate["appmsg_subtype"] = 57
    elif change == "other_field":
        previous["forwarded_record_title"] = "Original title"
        candidate["forwarded_record_title"] = "Changed title"
    assert not native_text_metadata_extends(previous, candidate)


def test_metadata_extension_does_not_relax_existing_quoted_roles():
    previous, candidate = metadata_extension_fixture()
    old_bundle = {
        "kind": "quoted_forwarded_text",
        "text": "First\n\nInside",
        "text_components": previous["text_components"],
    }
    new_bundle = {**old_bundle, "text_components": candidate["text_components"]}
    old = {
        "appmsg_subtype": 57,
        "text_components": [{"kind": "reply_text", "text": "Reply"}, old_bundle],
    }
    new = {
        "appmsg_subtype": 57,
        "text_components": [{"kind": "reply_text", "text": "Reply"}, new_bundle],
    }
    assert not native_text_metadata_extends(old, new)
