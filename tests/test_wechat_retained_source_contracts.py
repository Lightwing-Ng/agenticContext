"""Retained source bindings and label provenance keep their closed native roles.

Code version: v1.1.0-codex.0
"""

from copy import deepcopy
import json
import sqlite3

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_resources import present_resource
from tests import test_wechat_macos_recovery_account_binding as bindings
from tests import test_wechat_macos_recovery_bank as native


mac_archive = native.mac_archive
bank_archive = native.bank_archive
source_binding = bindings.source_binding


def _version2(fixture):
    packet = deepcopy(fixture.packet)
    binding = packet["account_binding"]
    binding["schema_version"] = 2
    binding["owner_proof"] = native._proof(7, packet["account_id"])
    for anchor in binding["anchors"]:
        if anchor["direction"] == "sent":
            anchor["real_sender_id"] = 7
            anchor["sender_proof"] = deepcopy(binding["owner_proof"])
    for message in packet["messages"]:
        if message["sender_id"] == packet["account_id"]:
            message.update(
                real_sender_id=7, sender_proof=deepcopy(binding["owner_proof"])
            )
    return packet


def test_binding_v2_proves_an_explicit_owner_row_without_relabeling_v1(source_binding):
    packet = _version2(source_binding)
    bank._validate_packet(packet, (packet["account_id"],))
    with sqlite3.connect(source_binding.history) as connection:
        connection.row_factory = sqlite3.Row
        bank._binding(connection, packet)
    row = bank._row(packet, packet["messages"][0])

    assert (
        json.loads(row[11])["macos_recovery"]["binding_mode"]
        == "authenticated_source_account_binding_v2"
    )
    packet["account_binding"]["schema_version"] = 1
    with pytest.raises(ValueError):
        bank._validate_packet(packet, (packet["account_id"],))


@pytest.mark.parametrize("defect", ["sent_row", "sent_proof", "unknown_version"])
def test_binding_v2_cannot_borrow_a_different_sent_owner_witness(
    source_binding, defect
):
    packet = _version2(source_binding)
    binding = packet["account_binding"]
    anchor = next(value for value in binding["anchors"] if value["direction"] == "sent")
    if defect == "sent_row":
        anchor["real_sender_id"] = 2
    elif defect == "sent_proof":
        anchor["sender_proof"] = native._proof(7, "wxid_different_owner")
    else:
        binding["schema_version"] = 3
    with pytest.raises(ValueError):
        bank._validate_packet(packet, (packet["account_id"],))


def _label_packet(fixture, subtype):
    packet = _version2(fixture)
    message = native._message(94, 49, resource="file")
    message.update(
        local_type=(subtype << 32) | 49,
        text="[App message]",
        sender_id=packet["peer_id"],
        sender_label=packet["peer_id"],
        real_sender_id=packet["peer_proof"]["row"]["rowid"],
        sender_proof=deepcopy(packet["peer_proof"]),
    )
    message["resource"] = {
        "kind": "app",
        "label": "[App message]",
        "native_time": message["create_time"],
        "app_subtype": str(subtype),
        "app_metadata_version": "1",
        "safe_title": "Retained title",
        "publisher_name": "Retained publisher",
        "publisher_name_source": "appmsg/sourcedisplayname",
        "app_name": "Retained application",
        "app_name_source": "appinfo/appname",
    }
    packet["messages"] = [message]
    return packet


@pytest.mark.parametrize("subtype", [5, 19])
def test_retained_source_labels_preserve_link_and_forwarding_role_separation(
    source_binding, subtype
):
    packet = _label_packet(source_binding, subtype)
    bank._validate_packet(packet, (packet["account_id"],))
    resource = packet["messages"][0]["resource"]
    presentation = present_resource({**resource, "availability": "metadata_only"}, None)
    assert presentation.get("is_link", False) is (subtype == 5)
    assert presentation["status"] == "metadata_only"
    if subtype == 19:
        assert presentation["status_label"] == "Metadata only"
        assert presentation["label"] == "App message"


@pytest.mark.parametrize(
    "defect",
    [
        "publisher_path",
        "application_path",
        "orphan_source",
        "control",
        "unknown_field",
        "native_subtype",
    ],
)
def test_retained_labels_reject_undeclared_paths_and_opaque_fields(
    source_binding, defect
):
    packet = _label_packet(source_binding, 5)
    resource = packet["messages"][0]["resource"]
    if defect == "publisher_path":
        resource["publisher_name_source"] = "appmsg/url"
    elif defect == "application_path":
        resource["app_name_source"] = "remote/application"
    elif defect == "orphan_source":
        resource.pop("publisher_name")
    elif defect == "control":
        resource["app_name"] = "Unsafe\x00label"
    elif defect == "unknown_field":
        resource["attachment_payload"] = "opaque"
    else:
        resource["app_subtype"] = "19"
    with pytest.raises(ValueError):
        bank._validate_packet(packet, (packet["account_id"],))


def _retained_pat_packet(fixture):
    packet = deepcopy(fixture.fixture.group)
    message = native._message(95, 10000, raw="Retained neutral system source")
    message.update(
        local_type=(62 << 32) | 49, text="Source member patted another member"
    )
    message["native_text"] = {
        "text": message["text"],
        "original_message_type": 49,
        "text_extraction_kind": "system_text",
        "appmsg_subtype": 62,
        "flags": ["system_notice"],
        "system_message_subtype": "pat",
        "system_source_ids": {
            "patinfo/fromusername": "wxid_source_actor",
            "patinfo/chatusername": native.ROOM,
            "patinfo/pattedusername": "wxid_source_target",
        },
    }
    packet["messages"] = [message]
    return packet


def test_retained_pat_representation_keeps_neutral_roles_and_exact_room(source_binding):
    packet = _retained_pat_packet(source_binding)
    bank._validate_packet(packet, (packet["account_id"],))
    assert packet["messages"][0]["sender_id"] == ""
    assert packet["messages"][0]["direction"] == "unknown"


@pytest.mark.parametrize(
    "defect",
    [
        "subtype",
        "room",
        "actor",
        "direction",
        "unknown_system",
        "extra_field",
        "flags",
        "participant",
    ],
)
def test_generic_or_conflicting_type49_cannot_bypass_sender_proof(
    source_binding, defect
):
    packet = _retained_pat_packet(source_binding)
    message = packet["messages"][0]
    metadata = message["native_text"]
    if defect == "subtype":
        metadata["appmsg_subtype"] = 57
    elif defect == "room":
        metadata["system_source_ids"]["patinfo/chatusername"] = "999999@chatroom"
    elif defect == "actor":
        message["sender_id"] = "wxid_source_actor"
    elif defect == "direction":
        message["direction"] = "received"
    elif defect == "unknown_system":
        metadata["system_message_subtype"] = "unknown"
    elif defect == "extra_field":
        metadata["opaque_action"] = "unexpected"
    elif defect == "flags":
        metadata["flags"] = []
    else:
        metadata["system_source_ids"]["patinfo/fromusername"] = "123"
    with pytest.raises(ValueError):
        bank._validate_packet(packet, (packet["account_id"],))
