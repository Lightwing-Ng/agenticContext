"""Global publication leases remain process-scoped and preserve the lock inode.

Code version: v1.0.0-codex.0
"""

from contextlib import contextmanager
import json
import multiprocessing

import pytest

from app.core import wechat_global_guard as guard
from app.core.wechat_archive import wechat_archive_path


def _hold_guard(root, connection):
    try:
        with guard.global_refresh_guard(root):
            connection.send("locked")
            connection.recv()
    finally:
        connection.close()


@contextmanager
def _foreign_guard(root):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_guard, args=(str(root), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "The isolated global lease did not start."
        assert parent.recv() == "locked"
        yield
    finally:
        if process.is_alive():
            parent.send("release")
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        assert process.exitcode == 0


def test_absent_guard_probe_does_not_create_runtime_files(tmp_path):
    root = tmp_path / "absent-store"
    assert guard.global_refresh_busy(root) is False
    assert not root.exists()


def test_global_merge_guard_is_cross_process_and_persists_one_lock_inode(tmp_path):
    root = tmp_path / "store"
    path = wechat_archive_path(root).with_name(guard.LOCK_FILENAME)
    with _foreign_guard(root):
        assert guard.global_refresh_busy(root) is True
        assert guard.global_refresh_busy(root) is True
        inode = path.stat().st_ino
        assert json.loads(path.read_text()) == {
            "schema_version": 1,
            "purpose": "global_chat_repair_final_merge",
        }
        assert str(root) not in path.read_text()
    assert path.stat().st_ino == inode
    assert guard.global_refresh_busy(root) is False


def test_stale_ordinary_lock_file_does_not_report_a_running_repair(tmp_path):
    path = wechat_archive_path(tmp_path).with_name(guard.LOCK_FILENAME)
    path.parent.mkdir(parents=True)
    path.write_text("stale metadata from an interrupted process\n")
    inode = path.stat().st_ino
    assert guard.global_refresh_busy(tmp_path) is False
    with guard.global_refresh_guard(tmp_path):
        assert guard.global_refresh_busy(tmp_path) is True
    assert guard.global_refresh_busy(tmp_path) is False
    assert path.stat().st_ino == inode


def test_global_merge_guard_releases_after_an_exception(tmp_path):
    with pytest.raises(RuntimeError, match="synthetic merge failure"):
        with guard.global_refresh_guard(tmp_path):
            assert guard.global_refresh_busy(tmp_path) is True
            raise RuntimeError("synthetic merge failure")
    assert guard.global_refresh_busy(tmp_path) is False
    with guard.global_refresh_guard(tmp_path):
        assert guard.global_refresh_busy(tmp_path) is True


@pytest.mark.parametrize("kind", ("symlink", "hardlink"))
def test_guard_rejects_aliased_files_without_overwriting_them(tmp_path, kind):
    path = wechat_archive_path(tmp_path).with_name(guard.LOCK_FILENAME)
    path.parent.mkdir(parents=True)
    original = tmp_path / "unrelated.txt"
    original.write_text("preserve unrelated bytes")
    if kind == "symlink":
        path.symlink_to(original)
    else:
        path.hardlink_to(original)
    with pytest.raises(OSError):
        guard.global_refresh_busy(tmp_path)
    with pytest.raises(OSError):
        with guard.global_refresh_guard(tmp_path):
            pytest.fail("An aliased lock must not be acquired.")
    assert original.read_text() == "preserve unrelated bytes"
