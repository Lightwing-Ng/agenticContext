"""Prove quoted replies through their already matched original text events.

Code version: v1.3.0-codex.0
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import re

from .wechat_contacts import canonical_contact_id, classify_wechat_identifier


VERSION = "v1.3.0-codex.0"
_ROOM = re.compile(r"[1-9][0-9]*@chatroom")


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _native_user(value):
    """Validate an explicit native user field without treating labels as IDs."""
    if classify_wechat_identifier(value)["kind"] not in {
        "wechat_account",
        "wechat_identity",
    }:
        return ""
    try:
        return canonical_contact_id(value, authority="native_message_sender")
    except (TypeError, ValueError):
        return ""


def _incomplete_native_quote(row, metadata):
    """Retain reference evidence without inventing the missing original content."""
    components = metadata.get("text_components", [])
    room = row.get("peer_id")
    if (
        metadata.get("appmsg_subtype") != 57
        or len(components) != 1
        or components[0].get("kind") != "reply_text"
        or row.get("kind") != "group"
        or not isinstance(room, str)
        or _ROOM.fullmatch(room) is None
    ):
        return None
    reply = components[0].get("text")
    reference = metadata.get("quoted_reference", {})
    original_type = reference.get("original_message_type")
    sender = reference.get("source_sender_id")
    chat = reference.get("source_chat_id")
    speaker = reference.get("source_sender_label")
    server = reference.get("source_ids", {}).get("svrid")
    if (
        not isinstance(reply, str)
        or not reply.strip()
        or row.get("text") != reply
        or type(original_type) is not int
        or original_type not in {1, 3, 49}
        or type(server) not in {str, int}
        or re.fullmatch(r"-?[1-9][0-9]*", str(server)) is None
        or not isinstance(speaker, str)
        or not speaker.strip()
        or sender != room
        and not _native_user(sender)
        or chat != room
        and not _native_user(chat)
    ):
        return None
    return {
        "reply_hash": _hash(reply),
        "text_hash": "",
        "author": "" if sender == room else _native_user(sender),
        "speaker": speaker,
        "server": str(server),
        "unavailable": True,
        "original_message_type": original_type,
        "reference_signature": (original_type, sender, chat, str(server)),
        "group_room": room,
        "reference_sender_id": sender,
        "reference_chat_id": chat,
    }


def quoted_event(row, provenance, *, native):
    """Keep current replies and quoted originals in separate structural roles."""
    from .wechat_history_browser import (
        _html_quoted_reply_presentation,
        _native_quoted_reply_presentation,
        _quoted_envelope_fields,
    )
    from .wechat_reconcile import native_text_metadata

    if native:
        metadata = native_text_metadata(provenance, provenance.get("locator", {}))
        components = metadata.get("text_components", [])
        if len(components) == 1:
            return _incomplete_native_quote(row, metadata)
        if (
            metadata.get("appmsg_subtype") != 57
            or len(components) != 2
            or [item.get("kind") for item in components]
            != ["reply_text", "quoted_text"]
        ):
            return None
        presentation = _native_quoted_reply_presentation(provenance)
        reference = metadata.get("quoted_reference", {})
        quoted = components[1]
        server = reference.get("source_ids", {}).get("svrid")
        author = reference.get("source_sender_id")
        if (
            type(reference.get("original_message_type")) is not int
            or reference["original_message_type"] != 1
            or reference.get("content_unavailable")
            or type(server) not in {str, int}
            or re.fullmatch(r"-?[1-9][0-9]*", str(server)) is None
            or not isinstance(author, str)
            or not author
            or any(
                quoted.get(key) != reference.get(key)
                for key in ("original_message_type", "source_sender_id", "source_ids")
            )
        ):
            return None
        group_room = row.get("peer_id", "") if row.get("kind") == "group" else ""
        if row.get("kind") == "group" and (
            not isinstance(group_room, str)
            or _ROOM.fullmatch(group_room) is None
            or reference.get("source_chat_id") != quoted.get("source_chat_id")
            or _ROOM.fullmatch(author) is not None
            and author != group_room
        ):
            return None
        quoted_author = (
            ""
            if group_room and author == group_room
            else canonical_contact_id(author, authority="native_message_sender")
        )
        label = reference.get("source_sender_label")
        speaker = (
            label
            if isinstance(label, str)
            and label
            and label == quoted.get("source_sender_label")
            else ""
        )
    else:
        presentation = _html_quoted_reply_presentation(row["text"], provenance)
        components = provenance.get("quoted_components", [])
        fields = (
            _quoted_envelope_fields(components[0]["text"])
            if len(components) == 1
            else None
        )
        if fields is None:
            return None
        speaker = fields["speaker"]
        quoted_author, server = "", ""
    if not presentation or not presentation.get("quoted_reply"):
        return None
    reply = presentation["display_text"]
    text = presentation["quoted_reply"]["text"]
    if (
        not reply.strip()
        or not text.strip()
        or native
        and row["text"] != reply + "\n\n" + text
    ):
        return None
    return {
        "reply_hash": _hash(reply),
        "text_hash": _hash(text),
        "author": quoted_author,
        "speaker": speaker,
        "server": str(server),
        **(
            {
                "group_room": group_room,
                "reference_sender_id": author,
                "reference_chat_id": reference.get("source_chat_id", ""),
            }
            if native and group_room
            else {}
        ),
    }


def _original_author_matches(item, target):
    """Resolve room-valued quote roles only through the exact native original."""
    quote = item["quote"]
    room = quote.get("group_room")
    if not room:
        return target["author"] == quote["author"]
    sender, chat = quote["reference_sender_id"], quote["reference_chat_id"]
    if sender == room:
        # In observed group envelopes, chatusr may identify the reply author.
        return chat in {target["context"]["sender_id"], item["context"]["sender_id"]}
    return target["author"] == quote["author"] and chat in {"", room, sender}


def proven_quote_candidates(candidates, mapping, members):
    """Link originals first; accept old labels only from the same native reply."""
    plain = [item for item in candidates if "quote" not in item]
    quoted = [item for item in candidates if "quote" in item]
    native_targets, html_targets = defaultdict(list), defaultdict(list)
    html_text_targets = defaultdict(list)
    proven_ids = set(mapping) | set(members)
    originals = {item["id"]: item for item in plain}
    for item in plain:
        if item["format"] == "native":
            native_targets[(item["source_key"], item["server"])].append(item)
        else:
            html_text_targets[(item["source_key"], item["key"][0])].append(item)
            html_targets[
                (item["source_key"], item["key"][0], item["context"]["sender_label"])
            ].append(item)
    result, native_envelopes = [], defaultdict(set)
    # Resolve native server references before consulting exporter-rendered labels.
    ordered = sorted(quoted, key=lambda item: item["format"] != "native")
    for item in ordered:
        quote = item["quote"]
        if quote.get("unavailable"):
            continue
        if item["format"] == "native":
            matches = native_targets[(item["source_key"], quote["server"])]
            matches = [
                target
                for target in matches
                if target["order"] < item["order"]
                and _original_author_matches(item, target)
                and target["key"][0] == quote["text_hash"]
            ]
        else:
            matches = [
                target
                for target in html_targets[
                    (item["source_key"], quote["text_hash"], quote["speaker"])
                ]
                if target["order"] < item["order"]
            ]
            if not matches:
                alternatives = [
                    target
                    for target in html_text_targets[
                        (item["source_key"], quote["text_hash"])
                    ]
                    if target["order"] < item["order"]
                ]
                if len(alternatives) == 1:
                    target = alternatives[0]
                    preferred = mapping.get(target["id"], target["id"])
                    envelope_key = (item["key"], item["author"], quote["speaker"])
                    if (preferred, target["author"]) in native_envelopes[envelope_key]:
                        matches = alternatives
        if len(matches) != 1 or matches[0]["id"] not in proven_ids:
            continue
        target = matches[0]
        preferred = mapping.get(target["id"], target["id"])
        original = originals[preferred]
        if item["format"] == "native" and quote["speaker"]:
            native_envelopes[(item["key"], item["author"], quote["speaker"])].add(
                (preferred, target["author"])
            )
        result.append(
            {
                **item,
                "key": (*item["key"], preferred, target["author"]),
                "context": {
                    **item["context"],
                    "event_kind": "quoted_reply",
                    "quoted_original_id": preferred,
                    "quoted_contact_id": target["author"],
                    "quoted_sender_id": original["context"]["sender_id"],
                    "quoted_server_id": original["server"],
                },
            }
        )
    return result
