"""Import portable WeChat observations without trusting foreign identity claims.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

import base64
from collections import Counter
from dataclasses import dataclass
import errno
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zlib

from .job_lock import CACHE_TASK_LOCK_NAME, CacheMaintenanceLock, CacheTaskLock
from .owner_only_files import ensure_owner_only
from .wechat_archive import (
    _SCHEMA,
    _file_identity,
    _source_exclusions,
    wechat_archive_path,
    wechat_history_partition_fingerprint,
)
from .wechat_export_merge import _clonefile, _manifest
from .wechat_completion_clone import (
    sync_completion_directory as _sync_directory,
    sync_completion_file as _sync,
)
from .wechat_exchange_rows import MAX_ROW_BYTES, encode_typed_row, typed_cell
from .wechat_shared_storage import shared_storage_installed


VERSION = "v1.0.0-codex.0"
MAX_METADATA_ROWS = 100_000
MINIMUM_FREE_BYTES = 256 * 1024**2
_SIDECARS = (
    "resource_references.sqlite3",
    "resource_revalidated.sqlite3",
    "media_events.sqlite3",
)
_STATE_FILES = (
    "history.sqlite3",
    "contacts.sqlite3",
    *_SIDECARS,
    "exclusions.json",
    "source_exclusions.json",
)
_MESSAGE_COLUMNS = (
    "id",
    "conversation_id",
    "ordinal",
    "sender_id",
    "sender_label",
    "direction",
    "text",
    "time_sort",
    "timestamp_value",
    "timestamp_label",
    "time_basis",
    "provenance",
    "quality_flags",
    "search_text",
)
_VISUAL_FIELDS = frozenset(
    {
        "message_type",
        "text_extraction_kind",
        "text_components",
        "quoted_reference",
        "appmsg_subtype",
        "forwarded_record_title",
        "omitted_forwarded_items",
        "resource_kind",
        "payment_card_fields",
        "forwarded_components",
        "quoted_components",
    }
)
_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange_bundles (
 package_id TEXT PRIMARY KEY, origin_id TEXT NOT NULL, manifest TEXT NOT NULL,
 imported_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS exchange_original_rows (
 origin_id TEXT NOT NULL, table_name TEXT NOT NULL, original_key TEXT NOT NULL,
 row_sha256 TEXT NOT NULL, row_payload BLOB NOT NULL,
 PRIMARY KEY(origin_id,table_name,original_key,row_sha256)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS exchange_bundle_rows (
 package_id TEXT NOT NULL, origin_id TEXT NOT NULL, table_name TEXT NOT NULL,
 original_key TEXT NOT NULL, row_sha256 TEXT NOT NULL,
 PRIMARY KEY(package_id,origin_id,table_name,original_key,row_sha256)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS exchange_row_mappings (
 origin_id TEXT NOT NULL, table_name TEXT NOT NULL, original_key TEXT NOT NULL,
 row_sha256 TEXT NOT NULL, local_table TEXT NOT NULL, local_id TEXT NOT NULL,
 partition_id TEXT NOT NULL,
 PRIMARY KEY(origin_id,table_name,original_key,row_sha256,local_table,local_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS exchange_mapping_partition ON exchange_row_mappings(partition_id);
"""


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(*values):
    return hashlib.sha256(_json(values).encode()).hexdigest()


def _safe(path):
    path = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Exchange import paths cannot contain symbolic links.")
    return path


