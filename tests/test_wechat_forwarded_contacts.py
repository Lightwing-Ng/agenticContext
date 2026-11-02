"""Published HTML forwarding verifies source, body, and identity without writes.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
import json
import sqlite3

import pytest

from app.core.wechat_contacts import (
    FORWARDED_BINDING_AUTHORITY,
    WechatContactRepository,
    canonical_contact_id,
    contact_registry_path,
    initialize_contact_registry,
)
from tests.test_wechat_contacts import insert_contact, observe
from tests.wechat_forwarded_fixtures import make_forwarded_html_store


@pytest.fixture
def registry(tmp_path):
    path = contact_registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        initialize_contact_registry(connection)
    return tmp_path


@pytest.fixture
def forwarded_html_store(registry):
    return make_forwarded_html_store(registry)


def _forwarded_result(fixture, request=None):
    request = request or fixture["request"]
    return WechatContactRepository(fixture["store"]).resolve_forwarded_components(
        [request]
    )[request["id"]]


def test_published_forwarded_avatar_resolves_missing_registry_identity_without_writes(
    forwarded_html_store,
):
    fixture = forwarded_html_store
    history_before = fixture["history"].read_bytes()
    contacts_path = contact_registry_path(fixture["store"])
    contacts_before = contacts_path.read_bytes()
    original_request = deepcopy(fixture["request"])
    first = _forwarded_result(fixture)
    second = _forwarded_result(fixture)
    component = first["components"][0]
    contact = component["contact"]
    assert first["status"] == "source_proof_verified"
    assert contact == second["components"][0]["contact"]
    assert contact["id"] == canonical_contact_id(
        "wxid_forwarded", authority="native_message_sender"
    )
    assert contact["identity_status"] == "verified"
    assert contact["registry_persisted"] is False
    assert contact["resolution_reason"] == FORWARDED_BINDING_AUTHORITY
    assert contact["evidence"][0]["authority"] == FORWARDED_BINDING_AUTHORITY
    assert (
        contact["source_display_name"]
        == original_request["forwarded_components"][0]["sender_label"]
    )
    assert component["text_completion_parent_context"] == {
        "immediate_parent_known": False
    }
    assert component["flags"] == ["original_nested_parent_unknown"]
    assert fixture["request"] == original_request
    assert fixture["history"].read_bytes() == history_before
    assert contacts_path.read_bytes() == contacts_before


def test_forwarded_native_identity_reuses_registry_profile_and_source_name(
    forwarded_html_store,
):
    fixture = forwarded_html_store
    with sqlite3.connect(contact_registry_path(fixture["store"])) as connection:
        canonical = insert_contact(connection, "wxid_forwarded")
        observe(connection, canonical, "nickname", "Existing canonical nickname")
    result = _forwarded_result(fixture)["components"][0]["contact"]
    assert result["id"] == canonical
    assert result["registry_persisted"] is True
    assert result["display_name"] == "Existing canonical nickname"
    assert result["source_display_name"] == "Original room-specific author"


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "0" * 64),
        ("source_id", "other-html-source"),
        ("account_id", "wxid_other_owner"),
        ("peer_id", "other@chatroom"),
        ("conversation_id", "wechat:other"),
        ("conversation_kind", "direct"),
        ("source_snapshot_id", "0" * 64),
        ("source_record_id", "forged-record"),
        ("timestamp_value", "2020-01-01T00:00:00"),
        ("text_sha256", "0" * 64),
        ("is_system", True),
    ],
)
def test_forwarded_proof_rejects_parent_scope_or_body_grafting(
    forwarded_html_store, field, value
):
    fixture = forwarded_html_store
    request = deepcopy(fixture["request"])
    request[field] = value
    result = _forwarded_result(fixture, request)
    assert result["components"][0]["contact"]["identity_status"] == "unresolved"


@pytest.mark.parametrize(
    "field,value",
    [
        ("sender_native_id", "wxid_forged"),
        ("avatar_path", "Room_files/Portrait/wxid_forged.jpg"),
        ("raw_timestamp", "2020-01-01 00:00:00"),
        ("sender_label", "A matching label cannot authorize a different identity"),
    ],
)
def test_forwarded_caller_tree_cannot_replace_persisted_component(
    forwarded_html_store, field, value
):
    request = deepcopy(forwarded_html_store["request"])
    request["forwarded_components"][0][field] = value
    assert (
        _forwarded_result(forwarded_html_store, request)["components"][0]["contact"][
            "identity_status"
        ]
        == "unresolved"
    )


def _persist_forwarded_tree(fixture, request):
    with sqlite3.connect(fixture["history"]) as connection:
        raw = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (request["id"],)
            ).fetchone()[0]
        )
        raw["forwarded_components"] = request["forwarded_components"]
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(raw), request["id"]),
        )


@pytest.mark.parametrize(
    "native,avatar,kind",
    [
        (
            "wxid_forwarded",
            "https://invalid.example/Portrait/wxid_forwarded.jpg",
            "forwarded_rendered_text",
        ),
        (
            "wxid_forwarded",
            "Other_files/Portrait/wxid_forwarded.jpg",
            "forwarded_rendered_text",
        ),
        (
            "wxid_forwarded",
            "Room_files/Portrait/wxid_other.jpg",
            "forwarded_rendered_text",
        ),
        (
            "person@openim",
            "Room_files/Portrait/person@openim.jpg",
            "forwarded_rendered_text",
        ),
        (
            "wxid_unknwn_1",
            "Room_files/Portrait/wxid_unknwn_1.jpg",
            "forwarded_rendered_text",
        ),
        ("wxid_forwarded", "Room_files/Portrait/wxid_forwarded.jpg", "system_text"),
    ],
)
def test_persisted_native_hint_needs_safe_matching_avatar_and_supported_identity(
    forwarded_html_store, native, avatar, kind
):
    request = deepcopy(forwarded_html_store["request"])
    request["forwarded_components"][0].update(
        sender_native_id=native, avatar_path=avatar, text_kind=kind
    )
    _persist_forwarded_tree(forwarded_html_store, request)
    contact = _forwarded_result(forwarded_html_store, request)["components"][0][
        "contact"
    ]
    assert contact["identity_status"] in {"unresolved", "system"}
    assert contact["resolution_reason"] != FORWARDED_BINDING_AUTHORITY


@pytest.mark.parametrize(
    "change",
    ["owner", "verified", "archive", "member_sha", "catalog_peer", "partition_account"],
)
def test_forwarded_proof_rejects_inconsistent_published_source_metadata(
    forwarded_html_store, change
):
    fixture = forwarded_html_store
    with sqlite3.connect(fixture["history"]) as connection:
        raw = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots LIMIT 1"
            ).fetchone()[0]
        )
        if change == "owner":
            raw["source_account_id"] = "wxid_wrong"
        elif change == "verified":
            raw["owner_verified"] = False
        elif change == "archive":
            raw["source_sha256"] = "0" * 64
        elif change == "member_sha":
            raw["selected_text_members"][0]["sha256"] = "0" * 64
        elif change == "catalog_peer":
            raw["source_conversation_catalog"][0]["index_avatar_native_id"] = (
                "other@chatroom"
            )
        elif change == "partition_account":
            connection.execute("UPDATE partitions SET account_key='account:wrong'")
        connection.execute("UPDATE source_snapshots SET metadata=?", (json.dumps(raw),))
    assert (
        _forwarded_result(fixture)["components"][0]["contact"]["identity_status"]
        == "unresolved"
    )


def test_forwarded_resource_limit_fails_closed_without_affecting_parent_text(
    forwarded_html_store, monkeypatch
):
    from app.core import wechat_contacts

    fixture = forwarded_html_store
    monkeypatch.setattr(wechat_contacts, "MAX_FORWARDED_COMPONENTS", 0)
    assert _forwarded_result(fixture) == {
        "components": [],
        "status": "component_resource_limit",
    }
    assert (
        fixture["request"]["forwarded_components"][0]["text"] == "Original child text"
    )


def test_typed_system_flags_cannot_be_forged_as_numeric_values(forwarded_html_store):
    request = deepcopy(forwarded_html_store["request"])
    request["forwarded_components"][0]["is_system"] = True
    _persist_forwarded_tree(forwarded_html_store, request)
    request["forwarded_components"][0]["is_system"] = 1
    assert (
        _forwarded_result(forwarded_html_store, request)["components"][0]["contact"][
            "identity_status"
        ]
        == "unresolved"
    )


@pytest.mark.parametrize(
    "field,value", [("realm", "other-app"), ("namespace", "openim")]
)
def test_forwarded_wechat_proof_cannot_claim_another_realm(
    forwarded_html_store, field, value
):
    request = deepcopy(forwarded_html_store["request"])
    request["forwarded_components"][0][field] = value
    _persist_forwarded_tree(forwarded_html_store, request)
    assert (
        _forwarded_result(forwarded_html_store, request)["components"][0]["contact"][
            "identity_status"
        ]
        == "unresolved"
    )


def test_same_contact_retains_each_forwarded_components_own_source_proof(
    forwarded_html_store,
):
    fixture = forwarded_html_store
    request = deepcopy(fixture["request"])
    second = deepcopy(request["forwarded_components"][0])
    second.update(component_ordinal=2, ordinal=3, export_msgid="42_component_2")
    second["source_locator"].update(message_ordinal=3, page_message_ordinal=3)
    request["forwarded_components"].append(second)
    _persist_forwarded_tree(fixture, request)

    result = _forwarded_result(fixture, request)
    assert result["status"] == "source_proof_verified"
    contacts = [component["contact"] for component in result["components"]]
    assert contacts[0]["id"] == contacts[1]["id"]
    for component, contact in zip(
        request["forwarded_components"], contacts, strict=True
    ):
        assert (
            contact["evidence"][0]["details"]["source_locator"]
            == component["source_locator"]
        )
    assert contacts[0]["evidence"] != contacts[1]["evidence"]
