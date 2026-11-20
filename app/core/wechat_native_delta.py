"""Compare complete native Chat tables without guessing from clocks or rowids.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import PurePosixPath
import re
import sqlite3


_CHAT = re.compile(r"Chat_[0-9a-f]{32}")
_PREFIX = re.compile(r"^([A-Za-z0-9_.@-]{1,256}):(\r?\n)")


@dataclass(frozen=True)
class NativeRowDelta:
    kind: str
    key: tuple
    before: dict | None
    after: dict | None


def table_layout(connection: sqlite3.Connection, table: str):
    """Require an ordinary Chat table with a declared, nonnullable primary key."""
    if not _CHAT.fullmatch(table):
        raise ValueError("An exact native Chat table name is required.")
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None or "VIRTUAL" in str(row[0]).upper():
        raise ValueError("Native delta input must be an ordinary table.")
    schema = list(connection.execute(f'PRAGMA table_info("{table}")'))
    columns = tuple(row[1] for row in schema)
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c) for c in columns):
        raise ValueError("A native column cannot be quoted safely.")
    keys = tuple(row[1] for row in sorted(schema, key=lambda r: r[5]) if row[5])
    if not keys:
        raise ValueError("A native delta requires a declared primary key.")
    nulls = " OR ".join(f'"{key}" IS NULL' for key in keys)
    if connection.execute(f'SELECT 1 FROM "{table}" WHERE {nulls} LIMIT 1').fetchone():
        raise ValueError("A native primary key contains NULL.")
    return columns, keys


def _typed(values):
    return tuple((type(value).__name__, value) for value in values)


def iter_native_row_deltas(old, new, table: str):
    """Merge full primary-key streams, retaining edits and deletions explicitly.

    The caller proves complete database hashes and immutable source identities.
    This function does not write either database, infer a recent-time boundary,
    interpret a body, or treat an internal rowid change as a new observation.
    """
    columns, keys = table_layout(new, table)
    if old is not None and table_layout(old, table) != (columns, keys):
        raise ValueError("A native table changed columns or primary-key layout.")
    select = ",".join(f'"{name}"' for name in columns)
    order = ",".join(f'"{name}"' for name in keys)
    query = f'SELECT rowid,{select} FROM "{table}" ORDER BY {order}'
    indexes = tuple(columns.index(key) for key in keys)

    def rows(connection):
        if connection is None:
            return
        cursor = connection.execute(query)
        try:
            for row in cursor:
                yield tuple(row[index + 1] for index in indexes), row
        finally:
            cursor.close()

    def values(row):
        return {"source_rowid": row[0], **dict(zip(columns, row[1:], strict=True))}

    left, right = iter(rows(old)), iter(rows(new))
    before, after = next(left, None), next(right, None)
    while before is not None or after is not None:
        if before is None or (after is not None and after[0] < before[0]):
            yield NativeRowDelta("inserted", after[0], None, values(after[1]))
            after = next(right, None)
        elif after is None or before[0] < after[0]:
            yield NativeRowDelta("deleted", before[0], values(before[1]), None)
            before = next(left, None)
        else:
            if _typed(before[1][1:]) != _typed(after[1][1:]):
                yield NativeRowDelta(
                    "changed", after[0], values(before[1]), values(after[1])
                )
            before, after = next(left, None), next(right, None)


def project_native_text(row, *, source, database, table, peer, kind):
    """Apply the current text projector to one fully retained native observation."""
    from . import wechat_reconcile, wechat_text, wechat_wcdb

    if not _CHAT.fullmatch(table) or kind not in {"direct", "group"}:
        raise ValueError("Text projection requires an exact proved native peer.")
    if hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest() != table[5:]:
        raise ValueError("The native peer does not reproduce its Chat table hash.")
    owner = source["source_account_id"]
    account_hash = hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    if source["source_id"] != "ios-" + account_hash or not source["account_hash_verified"]:
        raise ValueError("A projected source lost its native owner proof.")
    message_type = row["Type"]
    if message_type not in {1, 49, 10000, 10002}:
        raise ValueError("This native type is metadata-only in the text projector.")
    raw = row["Message"]
    blob = raw.encode() if isinstance(raw, str) else bytes(raw)
    decoded = wechat_wcdb.decode_wcdb_text(raw, row.get("WCDB_CT_Message") or 0)
    direction = {0: "sent", 1: "received"}.get(row["Des"], "unknown")
    sender = owner if direction == "sent" else peer if direction == "received" and kind == "direct" else ""
    native = {key: value for key, value in row.items() if key not in {"Message", "source_rowid"}}
    locator = {"database": PurePosixPath(database["path"]).name,
               "database_relative_path": database["path"], "database_sha256": database["sha256"],
               "file_id": database["file_id"], "table": table, "row_id": row["source_rowid"],
               "local_id": row["MesLocalID"], "native_direction": row["Des"],
               "recorded_native_metadata": native, "native_message_sha256": hashlib.sha256(blob).hexdigest(),
               "native_sqlite_storage_class": "text" if isinstance(raw, str) else "blob",
               "peer_identity_basis": "same_verified_native_partition_exact_peer_identifier_and_Chat_MD5"}
    payload, flags = decoded, ["native_ipad_checkpointed_main"]
    if message_type in {10000, 10002}:
        sender = ""
    elif kind == "group" and direction == "received":
        prefix = _PREFIX.match(decoded)
        if prefix and not prefix[1].endswith(("@chatroom", "@im.chatroom")):
            sender, payload = prefix[1], decoded[prefix.end():]
            locator.update(native_sender_prefix=sender, native_sender_prefix_literal=decoded[:prefix.end()],
                           native_sender_prefix_matches_owner=sender == owner,
                           sender_prefix_identity_status="native_message_prefix")
        else:
            flags.append("native_group_sender_prefix_unavailable")
    projected = wechat_text.extract_wechat_text(message_type, payload)
    if projected is None:
        raise ValueError("The current text projector cannot classify the native payload.")
    fields = (*wechat_reconcile._TEXT_METADATA_FIELDS, "forwarded_original_roles", "original_roles",
              "system_message_subtype", "system_source_ids")
    locator.update({key: projected[key] for key in fields if key in projected})
    epoch = row["CreateTime"]
    if type(epoch) is not int:
        raise ValueError("A native clock must retain its exact integer epoch.")
    if message_type == 1 and projected["text"] != payload:
        raise ValueError("The text projector changed literal Type 1 whitespace.")
    if message_type in {10000, 10002} and projected.get("text_extraction_kind") != "system_text":
        raise ValueError("A native system notice lost its source role.")
    identifier = hashlib.sha256("\0".join((source["snapshot_id"], database["file_id"],
        database["sha256"], table, str(row["source_rowid"]))).encode()).hexdigest()
    return {**projected, "source_id": source["source_id"], "source_account_id": owner,
            "account_partition": source["source_id"], "extraction_record_id": identifier,
            "conversation_hash": table[5:], "conversation_id": peer, "conversation_kind": kind,
            "conversation_display_name": peer, "sender_id": sender,
            "sender_display_name": "System Message" if message_type in {10000, 10002} else sender or "Unknown sender",
            "direction": direction, "unix_timestamp": epoch, "sent_at_raw": str(epoch),
            "sent_at_utc_iso": datetime.fromtimestamp(epoch, UTC).isoformat() if epoch > 1 else None,
            "timeline_timestamp_usable": epoch > 1, "message_type": message_type,
            "server_id": str(row["MesSvrID"]) if row.get("MesSvrID") is not None else None,
            "source_locator": locator, "flags": list(dict.fromkeys(projected.get("flags", []) + flags))}


def prove_preserved_tables(before, after, *, mutable_statistics=False, allowed_info=(), progress=None):
    """Independently join every old row by its declared logical primary key."""
    connection = sqlite3.connect(before.as_uri() + "?mode=ro&immutable=1", uri=True)
    result = {}
    try:
        connection.execute("ATTACH DATABASE ? AS candidate", (after.as_uri() + "?mode=ro&immutable=1",))
        schemas = {}
        for database in ("main", "candidate"):
            schemas[database] = list(connection.execute(
                f"SELECT type,name,tbl_name,sql FROM {database}.sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ))
        if schemas["main"] != schemas["candidate"]:
            raise ValueError("The candidate changed the retained schema.")
        for kind, table, _, sql in schemas["main"]:
            if kind != "table" or table == "archive_info":
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                raise ValueError("Unsafe retained table identifier.")
            layout = list(connection.execute(f'PRAGMA main.table_info("{table}")'))
            names = [row[1] for row in layout]
            fields = names[:6] if table == "conversations" and mutable_statistics else names
            keys = [row[1] for row in sorted(layout, key=lambda row: row[5]) if row[5]]
            if keys:
                null = " OR ".join(f'"{key}" IS NULL' for key in keys)
                for database in ("main", "candidate"):
                    if connection.execute(f'SELECT 1 FROM {database}."{table}" WHERE {null} LIMIT 1').fetchone():
                        raise ValueError("NULL retained logical key.")
                join = " AND ".join(f'n."{key}" IS o."{key}"' for key in keys)
                exists = f'n."{keys[0]}" IS NOT NULL'
            else:
                join, exists = "n.rowid=o.rowid", "n.rowid IS NOT NULL"
            order = ",".join(f'o."{key}"' for key in keys) if "WITHOUT ROWID" in str(sql).upper() else "o.rowid"
            selected = ",".join([f'o."{name}"' for name in fields] + [f'n."{name}"' for name in fields])
            count, fingerprint = 0, hashlib.sha256()
            if progress:
                progress({"event": "old_table_proof_started", "table": table})
            for row in connection.execute(f'SELECT {exists},{selected} FROM main."{table}" o LEFT JOIN candidate."{table}" n ON {join} ORDER BY {order}'):
                size = len(fields)
                if not row[0] or _typed(row[1:1 + size]) != _typed(row[1 + size:]):
                    raise ValueError("A previously retained row disappeared or changed: " + table)
                cells = [{"blob_hex": value.hex()} if isinstance(value, bytes) else value for value in row[1:1 + size]]
                fingerprint.update((json.dumps(cells, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
                count += 1
                if progress and count % 100_000 == 0:
                    progress({"event": "old_table_proof_progress", "table": table, "rows": count})
            result[table] = {"old_rows_verified": count, "sha256": fingerprint.hexdigest(), "key_columns": keys}
        if any(kind == "table" and table == "archive_info" for kind, table, _, _ in schemas["main"]):
            old = dict(connection.execute("SELECT key,value FROM main.archive_info"))
            new = dict(connection.execute("SELECT key,value FROM candidate.archive_info"))
            if {k: v for k, v in old.items() if k not in allowed_info} != {k: v for k, v in new.items() if k not in allowed_info}:
                raise ValueError("Protected archive metadata changed.")
        return result
    finally:
        connection.close()