def _resident(path, *, optional=False):
    path = _safe(path)
    if not os.path.lexists(path):
        if optional:
            return None
        raise ValueError("An exchange publication input is missing.")
    state = path.lstat()
    if not stat.S_ISREG(state.st_mode) or getattr(state, "st_flags", 0) & 0x40000000:
        raise ValueError("Exchange inputs must be resident regular files.")
    if any(
        os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("Finish database journals before exchange publication.")
    return _file_identity(path)


def archive_state(local_store_root):
    """Return exact target pins used by a preview and its later commit."""
    directory = _safe(wechat_archive_path(local_store_root)).parent
    result = {}
    for name in _STATE_FILES:
        path = directory / name
        if not os.path.lexists(path):
            result[name] = None
        else:
            _safe(path)
            state = path.lstat()
            result[name] = {
                **_file_identity(path),
                "ctime_ns": state.st_ctime_ns,
                "cloud_only": bool(getattr(state, "st_flags", 0) & 0x40000000),
            }
    return result


def _cell(value):
    return typed_cell(value)


def _encoded_row(row, columns):
    return encode_typed_row(row, columns)


def decode_preserved_row(payload):
    """Decode a bounded exact typed row for a subsequent lossless export."""
    if not isinstance(payload, bytes) or len(payload) > MAX_ROW_BYTES:
        raise ValueError("Invalid preserved row payload.")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(payload, MAX_ROW_BYTES + 1)
    if (
        len(raw) > MAX_ROW_BYTES
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise ValueError("A preserved row has an invalid compression envelope.")
    values = json.loads(raw)
    if not isinstance(values, list) or len(values) > 256:
        raise ValueError("A preserved row has an invalid typed schema.")
    result = {}
    for item in values:
        if (
            not isinstance(item, list)
            or len(item) != 3
            or not isinstance(item[0], str)
            or item[0] in result
        ):
            raise ValueError("A preserved row has ambiguous columns.")
        name, kind, value = item
        if kind == "null" and value is None:
            decoded = None
        elif kind == "integer" and isinstance(value, str):
            decoded = int(value)
            if str(decoded) != value or not -(2**63) <= decoded < 2**63:
                raise ValueError("A preserved integer is not an exact SQLite integer.")
        elif kind == "real" and isinstance(value, str):
            decoded = float.fromhex(value)
            if not math.isfinite(decoded) or decoded.hex() != value:
                raise ValueError("A preserved real is not an exact finite SQLite real.")
        elif kind == "text" and isinstance(value, str):
            decoded = value
        elif kind == "blob" and isinstance(value, str):
            decoded = base64.b64decode(value, validate=True)
        else:
            raise ValueError("A preserved cell has an unsupported type.")
        result[name] = decoded
    if _encoded_row(result, result) != raw:
        raise ValueError("A preserved row is not canonically encoded.")
    return result


def _key(row, descriptor):
    columns = descriptor.get("primary_key") or (["id"] if "id" in row else list(row))
    if (
        not isinstance(columns, list)
        or not columns
        or any(column not in row for column in columns)
    ):
        raise ValueError("An exchange row has an incomplete original key.")
    return _encoded_row(row, columns).decode()


def _archive_row(connection, origin, name, row, descriptor, package_id, report):
    columns = [column["name"] for column in descriptor["columns"]]
    if set(row) != set(columns):
        raise ValueError("An exchange row differs from its verified columns.")
    encoded = _encoded_row(row, columns)
    key, digest = _key(row, descriptor), hashlib.sha256(encoded).hexdigest()
    prior = connection.execute(
        "SELECT row_payload FROM exchange_original_rows WHERE origin_id=? AND table_name=? AND original_key=? AND row_sha256=?",
        (origin, name, key, digest),
    ).fetchone()
    if prior is None:
        conflict = connection.execute(
            "SELECT 1 FROM exchange_original_rows WHERE origin_id=? AND table_name=? AND original_key=? LIMIT 1",
            (origin, name, key),
        ).fetchone()
        payload = zlib.compress(encoded, 6)
        connection.execute(
            "INSERT INTO exchange_original_rows VALUES(?,?,?,?,?)",
            (origin, name, key, digest, payload),
        )
        report["preserved_rows"] += 1
        if conflict:
            report["conflict_variants"] += 1
    elif decode_preserved_row(prior[0]) != row:
        raise ValueError("An existing preserved row does not match its content digest.")
    connection.execute(
        "INSERT OR IGNORE INTO exchange_bundle_rows VALUES(?,?,?,?,?)",
        (package_id, origin, name, key, digest),
    )
    return key, digest


def _mapped(connection, origin, table, key, digest, local_table, identifier, partition):
    connection.execute(
        "INSERT OR IGNORE INTO exchange_row_mappings VALUES(?,?,?,?,?,?,?)",
        (origin, table, key, digest, local_table, identifier, partition),
    )


def _existing_table(connection, name):
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _selected_packages(connection, partition_ids, conversation_ids):
    selected = list(dict.fromkeys(conversation_ids))
    partitions = set(partition_ids)
    if not selected or len(selected) > MAX_METADATA_ROWS:
        raise ValueError(
            "Preserved exchange export requires an explicit conversation scope."
        )
    packages, imported_scope = set(), False
    for offset in range(0, len(selected), 400):
        batch = selected[offset : offset + 400]
        cursor = connection.execute(
            "SELECT DISTINCT b.package_id,p.manifest,r.row_payload,m.partition_id FROM exchange_row_mappings m "
            "JOIN exchange_original_rows r USING(origin_id,table_name,original_key,row_sha256) "
            "JOIN exchange_bundle_rows b USING(origin_id,table_name,original_key,row_sha256) "
            "JOIN exchange_bundles p ON p.package_id=b.package_id "
            "WHERE m.local_table='conversations' AND m.local_id IN ("
            + ",".join("?" for _ in batch)
            + ")",
            batch,
        )
        for package, manifest, payload, partition in cursor:
            if partition not in partitions:
                continue
            imported_scope = True
            original = decode_preserved_row(payload)
            scope = json.loads(manifest).get("scope", {})
            if partition in partitions and original.get("id") in scope.get(
                "conversation_ids", ()
            ):
                packages.add(package)
    if imported_scope and not packages:
        raise ValueError(
            "This imported dependency has no independently exportable root scope."
        )
    return sorted(packages)


def preserved_rows(connection, partition_ids, *, conversation_ids):
    """Yield immutable foreign envelopes associated with selected imported sources."""
    if not _existing_table(connection, "exchange_row_mappings"):
        return
    packages = _preserved_packages(connection, partition_ids, conversation_ids)
    for package in packages:
        cursor = connection.execute(
            "SELECT r.origin_id,r.table_name,r.original_key,r.row_sha256,r.row_payload FROM exchange_original_rows r "
            "JOIN exchange_bundle_rows b USING(origin_id,table_name,original_key,row_sha256) "
            "WHERE b.package_id=? ORDER BY r.origin_id,r.table_name,r.original_key,r.row_sha256",
            (package,),
        )
        for row in cursor:
            yield dict(
                zip(
                    (
                        "origin_id",
                        "table_name",
                        "original_key",
                        "row_sha256",
                        "row_payload",
                    ),
                    tuple(row),
                    strict=True,
                )
            )


def preserved_manifests(connection, partition_ids, *, conversation_ids):
    """Retain origin manifests instead of silently recasting an imported origin."""
    if not _existing_table(connection, "exchange_row_mappings"):
        return
    packages = _preserved_packages(connection, partition_ids, conversation_ids)
    emitted = set()
    for package in packages:
        row = connection.execute(
            "SELECT package_id,manifest FROM exchange_bundles WHERE package_id=?",
            (package,),
        ).fetchone()
        if row and row[0] not in emitted:
            emitted.add(row[0])
            yield {"package_id": row[0], "manifest": row[1]}


def _preserved_packages(connection, partition_ids, conversation_ids):
    packages = set(_selected_packages(connection, partition_ids, conversation_ids))
    pending = list(packages)
    while pending:
        package = pending.pop()
        for row in connection.execute(
            "SELECT r.row_payload FROM exchange_original_rows r JOIN exchange_bundle_rows b "
            "USING(origin_id,table_name,original_key,row_sha256) "
            "WHERE b.package_id=? AND r.table_name='preserved.bundles'",
            (package,),
        ):
            original = decode_preserved_row(row[0])
            identifier = original["package_id"]
            if identifier not in packages:
                if len(packages) >= MAX_METADATA_ROWS:
                    raise ValueError("Nested exchange manifests exceed their budget.")
                if not connection.execute(
                    "SELECT 1 FROM exchange_bundles WHERE package_id=?", (identifier,)
                ).fetchone():
                    raise ValueError("A preserved origin manifest is missing.")
                packages.add(identifier)
                pending.append(identifier)
    return sorted(packages)


def preserved_memberships(connection, partition_ids, *, conversation_ids):
    """Export exact original package membership, including nested origin packages."""
    if not _existing_table(connection, "exchange_row_mappings"):
        return
    for package in _preserved_packages(connection, partition_ids, conversation_ids):
        manifest = json.loads(
            connection.execute(
                "SELECT manifest FROM exchange_bundles WHERE package_id=?", (package,)
            ).fetchone()[0]
        )
        for row in connection.execute(
            "SELECT package_id,origin_id,table_name,original_key,row_sha256 FROM exchange_bundle_rows "
            "WHERE package_id=? AND origin_id=? ORDER BY table_name,original_key,row_sha256",
            (package, manifest["origin_id"]),
        ):
            yield dict(
                zip(
                    (
                        "package_id",
                        "origin_id",
                        "table_name",
                        "original_key",
                        "row_sha256",
                    ),
                    tuple(row),
                    strict=True,
                )
            )


def _readonly(path):
    connection = sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    return connection


def _expanded_bytes(verified):
    """Measure decoded typed rows instead of trusting a compression ratio."""
    total = 0
    for name, descriptor in verified.tables.items():
        columns = [column["name"] for column in descriptor["columns"]]
        for row in verified.iter_rows(name):
            total += len(_encoded_row(row, columns))
    return total


def _space(directory, minimum, reserve):
    if type(minimum) is not int or minimum < 0:
        raise ValueError("The exchange free-space floor must be nonnegative.")
    if shutil.disk_usage(directory).free < minimum + reserve:
        raise OSError(
            errno.ENOSPC, "Exchange staging exceeds the available disk-space budget."
        )


def _no_input_writers(path):
    if os.name == "nt":
        from .windows_file_metadata import has_file_writers

        if has_file_writers(path):
            raise RuntimeError(
                "A process currently owns an exchange publication input for writing."
            )
        return
    result = subprocess.run(
        ["lsof", "+c", "0", "-nP", "-Fpcfa", "--", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError(
            "Open-file inspection failed before exchange publication."
        )
    command = ""
    for line in result.stdout.splitlines():
        if line.startswith("c"):
            command = line[1:].casefold()
        elif line in {"aw", "au"} and command not in {"fileproviderd", "fileprovider"}:
            raise RuntimeError(
                "A process currently owns an exchange publication input for writing."
            )


def _clone(path, prefix):
    descriptor, name = tempfile.mkstemp(
        prefix=prefix, suffix=".sqlite3", dir=path.parent
    )
    os.close(descriptor)
    candidate = Path(name)
    try:
        before = _resident(path)
        if sys.platform == "darwin":
            candidate.unlink()
            try:
                _clonefile(path, candidate)
            except ValueError as error:
                raise RuntimeError(
                    "This volume cannot create the required APFS exchange rollback clone."
                ) from error
        else:
            # Other filesystems need a separately budgeted, exact full copy.
            ensure_owner_only(candidate)
            shutil.copyfile(path, candidate)
            if _file_sha256(candidate) != _file_sha256(path):
                raise ValueError(
                    "The complete exchange rollback copy failed its hash readback."
                )
        ensure_owner_only(candidate)
        if _resident(path) != before or candidate.stat().st_dev != path.stat().st_dev:
            raise ValueError(
                "A database changed while making its same-volume APFS clone."
            )
        return candidate
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def _guards(connection):
    shared = shared_storage_installed(connection)
    tables = ["partitions", "conversations", "messages"]
    if shared:
        tables[-1] = "message_storage"
        tables.append("shared_message_bodies")
    for table in tables:
        connection.execute(
            f"CREATE TEMP TRIGGER exchange_no_delete_{table} BEFORE DELETE ON main.{table} BEGIN SELECT RAISE(ABORT,'Exchange cannot delete retained rows'); END"
        )
        if table != "conversations":
            connection.execute(
                f"CREATE TEMP TRIGGER exchange_no_update_{table} BEFORE UPDATE ON main.{table} BEGIN SELECT RAISE(ABORT,'Exchange cannot update retained rows'); END"
            )
    connection.execute(
        "CREATE TEMP TABLE exchange_touched_conversations(id TEXT PRIMARY KEY) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE TEMP TRIGGER exchange_conversation_guard BEFORE UPDATE ON main.conversations WHEN OLD.id NOT IN (SELECT id FROM exchange_touched_conversations) OR OLD.id IS NOT NEW.id OR OLD.partition_id IS NOT NEW.partition_id OR OLD.account_key IS NOT NEW.account_key OR OLD.peer_id IS NOT NEW.peer_id OR OLD.title IS NOT NEW.title OR OLD.kind IS NOT NEW.kind BEGIN SELECT RAISE(ABORT,'Exchange cannot alter retained conversation identity'); END"
    )


def _text(value, name, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or "\0" in value:
        raise ValueError(f"A materialized exchange {name} must be a literal string.")
    return value


def _project_values(
    original,
    *,
    conversation,
    identifier,
    origin,
    package_id,
    original_table,
    presentation=None,
):
    """Build the same safe immutable projection for import and forwarding checks."""
    digest = hashlib.sha256(_encoded_row(original, _MESSAGE_COLUMNS)).hexdigest()
    for column in (
        "id",
        "conversation_id",
        "sender_label",
        "direction",
        "text",
        "timestamp_value",
        "timestamp_label",
        "time_basis",
        "provenance",
        "quality_flags",
        "search_text",
    ):
        _text(original[column], column)
    _text(original["sender_id"], "sender ID", nullable=True)
    if (
        type(original["ordinal"]) is not int
        or not isinstance(original["time_sort"], (int, float))
        or not math.isfinite(original["time_sort"])
    ):
        raise ValueError("An exchange message has an invalid source clock or ordinal.")
    try:
        raw_provenance = json.loads(original["provenance"])
    except (ValueError, TypeError):
        raw_provenance = {}
    provenance = (
        {key: value for key, value in raw_provenance.items() if key in _VISUAL_FIELDS}
        if isinstance(raw_provenance, dict)
        else {}
    )
    provenance["exchange"] = {
        "package_id": package_id,
        "origin_id": origin,
        "original_message_id": original["id"],
        "original_conversation_id": original["conversation_id"],
        "origin_table": original_table,
        "original_row_sha256": digest,
        "identity_authority": "external_origin_claim",
        "local_resources_available": False,
        "original_provenance": raw_provenance,
        "original_provenance_raw": original["provenance"],
    }
    if presentation is not None:
        if presentation[1]["source_provenance"] != original["provenance"]:
            raise ValueError(
                "An imported display snapshot has a different source message."
            )
        provenance["exchange"]["viewer_display_name"] = presentation[0]
        provenance["exchange"]["frozen_presentation"] = presentation[1]
    try:
        flags = json.loads(original["quality_flags"])
    except (ValueError, TypeError):
        flags = []
    flags = (
        flags
        if isinstance(flags, list) and all(isinstance(item, str) for item in flags)
        else []
    )
    values = [original[column] for column in _MESSAGE_COLUMNS]
    values[0], values[1], values[11], values[12] = (
        identifier,
        conversation,
        _json(provenance),
        _json([*flags, "external_origin_claim"]),
    )
    if presentation is not None and presentation[0]:
        values[4] = presentation[0]
    return values


def _materialize(connection, verified, report):
    origin = verified.manifest["origin_id"]
    partitions, conversations = {}, {}
    for row in verified.iter_rows("history.partitions"):
        if len(partitions) >= MAX_METADATA_ROWS:
            raise ValueError("Exchange sources exceed the bounded metadata budget.")
        original = _text(row["id"], "partition ID")
        source = (
            "xchg-"
            + _digest(origin, original, row.get("account_key"), row.get("account_id"))[
                :64
            ]
        )
        account_key = "source:" + source
        values = (
            source,
            account_key,
            None,
            _text(row["account_label"], "account label"),
            "Imported origin claim",
            "Exchange origin " + origin[:8],
            "",
            "",
        )
        prior = connection.execute(
            "SELECT * FROM partitions WHERE id=?", (source,)
        ).fetchone()
        if prior is None:
            connection.execute("INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)", values)
            report["added_partitions"] += 1
        elif tuple(prior[:3]) != values[:3] or prior[4] != values[4]:
            raise ValueError(
                "An imported source collides with an existing local source."
            )
        partitions[original] = (source, account_key)
        descriptor = verified.tables["history.partitions"]
        encoded = _encoded_row(row, [item["name"] for item in descriptor["columns"]])
        _mapped(
            connection,
            origin,
            "history.partitions",
            _key(row, descriptor),
            hashlib.sha256(encoded).hexdigest(),
            "partitions",
            source,
            source,
        )
    for name in (
        "history.conversations",
        "resource.conversations",
        "resource_revalidated.conversations",
        "media_events.conversations",
    ):
        for row in verified.iter_rows(name):
            if len(conversations) >= MAX_METADATA_ROWS:
                raise ValueError(
                    "Exchange conversations exceed the bounded metadata budget."
                )
            original = _text(row["id"], "conversation ID")
            source, account_key = partitions.get(row["partition_id"], (None, None))
            if source is None:
                raise ValueError(
                    "An exchange conversation has no original account partition."
                )
            peer, kind = (
                _text(row["peer_id"], "peer ID", nullable=True),
                _text(row["kind"], "conversation kind"),
            )
            identifier = "wechat:" + _digest(origin, source, original, peer, kind)
            values = (
                identifier,
                source,
                account_key,
                peer,
                _text(row["title"], "conversation title"),
                kind,
                0,
                0.0,
                0.0,
                "",
                "",
                "",
            )
            prior = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (identifier,)
            ).fetchone()
            if prior is None:
                connection.execute(
                    "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", values
                )
                report["inserted_conversations"] += 1
            elif tuple(prior[1:4]) != values[1:4] or prior[5] != kind:
                raise ValueError(
                    "An imported conversation collides with local identity."
                )
            conversations[(name.split(".", 1)[0], original)] = (identifier, source)
            descriptor = verified.tables[name]
            encoded = _encoded_row(
                row, [item["name"] for item in descriptor["columns"]]
            )
            _mapped(
                connection,
                origin,
                name,
                _key(row, descriptor),
                hashlib.sha256(encoded).hexdigest(),
                "conversations",
                identifier,
                source,
            )
    presentations = {}
    for row in verified.iter_rows("exchange.presentations"):
        identifier = _text(row["message_id"], "presentation message ID")
        name = _text(row["viewer_display_name"], "presentation viewer name")
        metadata = json.loads(row["metadata_json"])
        if (
            not isinstance(metadata, dict)
            or metadata.get("schema_version") != 1
            or metadata.get("authority") != "frozen_external_presentation"
            or metadata.get("source_message_id") != identifier
            or not isinstance(metadata.get("source_provenance"), str)
            or not isinstance(metadata.get("forwarded_items"), list)
            or not isinstance(metadata.get("event_members"), list)
            or identifier in presentations
        ):
            raise ValueError(
                "An imported display snapshot has ambiguous message scope."
            )
        presentations[identifier] = (name, metadata)
    touched, conflicts = set(), set()
    for name in ("exchange.timeline",):
        descriptor = verified.tables.get(name)
        if descriptor is None:
            continue
        for row in verified.iter_rows(name):
            original_table = row.get("origin_table", "history.messages")
            if original_table not in {
                "history.messages",
                "resource.messages",
                "resource_revalidated.messages",
                "media_events.messages",
            }:
                raise ValueError(
                    "An exchange timeline refers to an unsupported origin table."
                )
            original = {column: row[column] for column in _MESSAGE_COLUMNS}
            prefix = original_table.split(".", 1)[0]
            mapped = conversations.get(
                (prefix, original["conversation_id"])
            ) or conversations.get(("history", original["conversation_id"]))
            if mapped is None:
                raise ValueError("An exchange message has no preserved conversation.")
            conversation, partition = mapped
            encoded = _encoded_row(original, _MESSAGE_COLUMNS)
            digest = hashlib.sha256(encoded).hexdigest()
            identifier = _digest(
                "exchange-message", origin, conversation, original["id"], digest
            )
            if connection.execute(
                "SELECT 1 FROM messages WHERE id=?", (identifier,)
            ).fetchone():
                report["replayed_messages"] += 1
                continue
            original_key = _key(row, descriptor)
            if connection.execute(
                "SELECT 1 FROM exchange_row_mappings WHERE origin_id=? AND table_name=? AND original_key=? AND local_table='messages' LIMIT 1",
                (origin, name, original_key),
            ).fetchone():
                conflicts.add((origin, conversation, original["id"]))
            values = _project_values(
                original,
                conversation=conversation,
                identifier=identifier,
                origin=origin,
                package_id=verified.package_id,
                original_table=original_table,
                presentation=presentations.get(original["id"]),
            )
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
            )
            report["inserted_messages"] += 1
            touched.add(conversation)
            row_encoded = _encoded_row(
                row, [item["name"] for item in descriptor["columns"]]
            )
            _mapped(
                connection,
                origin,
                name,
                _key(row, descriptor),
                hashlib.sha256(row_encoded).hexdigest(),
                "messages",
                identifier,
                partition,
            )
            actual = connection.execute(
                "SELECT * FROM messages WHERE id=?", (identifier,)
            ).fetchone()
            if tuple(actual) != tuple(values):
                raise ValueError(
                    "A materialized message failed its exact staged readback."
                )
    for identifier in touched:
        connection.execute(
            "INSERT INTO exchange_touched_conversations VALUES(?)", (identifier,)
        )
        count = connection.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (identifier,)
        ).fetchone()[0]
        first = connection.execute(
            "SELECT time_sort,timestamp_label FROM messages WHERE conversation_id=? AND time_sort>0 ORDER BY time_sort,ordinal,id LIMIT 1",
            (identifier,),
        ).fetchone()
        last = connection.execute(
            "SELECT time_sort,timestamp_label,text FROM messages WHERE conversation_id=? ORDER BY time_sort DESC,ordinal DESC,id DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        connection.execute(
            "UPDATE conversations SET message_count=?,first_sort=?,last_sort=?,first_time=?,last_time=?,latest_text=? WHERE id=?",
            (
                count,
                first[0] if first else 0.0,
                last[0],
                first[1] if first else "",
                last[1],
                last[2],
                identifier,
            ),
        )
    report["source_count"], report["account_count"] = len(partitions), len(partitions)
    report["conflicting_messages"] = len(conflicts)


@dataclass(frozen=True)
class _ForwardSource:
    """Read a precise immutable origin graph through its package memberships."""

    connection: sqlite3.Connection
    manifest: dict
    selected_original_session_id: str

    @property
    def package_id(self):
        return self.manifest["package_id"]

    @property
    def tables(self):
        return {item["name"]: item for item in self.manifest["tables"]}

    @property
    def local_partition_ids(self):
        return tuple(
            "xchg-"
            + _digest(
                self.manifest["origin_id"],
                row["id"],
                row.get("account_key"),
                row.get("account_id"),
            )[:64]
            for row in self.iter_rows("history.partitions")
        )

    @property
    def local_conversation_ids(self):
        partitions = {
            row["id"]: "xchg-"
            + _digest(
                self.manifest["origin_id"],
                row["id"],
                row.get("account_key"),
                row.get("account_id"),
            )[:64]
            for row in self.iter_rows("history.partitions")
        }
        return tuple(
            sorted(
                {
                    "wechat:"
                    + _digest(
                        self.manifest["origin_id"],
                        partitions[row["partition_id"]],
                        row["id"],
                        row["peer_id"],
                        row["kind"],
                    )
                    for table in (
                        "history.conversations",
                        "resource.conversations",
                        "resource_revalidated.conversations",
                        "media_events.conversations",
                    )
                    for row in self.iter_rows(table)
                }
            )
        )

    def iter_rows(self, table_name):
        for row in self.connection.execute(
            "SELECT r.row_payload FROM exchange_original_rows r JOIN exchange_bundle_rows b "
            "USING(origin_id,table_name,original_key,row_sha256) "
            "WHERE b.package_id=? AND b.origin_id=? AND b.table_name=? "
            "ORDER BY r.original_key,r.row_sha256",
            (self.package_id, self.manifest["origin_id"], table_name),
        ):
            yield decode_preserved_row(row[0])


def fast_forward_source(connection, session_id, *, account="", kind="all"):
    """Forward only an unchanged, flat imported graph without wrapper growth.

    Exact projection checks also protect local titles, labels, clocks, and edits.
    Returning None retains the ordinary bounded export path for mixed or edited
    scopes. No local row is updated and claimed identity remains foreign.
    """
    from .wechat_exchange import validate_preserved_closure

    if not _existing_table(connection, "exchange_bundles"):
        return None
    current = connection.execute(
        "SELECT c.id,c.partition_id,c.account_key,c.kind,p.account_id,p.identity_status "
        "FROM conversations c JOIN partitions p ON p.id=c.partition_id WHERE c.id=?",
        (session_id,),
    ).fetchone()
    if (
        current is None
        or current[4] is not None
        or current[5] != "Imported origin claim"
        or not current[1].startswith("xchg-")
        or account
        and account != current[2]
        or kind != "all"
        and kind != current[3]
    ):
        return None
    anchor = connection.execute(
        "SELECT provenance FROM messages WHERE conversation_id=? ORDER BY time_sort,ordinal,id LIMIT 1",
        (session_id,),
    ).fetchone()
    if anchor is None:
        return None
    try:
        package_id = json.loads(anchor[0])["exchange"]["package_id"]
        row = connection.execute(
            "SELECT manifest FROM exchange_bundles WHERE package_id=?", (package_id,)
        ).fetchone()
        if row is None:
            return None
        manifest = json.loads(row[0])
        if any(table["name"].startswith("preserved.") for table in manifest["tables"]):
            return None
        original_session = None
        for (key,) in connection.execute(
            "SELECT original_key FROM exchange_row_mappings WHERE origin_id=? "
            "AND local_table='conversations' AND table_name IN ('history.conversations','resource.conversations','resource_revalidated.conversations','media_events.conversations') AND local_id=?",
            (manifest["origin_id"], session_id),
        ):
            for field, cell_kind, value in json.loads(key):
                if (
                    field == "id"
                    and cell_kind == "text"
                    and value in manifest["scope"]["conversation_ids"]
                ):
                    original_session = value
        if original_session is None:
            return None
        source = _ForwardSource(connection, manifest, original_session)
        validate_preserved_closure(manifest, source.iter_rows)
        origin, partitions, conversations = manifest["origin_id"], {}, {}
        for original in source.iter_rows("history.partitions"):
            identifier = (
                "xchg-"
                + _digest(
                    origin,
                    original["id"],
                    original.get("account_key"),
                    original.get("account_id"),
                )[:64]
            )
            expected = (
                identifier,
                "source:" + identifier,
                None,
                original["account_label"],
                "Imported origin claim",
                "Exchange origin " + origin[:8],
                "",
                "",
            )
            actual = connection.execute(
                "SELECT * FROM partitions WHERE id=?", (identifier,)
            ).fetchone()
            if actual is None or tuple(actual) != expected:
                return None
            partitions[original["id"]] = identifier
        local_conversations = {}
        for name in (
            "history.conversations",
            "resource.conversations",
            "resource_revalidated.conversations",
            "media_events.conversations",
        ):
            for original in source.iter_rows(name):
                partition = partitions[original["partition_id"]]
                identifier = "wechat:" + _digest(
                    origin,
                    partition,
                    original["id"],
                    original["peer_id"],
                    original["kind"],
                )
                expected_identity = (
                    identifier,
                    partition,
                    "source:" + partition,
                    original["peer_id"],
                    original["title"],
                    original["kind"],
                )
                prior = local_conversations.setdefault(identifier, expected_identity)
                if (
                    prior[:4] != expected_identity[:4]
                    or prior[5] != expected_identity[5]
                ):
                    return None
                conversations[(name.split(".", 1)[0], original["id"])] = identifier
        presentations = {
            row["message_id"]: (
                row["viewer_display_name"],
                json.loads(row["metadata_json"]),
            )
            for row in source.iter_rows("exchange.presentations")
        }
        counts = Counter()
        first, last = {}, {}
        for row in source.iter_rows("exchange.timeline"):
            original = {column: row[column] for column in _MESSAGE_COLUMNS}
            table = row["origin_table"]
            identifier = (
                conversations.get((table.split(".", 1)[0], original["conversation_id"]))
                or conversations[("history", original["conversation_id"])]
            )
            digest = hashlib.sha256(
                _encoded_row(original, _MESSAGE_COLUMNS)
            ).hexdigest()
            message_id = _digest(
                "exchange-message", origin, identifier, original["id"], digest
            )
            expected = _project_values(
                original,
                conversation=identifier,
                identifier=message_id,
                origin=origin,
                package_id=package_id,
                original_table=table,
                presentation=presentations.get(original["id"]),
            )
            actual = connection.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if actual is None or tuple(actual) != tuple(expected):
                return None
            counts[identifier] += 1
            order = (expected[7], expected[2], expected[0])
            if expected[7] > 0 and (
                identifier not in first or order < first[identifier][0]
            ):
                first[identifier] = (order, expected[9])
            if identifier not in last or order > last[identifier][0]:
                last[identifier] = (order, expected[9], expected[6])
        for identifier, identity in local_conversations.items():
            actual = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (identifier,)
            ).fetchone()
            if actual is None or tuple(actual[:6]) != identity:
                return None
            counter = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (identifier,)
            ).fetchone()[0]
            if counter != counts[identifier]:
                return None
            first_row, last_row = first.get(identifier), last.get(identifier)
            statistics = (
                counter,
                first_row[0][0] if first_row else 0.0,
                last_row[0][0] if last_row else 0.0,
                first_row[1] if first_row else "",
                last_row[1] if last_row else "",
                last_row[2] if last_row else "",
            )
            if tuple(actual[6:]) != statistics:
                return None
        return source
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        return None


