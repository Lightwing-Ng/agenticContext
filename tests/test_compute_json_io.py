"""Real-file compute JSON sharing, bounded reads, and native handle ownership.

Code version: v1.0.0-codex.1
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import stat
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from app.core.agent import compute_jobs as jobs


class _ObservedReader:
    def __init__(self, stream, before_read):
        self.stream = stream
        self.before_read = before_read

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *exception):
        return self.stream.__exit__(*exception)

    def fileno(self):
        return self.stream.fileno()

    def read(self, count):
        self.before_read(self.stream, count)
        return self.stream.read(count)


@pytest.mark.parametrize("artifact", ("metadata", "progress"))
@pytest.mark.parametrize("reader_kind", ("production", "ordinary-control"))
def test_real_production_reader_overlaps_atomic_writer(
    tmp_path, monkeypatch, artifact, reader_kind
):
    path = tmp_path / f"{artifact} 用户.json"
    old, new = {"value": 1}, {"value": 2}
    jobs._atomic_write_json(path, old)
    entered, release = Event(), Event()
    original_open = jobs._open_json_reader
    readers, replacements = [], []
    original_replace = os.replace

    def hold_reader(stream, count):
        assert count == 1_025
        assert not stream.closed
        entered.set()
        assert release.wait(10), "The synthetic writer did not release its reader."

    @contextmanager
    def observed_open(candidate):
        assert candidate == path
        context = (
            original_open(candidate)
            if reader_kind == "production"
            else candidate.open("rb")
        )
        with context as stream:
            readers.append(stream)
            yield _ObservedReader(stream, hold_reader)

    def observed_replace(source, destination):
        assert Path(destination) == path
        assert entered.is_set() and not release.is_set()
        assert len(readers) == 1 and not readers[0].closed
        assert json.loads(Path(source).read_text(encoding="utf-8")) == new
        replacements.append(True)
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(jobs, "_open_json_reader", observed_open)
        patch.setattr(os, "replace", observed_replace)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(jobs._read_json_object, path, maximum_bytes=1_024)
            try:
                assert entered.wait(10), "The production parser did not reach its real reader."
                if os.name == "nt" and reader_kind == "ordinary-control":
                    with pytest.raises(PermissionError) as caught:
                        jobs._atomic_write_json(path, new)
                    assert caught.value.winerror in {5, 32}
                    expected_at_path = old
                else:
                    jobs._atomic_write_json(path, new)
                    expected_at_path = new
                assert replacements == [True]
                assert json.loads(path.read_text(encoding="utf-8")) == expected_at_path
                assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]
            finally:
                release.set()
            assert pending.result(timeout=10) == old
            assert readers[0].closed
    jobs._atomic_write_json(path, new)
    assert jobs._read_json_object(path, maximum_bytes=1_024) == new
    path.unlink()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "content, maximum, message",
    (
        (b'{"x":"\xff"}', 64, "Invalid JSON"),
        (b"\xff\xfe{\x00}\x00", 64, "Invalid JSON"),
        (b"\xef\xbb\xbf{}", 64, "Invalid JSON"),
        (b"[]", 64, "must contain one object"),
        (b'{"value":1}', 4, "byte limit"),
        (b"{broken", 64, "Invalid JSON"),
    ),
)
def test_invalid_json_inputs_preserve_validation_and_close_reader(
    tmp_path, monkeypatch, content, maximum, message
):
    path = tmp_path / "record.json"
    path.write_bytes(content)
    original = jobs._open_json_reader
    readers = []

    @contextmanager
    def record_reader(candidate):
        with original(candidate) as stream:
            readers.append(stream)
            yield stream

    monkeypatch.setattr(jobs, "_open_json_reader", record_reader)
    with pytest.raises(jobs.ComputeJobError, match=message):
        jobs._read_json_object(path, maximum_bytes=maximum)
    assert len(readers) == 1 and readers[0].closed
    assert path.read_bytes() == content


def test_json_limit_counts_utf8_bytes_and_allows_exact_boundary(tmp_path):
    path = tmp_path / "record.json"
    content = '{"value":"用户"}'.encode("utf-8")
    path.write_bytes(content)
    assert jobs._read_json_object(path, maximum_bytes=len(content)) == {"value": "用户"}
    with pytest.raises(jobs.ComputeJobError, match="byte limit"):
        jobs._read_json_object(path, maximum_bytes=len(content) - 1)
    with pytest.raises(ValueError, match="negative"):
        jobs._read_json_object(path, maximum_bytes=-1)


def test_growth_after_handle_stat_is_still_bounded(tmp_path, monkeypatch):
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    original = jobs._open_json_reader
    requests, readers = [], []

    def grow_before_read(_stream, count):
        requests.append(count)
        with path.open("ab") as writer:
            writer.write(b" " * 64)

    @contextmanager
    def growing_reader(candidate):
        with original(candidate) as stream:
            readers.append(stream)
            yield _ObservedReader(stream, grow_before_read)

    monkeypatch.setattr(jobs, "_open_json_reader", growing_reader)
    with pytest.raises(jobs.ComputeJobError, match="byte limit"):
        jobs._read_json_object(path, maximum_bytes=16)
    assert requests == [17]
    assert readers[0].closed


@pytest.mark.parametrize("replacement", ("symlink", "directory", "fifo-or-directory"))
def test_leaf_replacement_between_path_check_and_open_is_rejected(
    tmp_path, monkeypatch, replacement
):
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"must_not_read":true}')
    original = jobs._open_json_reader
    original_os_open = os.open
    replacements = []

    def checked_os_open(candidate, flags, *args, **kwargs):
        if Path(candidate) == path and replacement == "fifo-or-directory":
            assert flags & os.O_NONBLOCK
        return original_os_open(candidate, flags, *args, **kwargs)

    def replaced_reader(candidate):
        replacements.append(True)
        path.unlink()
        if replacement == "symlink":
            path.symlink_to(outside)
        elif replacement == "fifo-or-directory" and os.name != "nt":
            os.mkfifo(path)
        else:
            path.mkdir()
        return original(candidate)

    if os.name != "nt":
        monkeypatch.setattr(os, "open", checked_os_open)
    monkeypatch.setattr(jobs, "_open_json_reader", replaced_reader)
    with pytest.raises(jobs.ComputeJobError, match="Invalid JSON|regular JSON"):
        jobs._read_json_object(path, maximum_bytes=1_024)
    assert replacements == [True]
    assert outside.read_bytes() == b'{"must_not_read":true}'


def test_reparse_metadata_is_rejected_before_read(tmp_path, monkeypatch):
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    original_open, original_stat = jobs._open_json_reader, os.fstat
    readers, reads = [], []

    def count_read(_stream, _count):
        reads.append(True)

    @contextmanager
    def record_reader(candidate):
        with original_open(candidate) as stream:
            readers.append(stream)
            yield _ObservedReader(stream, count_read)

    def changed_metadata(descriptor):
        if readers and descriptor == readers[0].fileno():
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600, st_size=2, st_file_attributes=0x400
            )
        return original_stat(descriptor)

    monkeypatch.setattr(jobs, "_open_json_reader", record_reader)
    monkeypatch.setattr(os, "fstat", changed_metadata)
    with pytest.raises(jobs.ComputeJobError, match="regular JSON"):
        jobs._read_json_object(path, maximum_bytes=1_024)
    assert reads == []
    assert readers[0].closed


@pytest.mark.parametrize("adoption", ("success", "raise", "negative", "invalid-handle"))
def test_native_reader_shares_delete_and_transfers_handle_ownership_once(
    tmp_path, monkeypatch, adoption
):
    calls = []
    primary = OSError("Synthetic descriptor adoption failure.")

    def create(name, access, sharing, attributes, disposition, flags, template):
        assert name.endswith("record 用户.json")
        assert access == 0x80000000
        assert sharing == 1 | 2 | 4
        assert attributes is None
        assert disposition == 3
        assert flags == 0x00200000
        assert template is None
        calls.append("create")
        return ctypes.c_void_p(-1).value if adoption == "invalid-handle" else 123

    def close(handle):
        assert handle == 123
        calls.append("close")
        return False

    def adopt(handle, flags):
        assert handle == 123
        assert flags & os.O_NOINHERIT == os.O_NOINHERIT
        calls.append("adopt")
        if adoption == "raise":
            raise primary
        return -1 if adoption == "negative" else 789

    kernel = SimpleNamespace(CreateFileW=create, CloseHandle=close)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 2, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda _code: primary, raising=False)
    monkeypatch.setattr(os, "O_BINARY", getattr(os, "O_BINARY", 0), raising=False)
    monkeypatch.setattr(os, "O_NOINHERIT", getattr(os, "O_NOINHERIT", 0), raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=adopt))
    if adoption == "success":
        assert jobs._open_windows_json_descriptor(tmp_path / "record 用户.json") == 789
        assert calls == ["create", "adopt"]
    elif adoption == "invalid-handle":
        with pytest.raises(OSError) as caught:
            jobs._open_windows_json_descriptor(tmp_path / "record 用户.json")
        assert caught.value is primary
        assert calls == ["create"]
    else:
        with pytest.raises(OSError) as caught:
            jobs._open_windows_json_descriptor(tmp_path / "record 用户.json")
        if adoption == "raise":
            assert caught.value is primary
        assert "unadopted compute JSON reader" in caught.value.__notes__[0]
        assert calls == ["create", "adopt", "close"]


def test_binary_stream_construction_failure_closes_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    original_close = os.close
    descriptors, closed = [], []
    primary = OSError("Synthetic binary stream failure.")

    def failed_fdopen(descriptor, mode, *, closefd):
        assert mode == "rb"
        assert closefd is False
        descriptors.append(descriptor)
        raise primary

    def close(descriptor):
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(os, "fdopen", failed_fdopen)
    monkeypatch.setattr(os, "close", close)
    with pytest.raises(OSError) as caught:
        with jobs._open_json_reader(path):
            pytest.fail("A failed reader must not yield.")
    assert caught.value is primary
    assert closed == descriptors and len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert path.read_bytes() == b"{}"


def test_partial_stream_construction_does_not_close_another_operations_descriptor(
    tmp_path, monkeypatch
):
    target, other = tmp_path / "record.json", tmp_path / "other.txt"
    target.write_bytes(b"{}")
    other.write_bytes(b"Unrelated synthetic bytes.")
    original_fdopen, original_close = os.fdopen, os.close
    primary = OSError("Synthetic buffer construction failure.")
    foreign, closed = [], []

    def failed_fdopen(descriptor, mode, **kwargs):
        stream = original_fdopen(descriptor, mode, **kwargs)
        stream.close()
        other_descriptor = os.open(other, os.O_RDONLY)
        if kwargs.get("closefd", True) and other_descriptor != descriptor:
            os.dup2(other_descriptor, descriptor)
            original_close(other_descriptor)
            other_descriptor = descriptor
        foreign.append(other_descriptor)
        raise primary

    def close(descriptor):
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(os, "fdopen", failed_fdopen)
    monkeypatch.setattr(os, "close", close)
    try:
        with pytest.raises(OSError) as caught:
            with jobs._open_json_reader(target):
                pytest.fail("A failed reader must not yield.")
        assert caught.value is primary
        assert foreign[0] not in closed
        assert os.read(foreign[0], 64) == b"Unrelated synthetic bytes."
    finally:
        if foreign and foreign[0] not in closed:
            original_close(foreign[0])


@pytest.mark.parametrize("failure", ("read", "validation", "close-only"))
def test_json_reader_preserves_primary_error_and_all_cleanup_diagnostics(
    tmp_path, monkeypatch, failure
):
    path = tmp_path / "record.json"
    path.write_bytes(b"{}")
    original_fdopen, original_close = os.fdopen, os.close
    read_error = OSError("Synthetic original JSON read failure.")
    stream_error = OSError("Synthetic stream close failure.")
    closed = []

    class FailedClose:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, count):
            if failure == "read":
                raise read_error
            return self.stream.read(count)

        def close(self):
            self.stream.close()
            raise stream_error

    def failed_close_stream(descriptor, mode, **kwargs):
        return FailedClose(original_fdopen(descriptor, mode, **kwargs))

    def failed_descriptor_close(descriptor):
        closed.append(descriptor)
        original_close(descriptor)
        raise OSError("Synthetic descriptor close failure.")

    monkeypatch.setattr(os, "fdopen", failed_close_stream)
    monkeypatch.setattr(os, "close", failed_descriptor_close)
    with pytest.raises(jobs.ComputeJobError) as caught:
        jobs._read_json_object(path, maximum_bytes=1 if failure == "validation" else 1_024)
    if failure == "validation":
        assert "byte limit" in str(caught.value)
        primary = caught.value
    else:
        assert "Invalid JSON" in str(caught.value)
        primary = caught.value.__cause__
        assert primary is (read_error if failure == "read" else stream_error)
        assert all(note in caught.value.__notes__ for note in primary.__notes__)
        assert any(str(primary) in note for note in caught.value.__notes__)
    if failure != "close-only":
        assert "stream close failure" in primary.__notes__[0]
    assert "descriptor close failure" in primary.__notes__[-1]
    worker_diagnostic = " ".join((str(caught.value), *caught.value.__notes__))
    if failure != "validation":
        assert str(primary) in worker_diagnostic
    assert "descriptor close failure" in worker_diagnostic
    if failure != "close-only":
        assert "stream close failure" in worker_diagnostic
    assert len(closed) == 1
    assert path.read_bytes() == b"{}"
