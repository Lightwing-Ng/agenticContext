"""Windows publication ABI models and retained-handle failure contracts.

Code version: v1.0.0-codex.1
"""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from app.core.agent import _windows_compute_io as windows_io
from app.core.agent import compute_jobs as jobs


@pytest.fixture
def native_model(monkeypatch):
    calls = []
    error = OSError("Synthetic Windows publication API failure.")
    configuration = SimpleNamespace(
        create=123, adopt=456, attributes=0x10, set_ok=True, close_ok=True,
        path="\\\\?\\C:\\compute 用户\\record.json", adopt_error=None,
    )

    def create(*arguments):
        calls.append(("create", arguments))
        return configuration.create

    def close(handle):
        calls.append(("close", handle))
        return configuration.close_ok

    def information(handle, kind, buffer, count):
        assert handle == 123 and kind == 9
        assert count == ctypes.sizeof(windows_io._AttributeTagInfo)
        ctypes.cast(buffer, ctypes.POINTER(windows_io._AttributeTagInfo)).contents.attributes = (
            configuration.attributes
        )
        return True

    def set_information(handle, kind, buffer, count):
        assert handle == 123
        calls.append(("set", kind, ctypes.string_at(buffer, count)))
        return configuration.set_ok

    def final_path(handle, buffer, count, flags):
        assert handle == 123 and flags == 0
        if buffer is None:
            assert count == 0
            return len(configuration.path) + 1
        buffer.value = configuration.path
        return len(configuration.path)

    def adopt(handle, flags):
        assert handle == 123
        calls.append(("adopt", flags))
        if configuration.adopt_error is not None:
            raise configuration.adopt_error
        return configuration.adopt

    def get_handle(descriptor):
        assert descriptor == 456
        return 123

    kernel = SimpleNamespace(
        CreateFileW=create, CloseHandle=close, GetFileInformationByHandleEx=information,
        SetFileInformationByHandle=set_information, GetFinalPathNameByHandleW=final_path,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 50, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda _code: error, raising=False)
    monkeypatch.setattr(os, "O_BINARY", getattr(os, "O_BINARY", 0), raising=False)
    monkeypatch.setattr(os, "O_NOINHERIT", getattr(os, "O_NOINHERIT", 0), raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        open_osfhandle=adopt, get_osfhandle=get_handle,
    ))
    return windows_io._WindowsComputeIO(), calls, configuration, error


def test_native_source_is_created_once_with_delete_access_and_single_crt_owner(
    tmp_path, native_model
):
    native, calls, _configuration, _error = native_model
    assert native.create_source(tmp_path / "record 用户.tmp") == 456
    create = calls[0][1]
    assert create[0].endswith("record 用户.tmp")
    assert create[1:] == (0xC0010000, 1, None, 1, 0x00200000, None)
    assert calls[1] == ("adopt", os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ("invalid", "adoption", "negative-adoption"))
def test_source_creation_failure_cleans_only_an_owned_native_handle(
    tmp_path, native_model, failure
):
    native, calls, configuration, error = native_model
    if failure == "invalid":
        configuration.create = ctypes.c_void_p(-1).value
    elif failure == "adoption":
        configuration.adopt_error = error
    else:
        configuration.adopt = -1
    with pytest.raises(OSError):
        native.create_source(tmp_path / "record.tmp")
    if failure == "invalid":
        assert len(calls) == 1
    else:
        assert calls[-2:] == [("set", 4, b"\x01"), ("close", 123)]


def test_source_adoption_failure_retains_removal_and_close_diagnostics(tmp_path, native_model):
    native, calls, configuration, error = native_model
    configuration.adopt_error = ValueError("Synthetic original adoption failure.")
    configuration.set_ok = configuration.close_ok = False
    with pytest.raises(ValueError) as caught:
        native.create_source(tmp_path / "record.tmp")
    assert caught.value is configuration.adopt_error
    assert "source removal" in caught.value.__notes__[0]
    assert "source close" in caught.value.__notes__[2]
    assert all(str(error) in note for note in caught.value.__notes__[::2])
    assert all("record.tmp" in note for note in caught.value.__notes__[1::2])
    assert calls[-1] == ("close", 123)


def test_parent_pin_uses_data_access_without_delete_sharing(tmp_path, native_model):
    native, calls, _configuration, _error = native_model
    assert native.open_parent(tmp_path) == 123
    assert calls[0][1][1:] == (0x81, 3, None, 3, 0x02200000, None)
    assert len(calls) == 1


@pytest.mark.parametrize("attributes", (0, 0x410))
def test_parent_non_directory_or_reparse_is_rejected_and_closed(
    tmp_path, native_model, attributes
):
    native, calls, configuration, _error = native_model
    configuration.attributes = attributes
    with pytest.raises(OSError, match="regular directory"):
        native.open_parent(tmp_path)
    assert calls[-1] == ("close", 123)


