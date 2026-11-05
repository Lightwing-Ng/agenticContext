"""Windows ChangeTime and cross-API identity preserve exact media hashing.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import ctypes
import os
import sys
from types import SimpleNamespace

import pytest

from app.core import local_media_browser as media
from app.core import windows_file_metadata as metadata
from tests.test_chatgpt_flat_media import _write_project


@pytest.fixture
def native_api(monkeypatch):
    class Info(ctypes.Structure):
        _fields_ = [("ChangeTime", ctypes.c_longlong), ("FileAttributes", ctypes.c_uint32)]

    state = SimpleNamespace(created=[], closed=[], attributes=0, valid=True, handle=42)

    class Kernel:
        def CreateFileW(self, *args):
            state.created.append(args)
            return state.handle

        def GetFileInformationByHandleEx(self, handle, kind, pointer, size):
            assert kind == 0 and size == ctypes.sizeof(Info)
            pointer._obj.ChangeTime = 123456789
            pointer._obj.FileAttributes = state.attributes
            return state.valid

        def CloseHandle(self, handle):
            state.closed.append(handle)

    class Ctypes:
        def __getattr__(self, name):
            return getattr(ctypes, name)

        def get_last_error(self):
            return 5

        def WinError(self, code):
            return OSError(code, "Synthetic native metadata failure")

    monkeypatch.setattr(metadata, "_native_api", lambda: (Ctypes(), Kernel(), Info))
    return state


def test_windows_path_query_is_metadata_only_no_follow_and_always_closed(native_api, tmp_path):
    assert metadata.file_change_time(tmp_path / "media.png") == 123456789
    _path, access, sharing, _security, creation, flags, _template = native_api.created[0]
    assert access == 0x0080
    assert sharing == 7
    assert creation == 3
    assert flags & 0x00200000
    assert native_api.closed == [42]


def test_existing_descriptor_is_borrowed_without_closing_its_owner(native_api, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 99))
    assert metadata.file_change_time(tmp_path / "media.png", descriptor=7) == 123456789
    assert native_api.created == native_api.closed == []


@pytest.mark.parametrize("failure", ["open", "query", "reparse", "directory"])
def test_native_metadata_failures_never_become_a_hash_identity(native_api, tmp_path, failure):
    if failure == "open":
        native_api.handle = ctypes.c_void_p(-1).value
    elif failure == "query":
        native_api.valid = False
    elif failure == "reparse":
        native_api.attributes = 0x400
    else:
        native_api.attributes = 0x10
    with pytest.raises(OSError):
        metadata.file_change_time(tmp_path / "media.png")
    assert native_api.closed == ([] if failure == "open" else [42])


@pytest.fixture
def windows_identity(monkeypatch):
    original_identity = media.LocalMediaCatalog._content_stat_identity

    class WindowsOS:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)

    def cross_api_stat(state, path, *, descriptor=None):
        values = {name: getattr(state, name) for name in dir(state) if name.startswith("st_")}
        # Model Windows path ctime=birth time and fd ctime=change time.
        values.update(st_birthtime_ns=1, st_ctime_ns=1 if descriptor is None else 2)
        return original_identity(SimpleNamespace(**values), path, descriptor=descriptor)

    monkeypatch.setattr(media, "os", WindowsOS())
    monkeypatch.setattr(media.LocalMediaCatalog, "_content_stat_identity", staticmethod(cross_api_stat))
    if os.name != "nt":
        # Only a foreign host needs a surrogate for the native Windows API.
        # Windows stat().ctime is creation time and cannot model byte mutations.
        monkeypatch.setattr(
            metadata, "file_change_time", lambda path, descriptor=None: os.stat(path).st_ctime_ns,
        )
    return original_identity


def test_windows_hashes_and_cache_preserve_same_size_reset_mtime_changes(tmp_path, windows_identity):
    directory = _write_project(tmp_path, (("one.png", "one", b"same"), ("two.png", "two", b"same")))
    catalog = media.LocalMediaCatalog(tmp_path)
    assert catalog.query(source="chatgpt", chatgpt_all_media=True).total_count == 1
    second = directory / "two.png"
    original = second.stat()
    second.write_bytes(b"else")
    os.utime(second, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert catalog.query(source="chatgpt", chatgpt_all_media=True).total_count == 2


@pytest.mark.parametrize("changed", ["st_dev", "st_ino", "st_size", "st_mtime_ns", "st_birthtime_ns"])
def test_windows_identity_keeps_every_stable_file_field(tmp_path, windows_identity, changed):
    path = tmp_path / "media.png"
    path.write_bytes(b"same")
    values = {name: 1 for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_birthtime_ns")}
    identity = windows_identity
    baseline = identity(SimpleNamespace(**values), path)
    values[changed] += 1
    assert identity(SimpleNamespace(**values), path) != baseline
