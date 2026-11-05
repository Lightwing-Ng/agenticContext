"""Typed, indexed media state with transactional keyed updates.

Code version: v1.0.0-codex.0

Legacy Parquet is read without mutation. The first explicit write imports a
matching legacy schema; callers repairing older schemas must explicitly replace
normalized rows. Media state never shares the compressed LLM snapshot format.
"""

from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
import struct
import tempfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, TypeVar

import pyarrow as pa
import pyarrow.parquet as pq

STORE_VERSION = 1
_ORDINAL = "__media_ordinal"
_T = TypeVar("_T")


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.absolute().as_uri() + "?mode=ro" if readonly else str(path),
        uri=readonly,
        timeout=30,
    )
    connection.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        connection.execute("PRAGMA synchronous=FULL")
    return connection


def sqlite_storage_kind(path: Path) -> str:
    """Identify a SQLite format without creating or modifying its file.

    Invalid existing files raise rather than allowing a legacy fallback.
    """
    with closing(_connect(Path(path), readonly=True)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    if {"media_info", "media_rows"} <= tables:
        return "media"
    if {"store_info", "row_groups"} <= tables:
        return "llm"
    return "unknown"


def _metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    metadata = dict(connection.execute("SELECT key,value FROM media_info"))
    try:
        if int(metadata["format_version"]) != STORE_VERSION:
            raise ValueError("Unsupported media SQLite storage version.")
        metadata["schema"] = pa.ipc.read_schema(
            pa.BufferReader(metadata["arrow_schema"])
        )
        metadata["keys"] = tuple(json.loads(metadata["key_fields"]))
        metadata["indexes"] = tuple(json.loads(metadata["index_fields"]))
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Incomplete media SQLite metadata.") from error
    return metadata


def read_media_sqlite_schema(path: Path) -> pa.Schema:
    """Return the exact stored Arrow schema, including field metadata."""
    with closing(_connect(Path(path), readonly=True)) as connection:
        return _metadata(connection)["schema"]


def _legacy_schema(parquet: pq.ParquetFile) -> pa.Schema:
    serialized = (parquet.metadata.metadata or {}).get(b"ARROW:schema")
    if serialized is None:
        return parquet.schema_arrow
    original = pa.ipc.read_schema(pa.BufferReader(base64.b64decode(serialized)))
    if not original.equals(parquet.schema_arrow, check_metadata=False):
        raise ValueError(
            "Legacy media Arrow metadata conflicts with its physical schema."
        )
    return original


def read_media_legacy_schema(path: Path) -> pa.Schema:
    """Read original Arrow provenance before Parquet normalizes list child names."""
    return _legacy_schema(pq.ParquetFile(path))


def _sql_type(data_type: pa.DataType) -> str:
    if pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
        return "TEXT"
    if pa.types.is_integer(data_type) or pa.types.is_boolean(data_type):
        return "INTEGER"
    if pa.types.is_floating(data_type):
        return "REAL"
    if pa.types.is_list(data_type) or pa.types.is_large_list(data_type):
        _sql_type(data_type.value_type)
        return "TEXT"
    if pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type):
        return "BLOB"
    raise ValueError(f"Unsupported media SQLite Arrow type: {data_type}.")


def _encode(value: Any, data_type: pa.DataType) -> Any:
    if value is None:
        return None
    if pa.types.is_boolean(data_type):
        return int(value)
    if pa.types.is_list(data_type) or pa.types.is_large_list(data_type):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if pa.types.is_floating(data_type) and math.isnan(value):
        # SQLite normalizes a bound NaN to NULL. Preserve its IEEE representation.
        return sqlite3.Binary(struct.pack(">d", value))
    if pa.types.is_uint64(data_type) and value > 2**63 - 1:
        return sqlite3.Binary(struct.pack(">Q", value))
    return value


def _codecs(data_type: pa.DataType) -> tuple[Callable, Callable] | None:
    """Prepare conversions once; ordinary scalar columns need no row decoder."""
    if pa.types.is_boolean(data_type):
        return (
            lambda value: None if value is None else int(value),
            lambda value: None if value is None else bool(value),
        )
    if pa.types.is_list(data_type) or pa.types.is_large_list(data_type):
        return (
            lambda value: (
                None
                if value is None
                else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            ),
            lambda value: None if value is None else json.loads(value),
        )
    if pa.types.is_floating(data_type):
        return (
            lambda value: (
                sqlite3.Binary(struct.pack(">d", value))
                if value is not None and math.isnan(value)
                else value
            ),
            lambda value: (
                struct.unpack(">d", value)[0] if isinstance(value, bytes) else value
            ),
        )
    if pa.types.is_uint64(data_type):
        return (
            lambda value: (
                sqlite3.Binary(struct.pack(">Q", value))
                if value is not None and value > 2**63 - 1
                else value
            ),
            lambda value: (
                struct.unpack(">Q", value)[0] if isinstance(value, bytes) else value
            ),
        )
    return None


