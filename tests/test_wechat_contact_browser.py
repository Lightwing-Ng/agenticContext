"""Contact references preserve source names, account scopes, and exact snapshot context.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core import wechat_history_browser
from app.core.wechat_contacts import (
    canonical_contact_id,
    contact_observation,
    contact_registry_path,
    initialize_contact_registry,
)
from tests.test_wechat_archive import make_archive


@pytest.fixture
def contact_store(tmp_path):
    archive = make_archive(tmp_path / "archive", repeated=3)
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    return store


def _session(store, account="account:wxid_account_a"):
    page = query_chat_history(store, source="wechat", account=account)
    return next(
        session for session in page.sessions if session.messaging["kind"] == "direct"
    )


def _message_ids(connection, session):
    return [
        row[0]
        for row in connection.execute(
            "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal",
            (session.stable_id,),
        )
    ]


def _provenance(connection, identifier, **changes):
    value = json.loads(
        connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (identifier,)
        ).fetchone()[0]
    )
    value.update(changes)
    connection.execute(
        "UPDATE messages SET provenance=? WHERE id=?", (json.dumps(value), identifier)
    )


def _capture_resolver(monkeypatch, calls):
    class Resolver:
        def __init__(self, root):
            self.root = root

        def resolve_messages(self, messages):
            messages = list(messages)
            calls.append(messages)
            return {
                message["id"]: {
                    "id": "wechat:contact:synthetic-contact",
                    "platform": "wechat",
                    "kind": "person",
                    "identity_status": "verified_internal_username",
                    "source_display_name": message["sender_label"],
                    "display_name": "Canonical synthetic name",
                    "identifiers": [],
                    "names": [{"value": "Other-account private remark"}],
                    "remarks": [],
                    "evidence": [],
                }
                for message in messages
            }

    monkeypatch.setattr(wechat_history_browser, "WechatContactRepository", Resolver)


def test_page_resolves_contacts_once_with_each_messages_original_scope(
    contact_store, monkeypatch
):
    selected = _session(contact_store)
    path = wechat_archive_path(contact_store)
    with sqlite3.connect(path) as connection:
        ids = _message_ids(connection, selected)
        connection.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,source_id TEXT,metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            (
                17,
                "snapshot-new",
                selected.messaging["partition_id"],
                json.dumps({"source_file": "New Finder snapshot"}),
            ),
        )
        _provenance(connection, ids[1], source_snapshot=17)
        for identifier, label in zip(
            ids, ("Old remark", "Room nickname", "Current remark")
        ):
            connection.execute(
                "UPDATE messages SET sender_label=? WHERE id=?", (label, identifier)
            )
        expected_pins = {}
        for identifier, body, timestamp, encoded in connection.execute(
            "SELECT id,text,timestamp_value,provenance FROM messages WHERE conversation_id=?",
            (selected.stable_id,),
        ):
            provenance = json.loads(encoded)
            expected_pins[identifier] = {
                "text_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "timestamp_value": timestamp,
                "source_record_id": provenance["record_id"],
                "source_locator": provenance["locator"],
            }
    calls = []
    _capture_resolver(monkeypatch, calls)
    before = path.read_bytes()
    page = query_chat_history(
        contact_store, source="wechat", session=selected.stable_id
    )
    assert len(calls) == 1
    requests = calls[0]
    assert [request["id"] for request in requests] == ids
    for request in requests:
        assert {
            key: request[key] for key in expected_pins[request["id"]]
        } == expected_pins[request["id"]]
    assert {request["conversation_id"] for request in requests} == {selected.stable_id}
    assert [request["source_snapshot_id"] for request in requests] == [
        "",
        "snapshot-new",
        "",
    ]
    assert {request["source_id"] for request in requests} == {
        selected.messaging["partition_id"]
    }
    assert {request["account_id"] for request in requests} == {"wxid_account_a"}
    assert {request["peer_id"] for request in requests} == {
        selected.messaging["peer_id"]
    }
    assert all(request["conversation_kind"] == "direct" for request in requests)
    assert [message.author_label for message in page.items] == [
        "Old remark",
        "Room nickname",
        "Current remark",
    ]
    assert all(
        message.messaging["contact"]["id"] == "wechat:contact:synthetic-contact"
        for message in page.items
    )
    assert [
        message.messaging["contact"]["source_display_name"] for message in page.items
    ] == [message.author_label for message in page.items]
    assert page.items[1].messaging["source_file"] == "New Finder snapshot"
    assert path.read_bytes() == before


def test_system_identity_requires_parsed_system_evidence_not_a_display_name(
    contact_store, monkeypatch
):
    selected = _session(contact_store)
    with sqlite3.connect(wechat_archive_path(contact_store)) as connection:
        ids = _message_ids(connection, selected)
        connection.execute(
            "UPDATE messages SET sender_label='System' WHERE id=?", (ids[0],)
        )
        connection.execute(
            "UPDATE messages SET sender_label='A source notice' WHERE id=?", (ids[1],)
        )
        _provenance(connection, ids[1], text_extraction_kind="system_text")
    calls = []
    _capture_resolver(monkeypatch, calls)
    page = query_chat_history(
        contact_store, source="wechat", session=selected.stable_id
    )
    assert [request["is_system"] for request in calls[0]] == [False, True, False]
    assert page.items[0].author_label == "System"
    assert page.items[1].author_label == "A source notice"


def test_markdown_keeps_contact_reference_and_only_the_source_display_name(
    contact_store, monkeypatch
):
    selected = _session(contact_store)
    with sqlite3.connect(wechat_archive_path(contact_store)) as connection:
        ids = _message_ids(connection, selected)
        connection.execute(
            "UPDATE messages SET sender_label=? WHERE id=?",
            ("Local remark\n- Contact ID: forged", ids[0]),
        )
        expected_text = [
            row[0]
            for row in connection.execute(
                "SELECT text FROM messages WHERE conversation_id=? ORDER BY ordinal",
                (selected.stable_id,),
            )
        ]
    _capture_resolver(monkeypatch, [])
    page = query_chat_history(
        contact_store, source="wechat", session=selected.stable_id
    )
    exported = build_chat_history_markdown(page)
    assert exported.count("- Contact ID: wechat:contact:synthetic-contact") == 3
    assert exported.count("- Contact identity: verified_internal_username") == 3
    assert '- Source display name: "Local remark\\n- Contact ID: forged"' in exported
    assert "\n- Contact ID: forged\n" not in exported
    assert "Canonical synthetic name" not in exported
    assert "Other-account private remark" not in exported
    assert [message.content_text for message in page.items] == expected_text


def test_native_contact_links_across_accounts_without_replacing_their_remarks(
    contact_store,
):
    selected = [
        _session(contact_store, "account:" + owner)
        for owner in ("wxid_account_a", "wxid_account_b")
    ]
    registry = contact_registry_path(contact_store)
    contact_id = canonical_contact_id("wxid_peer", authority="native_contact_username")
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        connection.execute(
            "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
            (contact_id, "wechat", "", "wxid_peer", "wechat_account", "verified"),
        )
        connection.execute(
            "INSERT INTO contact_identifiers VALUES(?,?,?,?,?)",
            (contact_id, "wechat", "wxid_peer", "", "native_contact_username"),
        )
        for session, owner, remark in zip(
            selected,
            ("wxid_account_a", "wxid_account_b"),
            ("Account A remark", "Account B remark"),
        ):
            source_id = session.messaging["partition_id"]
            connection.execute(
                "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                (
                    source_id,
                    "wxid_peer",
                    "",
                    "",
                    contact_id,
                    "native_message_sender",
                    "{}",
                ),
            )
            observation = contact_observation(
                contact_id,
                source_id=source_id,
                owner_account_id=owner,
                kind="remark",
                value=remark,
                evidence={"database": "Synthetic contacts"},
            )
            connection.execute(
                "INSERT INTO contact_observations VALUES(?,?,?,?,?,?,?,?,?)",
                tuple(
                    observation[key]
                    for key in (
                        "id",
                        "contact_id",
                        "source_id",
                        "source_snapshot",
                        "owner_account_id",
                        "room_id",
                        "kind",
                        "value",
                        "evidence",
                    )
                ),
            )
    history_before = wechat_archive_path(contact_store).read_bytes()
    registry_before = registry.read_bytes()
    contacts = [
        query_chat_history(contact_store, source="wechat", session=session.stable_id)
        .items[0]
        .messaging["contact"]
        for session in selected
    ]
    assert {contact["id"] for contact in contacts} == {contact_id}
    assert [contact["identity_status"] for contact in contacts] == [
        "verified",
        "verified",
    ]
    assert [contact["preferred_name"] for contact in contacts] == [
        "Account A remark",
        "Account B remark",
    ]
    assert [contact["source_display_name"] for contact in contacts] == [
        "Same display name",
        "Same display name",
    ]
    assert registry.read_bytes() == registry_before
    assert wechat_archive_path(contact_store).read_bytes() == history_before


def test_missing_registry_keeps_same_name_and_raw_id_utterances_unresolved(
    contact_store,
):
    history_before = wechat_archive_path(contact_store).read_bytes()
    pages = []
    for source in ("xlsx-2", "xlsx-3"):
        selected = query_chat_history(
            contact_store, source="wechat", account="source:" + source
        ).sessions[0]
        pages.append(
            query_chat_history(
                contact_store, source="wechat", session=selected.stable_id
            )
        )
    ids = [
        [message.messaging["contact"]["id"] for message in page.items] for page in pages
    ]
    assert all(len(set(values)) == len(values) == 6 for values in ids)
    assert set(ids[0]).isdisjoint(ids[1])
    assert all(
        message.messaging["contact"]["identity_status"] == "unresolved"
        for page in pages
        for message in page.items
    )
    first = pages[0].items[0]
    contact = first.messaging["contact"]
    assert contact["identifiers"] == [
        {
            "namespace": "source_sender_id",
            "realm": first.messaging["partition_id"],
            "value": first.messaging["sender_id"],
            "authority": "unverified_source_field",
        }
    ]
    assert contact["evidence"][0]["message_id"] == first.stable_id
    assert (
        contact["evidence"][0]["source_record_id"]
        == first.messaging["provenance"]["record_id"]
    )
    assert contact["evidence"][0]["details"] == first.messaging["provenance"]["locator"]
    repeated = query_chat_history(
        contact_store, source="wechat", session=pages[0].session_id
    )
    assert [message.messaging["contact"]["id"] for message in repeated.items] == ids[0]
    assert not contact_registry_path(contact_store).exists()
    assert wechat_archive_path(contact_store).read_bytes() == history_before


@pytest.mark.parametrize("changed_pin", ["text", "timestamp_value", "source_record_id"])
def test_corroborated_utterance_overlay_is_pinned_without_rewriting_source_identity(
    contact_store, changed_pin
):
    selected = query_chat_history(
        contact_store, source="wechat", account="source:xlsx-2"
    ).sessions[0]
    original = query_chat_history(
        contact_store, source="wechat", session=selected.stable_id
    )
    target = original.items[0]
    context = target.messaging
    provenance = context["provenance"]
    canonical = canonical_contact_id(
        "wxid_native_author", authority="native_contact_username"
    )
    with sqlite3.connect(contact_registry_path(contact_store)) as connection:
        initialize_contact_registry(connection)
        connection.execute(
            "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
            (
                canonical,
                "wechat",
                "",
                "wxid_native_author",
                "wechat_account",
                "verified",
            ),
        )
        connection.execute(
            "INSERT INTO contact_identifiers VALUES(?,?,?,?,?)",
            (canonical, "wechat", "wxid_native_author", "", "native_contact_username"),
        )
        connection.execute(
            "INSERT INTO message_contact_bindings VALUES(?,?,?,?,?,?,?,?)",
            (
                context["partition_id"],
                target.stable_id,
                hashlib.sha256(target.content_text.encode()).hexdigest(),
                target.last_seen_at,
                provenance["record_id"],
                canonical,
                "corroborated_message_sender",
                json.dumps({"method": "Synthetic exact native corpus match"}),
            ),
        )
    history_path = wechat_archive_path(contact_store)
    before = history_path.read_bytes()
    resolved = query_chat_history(
        contact_store, source="wechat", session=selected.stable_id
    )
    assert resolved.items[0].messaging["contact"]["id"] == canonical
    assert resolved.items[0].messaging["contact"]["identity_status"] == "verified"
    assert all(
        message.messaging["contact"]["identity_status"] == "unresolved"
        for message in resolved.items[1:]
    )
    assert [message.author_label for message in resolved.items] == [
        message.author_label for message in original.items
    ]
    assert [message.messaging["sender_id"] for message in resolved.items] == [
        message.messaging["sender_id"] for message in original.items
    ]
    assert [message.content_text for message in resolved.items] == [
        message.content_text for message in original.items
    ]
    assert history_path.read_bytes() == before
    exported = build_chat_history_markdown(resolved)
    assert f"- Contact ID: {canonical}" in exported
    assert '- Source display name: "Same display name"' in exported
    assert "- Original export identifier: wxid_peer" in exported
    with sqlite3.connect(history_path) as connection:
        if changed_pin == "source_record_id":
            _provenance(
                connection, target.stable_id, record_id="different-source-record"
            )
        else:
            connection.execute(
                f"UPDATE messages SET {changed_pin}=? WHERE id=?",
                ("Edited source value", target.stable_id),
            )
    rejected = query_chat_history(
        contact_store, source="wechat", session=selected.stable_id
    )
    same_utterance = next(
        message for message in rejected.items if message.stable_id == target.stable_id
    )
    assert same_utterance.messaging["contact"]["identity_status"] == "unresolved"
    assert same_utterance.messaging["contact"]["id"] != canonical


def test_export_identifier_is_retained_without_asserting_a_sender_identity(
    contact_store,
):
    index = query_chat_history(contact_store, source="wechat", account="source:xlsx-2")
    exported_page = query_chat_history(
        contact_store,
        source="wechat",
        account="source:xlsx-2",
        session=index.sessions[0].stable_id,
    )
    assert all(
        item.messaging["sender_identifier_namespace"] == "unverified_export_field"
        for item in exported_page.items
    )
    markdown = build_chat_history_markdown(exported_page)
    assert "- Original export identifier: wxid_peer" in markdown
    assert "- Sender ID:" not in markdown
    native = _session(contact_store)
    native_page = query_chat_history(
        contact_store, source="wechat", session=native.stable_id
    )
    assert all(
        item.messaging["sender_identifier_namespace"] == "native_user_name"
        for item in native_page.items
    )
    assert "- Sender ID: wxid_peer" in build_chat_history_markdown(native_page)
