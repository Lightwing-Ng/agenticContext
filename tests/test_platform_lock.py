"""Mandatory Windows byte-lock contention and content-preservation regressions.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import errno
import importlib.util
import os
from pathlib import Path
from threading import Condition, Event, Thread
from types import SimpleNamespace

import pytest

from app.core import platform_lock


class _MandatoryByteLock:
    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2

    def __init__(self):
        self.condition = Condition()
        self.owner = None

    def locking(self, descriptor, mode, count):
        assert count == 1
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        with self.condition:
            if mode == self.LK_UNLCK:
                assert self.owner == descriptor
                self.owner = None
                self.condition.notify_all()
                return
            if mode == self.LK_NBLCK and self.owner is not None:
                raise PermissionError(errno.EACCES, "The byte is already locked.")
            if not self.condition.wait_for(lambda: self.owner is None, timeout=5):
                raise OSError(errno.EDEADLK, "The modeled lock was not released.")
            self.owner = descriptor


class _StaleEmptyHandle:
    """Retain the EOF observation made before another initializer locked byte zero."""

    def __init__(self, handle, mandatory):
        self.handle = handle
        self.mandatory = mandatory

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def seek(self, offset, whence=os.SEEK_SET):
        if offset == 0 and whence == os.SEEK_END:
            return self.handle.seek(0)
        return self.handle.seek(offset, whence)

    def flush(self):
        with self.mandatory.condition:
            if self.mandatory.owner not in {None, self.handle.fileno()}:
                raise PermissionError(errno.EACCES, "Another handle owns this byte.")
        return self.handle.flush()


@pytest.fixture(params=("model", "native"))
def locked_file(request, tmp_path: Path):
    native = request.param == "native"
    if native and os.name != "nt":
        pytest.skip("The native mandatory-byte-lock proof requires Windows.")
    specification = importlib.util.spec_from_file_location(
        "platform_lock_contention_probe", platform_lock.__file__,
    )
    assert specification is not None and specification.loader is not None
    helper = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(helper)
    if native:
        import msvcrt

        mandatory = msvcrt
    else:
        mandatory = _MandatoryByteLock()
        helper.os = SimpleNamespace(name="nt", SEEK_END=os.SEEK_END)
        helper.msvcrt = mandatory
    path = tmp_path / "coordination.lock"
    original = b"" if native else b"0"
    path.write_bytes(original)
    owner = path.open("r+", encoding="ascii")
    contender = path.open("r+", encoding="ascii")
    observed = contender if native else _StaleEmptyHandle(contender, mandatory)
    owner.seek(0)
    mandatory.locking(owner.fileno(), mandatory.LK_NBLCK, 1)
    released = False

    def release_owner():
        nonlocal released
        if not released:
            owner.seek(0)
            mandatory.locking(owner.fileno(), mandatory.LK_UNLCK, 1)
            released = True

    try:
        yield SimpleNamespace(
            native=native, helper=helper, path=path, original=original,
            contender=observed, release_owner=release_owner,
        )
    finally:
        release_owner()
        contender.close()
        owner.close()


def test_nonblocking_windows_lock_reports_contention_without_writing(locked_file):
    if locked_file.native:
        # A second native descriptor cannot write even an empty locked range.
        with pytest.raises(PermissionError):
            os.write(locked_file.contender.fileno(), b"0")
        assert locked_file.path.stat().st_size == 0
    with pytest.raises(BlockingIOError):
        locked_file.helper.lock_file(locked_file.contender, blocking=False)
    assert locked_file.path.stat().st_size == len(locked_file.original)
    locked_file.release_owner()
    locked_file.helper.lock_file(locked_file.contender, blocking=False)
    locked_file.helper.unlock_file(locked_file.contender)
    assert locked_file.path.read_bytes() == locked_file.original


def test_blocking_windows_lock_waits_for_release_without_writing(locked_file):
    entered = Event()
    finished = Event()
    acquired = Event()
    errors = []

    def contend():
        entered.set()
        try:
            locked_file.helper.lock_file(locked_file.contender)
            try:
                acquired.set()
            finally:
                locked_file.helper.unlock_file(locked_file.contender)
        except OSError as error:
            errors.append((type(error).__name__, error.errno))
        finally:
            finished.set()

    worker = Thread(target=contend, name="byte-lock-contender")
    worker.start()
    try:
        assert entered.wait(5)
        assert not finished.wait(0.1), errors
        assert not acquired.is_set()
        assert locked_file.path.stat().st_size == len(locked_file.original)
    finally:
        locked_file.release_owner()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert finished.is_set()
    assert not errors
    assert acquired.is_set()
    assert locked_file.path.read_bytes() == locked_file.original
