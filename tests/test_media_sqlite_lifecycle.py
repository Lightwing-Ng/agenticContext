"""Private media publication and deterministic legacy-reader lifetimes.

Code version: v1.0.0-codex.0
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core import media_sqlite
from app.core.owner_only_files import OwnerOnlyFileError, owner_only_problem


SCHEMA = pa.schema([pa.field("identity", pa.string(), nullable=False)])
ROWS = [{"identity": "private media identity"}]


def test_legacy_schema_and_rows_close_every_retained_reader(tmp_path, monkeypatch):
    path = tmp_path / "media.sqlite3"
    legacy = path.with_suffix(".parquet")
    pq.write_table(pa.Table.from_pylist(ROWS, schema=SCHEMA), legacy)
    original = legacy.read_bytes()
    native_reader = pq.ParquetFile
    readers = []

    def retained_reader(*args, **kwargs):
        reader = native_reader(*args, **kwargs)
        readers.append(reader)
        return reader

    monkeypatch.setattr(media_sqlite.pq, "ParquetFile", retained_reader)
    assert media_sqlite.read_media_legacy_schema(legacy).equals(SCHEMA)
    store = media_sqlite.MediaSQLiteStore(path, SCHEMA, ("identity",))
    assert store.rows() == ROWS
    assert readers and all(reader.closed for reader in readers)
    assert legacy.read_bytes() == original
    assert not path.exists()


def test_private_boundary_is_verified_before_first_sqlite_bytes(tmp_path, monkeypatch):
    path = tmp_path / "media.sqlite3"
    native_connect = media_sqlite._connect
    seen = []

    def verified_connect(candidate, **kwargs):
        if not kwargs.get("readonly"):
            assert owner_only_problem(candidate) == ""
            seen.append((candidate, candidate.stat().st_size))
        return native_connect(candidate, **kwargs)

    monkeypatch.setattr(media_sqlite, "_connect", verified_connect)
    store = media_sqlite.MediaSQLiteStore(path, SCHEMA, ("identity",))
    store.upsert_rows(ROWS)

    assert seen and seen[0][1] == 0
    assert owner_only_problem(path) == ""
    assert store.rows() == ROWS


def test_failed_private_boundary_preserves_legacy_and_publishes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "media.sqlite3"
    legacy = path.with_suffix(".parquet")
    pq.write_table(pa.Table.from_pylist(ROWS, schema=SCHEMA), legacy)
    original = legacy.read_bytes()

    def refuse(candidate: Path):
        assert candidate.stat().st_size == 0
        raise OwnerOnlyFileError("Synthetic native boundary failure")

    monkeypatch.setattr(media_sqlite, "ensure_owner_only", refuse, raising=False)
    store = media_sqlite.MediaSQLiteStore(path, SCHEMA, ("identity",))
    with pytest.raises(OwnerOnlyFileError, match="native boundary"):
        store.upsert_rows([{"identity": "new private identity"}])

    assert legacy.read_bytes() == original
    assert not path.exists()
    assert list(tmp_path.glob(".*.tmp")) == []
