"""Single-file Parquet lifecycle, atomic replacement, and failure diagnostics.

Code version: v1.0.1-codex.1
"""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pytest

from app.core import resource_persistence as persistence


SCHEMA = pa.schema([pa.field("value", pa.string(), nullable=False)])


@pytest.fixture
def retained_readers(monkeypatch):
    """Keep real readers alive so closing cannot depend on reference destruction."""
    original = persistence.pq.ParquetFile
    readers = []

    def open_reader(source, *args, **kwargs):
        reader = original(source, *args, **kwargs)
        readers.append(reader)
        return reader

    def retained_table(source, *args, **kwargs):
        # Model a library retaining its file reader after returning a complete Table.
        return open_reader(source).read()

    monkeypatch.setattr(persistence.pq, "ParquetFile", open_reader)
    monkeypatch.setattr(persistence.pq, "read_table", retained_table)
    try:
        yield readers
    finally:
        for reader in readers:
            reader.close(force=True)


@pytest.mark.parametrize("rows", ([], [{"value": "用户"}]))
def test_parquet_verification_closes_retained_reader_before_replace(
    tmp_path, monkeypatch, retained_readers, rows
):
    path = tmp_path / "catalog 用户.parquet"
    original_replace = os.replace
    replaced = []

    def replace(source, destination):
        assert destination == path
        assert retained_readers and all(reader.closed for reader in retained_readers)
        replaced.append(True)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    persistence.write_parquet_rows_atomic(path, rows, SCHEMA)
    assert replaced == [True]
    assert persistence.read_parquet_rows(path) == rows
    assert all(reader.closed for reader in retained_readers)
    assert list(tmp_path.iterdir()) == [path]


def test_read_parquet_rows_closes_retained_reader_before_return(tmp_path, retained_readers):
    path = tmp_path / "catalog.parquet"
    persistence.pq.write_table(pa.Table.from_pylist([{"value": "old"}], schema=SCHEMA), path)
    assert persistence.read_parquet_rows(path) == [{"value": "old"}]
    assert retained_readers and all(reader.closed for reader in retained_readers)
    replacement = tmp_path / "replacement.parquet"
    persistence.pq.write_table(pa.Table.from_pylist([{"value": "new"}], schema=SCHEMA), replacement)
    os.replace(replacement, path)
    assert persistence.read_parquet_rows(path) == [{"value": "new"}]
    assert all(reader.closed for reader in retained_readers)


@pytest.mark.parametrize("rows", ([], [{"value": "用户"}]))
def test_real_parquet_readers_are_closed_before_repeated_replacement(tmp_path, rows):
    path = tmp_path / "catalog 用户.parquet"
    # Exercise nonempty-to-empty tombstone restoration as well as nonempty replacement.
    persistence.write_parquet_rows_atomic(path, [{"value": "original"}], SCHEMA)
    assert persistence.read_parquet_rows(path) == [{"value": "original"}]
    persistence.write_parquet_rows_atomic(path, rows, SCHEMA)
    assert persistence.read_parquet_rows(path) == rows
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("content", (None, b"broken parquet"))
def test_missing_or_invalid_single_file_keeps_unavailable_result(tmp_path, content):
    path = tmp_path / "catalog.parquet"
    if content is not None:
        path.write_bytes(content)
    assert persistence.read_parquet_rows(path) is None
    if content is not None:
        path.unlink()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage", ("read", "verification", "replace"))