def test_native_rename_ex_uses_posix_flags_correct_alignment_and_utf16_nul(
    tmp_path, native_model
):
    native, calls, _configuration, _error = native_model
    target = tmp_path / "record 用户 🧪.json"
    native.publish(456, target)
    assert len(calls) == 1
    _, kind, buffer = calls[0]
    assert kind == 22
    info = windows_io._RenameInfo.from_buffer_copy(buffer)
    expected = windows_io._extended_name(target).encode("utf-16-le")
    assert info.flags == 3 and info.root is None
    assert info.name_length == len(expected)
    offset = windows_io._RenameInfo.name.offset
    assert offset == (20 if ctypes.sizeof(ctypes.c_void_p) == 8 else 12)
    assert buffer[offset:offset + len(expected)] == expected
    assert buffer[offset + len(expected):offset + len(expected) + 2] == b"\x00\x00"


def test_native_unsupported_publication_fails_without_retry_or_legacy_fallback(
    tmp_path, native_model
):
    native, calls, configuration, error = native_model
    configuration.set_ok = False
    with pytest.raises(OSError) as caught:
        native.publish(456, tmp_path / "record.json")
    assert caught.value is error
    assert len(calls) == 1 and calls[0][1] == 22
    assert "no fallback" in caught.value.__notes__[0]


def test_native_post_publication_checks_the_same_handle_final_path(tmp_path, native_model):
    native, _calls, configuration, _error = native_model
    target = tmp_path / "record 用户.json"
    configuration.path = windows_io._extended_name(target)
    native.verify_published(456, target)
    configuration.path = windows_io._extended_name(target.with_name("unexpected.json"))
    with pytest.raises(OSError, match="expected path"):
        native.verify_published(456, target)


@pytest.fixture
def publication_model(tmp_path, monkeypatch):
    """Exercise production orchestration with real descriptors and injected native calls."""
    events, descriptors = [], []
    configuration = SimpleNamespace(failures={}, source=None, published=False)
    original_stat, original_close = os.fstat, os.close

    def event(label):
        events.append(label)
        failure = configuration.failures.get(label)
        if failure is not None:
            raise failure

    class Native:
        def open_parent(self, path):
            assert path == tmp_path
            event("parent")
            return "pinned parent"

        def final_path(self, parent):
            assert parent == "pinned parent"
            return str(tmp_path)

        def create_source(self, path):
            event("create")
            configuration.source = path
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            descriptors.append(descriptor)
            return descriptor

        def publish(self, descriptor, target):
            assert descriptor == descriptors[0] and target.parent == tmp_path
            assert original_stat(descriptor).st_size == len(b'{"value":2}')
            event("publish")
            configuration.published = True

        def verify_published(self, descriptor, target):
            assert configuration.published and descriptor == descriptors[0]
            event("verify")

        def discard(self, descriptor):
            assert descriptor == descriptors[0] and not configuration.published
            original_stat(descriptor)
            event("discard")

        def close_handle(self, parent):
            assert parent == "pinned parent"
            event("parent-close")

    def metadata(descriptor):
        current = original_stat(descriptor)
        return SimpleNamespace(
            st_mode=current.st_mode, st_nlink=current.st_nlink, st_file_attributes=0,
        )

    def close(descriptor):
        assert descriptor in descriptors
        original_close(descriptor)
        event("descriptor-close")

    monkeypatch.setattr(windows_io, "_WindowsComputeIO", Native)
    monkeypatch.setattr(os, "fstat", metadata)
    monkeypatch.setattr(os, "close", close)
    return configuration, events, descriptors


