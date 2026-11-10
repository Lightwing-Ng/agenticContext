"""Index native resource references without copying or decoding media payloads.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from xml.etree import ElementTree

from .owner_only_files import ensure_owner_only
from .wechat_archive import _identifier, _time_fields, wechat_archive_path


_CHAT = re.compile(r"Chat_([0-9a-f]{32})")
_DATABASE = re.compile(r"(?:message_[0-9]+|MM)\.sqlite")
_MD5 = re.compile(r"[0-9a-fA-F]{32}")
_KINDS = {
    3: "image",
    34: "audio",
    43: "video",
    47: "sticker",
    49: "app",
    62: "video",
    64: "unknown",
}
_LABELS = {
    "image": "[Image]",
    "video": "[Video]",
    "file": "[File]",
    "audio": "[Voice message]",
    "sticker": "[Sticker]",
    "app": "[App message]",
    "unknown": "[Other message]",
}
_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_MAX_XML = 256 * 1024
_APP_LABEL_FIELDS = ("safe_title", "publisher_name", "app_name")
_MAX_PEERS = 200_000
_SCHEMA = """
CREATE TABLE messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
 sender_id TEXT, sender_label TEXT NOT NULL, direction TEXT NOT NULL,
 text TEXT NOT NULL, time_sort REAL NOT NULL, timestamp_value TEXT NOT NULL,
 timestamp_label TEXT NOT NULL, time_basis TEXT NOT NULL,
 provenance TEXT NOT NULL, quality_flags TEXT NOT NULL, search_text TEXT NOT NULL
);
CREATE INDEX message_conversation_time ON messages(conversation_id,time_sort,ordinal);
CREATE TABLE conversations (
 id TEXT PRIMARY KEY, partition_id TEXT NOT NULL, account_key TEXT NOT NULL,
 peer_id TEXT, title TEXT NOT NULL, kind TEXT NOT NULL,
 message_count INTEGER NOT NULL, first_sort REAL NOT NULL, last_sort REAL NOT NULL,
 first_time TEXT NOT NULL, last_time TEXT NOT NULL, latest_text TEXT NOT NULL
);
CREATE INDEX conversation_account_kind ON conversations(account_key,kind,last_sort);
CREATE TABLE archive_info (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


def resource_index_path(local_store_root: Path | str) -> Path:
    """Keep the additive resource catalog beside the original text index."""
    return wechat_archive_path(local_store_root).with_name(
        "resource_references.sqlite3"
    )


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _identity(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Resource inputs must be regular local files.")
    state = path.stat()
    if getattr(state, "st_flags", 0) & 0x40000000:
        raise ValueError("Resource indexing cannot hydrate a cloud-only input.")
    return {
        "dev": state.st_dev,
        "inode": state.st_ino,
        "size": state.st_size,
        "mtime_ns": state.st_mtime_ns,
    }


@contextmanager
def _readonly(path, root=None):
    path = Path(path)
    _identity(path)
    resolved = path.resolve(strict=True)
    if root is not None and not resolved.is_relative_to(root):
        raise ValueError("A resource input escapes its preserved snapshot.")
    if root is not None:
        ancestor = root
        for part in path.relative_to(root).parts:
            ancestor /= part
            if ancestor.is_symlink():
                raise ValueError("A resource input cannot traverse a symbolic link.")
    connection = sqlite3.connect(resolved.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value)
    return (
        str(int(value))
        if value.isascii()
        and value.isdecimal()
        and 0 < int(value) <= 9223372036854775807
        else ""
    )


def _checksum(value):
    return value.lower() if isinstance(value, str) and _MD5.fullmatch(value) else ""


def _size(value):
    return value if type(value) is int and 0 <= value <= 9223372036854775807 else None


def _string(value, limit=1024):
    return (
        value
        if isinstance(value, str) and len(value) <= limit and "\0" not in value
        else ""
    )


def _app_label(value):
    """Retain only bounded, nonempty plain labels from their direct XML leaf."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 1024
        or any(ord(char) < 32 and char not in "\t\r\n" for char in value)
    ):
        return ""
    return value


def _extension(value):
    value = _string(value, 32).strip().lstrip(".").lower()
    return value if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", value) else ""


def _source_metadata(snapshot, inputs):
    manifest = snapshot.parent / "preservation-manifest.json"
    checksums = {}
    pinned = {}
    backup_id = ""
    if manifest.is_file():
        pinned[manifest] = _identity(manifest)
        if pinned[manifest]["size"] > 64 * 1024 * 1024:
            raise ValueError("The preservation manifest exceeds the metadata limit.")
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        candidate = metadata.get("snapshot_id")
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
            backup_id = candidate
        for item in metadata.get("selected_files", ()):
            if not isinstance(item, dict):
                continue
            digest = item.get("sha256")
            stored = item.get("snapshot_path")
            if (
                isinstance(stored, str)
                and isinstance(digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                checksums[str(Path(stored).resolve())] = digest
    if not backup_id:
        backup_id = _identifier(
            "resource-snapshot",
            _json(
                [
                    [str(path.relative_to(snapshot)), identity]
                    for path, identity in sorted(inputs.items())
                ]
            ),
        )
    return backup_id, checksums, pinned


def _account_peers(owner_root, root, existing, pinned):
    peers = {}
    conversations = {}
    for row in existing:
        peer = row["peer_id"] or ""
        if peer:
            hashed = hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
            peers[hashed] = peer
            conversations[hashed] = dict(row)
    specifications = (
        (owner_root / "DB/MM.sqlite", "Friend", "UsrName"),
        (owner_root / "DB/WCDB_Contact.sqlite", "Friend", "userName"),
        (owner_root / "FileIndex/fileIndexInfo.db", "FileIndexInfoTable", "userName"),
        (owner_root / "FileBrowse/fileInfo.db", "BrowseInfoTable", "fromChatName"),
    )
    for path, table, column in specifications:
        if not path.is_file():
            continue
        pinned.setdefault(path, _identity(path))
        with _readonly(path, root) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if table not in names:
                continue
            columns = {
                row[1].casefold()
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            if column.casefold() not in columns:
                continue
            for row in connection.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" LIMIT ?', (_MAX_PEERS + 1,)
            ):
                peer = _string(row[0])
                if not peer:
                    continue
                if len(peers) >= _MAX_PEERS:
                    raise ValueError(
                        "The account peer metadata exceeds the supported limit."
                    )
                peers[hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()] = (
                    peer
                )
    return peers, conversations


def _existing_observations(history, conversation_id, backup_id, snapshots, partition):
    server_ids = set()
    locators = set()

    def add(provenance, snapshot=""):
        if not isinstance(provenance, dict):
            return
        server_id = _positive_id(provenance.get("server_id"))
        if server_id:
            server_ids.add(server_id)
        snapshot = snapshot or snapshots.get(provenance.get("source_snapshot"), "")
        locator = provenance.get("locator")
        if snapshot == backup_id and isinstance(locator, dict):
            local_id = locator.get("local_id", locator.get("row_id"))
            if type(local_id) is int:
                locators.add((locator.get("database"), locator.get("table"), local_id))

    for row in history.execute(
        "SELECT provenance FROM messages WHERE conversation_id=?", (conversation_id,)
    ):
        add(json.loads(row[0]))
    tables = {
        row[0]
        for row in history.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "message_evidence" in tables:
        for row in history.execute(
            "SELECT e.snapshot_id,e.observation FROM messages m JOIN message_evidence e ON e.message_id=m.id "
            "WHERE m.conversation_id=? AND e.source_id=?",
            (conversation_id, partition),
        ):
            add(json.loads(row[1]), row[0])
    return server_ids, locators


def _resource_metadata(owner_root, root, peer, pinned):
    browse = {}
    indexed = defaultdict(list)
    specifications = (
        (owner_root / "FileBrowse/fileInfo.db", "BrowseInfoTable"),
        (owner_root / "FileIndex/fileIndexInfo.db", "FileIndexInfoTable"),
    )
    for path, table in specifications:
        if not path.is_file() or not peer:
            continue
        pinned.setdefault(path, _identity(path))
        with _readonly(path, root) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                continue
            if table == "BrowseInfoTable":
                for row in connection.execute(
                    "SELECT msgWrapSvrID,msgLocalID,fileTime,desTitle,fileExt,fileMD5,fileSize,fromUserName "
                    "FROM BrowseInfoTable WHERE fromChatName=?",
                    (peer,),
                ):
                    server_id = _positive_id(row[0])
                    if server_id:
                        browse[server_id] = dict(row)
            else:
                for row in connection.execute(
                    "SELECT msgLocalId,msgtime,msgType,indexType,fileMD5,fileSize FROM FileIndexInfoTable "
                    "WHERE userName=?",
                    (peer,),
                ):
                    indexed[(row[0], row[1], row[2])].append(dict(row))
    return browse, indexed


def _app_metadata(payload, group, received):
    result = {}
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return result
    if (
        not isinstance(payload, str)
        or len(payload) > _MAX_XML
        or _DECLARATION.search(payload)
    ):
        return result
    if group and received and ":\n" in payload:
        sender, payload = payload.split(":\n", 1)
        if re.fullmatch(r"[A-Za-z0-9_.@-]{1,256}", sender):
            result["sender_id"] = sender
    try:
        document = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, ValueError):
        return result
    if sum(1 for _ in document.iter()) > 4096:
        return result
    app = document if document.tag == "appmsg" else document.find("appmsg")
    if app is None:
        return result
    subtype = app.findtext("type", "")
    result["subtype"] = subtype
    result["kind"] = {"2": "image", "3": "audio", "4": "video", "6": "file"}.get(
        subtype, "app"
    )
    types = app.findall("type")
    if subtype == "5" and len(types) == 1 and len(types[0]) == 0:
        result.update(app_subtype="5", availability="metadata_only")
        labels = {}
        for key, parent, path in (
            ("safe_title", app, "title"),
            ("publisher_name", app, "sourcedisplayname"),
            ("app_name", document, "appinfo/appname"),
        ):
            nodes = parent.findall(path)
            if len(nodes) == 1 and len(nodes[0]) == 0:
                value = _app_label(nodes[0].text)
                if value:
                    labels[key] = value
        if labels:
            result.update(labels, app_metadata_version="1")
    if subtype == "6":
        result["filename"] = _string(app.findtext("title", ""))
        result["file_extension"] = _extension(app.findtext("appattach/fileext", ""))
        result["checksum_md5"] = _checksum(app.findtext("appattach/md5", ""))
        length = app.findtext("appattach/totallen", "")
        result["byte_size"] = (
            _size(int(length))
            if length.isascii() and length.isdecimal() and len(length) <= 19
            else None
        )
    return result


def _index_reference(rows, kind):
    """Choose a corroborated variant without guessing between conflicting hashes."""
    preferred, thumbnail = {"image": (10, 11), "video": (20, 21)}.get(kind, (30, None))
    for index_type, variant in (
        (preferred, "original" if kind in {"image", "video"} else "native_cache"),
        (thumbnail, "thumbnail"),
    ):
        if index_type is None:
            continue
        selected = [
            row
            for row in rows
            if row["indexType"] == index_type and _checksum(row["fileMD5"])
        ]
        checksums = {_checksum(row["fileMD5"]) for row in selected}
        if len(checksums) > 1:
            return {}, True
        if checksums:
            sizes = {_size(row["fileSize"]) for row in selected}
            sizes.discard(None)
            return {
                "checksum_md5": checksums.pop(),
                "byte_size": sizes.pop() if len(sizes) == 1 else None,
                "variant": variant,
            }, False
    return {}, False


def _table_rows(connection, table):
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    required = {"MesLocalID", "MesSvrID", "CreateTime", "Type", "Des", "Message"}
    if not required <= columns:
        raise ValueError("A native message table lacks required resource metadata.")
    marker = '"WCDB_CT_Message"' if "WCDB_CT_Message" in columns else "0"
    return connection.execute(
        f"SELECT MesLocalID,MesSvrID,CreateTime,Type,Des,CASE WHEN Type=49 AND "
        f"length(Message)<={_MAX_XML} AND (typeof(Message)='text' OR "
        f"(typeof(Message)='blob' AND COALESCE({marker},0) IN (0,1))) "
        f'THEN Message ELSE NULL END AS metadata_payload FROM "{table}" '
        f"WHERE Type IN ({','.join(str(value) for value in _KINDS)}) ORDER BY MesLocalID"
    )


def build_resource_index(local_store_root, preservation_snapshot, *, account_ids=()):
    """Atomically publish metadata placeholders; original databases stay read-only."""
    from .wechat_archive import wechat_history_partition_fingerprint

    history_path = wechat_archive_path(local_store_root)
    history_identity = _identity(history_path)
    selected = Path(preservation_snapshot).expanduser()
    if selected.is_symlink() or not selected.is_dir():
        raise ValueError("Select a regular, preserved snapshot directory.")
    snapshot = selected.resolve(strict=True)
    documents = snapshot / "AppDomain-com.tencent.xin/Documents"
    if not documents.is_dir() or not documents.resolve().is_relative_to(snapshot):
        raise ValueError("The snapshot lacks the native WeChat documents directory.")
    target = resource_index_path(local_store_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".wechat-resources-", suffix=".sqlite3", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    report = {
        "version": "v1.0.0",
        "messages": 0,
        "conversations": 0,
        "accounts": [],
        "missing_accounts": [],
        "excluded_existing_records": 0,
        "media_payload_files_read": 0,
        "kinds": {},
        "target": str(target),
    }
    pinned = {}
    try:
        ensure_owner_only(temporary)
        with _readonly(history_path) as history, closing(sqlite3.connect(temporary)) as output:
            output.executescript(_SCHEMA)
            fingerprint = wechat_history_partition_fingerprint(history)
            partitions = [
                dict(row) for row in history.execute("SELECT * FROM partitions")
            ]
            requested = set(account_ids)
            native = []
            for partition in partitions:
                owner = partition["account_id"]
                if not owner or partition["account_key"] != "account:" + owner:
                    continue
                digest = hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
                if partition["id"] != "ios-" + digest:
                    continue
                if not requested or owner in requested:
                    native.append((partition, documents / digest))
            if requested - {row[0]["account_id"] for row in native}:
                raise ValueError(
                    "Requested resource accounts are not verified archive owners."
                )
            groups = []
            for partition, owner_root in native:
                owner = partition["account_id"]
                if not owner_root.is_dir():
                    report["missing_accounts"].append(owner)
                    continue
                if owner_root.is_symlink() or not owner_root.resolve().is_relative_to(
                    snapshot
                ):
                    raise ValueError("A native account directory escapes its snapshot.")
                grouped = defaultdict(list)
                db_root = owner_root / "DB"
                if db_root.is_dir():
                    for path in sorted(db_root.iterdir()):
                        if not _DATABASE.fullmatch(path.name):
                            continue
                        pinned[path] = _identity(path)
                        with _readonly(path, snapshot) as connection:
                            for row in connection.execute(
                                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                            ):
                                match = _CHAT.fullmatch(row[0])
                                if match:
                                    grouped[match[1]].append((path, row[0]))
                groups.append((partition, owner_root, grouped))
            if not groups:
                raise ValueError(
                    "No verified native resource account exists in this snapshot."
                )
            backup_id, source_checksums, manifest_pins = _source_metadata(
                snapshot, pinned
            )
            pinned.update(manifest_pins)
            snapshots = {}
            if history.execute(
                "SELECT 1 FROM sqlite_master WHERE name='source_snapshots' AND type='table'"
            ).fetchone():
                snapshots = {
                    row[0]: row[1]
                    for row in history.execute(
                        "SELECT id,snapshot_id FROM source_snapshots"
                    )
                }
            kinds = Counter()
            for partition, owner_root, grouped in groups:
                owner = partition["account_id"]
                existing = list(
                    history.execute(
                        "SELECT * FROM conversations WHERE partition_id=?",
                        (partition["id"],),
                    )
                )
                peers, conversations = _account_peers(
                    owner_root, snapshot, existing, pinned
                )
                account_count = 0
                for hashed, tables in sorted(grouped.items()):
                    peer = peers.get(hashed, "")
                    current = conversations.get(hashed)
                    conversation_id = (
                        current["id"]
                        if current
                        else "wechat:" + _identifier(partition["id"], peer or hashed)
                    )
                    conversation_kind = (
                        current["kind"]
                        if current
                        else "group"
                        if peer.endswith("@chatroom")
                        else "direct"
                        if peer
                        else "unclassified"
                    )
                    title = (
                        current["title"]
                        if current
                        else peer or "Unidentified conversation · " + hashed[-8:]
                    )
                    excluded_servers, excluded_locators = _existing_observations(
                        history, conversation_id, backup_id, snapshots, partition["id"]
                    )
                    browse, indexed = _resource_metadata(
                        owner_root, snapshot, peer, pinned
                    )
                    stats = None
                    batch = []
                    for path, table in tables:
                        source_relative_path = str(path.relative_to(snapshot))
                        source_checksum = source_checksums.get(str(path.resolve()), "")
                        with _readonly(path, snapshot) as connection:
                            for row in _table_rows(connection, table):
                                (
                                    local_id,
                                    raw_server,
                                    unix_time,
                                    message_type,
                                    direction_value,
                                    payload,
                                ) = row
                                server_id = _positive_id(raw_server)
                                if (
                                    server_id
                                    and server_id in excluded_servers
                                    or (path.name, table, local_id) in excluded_locators
                                ):
                                    report["excluded_existing_records"] += 1
                                    continue
                                direction = {0: "sent", 1: "received"}.get(
                                    direction_value, "unknown"
                                )
                                received = direction == "received"
                                reference = {
                                    "kind": _KINDS[message_type],
                                    "filename": "",
                                    "file_extension": "",
                                    "checksum_md5": "",
                                    "byte_size": None,
                                    "account_id": owner,
                                    "peer_id": peer,
                                    "unix_timestamp": unix_time,
                                }
                                app = (
                                    _app_metadata(
                                        payload, conversation_kind == "group", received
                                    )
                                    if message_type == 49 and payload is not None
                                    else {}
                                )
                                reference.update(
                                    {
                                        key: value
                                        for key, value in app.items()
                                        if key
                                        in {
                                            "kind",
                                            "filename",
                                            "file_extension",
                                            "checksum_md5",
                                            "byte_size",
                                            "app_subtype",
                                            "availability",
                                            "safe_title",
                                            "publisher_name",
                                            "app_name",
                                            "app_metadata_version",
                                        }
                                    }
                                )
                                sender = (
                                    owner
                                    if direction == "sent"
                                    else peer
                                    if received and conversation_kind == "direct"
                                    else app.get("sender_id", "")
                                )
                                file = browse.get(server_id)
                                if (
                                    message_type == 49
                                    and file
                                    and file["fileTime"] == unix_time
                                    and (
                                        not app.get("subtype") or app["subtype"] == "6"
                                    )
                                ):
                                    name = _string(file["desTitle"])
                                    extension = _extension(file["fileExt"])
                                    if name or extension:
                                        reference.update(
                                            kind="file",
                                            filename=name,
                                            file_extension=extension,
                                            checksum_md5=_checksum(file["fileMD5"]),
                                            byte_size=_size(file["fileSize"]),
                                        )
                                        if received and not sender:
                                            sender = _string(file["fromUserName"])
                                index_rows = indexed.get(
                                    (local_id, unix_time, message_type), ()
                                )
                                ambiguous_checksum = False
                                if not reference["checksum_md5"]:
                                    indexed_reference, ambiguous_checksum = (
                                        _index_reference(index_rows, reference["kind"])
                                    )
                                    reference.update(indexed_reference)
                                reference["label"] = _LABELS[reference["kind"]]
                                record_id = _identifier(
                                    "wechat-resource-reference-v1",
                                    owner,
                                    backup_id,
                                    source_relative_path,
                                    table,
                                    str(local_id),
                                )
                                locator = {
                                    "database": path.name,
                                    "table": table,
                                    "local_id": local_id,
                                    "row_id": local_id,
                                    "text_origin": "native_resource_metadata",
                                }
                                provenance = {
                                    "record_id": record_id,
                                    "locator": locator,
                                    "server_id": server_id or None,
                                    "unix_timestamp": unix_time,
                                    "source_time": None,
                                    "backup_id": backup_id,
                                    "message_type": message_type,
                                    "text_extraction_kind": "resource_placeholder",
                                    "resource_reference": reference,
                                    "source_relative_path": source_relative_path,
                                    "source_sha256": source_checksum,
                                    "source_checksum_basis": "preservation_manifest",
                                }
                                try:
                                    native_time = (
                                        datetime.fromtimestamp(
                                            unix_time, UTC
                                        ).isoformat()
                                        if type(unix_time) is int and unix_time > 0
                                        else ""
                                    )
                                except (ValueError, OverflowError, OSError):
                                    native_time = ""
                                sort_time, timestamp, label, basis = _time_fields(
                                    {"sent_at_utc_iso": native_time}
                                )
                                text = reference["label"]
                                flags = [
                                    "resource_placeholder",
                                    "media_payload_not_read",
                                ]
                                if ambiguous_checksum:
                                    flags.append("resource_checksum_ambiguous")
                                if direction == "unknown":
                                    flags.append("direction_unavailable")
                                if not sender:
                                    flags.append("sender_id_unavailable")
                                batch.append(
                                    (
                                        record_id,
                                        conversation_id,
                                        local_id,
                                        sender or None,
                                        sender or "Unknown sender",
                                        direction,
                                        text,
                                        sort_time,
                                        timestamp,
                                        label,
                                        basis,
                                        _json(provenance),
                                        _json(flags),
                                        "\n".join(
                                            (
                                                text,
                                                reference["filename"],
                                                sender,
                                                title,
                                                peer,
                                            )
                                        ).casefold(),
                                    )
                                )
                                if stats is None:
                                    stats = [
                                        0,
                                        sort_time,
                                        sort_time,
                                        timestamp,
                                        timestamp,
                                        text,
                                    ]
                                stats[0] += 1
                                if sort_time and (not stats[1] or sort_time < stats[1]):
                                    stats[1], stats[3] = sort_time, timestamp
                                if sort_time >= stats[2]:
                                    stats[2], stats[4], stats[5] = (
                                        sort_time,
                                        timestamp,
                                        text,
                                    )
                                kinds[reference["kind"]] += 1
                                account_count += 1
                                if len(batch) >= 1000:
                                    output.executemany(
                                        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                        batch,
                                    )
                                    batch.clear()
                    if batch:
                        output.executemany(
                            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            batch,
                        )
                    if stats:
                        output.execute(
                            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                conversation_id,
                                partition["id"],
                                partition["account_key"],
                                peer,
                                title,
                                conversation_kind,
                                *stats,
                            ),
                        )
                report["accounts"].append(
                    {"account_id": owner, "messages": account_count}
                )
            report["messages"] = sum(kinds.values())
            report["kinds"] = dict(sorted(kinds.items()))
            report["conversations"] = output.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0]
            report["backup_id"] = backup_id
            info = {
                "schema_version": "1",
                "history_partitions_sha256": fingerprint,
                "history_file_identity": _json(history_identity),
                "messages": str(report["messages"]),
                "backup_id": backup_id,
                "report": _json(report),
            }
            output.executemany("INSERT INTO archive_info VALUES(?,?)", info.items())
            output.commit()
            if _identity(history_path) != history_identity or any(
                _identity(path) != identity for path, identity in pinned.items()
            ):
                raise ValueError("A resource input changed while the index was built.")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        if _identity(history_path) != history_identity:
            raise ValueError("The text archive changed before resource publication.")
        os.replace(temporary, target)
        ensure_owner_only(target)
        return report
    finally:
        temporary.unlink(missing_ok=True)
