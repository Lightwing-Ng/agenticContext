"""Project explicitly reviewed quote copies while retaining uncertain originals.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import stat

from .wechat_incomplete_quotes import (
    _complete_html,
    _epoch,
    _event,
    _same_epoch,
    _source,
    _verified,
)


VERSION = "v1.0.0-codex.0"
MATCH_BASIS = "user_reviewed_quoted_reply_occurrence_v1"
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_REVIEWS = 64
_ID = re.compile(r"[0-9a-f]{64}")
_SESSION = re.compile(r"wechat:[0-9a-f]{64}")
_SERVER = re.compile(r"-?[1-9][0-9]*")
_ROOM = re.compile(r"[1-9][0-9]*@chatroom")


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def reviewed_quotes_path(root):
    return Path(root) / "messaging" / "wechat" / "conversation_quote_reviews.json"


def _read(root):
    path = reviewed_quotes_path(root)
    try:
        if any(
            parent.is_symlink() for parent in (path, path.parent, path.parent.parent)
        ):
            return "unavailable", b""
        before = path.stat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > MAX_REGISTRY_BYTES
            or getattr(before, "st_flags", 0) & 0x40000000
        ):
            return "unavailable", b""
        with path.open("rb") as stream:
            raw = stream.read(MAX_REGISTRY_BYTES + 1)
        after = path.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            return "changed", b""
        return ("present" if len(raw) <= MAX_REGISTRY_BYTES else "oversized"), raw
    except FileNotFoundError:
        return "missing", b""
    except OSError:
        return "unavailable", b""


def reviewed_quotes_digest(root):
    status, raw = _read(root)
    return _sha(json.dumps([VERSION, status, hashlib.sha256(raw).hexdigest()]))


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate quote-review key.")
        value[key] = item
    return value


def _declarations(root):
    status, raw = _read(root)
    if status != "present":
        return []
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "quote_reviews"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or not isinstance(value["quote_reviews"], list)
            or len(value["quote_reviews"]) > MAX_REVIEWS
        ):
            return []
        used, evidence = set(), set()
        for review in value["quote_reviews"]:
            if (
                not isinstance(review, dict)
                or set(review)
                != {
                    "authority",
                    "conversation_id",
                    "account_key",
                    "preferred_message_id",
                    "members",
                    "evidence_id",
                }
                or review["authority"] != "user_confirmed_quoted_reply_copy"
                or not isinstance(review["conversation_id"], str)
                or _SESSION.fullmatch(review["conversation_id"]) is None
                or not isinstance(review["account_key"], str)
                or not review["account_key"].startswith("account:")
                or not isinstance(review["evidence_id"], str)
                or _ID.fullmatch(review["evidence_id"]) is None
                or review["evidence_id"] in evidence
                or not isinstance(review["members"], list)
                or not 2 <= len(review["members"]) <= 8
            ):
                return []
            selected = set()
            for member in review["members"]:
                if (
                    not isinstance(member, dict)
                    or set(member)
                    != {
                        "message_id",
                        "text_sha256",
                        "provenance_sha256",
                        "source_id",
                        "source_snapshot_id",
                    }
                    or any(
                        not isinstance(member[field], str)
                        or _ID.fullmatch(member[field]) is None
                        for field in ("message_id", "text_sha256", "provenance_sha256")
                    )
                    or any(
                        not isinstance(member[field], str) or not member[field]
                        for field in ("source_id", "source_snapshot_id")
                    )
                    or member["message_id"] in used
                ):
                    return []
                selected.add(member["message_id"])
                used.add(member["message_id"])
            if review["preferred_message_id"] not in selected:
                return []
            evidence.add(review["evidence_id"])
        return value["quote_reviews"]
    except (ValueError, TypeError, KeyError):
        return []


def _native_reference(item, room):
    quote = item["quote"]
    kind = quote.get("original_message_type", 1)
    sender, chat, server = (
        quote.get("reference_sender_id"),
        quote.get("reference_chat_id"),
        quote.get("server"),
    )
    if (
        type(kind) is not int
        or kind not in {1, 3, 49}
        or quote.get("group_room") != room
        or not isinstance(sender, str)
        or not sender
        or not isinstance(chat, str)
        or not chat
        or not isinstance(server, str)
        or _SERVER.fullmatch(server) is None
    ):
        return None
    return kind, sender, chat, server


def _plan(connection, review, records, counts, current, mapping, members):
    if review["account_key"] != current.get("account_key"):
        return None
    preferred = records.get(review["preferred_message_id"])
    if preferred is None or not _complete_html(preferred):
        return None
    selected, natives = [], []
    for pin in review["members"]:
        item = records.get(pin["message_id"])
        if (
            item is None
            or not _verified(item)
            or item.get("message_type") != 49
            or not isinstance(item.get("quote"), dict)
            or _event(item) != _event(preferred)
            or item["author"] != preferred["author"]
            or not _same_epoch(preferred, item)
            or item["source_key"] != (pin["source_id"], pin["source_snapshot_id"])
            or counts[(item["format"], _source(item), _event(item))] != 1
        ):
            return None
        # A stronger existing proof keeps its preference and full context unchanged.
        if item["id"] in mapping or item["id"] in members:
            return None
        row = connection.execute(
            "SELECT conversation_id,text,provenance FROM main.messages WHERE id=?",
            (item["id"],),
        ).fetchone()
        if (
            row is None
            or row["conversation_id"] != review["conversation_id"]
            or _sha(row["text"]) != pin["text_sha256"]
            or _sha(row["provenance"]) != pin["provenance_sha256"]
        ):
            return None
        quote = item["quote"]
        if item["format"] == "html":
            if (
                not _complete_html(item)
                or quote["text_hash"] != preferred["quote"]["text_hash"]
            ):
                return None
        else:
            reference = _native_reference(item, current["peer_id"])
            if reference is None:
                return None
            if (
                quote.get("unavailable") is not True
                and quote["text_hash"] != preferred["quote"]["text_hash"]
            ):
                return None
            if quote.get("speaker") != preferred["quote"].get("speaker"):
                return None
            natives.append((item["server"], _epoch(item), reference))
        selected.append(item)
    if not natives or len(set(natives)) != 1:
        return None
    server, epoch, reference = natives[0]
    for item in records.values():
        if item.get("format") == "native" and item.get("server") == server:
            if (
                _event(item) != _event(preferred)
                or item.get("author") != preferred["author"]
                or _epoch(item) != epoch
                or not isinstance(item.get("quote"), dict)
                or _native_reference(item, current["peer_id"]) != reference
            ):
                return None
    return selected


def bridge_reviewed_quotes(
    repository, connection, session, current, candidates, mapping, members
):
    """Apply only pinned human decisions; absence of an original remains uncertainty."""
    updated_mapping, updated_members = dict(mapping), dict(members)
    room = current.get("peer_id")
    if (
        current.get("kind") != "group"
        or not isinstance(room, str)
        or _ROOM.fullmatch(room) is None
    ):
        return updated_mapping, updated_members
    records = {item["id"]: item for item in candidates}
    if len(records) != len(candidates):
        return updated_mapping, updated_members
    counts = Counter(
        (item.get("format"), _source(item), _event(item)) for item in candidates
    )
    root = repository.path.parent.parent.parent
    for review in _declarations(root):
        if review["conversation_id"] != session:
            continue
        selected = _plan(
            connection,
            review,
            records,
            counts,
            current,
            updated_mapping,
            updated_members,
        )
        if not selected:
            continue
        preferred = review["preferred_message_id"]
        contexts = []
        for item in selected:
            previous = updated_mapping.pop(item["id"], item["id"])
            updated_members.pop(previous, None)
            context = {
                **item["context"],
                "event_kind": "quoted_reply",
                "quote_match_basis": MATCH_BASIS,
                "quote_review_evidence_id": review["evidence_id"],
            }
            if item["format"] == "native":
                kind, sender, chat, server = _native_reference(item, room)
                context.update(
                    quoted_reference_type=kind,
                    quoted_reference_server_id=server,
                    quoted_reference_sender_id=sender,
                    quoted_reference_chat_id=chat,
                    quoted_content_unavailable=item["quote"].get("unavailable") is True,
                )
            contexts.append(context)
            if item["id"] != preferred:
                updated_mapping[item["id"]] = preferred
        updated_members[preferred] = sorted(
            contexts, key=lambda value: (value["id"] != preferred, value["id"])
        )
    return updated_mapping, updated_members
