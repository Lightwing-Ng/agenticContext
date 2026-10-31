"""Canonical contact identity and scoped name evidence regression checks.

Code version: v1.1.2-codex.0
"""

import json
import sqlite3

import pytest

from app.core.wechat_contacts import (
    MESSAGE_BINDING_AUTHORITY,
    MAX_OBSERVATIONS,
    WechatContactRepository,
    canonical_contact_id,
    classify_wechat_identifier,
    contact_observation,
    contact_registry_path,
    initialize_contact_registry,
    provisional_contact_id,
    source_contact_id,
)


@pytest.fixture
def registry(tmp_path):
    path = contact_registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        initialize_contact_registry(connection)
    return tmp_path


def message(identifier="message-a", **updates):
    return {
        "id": identifier,
        "source_id": "source-a",
        "conversation_id": "conversation-a",
        "sender_id": "wxid_peer",
        "sender_label": "Original label",
        "source_snapshot_id": "snapshot-a",
        "account_id": "wxid_owner_a",
        "peer_id": "room-a@chatroom",
        "conversation_kind": "group",
        "is_system": False,
        **updates,
    }


def insert_contact(
    connection, username="wxid_peer", *, realm="", contact_id=None, status="verified"
):
    namespace = "openim" if username.endswith("@openim") else "wechat"
    identifier = contact_id or canonical_contact_id(
        username, authority="native_contact_username", namespace=namespace, realm=realm
    )
    connection.execute(
        "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
        (
            identifier,
            namespace,
            realm,
            username,
            classify_wechat_identifier(username)["kind"],
            status,
        ),
    )
    connection.execute(
        "INSERT INTO contact_identifiers VALUES(?,?,?,?,?)",
        (identifier, "native_username", username, realm, "native_contact_username"),
    )
    return identifier


def bind(
    connection,
    identifier,
    *,
    source="source-a",
    sender="wxid_peer",
    snapshot="",
    room="",
    authority="native_message_sender",
):
    connection.execute(
        "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
        (
            source,
            sender,
            snapshot,
            room,
            identifier,
            authority,
            '{"table":"Synthetic"}',
        ),
    )


