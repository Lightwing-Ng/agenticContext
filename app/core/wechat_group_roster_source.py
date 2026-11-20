"""Read a cross-checked member set from an authenticated local contact database.

Code version: v1.1.0-codex.0
"""

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

from . import wechat_mac_refresh_source as native
from .wechat_contacts import classify_wechat_identifier
from .wechat_identity import MAX_ROOM_MEMBERS, _fields, _selected_blobs, _text


VERSION = "v1.1.0-codex.0"
_CONTACT_DATABASE = "db_storage/contact/contact.db"
_SAFE_TAILS = frozenset({
    "none", "uncommitted_frames", "zero_preallocation", "stale_salt_tail",
})
_SCHEMA = {
    "chat_room": {"id", "username", "owner", "ext_buffer"},
    "chatroom_member": {"room_id", "member_id"},
    "name2id": {"username"},
    "contact": {"id", "username", "nick_name", "is_in_chat_room", "delete_flag"},
}


def _contact_reference(account_id, registry, *, load_key):
    if (
        not isinstance(account_id, str)
        or native._DIRECTORY.fullmatch(account_id) is None
        or account_id in {".", ".."}
    ):
        raise native.RefreshError("A roster needs an exact native account.")
    if not registry.key_root.is_dir():
        raise native.RefreshError("The contact database is not configured.")
    native._checked_path(registry.key_root)
    files = sorted(registry.key_root.glob("macos-*/verified-db-keys*.jsonl"))
    if len(files) > native.MAX_KEY_FILES:
        raise native.RefreshError("The configured key registry exceeds its file budget.")
    references = {}
    records = 0
    for path in files:
        raw = native.read_stable(path, native.MAX_VAULT_BYTES, private=True)
        for line in raw.splitlines():
            if not line.strip():
                continue
            records += 1
            if records > native.MAX_KEY_RECORDS:
                raise native.RefreshError("The configured key registry exceeds its row budget.")
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                raise native.RefreshError("The configured contact binding is invalid.") from None
            if not isinstance(row, dict):
                raise native.RefreshError("The configured contact binding is invalid.")
            directory = row.get("account_directory")
            if (
                not isinstance(directory, str)
                or not (directory == account_id or directory.startswith(account_id + "_"))
                or row.get("relative_path") != _CONTACT_DATABASE
            ):
                continue
            if native._DIRECTORY.fullmatch(directory) is None:
                raise native.RefreshError("The configured contact directory is invalid.")
            expected = registry.native_root / directory / _CONTACT_DATABASE
            key = row.get("enc_key")
            if (
                row.get("database_path") != str(expected)
                or not isinstance(key, str)
                or native._HEX_KEY.fullmatch(key) is None
                or not row.get("verification")
            ):
                raise native.RefreshError("The contact key lost its exact database binding.")
            native._checked_path(expected)
            native.identity(expected)
            reference = native.KeyReference(
                account_id, directory, _CONTACT_DATABASE, expected,
                bytes.fromhex(key) if load_key else None,
            )
            previous = references.get(expected)
            if previous is not None and load_key and previous.key != reference.key:
                raise native.RefreshError("Configured contact database keys conflict.")
            references[expected] = reference
    if len(references) != 1:
        raise native.RefreshError("A roster needs one configured contact database.")
    return next(iter(references.values()))


def roster_source_available(account_id, *, registry=None):
    """Check exact source metadata without decrypting or returning a secret."""
    try:
        _contact_reference(account_id, registry or native.KeyRegistry(), load_key=False)
    except (OSError, ValueError):
        return False
    return True


def _username(value):
    if (
        not isinstance(value, str) or not value or len(value.encode()) > 1024
        or "\0" in value or any(character.isspace() for character in value)
    ):
        raise native.RefreshError("The roster contains an invalid native username.")
    return value


def _room_members(blob):
    if not isinstance(blob, bytes):
        raise native.RefreshError("The native room member metadata is unavailable.")
    members = {}
    try:
        for number, wire, value in _fields(blob):
            if number != 1:
                continue
            if wire != 2 or not isinstance(value, bytes):
                raise ValueError("Invalid room member wire type.")
            fields = _selected_blobs(value, {1, 2})
            username = _username(_text(fields.get(1, b"")))
            if username in members or len(members) >= MAX_ROOM_MEMBERS:
                raise ValueError("Duplicate or excessive room members.")
            members[username] = _text(fields.get(2, b""))
    except (ValueError, TypeError):
        raise native.RefreshError("The native room member metadata is inconsistent.") from None
    return members