def _preserved_envelopes(connection, verified, report):
    from .wechat_exchange import _validate_manifest, validate_preserved_closure

    connection.execute(
        "CREATE TEMP TABLE exchange_incoming_rows(origin_id TEXT,table_name TEXT,original_key TEXT,row_sha256 TEXT,PRIMARY KEY(origin_id,table_name,original_key,row_sha256)) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE TEMP TABLE exchange_incoming_memberships(package_id TEXT,origin_id TEXT,table_name TEXT,original_key TEXT,row_sha256 TEXT,PRIMARY KEY(package_id,origin_id,table_name,original_key,row_sha256)) WITHOUT ROWID"
    )

    manifests = []
    for row in verified.iter_rows("preserved.bundles"):
        if set(row) != {"package_id", "manifest"}:
            raise ValueError("A preserved foreign manifest has an unsupported schema.")
        manifest = json.loads(row["manifest"])
        _validate_manifest(manifest)
        if manifest["package_id"] != row["package_id"]:
            raise ValueError(
                "A preserved foreign manifest has inconsistent identity claims."
            )
        manifests.append((row, manifest))
    for row in verified.iter_rows("preserved.original_rows"):
        if set(row) != {
            "origin_id",
            "table_name",
            "original_key",
            "row_sha256",
            "row_payload",
        }:
            raise ValueError("A preserved foreign envelope has an unsupported schema.")
        decoded = decode_preserved_row(row["row_payload"])
        encoded = _encoded_row(decoded, decoded)
        if hashlib.sha256(encoded).hexdigest() != row["row_sha256"]:
            raise ValueError("A preserved foreign row differs from its declared hash.")
        for name in ("origin_id", "table_name", "original_key", "row_sha256"):
            _text(row[name], name)
        descriptors = [
            table
            for _row, manifest in manifests
            if manifest["origin_id"] == row["origin_id"]
            for table in manifest["tables"]
            if table["name"] == row["table_name"]
        ]
        if not any(
            list(decoded) == [column["name"] for column in descriptor["columns"]]
            and _key(decoded, descriptor) == row["original_key"]
            and all(
                _cell(decoded[column["name"]])[0] in {"null", column["type"]}
                for column in descriptor["columns"]
            )
            for descriptor in descriptors
        ):
            raise ValueError(
                "A preserved foreign row lacks its exact origin schema and original key."
            )
        cursor = connection.execute(
            "INSERT OR IGNORE INTO exchange_original_rows VALUES(?,?,?,?,?)",
            tuple(
                row[name]
                for name in (
                    "origin_id",
                    "table_name",
                    "original_key",
                    "row_sha256",
                    "row_payload",
                )
            ),
        )
        report["preserved_rows"] += cursor.rowcount
        connection.execute(
            "INSERT INTO exchange_incoming_rows VALUES(?,?,?,?)",
            tuple(
                row[name]
                for name in ("origin_id", "table_name", "original_key", "row_sha256")
            ),
        )
    by_package = {manifest["package_id"]: manifest for _row, manifest in manifests}
    for row in verified.iter_rows("preserved.memberships"):
        fields = ("package_id", "origin_id", "table_name", "original_key", "row_sha256")
        if (
            set(row) != set(fields)
            or row["package_id"] not in by_package
            or row["origin_id"] != by_package[row["package_id"]]["origin_id"]
        ):
            raise ValueError(
                "A preserved row membership lacks its exact original manifest."
            )
        values = tuple(row[name] for name in fields)
        if not connection.execute(
            "SELECT 1 FROM exchange_incoming_rows WHERE origin_id=? AND table_name=? AND original_key=? AND row_sha256=?",
            values[1:],
        ).fetchone():
            raise ValueError("A preserved package member lacks its exact original row.")
        connection.execute(
            "INSERT INTO exchange_incoming_memberships VALUES(?,?,?,?,?)", values
        )
    if connection.execute(
        "SELECT 1 FROM exchange_incoming_rows r WHERE NOT EXISTS(SELECT 1 FROM exchange_incoming_memberships b WHERE b.origin_id=r.origin_id AND b.table_name=r.table_name AND b.original_key=r.original_key AND b.row_sha256=r.row_sha256) LIMIT 1"
    ).fetchone():
        raise ValueError(
            "A preserved original row is not bound to its original package."
        )
    for _row, manifest in manifests:

        def rows_by_table(table_name, package_id=manifest["package_id"]):
            cursor = connection.execute(
                "SELECT r.row_payload FROM exchange_original_rows r JOIN exchange_incoming_memberships b "
                "USING(origin_id,table_name,original_key,row_sha256) WHERE b.package_id=? AND b.table_name=? "
                "ORDER BY r.original_key,r.row_sha256",
                (package_id, table_name),
            )
            for item in cursor:
                yield decode_preserved_row(item[0])

        validate_preserved_closure(manifest, rows_by_table)
    connection.execute(
        "INSERT OR IGNORE INTO exchange_bundle_rows SELECT * FROM exchange_incoming_memberships"
    )
    for row, manifest in manifests:
        connection.execute(
            "INSERT OR IGNORE INTO exchange_bundles VALUES(?,?,?,?)",
            (
                row["package_id"],
                manifest["origin_id"],
                row["manifest"],
                "Preserved origin declaration",
            ),
        )


