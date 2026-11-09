"""Project only an authenticated requested peer into the existing Mac bank contract.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
import sqlite3

from . import wechat_macos_recovery_bank as bank
from .wechat_archive import WechatArchiveRepository, _file_identity, wechat_archive_path
from .wechat_mac_refresh_source import MAX_NAMES, RefreshError
from .wechat_resource_index import _KINDS, _LABELS
from .wechat_text import extract_wechat_text
from .wechat_wcdb import WCDBBackendUnavailableError, WCDBDecodeError, decode_wcdb_text


_SESSION = re.compile(r"wechat:[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SOURCE_BINDING_FIELDS = (
    "source_id",
    "snapshot_id",
    "main_sha256",
    "wal_sha256",
    "database_relative_path",
)
MAX_ARCHIVE_ANCHOR_ROWS = 1_000_000
MAX_NATIVE_TABLES = 10_000


@dataclass(frozen=True)
class ConversationScope:
    session_id: str
    account_id: str
    peer_id: str
    kind: str


def resolve_scope(local_store_root, session_id):
    if not isinstance(session_id, str) or _SESSION.fullmatch(session_id) is None:
        raise RefreshError("Select an existing private or group conversation.")
    path = wechat_archive_path(local_store_root)
    bank._identity(path)
    with sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    ) as history:
        row = history.execute(
            "SELECT c.account_key,c.peer_id,c.kind,p.account_id,p.account_key "
            "FROM conversations c JOIN partitions p ON p.id=c.partition_id WHERE c.id=?",
            (session_id,),
        ).fetchone()
    if (
        row is None
        or row[2] not in {"direct", "group"}
        or not isinstance(row[1], str)
        or not isinstance(row[3], str)
        or row[0] != "account:" + row[3]
        or row[4] != row[0]
        or row[1].startswith("gh_")
        or (row[2] == "group") != row[1].endswith(("@chatroom", "@im.chatroom"))
    ):
        raise RefreshError(
            "Only a verified private or group conversation can be refreshed."
        )
    return ConversationScope(session_id, row[3], row[1], row[2])


def _table(peer):
    return "Msg_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()


def _name_proof(row):
    return {"row": row, "row_sha256": bank._sha(row)}


def _comparison_signature(row):
    proof = json.loads(row["provenance"])
    # A non-app event has no app subtype. Old source adapters may omit the
    # optional field while the current text parser explicitly returns None.
    if (
        proof.get("message_type") in {1, 10000, 10002}
        and proof.get("appmsg_subtype") is None
    ):
        proof.pop("appmsg_subtype", None)
    return bank._event_signature({**dict(row), "provenance": json.dumps(proof)})


def _native_rows(connection, table, *, local_id=None):
    layout = list(connection.execute('PRAGMA table_info("' + table + '")'))
    columns = [row[1] for row in layout]
    if local_id is not None and not any(
        row[1] == "local_id" and row[5] for row in layout
    ):
        raise RefreshError("A native witness requires an indexed local primary key.")
    required = {
        "local_id",
        "server_id",
        "local_type",
        "create_time",
        "real_sender_id",
        "message_content",
    }
    marker = next(
        (name for name in columns if name.casefold() == "wcdb_ct_message_content"), None
    )
    if (
        not required.issubset(columns)
        or marker is None
        or _IDENTIFIER.fullmatch(marker) is None
    ):
        raise RefreshError("A native message table has an unsupported schema.")
    condition = " WHERE local_id=?" if local_id is not None else ""
    return connection.execute(
        'SELECT local_id,server_id,local_type,create_time,real_sender_id,message_content,"'
        + marker
        + '" AS compression_marker FROM "'
        + table
        + '"'
        + condition
        + " ORDER BY local_id",
        (local_id,) if local_id is not None else (),
    )


def _server(value):
    value = str(value)
    if re.fullmatch(r"[1-9][0-9]{0,18}", value) is None or int(value) > 2**63 - 1:
        raise RefreshError("A native event has no supported server identifier.")
    return value


def _project_message(row, owner, kind, names):
    typ = row["local_type"] & 0xFFFFFFFF
    sender_row = names.get(row["real_sender_id"])
    sender = sender_row["user_name"] if sender_row else ""
    if typ not in {10000, 10002} and not sender:
        raise RefreshError("A native message has no authenticated sender.")
    raw = row["message_content"]
    if not isinstance(raw, (str, bytes)):
        raise RefreshError("A native message body has an unsupported storage class.")
    message = {
        "local_id": row["local_id"],
        "local_type": row["local_type"],
        "server_id": _server(row["server_id"]),
        "create_time": row["create_time"],
        "sender_id": sender,
        "sender_label": sender,
        "direction": "sent" if sender == owner else "received",
        "real_sender_id": row["real_sender_id"],
        "content_sha256": hashlib.sha256(
            raw.encode() if isinstance(raw, str) else raw
        ).hexdigest(),
    }
    if typ in {10000, 10002}:
        message.update(sender_id="", sender_label="System Message", direction="unknown")
    elif sender_row:
        message["sender_proof"] = _name_proof(sender_row)
    projection = None
    if typ in {1, 49, 10000, 10002}:
        decoded = decode_wcdb_text(raw, row["compression_marker"])
        if kind == "group" and sender and sender != owner:
            for prefix in (sender + ":\r\n", sender + ":\n"):
                if decoded.startswith(prefix):
                    decoded = decoded[len(prefix) :]
                    break
        projection = extract_wechat_text(typ, decoded)
    if projection is not None:
        message.update(text=projection["text"], native_text=projection)
    else:
        if typ in {1, 10000, 10002}:
            raise RefreshError("The native text cannot be projected faithfully.")
        resource_kind = _KINDS.get(typ, "unknown")
        label = _LABELS[resource_kind]
        message.update(
            text=label,
            resource={
                "kind": resource_kind,
                "label": label,
                "native_time": row["create_time"],
            },
        )
        if typ == 49:
            message["resource"]["app_subtype"] = str(row["local_type"] >> 32)
    return message


def _archive_anchor(archived, row, message, peer, peer_row):
    proof = json.loads(archived["provenance"])
    try:
        stamp = datetime.fromisoformat(archived["timestamp_value"])
    except (ValueError, TypeError):
        return None
    if (
        row["local_type"] != 1
        or proof.get("message_type") != 1
        or str(proof.get("server_id")) != message["server_id"]
        or proof.get("unix_timestamp") != row["create_time"]
        or archived["text"] != message["text"]
        or archived["sender_id"] != message["sender_id"]
        or archived["direction"] != message["direction"]
        or stamp.tzinfo is None
        or stamp.timestamp() != row["create_time"]
    ):
        return None
    return {
        "archive_message_id": archived["id"],
        "peer_id": peer,
        "peer_proof": _name_proof(peer_row),
        "server_id": message["server_id"],
        "message_type": 1,
        "create_time": row["create_time"],
        "direction": message["direction"],
        "sender_id": message["sender_id"],
        "text_sha256": hashlib.sha256(message["text"].encode()).hexdigest(),
        "table": _table(peer),
        "local_id": row["local_id"],
        "content_sha256": message["content_sha256"],
        "real_sender_id": row["real_sender_id"],
        "sender_proof": message["sender_proof"],
    }


def _stored_witnesses(local_store_root, scope, source, directory, check_cancel):
    """Reuse at most 32 known coordinates; never search unrelated chat bodies."""
    path = bank.mac_recovery_bank_path(local_store_root)
    if not path.exists():
        return []
    identity = bank._identity(path)
    selected = {}
    with sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    ) as stored:
        operations = 0

        def bounded_query():
            nonlocal operations
            check_cancel()
            operations += 1
            return int(operations > 100)

        stored.set_progress_handler(bounded_query, 1_000)
        queries = (
            (
                "SELECT proof FROM conversation_proofs WHERE conversation_id=? LIMIT 16",
                (scope.session_id,),
            ),
            (
                "SELECT proof FROM sources WHERE account_id=? LIMIT 32",
                (scope.account_id,),
            ),
        )
        for query, parameters in queries:
            for (encoded,) in stored.execute(query, parameters):
                check_cancel()
                if not isinstance(encoded, str) or len(encoded.encode()) > 1024**2:
                    raise RefreshError(
                        "A stored account proof exceeds its bounded metadata budget."
                    )
                packet = json.loads(encoded)
                binding = packet.get("account_binding")
                if (
                    packet.get("account_id") != scope.account_id
                    or not isinstance(binding, dict)
                    or binding.get("account_directory") != directory
                    or packet.get("source", {}).get("database_relative_path")
                    != source["database_relative_path"]
                ):
                    continue
                bank._account_binding_shape(packet)
                for anchor in binding["anchors"]:
                    selected.setdefault(
                        (
                            anchor["table"],
                            anchor["local_id"],
                            anchor["archive_message_id"],
                        ),
                        anchor,
                    )
                    if len(selected) >= 32:
                        break
                if len(selected) >= 32:
                    break
            if len(selected) >= 32:
                break
    if bank._identity(path) != identity:
        raise RefreshError("The stored account proof changed during verification.")
    return list(selected.values())


def _global_binding(
    local_store_root,
    native,
    history,
    scope,
    source,
    directory,
    names,
    peer_rows,
    tables,
    check_cancel,
):
    selected = []
    for coordinate in _stored_witnesses(
        local_store_root, scope, source, directory, check_cancel
    ):
        check_cancel()
        peer = coordinate["peer_id"]
        table = _table(peer)
        if table not in tables or peer not in peer_rows:
            continue
        archived = history.execute(
            "SELECT m.*,c.peer_id,c.partition_id,p.account_id,p.identity_status "
            "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "JOIN partitions p ON p.id=c.partition_id WHERE m.id=?",
            (coordinate["archive_message_id"],),
        ).fetchone()
        if (
            archived is None
            or archived["peer_id"] != peer
            or archived["account_id"] != scope.account_id
        ):
            continue
        partition = (
            "ios-"
            + hashlib.md5(scope.account_id.encode(), usedforsecurity=False).hexdigest()
        )
        if (
            archived["partition_id"] != partition
            or archived["identity_status"] != "Verified archive ID"
        ):
            continue
        rows = _native_rows(native, table, local_id=coordinate["local_id"])
        row = rows.fetchone()
        if row is None or row["local_type"] != 1:
            continue
        try:
            message = _project_message(
                row,
                scope.account_id,
                "group" if peer.endswith(("@chatroom", "@im.chatroom")) else "direct",
                names,
            )
        except (ValueError, WCDBBackendUnavailableError):
            continue
        if message["server_id"] != coordinate["server_id"]:
            continue
        anchor = _archive_anchor(archived, row, message, peer, peer_rows[peer])
        if anchor and (message["direction"] != "sent" or row["real_sender_id"] == 2):
            if not any(
                item["peer_id"] == peer and item["direction"] == message["direction"]
                for item in selected
            ):
                selected.append(anchor)
        if len({item["peer_id"] for item in selected}) >= 2 and any(
            item["direction"] == "sent" for item in selected
        ):
            sent = next(item for item in selected if item["direction"] == "sent")
            other = next(
                item for item in selected if item["peer_id"] != sent["peer_id"]
            )
            return {
                "schema_version": 1,
                "account_id": scope.account_id,
                "account_directory": directory,
                "owner_proof": _name_proof(names[2]),
                "source": {field: source[field] for field in _SOURCE_BINDING_FIELDS},
                "anchors": [sent, other],
            }
    raise RefreshError(
        "This database has insufficient independently revalidated account evidence."
    )


def prepare_packet(local_store_root, scope, database, *, check_cancel=None):
    """Return only new or changed target events; identical replay creates no new bank copy."""
    check_cancel = check_cancel if check_cancel is not None else lambda: None
    check_cancel()
    native = database.connection
    tables = {
        row[0]
        for row in native.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if len(tables) > MAX_NATIVE_TABLES:
        raise RefreshError("The native database exceeds its bounded table budget.")
    table = _table(scope.peer_id)
    names = {}
    peer_rows = {}
    for row in native.execute(
        "SELECT rowid,user_name,is_session FROM Name2Id LIMIT ?", (MAX_NAMES + 1,)
    ):
        if len(names) >= MAX_NAMES:
            raise RefreshError("The native name index exceeds its bounded row budget.")
        value = dict(row)
        names[value["rowid"]] = value
        peer_rows.setdefault(value["user_name"], value)
    if names.get(2, {}).get("user_name") != scope.account_id:
        raise RefreshError(
            "The authenticated native owner does not match this conversation."
        )
    if table not in tables:
        return None, {"total": 0, "existing": 0, "excluded": 0}
    if scope.peer_id not in peer_rows:
        raise RefreshError(
            "The authenticated native peer does not match this conversation."
        )
    source = dict(database.source, table=table)
    packet = {
        "schema_version": 2,
        "receipt_id": "refresh-" + source["snapshot_id"],
        "account_id": scope.account_id,
        "session_id": scope.session_id,
        "peer_id": scope.peer_id,
        "conversation_kind": scope.kind,
        "peer_proof": _name_proof(peer_rows[scope.peer_id]),
        "source": source,
        "anchors": [],
        "messages": [],
    }
    history_path = wechat_archive_path(local_store_root)
    history = sqlite3.connect(
        history_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    history.row_factory = sqlite3.Row
    try:
        history.execute("PRAGMA temp_store=MEMORY")
        repository = WechatArchiveRepository(local_store_root)
        _conversations, message_table, status = repository._timeline_tables(
            history,
            _file_identity(history_path),
            strict=True,
        )
        if status not in {"ready", "absent"}:
            raise RefreshError(
                "The existing conversation timeline is temporarily unavailable."
            )
        known = {}
        for index, existing in enumerate(
            history.execute(
                f"SELECT * FROM {message_table} WHERE conversation_id=?",
                (scope.session_id,),
            )
        ):
            if index >= MAX_ARCHIVE_ANCHOR_ROWS:
                raise RefreshError(
                    "The existing conversation exceeds its bounded row budget."
                )
            check_cancel()
            proof = json.loads(existing["provenance"])
            known.setdefault(str(proof.get("server_id")), set()).add(
                _comparison_signature(existing)
            )
        archive = {}
        for index, row in enumerate(
            history.execute(
                "SELECT * FROM main.messages WHERE conversation_id=?",
                (scope.session_id,),
            )
        ):
            check_cancel()
            if index >= MAX_ARCHIVE_ANCHOR_ROWS:
                raise RefreshError(
                    "The original conversation exceeds its bounded anchor budget."
                )
            proof = json.loads(row["provenance"])
            archive.setdefault(str(proof.get("server_id")), []).append(row)
        local_anchors = {}
        counts = {"total": 0, "existing": 0, "excluded": 0}
        for row in _native_rows(native, table):
            check_cancel()
            counts["total"] += 1
            if counts["total"] > bank.MAX_PACKET_MESSAGES:
                raise RefreshError(
                    "This conversation exceeds the bounded native refresh budget."
                )
            try:
                message = _project_message(row, scope.account_id, scope.kind, names)
                for original in archive.get(message["server_id"], ()):
                    anchor = _archive_anchor(
                        original, row, message, scope.peer_id, peer_rows[scope.peer_id]
                    )
                    if anchor:
                        local_anchors[message["server_id"]] = anchor
                projected = bank._row(packet, message)
                signature = _comparison_signature(
                    {
                        "sender_id": projected[3],
                        "direction": projected[5],
                        "text": projected[6],
                        "timestamp_value": projected[8],
                        "provenance": projected[11],
                    }
                )
                if signature in known.get(message["server_id"], ()):
                    counts["existing"] += 1
                    continue
                # Validate every candidate with the established packet validator below.
                packet["messages"].append(message)
            except (WCDBDecodeError, WCDBBackendUnavailableError, RefreshError):
                counts["excluded"] += 1
        if not packet["messages"]:
            return None, counts
        if len(local_anchors) >= 2:
            packet["anchors"] = [
                {
                    field: anchor[field]
                    for field in ("server_id", "archive_message_id", "text_sha256")
                }
                for anchor in list(local_anchors.values())[:2]
            ]
        else:
            source["account_directory"] = database.account_directory
            packet["account_binding"] = _global_binding(
                local_store_root,
                native,
                history,
                scope,
                source,
                database.account_directory,
                names,
                peer_rows,
                tables,
                check_cancel,
            )
        bank._validate_packet(packet, (scope.account_id,))
        bank._binding(history, packet)
        return packet, counts
    finally:
        history.close()
