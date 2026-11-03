"""Present persistent WeChat resource references without exposing host paths.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections.abc import Mapping
import json

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


def resource_reference(provenance, account_id, peer_id):
    """Require the resource's original account and conversation scope."""
    if not isinstance(provenance, Mapping):
        return None
    reference = provenance.get("resource_reference")
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
    destination = WechatMacResourceResolver(local_store_root).resolve(reference)
    if destination["path"] is None or destination["status"] not in {
        "file",
        "candidate",
        "folder",
    }:
        raise LookupError("The local WeChat file or folder is no longer available.")
    return destination
