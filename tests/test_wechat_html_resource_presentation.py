"""Audited HTML attachments reuse resource cards without consulting local files.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core.wechat_resources import (
    present_resource,
    resolve_resource_for_reveal,
    resource_reference,
)
from scripts.index_wechat_html_media import projected
from tests import test_wechat_html_media_index as html
from tests import test_wechat_media_placeholders as frames


def _proof(source_type="image"):
    entry, header, members = html._source()
    entry["node"]["type"] = source_type
    row, _ = projected(entry, header, members, frames.OWNER, frames.PEER)
    return json.loads(row[11])


@pytest.mark.parametrize(
    "source_type,kind,label",
    [
        ("image", "image", "Image"),
        ("3", "image", "Image"),
        ("video", "video", "Video"),
        ("62", "video", "Video"),
        ("audio", "audio", "Voice message"),
        ("emoji", "sticker", "Sticker"),
        ("47", "sticker", "Sticker"),
        ("file", "file", "File"),
        ("49", "app", "App message"),
    ],
)
def test_exact_audited_types_use_standard_metadata_cards_without_mac_resolution(
    source_type, kind, label
):
    proof = _proof(source_type)
    before = deepcopy(proof)
    reference = resource_reference(proof, frames.OWNER, frames.PEER)
    resolver = Mock()
    assert present_resource(reference, resolver) == {
        "kind": kind,
        "kind_label": label,
        "label": label,
        "status": "metadata_only",
        "status_label": "Metadata only",
        "action_label": "",
        "can_reveal": False,
    }
    resolver.resolve.assert_not_called()
    assert proof == before


@pytest.mark.parametrize(
    "damage",
    [
        "account",
        "peer",
        "availability",
        "source_id",
        "archive_digest",
        "member_digest",
        "audit_digest",
        "record_id",
        "reference_id",
        "snapshot",
        "snapshot_boolean",
        "frame",
        "locator",
        "unsafe_member",
        "member_receipt",
        "member_path",
        "kind",
        "source_type",
        "text_kind",
        "role",
        "status",
        "audit_basis",
        "extraction",
        "namespace",
        "export_id",
        "export_type",
        "nonempty_app",
    ],
)
def test_unverified_or_inconsistent_frames_do_not_become_resource_cards(damage):
    proof = _proof("49" if damage == "nonempty_app" else "image")
    reference = proof["media_reference"]
    if damage == "account":
        reference["account_id"] = "wxid_another_owner"
    elif damage == "peer":
        reference["peer_id"] = "wxid_another_peer"
    elif damage == "availability":
        reference["availability"] = "file"
    elif damage == "source_id":
        reference["source_id"] = "html-" + "0" * 24
    elif damage == "archive_digest":
        reference["archive_sha256"] = "not-a-digest"
    elif damage == "member_digest":
        reference["member_sha256"] = "0" * 64
    elif damage == "audit_digest":
        reference["audit_sha256"] = None
    elif damage == "record_id":
        proof["record_id"] = "0" * 64
    elif damage == "reference_id":
        reference["id"] = "0" * 64
    elif damage == "snapshot":
        proof["source_snapshot"] = 2
    elif damage == "snapshot_boolean":
        reference["source_snapshot_id"] = True
    elif damage == "frame":
        reference["frame"]["message_ordinal"] = 123
    elif damage == "locator":
        proof["locator"]["page_message_ordinal"] = 123
    elif damage == "unsafe_member":
        proof["original_node"]["source_locator"]["member_path"] = "../private/file"
    elif damage == "member_receipt":
        proof["locator"]["member"] = {}
    elif damage == "member_path":
        proof["locator"]["member"]["member_path"] = "Another.html"
    elif damage == "kind":
        reference["kind"] = "video"
    elif damage == "source_type":
        proof["original_node"]["type"] = "1"
    elif damage == "text_kind":
        proof["original_node"]["text_kind"] = "plain_text"
    elif damage == "role":
        proof["original_node"]["role"] = "forwarded"
    elif damage == "status":
        proof["original_node"]["status"] = "retained"
    elif damage == "audit_basis":
        proof["audit_receipt_basis"] = "filename_guess"
    elif damage == "extraction":
        proof["text_extraction_kind"] = "plain_text"
    elif damage == "namespace":
        reference["export_msgid_namespace"] = "svrid"
    elif damage == "export_id":
        reference["export_msgid"] = "999"
    elif damage == "export_type":
        proof["locator"]["export_msgtype"] = "43"
    elif damage == "nonempty_app":
        proof["original_node"].update(text_kind="rendered_app_text", text_length=1)
    assert resource_reference(proof, frames.OWNER, frames.PEER) is None


@pytest.mark.parametrize("literal", ["[Image]", "[Sticker]", "Image", "file.pdf"])
def test_literal_message_labels_cannot_spoof_attachment_metadata(literal):
    assert resource_reference({"text": literal}, frames.OWNER, frames.PEER) is None


def test_metadata_only_attachment_rejects_finder_action_without_resolver_creation(
    tmp_path,
):
    proof = _proof()
    message = {
        "account_id": frames.OWNER,
        "peer_id": frames.PEER,
        "provenance": json.dumps(proof),
    }
    repository = SimpleNamespace(resource_message=Mock(return_value=message))
    with (
        patch(
            "app.core.wechat_resources.WechatArchiveRepository", return_value=repository
        ),
        patch("app.core.wechat_resources.WechatMacResourceResolver") as resolver,
    ):
        with pytest.raises(LookupError, match="metadata only"):
            resolve_resource_for_reveal(tmp_path, proof["record_id"])
        resolver.assert_not_called()