def _stage(verified, target, report):
    existed = target.exists()
    if existed:
        stage = _clone(target, ".wechat-exchange-stage-")
    else:
        descriptor, name = tempfile.mkstemp(
            prefix=".wechat-exchange-stage-", suffix=".sqlite3", dir=target.parent
        )
        os.close(descriptor)
        stage = Path(name)
    try:
        ensure_owner_only(stage)
        with closing(sqlite3.connect(stage)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA cache_size=-16384")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA recursive_triggers=ON")
            if not existed:
                connection.executescript(_SCHEMA)
            manifest = _manifest(connection, existing=existed)
            original_partitions = [
                tuple(row)
                for row in connection.execute("SELECT * FROM partitions ORDER BY id")
            ]
            report["messages_before"] = manifest["messages"]
            connection.executescript(_REGISTRY_SCHEMA)
            _guards(connection)
            for name, descriptor in verified.tables.items():
                for row in verified.iter_rows(name):
                    _archive_row(
                        connection,
                        verified.manifest["origin_id"],
                        name,
                        row,
                        descriptor,
                        verified.package_id,
                        report,
                    )
            _preserved_envelopes(connection, verified, report)
            _materialize(connection, verified, report)
            registered = connection.execute(
                "INSERT OR IGNORE INTO exchange_bundles VALUES(?,?,?,?)",
                (
                    verified.package_id,
                    verified.manifest["origin_id"],
                    _json(verified.manifest),
                    datetime.now(UTC).isoformat(),
                ),
            )
            report["registered_bundles"] = registered.rowcount
            retained = [
                tuple(
                    connection.execute(
                        "SELECT * FROM partitions WHERE id=?", (row[0],)
                    ).fetchone()
                )
                for row in original_partitions
            ]
            if retained != original_partitions:
                raise ValueError(
                    "Exchange import changed an existing source partition."
                )
            manifest = dict(manifest)
            manifest["messages"] += report["inserted_messages"]
            manifest["unique_original_message_count"] = None
            original_sources = {row[0] for row in original_partitions}
            for row in connection.execute("SELECT id FROM partitions ORDER BY id"):
                if row[0] not in original_sources:
                    manifest["sources"].append(
                        {
                            "source_id": row[0],
                            "account_id": None,
                            "identity_status": "Imported origin claim",
                        }
                    )
            connection.execute(
                "INSERT INTO archive_info(key,value) VALUES('manifest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_json(manifest),),
            )
            connection.commit()
            report["messages_after"] = manifest["messages"]
            report["history_partitions_sha256"] = wechat_history_partition_fingerprint(
                connection
            )
            report["old_identity_rows_unchanged"] = True
            checks = [
                connection.execute("PRAGMA quick_check('" + table + "')").fetchone()[0]
                for table in (
                    "exchange_original_rows",
                    "exchange_bundle_rows",
                    "exchange_row_mappings",
                    "exchange_bundles",
                )
            ]
            if checks != ["ok"] * len(checks):
                raise ValueError(
                    "The exchange registry failed its staged integrity checks."
                )
            report["integrity_check"] = (
                "ok; exchange tables and exact added-message readback"
            )
        _sync(stage)
        return stage
    except BaseException:
        stage.unlink(missing_ok=True)
        raise


