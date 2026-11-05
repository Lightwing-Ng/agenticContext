"""Atomic Parquet publication closes every file before Windows replacement.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import errno
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core import resource_persistence as persistence
from app.core.owner_only_files import OwnerOnlyFileError, owner_only_problem


SCHEMA = pa.schema(
    [("key", pa.string()), ("values", pa.list_(pa.field("element", pa.int64())))],
    metadata={b"fixture": b"typed lifecycle roundtrip"},
)
OLD_ROWS = [{"key": "original", "values": [1, 2]}]
NEW_ROWS = [{"key": "中文", "values": [3, None]}, {"key": "", "values": []}]


def _seed(path):
    pq.write_table(pa.Table.from_pylist(OLD_ROWS, schema=SCHEMA), path)
    return path.read_bytes()


def test_atomic_parquet_releases_verification_and_writer_handles_before_replace(tmp_path, monkeypatch):
    path = tmp_path / "history.parquet"
    previous = _seed(path)
    read_table = persistence.pq.read_table
    replace = persistence.os.replace
    open_file = Path.open
    implicit_reader_leases = set()
    handles = []

    def retain_path_reader(source, *args, **kwargs):
        # Model Windows keeping a dataset's path-backed reader alive after read_table.
        if isinstance(source, (str, Path)):
            implicit_reader_leases.add(Path(source))
        return read_table(source, *args, **kwargs)

    def tracked_open(candidate, *args, **kwargs):
        handle = open_file(candidate, *args, **kwargs)
        if candidate.parent == tmp_path and candidate.name.endswith(".tmp"):
            handles.append(handle)
        return handle

    def replace_with_windows_handle_rules(source, destination):
        if Path(source) in implicit_reader_leases or any(not handle.closed for handle in handles):
            raise PermissionError(errno.EACCES, "Windows refuses replacement while a reader or writer owns the file")
        assert path.read_bytes() == previous
        return replace(source, destination)

    monkeypatch.setattr(persistence.pq, "read_table", retain_path_reader)
    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(persistence.os, "replace", replace_with_windows_handle_rules)
    persistence.write_parquet_rows_atomic(path, NEW_ROWS, SCHEMA)
    assert handles and all(handle.closed for handle in handles)
    with open_file(path, "rb") as handle:
        actual = read_table(handle)
    assert actual.schema.equals(SCHEMA, check_metadata=True)
    assert actual.to_pylist() == NEW_ROWS
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_verification_keeps_previous_bytes_and_closes_reader(tmp_path, monkeypatch):
    path = tmp_path / "history.parquet"
    previous = _seed(path)
    parquet_file = persistence.pq.ParquetFile
    readers = []

    class IncompleteReadback:
        def __init__(self, source, *args, **kwargs):
            self.reader = parquet_file(source, *args, **kwargs)
            readers.append(self.reader)

        def __enter__(self):
            return self

        def read(self):
            return pa.Table.from_pylist([], schema=SCHEMA)

        def __exit__(self, *_args):
            self.reader.close()

    monkeypatch.setattr(persistence.pq, "ParquetFile", IncompleteReadback)
    with pytest.raises(RuntimeError, match="Parquet verification failed"):
        persistence.write_parquet_rows_atomic(path, NEW_ROWS, SCHEMA)
    assert readers and all(reader.closed for reader in readers)
    assert path.read_bytes() == previous
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_failure_closes_partial_output_and_preserves_previous_bytes(tmp_path, monkeypatch):
    path = tmp_path / "history.parquet"
    previous = _seed(path)
    opened = []

    def failed_write(_table, destination, **_kwargs):
        if isinstance(destination, (str, Path)):
            with Path(destination).open("wb") as handle:
                handle.write(b"incomplete parquet")
        else:
            opened.append(destination)
            destination.write(b"incomplete parquet")
        raise OSError(errno.ENOSPC, "Synthetic full device")

    monkeypatch.setattr(persistence.pq, "write_table", failed_write)
    with pytest.raises(OSError, match="Synthetic full device"):
        persistence.write_parquet_rows_atomic(path, NEW_ROWS, SCHEMA)
    assert opened and all(handle.closed for handle in opened)
    assert path.read_bytes() == previous
    assert list(tmp_path.glob("*.tmp")) == []


def test_parquet_temp_boundary_is_verified_before_typed_content_is_written(tmp_path, monkeypatch):
    path = tmp_path / "private.parquet"
    write = persistence.pq.write_table
    observed = []

    def checked_write(table, destination, **kwargs):
        assert owner_only_problem(Path(destination.name)) == ""
        observed.append(Path(destination.name))
        return write(table, destination, **kwargs)

    monkeypatch.setattr(persistence.pq, "write_table", checked_write)
    persistence.write_parquet_rows_atomic(path, NEW_ROWS, SCHEMA)
    assert observed
    assert owner_only_problem(path) == ""
    assert persistence.read_parquet_rows(path) == NEW_ROWS


def test_unverifiable_parquet_boundary_keeps_previous_history_bytes(tmp_path, monkeypatch):
    path = tmp_path / "history.parquet"
    previous = _seed(path)

    def reject_boundary(_path):
        raise OwnerOnlyFileError("Synthetic native file boundary failure")

    monkeypatch.setattr(persistence, "ensure_owner_only", reject_boundary)
    with pytest.raises(OwnerOnlyFileError, match="Synthetic native file boundary failure"):
        persistence.write_parquet_rows_atomic(path, NEW_ROWS, SCHEMA)
    assert path.read_bytes() == previous
    assert list(tmp_path.glob("*.tmp")) == []
