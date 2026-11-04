"""Isolated Cache lease acquisition and cancellation. Code version: v1.0.0-codex.0."""

from importlib import import_module
from pathlib import Path
from threading import Event, Thread
from unittest.mock import MagicMock, patch

import pytest

from app.core import safari_automation as safari
from app.core.cache_task_coordinator import CacheTaskCoordinator, CacheTaskIdentity
from app.core.config import CrawlConfig
from app.core.copilot_sync_state import read_copilot_sync_state
from app.core.service import CacheLikesService
from app.core.state import TaskSnapshot, TaskState


@pytest.fixture
def isolated_safari(monkeypatch, tmp_path):
    monkeypatch.setattr(safari, "SAFARI_CONTEXT_LOCK_PATH", tmp_path / "safari.lock")
    monkeypatch.setattr(safari, "retry_pending_safari_context_cleanup", lambda: (0, 0))
    monkeypatch.setattr(safari.SafariContext, "_reconcile_context_lease_state", lambda self: None)


def test_cache_waits_at_acquisition_without_replaying_sync_body(isolated_safari):
    waits = []
    messages = []
    body_calls = []
    context = safari.SafariContext("https://example.test/", lock_blocking=False)
    with patch.object(safari, "lock_file", side_effect=[BlockingIOError(), BlockingIOError(), None]):
        with safari.safari_cache_acquisition_scope(lambda: False, waits.append, messages.append):
            body_calls.append("persisted checkpoint")
            context._acquire_context_lock()
            context._release_context_lock()
    assert body_calls == ["persisted checkpoint"]
    assert waits == [0.25, 0.25]
    assert len(messages) == 1


def test_one_deadline_is_shared_by_sequential_contexts(monkeypatch, isolated_safari):
    elapsed = 0.0
    calls = 0
    waits = []

    def clock():
        return elapsed

    def wait(seconds):
        nonlocal elapsed
        elapsed += seconds
        waits.append(seconds)

    def lock(_handle, **_kwargs):
        nonlocal calls
        calls += 1
        if calls != 4:
            raise BlockingIOError()

    monkeypatch.setattr(safari.time, "monotonic", clock)
    first = safari.SafariContext("https://example.test/", lock_blocking=False)
    second = safari.SafariContext("https://example.test/", lock_blocking=False)
    with patch.object(safari, "lock_file", side_effect=lock):
        with safari.safari_cache_acquisition_scope(lambda: False, wait, lambda _message: None):
            first._acquire_context_lock()
            first._release_context_lock()
            elapsed += 59.0
            with pytest.raises(safari.SafariBusyError, match="bounded acquisition"):
                second._acquire_context_lock()
    assert elapsed == 60.0
    assert waits == [0.25] * 4
    assert second._context_lock_handle is None


def test_stop_before_enter_cleanup_closes_the_unacquired_handle(isolated_safari):
    stopped = Event()
    handle = MagicMock()
    context = safari.SafariContext("https://example.test/", lock_blocking=False)

    def stop(_seconds):
        stopped.set()

    with patch.object(Path, "open", return_value=handle), patch.object(
        safari, "lock_file", side_effect=BlockingIOError()
    ):
        with safari.safari_cache_acquisition_scope(stopped.is_set, stop, lambda _message: None):
            with pytest.raises(safari.SafariCacheCancelled):
                context.__enter__()
    handle.close.assert_called_once()
    assert context._context_lock_handle is None
    assert not context.pages


def test_stop_after_successful_lock_closes_handle_before_any_window(isolated_safari):
    stopped = Event()
    handle = MagicMock()
    context = safari.SafariContext("https://example.test/", lock_blocking=False)

    def acquire_then_stop(*_args, **_kwargs):
        stopped.set()

    with patch.object(Path, "open", return_value=handle), patch.object(
        safari, "lock_file", side_effect=acquire_then_stop
    ), patch.object(context, "_create_page") as create:
        with safari.safari_cache_acquisition_scope(stopped.is_set, lambda _seconds: None, lambda _message: None):
            with pytest.raises(safari.SafariCacheCancelled):
                context.__enter__()
    handle.close.assert_called_once()
    create.assert_not_called()
    assert context._context_lock_handle is None


def test_stop_during_reconciliation_releases_owned_handle_before_window(isolated_safari):
    stopped = Event()
    context = safari.SafariContext("https://example.test/", lock_blocking=False)
    acquired = []

    def stop_during_reconciliation():
        acquired.append(context._context_lock_handle)
        stopped.set()

    with patch.object(context, "_reconcile_context_lease_state", side_effect=stop_during_reconciliation), patch.object(
        context, "_create_page"
    ) as create:
        with safari.safari_cache_acquisition_scope(stopped.is_set, stopped.wait, lambda _message: None):
            with pytest.raises(safari.SafariCacheCancelled):
                context.__enter__()
    assert len(acquired) == 1 and acquired[0].closed
    assert context._context_lock_handle is None
    create.assert_not_called()


def test_probe_stays_fail_fast_and_real_errors_never_retry(isolated_safari):
    context = safari.SafariContext("https://example.test/", lock_blocking=False)
    with patch.object(safari, "lock_file", side_effect=BlockingIOError()) as lock:
        with pytest.raises(safari.SafariBusyError):
            context._acquire_context_lock()
    assert lock.call_count == 1
    waits = []
    with patch.object(safari, "lock_file"), patch.object(
        context, "_reconcile_context_lease_state", side_effect=RuntimeError("Ownership is unverified")
    ):
        with safari.safari_cache_acquisition_scope(lambda: False, waits.append, lambda _message: None):
            with pytest.raises(RuntimeError, match="Ownership is unverified"):
                context._acquire_context_lock()
    assert waits == []
    assert context._context_lock_handle is None


