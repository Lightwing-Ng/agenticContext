"""Read exact registry bytes while preserving Windows mutation evidence.

Code version: v1.0.0-codex.0
"""

import hashlib
import os
from types import SimpleNamespace

import pytest

from app.core import wechat_text_observations as observations
from app.core import windows_file_metadata


class WindowsMetadataOS:
    name = "nt"

    def __init__(self, path):
        self.path = path

    def __getattr__(self, name):
        return getattr(os, name)

    def fstat(self, descriptor):
        state = os.fstat(descriptor)
        path_state = self.path.stat()
        return SimpleNamespace(
            st_dev=state.st_dev, st_ino=state.st_ino, st_size=state.st_size,
            st_mtime_ns=state.st_mtime_ns, st_ctime_ns=state.st_ctime_ns + 100,
            st_birthtime_ns=getattr(path_state, "st_birthtime_ns", path_state.st_ctime_ns),
        )


def test_registry_compares_the_same_native_mutation_identity(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    raw = b'{"value":"preserved"}\r\n'
    path.write_bytes(raw)
    monkeypatch.setattr(observations, "os", WindowsMetadataOS(path))
    monkeypatch.setattr(windows_file_metadata, "file_change_time", lambda *_args, **_kwargs: 55)
    value, digest, _identity = observations._read_json(path)
    assert value == {"value": "preserved"}
    assert digest == hashlib.sha256(raw).hexdigest()


def test_native_mutation_during_registry_read_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    path.write_bytes(b'{"value":"preserved"}')
    changes = iter((55, 55, 56))
    monkeypatch.setattr(observations, "os", WindowsMetadataOS(path))
    monkeypatch.setattr(windows_file_metadata, "file_change_time", lambda *_args, **_kwargs: next(changes))
    with pytest.raises(ValueError, match="changed while reading"):
        observations._read_json(path)


def test_registry_crlf_hash_uses_exact_binary_source_bytes(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    raw = b'{"value":"preserved"}\r\n'
    path.write_bytes(raw)
    binary_flag = getattr(os, "O_BINARY", 1 << 28)
    flags_by_descriptor = {}

    class SourceReader:
        def __init__(self, stream, binary):
            self.stream, self.binary = stream, binary

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, limit):
            data = self.stream.read(limit)
            return data if self.binary else data.replace(b"\r\n", b"\n")

    class WindowsCRTOS(WindowsMetadataOS):
        name = "posix"
        O_BINARY = binary_flag

        def open(self, candidate, flags):
            native_flags = flags if hasattr(os, "O_BINARY") else flags & ~binary_flag
            descriptor = os.open(candidate, native_flags)
            flags_by_descriptor[descriptor] = bool(flags & binary_flag)
            return descriptor

        def fdopen(self, descriptor, mode):
            return SourceReader(os.fdopen(descriptor, mode), flags_by_descriptor[descriptor])

        def fstat(self, descriptor):
            state = super().fstat(descriptor)
            state.st_ctime_ns = self.path.stat().st_ctime_ns
            return state

    monkeypatch.setattr(observations, "os", WindowsCRTOS(path))
    _value, digest, _identity = observations._read_json(path)
    assert digest == hashlib.sha256(raw).hexdigest()