def test_failed_parquet_write_preserves_existing_bytes_and_reports_cleanup_failure(
    tmp_path, monkeypatch, stage
):
    path = tmp_path / "catalog.parquet"
    persistence.write_parquet_rows_atomic(path, [{"value": "original"}], SCHEMA)
    original_bytes = path.read_bytes()
    original_unlink, original_reader = Path.unlink, persistence.pq.ParquetFile
    primary = OSError(f"Synthetic original {stage} failure.")
    candidates = []

    class Reader:
        def __init__(self, source, **kwargs):
            self.reader = original_reader(source, **kwargs)

        def read(self):
            if stage == "read":
                raise primary
            if stage == "verification":
                return pa.Table.from_pylist([], schema=SCHEMA)
            return self.reader.read()

        def close(self):
            self.reader.close()

    def fail_replace(_source, _destination):
        raise primary

    def fail_temporary_unlink(candidate, *args, **kwargs):
        if candidate.suffix == ".tmp":
            candidates.append(candidate)
            raise PermissionError("Synthetic cleanup sharing violation.")
        return original_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(persistence.pq, "ParquetFile", Reader)
    if stage == "replace":
        monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)
    try:
        with pytest.raises((OSError, RuntimeError)) as caught:
            persistence.write_parquet_rows_atomic(path, [{"value": "new"}], SCHEMA)
        if stage == "verification":
            assert "Parquet verification failed" in str(caught.value)
        else:
            assert caught.value is primary
        assert len(candidates) == 1
        assert any("cleanup sharing violation" in note for note in caught.value.__notes__)
        assert any(candidates[0].name in note for note in caught.value.__notes__)
        assert path.read_bytes() == original_bytes
    finally:
        for candidate in candidates:
            original_unlink(candidate, missing_ok=True)


@pytest.mark.parametrize("read_fails", (False, True))
def test_parquet_reader_close_failure_cannot_mask_a_read_error_or_publish_success(
    tmp_path, monkeypatch, read_fails
):
    path = tmp_path / "catalog.parquet"
    persistence.write_parquet_rows_atomic(path, [{"value": "original"}], SCHEMA)
    original_bytes = path.read_bytes()
    original_reader = persistence.pq.ParquetFile
    primary = ValueError("Synthetic original Parquet read failure.")
    cleanup = OSError("Synthetic Parquet reader close failure.")
    closed = []

    class Reader:
        def __init__(self, source, **kwargs):
            self.reader = original_reader(source, **kwargs)

        def read(self):
            if read_fails:
                raise primary
            return self.reader.read()

        def close(self):
            self.reader.close()
            closed.append(self.reader.closed)
            raise cleanup

    monkeypatch.setattr(persistence.pq, "ParquetFile", Reader)
    with pytest.raises((ValueError, OSError)) as caught:
        persistence.write_parquet_rows_atomic(path, [{"value": "new"}], SCHEMA)
    assert caught.value is (primary if read_fails else cleanup)
    if read_fails:
        assert any("reader close failure" in note for note in primary.__notes__)
    assert closed == [True]
    assert path.read_bytes() == original_bytes
    assert list(tmp_path.iterdir()) == [path]


def test_successful_parquet_publication_does_not_unlink_a_reused_temporary_name(
    tmp_path, monkeypatch
):
    path = tmp_path / "catalog.parquet"
    original_replace = os.replace
    reused = []

    def replace_then_reuse(source, destination):
        original_replace(source, destination)
        source.write_bytes(b"Another synthetic operation owns this name.")
        reused.append(source)

    monkeypatch.setattr(os, "replace", replace_then_reuse)
    persistence.write_parquet_rows_atomic(path, [{"value": "new"}], SCHEMA)
    assert persistence.read_parquet_rows(path) == [{"value": "new"}]
    assert len(reused) == 1
    assert reused[0].read_bytes() == b"Another synthetic operation owns this name."


def test_reused_read_and_close_exception_keeps_primary_notes_and_bounds_new_diagnostic(
    tmp_path, monkeypatch
):
    primary = OSError("Original read failure " + "x" * 1_000)
    original_note = "Original note " + "y" * 1_000
    primary.add_note(original_note)
    # No long path is created; the injected reader tests only error diagnostics.
    candidate = tmp_path / ("long-prefix-" + "z" * 600 + "-catalog.parquet")

    class Reader:
        def read(self):
            raise primary

        def close(self):
            raise primary

    monkeypatch.setattr(persistence.pq, "ParquetFile", lambda *_args, **_kwargs: Reader())
    with pytest.raises(OSError) as caught:
        persistence._read_parquet_table(candidate)
    assert caught.value is primary
    assert str(primary) == "Original read failure " + "x" * 1_000
    assert primary.__notes__[0] == original_note
    assert len(primary.__notes__) == 2
    diagnostic = primary.__notes__[1]
    assert "-catalog.parquet" in diagnostic
    assert "Original read failure" in diagnostic
    assert "x" * 241 not in diagnostic and "z" * 481 not in diagnostic
    assert len(diagnostic) < 850