def test_child_workers_inherit_scope_and_cannot_acquire_after_job_ends(isolated_safari):
    calls = []

    def child():
        calls.append(safari.has_safari_cache_acquisition_scope())
        context = safari.SafariContext("https://example.test/", lock_blocking=False)
        context._acquire_context_lock()
        context._release_context_lock()

    with patch.object(safari, "lock_file", side_effect=[BlockingIOError(), None]) as lock:
        with safari.safari_cache_acquisition_scope(lambda: False, lambda _seconds: None, lambda _message: None):
            active = Thread(target=safari.inherit_safari_cache_context(child))
            active.start()
            active.join(timeout=2)
            assert not active.is_alive()
            late_target = safari.inherit_safari_cache_context(child)
        late = Thread(target=late_target)
        late.start()
        late.join(timeout=2)
    assert calls == [True, True]
    assert lock.call_count == 2


def test_managed_x_scope_never_adds_legacy_collection_retries(isolated_safari):
    service = CacheLikesService(TaskState("test", snapshot_factory=TaskSnapshot))
    error = safari.SafariBusyError("Acquisition budget exhausted")
    with patch("app.core.service.collect_liked_tweet_urls", side_effect=error) as collect, patch.object(
        service._stop_requested, "wait"
    ) as wait:
        with safari.safari_cache_acquisition_scope(lambda: False, lambda _seconds: None, lambda _message: None):
            with pytest.raises(safari.SafariBusyError) as caught:
                service._collect_likes(CrawlConfig(x_browser="safari"), on_text_posts=None)
    assert caught.value is error
    collect.assert_called_once()
    wait.assert_not_called()


def test_copilot_cancellation_finalizes_even_if_coverage_write_fails(tmp_path, isolated_safari):
    from app.core.copilot_history_service import CopilotHistoryService

    state = TaskState("test", snapshot_factory=TaskSnapshot)
    state.reset_for_run()
    state.update(phase="stopping")
    service = CopilotHistoryService(state, local_store_root=tmp_path)
    with patch.object(service, "_persist_outcome", side_effect=OSError("Disk write failed")):
        service._finish_cancelled_browser_wait()
    snapshot = state.snapshot()
    assert snapshot["running"] is False
    assert snapshot["phase"] == "stopped"
    assert snapshot["last_error"] == "Microsoft Copilot stopped, but its coverage state could not be saved."


class ImmediateThread:
    def __init__(self, *, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


PROVIDERS = [
    ("service", "CacheLikesService", "collect_liked_tweet_urls", "x", "text"),
    ("service", "CacheLikesService", "collect_liked_tweet_urls", "x", "media"),
    ("grok_history_service", "GrokHistoryService", "sync_grok_history", "grok", "text"),
    ("grok_service", "GrokDownloadService", "sync_grok_media", "grok", "media"),
    ("chatgpt_service", "ChatGPTDownloadService", "sync_chatgpt_images", "chatgpt", "text"),
    ("chatgpt_service", "ChatGPTDownloadService", "sync_chatgpt_images", "chatgpt", "media"),
    ("gemini_service", "GeminiHistoryService", "sync_gemini_history", "gemini", "text"),
    ("gemini_service", "GeminiHistoryService", "sync_gemini_media", "gemini", "media"),
    ("claude_history_service", "ClaudeHistoryService", "sync_claude_history", "claude", "text"),
    ("claude_history_service", "ClaudeHistoryService", "sync_claude_media", "claude", "media"),
    ("copilot_history_service", "CopilotHistoryService", "sync_copilot_history", "copilot", "text"),
]


@pytest.mark.parametrize("module_name,class_name,sync_name,source,mode", PROVIDERS)
def test_every_safari_provider_cancels_before_browser_without_failed_state(
    module_name, class_name, sync_name, source, mode, monkeypatch, tmp_path, isolated_safari,
):
    module = import_module("app.core." + module_name)
    state = TaskState("test", snapshot_factory=TaskSnapshot)
    coordinator = CacheTaskCoordinator(tmp_path / "locks")
    init = {"task": CacheTaskIdentity(source, mode, source), "coordinator": coordinator}
    if source in {"gemini", "claude", "copilot"} or module_name == "grok_history_service":
        init["local_store_root"] = tmp_path
    if source == "x":
        monkeypatch.setattr(module, "LOCAL_STORE_ROOT", tmp_path)
    service = getattr(module, class_name)(state, **init)
    config = CrawlConfig(**{source + "_browser": "safari"})
    monkeypatch.setattr(module, "Thread", ImmediateThread)

    def wait(_seconds):
        assert service.request_stop()
        return True

    def sync(*_args, **_kwargs):
        with safari.SafariContext("https://example.test/", lock_blocking=False):
            pytest.fail("A stopped worker must not open a browser")

    monkeypatch.setattr(module, sync_name, sync)
    monkeypatch.setattr(service._stop_requested, "wait", wait)
    monkeypatch.setattr(safari, "lock_file", MagicMock(side_effect=BlockingIOError()))
    with patch.object(safari.SafariContext, "_create_page") as create:
        if source in {"x", "chatgpt", "gemini", "claude", "copilot"}:
            service.start(config, content_mode=mode)
        else:
            service.start(config)
    assert state.snapshot()["phase"] == "stopped"
    assert state.snapshot()["task_failures"] == 0
    assert state.snapshot()["last_error"] == ""
    assert coordinator.task_states() == {}
    create.assert_not_called()
    if source == "copilot":
        assert read_copilot_sync_state(tmp_path)["status"] == "stopped"
