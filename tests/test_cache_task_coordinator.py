"""Concurrent Cache task admission, queueing, and store-wide exclusion.

Code version: v1.0.0-claude.0
"""

from __future__ import annotations

from pathlib import Path
from threading import Event
import time

import pytest

from app.core import cache_task_coordinator
from app.core.cache_service_support import (
    CooperativeCacheWorker,
    append_shadow_backup_completion,
)
from app.core.chatgpt_downloader import ChatGPTSyncResult
from app.core.chatgpt_service import ChatGPTDownloadService
from app.core.cache_task_coordinator import (
    SHADOW_BACKUP_DEFERRED_MESSAGE,
    SHADOW_BACKUP_SKIPPED_MESSAGE,
    CacheTaskBusyError,
    CacheTaskCoordinator,
    CacheTaskIdentity,
    exclusive_cache_browser,
)
from app.core.config import CrawlConfig
from app.core.job_lock import (
    CACHE_TASK_LOCK_NAME,
    CacheMaintenanceLock,
    CacheTaskLock,
    cache_task_resource_lock_path,
)
from app.core.service import CacheLikesService
from app.core.state import TaskSnapshot, TaskState


X_MEDIA = CacheTaskIdentity("x", "media", "X")
X_TEXT = CacheTaskIdentity("x", "text", "X")
CHATGPT_TEXT = CacheTaskIdentity("chatgpt", "text", "ChatGPT")
CHATGPT_MEDIA = CacheTaskIdentity("chatgpt", "media", "ChatGPT")
ZHIHU_TEXT = CacheTaskIdentity("zhihu", "text", "Zhihu")


class _Recorder:
    """Record what the coordinator asks one task's worker to do."""

    def __init__(self, coordinator: CacheTaskCoordinator, identity: CacheTaskIdentity) -> None:
        self.coordinator = coordinator
        self.identity = identity
        self.events: list[str] = []
        self.fail_launch: Exception | None = None

    def submit(self, *, browser: str = "", resources=(), allow_queue: bool = True) -> bool:
        return self.coordinator.submit(
            self.identity,
            launch=self._launch,
            on_queued=lambda message: self.events.append(f"queued: {message}"),
            on_queue_update=lambda message: self.events.append(f"update: {message}"),
            on_launch_error=lambda error: self.events.append(f"error: {error}"),
            lock_busy_message=f"{self.identity.title} is busy in another window.",
            browser=browser,
            resources=resources,
            allow_queue=allow_queue,
        )

    def _launch(self, queued: bool) -> None:
        if self.fail_launch is not None:
            raise self.fail_launch
        self.events.append("launched after queue" if queued else "launched")

    def finish(self) -> None:
        self.coordinator.finish(self.identity.key)


def _state() -> TaskState:
    return TaskState("test", snapshot_factory=TaskSnapshot)


class _IdleThread:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass


class _FailingThread:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        raise RuntimeError("thread unavailable")


def test_task_identity_names_the_source_and_mode() -> None:
    assert CHATGPT_TEXT.key == "chatgpt:text"
    assert CHATGPT_TEXT.title == "ChatGPT · Text"
    assert CacheTaskIdentity("zhihu", "", "Zhihu").title == "Zhihu"


def test_only_browsers_that_cannot_be_shared_are_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_task_coordinator, "is_windows_host", lambda: False)
    assert exclusive_cache_browser(" Safari ") == "safari"
    assert exclusive_cache_browser("edge") == ""
    assert exclusive_cache_browser("chrome") == ""
    # A running Windows browser locks its cookies, so tasks share one CDP profile there.
    monkeypatch.setattr(cache_task_coordinator, "is_windows_host", lambda: True)
    assert exclusive_cache_browser("edge") == "edge"
    assert exclusive_cache_browser("chrome") == "chrome"
    assert exclusive_cache_browser("") == ""