def _info(path):
    with closing(_readonly(path)) as connection:
        return dict(connection.execute("SELECT key,value FROM archive_info"))


def _sidecars(target, baseline, fingerprint):
    result, states = {}, {}
    for name in _SIDECARS:
        path = target.with_name(name)
        if not os.path.lexists(path):
            states[name] = "absent; unchanged"
            continue
        _safe(path)
        if getattr(path.lstat(), "st_flags", 0) & 0x40000000:
            states[name] = "cloud-only; unchanged"
            continue
        identity = _resident(path)
        _no_input_writers(path)
        try:
            info = _info(path)
            pin = json.loads(info.get("history_file_identity", "null"))
        except (ValueError, sqlite3.Error):
            states[name] = "unverified; unchanged"
            continue
        if (
            pin != baseline
            or info.get("schema_version") != "1"
            or info.get("history_partitions_sha256", fingerprint) != fingerprint
        ):
            states[name] = "already stale; unchanged"
            continue
        result[path] = (identity, info)
        states[name] = "preserved with new primary pin"
    return result, states


def _repin(path, previous, primary_identity, fingerprint, canonical_identity=None):
    expected = {**previous, "history_file_identity": _json(primary_identity)}
    if "history_partitions_sha256" in previous:
        expected["history_partitions_sha256"] = fingerprint
    if canonical_identity is not None:
        expected["canonical_file_identity"] = _json(canonical_identity)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")

        def authorize(action, table, column, database, trigger):
            if (
                action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE}
                or action == sqlite3.SQLITE_UPDATE
                and table != "archive_info"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        for key in expected.keys() & {
            "history_file_identity",
            "history_partitions_sha256",
            "canonical_file_identity",
        }:
            cursor = connection.execute(
                "UPDATE archive_info SET value=? WHERE key=?", (expected[key], key)
            )
            if cursor.rowcount != 1:
                raise ValueError("A current resource sidecar lost its publication pin.")
        connection.commit()
    _sync(path)
    if _info(path) != expected:
        raise ValueError(
            "Resource repinning changed metadata outside its declared pins."
        )
    return expected


