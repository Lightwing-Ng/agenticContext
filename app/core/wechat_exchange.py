"""Exchange bounded, source-scoped WeChat records without executing package content.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import ExitStack
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import uuid
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq

from .wechat_archive import (
    WECHAT_KINDS, _source_exclusions, wechat_archive_path,
)
from .wechat_exchange_rows import encode_typed_row


VERSION = "v1.0.0-codex.0"
FORMAT = "agenticcontext.wechat-exchange"
MAX_PACKAGE_BYTES = 256 * 1024**2
MAX_RAW_BYTES = 256 * 1024**2
MAX_SERIALIZED_BYTES = 512 * 1024**2
MAX_LINE_BYTES = 64 * 1024**2
MAX_ROWS = 250_000
MAX_CELL_BYTES = 16 * 1024**2
MAX_MANIFEST_BYTES = 1024**2
MAX_TABLES = 160
_NAME = re.compile(r"[a-z][a-z0-9_]*")
_HEX = re.compile(r"[0-9a-f]{64}")
_SESSION = re.compile(r"wechat:[0-9a-f]{64}")
_KINDS = {"null": pa.null(), "integer": pa.int64(), "real": pa.float64(),
          "text": pa.string(), "blob": pa.binary()}
_MESSAGE_COLUMNS = (
    "id", "conversation_id", "ordinal", "sender_id", "sender_label", "direction",
    "text", "time_sort", "timestamp_value", "timestamp_label", "time_basis",
    "provenance", "quality_flags", "search_text",
)
_BASE_TABLES = {"messages", "conversations", "archive_info"}
_NATIVE_TABLES = {
    "native_resource_sources", "native_resource_source_inputs",
    "native_resource_occurrences", "native_resource_coverage",
    "native_resource_direction_scopes", "revalidation_source_proofs",
    "revalidation_native_witnesses", "html_forwarded_resources",
}
_ALLOWED = {
    "history": _BASE_TABLES | {"partitions", "message_storage", "shared_message_bodies",
        "source_snapshots", "message_evidence", "export_imports", "derived_message_aliases"},
    "resource": _BASE_TABLES | _NATIVE_TABLES,
    "resource_revalidated": _BASE_TABLES | _NATIVE_TABLES,
    "media_events": _BASE_TABLES | {"sources", "conversation_proofs", "html_forwarded_resources"},
    "contacts": {"contacts", "contact_identifiers", "contact_observations", "sender_bindings",
        "message_contact_bindings", "contact_registry_info"},
    "forwarded_clocks": {"links", "info"},
    "registries": {"entries", "artifacts"},
    "exchange": {"timeline", "presentations", "forwarded_items"},
    "preserved": {"original_rows", "bundles", "memberships"},
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate exchange JSON key.")
        result[key] = value
    return result


def _table_name(value):
    if not isinstance(value, str) or value.count(".") != 1:
        raise ValueError("Unsupported exchange table.")
    prefix, table = value.split(".")
    if table not in _ALLOWED.get(prefix, set()):
        raise ValueError("Unsupported exchange table.")
    return prefix, table


def _identity(path):
    path = Path(path)
    state = path.lstat()
    if (not stat.S_ISREG(state.st_mode) or path.is_symlink()
            or getattr(state, "st_flags", 0) & 0x40000000):
        raise ValueError("Exchange inputs must be resident regular files.")
    return (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns)


def _cell_kind(value):
    if value is None:
        return "null"
    if type(value) is int and -(2**63) <= value < 2**63:
        return "integer"
    if type(value) is float and math.isfinite(value):
        return "real"
    if isinstance(value, str):
        size = len(value.encode("utf-8"))
        kind = "text"
    elif isinstance(value, bytes):
        size, kind = len(value), "blob"
    else:
        raise ValueError("Unsupported exchange cell type.")
    if size > MAX_CELL_BYTES:
        raise ValueError("An exchange cell exceeds its byte budget.")
    return kind


def _package_id(manifest):
    return hashlib.sha256(_json({k: v for k, v in manifest.items() if k != "package_id"}).encode()).hexdigest()


def logical_row_sha256(row):
    """Bind exact SQLite cell types independently of a container codec or row order."""
    return hashlib.sha256(encode_typed_row(row, sorted(row))).hexdigest()


def _row_multiset_digest(hashes):
    return hashlib.sha256("".join(sorted(hashes)).encode("ascii")).hexdigest()


def _origin(base):
    """Persist an export namespace separately from source databases and identity claims."""
    path = base / "export_origin.json"
    if not path.exists():
        raw = (_json({"schema_version": 1, "origin_id": str(uuid.uuid4())}) + "\n").encode()
        descriptor, temporary_name = tempfile.mkstemp(prefix=".export-origin-", dir=base)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    if _identity(path)[2] > 1024:
        raise ValueError("Invalid export origin namespace.")
    value = json.loads(path.read_bytes(), object_pairs_hook=_unique)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "origin_id"}
            or value["schema_version"] != 1):
        raise ValueError("Invalid export origin namespace.")
    if str(uuid.UUID(value["origin_id"])) != value["origin_id"]:
        raise ValueError("Invalid export origin namespace.")
    return value["origin_id"]


@dataclass(frozen=True)
class VerifiedPackage:
    """A checked archive whose table reads remain bound to the checked local file."""

    path: Path
    manifest: dict
    identity: tuple

    @property
    def package_id(self):
        return self.manifest["package_id"]

    @property
    def tables(self):
        return {item["name"]: item for item in self.manifest["tables"]}

    def assert_unchanged(self):
        if _identity(self.path) != self.identity:
            raise ValueError("The exchange package changed after validation.")

    def iter_rows(self, table):
        self.assert_unchanged()
        descriptor = self.tables.get(table)
        if descriptor is None:
            return
        with zipfile.ZipFile(self.path) as archive:
            with archive.open(descriptor["path"]) as stream:
                yield from _member_rows(stream, descriptor)
        self.assert_unchanged()


def _validate_manifest(manifest):
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or type(manifest.get("format_version")) is not int
            or manifest["format_version"] != 1
            or not isinstance(manifest.get("package_id"), str)
            or not _HEX.fullmatch(manifest["package_id"])
            or _package_id(manifest) != manifest["package_id"]):
        raise ValueError("Unsupported or damaged WeChat exchange manifest.")
    try:
        valid_origin = str(uuid.UUID(manifest["origin_id"])) == manifest["origin_id"]
    except (KeyError, TypeError, ValueError, AttributeError):
        valid_origin = False
    if not valid_origin:
        raise ValueError("The exchange origin namespace is invalid.")
    scope = manifest.get("scope")
    if (not isinstance(scope, dict) or not isinstance(scope.get("conversation_ids"), list)
            or not scope["conversation_ids"] or len(scope["conversation_ids"]) > 128
            or any(not isinstance(v, str) or not _SESSION.fullmatch(v) for v in scope["conversation_ids"])
            or len(set(scope["conversation_ids"])) != len(scope["conversation_ids"])
            or scope.get("root_session_id") not in scope["conversation_ids"]):
        raise ValueError("The exchange conversation scope is invalid.")
    dependencies = scope.get("dependency_conversation_ids", [])
    if (not isinstance(dependencies, list) or len(dependencies) > 128
            or any(not isinstance(value, str) or not _SESSION.fullmatch(value) for value in dependencies)
            or len(set(dependencies)) != len(dependencies)
            or set(dependencies) & set(scope["conversation_ids"])):
        raise ValueError("The exchange dependency scope is invalid.")
    tables = manifest.get("tables")
    if not isinstance(tables, list) or not 1 <= len(tables) <= MAX_TABLES:
        raise ValueError("The exchange table budget is invalid.")
    names, paths, rows, size, expansion = set(), set(), 0, 0, 0
    for item in tables:
        if not isinstance(item, dict):
            raise ValueError("Invalid exchange table descriptor.")
        prefix, name = _table_name(item.get("name"))
        codec = item.get("codec")
        if codec not in {"parquet-zstd", "jsonl-zstd"}:
            raise ValueError("Unsupported exchange table codec.")
        expected = f"tables/{prefix}/{name}." + ("parquet" if codec == "parquet-zstd" else "jsonl.zst")
        if item["name"] in names or item.get("path") != expected or expected in paths:
            raise ValueError("Exchange table paths must be unique and canonical.")
        names.add(item["name"])
        paths.add(expected)
        if (type(item.get("row_count")) is not int or item["row_count"] < 0
                or type(item.get("bytes")) is not int or item["bytes"] < 0
                or type(item.get("uncompressed_bytes")) is not int or not 0 <= item["uncompressed_bytes"] <= MAX_SERIALIZED_BYTES
                or not isinstance(item.get("sha256"), str) or not _HEX.fullmatch(item["sha256"])
                or not isinstance(item.get("rows_sha256"), str) or not _HEX.fullmatch(item["rows_sha256"])):
            raise ValueError("Invalid exchange table size or digest.")
        rows += item["row_count"]
        size += item["bytes"]
        expansion += item["uncompressed_bytes"]
        columns = item.get("columns")
        if not isinstance(columns, list) or not 1 <= len(columns) <= 64:
            raise ValueError("Invalid exchange columns.")
        column_names = set()
        for column in columns:
            if (not isinstance(column, dict) or not isinstance(column.get("name"), str)
                    or not _NAME.fullmatch(column["name"]) or column["name"] in column_names
                    or column.get("type") not in _KINDS
                    or not isinstance(column.get("declared_type"), str)
                    or len(column["declared_type"]) > 128):
                raise ValueError("Unsupported exchange column.")
            column_names.add(column["name"])
        keys = item.get("primary_key", [])
        if (not isinstance(keys, list) or len(set(keys)) != len(keys)
                or any(key not in column_names for key in keys)):
            raise ValueError("Invalid exchange primary key.")
    if rows > MAX_ROWS or size > MAX_PACKAGE_BYTES or expansion > MAX_SERIALIZED_BYTES:
        raise ValueError("The exchange package exceeds its storage budget.")
    if not {"history.partitions", "history.conversations", "history.messages", "exchange.timeline"} <= names:
        raise ValueError("The exchange source closure is incomplete.")
    counts = manifest.get("counts")
    if (not isinstance(counts, dict) or any(type(counts.get(key)) is not int or counts[key] < 0
            for key in ("rows", "accounts", "sources", "conversations", "dependency_conversations", "messages", "resources"))
            or counts["rows"] != rows or counts["conversations"] != len(scope["conversation_ids"])
            or counts["dependency_conversations"] != len(dependencies)):
        raise ValueError("The exchange summary counts are invalid.")
    accounts = manifest.get("accounts")
    if (not isinstance(accounts, list) or len(accounts) != counts["accounts"]
            or any(not isinstance(row, dict) or set(row) != {"label", "external_id"}
                   or any(not isinstance(value, str) or len(value.encode()) > 4096 for value in row.values())
                   for row in accounts)):
        raise ValueError("The exchange account summary is invalid.")
    return paths


def _member_rows(stream, item):
    if item["codec"] == "parquet-zstd":
        parquet = pq.ParquetFile(stream, arrow_extensions_enabled=False,
                                 thrift_string_size_limit=MAX_MANIFEST_BYTES,
                                 thrift_container_size_limit=4096)
        expected = pa.schema([(c["name"], _KINDS[c["type"]]) for c in item["columns"]])
        if parquet.schema_arrow != expected or parquet.metadata.num_rows != item["row_count"]:
            raise ValueError("The exchange Parquet schema or row count differs.")
        if parquet.metadata.num_row_groups > MAX_ROWS:
            raise ValueError("The exchange row-group budget is exceeded.")
        expansion = sum(parquet.metadata.row_group(group).column(column).total_uncompressed_size
                        for group in range(parquet.metadata.num_row_groups)
                        for column in range(parquet.metadata.num_columns))
        if expansion > MAX_SERIALIZED_BYTES:
            raise ValueError("The exchange Parquet expansion budget is exceeded.")
        for batch in parquet.iter_batches(batch_size=256):
            yield from batch.to_pylist()
    else:
        size = 0
        with pa.CompressedInputStream(stream, "zstd") as compressed:
            with io.BufferedReader(compressed) as reader:
                while raw := reader.readline(MAX_LINE_BYTES + 1):
                    size += len(raw)
                    if size > item["uncompressed_bytes"] or len(raw) > MAX_LINE_BYTES:
                        raise ValueError("The exchange JSONL expansion budget is exceeded.")
                    row = json.loads(raw, object_pairs_hook=_unique)
                    if not isinstance(row, dict) or set(row) != {c["name"] for c in item["columns"]}:
                        raise ValueError("The exchange JSONL columns differ.")
                    for column in item["columns"]:
                        key = column["name"]
                        if column["type"] == "blob" and row[key] is not None:
                            if not isinstance(row[key], str):
                                raise ValueError("Invalid exchange binary cell.")
                            try:
                                row[key] = base64.b64decode(row[key], validate=True)
                            except ValueError as error:
                                raise ValueError("Invalid exchange binary cell.") from error
                    yield row
        if size != item["uncompressed_bytes"]:
            raise ValueError("The exchange JSONL byte count differs.")


def read_package(path):
    """Validate every declared member, typed cell, and bound before exposing a package."""
    path = Path(path).absolute()
    before = _identity(path)
    if before[2] > MAX_PACKAGE_BYTES:
        raise ValueError("The exchange package exceeds its storage budget.")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if (len(members) > MAX_TABLES + 1 or len({m.filename for m in members}) != len(members)
                    or any(m.compress_type != zipfile.ZIP_STORED or m.flag_bits & 1
                           or stat.S_ISLNK(m.external_attr >> 16) for m in members)):
                raise ValueError("Exchange members must be unique, stored, unencrypted regular files.")
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise ValueError("The exchange manifest exceeds its byte budget.")
            manifest = json.loads(archive.read(manifest_info), object_pairs_hook=_unique)
            expected_paths = _validate_manifest(manifest) | {"manifest.json"}
            if {m.filename for m in members} != expected_paths:
                raise ValueError("The exchange contains undeclared or missing members.")
            cell_bytes = 0
            actual = {"accounts": set(), "sources": 0, "messages": 0, "resources": 0}
            partitions, partition_accounts, account_labels = set(), {}, {}
            conversations, message_scopes, timeline_scopes = {}, set(), set()
            conversation_accounts = {}
            conversation_partitions = set()
            for item in manifest["tables"]:
                member = archive.getinfo(item["path"])
                if member.file_size != item["bytes"]:
                    raise ValueError("The exchange member size differs from its manifest.")
                digest = hashlib.sha256()
                with archive.open(member) as stream:
                    while chunk := stream.read(1024**2):
                        digest.update(chunk)
                if digest.hexdigest() != item["sha256"]:
                    raise ValueError("The exchange member digest differs from its manifest.")
                with archive.open(member) as stream:
                    count = 0
                    primary_keys = set()
                    row_hashes = []
                    for row in _member_rows(stream, item):
                        count += 1
                        if count > item["row_count"]:
                            raise ValueError("The exchange row budget is exceeded.")
                        row_hashes.append(logical_row_sha256(row))
                        key = tuple(row[column] for column in item.get("primary_key", []))
                        if item.get("primary_key"):
                            if key in primary_keys:
                                raise ValueError("The exchange table repeats a primary key.")
                            primary_keys.add(key)
                        if item["name"] == "history.partitions":
                            actual["sources"] += 1
                            actual["accounts"].add(row.get("account_id") or row.get("account_key") or "")
                            partitions.add(row.get("id"))
                            partition_accounts[row.get("id")] = row.get("account_key")
                            external = row.get("account_id") or row.get("account_key") or ""
                            account_labels.setdefault(external, set()).add(row.get("account_label"))
                        elif item["name"] == "exchange.timeline":
                            actual["messages"] += 1
                            actual["resources"] += row.get("origin_table") != "history.messages"
                            timeline_scopes.add(row.get("conversation_id"))
                        if item["name"].split(".")[1] == "conversations":
                            identifier = row.get("id")
                            partition = row.get("partition_id")
                            if identifier in conversations and conversations[identifier] != partition:
                                raise ValueError("The exchange conversation changes its source partition.")
                            conversations[identifier] = partition
                            conversation_accounts[identifier] = row.get("account_key")
                            conversation_partitions.add(partition)
                        elif item["name"].split(".")[1] in {"messages", "message_storage", "timeline"}:
                            message_scopes.add(row.get("conversation_id"))
                        for column in item["columns"]:
                            value = row[column["name"]]
                            kind = _cell_kind(value)
                            if kind not in {"null", column["type"]}:
                                raise ValueError("The exchange cell type differs from its schema.")
                            cell_bytes += len(value.encode()) if isinstance(value, str) else len(value) if isinstance(value, bytes) else 8
                            if cell_bytes > MAX_RAW_BYTES:
                                raise ValueError("The exchange cell-byte budget is exceeded.")
                    if count != item["row_count"]:
                        raise ValueError("The exchange rows are incomplete.")
                    if _row_multiset_digest(row_hashes) != item["rows_sha256"]:
                        raise ValueError("The exchange logical rows differ from their manifest.")
            if any(manifest["counts"][key] != (len(value) if isinstance(value, set) else value)
                   for key, value in actual.items()):
                raise ValueError("The exchange summary differs from its source rows.")
            if ({row["external_id"] for row in manifest["accounts"]} != actual["accounts"]
                    or any(row["label"] not in account_labels.get(row["external_id"], set())
                           for row in manifest["accounts"])):
                raise ValueError("The exchange account preview differs from its source rows.")
            declared = set(manifest["scope"]["conversation_ids"]) | set(manifest["scope"].get("dependency_conversation_ids", []))
            if (set(conversations) != declared or not message_scopes <= declared
                    or not timeline_scopes <= set(manifest["scope"]["conversation_ids"])
                    or not conversation_partitions <= partitions
                    or any(conversation_accounts[key] != partition_accounts.get(partition)
                           for key, partition in conversations.items())):
                raise ValueError("The exchange rows exceed or contradict the declared conversation closure.")
    except (zipfile.BadZipFile, KeyError, UnicodeError, pa.ArrowException, json.JSONDecodeError) as error:
        raise ValueError("The WeChat exchange package is malformed.") from error
    if _identity(path) != before:
        raise ValueError("The exchange package changed during validation.")
    verified = VerifiedPackage(path, manifest, before)
    validate_preserved_closure(manifest, verified.iter_rows)
    return verified


def validate_preserved_closure(manifest, rows_by_table):
    """Recheck a nested original bundle by its exact package membership.

    The caller decodes preserved typed cells and supplies only rows belonging
    to this original package. An origin namespace alone is never a row scope.
    """
    _validate_manifest(manifest)
    partitions, labels, conversations, contacts, message_ids = {}, {}, {}, set(), set()
    message_conversations, timeline_conversations, source_refs, contact_refs = set(), set(), set(), set()
    source_owners = set()
    messages, resources = 0, 0
    total_bytes = 0
    for descriptor in manifest["tables"]:
        name = descriptor["name"]
        domain, table = _table_name(name)
        columns = descriptor["columns"]
        names = {column["name"] for column in columns}
        seen, count, row_hashes = set(), 0, []
        for row in rows_by_table(name):
            if not isinstance(row, dict) or set(row) != names:
                raise ValueError("A preserved row differs from its original schema.")
            count += 1
            if count > descriptor["row_count"]:
                raise ValueError("Preserved rows exceed their original package membership.")
            row_hashes.append(logical_row_sha256(row))
            for column in columns:
                value = row[column["name"]]
                if _cell_kind(value) not in {"null", column["type"]}:
                    raise ValueError("A preserved row changed its original storage type.")
                total_bytes += len(value.encode()) if isinstance(value, str) else len(value) if isinstance(value, bytes) else 8
                if total_bytes > MAX_RAW_BYTES:
                    raise ValueError("The preserved source closure exceeds its byte budget.")
            key = tuple(row[column] for column in descriptor.get("primary_key", []))
            if descriptor.get("primary_key"):
                if key in seen:
                    raise ValueError("A preserved source table repeats a primary key.")
                seen.add(key)
            if name == "history.partitions":
                partitions[row["id"]] = row
                external = row.get("account_id") or row["account_key"]
                labels.setdefault(external, set()).add(row["account_label"])
                if row.get("account_id"):
                    source_owners.add(row["account_id"])
            if table == "conversations":
                value = row["partition_id"], row["account_key"]
                if row["id"] in conversations and conversations[row["id"]] != value:
                    raise ValueError("Preserved conversation source bindings conflict.")
                conversations[row["id"]] = value
            elif table in {"messages", "message_storage", "timeline"}:
                message_conversations.add(row["conversation_id"])
                message_ids.add(row["id"])
                if name == "exchange.timeline":
                    timeline_conversations.add(row["conversation_id"])
                    messages += 1
                    resources += row.get("origin_table") != "history.messages"
            if name == "contacts.contacts":
                contacts.add(row["id"])
            if domain == "contacts" and table in {"contact_identifiers", "contact_observations", "sender_bindings", "message_contact_bindings"}:
                contact_refs.add(row["contact_id"])
            if "source_id" in row and domain not in {"preserved", "registries", "exchange"}:
                source_refs.add(row["source_id"])
        if count != descriptor["row_count"]:
            raise ValueError("A preserved original table is incomplete.")
        if _row_multiset_digest(row_hashes) != descriptor["rows_sha256"]:
            raise ValueError("Preserved logical rows differ from the original package manifest.")
    family = set(manifest["scope"]["conversation_ids"])
    declared = family | set(manifest["scope"].get("dependency_conversation_ids", []))
    if (set(conversations) != declared or not message_conversations <= declared
            or not timeline_conversations <= family or not source_refs <= partitions.keys()
            or not contact_refs <= contacts):
        raise ValueError("Preserved rows exceed their original declared conversation closure.")
    for partition, account in conversations.values():
        if partition not in partitions or account != partitions[partition]["account_key"]:
            raise ValueError("A preserved conversation changes its original account binding.")
    expected_counts = {"sources": len(partitions), "accounts": len(labels),
                       "messages": messages, "resources": resources}
    if (any(manifest["counts"][key] != value for key, value in expected_counts.items())
            or {row["external_id"] for row in manifest["accounts"]} != labels.keys()
            or any(row["label"] not in labels.get(row["external_id"], set()) for row in manifest["accounts"])):
        raise ValueError("The preserved account preview differs from its original rows.")
    return {**expected_counts, "conversations": len(family),
            "dependency_conversations": len(declared - family)}


class _Collector:
    def __init__(self, stack):
        self.stack = stack
        self.connections, self.identities, self.tables, self.columns, self.keys = {}, {}, {}, {}, {}
        self.absent_inputs = set()
        self.byte_count, self.row_count = 0, 0

    def connect(self, domain, path, *, required=False):
        if not path.exists():
            if required:
                raise ValueError("The requested WeChat archive is unavailable.")
            return None
        identity = _identity(path)
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("A WeChat source is being published; retry the export.")
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        self.stack.callback(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA temp_store=MEMORY")
        self.connections[domain] = connection
        self.identities[path] = identity
        return connection

    def check(self):
        if any(os.path.lexists(path) for path in self.absent_inputs):
            raise ValueError("A previously absent WeChat presentation input appeared during export.")
        for path, identity in self.identities.items():
            if _identity(path) != identity:
                raise ValueError("A WeChat source changed during export: " + path.name)

    def available(self, domain, table):
        connection = self.connections.get(domain)
        return connection is not None and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table','view')", (table,)
        ).fetchone() is not None

    def add(self, name, rows, columns=None, primary_key=None):
        _table_name(name)
        target = self.tables.setdefault(name, {})
        if columns is not None:
            self.columns[name] = columns
        if primary_key is not None:
            self.keys[name] = primary_key
        for value in rows:
            row = dict(value)
            encode_typed_row(row, row)
            key = tuple(row.get(column) for column in self.keys.get(name, row.keys()))
            if key in target:
                if target[key] != row:
                    raise ValueError("A source table contains conflicting primary keys.")
                continue
            for cell in row.values():
                _cell_kind(cell)
                self.byte_count += len(cell.encode()) if isinstance(cell, str) else len(cell) if isinstance(cell, bytes) else 8
            self.row_count += 1
            if self.row_count > MAX_ROWS or self.byte_count > MAX_RAW_BYTES:
                raise ValueError("This conversation exceeds the bounded exchange budget.")
            target[key] = row

    def query(self, domain, table, where="1=1", params=()):
        if not self.available(domain, table):
            return
        info = list(self.connections[domain].execute(f'PRAGMA table_info("{table}")'))
        columns = [(row[1], row[2]) for row in info]
        key = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        if not key:
            key = ["id"] if "id" in dict(columns) else list(dict(columns))
        self.add(domain + "." + table,
                 self.connections[domain].execute(f'SELECT * FROM "{table}" WHERE ' + where, params),
                 columns, key)

    def select(self, domain, table, column, values):
        values = tuple(sorted(set(values)))
        if not values:
            return
        for offset in range(0, len(values), 256):
            batch = values[offset:offset + 256]
            self.query(domain, table, '"' + column + '" IN (' + ",".join("?" for _ in batch) + ")", batch)

    def rows(self, name):
        return list(self.tables.get(name, {}).values())


def _native_partition(row):
    owner = row.get("account_id")
    return (isinstance(owner, str) and bool(owner) and row.get("account_key") == "account:" + owner
            and row.get("id") == "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
            and row.get("identity_status") == "Verified archive ID")


def _family(connection, session, account, kind, local_store_root):
    row = connection.execute(
        "SELECT c.*,p.account_id,p.identity_status FROM conversations c JOIN partitions p ON p.id=c.partition_id WHERE c.id=?",
        (session,),
    ).fetchone()
    if row is None:
        from .wechat_session_export import WechatSessionExport
        exporter = WechatSessionExport(local_store_root, session, account=account, kind=kind,
                                       write_event_cache=False)
        try:
            row = exporter.conversation
        finally:
            exporter.close()
    if row is None or account and row["account_key"] != account or kind != "all" and row["kind"] != kind:
        raise ValueError("The requested conversation is unavailable in this scope.")
    current = dict(row)
    family = {session}
    if not _native_partition({**current, "id": current["partition_id"]}):
        return current, [session]
    owner, peer = current["account_id"], current["peer_id"]
    for candidate in connection.execute(
        "SELECT c.id,c.partition_id,c.peer_id,c.kind,c.account_key,p.account_id,p.identity_status,p.id AS native_partition FROM conversations c JOIN partitions p ON p.id=c.partition_id WHERE c.kind=? AND (c.peer_id=? OR c.peer_id=?)",
        (current["kind"], peer, owner),
    ):
        candidate = dict(candidate)
        verified = _native_partition({**candidate, "id": candidate["native_partition"]})
        if not verified:
            continue
        if (candidate["peer_id"] == peer and (current["kind"] == "group" or candidate["account_id"] == owner)
                or current["kind"] == "direct" and candidate["peer_id"] == owner and candidate["account_id"] == peer):
            family.add(candidate["id"])
    if len(family) > 128:
        raise ValueError("The conversation family exceeds its exchange budget.")
    return current, sorted(family)


def _registry_entries(collector, base, message_ids):
    entries = []
    for filename in ("forwarded_author_links.json", "forwarded_clock_links.json",
                     "forwarded_previews.json", "context_links.json", "text_observations.json"):
        path = base / filename
        if not path.exists():
            continue
        identity = _identity(path)
        if identity[2] > 16 * 1024**2:
            raise ValueError("A presentation registry exceeds its exchange budget.")
        raw = path.read_bytes()
        registry = json.loads(raw, object_pairs_hook=_unique)
        collector.identities[path] = identity
        for key in ("links", "observations", "previews"):
            for entry in registry.get(key, ()):
                if isinstance(entry, dict) and any(entry.get(field) in message_ids for field in (
                        "target_message_id", "target_parent_id", "message_id", "source_parent_id", "native_message_id")):
                    entries.append({"kind": filename, "collection": key,
                                    "entry_json": _json(entry), "source_file_sha256": hashlib.sha256(raw).hexdigest()})
                    if filename == "text_observations.json" and isinstance(entry.get("source"), dict):
                        source = entry["source"]
                        relative = Path(source.get("path", ""))
                        if (relative.is_absolute() or ".." in relative.parts or relative.suffix != ".json"
                                or not (base / relative).resolve().is_relative_to(base.resolve())):
                            raise ValueError("Text-observation artifacts must remain managed JSON metadata.")
                        artifact = base / relative
                        for candidate in (artifact, *artifact.parents):
                            if candidate.is_symlink():
                                raise ValueError("Text-observation artifact paths cannot contain symbolic links.")
                        artifact_identity = _identity(artifact)
                        if artifact_identity[2] > 1024**2:
                            raise ValueError("A text-observation artifact exceeds its byte budget.")
                        content = artifact.read_bytes()
                        digest = hashlib.sha256(content).hexdigest()
                        if digest != source.get("sha256"):
                            raise ValueError("A text-observation artifact no longer matches its source pin.")
                        collector.identities[artifact] = artifact_identity
                        collector.add("registries.artifacts", [{"kind": "text_observation_json", "path_hint": str(relative),
                                      "sha256": digest, "content": content}],
                                      [("kind", "TEXT"), ("path_hint", "TEXT"), ("sha256", "TEXT"), ("content", "BLOB")],
                                      ["sha256"])
    if entries:
        collector.add("registries.entries", entries,
                      [(key, "TEXT") for key in entries[0]], ["kind", "collection", "entry_json"])
    return entries


_PRESENTATION_FIELDS = {
    "display_text", "viewer_display_name", "is_system_notice", "resource", "quoted_reply",
    "forwarded_record", "system_notices", "record_fields", "payment", "direction", "sender_id",
    "timestamp_label", "time_basis", "quality_flags",
}


def _freeze_presentation(metadata, *, depth=0, budget=None):
    if depth > 32:
        raise ValueError("A presentation graph exceeds its nesting budget.")
    budget = [0] if budget is None else budget

    def plain(value, level):
        if level > 64:
            raise ValueError("A presentation value exceeds its nesting budget.")
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (list, tuple)):
            return [plain(item, level + 1) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("Presentation metadata keys must be strings.")
                result[key] = record(item) if key == "forwarded_record" and isinstance(item, dict) else plain(item, level + 1)
            return result
        raise ValueError("A presentation graph contains an unsupported object.")

    def record(value):
        if not isinstance(value, dict):
            raise ValueError("A forwarded presentation must remain an object.")
        result = {key: plain(value[key], depth + 1) for key in ("title", "source_title", "presentation_label", "preview_text") if key in value}
        children = []
        for child in value.get("items", ()):
            budget[0] += 1
            if budget[0] > 10_000:
                raise ValueError("A presentation graph exceeds its component budget.")
            children.append({"stable_id": child.stable_id, "content_text": child.content_text,
                             "author_label": child.author_label, "timestamp_value": child.last_seen_at,
                             "metadata": _freeze_presentation(child.messaging or {}, depth=depth + 1, budget=budget)})
        result["items"] = children
        return result

    result = {}
    for key in _PRESENTATION_FIELDS:
        if key in metadata:
            result[key] = record(metadata[key]) if key == "forwarded_record" else plain(metadata[key], depth + 1)
    return result


def _full_presentations(collector, local_store_root, family, expected_ids):
    from .wechat_history_browser import query_wechat_history

    snapshots, size = {}, 0
    for session in family:
        number = 1
        while True:
            page = query_wechat_history(local_store_root, session=session, page=number, page_size=100,
                                        persist_event_cache=False)
            if not page.current_session:
                raise ValueError("A presentation conversation changed during export.")
            for item in page.items:
                if item.stable_id not in expected_ids or item.stable_id in snapshots:
                    raise ValueError("The full presentation changed its canonical message membership.")
                snapshot = _freeze_presentation(item.messaging or {})
                size += len(_json(snapshot).encode())
                if size + collector.byte_count > MAX_RAW_BYTES:
                    raise ValueError("The full presentation exceeds its byte budget.")
                snapshots[item.stable_id] = snapshot
            if number >= page.total_pages:
                break
            number += 1
            if number * 100 > MAX_ROWS:
                raise ValueError("The full presentation exceeds its paging budget.")
    if set(snapshots) != expected_ids:
        raise ValueError("The full presentation does not cover its canonical source timeline.")
    return snapshots


def _validated_timeline(collector, local_store_root, family, stack):
    from .wechat_session_export import WechatSessionExport

    rows, statuses, presentations, forwarded = [], {}, [], []
    history_ids = {row["id"] for row in collector.rows("history.messages")}
    resource_ids = {domain: {row["id"] for row in collector.rows(domain + ".messages")}
                    for domain in ("resource", "resource_revalidated")}
    for session in family:
        exporter = WechatSessionExport(local_store_root, session, write_event_cache=False)
        stack.callback(exporter.close)
        statuses[session] = {"resources": exporter.resource_status, "events": exporter.events["status"]}
        selected_domain = "resource_revalidated" if any(path.name == "resource_revalidated.sqlite3"
                           for path in exporter.sources) else "resource"
        collector.identities.update(exporter.sources)
        for value in exporter.items():
            row = {key: value[key] for key in _MESSAGE_COLUMNS}
            origin = "history.messages"
            if row["id"] not in history_ids:
                if row["id"] in resource_ids[selected_domain]:
                    origin = selected_domain + ".messages"
                else:
                    origin = "media_events.messages"
            rows.append({**row, "origin_table": origin})
            leaves = value.get("forwarded_items", ())
            members = exporter.events.get("members_by_preferred", {}).get(row["id"], ())
            if value.get("viewer_display_name") or leaves or members:
                metadata = {"schema_version": 1, "authority": "frozen_external_presentation",
                            "source_message_id": row["id"], "source_provenance": row["provenance"],
                            "forwarded_items": leaves, "event_members": members}
                presentations.append({"message_id": row["id"],
                                      "viewer_display_name": value.get("viewer_display_name", ""),
                                      "metadata_json": _json(metadata)})
            for leaf in leaves:
                forwarded.append({"parent_id": row["id"], "component_path": _json(leaf["component_path"]),
                                  **{key: leaf.get(key, "") for key in (
                                      "id", "sender_id", "sender_label", "viewer_display_name", "text",
                                      "timestamp_value", "timestamp_label", "time_basis", "source_clock")},
                                  "provenance": _json(leaf["provenance"])})
    full = _full_presentations(collector, local_store_root, family, {row["id"] for row in rows})
    existing = {row["message_id"]: row for row in presentations}
    presentations = []
    for row in rows:
        item = existing.get(row["id"], {})
        metadata = json.loads(item["metadata_json"]) if item else {
            "schema_version": 1, "authority": "frozen_external_presentation",
            "source_message_id": row["id"], "source_provenance": row["provenance"],
            "forwarded_items": [], "event_members": [],
        }
        metadata["presentation"] = full[row["id"]]
        presentations.append({"message_id": row["id"],
                              "viewer_display_name": full[row["id"]].get("viewer_display_name", item.get("viewer_display_name", "")),
                              "metadata_json": _json(metadata)})
    if presentations:
        collector.add("exchange.presentations", presentations,
                      [(key, "TEXT") for key in presentations[0]], ["message_id"])
    if forwarded:
        collector.add("exchange.forwarded_items", forwarded,
                      [(key, "TEXT") for key in forwarded[0]], ["parent_id", "component_path"])
    return rows, statuses


def _policy_exclusions(collector, base, history):
    exclusions, excluded_owners = {}, set()
    for filename in ("exclusions.json", "source_exclusions.json"):
        policy = base / filename
        if policy.exists():
            collector.identities[policy] = _identity(policy)
            exclusions.update(_source_exclusions(policy))
        else:
            collector.absent_inputs.add(policy)
    for source, entry in exclusions.items():
        owner = history.execute("SELECT account_id FROM partitions WHERE id=?", (source,)).fetchone()
        if owner and owner[0]:
            excluded_owners.add(owner[0])
        excluded_owners.update(entry[field] for field in ("account_id", "owner_account_id", "source_account_id")
                               if isinstance(entry.get(field), str) and entry[field])
    return exclusions, excluded_owners


def _scoped_local_presentation_changes(collector, base, history, source):
    """Keep unrelated native preferences outside an unchanged imported graph.

    Imported owners have no verified native account ID. Contact viewing names
    likewise require a verified, exactly bound source contact; matching an
    original username or a global preference key cannot rename an unresolved
    imported sender. New bindings that match this graph are retained through
    the ordinary export path instead of silently discarding local evidence.
    """
    conversations = set(getattr(source, "local_conversation_ids", ()))
    partitions = set(getattr(source, "local_partition_ids", ()))
    message_ids, sender_scopes = set(), set()
    for conversation in conversations:
        for row in history.execute(
            "SELECT m.id,m.sender_id,c.partition_id,c.peer_id,c.kind FROM messages m "
            "JOIN conversations c ON c.id=m.conversation_id WHERE c.id=?", (conversation,),
        ):
            message_ids.add(row["id"])
            if row["sender_id"]:
                sender_scopes.add((row["partition_id"], row["sender_id"],
                                   row["peer_id"] if row["kind"] == "group" else ""))
    contact_path = base / "contacts.sqlite3"
    if contact_path.exists():
        contacts = collector.connect("contacts", contact_path)
        from .wechat_contacts import CANONICAL_AUTHORITIES
        authorities = tuple(sorted(CANONICAL_AUTHORITIES | {"source_scoped_identifier"}))
        if collector.available("contacts", "sender_bindings"):
            for partition, sender, room in sender_scopes:
                if contacts.execute(
                    "SELECT 1 FROM sender_bindings WHERE source_id=? AND sender_id=? "
                    "AND source_snapshot='' AND room_id IN ('',?) AND authority IN ("
                    + ",".join("?" for _ in authorities) + ") LIMIT 1",
                    (partition, sender, room, *authorities),
                ).fetchone():
                    return True
        if collector.available("contacts", "message_contact_bindings"):
            ordered_messages = sorted(message_ids)
            for partition in partitions:
                for start in range(0, len(ordered_messages), 256):
                    batch = ordered_messages[start:start + 256]
                    if contacts.execute(
                        "SELECT 1 FROM message_contact_bindings WHERE source_id=? AND message_id IN ("
                        + ",".join("?" for _ in batch) + ") LIMIT 1", (partition, *batch),
                    ).fetchone():
                        return True
    else:
        collector.absent_inputs.add(contact_path)

    from .wechat_reviewed_quote_events import reviewed_quotes_path
    registries = [(base / filename, collection, target) for filename, collection, target in (
        ("forwarded_author_links.json", "links", "target_message_id"),
        ("forwarded_clock_links.json", "links", "target_message_id"),
        ("forwarded_previews.json", "previews", "target_parent_id"),
        ("context_links.json", "links", "target_message_id"),
        ("text_observations.json", "observations", "message_id"),
    )]
    registries.append((reviewed_quotes_path(base.parent.parent), "quote_reviews", "preferred_message_id"))
    for path, collection, target in registries:
        if not path.exists():
            collector.absent_inputs.add(path)
            continue
        collector.identities[path] = _identity(path)
        if collector.identities[path][2] > 16 * 1024**2:
            raise ValueError("A presentation registry exceeds its metadata budget.")
        value = json.loads(path.read_bytes(), object_pairs_hook=_unique)
        entries = value.get(collection, ()) if isinstance(value, dict) else ()
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get(target), str) and entry[target] in message_ids:
                return True
            if collection == "quote_reviews":
                if isinstance(entry.get("conversation_id"), str) and entry["conversation_id"] in conversations:
                    return True
                members = entry.get("members", ())
                if isinstance(members, list) and any(isinstance(member, str) and member in message_ids for member in members):
                    return True
    clocks = base / "forwarded_clock_links.sqlite3"
    if clocks.exists():
        connection = collector.connect("forwarded_clocks", clocks)
        if collector.available("forwarded_clocks", "links"):
            ordered = sorted(message_ids)
            for start in range(0, len(ordered), 256):
                batch = ordered[start:start + 256]
                marks = ",".join("?" for _ in batch)
                if connection.execute("SELECT 1 FROM links WHERE target_message_id IN (" + marks
                                      + ") LIMIT 1", batch).fetchone():
                    return True
    else:
        collector.absent_inputs.add(clocks)
    return False


def _maybe_fast_collect(local_store_root, session, account, kind, stack):
    path = wechat_archive_path(local_store_root)
    base = path.parent
    collector = _Collector(stack)
    history = collector.connect("history", path, required=True)
    if not collector.available("history", "exchange_original_rows"):
        return None
    preferences = base / "display_names.json"
    if preferences.exists():
        collector.identities[preferences] = _identity(preferences)
        if collector.identities[preferences][2] > 64 * 1024:
            raise ValueError("The viewing-name preferences exceed their byte budget.")
    else:
        collector.absent_inputs.add(preferences)
    from .wechat_exchange_import import fast_forward_source
    source = fast_forward_source(history, session, account=account, kind=kind)
    if source is None:
        return None
    if _scoped_local_presentation_changes(collector, base, history, source):
        return None
    exclusions, excluded_owners = _policy_exclusions(collector, base, history)
    local_partitions = set(getattr(source, "local_partition_ids", ()))
    local = history.execute("SELECT partition_id FROM conversations WHERE id=?", (session,)).fetchone()
    if local is not None:
        local_partitions.add(local[0])
    for partition in local_partitions:
        local_owner = history.execute("SELECT account_id FROM partitions WHERE id=?", (partition,)).fetchone()
        if partition in exclusions or local_owner and local_owner[0] in excluded_owners:
            raise ValueError("The preserved conversation includes an explicitly excluded local source or account.")
    for descriptor in source.manifest["tables"]:
        collector.add(descriptor["name"], source.iter_rows(descriptor["name"]),
                      [(column["name"], column["declared_type"]) for column in descriptor["columns"]],
                      descriptor.get("primary_key", []))
    validate_preserved_closure(source.manifest, collector.rows)
    for name in collector.tables:
        for row in collector.rows(name):
            source_fields = ("id", "source_id", "partition_id") if name == "history.partitions" else ("source_id", "partition_id")
            if (any(row.get(field) in exclusions for field in source_fields if isinstance(row.get(field), str))
                    or any(row.get(field) in excluded_owners for field in ("account_id", "owner_account_id") if isinstance(row.get(field), str))):
                raise ValueError("The preserved conversation depends on an explicitly excluded source or account.")
    scope = json.loads(_json(source.manifest["scope"]))
    selected = getattr(source, "selected_original_session_id", None)
    if selected in scope["conversation_ids"]:
        scope["root_session_id"] = selected
    collector.check()
    return collector, scope, base, source.manifest["origin_id"]


def _collect(local_store_root, session, account, kind, stack):
    path = wechat_archive_path(local_store_root)
    base = path.parent
    collector = _Collector(stack)
    history = collector.connect("history", path, required=True)
    exclusions, excluded_owners = _policy_exclusions(collector, base, history)
    current, family = _family(history, session, account, kind, local_store_root)
    for candidate in family:
        partition = history.execute("SELECT p.id,p.account_id FROM conversations c JOIN partitions p ON p.id=c.partition_id WHERE c.id=?", (candidate,)).fetchone()
        if partition and (partition[0] in exclusions or partition[1] in excluded_owners):
            raise ValueError("This conversation closure depends on an explicitly excluded source or account.")
    for domain, filename in (("resource", "resource_references.sqlite3"),
                             ("resource_revalidated", "resource_revalidated.sqlite3"),
                             ("media_events", "media_events.sqlite3"),
                             ("contacts", "contacts.sqlite3"),
                             ("forwarded_clocks", "forwarded_clock_links.sqlite3")):
        collector.connect(domain, base / filename)
    for domain in ("history", "resource", "resource_revalidated", "media_events"):
        collector.select(domain, "conversations", "id", family)
        collector.select(domain, "messages", "conversation_id", family)
    message_ids = {row["id"] for domain in ("history", "resource", "resource_revalidated", "media_events")
                   for row in collector.rows(domain + ".messages")}
    for _ in range(8):
        before = len(message_ids)
        for column in ("target_message_id", "native_message_id"):
            collector.select("forwarded_clocks", "links", column, message_ids)
        for column in ("old_id", "native_id"):
            collector.select("history", "derived_message_aliases", column, message_ids)
        entries = _registry_entries(collector, base, message_ids)
        endpoints = set(message_ids)
        for row in collector.rows("forwarded_clocks.links") + collector.rows("history.derived_message_aliases"):
            endpoints.update(row.get(column) for column in ("target_message_id", "native_message_id", "old_id", "native_id") if row.get(column))
        for row in entries:
            entry = json.loads(row["entry_json"])
            endpoints.update(entry.get(column) for column in ("target_message_id", "target_parent_id", "native_message_id", "source_message_id", "source_parent_id") if entry.get(column))
            for item in entry.get("items", ()):
                if isinstance(item, dict):
                    for key in ("message_id", "anchor_resource_id", "resource_id"):
                        identifier = item.get(key)
                        if isinstance(identifier, str) and _HEX.fullmatch(identifier):
                            endpoints.add(identifier)
        # Explicit forwarded references carry exact IDs and account scope; they
        # add dependency endpoints without merging account observations.
        for domain in ("history", "resource", "resource_revalidated", "media_events"):
            for message in collector.rows(domain + ".messages"):
                provenance = json.loads(message["provenance"])
                references = provenance.get("forwarded_references", {}) if isinstance(provenance, dict) else {}
                for item in references.get("items", ()) if isinstance(references, dict) else ():
                    if isinstance(item, dict):
                        identifier = item.get("message_id") or item.get("resource_id")
                        if isinstance(identifier, str) and _HEX.fullmatch(identifier):
                            endpoints.add(identifier)
        for domain in ("history", "resource", "resource_revalidated", "media_events"):
            collector.select(domain, "messages", "id", endpoints - message_ids)
        message_ids = endpoints
        if len(message_ids) == before:
            break
    else:
        raise ValueError("The conversation proof closure exceeds its depth budget.")
    conversation_ids = set(family)
    for domain in ("history", "resource", "resource_revalidated", "media_events"):
        conversation_ids.update(row["conversation_id"] for row in collector.rows(domain + ".messages"))
        collector.select(domain, "conversations", "id", conversation_ids)
    partitions = {row["partition_id"] for domain in ("history", "resource", "resource_revalidated", "media_events")
                  for row in collector.rows(domain + ".conversations")}
    collector.select("history", "partitions", "id", partitions)
    collector.select("history", "message_storage", "id", message_ids)
    collector.select("history", "shared_message_bodies", "id", {
        row["content_ref"] for row in collector.rows("history.message_storage") if row.get("content_ref") is not None})
    collector.select("history", "message_evidence", "message_id", message_ids)
    snapshot_refs = set()
    for name, column in (("history.messages", "provenance"), ("history.message_evidence", "observation")):
        for row in collector.rows(name):
            observation = json.loads(row[column])
            snapshot = observation.get("source_snapshot") if isinstance(observation, dict) else None
            if type(snapshot) is int:
                snapshot_refs.add(snapshot)
    collector.select("history", "source_snapshots", "id", snapshot_refs)
    partitions.update(row["source_id"] for row in collector.rows("history.source_snapshots"))
    partitions.update(row["source_id"] for row in collector.rows("history.message_evidence"))
    collector.select("history", "partitions", "id", partitions)
    collector.select("history", "source_snapshots", "source_id", partitions)
    collector.select("history", "export_imports", "source_id", partitions)
    for domain in ("resource", "resource_revalidated"):
        collector.select(domain, "native_resource_coverage", "conversation_id", conversation_ids)
        for coverage in collector.rows(domain + ".native_resource_coverage"):
            collector.query(domain, "native_resource_occurrences",
                            "source_snapshot_id=? AND database_relative_path=? AND database_sha256=? AND chat_table=?",
                            tuple(coverage[key] for key in ("source_snapshot_id", "database_relative_path", "database_sha256", "chat_table")))
        snapshots = {row["source_snapshot_id"] for table in ("native_resource_occurrences", "native_resource_coverage")
                     for row in collector.rows(domain + "." + table)}
        for table in ("native_resource_sources", "native_resource_source_inputs", "revalidation_source_proofs"):
            collector.select(domain, table, "source_snapshot_id", snapshots)
        owners = {row["account_id"] for row in collector.rows("history.partitions") if row.get("account_id")}
        collector.select(domain, "native_resource_direction_scopes", "account_id", owners)
        collector.select(domain, "revalidation_native_witnesses", "message_id", message_ids)
    collector.select("media_events", "conversation_proofs", "conversation_id", conversation_ids)
    collector.select("media_events", "sources", "id", {row["source_snapshot_id"] for row in collector.rows("media_events.conversation_proofs")})
    for domain in ("resource", "resource_revalidated", "media_events"):
        collector.select(domain, "html_forwarded_resources", "parent_id", message_ids)
    collector.select("contacts", "message_contact_bindings", "message_id", message_ids)
    sender_ids = {row["sender_id"] for domain in ("history", "resource", "resource_revalidated", "media_events")
                  for row in collector.rows(domain + ".messages") if row.get("sender_id")}
    peers = {row["peer_id"] for row in collector.rows("history.conversations") if row.get("peer_id")} | {""}
    if collector.available("contacts", "sender_bindings"):
        for source in partitions:
            for sender in sender_ids:
                collector.query("contacts", "sender_bindings", "source_id=? AND sender_id=? AND room_id IN (" + ",".join("?" for _ in peers) + ")", (source, sender, *sorted(peers)))
    contact_ids = {row["contact_id"] for table in ("message_contact_bindings", "sender_bindings") for row in collector.rows("contacts." + table)}
    for table in ("contacts", "contact_identifiers"):
        collector.select("contacts", table, "id" if table == "contacts" else "contact_id", contact_ids)
    if collector.available("contacts", "contact_observations"):
        for contact in contact_ids:
            collector.query("contacts", "contact_observations", "contact_id=? AND source_id IN (" + ",".join("?" for _ in partitions) + ") AND room_id IN (" + ",".join("?" for _ in peers) + ")", (contact, *sorted(partitions), *sorted(peers)))
    preferences = base / "display_names.json"
    if preferences.exists():
        identity = _identity(preferences)
        if identity[2] > 64 * 1024:
            raise ValueError("The viewing-name preferences exceed their byte budget.")
        raw = preferences.read_bytes()
        value = json.loads(raw, object_pairs_hook=_unique)
        scoped_names = [{"kind": "display_names.json", "collection": "contacts",
                         "entry_json": _json({"contact_id": contact, "name": name}),
                         "source_file_sha256": hashlib.sha256(raw).hexdigest()}
                        for contact, name in value.get("contacts", {}).items() if contact in contact_ids]
        collector.identities[preferences] = identity
        collector.add("registries.entries", scoped_names,
                      [(key, "TEXT") for key in ("kind", "collection", "entry_json", "source_file_sha256")],
                      ["kind", "collection", "entry_json"])
    for domain in collector.connections:
        table = "archive_info" if domain not in {"contacts", "forwarded_clocks"} else "contact_registry_info" if domain == "contacts" else "info"
        # Global build reports and archive catalogs are derived metadata outside
        # a selected conversation. Exact selected source snapshots stay intact.
        collector.query(domain, table, "key NOT IN ('manifest','report','covered_conversations','source_receipts','supplement_source_receipts')")
    timeline, statuses = _validated_timeline(collector, local_store_root, family, stack)
    message_columns = collector.columns["history.messages"]
    collector.add("exchange.timeline", timeline, [*message_columns, ("origin_table", "TEXT")], ["id"])
    if collector.available("history", "exchange_original_rows"):
        from .wechat_exchange_import import (
            preserved_manifests, preserved_memberships, preserved_rows,
        )
        original_rows = list(preserved_rows(history, partitions, conversation_ids=family))
        bundles = list(preserved_manifests(history, partitions, conversation_ids=family))
        memberships = list(preserved_memberships(history, partitions, conversation_ids=family))
        if original_rows:
            collector.add("preserved.original_rows", original_rows,
                          [(key, "BLOB" if key == "row_payload" else "TEXT") for key in original_rows[0]],
                          ["origin_id", "table_name", "original_key", "row_sha256"])
        if bundles:
            collector.add("preserved.bundles", bundles, [(key, "TEXT") for key in bundles[0]], ["package_id"])
        if memberships:
            collector.add("preserved.memberships", memberships,
                          [(key, "TEXT") for key in memberships[0]],
                          ["package_id", "origin_id", "table_name", "original_key", "row_sha256"])
    collector.check()
    for name in collector.tables:
        for row in collector.rows(name):
            if (any(row.get(field) in exclusions for field in ("source_id", "partition_id") if isinstance(row.get(field), str))
                    or any(row.get(field) in excluded_owners for field in ("account_id", "owner_account_id") if isinstance(row.get(field), str))):
                raise ValueError("This conversation closure depends on an explicitly excluded source or account.")
    return collector, {"root_session_id": session, "conversation_ids": family,
                       "dependency_conversation_ids": sorted(conversation_ids - set(family)),
                       "account": account, "kind": kind, "timeline_status": statuses}, base


def _write_table(path, rows, columns):
    kinds = {}
    for key, _ in columns:
        types = {_cell_kind(row[key]) for row in rows} - {"null"}
        if len(types) > 1:
            raise ValueError("Mixed SQLite storage types need a newer exchange format.")
        kinds[key] = next(iter(types), "null")
    schema = pa.schema([(key, _KINDS[kinds[key]]) for key, _ in columns])
    parquet_path = Path(str(path) + ".parquet")
    with pq.ParquetWriter(parquet_path, schema, compression="zstd", compression_level=19,
                          use_dictionary=True, write_statistics=False) as writer:
        for offset in range(0, len(rows), 4096):
            writer.write_table(pa.Table.from_pylist(rows[offset:offset + 4096], schema=schema))
    jsonl_path = Path(str(path) + ".jsonl.zst")
    raw_bytes = 0
    codec = pa.Codec("zstd", compression_level=19)
    with jsonl_path.open("wb") as writer:
        # Concatenated bounded Zstandard frames are standard-readable without
        # requiring Python 3.14's optional stdlib streaming compressor.
        for offset in range(0, len(rows), 4096):
            chunk = bytearray()
            for row in rows[offset:offset + 4096]:
                encoded = {key: base64.b64encode(value).decode("ascii") if isinstance(value, bytes) else value for key, value in row.items()}
                raw = (_json(encoded) + "\n").encode()
                if len(raw) > MAX_LINE_BYTES:
                    raise ValueError("An exchange row exceeds its serialized line budget.")
                chunk.extend(raw)
            raw_bytes += len(chunk)
            writer.write(codec.compress(chunk))
    selected = min((parquet_path, jsonl_path), key=lambda target: target.stat().st_size)
    return selected, ("jsonl-zstd" if selected == jsonl_path else "parquet-zstd"), raw_bytes, [
        {"name": key, "type": kinds[key], "declared_type": declaration} for key, declaration in columns]


def export_conversation(local_store_root, session_id, output_path, *, account="", kind="all"):
    """Publish a private, bounded package containing complete scoped source observations."""
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id) or kind not in WECHAT_KINDS:
        raise ValueError("A canonical WeChat conversation scope is required.")
    output_path = Path(output_path).absolute()
    if output_path.suffix.casefold() != ".wechatx":
        raise ValueError("WeChat exchange output must use the .wechatx extension.")
    if output_path.is_symlink():
        raise ValueError("Exchange output cannot replace a symbolic link.")
    output_path = output_path.parent.resolve(strict=True) / output_path.name
    if output_path.exists():
        output_identity = _identity(output_path)
        runtime = wechat_archive_path(local_store_root).parent
        if runtime.exists():
            for candidate in runtime.iterdir():
                state = candidate.lstat()
                if stat.S_ISREG(state.st_mode) and output_identity[:2] == (state.st_dev, state.st_ino):
                    raise ValueError("Exchange output must never replace a source or registry input.")
    with ExitStack() as stack:
        fast = _maybe_fast_collect(local_store_root, session_id, account, kind, stack)
        if fast is None:
            collector, scope, base = _collect(local_store_root, session_id, account, kind, stack)
            original_origin = None
        else:
            collector, scope, base, original_origin = fast
        output_identity = _identity(output_path) if output_path.exists() else None
        if any(output_path == source.resolve()
               or output_identity is not None and output_identity[:2] == identity[:2]
               for source, identity in collector.identities.items()):
            raise ValueError("Exchange output must never replace a source or registry input.")
        accounts = {}
        for partition in collector.rows("history.partitions"):
            external = partition.get("account_id") or partition["account_key"]
            accounts.setdefault(external, {"label": partition["account_label"], "external_id": external})
        timeline = collector.rows("exchange.timeline")
        serialized_size = 0
        for name in collector.tables:
            for row in collector.rows(name):
                wire_row = {key: base64.b64encode(value).decode("ascii") if isinstance(value, bytes) else value
                            for key, value in row.items()}
                row_size = len(_json(wire_row).encode()) + 1
                if row_size > MAX_LINE_BYTES:
                    raise ValueError("An exchange row exceeds its serialized line budget.")
                serialized_size += row_size
                if serialized_size > MAX_SERIALIZED_BYTES:
                    raise ValueError("The exchange serialized expansion exceeds its byte budget.")
        manifest = {"format": FORMAT, "format_version": 1, "code_version": VERSION,
                    "origin_id": original_origin or _origin(base), "created_at": datetime.now(UTC).isoformat(),
                    "scope": scope, "media_bytes_included": False,
                    "source_authority": "Sender claims; package hashes prove integrity, not identity.",
                    "accounts": list(accounts.values()),
                    "tables": [], "counts": {"rows": collector.row_count,
                        "accounts": len(accounts), "sources": len(collector.rows("history.partitions")),
                        "conversations": len(scope["conversation_ids"]), "messages": len(timeline),
                        "dependency_conversations": len(scope["dependency_conversation_ids"]),
                        "resources": sum(row["origin_table"] != "history.messages" for row in timeline),
                        "timeline_messages": len(timeline)}}
        with tempfile.TemporaryDirectory(prefix="wechat-exchange-", dir=output_path.parent) as temporary:
            temporary = Path(temporary)
            for name in sorted(collector.tables):
                rows = collector.rows(name)
                selected, codec, raw_bytes, columns = _write_table(temporary / name, rows, collector.columns[name])
                prefix, table = name.split(".")
                extension = ".parquet" if codec == "parquet-zstd" else ".jsonl.zst"
                manifest["tables"].append({"name": name, "path": f"tables/{prefix}/{table}" + extension,
                    "columns": columns, "primary_key": collector.keys[name], "row_count": len(rows),
                    "codec": codec, "uncompressed_bytes": raw_bytes,
                    "rows_sha256": _row_multiset_digest(logical_row_sha256(row) for row in rows),
                    "sha256": hashlib.sha256(selected.read_bytes()).hexdigest(), "bytes": selected.stat().st_size,
                    "_staged_filename": selected.name})
            staged_filenames = {item["name"]: item.pop("_staged_filename") for item in manifest["tables"]}
            manifest["package_id"] = _package_id(manifest)
            _validate_manifest(manifest)
            staged = temporary / "package.wechatx"
            descriptor = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                    for item in manifest["tables"]:
                        archive.write(temporary / staged_filenames[item["name"]], item["path"])
                    archive.writestr("manifest.json", _json(manifest).encode())
                stream.flush()
                os.fsync(stream.fileno())
            if staged.stat().st_size > MAX_PACKAGE_BYTES:
                raise ValueError("The exchange package exceeds its uploadable storage budget.")
            read_package(staged)
            collector.check()
            os.replace(staged, output_path)
        return manifest