class MediaSQLiteStore:
    """Store ordered typed rows, with SQL point queries and atomic keyed deltas."""

    def __init__(
        self,
        path: Path,
        schema: pa.Schema,
        key_fields: tuple[str, ...],
        index_fields: tuple[str, ...] = (),
    ) -> None:
        self.path = Path(path)
        self.schema = schema
        self.key_fields = tuple(key_fields)
        self.index_fields = tuple(dict.fromkeys(index_fields))
        if len(set(schema.names)) != len(schema.names) or _ORDINAL in schema.names:
            raise ValueError("Media schema has duplicate or reserved column names.")
        if not self.key_fields or len(set(self.key_fields)) != len(self.key_fields):
            raise ValueError("Media SQLite requires distinct primary-key fields.")
        for field in schema:
            _sql_type(field.type)
        for name in (*self.key_fields, *self.index_fields):
            if name not in schema.names:
                raise ValueError(f"Unknown media key or index field: {name}.")
            data_type = schema.field(name).type
            if pa.types.is_list(data_type) or pa.types.is_large_list(data_type):
                raise ValueError("Media indexes require scalar fields.")
        self._fields = tuple(schema)
        self._column_names = tuple(schema.names)
        self._field_names = frozenset(schema.names)
        self._required_fields = tuple(
            field.name for field in schema if not field.nullable
        )
        self._conversions = tuple(
            (index, field.name, codecs)
            for index, field in enumerate(schema)
            if (codecs := _codecs(field.type)) is not None
        )
        self._key_conversions = {
            name: codecs[0]
            for _, name, codecs in self._conversions
            if name in self.key_fields
        }
        self._columns = ",".join(_quoted(name) for name in schema.names)
        self._where = " AND ".join(f"{_quoted(name)}=?" for name in self.key_fields)
        if _exists(self.path):
            with closing(_connect(self.path, readonly=True)) as connection:
                self._validate(connection)

    def _validate(self, connection: sqlite3.Connection) -> None:
        metadata = _metadata(connection)
        if not metadata["schema"].equals(self.schema, check_metadata=True):
            raise ValueError("Media SQLite Arrow schema does not match the caller.")
        if metadata["keys"] != self.key_fields:
            raise ValueError("Media SQLite primary keys do not match the caller.")
        if metadata["indexes"] != self.index_fields:
            raise ValueError("Media SQLite indexes do not match the caller.")
        columns = connection.execute("PRAGMA table_info(media_rows)").fetchall()
        expected = [
            (_ORDINAL, "INTEGER"),
            *((field.name, _sql_type(field.type)) for field in self.schema),
        ]
        if [(row[1], row[2]) for row in columns] != expected:
            raise ValueError("Media SQLite row table does not match its metadata.")
        actual_keys = tuple(
            row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]
        )
        if actual_keys != self.key_fields:
            raise ValueError("Media SQLite row primary key is invalid.")
        for index, field in enumerate(self.index_fields):
            indexed_columns = connection.execute(
                f"PRAGMA index_info(media_index_{index})"
            ).fetchall()
            if [row[2] for row in indexed_columns] != [field]:
                raise ValueError("Media SQLite declared index is missing or invalid.")

    def _legacy(self, *, exact_schema: bool = False) -> list[dict[str, Any]]:
        legacy = self.path.with_suffix(".parquet")
        if not _exists(legacy):
            return []
        before = legacy.stat()
        parquet = pq.ParquetFile(legacy)
        table = parquet.read()
        embedded_schema = _legacy_schema(parquet)
        after = legacy.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("Legacy media Parquet changed during its read.")
        if exact_schema and not (
            table.schema.equals(self.schema, check_metadata=True)
            or embedded_schema.equals(self.schema, check_metadata=True)
        ):
            raise ValueError(
                "Legacy media schema needs explicit normalization with replace_rows."
            )
        return table.to_pylist()

    def _key(self, key: Any) -> tuple[Any, ...]:
        values = (key,) if len(self.key_fields) == 1 else tuple(key)
        if len(values) != len(self.key_fields) or any(
            value is None for value in values
        ):
            raise ValueError("Media primary key is incomplete.")
        normalized = pa.Table.from_pylist(
            [dict(zip(self.key_fields, values, strict=True))],
            schema=pa.schema([self.schema.field(name) for name in self.key_fields]),
        ).to_pylist()[0]
        return tuple(
            _encode(normalized[name], self.schema.field(name).type)
            for name in self.key_fields
        )

    def _normalize(self, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        materialized = [dict(row) for row in rows]
        for row in materialized:
            if set(row) - self._field_names:
                raise ValueError("Media row contains fields outside its Arrow schema.")
        normalized = pa.Table.from_pylist(materialized, schema=self.schema).to_pylist()
        keys: set[tuple[Any, ...]] = set()
        for row in normalized:
            for name in self._required_fields:
                if row[name] is None:
                    raise ValueError(f"Media field {name} cannot be null.")
            key = self._key_from_row(row)
            if key in keys:
                raise ValueError("Duplicate media primary key in one write batch.")
            keys.add(key)
        return normalized

    def _key_from_row(self, row: Mapping[str, Any]) -> tuple[Any, ...]:
        values = tuple(row[name] for name in self.key_fields)
        if any(value is None for value in values):
            raise ValueError("Media primary key is incomplete.")
        return tuple(
            self._key_conversions[name](value)
            if name in self._key_conversions
            else value
            for name, value in zip(self.key_fields, values, strict=True)
        )

    def _decoded(self, values: tuple[Any, ...]) -> dict[str, Any]:
        row = dict(zip(self._column_names, values, strict=True))
        for index, name, (_, decode) in self._conversions:
            row[name] = decode(values[index])
        return row

    def _fetch(self, connection: sqlite3.Connection, key: Any) -> dict[str, Any] | None:
        row = connection.execute(
            f"SELECT {self._columns} FROM media_rows WHERE {self._where}",
            self._key(key),
        ).fetchone()
        return self._decoded(row) if row is not None else None

    def get(self, key: Any) -> dict[str, Any] | None:
        """Read one primary key; an absent canonical store remains read-only."""
        encoded = self._key(key)
        if not _exists(self.path):
            return next(
                (row for row in self._legacy() if self._key_from_row(row) == encoded),
                None,
            )
        with closing(_connect(self.path, readonly=True)) as connection:
            self._validate(connection)
            return self._fetch(connection, key)

    def find(self, field: str, value: Any) -> list[dict[str, Any]]:
        """Query a declared scalar index or primary-key column through SQLite."""
        if field not in (*self.key_fields, *self.index_fields):
            raise ValueError(f"Media field is not indexed: {field}.")
        normalized = pa.scalar(value, type=self.schema.field(field).type).as_py()
        if not _exists(self.path):
            return [row for row in self._legacy() if row.get(field) == normalized]
        with closing(_connect(self.path, readonly=True)) as connection:
            self._validate(connection)
            values = connection.execute(
                f"SELECT {self._columns} FROM media_rows WHERE {_quoted(field)} IS ? "
                f"ORDER BY {_quoted(_ORDINAL)}",
                (_encode(normalized, self.schema.field(field).type),),
            )
            return [self._decoded(row) for row in values]

    def rows(self) -> list[dict[str, Any]]:
        """Read original insertion order, or raw legacy rows for schema repair."""
        if not _exists(self.path):
            return self._legacy()
        with closing(_connect(self.path, readonly=True)) as connection:
            self._validate(connection)
            return [
                self._decoded(row)
                for row in connection.execute(
                    f"SELECT {self._columns} FROM media_rows ORDER BY {_quoted(_ORDINAL)}"
                )
            ]

    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE media_info(key TEXT PRIMARY KEY,value BLOB NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO media_info VALUES (?,?)",
            (
                ("format_version", str(STORE_VERSION)),
                ("arrow_schema", self.schema.serialize().to_pybytes()),
                ("key_fields", json.dumps(self.key_fields)),
                ("index_fields", json.dumps(self.index_fields)),
            ),
        )
        definitions = [f"{_quoted(_ORDINAL)} INTEGER NOT NULL UNIQUE"]
        definitions.extend(
            f"{_quoted(field.name)} {_sql_type(field.type)}"
            + (
                " NOT NULL"
                if not field.nullable or field.name in self.key_fields
                else ""
            )
            for field in self.schema
        )
        definitions.append(
            "PRIMARY KEY(" + ",".join(_quoted(name) for name in self.key_fields) + ")"
        )
        connection.execute("CREATE TABLE media_rows(" + ",".join(definitions) + ")")
        for index, field in enumerate(self.index_fields):
            connection.execute(
                f"CREATE INDEX media_index_{index} ON media_rows({_quoted(field)})"
            )
        # Composite keys also need indexes for a point query on a later key field.
        for index, field in enumerate(self.key_fields[1:]):
            if field not in self.index_fields:
                connection.execute(
                    f"CREATE INDEX media_key_index_{index} ON media_rows({_quoted(field)})"
                )

    def _upsert(
        self, connection: sqlite3.Connection, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        ordinal = connection.execute(
            f"SELECT COALESCE(MAX({_quoted(_ORDINAL)}),-1)+1 FROM media_rows"
        ).fetchone()[0]
        assignments = ",".join(
            f"{_quoted(name)}=excluded.{_quoted(name)}" for name in self.schema.names
        )
        different = " OR ".join(
            f"media_rows.{_quoted(name)} IS NOT excluded.{_quoted(name)}"
            for name in self.schema.names
        )
        sql = (
            f"INSERT INTO media_rows({_quoted(_ORDINAL)},{self._columns}) VALUES("
            + ",".join("?" for _ in range(len(self.schema) + 1))
            + ") ON CONFLICT("
            + ",".join(_quoted(name) for name in self.key_fields)
            + f") DO UPDATE SET {assignments} WHERE {different}"
        )
        for row in rows:
            values = [row[name] for name in self._column_names]
            for index, name, (encode, _) in self._conversions:
                values[index] = encode(row[name])
            connection.execute(sql, (ordinal, *values))
            ordinal += 1

    def _mutate(
        self,
        operation: Callable[[sqlite3.Connection], _T],
        *,
        import_legacy: bool = True,
    ) -> _T:
        if _exists(self.path):
            with closing(_connect(self.path)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._validate(connection)
                    result = operation(connection)
                    connection.commit()
                    return result
                except BaseException:
                    connection.rollback()
                    raise
        self.path.parent.mkdir(parents=True, exist_ok=True)
        baseline = (
            self._normalize(self._legacy(exact_schema=True)) if import_legacy else []
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with closing(_connect(temporary)) as connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._initialize(connection)
                    self._upsert(connection, baseline)
                    result = operation(connection)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("New media SQLite integrity verification failed.")
                self._validate(connection)
            try:
                # Never overwrite another writer's first durable publication.
                os.link(temporary, self.path)
                if os.name == "posix":
                    parent_descriptor = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(parent_descriptor)
                    finally:
                        os.close(parent_descriptor)
                return result
            except FileExistsError:
                return self._mutate(operation, import_legacy=import_legacy)
        finally:
            temporary.unlink(missing_ok=True)

    def apply_changes(
        self,
        rows: Iterable[Mapping[str, Any]] = (),
        delete_keys: Iterable[Any] = (),
    ) -> None:
        """Commit deletions and keyed upserts together; an upsert wins overlap."""
        normalized = self._normalize(rows)
        keys = [self._key(key) for key in delete_keys]
        if not normalized and not keys:
            return

        def apply(connection: sqlite3.Connection) -> None:
            connection.executemany(f"DELETE FROM media_rows WHERE {self._where}", keys)
            self._upsert(connection, normalized)

        self._mutate(apply)

    def upsert_rows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Insert or change only the provided primary keys."""
        self.apply_changes(rows=rows)

    def delete_keys(self, keys: Iterable[Any]) -> None:
        """Delete only the provided primary keys in one transaction."""
        self.apply_changes(delete_keys=keys)

    def replace_rows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Explicitly replace a snapshot, for migration and compatibility only."""
        normalized = self._normalize(rows)

        def replace(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM media_rows")
            self._upsert(connection, normalized)

        self._mutate(replace, import_legacy=False)

    def compare_and_swap(
        self,
        key: Any,
        expected: Mapping[str, Any],
        updates: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Update a matching latest row under an immediate write transaction."""
        self._key(key)
        if (set(expected) | set(updates)) - set(self.schema.names):
            raise ValueError("Unknown media compare-and-swap field.")
        if set(updates) & set(self.key_fields):
            raise ValueError("Media compare-and-swap cannot change primary keys.")

        def swap(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = self._fetch(connection, key)
            if row is None or any(
                row[name] != value for name, value in expected.items()
            ):
                return None
            changed = self._normalize([{**row, **updates}])[0]
            self._upsert(connection, [changed])
            return changed

        return self._mutate(swap)


def media_store_for(path: Path, schema: pa.Schema | None = None) -> MediaSQLiteStore:
    """Construct one of the five canonical media stores without creating it."""
    from .resource_persistence import MEDIA_SQLITE_SPECS

    path = Path(path)
    name = path.with_suffix(".sqlite3").name
    try:
        default_schema, keys, indexes = MEDIA_SQLITE_SPECS[name]
    except KeyError as error:
        raise ValueError(
            f"Not a canonical media SQLite filename: {path.name}."
        ) from error
    if schema is None:
        if _exists(path):
            stored_schema = read_media_sqlite_schema(path)
            if not stored_schema.equals(default_schema, check_metadata=False):
                raise ValueError(
                    "Stored media schema is incompatible with its canonical type."
                )
            schema = stored_schema
        else:
            legacy = path.with_suffix(".parquet")
            if _exists(legacy):
                legacy_schema = read_media_legacy_schema(legacy)
                if legacy_schema.equals(default_schema, check_metadata=False):
                    schema = legacy_schema
    return MediaSQLiteStore(
        path, schema if schema is not None else default_schema, keys, indexes
    )