def test_tasks_on_different_browsers_run_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_task_coordinator, "is_windows_host", lambda: False)
    coordinator = CacheTaskCoordinator(tmp_path)
    x_media = _Recorder(coordinator, X_MEDIA)
    chatgpt_text = _Recorder(coordinator, CHATGPT_TEXT)
    zhihu = _Recorder(coordinator, ZHIHU_TEXT)
    chatgpt_media = _Recorder(coordinator, CHATGPT_MEDIA)

    assert x_media.submit(browser="chrome") is True
    assert chatgpt_text.submit(browser="safari") is True
    # Two macOS Chromium tasks each clone the profile, so they do not wait either.
    assert zhihu.submit(browser="edge") is True
    assert chatgpt_media.submit(browser="edge") is True

    assert {key: state["status"] for key, state in coordinator.task_states().items()} == {
        "x:media": "running",
        "chatgpt:text": "running",
        "zhihu:text": "running",
        "chatgpt:media": "running",
    }
    assert coordinator.running_browser_task("safari") == "ChatGPT · Text"
    assert coordinator.running_browser_task("edge") == ""
    assert coordinator.task_states()["x:media"]["browser"] == "chrome"
    for recorder in (x_media, chatgpt_text, zhihu, chatgpt_media):
        assert recorder.events == ["launched"]
        recorder.finish()
    assert coordinator.task_states() == {}


def test_safari_tasks_queue_in_arrival_order_and_start_as_safari_frees(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    x_media = _Recorder(coordinator, X_MEDIA)
    chatgpt_text = _Recorder(coordinator, CHATGPT_TEXT)
    chatgpt_media = _Recorder(coordinator, CHATGPT_MEDIA)

    assert x_media.submit(browser="safari") is True
    assert chatgpt_text.submit(browser="safari") is False
    assert chatgpt_media.submit(browser="safari") is False

    assert chatgpt_text.events == [
        "queued: Queued. Safari is busy with the X · Media cache. "
        "This task starts automatically when Safari is free."
    ]
    # The third task waits behind the second, which is next in line for Safari.
    assert chatgpt_media.events == [
        "queued: Queued. Safari is busy with the X · Media cache. "
        "This task starts automatically when Safari is free."
    ]
    states = coordinator.task_states()
    assert states["chatgpt:text"] == {
        "status": "queued",
        "browser": "safari",
        "waiting_for": "Safari",
        "blocked_by": "X · Media",
        "message": (
            "Queued. Safari is busy with the X · Media cache. "
            "This task starts automatically when Safari is free."
        ),
    }

    x_media.finish()
    assert chatgpt_text.events[-1] == "launched after queue"
    assert chatgpt_media.events[-1] == (
        "update: Queued. Safari is busy with the ChatGPT · Text cache. "
        "This task starts automatically when Safari is free."
    )
    assert coordinator.task_states()["chatgpt:media"]["blocked_by"] == "ChatGPT · Text"

    chatgpt_text.finish()
    assert chatgpt_media.events[-1] == "launched after queue"
    chatgpt_media.finish()
    assert coordinator.task_states() == {}


def test_a_shared_store_queues_tasks_that_use_different_browsers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cache_task_coordinator, "is_windows_host", lambda: False)
    coordinator = CacheTaskCoordinator(tmp_path)
    x_media = _Recorder(coordinator, X_MEDIA)
    x_text = _Recorder(coordinator, X_TEXT)

    assert x_media.submit(browser="safari", resources=("x-text-history",)) is True
    assert x_text.submit(browser="chrome", resources=("x-text-history",)) is False
    assert x_text.events == [
        "queued: Queued. This task starts automatically after the X · Media cache finishes."
    ]
    assert coordinator.task_states()["x:text"]["waiting_for"] == ""
    x_media.finish()
    assert x_text.events[-1] == "launched after queue"
    x_text.finish()


def test_a_queued_task_can_leave_the_queue_and_unblock_the_task_behind_it(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cache_task_coordinator, "is_windows_host", lambda: False)
    coordinator = CacheTaskCoordinator(tmp_path)
    x_media = _Recorder(coordinator, X_MEDIA)
    chatgpt_text = _Recorder(coordinator, CHATGPT_TEXT)
    x_text = _Recorder(coordinator, X_TEXT)

    x_media.submit(browser="safari")
    # Waits for Safari and, once admitted, will own the shared X text store.
    chatgpt_text.submit(browser="safari", resources=("x-text-history",))
    # Waits only behind the queued task's claim on that store.
    x_text.submit(browser="chrome", resources=("x-text-history",))
    assert coordinator.task_states()["x:text"]["blocked_by"] == "ChatGPT · Text"

    assert coordinator.cancel("chatgpt:text") is True
    assert coordinator.cancel("chatgpt:text") is False
    assert x_text.events[-1] == "launched after queue"
    assert coordinator.cancel("x:media") is False  # Running tasks stop cooperatively instead.
    x_media.finish()
    x_text.finish()