def test_publication_keeps_descriptor_open_through_fsync_and_verification(
    tmp_path, monkeypatch, publication_model
):
    configuration, events, descriptors = publication_model
    original_sync = os.fsync

    def sync(descriptor):
        assert descriptor == descriptors[0] and "publish" not in events
        events.append("fsync")
        original_sync(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    windows_io.atomic_write_bytes(tmp_path / "record.json", b'{"value":2}')
    assert events == [
        "parent", "create", "fsync", "publish", "verify", "descriptor-close", "parent-close",
    ]
    assert configuration.source.read_bytes() == b'{"value":2}'
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize("stage", ("publish", "verify", "descriptor-close", "parent-close"))
def test_publication_failure_is_never_success_and_only_unpublished_source_is_discarded(
    tmp_path, publication_model, stage
):
    configuration, events, _descriptors = publication_model
    primary = OSError(f"Synthetic {stage} failure.")
    configuration.failures[stage] = primary
    with pytest.raises(OSError) as caught:
        windows_io.atomic_write_bytes(tmp_path / "record.json", b'{"value":2}')
    assert caught.value is primary
    assert ("discard" in events) == (stage == "publish")
    assert events[-2:] == ["descriptor-close", "parent-close"]
    assert configuration.source.exists()
    if stage in {"descriptor-close", "parent-close"}:
        assert any("published target candidate" in note for note in primary.__notes__)
        assert any("record.json" in note for note in primary.__notes__)


def test_publication_primary_preserves_all_secondary_cleanup_failures(tmp_path, publication_model):
    configuration, events, _descriptors = publication_model
    primary = ValueError("Synthetic original publication failure.")
    configuration.failures = {
        "publish": primary,
        "discard": OSError("Synthetic source removal failure."),
        "descriptor-close": OSError("Synthetic descriptor close failure."),
        "parent-close": OSError("Synthetic parent close failure."),
    }
    with pytest.raises(ValueError) as caught:
        windows_io.atomic_write_bytes(tmp_path / "record.json", b'{"value":2}')
    assert caught.value is primary
    assert len(primary.__notes__) == 6
    for text in ("source removal", "descriptor close", "parent close"):
        assert any(text in note for note in primary.__notes__)
    assert any(configuration.source.name in note for note in primary.__notes__)
    assert any("source candidate may remain" in note for note in primary.__notes__)
    assert events[-3:] == ["discard", "descriptor-close", "parent-close"]


def test_partial_writer_construction_keeps_single_descriptor_owner(
    tmp_path, monkeypatch, publication_model
):
    _configuration, events, descriptors = publication_model
    original = os.fdopen
    primary = OSError("Synthetic writer stream construction failure.")

    def fail(descriptor, mode, *, closefd):
        assert mode == "wb" and closefd is False
        borrowed = original(descriptor, mode, closefd=closefd)
        borrowed.close()
        os.fstat(descriptor)
        raise primary

    monkeypatch.setattr(os, "fdopen", fail)
    with pytest.raises(OSError) as caught:
        windows_io.atomic_write_bytes(tmp_path / "record.json", b'{"value":2}')
    assert caught.value is primary
    assert events == ["parent", "create", "discard", "descriptor-close", "parent-close"]
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize("stage", ("write", "flush", "fsync", "stream-close"))
def test_writer_io_failure_preserves_error_and_cleans_the_correct_handle(
    tmp_path, monkeypatch, publication_model, stage
):
    configuration, events, _descriptors = publication_model
    original_fdopen, original_sync = os.fdopen, os.fsync
    primary = OSError(f"Synthetic {stage} failure.")

    class FailedStream:
        def __init__(self, stream):
            self.stream = stream

        def write(self, content):
            if stage == "write":
                raise primary
            return self.stream.write(content)

        def flush(self):
            if stage == "flush":
                raise primary
            self.stream.flush()

        def close(self):
            self.stream.close()
            if stage == "stream-close":
                raise primary

    def open_stream(descriptor, mode, *, closefd):
        assert closefd is False
        return FailedStream(original_fdopen(descriptor, mode, closefd=closefd))

    def sync(descriptor):
        if stage == "fsync":
            raise primary
        original_sync(descriptor)

    monkeypatch.setattr(os, "fdopen", open_stream)
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(OSError) as caught:
        windows_io.atomic_write_bytes(tmp_path / "record.json", b'{"value":2}')
    assert caught.value is primary
    assert ("publish" in events) == (stage == "stream-close")
    assert ("discard" in events) == (stage != "stream-close")
    assert events[-2:] == ["descriptor-close", "parent-close"]
    assert configuration.published == (stage == "stream-close")


@pytest.mark.parametrize("kind", ("reparse", "hardlink", "directory", "missing-attributes"))
def test_invalid_source_handle_never_receives_json_bytes(
    tmp_path, monkeypatch, publication_model, kind
):
    configuration, events, _descriptors = publication_model
    metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_file_attributes=0)
    if kind == "reparse":
        metadata.st_file_attributes = 0x400
    elif kind == "hardlink":
        metadata.st_nlink = 2
    elif kind == "directory":
        metadata.st_mode = stat.S_IFDIR | 0o700
    else:
        del metadata.st_file_attributes
    monkeypatch.setattr(os, "fstat", lambda _descriptor: metadata)
    with pytest.raises(OSError, match="regular unlinked"):
        windows_io.atomic_write_bytes(tmp_path / "record.json", b'{"value":2}')
    assert "publish" not in events and "discard" in events
    assert configuration.source.read_bytes() == b""


def test_compute_dispatch_serializes_before_windows_publication(tmp_path, monkeypatch):
    calls = []
    host_os = jobs.os
    monkeypatch.setattr(jobs, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(windows_io, "atomic_write_bytes", lambda *arguments: calls.append(arguments))
    jobs._atomic_write_json(tmp_path / "record.json", {"value": "用户"})
    assert calls == [(tmp_path / "record.json", b'{"value":"\\u7528\\u6237"}')]
    assert host_os.name in {"nt", "posix"}
