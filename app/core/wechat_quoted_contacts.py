"""Project verified native quoted-speaker names without rewriting source labels.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

import hashlib
import re

from .wechat_contacts import canonical_contact_id, classify_wechat_identifier
from .wechat_reconcile import native_text_metadata


_NATIVE_USER = re.compile(r"wxid_[A-Za-z0-9_]{1,100}")
_ROOM = re.compile(r"[1-9][0-9]*@chatroom")
_EXACT_ROOM = re.compile(r"[1-9][0-9]*@(?:im\.)?chatroom")
_MAX_CHILDREN = 4_096
_MAX_DEPTH = 32
_MESSAGE_ID = re.compile(r"[0-9a-f]{64}")


def _native_user(value):
    return isinstance(value, str) and _NATIVE_USER.fullmatch(value) is not None


def _quoted_author(reference, peer):
    """Interpret the two observed native fromusr/chatusr roles without name matching."""
    sender = reference.get("source_sender_id", "")
    chat = reference.get("source_chat_id", "")
    if (
        not isinstance(sender, str)
        or not isinstance(chat, str)
        or not isinstance(peer, str)
    ):
        return ""
    if _ROOM.fullmatch(sender):
        return chat if sender == peer and _native_user(chat) else ""
    if not _native_user(sender):
        return ""
    # An explicit chat may identify this room or repeat the author. Other roles
    # are ambiguous and must retain their source-rendered label.
    if chat and chat not in {sender, peer}:
        return ""
    return sender


def _exact_original_quoted_author(reference, peer, original_sender, reply_sender):
    """Interpret source roles only after the caller proves the exact native original."""
    if not isinstance(reference, dict) or not isinstance(peer, str) or not peer:
        return ""

    def native_user(value):
        if classify_wechat_identifier(value)["kind"] not in {
            "wechat_account", "wechat_identity"
        }:
            return False
        try:
            canonical_contact_id(value, authority="native_message_sender")
        except (TypeError, ValueError):
            return False
        return True

    if not native_user(original_sender):
        return ""
    sender = reference.get("source_sender_id", "")
    chat = reference.get("source_chat_id", "")
    if not isinstance(sender, str) or not isinstance(chat, str):
        return ""
    if _EXACT_ROOM.fullmatch(peer) and sender == peer:
        return (
            original_sender
            if chat == original_sender
            or chat == reply_sender and native_user(reply_sender)
            else ""
        )
    if sender != original_sender or chat not in {"", peer, original_sender}:
        return ""
    return original_sender


def _native_quote(context):
    """Require the rendered reference to agree with validated native evidence."""
    quote = context.get("quoted_reply")
    provenance = context.get("provenance")
    if not isinstance(quote, dict) or not isinstance(provenance, dict):
        return None
    component = provenance.get("embedded_forwarded_component")
    if isinstance(component, dict):
        if component.get("kind") != "forwarded_quote":
            return None
        evidence = {
            "message_type": 49,
            "text_extraction_kind": "quoted_reply",
            "appmsg_subtype": 57,
            "text_components": component.get("text_components"),
            "quoted_reference": component.get("quoted_reference"),
        }
    else:
        evidence = provenance
    if (
        type(evidence.get("message_type")) is not int
        or evidence["message_type"] != 49
        or evidence.get("text_extraction_kind") != "quoted_reply"
    ):
        return None
    locator = evidence.get("locator", {})
    if not isinstance(locator, dict):
        return None
    try:
        metadata = native_text_metadata(evidence, locator)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
    if metadata.get("appmsg_subtype") not in {None, 57}:
        return None
    reference = metadata.get("quoted_reference")
    if reference is None:
        components = [
            item
            for item in metadata.get("text_components", [])
            if item["kind"] in {"quoted_text", "quoted_forwarded_text"}
        ]
        if len(components) != 1:
            return None
        reference = {
            key: value
            for key, value in components[0].items()
            if key
            not in {
                "kind",
                "text",
                "text_components",
                "flags",
                "omitted_forwarded_items",
            }
        }
    if (
        not isinstance(reference, dict)
        or quote.get("reference") != reference
        or reference.get("authority") == "user_confirmation"
    ):
        return None
    return quote, reference


def _source_envelope(context, partitions, display_names, source_snapshots):
    owner = display_names.account_owner(context)
    partition = partitions.get(context.get("partition_id"))
    provenance = context.get("provenance")
    if (
        not owner
        or partition is None
        or display_names.account_owner(partition) != owner
        or partition.get("key") != context.get("account_key")
        or not isinstance(provenance, dict)
    ):
        return None
    key = provenance.get("source_snapshot")
    snapshot = source_snapshots.get(key) if type(key) is int else None
    if not isinstance(snapshot, dict) or snapshot.get("source_id") != context.get(
        "partition_id"
    ):
        return None
    source = snapshot.get("metadata")
    if (
        not isinstance(source, dict)
        or source.get("source_id") != context["partition_id"]
        or source.get("source_account_id") != owner
        or source.get("account_hash_verified") is not True
        or source.get("account_hash") != hashlib.md5(owner.encode()).hexdigest()
        or not isinstance(snapshot.get("snapshot_id"), str)
        or not snapshot["snapshot_id"]
    ):
        return None
    return owner, snapshot


def _proven_original_author(item, snapshot, reference):
    """Use a pinned source event's original author without interpreting chat labels."""
    context = item.messaging
    observations = context.get("source_observations", [])
    server = reference.get("source_ids", {}).get("svrid")
    if (
        not isinstance(observations, list)
        or len(observations) > _MAX_CHILDREN
        or type(server) not in {str, int}
        or re.fullmatch(r"-?[1-9][0-9]*", str(server)) is None
    ):
        return ""
    authors = set()
    for observation in observations:
        if not isinstance(observation, dict) or any(
            observation.get(key) != value
            for key, value in (
                ("id", item.stable_id),
                ("source_id", context.get("partition_id")),
                ("source_snapshot_id", snapshot["snapshot_id"]),
                ("sender_id", context.get("sender_id")),
                ("event_kind", "quoted_reply"),
                ("quoted_server_id", str(server)),
            )
        ):
            continue
        original = observation.get("quoted_original_id")
        sender = observation.get("quoted_sender_id")
        if (
            not isinstance(original, str)
            or _MESSAGE_ID.fullmatch(original) is None
            or original == item.stable_id
            or not isinstance(sender, str)
            or not sender
            or sender.endswith("@chatroom")
        ):
            continue
        try:
            contact = canonical_contact_id(sender, authority="native_message_sender")
        except ValueError:
            continue
        if observation.get("quoted_contact_id") == contact:
            authors.add(sender)
    return next(iter(authors)) if len(authors) == 1 else ""


