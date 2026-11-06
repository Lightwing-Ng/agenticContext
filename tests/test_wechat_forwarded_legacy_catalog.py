"""HTML forwarding preserves literal frame paths and independent native-avatar proof.

Code version: v1.1.0-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from app.core.wechat_contacts import (
    WechatContactRepository,
    canonical_contact_id,
    contact_registry_path,
)
from tests import test_wechat_contacts as contacts


registry = contacts.registry


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(*values):
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


@pytest.fixture
def legacy_async_store(registry):
    fixture = contacts._make_forwarded_html_store(registry)
    source = deepcopy(fixture["source"])
    source["parser_version"] = "v1.1.0"
    catalog = source["source_conversation_catalog"][0]
    catalog.pop("declared_async_counts")
    catalog["counts"] = {"async_array_items": 1, "all_message_nodes": 1002}
    page_member = {
        **source["selected_text_members"][0],
        "member_path": "Export/Room_files/Data/msg-1.js",
        "sha256": hashlib.sha256(b"synthetic asynchronous member").hexdigest(),
        "zip_filename": "Export/Room_files/Data/msg-1.js",
        "size": 2000,
    }
    source["selected_text_members"].append(page_member)
    request = deepcopy(fixture["request"])
    locator = request["source_locator"]
    locator.update(
        member_path=page_member["member_path"],
        member=page_member,
        page=1,
        message_ordinal=1001,
        page_message_ordinal=1,
    )
    child = request["forwarded_components"][0]
    child["source_locator"].update(
        member_path=page_member["member_path"],
        page=1,
        message_ordinal=1002,
        page_message_ordinal=2,
    )
    original_id = request["id"]
    request["source_record_id"] = _digest(
        "html-text-v1", source["source_sha256"], _json(locator)
    )
    request["id"] = _digest(source["source_id"], request["source_record_id"])
    with sqlite3.connect(fixture["history"]) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (original_id,)
            ).fetchone()[0]
        )
        provenance.update(
            locator=locator,
            record_id=request["source_record_id"],
            forwarded_components=request["forwarded_components"],
            avatar_path_metadata="Room_files/Portrait/wxid_owner_a.jpg",
            sender_native_id="wxid_owner_a",
        )
        connection.execute("UPDATE source_snapshots SET metadata=?", (_json(source),))
        connection.execute(
            "UPDATE messages SET id=?,provenance=? WHERE id=?",
            (request["id"], _json(provenance), original_id),
        )
    fixture.update(source=source, request=request)
    return fixture


def _source_update(fixture, mutate):
    with sqlite3.connect(fixture["history"]) as connection:
        source = json.loads(
            connection.execute("SELECT metadata FROM source_snapshots").fetchone()[0]
        )
        mutate(source)
        connection.execute("UPDATE source_snapshots SET metadata=?", (_json(source),))


def _provenance_update(fixture, mutate):
    with sqlite3.connect(fixture["history"]) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?",
                (fixture["request"]["id"],),
            ).fetchone()[0]
        )
        mutate(provenance)
        fixture["request"]["forwarded_components"] = provenance["forwarded_components"]
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (_json(provenance), fixture["request"]["id"]),
        )


def _result(fixture):
    return WechatContactRepository(fixture["store"]).resolve_forwarded_components(
        [fixture["request"]]
    )[fixture["request"]["id"]]


@pytest.mark.parametrize("version", ["v1.0.0", "v1.1.0"])
def test_legacy_async_catalog_recovers_only_receipted_native_frame(
    legacy_async_store, version
):
    fixture = legacy_async_store
    _source_update(fixture, lambda source: source.update(parser_version=version))
    before = (
        fixture["history"].read_bytes(),
        contact_registry_path(fixture["store"]).read_bytes(),
    )
    request_before = deepcopy(fixture["request"])
    result = _result(fixture)
    (child,) = result["components"]
    assert result["status"] == "source_proof_verified"
    assert child["contact"]["identity_status"] == "verified"
    assert child["contact"]["id"] == canonical_contact_id(
        "wxid_forwarded", authority="native_message_sender"
    )
    evidence = child["contact"]["evidence"][0]["details"]
    assert evidence["selected_member"]["member_path"] == (
        "Export/Room_files/Data/msg-1.js"
    )
    assert evidence["parent_message_id"] == fixture["request"]["id"]
    assert fixture["request"] == request_before
    assert before == (
        fixture["history"].read_bytes(),
        contact_registry_path(fixture["store"]).read_bytes(),
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "wrong_owner",
        "other_peer",
        "ambiguous_peer",
        "ambiguous_frame",
        "wrong_js_hash",
        "missing_main_receipt",
        "missing_js_receipt",
        "bad_explicit_data_path",
        "empty_explicit_data_path",
        "modern_missing_path",
        "no_async_evidence",
        "parent_avatar_other_frame",
        "parent_avatar_alias",
        "component_avatar_other_frame",
        "component_avatar_alias",
        "caller_record_index",
    ],
)
def test_legacy_fallback_keeps_ambiguous_or_forged_sources_unresolved(
    legacy_async_store, tamper
):
    fixture = legacy_async_store
    if tamper in {"parent_avatar_other_frame", "parent_avatar_alias"}:
        value = (
            "Other_files/Portrait/wxid_owner_a.jpg"
            if tamper == "parent_avatar_other_frame"
            else "Room_files/Portrait/public-owner-alias.jpg"
        )
        _provenance_update(
            fixture,
            lambda value_object: value_object.update(avatar_path_metadata=value),
        )
    elif tamper in {"component_avatar_other_frame", "component_avatar_alias"}:
        value = (
            "Other_files/Portrait/wxid_forwarded.jpg"
            if tamper == "component_avatar_other_frame"
            else "Room_files/Portrait/public-forwarded-alias.jpg"
        )
        _provenance_update(
            fixture,
            lambda value_object: value_object["forwarded_components"][0].update(
                avatar_path=value
            ),
        )
    elif tamper == "caller_record_index":
        fixture["request"]["source_locator"]["message_ordinal"] += 1
    else:

        def mutate(source):
            catalog = source["source_conversation_catalog"]
            if tamper == "wrong_owner":
                source["source_account_id"] = "wxid_wrong_owner"
            elif tamper == "other_peer":
                catalog[0]["index_avatar_native_id"] = "other@chatroom"
            elif tamper in {"ambiguous_peer", "ambiguous_frame"}:
                copied = deepcopy(catalog[0])
                if tamper == "ambiguous_frame":
                    copied["index_avatar_native_id"] = "other@chatroom"
                catalog.append(copied)
            elif tamper == "wrong_js_hash":
                source["selected_text_members"][1]["sha256"] = "0" * 64
            elif tamper == "missing_main_receipt":
                source["selected_text_members"].pop(0)
            elif tamper == "missing_js_receipt":
                source["selected_text_members"].pop(1)
            elif tamper in {"bad_explicit_data_path", "empty_explicit_data_path"}:
                catalog[0]["declared_async_counts"] = {
                    "data_path": "Other_files/Data"
                    if tamper == "bad_explicit_data_path"
                    else ""
                }
            elif tamper == "modern_missing_path":
                source["parser_version"] = "v1.2.0"
            elif tamper == "no_async_evidence":
                catalog[0]["counts"]["async_array_items"] = 0

        _source_update(fixture, mutate)
    before = (
        fixture["history"].read_bytes(),
        contact_registry_path(fixture["store"]).read_bytes(),
    )
    result = _result(fixture)
    assert result["components"][0]["contact"]["identity_status"] == "unresolved"
    assert before == (
        fixture["history"].read_bytes(),
        contact_registry_path(fixture["store"]).read_bytes(),
    )


def test_exact_other_js_receipt_cannot_graft_a_different_conversation_frame(
    legacy_async_store,
):
    fixture = legacy_async_store
    request = fixture["request"]
    original_id = request["id"]
    with sqlite3.connect(fixture["history"]) as connection:
        source = json.loads(
            connection.execute("SELECT metadata FROM source_snapshots").fetchone()[0]
        )
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (original_id,)
            ).fetchone()[0]
        )
        other = deepcopy(source["selected_text_members"][1])
        other["member_path"] = "Export/Other_files/Data/msg-1.js"
        other["zip_filename"] = other["member_path"]
        source["selected_text_members"][1] = other
        request["source_locator"].update(member_path=other["member_path"], member=other)
        request["forwarded_components"][0]["source_locator"]["member_path"] = other[
            "member_path"
        ]
        request["source_record_id"] = _digest(
            "html-text-v1", source["source_sha256"], _json(request["source_locator"])
        )
        request["id"] = _digest(source["source_id"], request["source_record_id"])
        provenance.update(
            locator=request["source_locator"],
            record_id=request["source_record_id"],
            forwarded_components=request["forwarded_components"],
        )
        connection.execute("UPDATE source_snapshots SET metadata=?", (_json(source),))
        connection.execute(
            "UPDATE messages SET id=?,provenance=? WHERE id=?",
            (request["id"], _json(provenance), original_id),
        )
    before = fixture["history"].read_bytes()
    result = _result(fixture)
    assert result["status"] == "unverified_source_proof"
    assert result["components"][0]["contact"]["identity_status"] == "unresolved"
    assert fixture["history"].read_bytes() == before


@pytest.fixture
def literal_space_store(registry):
    fixture = contacts._make_forwarded_html_store(registry)
    source = deepcopy(fixture["source"])
    source["parser_version"] = "v1.3.0"
    member = source["selected_text_members"][0]
    member["member_path"] = "Export/ Room.html"
    member["zip_filename"] = member["member_path"]
    catalog = source["source_conversation_catalog"][0]
    catalog.update(
        member_path=member["member_path"],
        declared_async_counts={"data_path": "%20Room_files/Data"},
    )
    request = deepcopy(fixture["request"])
    original_id = request["id"]
    request["source_locator"].update(member_path=member["member_path"], member=member)
    child = request["forwarded_components"][0]
    child["source_locator"]["member_path"] = member["member_path"]
    child["avatar_path"] = " Room_files/Portrait//wxid_forwarded.jpg"
    request["source_record_id"] = _digest(
        "html-text-v1", source["source_sha256"], _json(request["source_locator"])
    )
    request["id"] = _digest(source["source_id"], request["source_record_id"])
    with sqlite3.connect(fixture["history"]) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (original_id,)
            ).fetchone()[0]
        )
        provenance.update(
            locator=request["source_locator"],
            record_id=request["source_record_id"],
            forwarded_components=request["forwarded_components"],
            avatar_path_metadata=" Room_files/Portrait/wxid_owner_a.jpg",
            sender_native_id="wxid_owner_a",
        )
        connection.execute("UPDATE source_snapshots SET metadata=?", (_json(source),))
        connection.execute(
            "UPDATE messages SET id=?,provenance=? WHERE id=?",
            (request["id"], _json(provenance), original_id),
        )
    fixture.update(source=source, request=request)
    return fixture


@pytest.mark.parametrize("prefix", [" ", "%20"])
def test_literal_space_avatar_retains_exact_receipted_frame(
    literal_space_store, prefix
):
    fixture = literal_space_store
    _provenance_update(
        fixture,
        lambda value: value["forwarded_components"][0].update(
            avatar_path=prefix + "Room_files/Portrait//wxid_forwarded.jpg"
        ),
    )
    before = fixture["history"].read_bytes()
    result = _result(fixture)
    assert result["status"] == "source_proof_verified"
    assert result["components"][0]["contact"]["identity_status"] == "verified"
    evidence = result["components"][0]["contact"]["evidence"][0]["details"]
    assert evidence["selected_member"]["member_path"] == "Export/ Room.html"
    assert evidence["avatar_path"].startswith(prefix)
    assert fixture["history"].read_bytes() == before


@pytest.mark.parametrize(
    "avatar",
    [
        "Room_files/Portrait/wxid_forwarded.jpg",
        " https://invalid.example/Portrait/wxid_forwarded.jpg",
        " //invalid.example/Portrait/wxid_forwarded.jpg",
        "\t Room_files/Portrait/wxid_forwarded.jpg",
        "\x85 Room_files/Portrait/wxid_forwarded.jpg",
        " Room_files/../Other_files/Portrait/wxid_forwarded.jpg",
        " Room_files/%2e%2e/Other_files/Portrait/wxid_forwarded.jpg",
        " Room_files/%252e%252e/Other_files/Portrait/wxid_forwarded.jpg",
        "%2520Room_files/Portrait/wxid_forwarded.jpg",
    ],
)
def test_literal_space_proof_rejects_remote_control_or_reencoded_paths(
    literal_space_store, avatar
):
    fixture = literal_space_store
    _provenance_update(
        fixture,
        lambda value: value["forwarded_components"][0].update(avatar_path=avatar),
    )
    before = fixture["history"].read_bytes()
    result = _result(fixture)
    assert result["components"][0]["contact"]["identity_status"] == "unresolved"
    assert fixture["history"].read_bytes() == before
