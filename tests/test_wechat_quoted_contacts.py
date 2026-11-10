"""Quoted speakers reuse proven native identities without changing source labels.

Code version: v1.1.1-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.wechat_contacts import (
    WechatContactRepository,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_display_names import WechatDisplayNames, display_names_path
from app.core.wechat_history_browser import _native_quoted_reply_presentation
from app.core.wechat_quoted_contacts import (
    _exact_original_quoted_author,
    _quoted_author,
    project_native_quoted_contacts,
)
from tests.test_wechat_contacts import bind, insert_contact, observe
from tests.test_wechat_reconcile import merge, record, source


OWNER = "wxid_quote_owner"
TARGET = "wxid_quote_target"
OTHER = "wxid_quote_namesake"
ROOM = "123456@chatroom"
SOURCE = "ios-" + hashlib.md5(OWNER.encode()).hexdigest()
SNAPSHOT = "synthetic-native-snapshot"
OLD_LABEL = "Previous room name"
VIEWER = "Preferred <viewer> name"


def _item(*, sender=ROOM, chat=TARGET, label=OLD_LABEL, number="a"):
    reference = {
        "original_message_type": 1,
        "source_sender_id": sender,
        "source_chat_id": chat,
        "source_sender_label": label,
        "source_ids": {"svrid": "8591339163006592468"},
    }
    provenance = {
        "message_type": 49,
        "text_extraction_kind": "quoted_reply",
        "appmsg_subtype": 57,
        "source_snapshot": 1,
        "quoted_reference": reference,
        "text_components": [
            {"kind": "reply_text", "text": "Current reply"},
            {"kind": "quoted_text", "text": "  Original body\n", **reference},
        ],
    }
    return SimpleNamespace(
        stable_id=number * 64,
        conversation_id="wechat:synthetic-room",
        last_seen_at="2021-10-11T15:47:27+08:00",
        content_text="Current reply\n\n  Original body\n",
        messaging={
            "account_id": OWNER,
            "account_key": f"account:{OWNER}",
            "partition_id": SOURCE,
            "identity_status": "Verified archive ID",
            "peer_id": ROOM,
            "provenance": provenance,
            **_native_quoted_reply_presentation(provenance),
        },
    )


@pytest.fixture
def quote_store(tmp_path):
    path = contact_registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        identifiers = {}
        initialize_contact_registry(connection)
        for native, canonical in (
            (TARGET, "Canonical target"),
            (OTHER, "Canonical namesake"),
        ):
            identifier = insert_contact(connection, native)
            identifiers[native] = identifier
            bind(
                connection,
                identifier,
                source=SOURCE,
                sender=native,
                snapshot=SNAPSHOT,
                room=ROOM,
            )
            observe(
                connection,
                identifier,
                "nickname",
                canonical,
                source=SOURCE,
                snapshot=SNAPSHOT,
                owner=OWNER,
            )
    display_names_path(tmp_path).write_text(
        json.dumps({"schema_version": 1, "contacts": {identifiers[TARGET]: VIEWER}})
    )
    account = {
        "partition_id": SOURCE,
        "account_id": OWNER,
        "key": f"account:{OWNER}",
        "identity_status": "Verified archive ID",
    }
    snapshot = {
        "source_id": SOURCE,
        "snapshot_id": SNAPSHOT,
        "metadata": {
            "source_id": SOURCE,
            "source_account_id": OWNER,
            "account_hash_verified": True,
            "account_hash": hashlib.md5(OWNER.encode()).hexdigest(),
        },
    }
    return SimpleNamespace(
        store=tmp_path,
        path=path,
        identifiers=identifiers,
        account_rows=[account],
        snapshots={1: snapshot},
    )


def _project(fixture, items, *, originals=()):
    project_native_quoted_contacts(
        items,
        WechatContactRepository(fixture.store),
        WechatDisplayNames(fixture.store),
        fixture.account_rows,
        fixture.snapshots,
        originals=originals,
    )


def test_group_explicit_native_author_keeps_contact_without_rewriting_sources(
    quote_store,
):
    item = _item(sender=TARGET, chat=ROOM)
    original = deepcopy(item)
    registry = quote_store.path.read_bytes()
    preferences = display_names_path(quote_store.store).read_bytes()
    _project(quote_store, [item])
    quote = item.messaging["quoted_reply"]
    assert quote["viewer_display_name"] == VIEWER
    assert quote["contact"]["id"] == quote_store.identifiers[TARGET]
    assert quote["contact"]["viewer_display_name_source"] == "local_user_preference"
    assert quote["speaker"] == OLD_LABEL
    for key in ("reference", "text_components", "text"):
        assert quote[key] == original.messaging["quoted_reply"][key]
    assert item.content_text == original.content_text
    assert item.messaging["provenance"] == original.messaging["provenance"]
    assert quote_store.path.read_bytes() == registry
    assert display_names_path(quote_store.store).read_bytes() == preferences


@pytest.mark.parametrize("chat", ["", ROOM, TARGET])
def test_direct_native_author_role_uses_same_contact(quote_store, chat):
    item = _item(sender=TARGET, chat=chat)
    _project(quote_store, [item])
    assert item.messaging["quoted_reply"]["viewer_display_name"] == VIEWER


def test_identical_labels_never_merge_different_ids(quote_store):
    first = _item(sender=TARGET, chat=ROOM)
    namesake = _item(sender=OTHER, chat=ROOM, number="b")
    _project(quote_store, [first, namesake])
    assert (
        first.messaging["quoted_reply"]["speaker"]
        == namesake.messaging["quoted_reply"]["speaker"]
    )
    assert first.messaging["quoted_reply"]["viewer_display_name"] == VIEWER
    assert (
        namesake.messaging["quoted_reply"]["viewer_display_name"]
        == "Canonical namesake"
    )
    assert (
        namesake.messaging["quoted_reply"]["contact"]["id"]
        == quote_store.identifiers[OTHER]
    )


def test_different_source_labels_for_one_id_share_viewer_name(quote_store):
    first = _item(sender=TARGET, chat=ROOM)
    second = _item(
        sender=TARGET, chat=ROOM, label="Different historical nickname", number="b"
    )
    _project(quote_store, [first, second])
    assert (
        first.messaging["quoted_reply"]["viewer_display_name"]
        == second.messaging["quoted_reply"]["viewer_display_name"]
    )
    assert (
        second.messaging["quoted_reply"]["speaker"] == "Different historical nickname"
    )


@pytest.mark.parametrize(
    "sender,chat",
    [
        ("999999@chatroom", TARGET),
        (ROOM, "PublicAlias"),
        (ROOM, OTHER + " "),
        (ROOM, "wxid_unregistered"),
        (TARGET, OTHER),
        ("PublicAlias", ""),
        ("", ""),
    ],
)
def test_ambiguous_or_unverified_reference_retains_source_label(
    quote_store, sender, chat
):
    item = _item(sender=sender, chat=chat)
    original = deepcopy(item.messaging)
    _project(quote_store, [item])
    assert item.messaging == original


@pytest.mark.parametrize(
    "scope,key,value",
    [
        ("message", "account_id", "wxid_other_owner"),
        ("message", "account_key", "account:wxid_other_owner"),
        ("message", "partition_id", "html-other-export"),
        ("message", "identity_status", "Corroborated export ID"),
        ("account", "account_id", "wxid_other_owner"),
        ("account", "key", "account:wxid_other_owner"),
        ("snapshot", "source_id", "ios-other-source"),
        ("metadata", "source_account_id", "wxid_other_owner"),
        ("metadata", "account_hash", "0" * 32),
        ("metadata", "account_hash_verified", False),
    ],
)
def test_native_account_and_snapshot_scope_must_agree(quote_store, scope, key, value):
    item = _item(sender=TARGET, chat=ROOM)
    targets = {
        "message": item.messaging,
        "account": quote_store.account_rows[0],
        "snapshot": quote_store.snapshots[1],
        "metadata": quote_store.snapshots[1]["metadata"],
    }
    targets[scope][key] = value
    original = deepcopy(item.messaging)
    _project(quote_store, [item])
    assert item.messaging == original


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_type",
        "html",
        "forged_reference",
        "malformed",
        "confirmation",
        "unknown_snapshot",
    ],
)
def test_non_native_or_inconsistent_quote_does_not_gain_identity(quote_store, mutation):
    item = _item(sender=TARGET, chat=ROOM)
    provenance = item.messaging["provenance"]
    if mutation == "wrong_type":
        provenance["message_type"] = 1
    elif mutation == "html":
        del provenance["message_type"]
        provenance["export_msgid_namespace"] = "MesLocalID"
    elif mutation == "forged_reference":
        item.messaging["quoted_reply"]["reference"] = {"source_sender_id": OTHER}
    elif mutation == "malformed":
        provenance["quoted_reference"] = []
    elif mutation == "confirmation":
        provenance["quoted_reference"]["authority"] = "user_confirmation"
    else:
        provenance["source_snapshot"] = 999
    original = deepcopy(item.messaging)
    _project(quote_store, [item])
    assert item.messaging == original


def test_unavailable_body_can_still_have_a_proven_speaker(quote_store):
    item = _item(sender=TARGET, chat=ROOM)
    provenance = item.messaging["provenance"]
    provenance["text_components"] = [{"kind": "reply_text", "text": "Current reply"}]
    provenance["quoted_reference"]["content_unavailable"] = True
    item.messaging.update(_native_quoted_reply_presentation(provenance))
    _project(quote_store, [item])
    assert item.messaging["quoted_reply"]["viewer_display_name"] == VIEWER
    assert (
        item.messaging["quoted_reply"]["text"]
        == "Original quoted content is unavailable."
    )


def test_native_forwarded_child_quote_reuses_verified_receiving_envelope(quote_store):
    parent = _item(sender=TARGET, chat=ROOM)
    child = _item(sender=TARGET, chat="", number="b")
    evidence = child.messaging["provenance"]
    child.messaging["provenance"] = {
        "embedded_forwarded_component": {
            "kind": "forwarded_quote",
            "source_chat_id": ROOM,
            "text_components": evidence["text_components"],
            "quoted_reference": evidence["quoted_reference"],
        }
    }
    parent.messaging["forwarded_record"] = {
        "authority": "native_forwarded_envelope",
        "items": [child],
    }
    _project(quote_store, [parent])
    assert child.messaging["quoted_reply"]["viewer_display_name"] == VIEWER


def test_nested_room_quote_cannot_borrow_reply_author_or_parent_original_proof(quote_store):
    parent = _item(sender=ROOM, chat=OTHER)
    parent.messaging["sender_id"] = OTHER
    parent.messaging["source_observations"] = [{
        "id": parent.stable_id,
        "source_id": SOURCE,
        "source_snapshot_id": SNAPSHOT,
        "sender_id": OTHER,
        "event_kind": "quoted_reply",
        "quoted_original_id": "c" * 64,
        "quoted_server_id": "8591339163006592468",
        "quoted_sender_id": TARGET,
        "quoted_contact_id": quote_store.identifiers[TARGET],
    }]
    child = _item(sender=ROOM, chat=OTHER, number="b")
    child.messaging["sender_id"] = OTHER
    evidence = child.messaging["provenance"]
    child.messaging["provenance"] = {
        "embedded_forwarded_component": {
            "kind": "forwarded_quote",
            "source_chat_id": ROOM,
            "text_components": evidence["text_components"],
            "quoted_reference": evidence["quoted_reference"],
        }
    }
    child.messaging["source_observations"] = deepcopy(parent.messaging["source_observations"])
    parent.messaging["forwarded_record"] = {
        "authority": "native_forwarded_envelope", "items": [child]
    }
    before = deepcopy(child)
    _project(quote_store, [parent])
    assert parent.messaging["quoted_reply"]["viewer_display_name"] == VIEWER
    assert child == before


@pytest.mark.parametrize("room", [ROOM, "123456@im.chatroom"])
@pytest.mark.parametrize("chat_role", ["original", "reply"])
def test_exact_original_author_accepts_only_proven_room_roles_and_legacy_names(room, chat_role):
    original, reply = "legacy_native_original", "wxid_reply_author"
    reference = {
        "source_sender_id": room,
        "source_chat_id": original if chat_role == "original" else reply,
    }
    assert _exact_original_quoted_author(reference, room, original, reply) == original
    assert _quoted_author({"source_sender_id": original}, room) == ""


@pytest.mark.parametrize(
    "sender,chat,original,reply",
    [
        (ROOM, "wxid_third_person", TARGET, OTHER),
        ("999999@chatroom", TARGET, TARGET, OTHER),
        (OTHER, ROOM, TARGET, OTHER),
        (TARGET, OTHER, TARGET, OTHER),
        (ROOM, TARGET, ROOM, OTHER),
        (ROOM, "789", "789", OTHER),
        (ROOM, "wxid_unknwn_123", "wxid_unknwn_123", OTHER),
        (ROOM, OTHER, TARGET, ROOM),
        (ROOM, "", TARGET, OTHER),
    ],
)
def test_exact_original_author_does_not_promote_conflicting_or_unresolved_roles(
    sender, chat, original, reply
):
    assert _exact_original_quoted_author(
        {"source_sender_id": sender, "source_chat_id": chat}, ROOM, original, reply
    ) == ""


def _resource_quote_original(fixture):
    reply = _item(sender=ROOM, chat=OTHER)
    reply.messaging["sender_id"] = OTHER
    locator = {
        "database": "message_4.sqlite",
        "database_relative_path": f"Documents/{SOURCE[4:]}/DB/message_4.sqlite",
        "database_sha256": "a" * 64,
        "table": "Chat_" + hashlib.md5(ROOM.encode(), usedforsecurity=False).hexdigest(),
        "local_id": 117, "row_id": 117,
    }
    proof = reply.messaging["provenance"]
    proof.update(
        server_id="8591339163006592469", unix_timestamp=1700000020, locator=locator,
        text_components=[{"kind": "reply_text", "text": "Current reply"}],
    )
    reference = proof["quoted_reference"]
    reference.update(original_message_type=49, source_timestamp="1700000000")
    reply.content_text = "Current reply"
    reply.messaging.update(_native_quoted_reply_presentation(proof))
    original = deepcopy(reply)
    original.stable_id = "c" * 64
    original.content_text = "[App message]"
    original.messaging["sender_id"] = TARGET
    original.messaging.pop("quoted_reply", None)
    original.messaging["provenance"] = {
        "message_type": 49, "text_extraction_kind": "resource_placeholder",
        "source_snapshot": 1, "server_id": reference["source_ids"]["svrid"],
        "unix_timestamp": 1700000000,
        "locator": {**locator, "local_id": 115, "row_id": 115},
        "resource_reference": {
            "kind": "app", "account_id": OWNER, "peer_id": ROOM,
            "availability": "metadata_only", "unix_timestamp": 1700000000,
            "safe_title": "Original source title",
        },
    }
    fixture.snapshots[1]["metadata"]["input_databases"] = [
        {"path": locator["database_relative_path"], "sha256": "a" * 64}
    ]
    return reply, original


def test_exact_resource_original_resolves_speaker_without_borrowing_reply_identity(quote_store):
    reply, original = _resource_quote_original(quote_store)
    before, original_before = deepcopy(reply), deepcopy(original)
    registry = quote_store.path.read_bytes()
    _project(quote_store, [reply], originals=[original])
    quote = reply.messaging["quoted_reply"]
    assert quote["viewer_display_name"] == VIEWER
    assert quote["contact"]["id"] == quote_store.identifiers[TARGET]
    assert quote["contact"]["id"] != quote_store.identifiers[OTHER]
    for key in ("reference", "text_components", "speaker", "text"):
        assert quote[key] == before.messaging["quoted_reply"][key]
    assert reply.messaging["provenance"] == before.messaging["provenance"]
    assert reply.content_text == before.content_text and original == original_before
    assert quote_store.path.read_bytes() == registry


@pytest.mark.parametrize("defect", ["absent", "snapshot", "database", "duplicate", "unregistered"])
def test_resource_speaker_requires_the_exact_original_and_verified_contact(quote_store, defect):
    reply, original = _resource_quote_original(quote_store)
    originals = [original]
    if defect == "absent":
        originals = []
        reply.messaging["quoted_reply"]["resource_original"] = {
            "message_id": original.stable_id, "sender_id": TARGET
        }
    elif defect == "snapshot":
        original.messaging["provenance"]["source_snapshot"] = 999
    elif defect == "database":
        original.messaging["provenance"]["locator"]["database_sha256"] = "b" * 64
    elif defect == "duplicate":
        originals.append(deepcopy(original))
    else:
        with sqlite3.connect(quote_store.path) as connection:
            connection.execute("DELETE FROM sender_bindings WHERE contact_id=?", (quote_store.identifiers[TARGET],))
    before = deepcopy(reply)
    _project(quote_store, [reply], originals=originals)
    assert reply == before


def test_html_forwarded_tree_cannot_borrow_native_parent_identity(quote_store):
    parent = _item(sender=TARGET, chat=ROOM)
    child = _item(sender=TARGET, chat=ROOM, number="b")
    parent.messaging["forwarded_record"] = {
        "authority": "source_verified_html_export_tree",
        "items": [child],
    }
    original = deepcopy(child.messaging)
    _project(quote_store, [parent])
    assert child.messaging == original


def test_archive_query_reuses_quote_identity_and_retains_raw_native_record(quote_store):
    from app.core.chat_history_browser import query_chat_history
    from app.core.wechat_archive import wechat_archive_path

    native = _item(sender=TARGET, chat=ROOM)
    provenance = native.messaging["provenance"]
    metadata = source(OWNER)
    observation = record(
        metadata,
        peer=ROOM,
        text=native.content_text,
        message_type=49,
        text_extraction_kind="quoted_reply",
        appmsg_subtype=57,
        text_components=provenance["text_components"],
        quoted_reference=provenance["quoted_reference"],
    )
    merge(quote_store.store, [metadata], [observation], SNAPSHOT)
    archive = wechat_archive_path(quote_store.store)
    raw = archive.read_bytes()
    sessions = query_chat_history(quote_store.store, source="wechat").sessions
    page = query_chat_history(
        quote_store.store, source="wechat", session=sessions[0].stable_id
    )
    item = page.items[0]
    quote = item.messaging["quoted_reply"]
    assert quote["viewer_display_name"] == VIEWER
    assert quote["speaker"] == OLD_LABEL
    assert quote["reference"] == observation["quoted_reference"]
    assert quote["contact"]["id"] == quote_store.identifiers[TARGET]
    assert item.content_text == observation["text"]
    assert (
        item.messaging["provenance"]["quoted_reference"]
        == observation["quoted_reference"]
    )
    assert archive.read_bytes() == raw


def test_private_chat_quote_reuses_account_scoped_contact(quote_store):
    item = _item(sender=TARGET, chat="")
    item.messaging["peer_id"] = TARGET
    with sqlite3.connect(quote_store.path) as connection:
        connection.execute(
            "UPDATE sender_bindings SET room_id='' WHERE sender_id=?", (TARGET,)
        )
    _project(quote_store, [item])
    assert item.messaging["quoted_reply"]["viewer_display_name"] == VIEWER


def test_same_identity_bound_only_in_other_account_does_not_match(quote_store):
    item = _item(sender=TARGET, chat=ROOM)
    with sqlite3.connect(quote_store.path) as connection:
        connection.execute(
            "UPDATE sender_bindings SET source_id='other-account-source'"
        )
    original = deepcopy(item.messaging)
    _project(quote_store, [item])
    assert item.messaging == original


def test_conflicting_verified_bindings_do_not_choose_a_name(quote_store):
    item = _item(sender=TARGET, chat=ROOM)
    with sqlite3.connect(quote_store.path) as connection:
        bind(
            connection,
            quote_store.identifiers[OTHER],
            source=SOURCE,
            sender=TARGET,
            snapshot=SNAPSHOT,
        )
    original = deepcopy(item.messaging)
    _project(quote_store, [item])
    assert item.messaging == original


def test_malformed_native_locator_does_not_crash_projection(quote_store):
    item = _item(sender=TARGET, chat=ROOM)
    item.messaging["provenance"]["locator"] = []
    original = deepcopy(item.messaging)
    _project(quote_store, [item])
    assert item.messaging == original


def test_exact_legacy_resource_quote_reuses_the_receiving_owners_contact_remark(quote_store):
    reply, original = _resource_quote_original(quote_store)
    original.messaging["provenance"]["resource_reference"].pop("availability")
    display_names_path(quote_store.store).write_text(json.dumps({"schema_version": 1, "contacts": {}}))
    with sqlite3.connect(quote_store.path) as connection:
        observe(connection, quote_store.identifiers[TARGET], "remark", "Owner-scoped preferred remark", source=SOURCE, snapshot=SNAPSHOT, owner=OWNER, room=ROOM)
    before = deepcopy(reply)
    _project(quote_store, [reply], originals=[original])
    quote = reply.messaging["quoted_reply"]
    assert quote["viewer_display_name"] == "Owner-scoped preferred remark"
    assert quote["contact"]["id"] == quote_store.identifiers[TARGET]
    assert quote["contact"]["display_name"] == "Canonical target"
    assert quote["speaker"] == before.messaging["quoted_reply"]["speaker"]
    assert reply.messaging["provenance"] == before.messaging["provenance"]
