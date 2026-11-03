"""Reference literal native authors without copying text or inventing conversations.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from threading import Lock

from .wechat_forwarded import forwarded_component_clock, project_native_forwarded
from .wechat_reconcile import native_text_metadata


VERSION = "v1.2.0-codex.0"
CANDIDATE_INDEX_VERSION = "v1.1.0-codex.0"
_LOCK = Lock()


def _identity(path):
    value = path.stat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(value.st_mode)
        or getattr(value, "st_flags", 0) & 0x40000000
    ):
        raise ValueError("Native author inputs must be local regular files.")
    if any(
        Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal", "-shm")
    ):
        raise ValueError("Native author inputs have an unpublished database journal.")
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _readonly(path):
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _authors(value):
    pending, count = [value], 0
    while pending:
        node = pending.pop()
        count += 1
        if count > 20_000:
            raise ValueError("Native author metadata exceeds its supported bounds.")
        if isinstance(node, dict):
            sender = node.get("source_sender_id")
            if isinstance(sender, str) and sender:
                yield sender
                # Reversed quote roles are hints until the detail proves its room.
                if sender.endswith("@chatroom"):
                    roles = native_quote_roles(node, peer_id=sender)
                    if roles:
                        yield roles[0]
            pending.extend(
                item for item in node.values() if isinstance(item, (dict, list))
            )
        elif isinstance(node, list):
            pending.extend(item for item in node if isinstance(item, (dict, list)))


def native_candidate_ids(history, directory, username):
    """Share a compact author/parent hint index; every result still requires proof.

    The index is not an identity authority. It contains no bodies, contact names,
    clocks, media, or inferred original conversations.
    """
    history, directory = Path(history), Path(directory)
    initial = _identity(history)
    fingerprint = hashlib.sha256(
        json.dumps([CANDIDATE_INDEX_VERSION, str(history.resolve()), initial]).encode()
    ).hexdigest()
    target = directory / "native-author-candidates.sqlite3"
    if target.is_symlink():
        raise ValueError("The native author cache cannot be a symbolic link.")

    def ready():
        if not target.is_file():
            return False
        try:
            _identity(target)
            with closing(_readonly(target)) as connection:
                return (
                    connection.execute(
                        "SELECT value FROM info WHERE key='fingerprint'"
                    ).fetchone()[0]
                    == fingerprint
                )
        except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
            return False

    with _LOCK:
        if not ready():
            if directory.is_symlink():
                raise ValueError("The native author cache cannot use a symbolic link.")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, name = tempfile.mkstemp(
                prefix=".native-authors-", suffix=".sqlite3", dir=directory
            )
            os.close(descriptor)
            temporary = Path(name)
            try:
                with (
                    closing(_readonly(history)) as source,
                    closing(sqlite3.connect(temporary)) as output,
                ):
                    output.executescript(
                        "CREATE TABLE candidates(author TEXT,parent_id TEXT,PRIMARY KEY(author,parent_id)) WITHOUT ROWID; CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID;"
                    )
                    cursor = source.execute(
                        "SELECT id,provenance FROM messages WHERE instr(provenance,'text_components')>0"
                    )
                    invalid = 0
                    for row in cursor:
                        try:
                            provenance = json.loads(row["provenance"])
                        except (ValueError, TypeError, RecursionError):
                            invalid += 1
                            continue
                        if not isinstance(provenance, dict):
                            invalid += 1
                            continue
                        if provenance.get("message_type") != 49 or provenance.get(
                            "text_extraction_kind"
                        ) not in {"forwarded_text", "quoted_reply"}:
                            continue
                        try:
                            authors = set(_authors(provenance))
                        except ValueError:
                            invalid += 1
                            continue
                        output.executemany(
                            "INSERT OR IGNORE INTO candidates VALUES(?,?)",
                            [(author, row["id"]) for author in authors],
                        )
                    output.execute(
                        "INSERT INTO info VALUES('fingerprint',?)", (fingerprint,)
                    )
                    output.execute(
                        "INSERT INTO info VALUES('invalid_rows',?)", (str(invalid),)
                    )
                    output.commit()
                if _identity(history) != initial:
                    raise ValueError("Native author inputs changed while indexing.")
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
    with closing(_readonly(target)) as connection:
        for row in connection.execute(
            "SELECT parent_id FROM candidates WHERE author=? ORDER BY parent_id",
            (username,),
        ):
            yield row[0]
    if _identity(history) != initial:
        raise ValueError("Native author inputs changed while reading.")


def _forwarded_items(record, path=()):
    for number, child in enumerate(record["items"], 1):
        position = (*path, number)
        yield child, position
        nested = child.messaging.get("forwarded_record")
        if nested:
            yield from _forwarded_items(nested, position)
        quoted = child.messaging.get("quoted_reply")
        quoted_record = (
            quoted.get("forwarded_record") if isinstance(quoted, dict) else None
        )
        if quoted_record:
            yield from _forwarded_items(quoted_record, (*position, "quote"))


def _path_key(parent_id, position):
    return parent_id + "".join(
        "_quote" if part == "quote" else "_forwarded_" + str(part) for part in position
    )


def native_quote_roles(reference, *, peer_id=""):
    """Resolve native quote roles without changing their original source fields.

    A room-valued sender requires an independently supplied matching room and
    a native internal author ID. Ordinary native usernames keep their assertions.
    """
    if not isinstance(reference, dict):
        return None
    sender = reference.get("source_sender_id", "")
    chat = reference.get("source_chat_id", "")
    if not isinstance(sender, str) or not sender or not isinstance(chat, str):
        return None
    if sender.endswith("@chatroom"):
        if (
            not isinstance(peer_id, str)
            or sender != peer_id
            or re.fullmatch(r"[1-9][0-9]*@chatroom", sender) is None
            or re.fullmatch(r"wxid_[A-Za-z0-9_]{1,100}", chat) is None
        ):
            return None
        return chat, sender
    return sender, chat


def _quote_detail(component, *, parent_id, number, position=(), peer_id=""):
    original_type = component.get("original_message_type")
    if original_type is not None and (
        type(original_type) is not int or original_type != 1
    ):
        return None
    roles = native_quote_roles(component, peer_id=peer_id)
    if roles is None:
        return None
    sender, chat = roles
    if (
        component.get("kind") != "quoted_text"
        or not component.get("source_sender_id")
        or not component.get("text", "").strip()
    ):
        return None
    clock = component.get("source_timestamp", "")
    timestamp, label, basis, flags = forwarded_component_clock(
        {"source_create_time": clock, "source_timestamp": clock}, native=True
    )
    anchor = _path_key(parent_id, position)
    return {
        "path": ["native_quote", *position, number] if position else ["quote", number],
        "id": anchor + "_quoted_" + str(number),
        "anchor_id": "wechat_message_" + anchor,
        "text": component["text"],
        "sender_id": sender,
        "sender_label": component.get("source_sender_label", ""),
        "direction": "unknown",
        "timestamp_value": timestamp,
        "timestamp_label": label,
        "time_basis": basis,
        "source_clock": clock,
        "quality_flags": list(flags),
        "source_ids": component.get("source_ids", {}),
        "original_message_type": original_type,
        "original_chat_id": chat,
        "reference_kind": "quoted_text",
        "original_conversation_unknown": not bool(chat),
    }


def native_details(provenance, *, parent_id, account_id, peer_id=""):
    """Expose source assertions, keeping quotes and forwarded authors distinct."""
    if (
        not isinstance(provenance, dict)
        or not isinstance(parent_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", parent_id) is None
        or not isinstance(account_id, str)
        or not account_id
    ):
        return
    if (
        type(provenance.get("message_type")) is not int
        or provenance["message_type"] != 49
    ):
        return
    try:
        metadata = native_text_metadata(provenance, provenance.get("locator", {}))
        if provenance.get("text_extraction_kind") == "quoted_reply" and metadata.get(
            "appmsg_subtype"
        ) not in {None, 57}:
            return
        record = project_native_forwarded(
            provenance, parent_id=parent_id, account_id=account_id
        )
    except (ValueError, TypeError, KeyError, RecursionError):
        return
    if record:
        initial = (
            ("quote",)
            if provenance.get("text_extraction_kind") == "quoted_reply"
            else ()
        )
        for child, position in _forwarded_items(record, initial):
            context = child.messaging
            component = context["provenance"].get("embedded_forwarded_component", {})
            kind = component.get("kind")
            if kind == "forwarded_text":
                text = component["text"]
            elif kind == "forwarded_quote":
                for number, part in enumerate(component.get("text_components", []), 1):
                    quoted = _quote_detail(
                        part,
                        parent_id=parent_id,
                        number=number,
                        position=position,
                        peer_id=component.get("source_chat_id", ""),
                    )
                    if quoted:
                        yield quoted
                replies = [
                    part["text"]
                    for part in component.get("text_components", [])
                    if part.get("kind") == "reply_text"
                ]
                text = replies[0] if len(replies) == 1 else ""
            else:
                continue
            if not text.strip() or not component.get("source_sender_id"):
                continue
            yield {
                "path": ["native", *position],
                "id": _path_key(parent_id, position),
                "anchor_id": "wechat_message_" + _path_key(parent_id, position),
                "text": text,
                "sender_id": component["source_sender_id"],
                "sender_label": child.author_label,
                "direction": context["direction"],
                "timestamp_value": child.last_seen_at,
                "timestamp_label": context["timestamp_label"],
                "time_basis": context["time_basis"],
                "source_clock": context["provenance"].get("source_time", ""),
                "quality_flags": list(context["quality_flags"]),
                "source_ids": component.get("source_ids", {}),
                "source_data_id": component.get("source_data_id", ""),
                "original_message_type": 1 if kind == "forwarded_text" else 49,
                "original_chat_id": component.get("source_chat_id", ""),
                "reference_kind": "native_forwarded",
                "original_conversation_unknown": not bool(
                    component.get("source_chat_id")
                ),
            }
    if provenance.get("text_extraction_kind") != "quoted_reply":
        return
    for number, component in enumerate(metadata.get("text_components", []), 1):
        detail = _quote_detail(
            component, parent_id=parent_id, number=number, peer_id=peer_id
        )
        if detail:
            yield detail


def native_detail(provenance, path, *, parent_id, account_id, peer_id=""):
    if (
        not isinstance(path, list)
        or len(path) < 2
        or not isinstance(path[0], str)
        or path[0] not in {"native", "native_quote", "quote"}
        or path[-1] == "quote"
        or any(
            (type(part) is not int or part < 1) and part != "quote" for part in path[1:]
        )
        or any(left == right == "quote" for left, right in zip(path[1:], path[2:]))
        or (path[0] == "quote" and (len(path) != 2 or type(path[1]) is not int))
    ):
        return None
    return next(
        (
            item
            for item in native_details(
                provenance,
                parent_id=parent_id,
                account_id=account_id,
                peer_id=peer_id,
            )
            if item["path"] == path
        ),
        None,
    )
