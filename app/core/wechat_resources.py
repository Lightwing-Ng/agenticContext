"""Present persistent WeChat resource references without exposing host paths.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re

from .wechat_archive import WechatArchiveRepository
from .wechat_mac_resources import WechatMacResourceResolver


RESOURCE_LABELS = {
    "image": "Image",
    "video": "Video",
    "file": "File",
    "audio": "Voice message",
    "sticker": "Sticker",
    "app": "App message",
    "unknown": "Message attachment",
}
_STATUS_LABELS = {
    "file": "Show in Finder",
    "candidate": "Local file candidate",
    "folder": "Mac folder",
    "missing": "Not found on this Mac",
}
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _card_label(value):
    """Keep a bounded original label as inert text without accepting markup."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 1024
        or any(ord(char) < 32 and char not in "\t\r\n" for char in value)
    ):
        return ""
    return value


def _html_resource_reference(provenance, account_id, peer_id):
    """Adapt an audited HTML frame without guessing a corresponding Mac file."""
    from .wechat_media_placeholders import _KINDS, _frame, media_placeholder_id

    reference = provenance.get("media_reference")
    node, locator = provenance.get("original_node"), provenance.get("locator")
    if (
        not isinstance(reference, dict)
        or not isinstance(node, dict)
        or not isinstance(locator, dict)
        or provenance.get("text_extraction_kind") != "resource_placeholder"
        or provenance.get("audit_receipt_basis")
        != "preserved_source_artifact_digest; no_payload_read"
        or reference.get("availability") != "metadata_only"
        or not account_id
        or reference.get("account_id") != account_id
        or reference.get("peer_id") != peer_id
        or type(reference.get("source_snapshot_id")) is not int
        or reference["source_snapshot_id"] <= 0
        or type(provenance.get("source_snapshot")) is not int
        or provenance.get("source_snapshot") != reference["source_snapshot_id"]
        or node.get("role") != "top_level"
        or node.get("status") != "deferred_top_level"
        or any(
            not isinstance(reference.get(key), str)
            or _DIGEST.fullmatch(reference[key]) is None
            for key in ("archive_sha256", "member_sha256", "audit_sha256")
        )
    ):
        return None
    source_type = node.get("type")
    kind = _KINDS.get(source_type) if isinstance(source_type, str) else None
    if source_type == "49":
        kind = ("app", "[App message]")
        if node.get("text_kind") == "rendered_app_text" and (
            type(node.get("text_length")) is not int
            or node["text_length"] != 0
            or node.get("text_sha256") != hashlib.sha256(b"").hexdigest()
        ):
            return None
    if node.get("text_kind") != "deferred" and not (
        source_type == "49" and node.get("text_kind") == "rendered_app_text"
    ):
        return None
    if kind is None or reference.get("kind") != kind[0]:
        return None
    try:
        entry = {
            "source_id": reference.get("source_id"),
            "archive_sha256": reference["archive_sha256"],
            "node": node,
        }
        frame = _frame(entry)
        identifier = media_placeholder_id(entry)
        member = locator.get("member")
        if (
            reference.get("frame") != frame
            or reference.get("id") != identifier
            or provenance.get("record_id") != identifier
            or any(locator.get(key) != value for key, value in frame.items())
            or locator.get("archive_sha256") != reference["archive_sha256"]
            or not isinstance(member, dict)
            or member.get("member_path") != frame["member_path"]
            or member.get("sha256") != reference["member_sha256"]
            or reference.get("export_msgid_namespace") != "MesLocalID"
            or locator.get("export_msgid_namespace") != "MesLocalID"
            or provenance.get("export_msgid_namespace") != "MesLocalID"
            or not isinstance(node.get("export_msgid"), str)
            or reference.get("export_msgid") != node["export_msgid"]
            or locator.get("export_msgid") != node["export_msgid"]
            or locator.get("export_msgtype") != source_type
        ):
            return None
    except (TypeError, ValueError):
        return None
    return {
        "kind": kind[0],
        "account_id": account_id,
        "peer_id": peer_id,
        "availability": "metadata_only",
    }


def resource_reference(provenance, account_id, peer_id):
    """Require the resource's original account and conversation scope."""
    if not isinstance(provenance, Mapping):
        return None
    reference = provenance.get("resource_reference")
    if reference is None:
        return _html_resource_reference(provenance, account_id, peer_id)
    if (
        not isinstance(reference, Mapping)
        or not isinstance(reference.get("kind"), str)
        or reference["kind"] not in RESOURCE_LABELS
        or not account_id
        or reference.get("account_id") != account_id
        or reference.get("peer_id") != peer_id
    ):
        return None
    return dict(reference)


def present_resource(reference, resolver):
    """Keep unavailable references visible and distinguish folders from files."""
    if reference.get("availability") == "metadata_only":
        label = RESOURCE_LABELS[reference["kind"]]
        result = {
            "kind": reference["kind"],
            "kind_label": label,
            "label": label,
            "status": "metadata_only",
            "status_label": "Metadata only",
            "action_label": "",
            "can_reveal": False,
        }
        if reference["kind"] == "app" and reference.get("app_subtype") == "5":
            result.update(
                label=_card_label(reference.get("safe_title")) or label,
                publisher_name=_card_label(reference.get("publisher_name"))
                or _card_label(reference.get("app_name")),
                is_link=True,
            )
        return result
    destination = resolver.resolve(reference)
    status = destination["status"]
    filename = reference.get("filename")
    label = (
        filename
        if isinstance(filename, str) and filename
        else RESOURCE_LABELS[reference["kind"]]
    )
    return {
        "kind": reference["kind"],
        "kind_label": RESOURCE_LABELS[reference["kind"]],
        "label": label,
        "status": status,
        "status_label": _STATUS_LABELS.get(status, _STATUS_LABELS["missing"]),
        "action_label": destination["label"],
        "can_reveal": status in {"file", "candidate", "folder"}
        and destination["path"] is not None,
    }


def resolve_resource_for_reveal(local_store_root, message_id):
    """Revalidate a stored reference and destination for each native action."""
    message = WechatArchiveRepository(local_store_root).resource_message(message_id)
    if message is None:
        raise LookupError("This resource reference is unavailable or needs refreshing.")
    reference = resource_reference(
        json.loads(message["provenance"]), message["account_id"], message["peer_id"]
    )
    if reference is None:
        raise LookupError("The resource account or conversation could not be verified.")
    if reference.get("availability") == "metadata_only":
        raise LookupError("This source preserves attachment metadata only.")
    destination = WechatMacResourceResolver(local_store_root).resolve(reference)
    if destination["path"] is None or destination["status"] not in {
        "file",
        "candidate",
        "folder",
    }:
        raise LookupError("The local WeChat file or folder is no longer available.")
    return destination