def _read_members(database, account_id, peer_id):
    connection = database.connection
    for table, columns in _SCHEMA.items():
        ordinary = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if (
            ordinary is None or "VIRTUAL" in ordinary[0].upper()
            or not columns.issubset({
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            })
        ):
            raise native.RefreshError("The native contact schema is unsupported.")
    rooms = connection.execute(
        "SELECT id,owner,ext_buffer FROM chat_room WHERE username=?", (peer_id,)
    ).fetchall()
    if len(rooms) != 1:
        raise native.RefreshError("The exact native group is unavailable or ambiguous.")
    room = rooms[0]
    owner = _username(room["owner"])
    peer_rows = connection.execute(
        "SELECT c.id,c.is_in_chat_room,c.delete_flag,n.rowid AS native_id "
        "FROM contact c JOIN name2id n ON n.username=c.username WHERE c.username=?",
        (peer_id,),
    ).fetchall()
    if (
        len(peer_rows) != 1 or peer_rows[0]["id"] != room["id"]
        or peer_rows[0]["native_id"] != room["id"]
        or peer_rows[0]["is_in_chat_room"] != 1 or peer_rows[0]["delete_flag"] != 0
    ):
        raise native.RefreshError("The native group membership scope is inconsistent.")
    rows = connection.execute(
        "SELECT m.member_id,n.username,c.username AS contact_username,c.nick_name "
        "FROM chatroom_member m LEFT JOIN name2id n ON n.rowid=m.member_id "
        "LEFT JOIN contact c ON c.id=m.member_id WHERE m.room_id=?",
        (room["id"],),
    ).fetchmany(MAX_ROOM_MEMBERS + 1)
    if not rows or len(rows) > MAX_ROOM_MEMBERS:
        raise native.RefreshError("The native group member set is empty or too large.")
    ext_members = _room_members(room["ext_buffer"])
    members = {}
    for row in rows:
        username = _username(row["username"])
        if username != row["contact_username"] or username in members:
            raise native.RefreshError("The native member identity joins are inconsistent.")
        label = row["nick_name"] or ""
        if not isinstance(label, str) or "\0" in label or len(label.encode()) > 64 * 1024:
            raise native.RefreshError("The native member display name is invalid.")
        members[username] = {
            "username": username,
            "display_name": ext_members.get(username) or label,
        }
    if set(members) != set(ext_members) or account_id not in members or owner not in members:
        raise native.RefreshError("The native room member sets do not agree.")
    # The relation verifies membership; repeated protobuf entries preserve order.
    member_order = tuple(ext_members)
    member_hash = hashlib.sha256(json.dumps(
        sorted(members), ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    member_order_hash = hashlib.sha256(json.dumps(
        member_order, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    return tuple(members[key] for key in member_order), {
        "owner_id": owner,
        "membership_sha256": member_hash,
        "member_order_sha256": member_order_hash,
        "room_ext_sha256": hashlib.sha256(room["ext_buffer"]).hexdigest(),
    }


def capture_group_roster(account_id, peer_id, *, registry=None, check_cancel=None):
    """Capture a complete committed local roster with retained source boundaries."""
    if classify_wechat_identifier(peer_id)["kind"] != "group":
        raise native.RefreshError("A roster needs an exact native group.")
    reference = _contact_reference(
        account_id, registry or native.KeyRegistry(), load_key=True,
    )
    database = native.read_database(reference, check_cancel=check_cancel)
    try:
        if database.wal_tail_status not in _SAFE_TAILS:
            raise native.RefreshError("The contact database has an incomplete WAL tail.")
        if (
            database.source.get("integrity_check") != "ok"
            or database.source.get("authenticated_pages") is not True
            or database.account_directory != reference.account_directory
        ):
            raise native.RefreshError("The contact database authentication is incomplete.")
        try:
            members, member_proof = _read_members(database, account_id, peer_id)
        except sqlite3.DatabaseError:
            raise native.RefreshError("The native contact schema is unsupported.") from None
        return {
            "account_id": account_id,
            "peer_id": peer_id,
            "observed_at": datetime.now(UTC).isoformat(),
            "members": members,
            "member_count": len(members),
            "complete": True,
            "source": {
                **database.source,
                "adapter_version": VERSION,
                "account_directory": reference.account_directory,
                "table": "chat_room",
                "database_relative_path": _CONTACT_DATABASE,
                "completeness_scope": "committed_local_roster",
                "member_set_cross_checked": True,
                "roster_count": len(members),
                "wal_tail_complete": not database.partial,
                "wal_tail_status": database.wal_tail_status,
                **member_proof,
            },
        }
    finally:
        database.close()
