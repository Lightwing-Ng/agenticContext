"""Retain export labels without mistaking mutable aliases for native usernames.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import csv
from contextlib import closing
import ctypes
import errno
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
import tempfile

from app.core.wechat_contacts import (
    MAX_EVIDENCE_BYTES,
    MESSAGE_BINDING_AUTHORITY,
    canonical_contact_id,
    classify_wechat_identifier,
    contact_observation,
    contact_registry_path,
    initialize_contact_registry,
    source_contact_id,
)


VERSION = "v1.0.1-codex.0"
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _exact(value, label, *, empty=False):
    if (
        not isinstance(value, str)
        or (not empty and not value)
        or value != value.strip()
        or "\0" in value
        or len(value.encode()) > 1024
    ):
        raise ValueError(f"An exact {label} is required.")
    return value


def _evidence(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("Nonempty structured evidence is required.")
    encoded = _json(value)
    if len(encoded.encode()) > MAX_EVIDENCE_BYTES:
        raise ValueError("Export contact evidence is too large.")
    return encoded


def parse_contacts_csv(path: Path | str) -> dict:
    """Read the exporter's headerless four-column CSV without fetching avatars."""
    payload = Path(path).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    text = payload.decode("utf-8-sig", errors="strict")
    rows = []
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        for number, fields in enumerate(reader, 1):
            if len(fields) != 4 or any("\0" in field for field in fields):
                raise ValueError("Contacts CSV requires four NUL-free columns.")
            identifier, label, avatar, tag = fields
            _exact(identifier, "export contact identifier")
            if any(character.isspace() for character in identifier):
                raise ValueError("Export contact identifiers cannot contain whitespace.")
            if len(label.encode()) > 64 * 1024:
                raise ValueError("Export contact label is too large.")
            rows.append(
                {
                    "row_number": number,
                    "export_id": identifier,
                    "label": label,
                    "avatar_url": avatar,
                    "tag_raw": tag,
                    "csv_sha256": digest,
                }
            )
    except csv.Error as error:
        raise ValueError("Malformed contacts CSV.") from error
    return {"sha256": digest, "rows": rows}


def _file_identity(path):
    if path.is_symlink():
        raise ValueError("Contact registry files cannot be symbolic links.")
    if not path.exists():
        return None
    metadata = path.stat()
    if not path.is_file() or metadata.st_nlink != 1:
        raise ValueError("Contact registry files must be singly linked regular files.")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
    )


def _database_identity(path):
    return tuple(
        _file_identity(Path(str(path) + suffix))
        for suffix in ("", "-wal", "-journal", "-shm")
    )


def _clone_database(source, destination):
    """Require a filesystem clone; never silently copy the entire registry."""
    if sys.platform != "darwin":
        raise OSError(errno.ENOTSUP, "An APFS clone is required for contact publication.")
    library = ctypes.CDLL(None, use_errno=True)
    clone = getattr(library, "clonefile", None)
    if clone is None:
        raise OSError(errno.ENOTSUP, "The host does not support APFS clonefile.")
    clone.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(destination), 0):
        code = ctypes.get_errno()
        raise OSError(code, "APFS contact registry clone failed.")


def _insert(connection, table, columns, keys, values, counts):
    """Insert an additive record or require its existing value to be identical."""
    where = " AND ".join(key + "=?" for key in keys)
    parameters = tuple(values[columns.index(key)] for key in keys)
    previous = connection.execute(
        "SELECT " + ",".join(columns) + " FROM " + table + " WHERE " + where,
        parameters,
    ).fetchone()
    if previous is not None:
        if tuple(previous) != tuple(values):
            raise ValueError(f"Conflicting immutable export record in {table}.")
        return
    connection.execute(
        "INSERT INTO " + table + "(" + ",".join(columns) + ") VALUES("
        + ",".join("?" for _ in values) + ")",
        values,
    )
    counts[table] = counts.get(table, 0) + 1


def _paired_identity(export_id, identity):
    _exact(export_id, "export sender identifier")
    if not isinstance(identity, dict):
        raise ValueError("Sender identities must be structured objects.")
    username = _exact(identity.get("native_username"), "native username")
    evidence = identity.get("evidence")
    _evidence(evidence)
    avatar = evidence.get("avatar_path")
    locator = evidence.get("source_locator")
    if (
        evidence.get("sender_export_id") != export_id
        or evidence.get("sender_native_id") != username
        or not isinstance(locator, dict)
        or not locator
        or not isinstance(avatar, str)
        or "\\" in avatar
        or "?" in avatar
        or "#" in avatar
        or ":" in avatar
        or ".." in PurePosixPath(avatar).parts
        or PurePosixPath(avatar).is_absolute()
        or PurePosixPath(avatar).name != username + ".jpg"
    ):
        raise ValueError("A matching same-frame export ID and native avatar pair is required.")
    realm = _exact(identity.get("realm", ""), "application realm", empty=True)
    namespace = "openim" if username.endswith("@openim") else "wechat"
    identifier = canonical_contact_id(
        username,
        authority="corroborated_export_username",
        namespace=namespace,
        realm=realm,
    )
    return {
        "contact_id": identifier,
        "native_username": username,
        "namespace": namespace,
        "realm": realm,
        "evidence": evidence,
    }