def _readback(target, verified, report):
    with closing(_readonly(target)) as connection:
        row = connection.execute(
            "SELECT manifest FROM exchange_bundles WHERE package_id=?",
            (verified.package_id,),
        ).fetchone()
        if row is None or row[0] != _json(verified.manifest):
            raise ValueError("The published exchange manifest failed its readback.")
        if (
            _manifest(connection, existing=True)["messages"] != report["messages_after"]
            or wechat_history_partition_fingerprint(connection)
            != report["history_partitions_sha256"]
        ):
            raise ValueError("The published exchange counters failed their readback.")
        for name, descriptor in verified.tables.items():
            columns = [column["name"] for column in descriptor["columns"]]
            for original in verified.iter_rows(name):
                digest = hashlib.sha256(_encoded_row(original, columns)).hexdigest()
                row = connection.execute(
                    "SELECT row_payload FROM exchange_original_rows WHERE origin_id=? AND table_name=? AND original_key=? AND row_sha256=?",
                    (
                        verified.manifest["origin_id"],
                        name,
                        _key(original, descriptor),
                        digest,
                    ),
                ).fetchone()
                if row is None or decode_preserved_row(row[0]) != original:
                    raise ValueError(
                        "A preserved original failed its exact published readback."
                    )


def preview_package(local_store_root, verified):
    """Validate a complete reversible stage and discard it without publication."""
    return import_package(local_store_root, verified, apply=False)


