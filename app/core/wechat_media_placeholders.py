"""Overlay audited HTML media events without reading or duplicating payloads.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat

from .wechat_archive import _file_identity, _identifier, _time_fields
from .wechat_html_export import (
    _local_reference,
    _native_avatar_id,
    normalize_html_clock,
)


VERSION = "v1.1.0-codex.0"
MAX_OVERLAY_BYTES = 4 * 1024**2
MAX_SCOPED_WITNESS_ROWS = 2_000
MAX_SCOPED_WITNESS_BYTES = 4 * 1024**2
_HEX = re.compile(r"[0-9a-f]{64}")
_TABLE = re.compile(r"(?:(?:main|temp)\.)?[A-Za-z_][A-Za-z0-9_]*")
_KINDS = {
    "3": ("image", "[Image]"),
    "image": ("image", "[Image]"),
    "43": ("video", "[Video]"),
    "62": ("video", "[Video]"),
    "video": ("video", "[Video]"),
    "34": ("audio", "[Voice message]"),
    "audio": ("audio", "[Voice message]"),
    "47": ("sticker", "[Sticker]"),
    "sticker": ("sticker", "[Sticker]"),
    "emoji": ("sticker", "[Sticker]"),
    "file": ("file", "[File]"),
}
_CONVERSATION_COLUMNS = (
    "id",
    "partition_id",
    "account_key",
    "peer_id",
    "title",
    "kind",
    "message_count",
    "first_sort",
    "last_sort",
    "first_time",
    "last_time",
    "latest_text",
)


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value):
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        raise ValueError("An exact source receipt digest is required.")
    return value


def _frame(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("node"), dict):
        raise ValueError("Media placeholder entries require an original audited node.")
    locator = entry["node"].get("source_locator")
    if not isinstance(locator, dict):
        raise ValueError("An exact original frame locator is required.")
    member = locator.get("member_path")
    if (
        not isinstance(member, str)
        or not member
        or len(member) > 4096
        or "\\" in member
        or "\0" in member
        or PurePosixPath(member).is_absolute()
        or any(part in {"", ".", ".."} for part in member.split("/"))
        or ":" in member
    ):
        raise ValueError("Media frame members must remain archive-relative paths.")
    values = {"member_path": member}
    for key, minimum, maximum in (
        ("page", 0, 10_000),
        ("message_ordinal", 1, 3_000_000),
        ("page_message_ordinal", 1, 3_000_000),
    ):
        value = locator.get(key)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError("Media frame positions must be exact bounded integers.")
        values[key] = value
    return values


def media_placeholder_id(entry):
    """Identify a source event independently of time, media availability, or local ID."""
    archive = _digest(entry.get("archive_sha256")) if isinstance(entry, dict) else ""
    source = entry.get("source_id") if isinstance(entry, dict) else None
    if (
        not isinstance(source, str)
        or re.fullmatch(r"html-" + archive[:24] + r"(?:-text-completion-v1)?", source)
        is None
    ):
        raise ValueError("Media placeholders require an exact published HTML source.")
    return _identifier(
        "wechat-html-media-placeholder-v1", source, archive, _json(_frame(entry))
    )


def _row(connection, sql, parameters=()):
    cursor = connection.execute(sql, parameters)
    value = cursor.fetchone()
    return (
        dict(zip((column[0] for column in cursor.description), value, strict=True))
        if value is not None
        else None
    )


def _read_overlay(path):
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("The media overlay must be a regular local file.")
    if getattr(before, "st_flags", 0) & getattr(stat, "UF_DATALESS", 0x40000000):
        return None
    if before.st_size > MAX_OVERLAY_BYTES:
        raise ValueError("The media overlay exceeds its metadata budget.")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if getattr(opened, "st_flags", 0) & getattr(stat, "UF_DATALESS", 0x40000000):
            return None
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("The media overlay changed before reading.")
        raw = stream.read(MAX_OVERLAY_BYTES + 1)
    after = path.lstat()
    if len(raw) > MAX_OVERLAY_BYTES or any(
        getattr(before, field, None) != getattr(after, field, None)
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    ):
        raise ValueError("The media overlay changed while reading.")
    value = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not isinstance(value.get("entries"), list)
        or len(value["entries"]) > 2048
    ):
        raise ValueError("Unsupported media placeholder metadata.")
    return value["entries"]


def media_placeholder_conversation(history_path, message_id):
    """Locate one bounded overlay entry without reading conversation message bodies."""
    path = Path(history_path).with_name("media_placeholders.json")
    if not path.exists():
        return None
    entries = _read_overlay(path)
    if entries is None:
        return None
    matches = set()
    for entry in entries:
        if media_placeholder_id(entry) == message_id:
            conversation = entry.get("conversation_id")
            if not isinstance(conversation, str) or not conversation:
                raise ValueError("A media placeholder needs an explicit conversation ID.")
            matches.add(conversation)
    if len(matches) > 1:
        raise ValueError("One media placeholder cannot identify several conversations.")
    return next(iter(matches), None)


class _ProjectionIncomplete(ValueError):
    """Stop a scoped proof before it can read an unbounded conversation."""


def _scope(connection, entry, cache, *, witness_budget=None):
    source = entry["source_id"]
    snapshot_key = entry.get("source_snapshot_id")
    conversation_id = entry.get("conversation_id")
    if (
        type(snapshot_key) is not int
        or not 1 <= snapshot_key <= 9223372036854775807
        or not isinstance(conversation_id, str)
    ):
        raise ValueError(
            "Media placeholders require an existing source and conversation."
        )
    key = (snapshot_key, source, entry["archive_sha256"], conversation_id)
    if key in cache:
        return cache[key]
    snapshot = _row(
        connection,
        "SELECT * FROM main.source_snapshots WHERE id=? AND source_id=?",
        (snapshot_key, source),
    )
    conversation = _row(
        connection, "SELECT * FROM main.conversations WHERE id=?", (conversation_id,)
    )
    partition = _row(connection, "SELECT * FROM main.partitions WHERE id=?", (source,))
    if snapshot is None or conversation is None or partition is None:
        raise ValueError("A media placeholder source is not published.")
    metadata = json.loads(snapshot["metadata"])
    owner = partition["account_id"]
    peer = conversation["peer_id"]
    evidence = metadata.get("owner_evidence", {}) if isinstance(metadata, dict) else {}
    if (
        snapshot["snapshot_id"] != entry["archive_sha256"]
        or not isinstance(metadata, dict)
        or metadata.get("source_id") != source
        or metadata.get("source_sha256") != entry["archive_sha256"]
        or not isinstance(owner, str)
        or not owner
        or metadata.get("source_account_id") != owner
        or metadata.get("owner_verified") is not True
        or not isinstance(evidence, dict)
        or evidence.get("native_username") != owner
        or partition["account_key"] != "account:" + owner
        or partition["identity_status"]
        not in {"Corroborated export ID", "Verified archive ID"}
        or conversation["partition_id"] != source
        or conversation["account_key"] != partition["account_key"]
        or not isinstance(peer, str)
        or not peer
    ):
        raise ValueError("A media placeholder crosses its verified source account.")
    witnesses = []
    sql = "SELECT id,provenance FROM main.messages WHERE conversation_id=?"
    parameters = [conversation_id]
    if witness_budget is not None:
        # LIMIT bounds visited source rows; the CASE also bounds one projected cell.
        sql = (
            "SELECT id,CASE WHEN length(CAST(provenance AS BLOB))<=? "
            "THEN provenance END FROM main.messages WHERE conversation_id=? LIMIT ?"
        )
        parameters = [
            MAX_SCOPED_WITNESS_BYTES,
            conversation_id,
            MAX_SCOPED_WITNESS_ROWS - witness_budget["rows"] + 1,
        ]
    for value in connection.execute(sql, parameters):
        if witness_budget is not None:
            witness_budget["rows"] += 1
            if value[1] is None:
                raise _ProjectionIncomplete("A media witness exceeds its byte budget.")
            witness_budget["bytes"] += len(value[1].encode("utf-8"))
            if (
                witness_budget["rows"] > MAX_SCOPED_WITNESS_ROWS
                or witness_budget["bytes"] > MAX_SCOPED_WITNESS_BYTES
            ):
                raise _ProjectionIncomplete("The media witness scope exceeds its proof budget.")
        provenance = json.loads(value[1])
        if (
            isinstance(provenance, dict)
            and provenance.get("source_snapshot") == snapshot_key
        ):
            locator = provenance.get("locator")
            if (
                isinstance(locator, dict)
                and locator.get("archive_sha256") == entry["archive_sha256"]
            ):
                witnesses.append(
                    {"id": value[0], "locator": locator, "provenance": provenance}
                )
    result = (metadata, conversation, owner, peer, witnesses)
    cache[key] = result
    return result


def _project(connection, entry, cache, *, witness_budget=None):
    identifier = media_placeholder_id(entry)
    frame = _frame(entry)
    member_digest = _digest(entry.get("member_sha256"))
    _digest(entry.get("audit_sha256"))
    node = entry["node"]
    metadata, conversation, owner, peer, witnesses = _scope(
        connection, entry, cache, witness_budget=witness_budget
    )
    catalog = metadata.get("source_conversation_catalog")
    if not isinstance(catalog, list):
        raise ValueError("Published source conversation evidence is unavailable.")
    matched = []
    for value in catalog:
        if not isinstance(value, dict):
            continue
        main = value.get("member_path")
        if not isinstance(main, str):
            continue
        declaration = value.get("declared_async_counts", {})
        if frame["page"] == 0:
            expected = main
        elif (
            isinstance(declaration, dict)
            and type(declaration.get("numberOfPages")) is int
            and frame["page"] <= declaration["numberOfPages"]
        ):
            expected = _local_reference(
                main, str(declaration.get("data_path", "")) + f"/msg-{frame['page']}.js"
            )
        else:
            continue
        if expected == frame["member_path"]:
            matched.append(value)
    if len(matched) != 1:
        raise ValueError("A media frame must belong to one exact source conversation.")
    original = matched[0]
    counts = original.get("counts", {})
    all_message_nodes = (
        counts.get("all_message_nodes") if isinstance(counts, dict) else None
    )
    resolution = original.get("reviewed_resolution", {})
    main_digest = (
        resolution.get("main_html_sha256") if isinstance(resolution, dict) else None
    )
    _digest(main_digest)
    if (
        original.get("index_avatar_native_id") != peer
        or original.get("conversation_kind") != conversation["kind"]
        or frame["page"] == 0
        and main_digest != member_digest
        or type(all_message_nodes) is not int
        or not 1 <= frame["message_ordinal"] <= all_message_nodes
    ):
        raise ValueError(
            "A media frame differs from its original reviewed peer or member."
        )
    witness = next(
        (
            value["locator"]
            for value in witnesses
            if value["provenance"].get("text_extraction_kind") != "resource_placeholder"
            and value["locator"].get("member_path") == frame["member_path"]
            and isinstance(value["locator"].get("member"), dict)
            and value["locator"]["member"].get("sha256") == member_digest
        ),
        None,
    )
    if witness is None:
        raise ValueError(
            "An existing published member receipt must corroborate the media frame."
        )
    flags = node.get("flags", [])
    classes = node.get("classes", [])
    direction = node.get("direction")
    sender = node.get("sender_native_id")
    kind = _KINDS.get(node.get("type")) if isinstance(node.get("type"), str) else None
    if (
        node.get("role") != "top_level"
        or node.get("status") != "deferred_top_level"
        or node.get("text_kind") != "deferred"
        or kind is None
        or direction not in {"sent", "received"}
        or not isinstance(classes, list)
        or "msg" not in classes
        or not ({"chat", "media"} & set(classes))
        or ("right" if direction == "sent" else "left") not in classes
        or ("left" if direction == "sent" else "right") in classes
        or not isinstance(sender, str)
        or not sender
        or not isinstance(node.get("avatar_path"), str)
        or _native_avatar_id(node.get("avatar_path", "")) != sender
        or direction == "sent"
        and sender != owner
        or direction == "received"
        and conversation["kind"] == "direct"
        and sender != peer
        or not isinstance(flags, list)
        or any(not isinstance(flag, str) for flag in flags)
        or not isinstance(node.get("raw_timestamp"), str)
        or not isinstance(node.get("sender_label", ""), str)
        or not isinstance(node.get("export_msgid"), str)
    ):
        raise ValueError(
            "Only explicit original media nodes with verified authors can be projected."
        )
    stamp, precision = normalize_html_clock(node["raw_timestamp"])
    ordering, timestamp, label, basis = _time_fields(
        {"sent_at_raw": stamp, "timeline_timestamp_usable": bool(stamp)}
    )
    flags = list(
        dict.fromkeys(
            [
                *flags,
                "resource_placeholder",
                "media_payload_not_read",
                "html_media_node_audit_placeholder",
            ]
        )
    )
    if precision == "minute" and "export_timestamp_precision_minutes" not in flags:
        flags.append("export_timestamp_precision_minutes")
    locator = {
        **frame,
        "archive_sha256": entry["archive_sha256"],
        "member": witness["member"],
        "export_msgid": node["export_msgid"],
        "export_msgid_namespace": "MesLocalID",
        "export_msgtype": node["type"],
    }
    reference = {
        "id": identifier,
        "kind": kind[0],
        "availability": "metadata_only",
        "source_id": entry["source_id"],
        "source_snapshot_id": entry["source_snapshot_id"],
        "account_id": owner,
        "peer_id": peer,
        "archive_sha256": entry["archive_sha256"],
        "member_sha256": member_digest,
        "audit_sha256": entry["audit_sha256"],
        "frame": frame,
        "export_msgid": node["export_msgid"],
        "export_msgid_namespace": "MesLocalID",
    }
    for value in witnesses:
        if not all(value["locator"].get(key) == frame[key] for key in frame):
            continue
        existing = value["provenance"]
        if (
            value["id"] != identifier
            or existing.get("text_extraction_kind") != "resource_placeholder"
            or existing.get("media_reference") != reference
            or existing.get("original_node") != node
        ):
            raise ValueError("A retained frame conflicts with its audited media event.")
    provenance = {
        "record_id": identifier,
        "source_snapshot": entry["source_snapshot_id"],
        "source_time": node["raw_timestamp"],
        "source_clock_precision": precision or "unknown",
        "locator": locator,
        "export_msgid_namespace": "MesLocalID",
        "text_extraction_kind": "resource_placeholder",
        "media_reference": reference,
        "audit_receipt_basis": "preserved_source_artifact_digest; no_payload_read",
        "original_node": node,
    }
    return (
        identifier,
        conversation["id"],
        frame["message_ordinal"],
        sender,
        node.get("sender_label") or sender,
        direction,
        kind[1],
        ordering,
        timestamp,
        label,
        basis,
        _json(provenance),
        _json(flags),
        (kind[1] + "\n" + node.get("sender_label", "")).casefold(),
    )


def attach_media_placeholders(
    connection, history_path, conversation_table, message_table, *, conversation_id=None
):
    """Attach a bounded optional overlay while leaving every persistent file untouched."""
    default = (conversation_table, message_table)
    path = Path(history_path).with_name("media_placeholders.json")
    if not path.exists():
        return *default, "absent"
    try:
        if any(
            not isinstance(value, str) or _TABLE.fullmatch(value) is None
            for value in default
        ):
            raise ValueError("Unsupported media placeholder query surface.")
        identity = _file_identity(Path(history_path))
        entries = _read_overlay(path)
        if entries is None:
            return *default, "absent"
        witness_budget = None
        if conversation_id is not None:
            if not isinstance(conversation_id, str) or not conversation_id:
                raise ValueError("A scoped media projection needs a conversation ID.")
            if any(
                not isinstance(entry, dict)
                or not isinstance(entry.get("conversation_id"), str)
                for entry in entries
            ):
                raise ValueError("Media overlay entries need an explicit conversation ID.")
            entries = [
                entry for entry in entries if entry["conversation_id"] == conversation_id
            ]
            if not entries:
                return *default, "absent"
            witness_budget = {"rows": 0, "bytes": 0}
        cache, messages = {}, {}
        for entry in entries:
            row = _project(connection, entry, cache, witness_budget=witness_budget)
            if row[0] in messages and messages[row[0]] != row:
                raise ValueError("One audited media frame has conflicting metadata.")
            messages[row[0]] = row
        rows = [
            row
            for row in messages.values()
            if connection.execute(
                f"SELECT 1 FROM {message_table} WHERE id=? LIMIT 1", (row[0],)
            ).fetchone()
            is None
        ]
        if _file_identity(Path(history_path)) != identity:
            return *default, "invalid"
        connection.execute(
            "CREATE TEMP TABLE media_placeholder_messages AS SELECT * FROM main.messages WHERE 0"
        )
        connection.executemany(
            "INSERT INTO media_placeholder_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        connection.execute(
            f"CREATE TEMP VIEW placeholder_timeline_messages AS SELECT * FROM {message_table} UNION ALL SELECT p.* FROM temp.media_placeholder_messages p WHERE NOT EXISTS(SELECT 1 FROM {message_table} b WHERE b.id=p.id)"
        )
        connection.execute(
            f"CREATE TEMP TABLE media_placeholder_conversations AS SELECT * FROM {conversation_table} WHERE 0"
        )
        for conversation_id in sorted({row[1] for row in rows}):
            conversation = _row(
                connection,
                f"SELECT * FROM {conversation_table} WHERE id=?",
                (conversation_id,),
            )
            if conversation is None:
                raise ValueError("Media placeholders cannot create a new conversation.")
            count = connection.execute(
                "SELECT COUNT(*) FROM placeholder_timeline_messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            first = _row(
                connection,
                "SELECT time_sort,timestamp_value,text FROM placeholder_timeline_messages WHERE conversation_id=? ORDER BY (time_sort=0),time_sort,ordinal,id LIMIT 1",
                (conversation_id,),
            )
            last = _row(
                connection,
                "SELECT time_sort,timestamp_value,text FROM placeholder_timeline_messages WHERE conversation_id=? ORDER BY (time_sort=0),time_sort DESC,ordinal DESC,id DESC LIMIT 1",
                (conversation_id,),
            )
            conversation.update(
                message_count=count,
                first_sort=first["time_sort"],
                first_time=first["timestamp_value"],
                last_sort=last["time_sort"],
                last_time=last["timestamp_value"],
                latest_text=last["text"][:300],
            )
            connection.execute(
                "INSERT INTO media_placeholder_conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(conversation[key] for key in _CONVERSATION_COLUMNS),
            )
        connection.execute(
            f"CREATE TEMP VIEW placeholder_timeline_conversations AS SELECT c.* FROM {conversation_table} c WHERE NOT EXISTS(SELECT 1 FROM temp.media_placeholder_conversations p WHERE p.id=c.id) UNION ALL SELECT * FROM temp.media_placeholder_conversations"
        )
        if _file_identity(Path(history_path)) != identity:
            return *default, "invalid"
        return (
            "placeholder_timeline_conversations",
            "placeholder_timeline_messages",
            "ready",
        )
    except _ProjectionIncomplete:
        return *default, "incomplete"
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, RecursionError):
        return *default, "invalid"