def observe(
    connection,
    identifier,
    kind,
    value,
    *,
    source="source-a",
    snapshot="snapshot-a",
    owner="wxid_owner_a",
    room="",
    forced_id=None,
):
    row = contact_observation(
        identifier,
        source_id=source,
        source_snapshot=snapshot,
        owner_account_id=owner,
        room_id=room,
        kind=kind,
        value=value,
        evidence={"table": "Synthetic"},
    )
    if forced_id:
        row["id"] = forced_id
    connection.execute(
        "INSERT INTO contact_observations VALUES(?,?,?,?,?,?,?,?,?)",
        tuple(
            row[key]
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


def message_proof(connection, identifier, row, **updates):
    proof = {
        "source_id": row["source_id"],
        "message_id": row["id"],
        "text_sha256": row["text_sha256"],
        "timestamp_value": row["timestamp_value"],
        "source_record_id": row["source_record_id"],
        "contact_id": identifier,
        "authority": MESSAGE_BINDING_AUTHORITY,
        "evidence": '{"method":"verified_native_message_evidence"}',
        **updates,
    }
    connection.execute(
        "INSERT INTO message_contact_bindings VALUES(?,?,?,?,?,?,?,?)",
        tuple(
            proof[key]
            for key in (
                "source_id",
                "message_id",
                "text_sha256",
                "timestamp_value",
                "source_record_id",
                "contact_id",
                "authority",
                "evidence",
            )
        ),
    )


def unidentified_message(identifier="message-a", **updates):
    return message(
        identifier,
        **{
            "sender_id": "",
            "text_sha256": "a" * 64,
            "timestamp_value": "2026-10-30T10:12:00+08:00",
            "source_record_id": "source-record-a",
            **updates,
        },
    )


def test_native_identity_is_exact_and_independent_of_aliases_and_owners():
    a = canonical_contact_id("wxid_peer", authority="native_contact_username")
    assert a == canonical_contact_id("wxid_peer", authority="native_account_owner")
    assert a != canonical_contact_id("wxid_Peer", authority="native_contact_username")
    assert a != canonical_contact_id("wxid_other", authority="native_contact_username")
    with pytest.raises(ValueError, match="native identity evidence"):
        canonical_contact_id("SameNickname", authority="remark")
    with pytest.raises(ValueError, match="whitespace"):
        canonical_contact_id("Same Nickname", authority="native_contact_username")


@pytest.mark.parametrize(
    "value,category,kind",
    [
        ("wxid_peer", "wxid_username", "wechat_account"),
        ("wxid_unknwn_12", "synthetic_placeholder", "unresolved"),
        ("legacy.name", "legacy_username_candidate", "wechat_identity"),
        ("1" * 32, "opaque_hash", "unresolved"),
        ("123456", "numeric_or_phone_value", "unresolved"),
        ("123@chatroom", "room_username", "group"),
        ("123@im.chatroom", "room_username", "group"),
        ("123@openim", "openim_username", "openim_contact"),
        ("gh_service", "official_username", "official_account"),
        ("filehelper", "service_username", "service"),
        ("A display name", "unclassified", "unresolved"),
    ],
)
def test_identifier_syntax_does_not_claim_identity(value, category, kind):
    assert classify_wechat_identifier(value) == {
        "category": category,
        "kind": kind,
        "requires_realm": category == "openim_username",
    }


def test_openim_realms_are_mandatory_and_remain_separate():
    a = canonical_contact_id(
        "peer@openim",
        authority="native_contact_username",
        namespace="openim",
        realm="app-a",
    )
    b = canonical_contact_id(
        "peer@openim",
        authority="native_contact_username",
        namespace="openim",
        realm="app-b",
    )
    assert a != b
    with pytest.raises(ValueError, match="application realm"):
        canonical_contact_id("peer@openim", authority="native_contact_username")
    with pytest.raises(ValueError):
        canonical_contact_id(
            "peer@openim",
            authority="native_contact_username",
            namespace="openim",
            realm=" ",
        )
    with pytest.raises(ValueError):
        canonical_contact_id(
            "wxid_peer", authority="native_contact_username", realm="app-a"
        )


@pytest.mark.parametrize(
    "authority",
    ["native_message_sender", "native_peer_username", "corroborated_export_username"],
)
def test_exporter_synthetic_username_is_not_canonical_from_message_syntax(authority):
    with pytest.raises(ValueError, match="Exporter placeholders"):
        canonical_contact_id("wxid_unknwn_12", authority=authority)


def test_canonical_contact_has_different_owner_remarks_and_original_labels(registry):
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        bind(connection, identifier)
        bind(connection, identifier, source="source-b")
        observe(connection, identifier, "nickname", "Global nickname")
        observe(connection, identifier, "remark", "Owner A remark")
        observe(
            connection,
            identifier,
            "remark",
            "Owner B remark",
            source="source-b",
            owner="wxid_owner_b",
            snapshot="snapshot-b",
        )
        observe(
            connection,
            identifier,
            "room_display_name",
            "Room nickname",
            room="room-a@chatroom",
        )
    repository = WechatContactRepository(registry)
    result = repository.resolve_messages(
        [
            message(),
            message(
                "message-b",
                source_id="source-b",
                account_id="wxid_owner_b",
                source_snapshot_id="snapshot-b",
                sender_label="Other original label",
            ),
        ]
    )
    a, b = result.values()
    assert a["id"] == b["id"] == identifier
    assert a["display_name"] == b["display_name"] == "Global nickname"
    assert a["preferred_name"] == "Owner A remark"
    assert b["preferred_name"] == "Owner B remark"
    assert a["source_display_name"] == "Original label"
    assert b["source_display_name"] == "Other original label"
    assert a["names"] is b["names"]
    assert {item["value"] for item in a["remarks"]} == {
        "Owner A remark",
        "Owner B remark",
    }


def test_same_nickname_or_alias_never_joins_different_native_users(registry):
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        a = insert_contact(connection)
        b = insert_contact(connection, "wxid_other")
        bind(connection, a)
        bind(connection, b, sender="wxid_other")
        for identifier in (a, b):
            observe(connection, identifier, "nickname", "Same nickname")
            observe(connection, identifier, "alias", "same_alias")
    result = WechatContactRepository(registry).resolve_messages(
        [message(), message("message-b", sender_id="wxid_other")]
    )
    assert result["message-a"]["id"] != result["message-b"]["id"]


def test_unidentified_utterances_never_collapse_by_name_or_raw_syntax(tmp_path):
    repository = WechatContactRepository(tmp_path)
    messages = [
        message(),
        message("message-b"),
        message("message-c", source_id="source-b"),
    ]
    result = repository.resolve_messages(messages)
    assert len({contact["id"] for contact in result.values()}) == 3
    assert {contact["identity_status"] for contact in result.values()} == {"unresolved"}
    assert result == repository.resolve_messages(messages)
    assert not contact_registry_path(tmp_path).exists()
    assert provisional_contact_id("a", "room-a", "message") != provisional_contact_id(
        "a", "room-b", "message"
    )


def test_system_requires_typed_provenance_not_the_label(registry):
    result = WechatContactRepository(registry).resolve_messages(
        [message(sender_label="System"), message("event", is_system=True, sender_id="")]
    )
    assert result["message-a"]["identity_status"] == "unresolved"
    assert result["event"]["identity_status"] == "system"
    assert result["event"]["kind"] == "system"


def test_room_and_snapshot_scopes_prevent_binding_leaks(registry):
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        bind(connection, identifier, snapshot="snapshot-a", room="room-a@chatroom")
    result = WechatContactRepository(registry).resolve_messages(
        [
            message(),
            message("other-room", peer_id="room-b@chatroom"),
            message("other-snapshot", source_snapshot_id="snapshot-b"),
            message("unknown-snapshot", source_snapshot_id=""),
        ]
    )
    assert result["message-a"]["identity_status"] == "verified"
    assert all(
        result[key]["identity_status"] == "unresolved"
        for key in ("other-room", "other-snapshot", "unknown-snapshot")
    )


def test_conflicting_bindings_remain_unresolved(registry):
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        a = insert_contact(connection)
        b = insert_contact(connection, "wxid_other")
        bind(connection, a)
        bind(connection, b, room="room-a@chatroom")
    result = WechatContactRepository(registry).resolve_messages([message()])[
        "message-a"
    ]
    assert result["identity_status"] == "unresolved"
    assert result["resolution_reason"] == "conflicting_bindings"


def test_source_scoped_ids_cannot_cross_source_boundaries(registry):
    identifier = source_contact_id(
        "source-a",
        "12",
        conversation_id="room-a@chatroom",
        source_snapshot="snapshot-a",
    )
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        insert_contact(connection, "12", contact_id=identifier, status="source_scoped")
        for source in ("source-a", "source-b"):
            bind(
                connection,
                identifier,
                source=source,
                sender="12",
                room="room-a@chatroom",
                snapshot="snapshot-a",
                authority="source_scoped_identifier",
            )
    result = WechatContactRepository(registry).resolve_messages(
        [
            message(sender_id="12"),
            message("other-source", sender_id="12", source_id="source-b"),
        ]
    )
    assert result["message-a"]["identity_status"] == "source_scoped"
    assert result["other-source"]["identity_status"] == "unresolved"


def test_global_name_and_context_remark_survive_truncated_history(registry):
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        bind(connection, identifier)
        for index in range(MAX_OBSERVATIONS):
            observe(
                connection,
                identifier,
                "nickname",
                "Stale nickname",
                source="other-owner",
                owner="other-owner",
                room=f"other-{index}@chatroom",
                forced_id=f"a-{index:04d}",
            )
        for index in range(MAX_OBSERVATIONS + 10):
            observe(
                connection,
                identifier,
                "nickname",
                "Current nickname",
                source="native-owner",
                room=f"native-{index}@chatroom",
                forced_id=f"y-{index:04d}",
            )
        observe(
            connection,
            identifier,
            "remark",
            "Current owner remark",
            forced_id="z-current-owner",
        )
        observe(
            connection,
            identifier,
            "remark",
            "Wrong owner remark",
            owner="different-owner",
            forced_id="z-wrong-owner",
        )
    contact = WechatContactRepository(registry).resolve_messages([message()])[
        "message-a"
    ]
    assert contact["display_name"] == "Current nickname"
    assert contact["preferred_name"] == "Current owner remark"
    assert len(contact["names"]) + len(contact["remarks"]) == MAX_OBSERVATIONS
    assert contact["observations_truncated"] is True
    assert contact["observation_count"] == MAX_OBSERVATIONS * 2 + 12


def test_bad_canonical_record_only_invalidates_its_own_message(registry):
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        good = insert_contact(connection)
        bad = insert_contact(connection, "bad@openim", realm="app-a")
        bind(connection, good)
        bind(connection, bad, sender="bad@openim")
        connection.execute("UPDATE contacts SET realm='' WHERE id=?", (bad,))
    result = WechatContactRepository(registry).resolve_messages(
        [message(), message("bad", sender_id="bad@openim")]
    )
    assert result["message-a"]["identity_status"] == "verified"
    assert result["bad"]["identity_status"] == "unresolved"


def test_resolver_does_not_modify_registry_or_follow_symlinks(registry, tmp_path):
    path = contact_registry_path(registry)
    with sqlite3.connect(path) as connection:
        identifier = insert_contact(connection)
        bind(connection, identifier)
    before = path.read_bytes()
    assert (
        WechatContactRepository(registry).resolve_messages([message()])["message-a"][
            "identity_status"
        ]
        == "verified"
    )
    assert path.read_bytes() == before
    other = tmp_path / "other-store"
    link = contact_registry_path(other)
    link.parent.mkdir(parents=True)
    link.symlink_to(path)
    assert (
        WechatContactRepository(other).resolve_messages([message()])["message-a"][
            "identity_status"
        ]
        == "unresolved"
    )
    assert path.read_bytes() == before


def test_malformed_sidecar_and_evidence_fail_safely(registry):
    path = contact_registry_path(registry)
    with sqlite3.connect(path) as connection:
        identifier = insert_contact(connection)
        bind(connection, identifier)
        connection.execute("UPDATE sender_bindings SET evidence='not JSON'")
    contact = WechatContactRepository(registry).resolve_messages([message()])[
        "message-a"
    ]
    assert contact["identity_status"] == "verified"
    assert contact["evidence"][0]["details"] == {}
    path.write_bytes(b"Not a SQLite database")
    assert (
        WechatContactRepository(registry).resolve_messages([message()])["message-a"][
            "identity_status"
        ]
        == "unresolved"
    )


def test_name_observations_keep_versions_and_exact_scope():
    a = contact_observation(
        "contact", source_id="a", kind="remark", value="First", evidence={"record": 1}
    )
    b = contact_observation(
        "contact", source_id="b", kind="remark", value="First", evidence={"record": 1}
    )
    c = contact_observation(
        "contact", source_id="a", kind="remark", value="Second", evidence={"record": 1}
    )
    assert len({a["id"], b["id"], c["id"]}) == 3
    assert json.loads(a["evidence"]) == {"record": 1}
    with pytest.raises(ValueError):
        contact_observation("contact", source_id="a", kind="avatar", value="image")


def test_unverified_sender_field_and_source_locator_are_preserved(registry):
    row = message(
        sender_id="legacy_alias",
        source_record_id="row-2",
        source_locator={"sheet": "History", "row": 2},
    )
    contact = WechatContactRepository(registry).resolve_messages([row])[row["id"]]
    assert contact["identity_status"] == "unresolved"
    assert contact["identifiers"] == [
        {
            "namespace": "source_sender_id",
            "realm": "source-a",
            "value": "legacy_alias",
            "authority": "unverified_source_field",
        }
    ]
    assert contact["evidence"][0]["source_snapshot"] == "snapshot-a"
    assert contact["evidence"][0]["conversation_id"] == "conversation-a"
    assert contact["evidence"][0]["source_record_id"] == "row-2"
    assert contact["evidence"][0]["details"] == row["source_locator"]


def test_complete_name_history_remains_queryable_after_page_caps(registry):
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        for index in range(MAX_OBSERVATIONS + 5):
            observe(
                connection,
                identifier,
                "remark",
                f"Version {index}",
                snapshot=f"snapshot-{index}",
            )
        observe(
            connection,
            identifier,
            "remark",
            "Other owner",
            source="source-b",
            owner="wxid_owner_b",
        )
    repository = WechatContactRepository(registry)
    first = repository.query_observations(identifier)
    second = repository.query_observations(identifier, page=2)
    assert first["total_count"] == MAX_OBSERVATIONS + 6
    assert len(first["items"]) == MAX_OBSERVATIONS
    assert len(second["items"]) == 6
    assert {item["value"] for item in first["items"] + second["items"]} == {
        f"Version {index}" for index in range(MAX_OBSERVATIONS + 5)
    } | {"Other owner"}
    scoped = repository.query_observations(
        identifier,
        source_id="source-a",
        source_snapshot="snapshot-2",
        owner_account_id="wxid_owner_a",
        room_id="",
    )
    assert scoped["total_count"] == 1
    assert scoped["items"][0]["value"] == "Version 2"


def test_available_registry_does_not_construct_unused_fallbacks(registry, monkeypatch):
    import app.core.wechat_contacts as contacts_module

    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        bind(connection, identifier)

    def unused(*args):
        pytest.fail("A verified page must not allocate provisional fallback objects")

    monkeypatch.setattr(contacts_module, "_fallback", unused)
    row = message()
    original = dict(row)
    result = WechatContactRepository(registry).resolve_messages([row])
    assert result[row["id"]]["identity_status"] == "verified"
    assert row == original


def test_observation_query_validates_pagination_without_creating_a_registry(tmp_path):
    repository = WechatContactRepository(tmp_path)
    assert repository.query_observations("contact")["total_count"] == 0
    for options in ({"page": 0}, {"page_size": 257}, {"page": True}, {"source_id": 2}):
        with pytest.raises(ValueError):
            repository.query_observations("contact", **options)
    assert not contact_registry_path(tmp_path).exists()


def test_exact_message_proof_resolves_an_idless_utterance(registry):
    row = unidentified_message()
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        message_proof(connection, identifier, row)
    contact = WechatContactRepository(registry).resolve_messages([row])[row["id"]]
    assert contact["id"] == identifier
    assert contact["identity_status"] == "verified"
    assert contact["source_display_name"] == row["sender_label"]
    assert contact["resolution_reason"] == MESSAGE_BINDING_AUTHORITY
    proof = contact["evidence"][0]
    assert proof["message_id"] == row["id"]
    assert proof["verification"] == {
        key: row[key] for key in ("text_sha256", "timestamp_value", "source_record_id")
    }
    assert row["sender_id"] == ""


@pytest.mark.parametrize(
    "changed",
    [
        {"text_sha256": "b" * 64},
        {"timestamp_value": "2026-10-30T10:13:00+08:00"},
        {"source_record_id": "different-record"},
        {"source_record_id": ""},
    ],
)
def test_changed_message_pins_do_not_reuse_sender_proof(registry, changed):
    row = unidentified_message()
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        message_proof(connection, identifier, row)
    request = {**row, **changed}
    contact = WechatContactRepository(registry).resolve_messages([request])[row["id"]]
    assert contact["identity_status"] == "unresolved"


def test_message_proof_cannot_cross_source_accounts(registry):
    row = unidentified_message()
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        message_proof(connection, identifier, row, source_id="source-b")
    repository = WechatContactRepository(registry)
    assert (
        repository.resolve_messages([row])[row["id"]]["identity_status"] == "unresolved"
    )
    other = {**row, "source_id": "source-b"}
    assert repository.resolve_messages([other])[row["id"]]["id"] == identifier


def test_conflicting_message_proofs_remain_unresolved(registry):
    row = unidentified_message()
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        a = insert_contact(connection)
        b = insert_contact(connection, "wxid_other")
        message_proof(connection, a, row)
        message_proof(connection, b, row)
    contact = WechatContactRepository(registry).resolve_messages([row])[row["id"]]
    assert contact["identity_status"] == "unresolved"
    assert contact["resolution_reason"] == "conflicting_bindings"


@pytest.mark.parametrize("same_contact", [True, False])
def test_message_and_native_sender_bindings_must_agree(registry, same_contact):
    row = unidentified_message(sender_id="wxid_peer")
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        a = insert_contact(connection)
        b = a if same_contact else insert_contact(connection, "wxid_other")
        bind(connection, a)
        message_proof(connection, b, row)
    contact = WechatContactRepository(registry).resolve_messages([row])[row["id"]]
    if same_contact:
        assert contact["id"] == a
        assert contact["resolution_reason"] == MESSAGE_BINDING_AUTHORITY
        assert {value["authority"] for value in contact["evidence"]} == {
            "native_message_sender",
            MESSAGE_BINDING_AUTHORITY,
        }
    else:
        assert contact["identity_status"] == "unresolved"
        assert contact["resolution_reason"] == "conflicting_bindings"


def test_optional_message_table_is_backward_compatible(registry):
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        bind(connection, identifier)
        connection.execute("DROP TABLE message_contact_bindings")
        connection.execute(
            "UPDATE contact_registry_info SET value='1' WHERE key='schema_version'"
        )
    repository = WechatContactRepository(registry)
    assert repository.resolve_messages([message()])["message-a"]["id"] == identifier
    row = unidentified_message()
    assert (
        repository.resolve_messages([row])[row["id"]]["identity_status"] == "unresolved"
    )


def test_message_proof_evidence_is_not_reused_for_other_messages(registry):
    a = unidentified_message()
    b = unidentified_message(
        "message-b", text_sha256="b" * 64, source_record_id="source-record-b"
    )
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        message_proof(connection, identifier, a)
        message_proof(connection, identifier, b)
    contacts = WechatContactRepository(registry).resolve_messages([a, b])
    for row in (a, b):
        assert contacts[row["id"]]["evidence"][0]["message_id"] == row["id"]
        assert (
            contacts[row["id"]]["evidence"][0]["verification"]["text_sha256"]
            == row["text_sha256"]
        )


def test_message_authority_cannot_be_promoted_to_sender_wide_binding(registry):
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        bind(connection, identifier, authority=MESSAGE_BINDING_AUTHORITY)
    assert (
        WechatContactRepository(registry).resolve_messages([message()])["message-a"][
            "identity_status"
        ]
        == "unresolved"
    )


def test_verified_native_identity_with_unclassified_syntax_is_not_unresolved(registry):
    username = "a" * 32
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection, username)
        connection.execute(
            "UPDATE contacts SET kind='wechat_identity' WHERE id=?", (identifier,)
        )
        bind(connection, identifier, sender=username)
    contact = WechatContactRepository(registry).resolve_messages(
        [message(sender_id=username)]
    )["message-a"]
    assert contact["identity_status"] == "verified"
    assert contact["kind"] == "wechat_identity"
    assert contact["id"] == identifier


def test_historical_source_labels_do_not_replace_each_utterances_original_name(
    registry,
):
    with sqlite3.connect(contact_registry_path(registry)) as connection:
        identifier = insert_contact(connection)
        bind(connection, identifier)
        for label in ("Z original label", "A original label"):
            observe(
                connection,
                identifier,
                "source_sender_label",
                label,
                snapshot="",
                owner="",
            )
    requests = [
        message(
            "first",
            sender_label="Z original label",
            source_snapshot_id="",
            account_id="",
            peer_id="",
            conversation_kind="unclassified",
            conversation_id="export-conversation-one",
        ),
        message(
            "second",
            sender_label="A original label",
            source_snapshot_id="",
            account_id="",
            peer_id="",
            conversation_kind="unclassified",
            conversation_id="export-conversation-two",
        ),
    ]
    resolved = WechatContactRepository(registry).resolve_messages(requests)
    for request in requests:
        contact = resolved[request["id"]]
        assert contact["id"] == identifier
        assert contact["preferred_name"] == request["sender_label"]
        assert contact["source_display_name"] == request["sender_label"]
        assert {item["value"] for item in contact["names"]} == {
            "Z original label",
            "A original label",
        }