def test_a_task_that_may_not_queue_fails_at_once_with_the_reason(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    x_media = _Recorder(coordinator, X_MEDIA)
    chatgpt_media = _Recorder(coordinator, CHATGPT_MEDIA)
    x_media.submit(browser="safari")

    with pytest.raises(CacheTaskBusyError, match="Safari is busy with the X · Media cache"):
        chatgpt_media.submit(browser="safari", allow_queue=False)

    assert chatgpt_media.events == []
    assert "chatgpt:media" not in coordinator.task_states()
    x_media.finish()
    assert chatgpt_media.submit(browser="safari", allow_queue=False) is True
    chatgpt_media.finish()


def test_another_process_holding_a_resource_rejects_the_start(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cache_task_coordinator, "CACHE_TASK_GATE_WAIT_SECONDS", 0.05)
    first_process = CacheTaskCoordinator(tmp_path)
    second_process = CacheTaskCoordinator(tmp_path)
    first = _Recorder(first_process, X_MEDIA)
    same_task = _Recorder(second_process, X_MEDIA)
    same_browser = _Recorder(second_process, CHATGPT_TEXT)
    other_browser = _Recorder(second_process, ZHIHU_TEXT)

    first.submit(browser="safari")
    with pytest.raises(CacheTaskBusyError, match="X · Media is busy in another window"):
        same_task.submit(browser="chrome")
    with pytest.raises(CacheTaskBusyError, match="ChatGPT · Text is busy in another window"):
        same_browser.submit(browser="safari")
    assert other_browser.submit(browser="edge") is True
    assert second_process.task_states() == {
        "zhihu:text": {
            "status": "running", "browser": "edge", "waiting_for": "", "blocked_by": "", "message": "",
        },
    }

    first.finish()
    assert same_browser.submit(browser="safari") is True
    same_browser.finish()
    other_browser.finish()


def test_a_held_gate_keeps_every_task_out(tmp_path: Path, monkeypatch) -> None:
    """A maintenance step or an older single-lock process owns the gate for its whole run."""
    monkeypatch.setattr(cache_task_coordinator, "CACHE_TASK_GATE_WAIT_SECONDS", 0.05)
    gate = CacheTaskLock(tmp_path / CACHE_TASK_LOCK_NAME)
    coordinator = CacheTaskCoordinator(tmp_path)
    task = _Recorder(coordinator, X_MEDIA)

    assert gate.acquire("shadow-cloud-backup")
    with pytest.raises(CacheTaskBusyError, match="busy in another window"):
        task.submit(browser="chrome")
    assert task.events == []
    gate.release()
    assert task.submit(browser="chrome") is True
    task.finish()


def test_maintenance_waits_for_every_cache_task_in_any_process(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    task = _Recorder(coordinator, CHATGPT_TEXT)
    maintenance = CacheMaintenanceLock(CacheTaskLock(tmp_path / CACHE_TASK_LOCK_NAME))

    task.submit(browser="safari")
    assert maintenance.acquire("chatgpt-history-cleanup") is False
    # The task's own locks may be ignored by the task that runs a post-cache backup.
    own_locks = frozenset({
        cache_task_resource_lock_path(tmp_path, "task:chatgpt:text"),
        cache_task_resource_lock_path(tmp_path, "browser:safari"),
    })
    assert maintenance.acquire("shadow-cloud-backup", ignore=own_locks) is True
    maintenance.release()

    task.finish()
    assert maintenance.acquire("chatgpt-history-cleanup") is True
    maintenance.release()


def test_a_queued_task_reports_its_own_start_failure_and_frees_the_queue(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    x_media = _Recorder(coordinator, X_MEDIA)
    chatgpt_text = _Recorder(coordinator, CHATGPT_TEXT)
    chatgpt_media = _Recorder(coordinator, CHATGPT_MEDIA)
    x_media.submit(browser="safari")
    chatgpt_text.submit(browser="safari")
    chatgpt_media.submit(browser="safari")
    chatgpt_text.fail_launch = RuntimeError("thread unavailable")

    x_media.finish()

    assert chatgpt_text.events[-1] == "error: thread unavailable"
    # The failed task released Safari, so the next task did not stay queued behind it.
    assert chatgpt_media.events[-1] == "launched after queue"
    assert set(coordinator.task_states()) == {"chatgpt:media"}
    chatgpt_media.finish()


def test_an_immediate_start_failure_releases_every_lock(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    task = _Recorder(coordinator, X_MEDIA)
    task.fail_launch = RuntimeError("thread unavailable")

    with pytest.raises(RuntimeError, match="thread unavailable"):
        task.submit(browser="safari")

    assert coordinator.task_states() == {}
    task.fail_launch = None
    assert task.submit(browser="safari") is True
    task.finish()


class _BackupService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.during_backup = None

    def sync_after_cache_task(self, config: CrawlConfig) -> str:
        if self.during_backup is not None:
            self.during_backup()
        self.calls.append(("sync", str(config.shadow_backup_destination)))
        return "Shadow cloud backup copied 2 files."


def _backup_config(tmp_path: Path) -> CrawlConfig:
    return CrawlConfig(
        shadow_backup_enabled=True,
        shadow_backup_auto_sync=True,
        shadow_backup_destination=tmp_path / "cloud",
    )


def test_post_cache_backup_runs_alone_and_queues_tasks_that_arrive_meanwhile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cache_task_coordinator, "is_windows_host", lambda: False)
    coordinator = CacheTaskCoordinator(tmp_path / "store")
    finishing = _Recorder(coordinator, X_MEDIA)
    arriving = _Recorder(coordinator, ZHIHU_TEXT)
    backup = _BackupService()
    finishing.submit(browser="chrome")

    def start_another_task() -> None:
        # The store is being copied, so a new writer waits instead of starting.
        assert arriving.submit(browser="edge") is False
        assert arriving.events == [
            "queued: Queued. This task starts automatically after the shadow cloud backup finishes."
        ]

    backup.during_backup = start_another_task
    message = coordinator.sync_shadow_backup_after_task("x:media", backup, _backup_config(tmp_path))

    assert message == "Shadow cloud backup copied 2 files."
    assert backup.calls == [("sync", str(tmp_path / "cloud"))]
    assert arriving.events[-1] == "launched after queue"
    finishing.finish()
    arriving.finish()


def test_post_cache_backup_is_deferred_until_the_last_task_finishes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cache_task_coordinator, "is_windows_host", lambda: False)
    coordinator = CacheTaskCoordinator(tmp_path / "store")
    first = _Recorder(coordinator, X_MEDIA)
    second = _Recorder(coordinator, CHATGPT_TEXT)
    backup = _BackupService()
    first.submit(browser="chrome")
    second.submit(browser="safari")

    state = _state()
    completion = append_shadow_backup_completion(
        "Finished.",
        shadow_backup_service=backup,
        state=state,
        config=_backup_config(tmp_path),
        coordinator=coordinator,
        task=X_MEDIA,
    )

    # Another task is still writing the store, so nothing is copied yet.
    assert completion == f"Finished. {SHADOW_BACKUP_DEFERRED_MESSAGE}"
    assert backup.calls == []
    first.finish()
    assert backup.calls == []
    second.finish()
    assert backup.calls == [("sync", str(tmp_path / "cloud"))]
    # The gate is free again once the deferred backup has ended.
    assert first.submit(browser="chrome") is True
    first.finish()


def test_post_cache_backup_is_skipped_while_another_process_writes_the_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cache_task_coordinator, "is_windows_host", lambda: False)
    store = tmp_path / "store"
    coordinator = CacheTaskCoordinator(store)
    other_process = CacheTaskCoordinator(store)
    finishing = _Recorder(coordinator, X_MEDIA)
    foreign = _Recorder(other_process, CHATGPT_TEXT)
    backup = _BackupService()
    finishing.submit(browser="chrome")
    foreign.submit(browser="safari")

    message = coordinator.sync_shadow_backup_after_task("x:media", backup, _backup_config(tmp_path))

    assert message == SHADOW_BACKUP_SKIPPED_MESSAGE
    assert backup.calls == []
    finishing.finish()
    foreign.finish()


def test_backup_completion_without_auto_sync_never_defers(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    first = _Recorder(coordinator, X_MEDIA)
    second = _Recorder(coordinator, CHATGPT_TEXT)
    first.submit(browser="chrome")
    second.submit(browser="safari")

    class DisabledBackup:
        def sync_after_cache_task(self, _config: CrawlConfig) -> None:
            return None

    assert append_shadow_backup_completion(
        "Finished.",
        shadow_backup_service=DisabledBackup(),
        state=_state(),
        config=CrawlConfig(shadow_backup_auto_sync=False),
        coordinator=coordinator,
        task=X_MEDIA,
    ) == "Finished."
    first.finish()
    second.finish()


def test_worker_waits_in_the_queue_then_starts_when_the_browser_is_free(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    running_state, queued_state = _state(), _state()
    running = CooperativeCacheWorker(running_state, task=X_MEDIA, coordinator=coordinator)
    queued = CooperativeCacheWorker(queued_state, task=CHATGPT_TEXT, coordinator=coordinator)
    prepared: list[str] = []

    def start(worker: CooperativeCacheWorker, **options) -> None:
        worker._start_worker(
            lock_owner="fixture",
            already_running_message="already running",
            lock_busy_message="busy",
            target=lambda: None,
            thread_factory=_IdleThread,
            browser="safari",
            **options,
        )

    start(running)
    start(queued, prepare=lambda: prepared.append("ready"))

    snapshot = queued_state.snapshot()
    assert prepared == ["ready"]
    assert queued.is_running() is True and queued.is_queued() is True
    assert snapshot["phase"] == "queued"
    assert snapshot["message"] == (
        "Queued. Safari is busy with the X · Media cache. "
        "This task starts automatically when Safari is free."
    )
    queued_at = snapshot["started_at"]
    with pytest.raises(RuntimeError, match="ChatGPT · Text cache is already queued"):
        start(queued)
    with pytest.raises(RuntimeError, match="already running"):
        start(running)

    running_state.finish_success("Done.")
    running._release_task_lock()

    snapshot = queued_state.snapshot()
    assert prepared == ["ready"]
    assert snapshot["running"] is True
    assert snapshot["phase"] == "starting"
    assert snapshot["message"] == "Initializing job."
    assert snapshot["started_at"] >= queued_at
    assert queued.is_queued() is False
    queued_state.finish_success("Done.")
    queued._release_task_lock()
    assert coordinator.task_states() == {}


def test_stopping_a_queued_worker_removes_it_without_touching_the_running_task(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    running_state, queued_state = _state(), _state()
    running = CooperativeCacheWorker(running_state, task=X_MEDIA, coordinator=coordinator)
    queued = CooperativeCacheWorker(queued_state, task=CHATGPT_TEXT, coordinator=coordinator)
    for worker in (running, queued):
        worker._start_worker(
            lock_owner="fixture",
            already_running_message="already running",
            lock_busy_message="busy",
            target=lambda: None,
            thread_factory=_IdleThread,
            browser="safari",
        )

    assert queued._request_stop("Stopping after the current item.") is True

    snapshot = queued_state.snapshot()
    assert snapshot["running"] is False
    assert snapshot["phase"] == "stopped"
    assert snapshot["message"] == "The ChatGPT · Text cache left the queue before it started."
    assert queued._is_stop_requested() is False
    assert running_state.snapshot()["phase"] == "starting"
    assert set(coordinator.task_states()) == {"x:media"}
    # A running task still stops cooperatively.
    assert running._request_stop("Stopping after the current item.") is True
    assert running_state.snapshot()["phase"] == "stopping"
    assert running._is_stop_requested() is True
    running._release_task_lock()


def test_worker_that_may_not_queue_leaves_its_state_untouched(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    running = CooperativeCacheWorker(_state(), task=X_MEDIA, coordinator=coordinator)
    refused_state = _state()
    refused = CooperativeCacheWorker(refused_state, task=CHATGPT_MEDIA, coordinator=coordinator)
    running._start_worker(
        lock_owner="fixture",
        already_running_message="already running",
        lock_busy_message="busy",
        target=lambda: None,
        thread_factory=_IdleThread,
        browser="safari",
    )

    with pytest.raises(CacheTaskBusyError, match="Safari is busy with the X · Media cache"):
        refused._start_worker(
            lock_owner="fixture",
            already_running_message="already running",
            lock_busy_message="busy",
            target=lambda: None,
            prepare=lambda: pytest.fail("A refused task must not be prepared."),
            thread_factory=_IdleThread,
            browser="safari",
            allow_queue=False,
        )

    assert refused_state.snapshot() == _state().snapshot()
    running._release_task_lock()


def test_coordinated_worker_start_failure_publishes_a_terminal_state_and_frees_the_task(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    state = _state()
    worker = CooperativeCacheWorker(state, task=X_MEDIA, coordinator=coordinator)

    with pytest.raises(RuntimeError, match="thread unavailable"):
        worker._start_worker(
            lock_owner="fixture",
            already_running_message="already running",
            lock_busy_message="busy",
            target=lambda: None,
            thread_factory=_FailingThread,
            browser="safari",
        )

    snapshot = state.snapshot()
    assert snapshot["running"] is False
    assert snapshot["phase"] == "failed"
    assert snapshot["last_error"] == "thread unavailable"
    assert coordinator.task_states() == {}


def test_queued_worker_start_failure_publishes_a_terminal_state(tmp_path: Path) -> None:
    coordinator = CacheTaskCoordinator(tmp_path)
    running = CooperativeCacheWorker(_state(), task=X_MEDIA, coordinator=coordinator)
    queued_state = _state()
    queued = CooperativeCacheWorker(queued_state, task=CHATGPT_TEXT, coordinator=coordinator)
    running._start_worker(
        lock_owner="fixture",
        already_running_message="already running",
        lock_busy_message="busy",
        target=lambda: None,
        thread_factory=_IdleThread,
        browser="safari",
    )
    queued._start_worker(
        lock_owner="fixture",
        already_running_message="already running",
        lock_busy_message="busy",
        target=lambda: None,
        thread_factory=_FailingThread,
        browser="safari",
    )

    running._release_task_lock()

    snapshot = queued_state.snapshot()
    assert snapshot["running"] is False
    assert snapshot["phase"] == "failed"
    assert snapshot["last_error"] == "thread unavailable"
    assert coordinator.task_states() == {}


def _wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "Timed out waiting for the cache workers."
        time.sleep(0.01)


def test_x_media_and_chatgpt_text_overlap_in_different_browsers_and_take_turns_in_safari(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real workers run on their own threads: together when they can, in turn when not."""
    monkeypatch.setattr(cache_task_coordinator, "is_windows_host", lambda: False)
    monkeypatch.setattr("app.core.service.LOCAL_STORE_ROOT", tmp_path / "store")
    coordinator = CacheTaskCoordinator(tmp_path / "locks")
    x_state, chatgpt_state = _state(), _state()
    x_service = CacheLikesService(x_state, task=X_MEDIA, coordinator=coordinator)
    chatgpt_service = ChatGPTDownloadService(chatgpt_state, task=CHATGPT_TEXT, coordinator=coordinator)
    x_collecting, release_x = Event(), Event()
    chatgpt_syncing, release_chatgpt = Event(), Event()
    x_running_when_chatgpt_began: list[bool] = []

    def collect_likes(_config, _state, **_options):
        x_collecting.set()
        assert release_x.wait(5)
        return "fixture", []

    def sync_chatgpt(_state, *, config, should_stop, content_mode):
        assert content_mode == "text" and config.chatgpt_browser == "safari"
        x_running_when_chatgpt_began.append(x_state.snapshot()["running"])
        chatgpt_syncing.set()
        assert release_chatgpt.wait(5)
        return ChatGPTSyncResult(discovered_conversations=1, cached_messages=2)

    monkeypatch.setattr("app.core.service.collect_liked_tweet_urls", collect_likes)
    monkeypatch.setattr("app.core.chatgpt_service.sync_chatgpt_images", sync_chatgpt)

    def both_idle() -> bool:
        return (
            not x_state.snapshot()["running"]
            and not chatgpt_state.snapshot()["running"]
            and coordinator.task_states() == {}
        )

    # X media in Chrome and ChatGPT text in Safari need nothing from each other.
    x_service.start(CrawlConfig(x_browser="chrome"), content_mode="media")
    chatgpt_service.start(CrawlConfig(chatgpt_browser="safari"), content_mode="text")
    assert x_collecting.wait(5) and chatgpt_syncing.wait(5)
    assert x_running_when_chatgpt_began == [True]
    assert chatgpt_state.snapshot()["phase"] != "queued"
    release_x.set()
    release_chatgpt.set()
    _wait_until(both_idle)
    assert x_state.snapshot()["phase"] == "finished"
    assert chatgpt_state.snapshot()["phase"] == "finished"

    # Both in Safari: the second waits, then starts by itself when the first ends.
    for event in (x_collecting, release_x, chatgpt_syncing, release_chatgpt):
        event.clear()
    x_service.start(CrawlConfig(x_browser="safari"), content_mode="media")
    assert x_collecting.wait(5)
    chatgpt_service.start(CrawlConfig(chatgpt_browser="safari"), content_mode="text")
    assert chatgpt_state.snapshot()["phase"] == "queued"
    assert not chatgpt_syncing.wait(0.2)
    release_x.set()
    assert chatgpt_syncing.wait(5)
    assert x_running_when_chatgpt_began == [True, False]
    assert x_state.snapshot()["phase"] == "finished"
    release_chatgpt.set()
    _wait_until(both_idle)
    assert chatgpt_state.snapshot()["message"].startswith("Finished ChatGPT text sync.")
