"""Store larger resource detail batches with indexed, replayable native proof.

Code version: v1.0.0-codex.0
"""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import stat

from .owner_only_files import ensure_owner_only, owner_only_problem
from .wechat_completion_json_codec import encode_json_cell, load_json_cell
from .wechat_resource_details import (
    _HEADER_FIELDS, _entry_shape, _json, _metadata_pin, _sha,
)


FILENAME = "resource_details.sqlite3"
VERSION = 1
MAX_ENTRIES = 250_000
MAX_BYTES = 2 * 1024**3
_SCHEMA = """
CREATE TABLE scopes (id TEXT PRIMARY KEY, body NOT NULL) WITHOUT ROWID;
CREATE TABLE sources (id TEXT PRIMARY KEY, body NOT NULL) WITHOUT ROWID;
CREATE TABLE witnesses (id TEXT PRIMARY KEY, body NOT NULL) WITHOUT ROWID;
CREATE TABLE entries (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    original_row_sha256 TEXT NOT NULL,
    scope_key TEXT NOT NULL REFERENCES scopes(id),
    source_key TEXT NOT NULL REFERENCES sources(id),
    witness_key TEXT NOT NULL REFERENCES witnesses(id),
    projected_sha256 TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX resource_detail_scope ON entries(conversation_id,message_id);
"""


def index_identity(path):
    """Pin a private, resident SQLite file and refuse transaction journals."""
    path = Path(path)
    try:
        state = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(state.st_mode) or state.st_size > MAX_BYTES
        or owner_only_problem(path) or path.parent.is_symlink()
        or getattr(state, "st_flags", 0) & 0x40000000
        or any(os.path.lexists(str(path) + s) for s in ("-wal", "-shm", "-journal"))
    ):
        raise ValueError("Resource detail index must be a stable private SQLite file.")
    return _metadata_pin(path, state)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(4 * 1024**2):
            digest.update(block)
    return digest.hexdigest()


def _read(connection, conversation_id):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != VERSION:
        raise ValueError("Unsupported resource detail index version.")
    filters = " WHERE e.conversation_id=?" if conversation_id is not None else ""
    query = (
        "SELECT e.*,c.body AS scope,s.body AS source,w.body AS witness "
        "FROM entries e LEFT JOIN scopes c ON c.id=e.scope_key "
        "LEFT JOIN sources s ON s.id=e.source_key "
        "LEFT JOIN witnesses w ON w.id=e.witness_key" + filters + " ORDER BY e.message_id"
    )
    parameters = (conversation_id,) if conversation_id is not None else ()
    headers, scopes = {}, {}
    for count, row in enumerate(connection.execute(query, parameters), start=1):
        if count > MAX_ENTRIES:
            raise ValueError("Resource detail index exceeds its row budget.")
        for key, field, cache in (
            ("source_key", "source", headers), ("scope_key", "scope", scopes),
        ):
            if row[key] not in cache:
                value = load_json_cell(row[field])
                if not isinstance(value, dict) or _sha(value) != row[key]:
                    raise ValueError("Resource detail index lost its shared proof.")
                cache[row[key]] = value
        header = headers[row["source_key"]]
        witness = load_json_cell(row["witness"])
        if (
            set(header) - _HEADER_FIELDS or not isinstance(witness, dict)
            or _sha(witness) != row["witness_key"]
        ):
            raise ValueError("Resource detail index lost its exact native witness.")
        entry = {
            "message_id": row["message_id"],
            "original_row_sha256": row["original_row_sha256"],
            "conversation": scopes[row["scope_key"]],
            "packet": {**header, "messages": [witness]},
        }
        if _entry_shape(entry)["id"] != row["conversation_id"]:
            raise ValueError("Resource detail index contains a mismatched room.")
        yield entry, row["projected_sha256"], row["source_key"]


def indexed_entries(path, identity, *, conversation_id=None):
    """Read only the selected scope while checking the immutable file pin."""
    if identity is None:
        return
    if index_identity(path) != identity:
        raise ValueError("Resource detail index changed before reading.")
    with closing(sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        yield from _read(connection, conversation_id)
    if index_identity(path) != identity:
        raise ValueError("Resource detail index changed during validation.")


def indexed_message_ids(path, identity):
    """Reserve indexed IDs when a small batch updates the older JSON registry."""
    if identity is None:
        return set()
    if index_identity(path) != identity:
        raise ValueError("Resource detail index changed before reading.")
    uri = Path(path).absolute().as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        rows = connection.execute("SELECT message_id FROM entries LIMIT ?", (MAX_ENTRIES + 1,))
        identifiers = {row[0] for row in rows}
    if len(identifiers) > MAX_ENTRIES or index_identity(path) != identity:
        raise ValueError("Resource detail index changed or exceeded its row budget.")
    return identifiers


def write_index(path, entries):
    """Write a complete staged index from already replayed resource details."""
    path = Path(path).absolute()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        ensure_owner_only(path)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA user_version={VERSION}")
            connection.executescript(_SCHEMA)
            keys = {"scopes": set(), "sources": set(), "witnesses": set()}
            count = 0
            for entry, projected_sha in entries:
                count += 1
                if count > MAX_ENTRIES:
                    raise ValueError("Resource detail index exceeds its row budget.")
                scope = _entry_shape(entry)
                packet = entry["packet"]
                header = {key: packet[key] for key in _HEADER_FIELDS if key in packet}
                parts = []
                for table, body in (
                    ("scopes", scope), ("sources", header), ("witnesses", packet["messages"][0]),
                ):
                    key = _sha(body)
                    if key not in keys[table]:
                        connection.execute(f"INSERT INTO {table} VALUES(?,?)", (
                            key, encode_json_cell(_json(body)),
                        ))
                        keys[table].add(key)
                    parts.append(key)
                connection.execute("INSERT INTO entries VALUES(?,?,?,?,?,?,?)", (
                    entry["message_id"], scope["id"], entry["original_row_sha256"],
                    *parts, projected_sha,
                ))
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Staged resource detail SQLite integrity check failed.")
        identity = index_identity(path)
        if sum(1 for _ in indexed_entries(path, identity)) != count:
            raise ValueError("Staged resource detail index lost proof during readback.")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