def publish_export_contacts(
    root: Path | str,
    *,
    source_id: str,
    snapshot_id: str,
    rows: list[dict],
    sender_identities: dict,
    message_proofs: list[dict],
    owner_account_id: str = "",
) -> dict:
    """Atomically append only caller-proven identities and source-scoped labels.

    ``sender_identities`` must come from paired fields in individual HTML frames,
    not from CSV aliases, filenames of the archive, or contact-set overlap. The
    caller owns message-to-archive verification; proofs must contain the exact
    source, message ID, text digest, timestamp, and original source record ID.
    """
    _exact(source_id, "source ID")
    _exact(snapshot_id, "snapshot ID")
    _exact(owner_account_id, "owner account ID", empty=True)
    if not isinstance(rows, list) or not isinstance(sender_identities, dict):
        raise ValueError("Export contacts require rows and a sender identity map.")
    if not isinstance(message_proofs, list):
        raise ValueError("Message proofs must be a list.")
    identities = {
        export_id: _paired_identity(export_id, value)
        for export_id, value in sender_identities.items()
    }
    input_digest = hashlib.sha256(
        _json([rows, sender_identities, message_proofs, owner_account_id]).encode()
    ).hexdigest()
    target = contact_registry_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    original = _database_identity(target)
    if any(value is not None for value in original[1:]):
        raise ValueError("Contact registry sidecars must be absent before publication.")
    counts = {}
    report = {
        "version": VERSION,
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "input_rows": len(rows),
        "paired_sender_identities": len(identities),
        "csv_known_native_string_matches": 0,
        "inserted": counts,
        "changed": False,
    }
    lock = target.open("rb") if original[0] is not None and sys.platform == "darwin" else None
    try:
        if lock is not None and sys.platform == "darwin":
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _database_identity(target) != original:
            raise ValueError("The contact registry changed before staging.")
        with tempfile.TemporaryDirectory(prefix=".export-contacts-", dir=target.parent) as stage:
            temporary = Path(stage) / "contacts.sqlite3"
            if original[0] is not None:
                _clone_database(target, temporary)
            if _database_identity(target) != original:
                raise ValueError("The contact registry changed during cloning.")
            with closing(sqlite3.connect(temporary)) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA journal_mode=DELETE")
                if original[0] is None:
                    initialize_contact_registry(connection)
                schema = connection.execute(
                    "SELECT value FROM contact_registry_info WHERE key='schema_version'"
                ).fetchone()
                if schema != ("2",):
                    raise ValueError("Export contacts require contact registry schema 2.")

                def insert(table, columns, keys, values):
                    _insert(
                        connection, table, columns.split(","),
                        keys.split(","), values, counts,
                    )

                def retain_identity(export_id, identity):
                    username = identity["native_username"]
                    previous = connection.execute(
                        "SELECT namespace,realm,username,kind,identity_status "
                        "FROM contacts WHERE id=?", (identity["contact_id"],),
                    ).fetchone()
                    kind = classify_wechat_identifier(username)["kind"]
                    if previous is not None:
                        if (previous[0], previous[1], previous[2], previous[4]) != (
                            identity["namespace"], identity["realm"], username, "verified",
                        ):
                            raise ValueError("Existing canonical contact identity conflicts.")
                        kind = previous[3]
                    insert(
                        "contacts", "id,namespace,realm,username,kind,identity_status", "id",
                        (
                            identity["contact_id"], identity["namespace"], identity["realm"],
                            username, kind, "verified",
                        ),
                    )
                    insert(
                        "contact_identifiers", "contact_id,namespace,value,realm,authority",
                        "contact_id,namespace,value,realm,authority",
                        (
                            identity["contact_id"], identity["namespace"],
                            username, identity["realm"],
                            "corroborated_export_username",
                        ),
                    )
                    insert(
                        "sender_bindings",
                        "source_id,sender_id,source_snapshot,room_id,"
                        "contact_id,authority,evidence",
                        "source_id,sender_id,source_snapshot,room_id",
                        (
                            source_id, export_id, snapshot_id, "", identity["contact_id"],
                            "corroborated_export_username", _evidence(identity["evidence"]),
                        ),
                    )

                seen_known = set()
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("CSV contact rows must be objects.")
                    export_id = _exact(row.get("export_id"), "CSV export ID")
                    if connection.execute(
                        "SELECT 1 FROM contacts WHERE namespace='wechat' AND realm='' "
                        "AND username=? AND identity_status='verified' LIMIT 1", (export_id,),
                    ).fetchone():
                        seen_known.add(export_id)
                for export_id, identity in identities.items():
                    retain_identity(export_id, identity)
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("CSV contact rows must be objects.")
                    export_id = _exact(row.get("export_id"), "CSV export ID")
                    digest = row.get("csv_sha256")
                    number = row.get("row_number")
                    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                        raise ValueError("Every CSV row requires its original file SHA-256.")
                    if type(number) is not int or number < 1:
                        raise ValueError("Every CSV row requires a positive row number.")
                    for field in ("label", "avatar_url", "tag_raw"):
                        if not isinstance(row.get(field), str) or "\0" in row[field]:
                            raise ValueError("CSV raw fields must be NUL-free strings.")
                    identity = identities.get(export_id)
                    if identity:
                        identifier = identity["contact_id"]
                    else:
                        identifier = source_contact_id(
                            source_id, export_id, source_snapshot=snapshot_id,
                        )
                        insert(
                            "contacts", "id,namespace,realm,username,kind,identity_status", "id",
                            (
                                identifier, "source_sender_id", source_id,
                                export_id, "unresolved", "source_scoped",
                            ),
                        )
                        insert(
                            "contact_identifiers", "contact_id,namespace,value,realm,authority",
                            "contact_id,namespace,value,realm,authority",
                            (
                                identifier, "export_alias_or_username", export_id,
                                source_id, "source_scoped_identifier",
                            ),
                        )
                        insert(
                            "sender_bindings",
                            "source_id,sender_id,source_snapshot,room_id,"
                            "contact_id,authority,evidence",
                            "source_id,sender_id,source_snapshot,room_id",
                            (
                                source_id, export_id, snapshot_id, "", identifier,
                                "source_scoped_identifier",
                                _json({"csv_sha256": digest, "field": "getWxName"}),
                            ),
                        )
                    observation = contact_observation(
                        identifier,
                        source_id=source_id,
                        source_snapshot=snapshot_id,
                        owner_account_id=owner_account_id,
                        kind="source_sender_label",
                        value=row["label"],
                        evidence={
                            "csv_sha256": digest, "row_number": number, "column_number": 2,
                            "export_id": export_id,
                            "raw": {
                                field: row[field]
                                for field in ("export_id", "label", "avatar_url", "tag_raw")
                            },
                        },
                    )
                    insert(
                        "contact_observations",
                        "id,contact_id,source_id,source_snapshot,"
                        "owner_account_id,room_id,kind,value,evidence",
                        "id", tuple(observation.values()),
                    )
                report["csv_known_native_string_matches"] = len(seen_known)
                for proof in message_proofs:
                    if not isinstance(proof, dict) or proof.get("source_id") != source_id:
                        raise ValueError("Message proofs must belong to this exact source.")
                    message_id = _exact(proof.get("message_id"), "message ID")
                    timestamp = _exact(proof.get("timestamp_value"), "timestamp")
                    record_id = _exact(proof.get("source_record_id"), "source record ID")
                    text_hash = proof.get("text_sha256")
                    if not isinstance(text_hash, str) or not _SHA256.fullmatch(text_hash):
                        raise ValueError("Message proofs require an exact text SHA-256.")
                    identity = identities.get(proof.get("sender_export_id"))
                    if identity is None:
                        matches = [
                            value for value in identities.values()
                            if value["native_username"] == proof.get("native_username")
                            and value["realm"] == proof.get("realm", "")
                        ]
                        if len({value["contact_id"] for value in matches}) == 1:
                            identity = matches[0]
                    if identity is None:
                        raise ValueError("Message proofs require a paired native sender identity.")
                    if (
                        proof.get("native_username", identity["native_username"])
                        != identity["native_username"]
                        or proof.get("realm", identity["realm"]) != identity["realm"]
                    ):
                        raise ValueError(
                            "Message proof native sender conflicts with its paired identity."
                        )
                    insert(
                        "message_contact_bindings",
                        "source_id,message_id,text_sha256,timestamp_value,"
                        "source_record_id,contact_id,authority,evidence",
                        "source_id,message_id,contact_id",
                        (
                            source_id, message_id, text_hash, timestamp,
                            record_id, identity["contact_id"],
                            MESSAGE_BINDING_AUTHORITY,
                            _evidence(
                                {
                                    "source_snapshot": snapshot_id,
                                    "pair": identity["evidence"],
                                    "message": json.loads(_evidence(proof.get("evidence"))),
                                }
                            ),
                        ),
                    )
                insert(
                    "contact_registry_info", "key,value", "key",
                    (
                        "export_contacts:" + hashlib.sha256(
                            (source_id + "\0" + snapshot_id).encode()
                        ).hexdigest(),
                        _json({"version": VERSION, "input_sha256": input_digest}),
                    ),
                )
                connection.commit()
            if _database_identity(target) != original:
                raise ValueError("The contact registry or sidecars changed before publication.")
            if counts:
                os.chmod(temporary, 0o600)
                with temporary.open("r+b") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                if os.name != "nt":
                    descriptor = os.open(target.parent, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                report["changed"] = True
        return report
    finally:
        if lock is not None:
            lock.close()
