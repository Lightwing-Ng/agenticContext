"""Prefer complete source quotes after independently proving the reply occurrence.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
from itertools import combinations
import re

from .wechat_contacts import canonical_contact_id


VERSION = "v1.0.0-codex.0"
MATCH_BASIS = "verified_incomplete_quote_sequence_v1"
_HASH = re.compile(r"[0-9a-f]{64}")
_CONTACT = re.compile(r"wechat:contact:[0-9a-f]{64}")
_ROOM = re.compile(r"[1-9][0-9]*@chatroom")
_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_SERVER = re.compile(r"-?[1-9][0-9]*")
_PHOTO_HASH = hashlib.sha256(b"[Photo]").hexdigest()


def _positive(value):
    return type(value) is int and value > 0


def _source(item):
    source = item.get("source_key")
    return (
        source
        if isinstance(source, tuple)
        and len(source) == 2
        and all(isinstance(value, str) and value for value in source)
        else None
    )


def _epoch(item):
    try:
        value = Decimal(str(item["epoch"]))
        return value if value.is_finite() else None
    except (KeyError, ValueError, InvalidOperation):
        return None


def _same_epoch(left, right):
    if left.get("epoch") is None:
        return True
    first, second = _epoch(left), _epoch(right)
    return first is not None and second is not None and first == second


def _event(item):
    """Count source ambiguity independently of whether a sender is verified."""
    key = item.get("key")
    if not isinstance(key, tuple) or len(key) != 3:
        return None
    quote = item.get("quote")
    reply_hash = quote.get("reply_hash") if isinstance(quote, dict) else key[0]
    if (
        not isinstance(reply_hash, str)
        or _HASH.fullmatch(reply_hash) is None
        or not isinstance(key[1], str)
        or _CLOCK.fullmatch(key[1]) is None
        or key[2] not in {"sent", "received"}
    ):
        return None
    try:
        if datetime.fromisoformat(key[1]).year <= 1970:
            return None
    except ValueError:
        return None
    return reply_hash, key[1], key[2]


def _verified(item):
    author = item.get("author")
    context = item.get("context", {})
    if (
        not isinstance(author, str)
        or _CONTACT.fullmatch(author) is None
        or not isinstance(context, dict)
        or context.get("contact_id") != author
        or _source(item) is None
        or _event(item) is None
        or not _positive(item.get("order"))
    ):
        return False
    if item.get("format") == "html":
        return True
    try:
        return (
            item.get("format") == "native"
            and _epoch(item) is not None
            and isinstance(item.get("server"), str)
            and _SERVER.fullmatch(item["server"]) is not None
            and author
            == canonical_contact_id(
                context.get("sender_id"), authority="native_message_sender"
            )
        )
    except (TypeError, ValueError):
        return False


def _reference(item, room):
    quote = item.get("quote")
    if not isinstance(quote, dict):
        return None
    signature = quote.get("reference_signature")
    if (
        not isinstance(signature, tuple)
        or len(signature) != 4
        or type(signature[0]) is not int
        or signature[0] not in {1, 3, 49}
        or not isinstance(signature[1], str)
        or not signature[1]
        or not isinstance(signature[2], str)
        or not isinstance(signature[3], str)
        or _SERVER.fullmatch(signature[3]) is None
        or quote.get("original_message_type") != signature[0]
        or quote.get("reference_sender_id") != signature[1]
        or quote.get("reference_chat_id") != signature[2]
        or quote.get("server") != signature[3]
        or quote.get("group_room") != room
        or _ROOM.fullmatch(signature[1]) is not None
        and signature[1] != room
    ):
        return None
    if signature[1] != room:
        try:
            if quote.get("author") != canonical_contact_id(
                signature[1], authority="native_message_sender"
            ):
                return None
        except (TypeError, ValueError):
            return None
    return signature


def _incomplete(item, room):
    quote = item.get("quote", {})
    return (
        _verified(item)
        and item.get("format") == "native"
        and type(item.get("message_type")) is int
        and item["message_type"] == 49
        and quote.get("unavailable") is True
        and quote.get("text_hash") == ""
        and _reference(item, room) is not None
    )


def _complete_html(item):
    quote = item.get("quote", {})
    return (
        _verified(item)
        and item.get("format") == "html"
        and type(item.get("message_type")) is int
        and item["message_type"] == 49
        and quote.get("unavailable") is not True
        and isinstance(quote.get("text_hash"), str)
        and _HASH.fullmatch(quote["text_hash"]) is not None
    )


def _quote_content_agrees(left, right, originals):
    """Use retained reference metadata; never create a missing original record."""
    from .wechat_conversation_quotes import _original_author_matches

    quote = right["quote"]
    if quote["original_message_type"] == 3:
        return (
            left["quote"]["text_hash"] == _PHOTO_HASH
            and isinstance(quote.get("speaker"), str)
            and bool(quote["speaker"])
            and left["quote"].get("speaker") == quote["speaker"]
        )
    targets = originals.get((right["source_key"], quote["server"]), ())
    matches = []
    for target in targets:
        if (
            target["order"] >= right["order"]
            or type(target.get("message_type")) is not int
            or target["message_type"] != quote["original_message_type"]
            or _event(target)[0] != left["quote"]["text_hash"]
        ):
            continue
        try:
            if _original_author_matches(right, target):
                matches.append(target)
        except (KeyError, TypeError, ValueError):
            continue
    return len(matches) == 1


def _anchor_indexes(records, counts, mapping, members, conflicts, owner):
    pairs = defaultdict(list)
    for preferred, group in members.items():
        proven = [
            records[context["id"]]
            for context in group
            if context.get("id") in records
            and mapping.get(context["id"], context["id"]) == preferred
            and _verified(records[context["id"]])
        ]
        for left in proven:
            if left["format"] != "html":
                continue
            event = _event(left)
            if counts[("html", left["source_key"], event)] != 1:
                continue
            for right in proven:
                if (
                    right["format"] == "native"
                    and right["server"] not in conflicts
                    and left["author"] == right["author"]
                    and left["key"] == right["key"]
                    and counts[("native", right["source_key"], event)] == 1
                    and _same_epoch(left, right)
                ):
                    pairs[(left["source_key"], right["source_key"])].append(
                        (left, right, preferred)
                    )
    result = {}
    for pair, anchors in pairs.items():
        anchors.sort(key=lambda value: (value[0]["order"], value[0]["id"]))
        authors = {left["author"] for left, _, _ in anchors}
        if (
            owner not in authors
            or len(authors) < 2
            or len(anchors) < 3
            or any(
                first[0]["order"] >= second[0]["order"]
                or first[1]["order"] >= second[1]["order"]
                for first, second in zip(anchors, anchors[1:])
            )
        ):
            continue
        result[pair] = [left["order"] for left, _, _ in anchors], anchors
    return result


def _neighbors(index, left, right):
    if index is None:
        return None
    orders, anchors = index
    low = bisect_left(orders, left["order"] - 8)
    high = bisect_right(orders, left["order"] + 8)
    nearby = anchors[low:high]
    if len(nearby) < 3 or any(
        pair[0]["order"] == left["order"]
        or pair[1]["order"] == right["order"]
        or (pair[0]["order"] < left["order"]) != (pair[1]["order"] < right["order"])
        for pair in nearby
    ):
        return None
    for proof in combinations(nearby, 3):
        if (
            len({pair[0]["author"] for pair in proof}) >= 2
            and proof[0][0]["order"] < left["order"] < proof[-1][0]["order"]
            and proof[0][1]["order"] < right["order"] < proof[-1][1]["order"]
        ):
            return [pair[2] for pair in proof]
    return None


def _group_members(left, matches, records, mapping, members, room):
    contexts, previous = {}, set()
    for item in (left, *(right for right, _ in matches)):
        preferred = mapping.get(item["id"], item["id"])
        if preferred in mapping:
            return None
        if preferred in members:
            previous.add(preferred)
            contexts.update((value["id"], dict(value)) for value in members[preferred])
        else:
            contexts[item["id"]] = dict(item["context"])
    expected_reference = matches[0][0]["quote"]["reference_signature"]
    for identifier in contexts:
        item = records.get(identifier)
        if (
            item is None
            or not _verified(item)
            or _event(item) != _event(left)
            or item["author"] != left["author"]
            or item.get("message_type") != 49
        ):
            return None
        if item["format"] == "html":
            if (
                not _complete_html(item)
                or item["quote"]["text_hash"] != left["quote"]["text_hash"]
            ):
                return None
        elif (
            not _incomplete(item, room)
            or item["quote"]["reference_signature"] != expected_reference
            or item["server"] != matches[0][0]["server"]
        ):
            # Existing complete native quote groups keep their established preference.
            return None
    return contexts, previous


def bridge_incomplete_quotes(candidates, current, mapping, members):
    """Project one complete reply while retaining every original source context."""
    updated_mapping, updated_members = dict(mapping), dict(members)
    room, owner = current.get("peer_id"), current.get("account_id")
    if (
        current.get("kind") != "group"
        or not isinstance(room, str)
        or _ROOM.fullmatch(room) is None
        or current.get("account_key", "account:" + str(owner))
        != "account:" + str(owner)
    ):
        return updated_mapping, updated_members
    try:
        owner = canonical_contact_id(owner, authority="native_account_owner")
    except (TypeError, ValueError):
        return updated_mapping, updated_members
    records = {item["id"]: item for item in candidates}
    if len(records) != len(candidates):
        return updated_mapping, updated_members
    counts = Counter(
        (item.get("format"), _source(item), _event(item)) for item in candidates
    )
    signatures, event_servers = defaultdict(set), defaultdict(set)
    for item in candidates:
        if item.get("format") == "native":
            quote = item.get("quote", {})
            signatures[item.get("server")].add(
                (
                    _event(item),
                    item.get("author"),
                    _epoch(item),
                    repr(quote.get("reference_signature")),
                )
            )
            if _verified(item):
                event_servers[_event(item)].add(item["server"])
    conflicts = {server for server, values in signatures.items() if len(values) > 1}
    originals, incomplete = defaultdict(list), defaultdict(list)
    for item in candidates:
        if (
            item.get("format") == "native"
            and _verified(item)
            and item["server"] not in conflicts
        ):
            originals[(item["source_key"], item["server"])].append(item)
            if _incomplete(item, room):
                incomplete[_event(item)].append(item)
    anchors = _anchor_indexes(records, counts, mapping, members, conflicts, owner)
    for left in candidates:
        if (
            not _complete_html(left)
            or counts[("html", left["source_key"], _event(left))] != 1
            or len(event_servers[_event(left)]) != 1
        ):
            continue
        matches = []
        for right in incomplete[_event(left)]:
            if (
                right["author"] != left["author"]
                or counts[("native", right["source_key"], _event(right))] != 1
                or not _same_epoch(left, right)
                or not _quote_content_agrees(left, right, originals)
            ):
                continue
            proof = _neighbors(
                anchors.get((left["source_key"], right["source_key"])), left, right
            )
            if proof:
                matches.append((right, proof))
        if (
            not matches
            or len({right["quote"]["reference_signature"] for right, _ in matches}) != 1
        ):
            continue
        grouped = _group_members(
            left, matches, records, updated_mapping, updated_members, room
        )
        if grouped is None:
            continue
        contexts, previous = grouped
        preferred = min(
            (
                records[identifier]
                for identifier in contexts
                if records[identifier]["format"] == "html"
            ),
            key=lambda item: (item["rank"], item["id"]),
        )["id"]
        best_proof = min(matches, key=lambda value: (value[0]["rank"], value[0]["id"]))[
            1
        ]
        proofs = {right["id"]: proof for right, proof in matches}
        for identifier, context in contexts.items():
            context.update(
                event_kind="quoted_reply",
                quote_match_basis=MATCH_BASIS,
                quote_match_anchor_ids=proofs.get(identifier, best_proof),
            )
            item = records[identifier]
            if item["format"] == "native":
                kind, sender, chat, server = item["quote"]["reference_signature"]
                context.update(
                    quoted_reference_type=kind,
                    quoted_reference_server_id=server,
                    quoted_reference_sender_id=sender,
                    quoted_reference_chat_id=chat,
                )
        for identifier in previous:
            updated_members.pop(identifier)
        updated_members[preferred] = [
            contexts[identifier]
            for identifier in sorted(
                contexts,
                key=lambda identifier: (
                    identifier != preferred,
                    records[identifier]["rank"],
                    identifier,
                ),
            )
        ]
        updated_mapping.pop(preferred, None)
        updated_mapping.update(
            (identifier, preferred)
            for identifier in contexts
            if identifier != preferred
        )
    return updated_mapping, updated_members
