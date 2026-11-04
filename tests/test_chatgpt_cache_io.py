"""Focused tests for bounded, read-only ChatGPT image cache access."""

# Code version: v1.0.1-codex.0

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
from io import BytesIO
from pathlib import Path
from threading import BoundedSemaphore, Event, Thread

import pytest

from app.core import chatgpt_cache_io


@pytest.mark.parametrize(
    ("size", "expected"),
    [(-1, b"cached-image-payload"), (6, b"cached"), (0, b"")],
)
def test_read_cached_image_returns_requested_bytes(
    tmp_path: Path, size: int, expected: bytes,
) -> None:
    cache_path = tmp_path / "image.png"
    cache_path.write_bytes(b"cached-image-payload")

    assert chatgpt_cache_io.read_cached_image(cache_path, size) == expected


@pytest.mark.parametrize("failure_stage", ["open", "read"])
@pytest.mark.parametrize("error_kind", ["plain", "permission", "io", "timeout", "os_timeout"])
def test_read_cached_image_classifies_oserror_and_releases_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str, error_kind: str,
) -> None:
    cache_path = tmp_path / "image.png"
    cache_path.write_bytes(b"cached-image-payload")
    error = OSError("The cache volume is unavailable")
    if error_kind == "timeout":
        error = TimeoutError("The cache read timed out")
    elif error_kind != "plain":
        error.errno = {
            "permission": errno.EACCES,
            "io": errno.EIO,
            "os_timeout": errno.ETIMEDOUT,
        }[error_kind]
    expected_error = (
        chatgpt_cache_io.ChatGPTCacheReadTimeoutError
        if error_kind in {"timeout", "os_timeout"}
        else OSError
    )
    original_open = Path.open
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(chatgpt_cache_io, "_READ_SLOTS", slots)

    class FailingRead(BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise error

    def failing_open(path: Path, *args, **kwargs):
        if path != cache_path:
            return original_open(path, *args, **kwargs)
        if failure_stage == "open":
            raise error
        return FailingRead()

    with monkeypatch.context() as patched_open:
        patched_open.setattr(Path, "open", failing_open)
        with pytest.raises(expected_error) as captured:
            chatgpt_cache_io.read_cached_image(cache_path)

    if error_kind in {"timeout", "os_timeout"}:
        assert captured.value.__cause__ is error
        assert "Existing files were preserved" in str(captured.value)
    else:
        assert captured.value is error
    assert chatgpt_cache_io.read_cached_image(cache_path) == b"cached-image-payload"
    assert slots.acquire(timeout=1.0)
    slots.release()


def test_stalled_read_times_out_without_mutation_or_unbounded_threads_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "image.png"
    original_payload = b"existing-cache-image-must-survive"
    cache_path.write_bytes(original_payload)
    original_open = Path.open
    read_started = Event()
    release_read = Event()
    workers: list[Thread] = []
    monkeypatch.setattr(chatgpt_cache_io, "CACHE_IMAGE_READ_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(chatgpt_cache_io, "_READ_SLOTS", BoundedSemaphore(1))

    class StalledRead:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size: int = -1) -> bytes:
            read_started.set()
            release_read.wait()
            return self.handle.read(size)

    def stalled_open(path: Path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path != cache_path:
            return handle
        assert args == ("rb",)
        return StalledRead(handle)

    def tracked_thread(*args, **kwargs) -> Thread:
        worker = Thread(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(Path, "open", stalled_open)
    monkeypatch.setattr(chatgpt_cache_io, "Thread", tracked_thread)

    with ThreadPoolExecutor(max_workers=1) as caller:
        try:
            first_read = caller.submit(chatgpt_cache_io.read_cached_image, cache_path)
            assert read_started.wait(timeout=1.0)
            with pytest.raises(chatgpt_cache_io.ChatGPTCacheReadTimeoutError):
                first_read.result(timeout=1.0)

            blocked_retry = caller.submit(chatgpt_cache_io.read_cached_image, cache_path)
            with pytest.raises(chatgpt_cache_io.ChatGPTCacheReadTimeoutError):
                blocked_retry.result(timeout=1.0)

            assert len(workers) == 1
            assert workers[0].is_alive()
            assert workers[0].daemon
            with original_open(cache_path, "rb") as handle:
                assert handle.read() == original_payload
        finally:
            release_read.set()
            for worker in workers:
                worker.join(timeout=1.0)

        assert all(not worker.is_alive() for worker in workers)
        recovered_read = caller.submit(chatgpt_cache_io.read_cached_image, cache_path)
        assert recovered_read.result(timeout=1.0) == original_payload
        assert len(workers) == 2
        workers[-1].join(timeout=1.0)
        assert not workers[-1].is_alive()
        with original_open(cache_path, "rb") as handle:
            assert handle.read() == original_payload
