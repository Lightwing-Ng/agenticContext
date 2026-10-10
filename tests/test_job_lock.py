"""Regression tests for cross-window cache task exclusion.

Code version: v1.1.0-claude.0
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.config import CrawlConfig
from app.core.grok_service import GrokDownloadService
from app.core.job_lock import (
    CacheMaintenanceLock,
    CacheTaskLock,
    cache_task_resource_lock_path,
    held_cache_task_resource_locks,
    is_cache_task_lock_name,
)
from app.core.service import CacheLikesService
from app.core.state import TaskState


def test_cache_task_lock_excludes_another_owner_until_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "cache-task.lock"
    first_owner = CacheTaskLock(lock_path)
    second_owner = CacheTaskLock(lock_path)

    assert first_owner.acquire("x-cache")
    assert not second_owner.acquire("grok-sync")

    first_owner.release()

    assert second_owner.acquire("grok-sync")
    second_owner.release()


class _IdleThread:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass


def test_workers_given_one_explicit_lock_still_exclude_each_other(tmp_path: Path) -> None:
    """Without a coordinator, a worker keeps its task lock for the whole run."""
    task_lock = CacheTaskLock(tmp_path / "cache-task.lock")
    x_service = CacheLikesService(TaskState("test"), task_lock=task_lock)
    grok_service = GrokDownloadService(TaskState("test"), task_lock=task_lock)

    with patch("app.core.service.Thread", _IdleThread):
        x_service.start(CrawlConfig())

    try:
        with pytest.raises(RuntimeError, match="already running"):
            grok_service.start(CrawlConfig())
    finally:
        x_service._owns_task_lock = False
        task_lock.release()


def test_resource_locks_are_named_store_root_files(tmp_path: Path) -> None:
    assert cache_task_resource_lock_path(tmp_path, "task:chatgpt:text") == (
        tmp_path / ".cache_task.task-chatgpt-text.lock"
    )
    assert cache_task_resource_lock_path(tmp_path, "browser:safari").name == ".cache_task.browser-safari.lock"
    assert cache_task_resource_lock_path(tmp_path, " Store:X Text/History ").name == (
        ".cache_task.store-x-text-history.lock"
    )
    with pytest.raises(ValueError, match="requires a name"):
        cache_task_resource_lock_path(tmp_path, " :: ")
    assert is_cache_task_lock_name(".cache_task.lock")
    assert is_cache_task_lock_name(".cache_task.browser-safari.lock")
    assert not is_cache_task_lock_name(".cache_catalog.parquet")
    assert not is_cache_task_lock_name("cache_task.lock")


def test_held_resource_locks_are_found_without_claiming_them(tmp_path: Path) -> None:
    assert held_cache_task_resource_locks(tmp_path / "missing") == ()
    task = CacheTaskLock(cache_task_resource_lock_path(tmp_path, "task:x:media"))
    browser = CacheTaskLock(cache_task_resource_lock_path(tmp_path, "browser:safari"))
    gate = CacheTaskLock(tmp_path / ".cache_task.lock")
    assert task.acquire("x:media") and browser.acquire("x:media") and gate.acquire("gate")
    owner_record = task.path.read_text(encoding="utf-8")

    # The gate is not a task resource, and a probe leaves the owner record untouched.
    assert held_cache_task_resource_locks(tmp_path) == (browser.path, task.path)
    assert held_cache_task_resource_locks(tmp_path, ignore=frozenset({browser.path})) == (task.path,)
    assert task.path.read_text(encoding="utf-8") == owner_record

    browser.release()
    task.release()
    gate.release()
    assert held_cache_task_resource_locks(tmp_path) == ()
    # A released lock file stays on disk and can be taken again.
    assert task.acquire("x:media")
    task.release()


def test_maintenance_lock_holds_the_gate_only_when_no_task_is_active(tmp_path: Path) -> None:
    gate = CacheTaskLock(tmp_path / ".cache_task.lock")
    maintenance = CacheMaintenanceLock(gate)
    other_gate_owner = CacheTaskLock(tmp_path / ".cache_task.lock")
    task = CacheTaskLock(cache_task_resource_lock_path(tmp_path, "task:grok:media"))

    assert task.acquire("grok:media")
    assert maintenance.acquire("shadow-cloud-backup") is False
    # A refused maintenance step leaves the gate free for the next starting task.
    assert other_gate_owner.acquire("chatgpt:text")
    other_gate_owner.release()
    task.release()

    assert maintenance.acquire("shadow-cloud-backup") is True
    assert other_gate_owner.acquire("chatgpt:text") is False
    maintenance.release()
    assert other_gate_owner.acquire("chatgpt:text")
    other_gate_owner.release()