def import_package(
    local_store_root,
    verified_or_path,
    *,
    apply=True,
    expected_state=None,
    minimum_free_bytes=MINIMUM_FREE_BYTES,
):
    """Publish a source-separated append through coordinated APFS clones.

    The package is fully reverified before any target database is opened for
    writing. Original rows remain compressed typed envelopes; only explicitly
    unverified, source-local display projections join the live archive.
    """
    from .wechat_exchange import read_package

    package_path = getattr(verified_or_path, "path", verified_or_path)
    verified = read_package(package_path)
    try:
        root = _safe(local_store_root)
        target = _safe(wechat_archive_path(root))
        exclusions = {}
        for filename in ("exclusions.json", "source_exclusions.json"):
            exclusions.update(_source_exclusions(target.with_name(filename)))
        state = archive_state(root)
        if expected_state is not None and state != expected_state:
            raise ValueError(
                "The WeChat archive changed after preview; preview the package again."
            )
        origin = verified.manifest["origin_id"]
        report = dict(
            version=VERSION,
            package_id=verified.package_id,
            origin_id=origin,
            input_rows=sum(table["row_count"] for table in verified.tables.values()),
            inserted_messages=0,
            replayed_messages=0,
            inserted_conversations=0,
            added_partitions=0,
            preserved_rows=0,
            conflict_variants=0,
            conflicting_messages=0,
            registered_bundles=0,
            published=False,
            changed=False,
            replayed=False,
            resources_metadata_only=True,
            source_count=0,
            account_count=0,
            expected_state=state,
            database_path=str(target),
            sidecars={},
        )
        if target.exists():
            _resident(target)
            with closing(_readonly(target)) as connection:
                if (
                    _existing_table(connection, "exchange_bundles")
                    and connection.execute(
                        "SELECT 1 FROM exchange_bundles WHERE package_id=?",
                        (verified.package_id,),
                    ).fetchone()
                ):
                    count = connection.execute(
                        "SELECT COUNT(DISTINCT m.local_id) FROM exchange_row_mappings m "
                        "JOIN exchange_bundle_rows b USING(origin_id,table_name,original_key,row_sha256) "
                        "WHERE b.package_id=? AND m.local_table='messages'",
                        (verified.package_id,),
                    ).fetchone()[0]
                    report.update(replayed=True, replayed_messages=count)
                    return report
        if any(
            row["id"] in exclusions
            or "xchg-"
            + _digest(origin, row["id"], row.get("account_key"), row.get("account_id"))[
                :64
            ]
            in exclusions
            for row in verified.iter_rows("history.partitions")
        ):
            raise ValueError(
                "The exchange package contains an explicitly excluded source."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        expanded = _expanded_bytes(verified)
        copy_reserve = 0
        if sys.platform != "darwin":
            copy_reserve = 2 * sum(
                path.stat().st_size
                for path in (target, *(target.with_name(name) for name in _SIDECARS))
                if path.exists()
            )
        reserve = (
            expanded * 4 + report["input_rows"] * 512 + 32 * 1024**2 + copy_reserve
        )
        report.update(
            expanded_input_bytes=expanded,
            estimated_peak_bytes=reserve,
            clone_backend="APFS copy-on-write"
            if sys.platform == "darwin"
            else "budgeted full copy",
        )
        _space(target.parent, minimum_free_bytes, reserve)
        maintenance = CacheMaintenanceLock(CacheTaskLock(root / CACHE_TASK_LOCK_NAME))
        if not maintenance.acquire("wechat-exchange-import"):
            raise RuntimeError(
                "Another cache task owns the exchange maintenance boundary."
            )
        stage, backups, side_stages, current, published = None, {}, {}, {}, False
        try:
            if archive_state(root) != state:
                raise ValueError("The WeChat archive changed before exchange staging.")
            if target.exists():
                _no_input_writers(target)
                baseline = _resident(target)
                with closing(_readonly(target)) as connection:
                    fingerprint = wechat_history_partition_fingerprint(connection)
                current, report["sidecars"] = _sidecars(target, baseline, fingerprint)
            else:
                baseline, fingerprint = None, None
            stage = _stage(verified, target, report)
            report["changed"] = bool(
                report["preserved_rows"]
                or report["inserted_messages"]
                or report["added_partitions"]
                or report["registered_bundles"]
            )
            report["replayed"] = not (
                report["preserved_rows"] or report["inserted_messages"]
            )
            verified.assert_unchanged()
            if archive_state(root) != state:
                raise ValueError(
                    "The WeChat archive changed while the exchange stage was built."
                )
            if not apply or not report["changed"]:
                return report
            _space(target.parent, minimum_free_bytes, reserve)
            new_pin = _resident(stage)
            for path in (target, *current):
                if path.exists():
                    _no_input_writers(path)
                    backups[path] = _clone(path, ".wechat-exchange-rollback-")
            canonical = target.with_name("resource_references.sqlite3")
            ordered = sorted(
                current, key=lambda path: path.name != "resource_references.sqlite3"
            )
            side_expected = {}
            for path in ordered:
                side_stages[path] = _clone(path, ".wechat-exchange-sidecar-")
                previous = current[path][1]
                canonical_pin = None
                if (
                    previous.get("supplemental_metadata") == "1"
                    and canonical in side_stages
                    and json.loads(previous.get("canonical_file_identity", "null"))
                    == current[canonical][0]
                ):
                    canonical_pin = _resident(side_stages[canonical])
                side_expected[path] = _repin(
                    side_stages[path],
                    previous,
                    new_pin,
                    report["history_partitions_sha256"],
                    canonical_pin,
                )
            if archive_state(root) != state:
                raise ValueError(
                    "A publication input changed before exchange replacement."
                )
            verified.assert_unchanged()
            os.replace(stage, target)
            published = True
            for path, candidate in side_stages.items():
                os.replace(candidate, path)
            _sync_directory(target.parent)
            _readback(target, verified, report)
            if _resident(target) != new_pin:
                raise ValueError(
                    "The published archive differs from its verified stage."
                )
            for path, expected in side_expected.items():
                if _info(path) != expected:
                    raise ValueError(
                        "A published resource sidecar failed its exact pin readback."
                    )
            report.update(
                published=True,
                readback="exact preserved rows, added messages, manifest and resource pins",
                expected_state=archive_state(root),
            )
            return report
        except BaseException:
            if published:
                try:
                    if target in backups:
                        os.replace(backups[target], target)
                        restored_pin = _resident(target)
                        with closing(_readonly(target)) as connection:
                            restored_fingerprint = wechat_history_partition_fingerprint(
                                connection
                            )
                        canonical = target.with_name("resource_references.sqlite3")
                        for path in sorted(
                            current,
                            key=lambda value: (
                                value.name != "resource_references.sqlite3"
                            ),
                        ):
                            previous = current[path][1]
                            canonical_pin = (
                                _resident(canonical)
                                if previous.get("supplemental_metadata") == "1"
                                and canonical in backups
                                else None
                            )
                            _repin(
                                backups[path],
                                previous,
                                restored_pin,
                                restored_fingerprint,
                                canonical_pin,
                            )
                            os.replace(backups[path], path)
                    else:
                        target.unlink(missing_ok=True)
                    _sync_directory(target.parent)
                except BaseException as rollback_error:
                    backups = {}
                    raise RuntimeError(
                        "Exchange rollback requires review; retain .wechat-exchange-rollback files."
                    ) from rollback_error
            raise
        finally:
            if stage is not None:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    Path(str(stage) + suffix).unlink(missing_ok=True)
            for path in (*backups.values(), *side_stages.values()):
                for suffix in ("", "-wal", "-shm", "-journal"):
                    Path(str(path) + suffix).unlink(missing_ok=True)
            maintenance.release()
    finally:
        close = getattr(verified, "close", None)
        if close is not None:
            close()