def project_native_quoted_contacts(
    items, repository, display_names, account_rows, source_snapshots, *, originals=()
):
    """Add viewer fields to native quotes inside their verified receiving accounts.

    The source speaker, reference, body, and component evidence are left untouched.
    Label-only HTML/PDF quotations and unverified native accounts retain their labels.
    """
    partitions = {row["partition_id"]: row for row in account_rows}
    from .wechat_quoted_resources import _exact_resource_original, _originals_by_server

    resource_originals = _originals_by_server(items, originals)
    requests, quotes, expected_contacts = [], {}, {}
    remaining = _MAX_CHILDREN

    def collect(item, parent, owner, snapshot, peer):
        context = item.messaging
        candidate = _native_quote(context)
        if candidate is None:
            return
        quote, reference = candidate
        primary_group = (
            item is parent and isinstance(peer, str) and _ROOM.fullmatch(peer)
        )
        proved_sender = (
            _proven_original_author(item, snapshot, reference) if primary_group else ""
        )
        if not proved_sender and item is parent:
            original = _exact_resource_original(
                item, resource_originals, partitions, display_names, source_snapshots
            )
            if original is not None:
                proved_sender = original[0].messaging["sender_id"]
        sender = proved_sender
        if not sender:
            if isinstance(peer, str) and _ROOM.fullmatch(peer) and reference.get("source_sender_id") == peer:
                # Room envelopes do not distinguish a quoted author from a reply
                # author in chatusr. A nested quote cannot borrow its parent's proof.
                return
            sender = _quoted_author(reference, peer)
        if not sender:
            return
        identifier = item.stable_id + ":quoted-speaker"
        requests.append(
            {
                "id": identifier,
                "conversation_id": parent.conversation_id,
                "source_id": parent.messaging["partition_id"],
                "source_snapshot_id": snapshot["snapshot_id"],
                "account_id": owner,
                "sender_id": sender,
                "sender_label": reference.get("source_sender_label")
                or quote.get("speaker", ""),
                "peer_id": peer,
                "conversation_kind": "group"
                if isinstance(peer, str) and _ROOM.fullmatch(peer)
                else "unclassified",
                "timestamp_value": item.last_seen_at,
                "is_system": False,
            }
        )
        quotes[identifier] = quote
        if proved_sender:
            expected_contacts[identifier] = canonical_contact_id(
                proved_sender, authority="native_message_sender"
            )

    def visit(record, parent, owner, snapshot, depth=0, seen=None):
        nonlocal remaining
        if (
            depth > _MAX_DEPTH
            or remaining <= 0
            or not isinstance(record, dict)
            or record.get("authority") != "native_forwarded_envelope"
            or not isinstance(record.get("items"), (list, tuple))
        ):
            return
        seen = set() if seen is None else seen
        if id(record) in seen:
            return
        seen.add(id(record))
        for child in record["items"]:
            if remaining <= 0:
                break
            remaining -= 1
            context = child.messaging
            component = context.get("provenance", {}).get(
                "embedded_forwarded_component", {}
            )
            peer = component.get("source_chat_id", "")
            peer = peer if isinstance(peer, str) else ""
            collect(child, parent, owner, snapshot, peer)
            visit(
                context.get("forwarded_record"),
                parent,
                owner,
                snapshot,
                depth + 1,
                seen,
            )
            quote = context.get("quoted_reply")
            if isinstance(quote, dict):
                visit(
                    quote.get("forwarded_record"),
                    parent,
                    owner,
                    snapshot,
                    depth + 1,
                    seen,
                )

    for item in items:
        envelope = _source_envelope(
            item.messaging, partitions, display_names, source_snapshots
        )
        if envelope is None:
            continue
        owner, snapshot = envelope
        collect(item, item, owner, snapshot, item.messaging.get("peer_id", ""))
        visit(item.messaging.get("forwarded_record"), item, owner, snapshot)
        quote = item.messaging.get("quoted_reply")
        if isinstance(quote, dict):
            visit(quote.get("forwarded_record"), item, owner, snapshot)
    for offset in range(0, len(requests), 400):
        resolved = repository.resolve_messages(requests[offset : offset + 400])
        for identifier, contact in resolved.items():
            if contact.get("identity_status") != "verified":
                continue
            expected = expected_contacts.get(identifier)
            if expected and contact.get("id") != expected:
                continue
            name = display_names.contact_name(contact)
            viewer = name or contact.get("preferred_name") or contact.get("display_name")
            if not isinstance(viewer, str) or not viewer:
                continue
            quotes[identifier]["viewer_display_name"] = viewer
            quotes[identifier]["contact"] = {
                **contact,
                "viewer_display_name": viewer,
                "viewer_display_name_source": "local_user_preference"
                if name
                else "canonical_contact",
            }
