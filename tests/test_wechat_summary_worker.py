"""Exercise the prewarm worker's stable-input and lifetime contracts."""

# Code version: v1.0.0-codex.0

from threading import Event
from types import SimpleNamespace

from app.core.storage import wechat_summary_worker as summaries


def _script(worker, monkeypatch, observations, prepare):
    steps = iter(observations)
    current = None

    def wait(_delay):
        nonlocal current
        current = next(steps, None)
        return current is not None, bool(current and current[2])

    monkeypatch.setattr(worker, "_wait", wait)
    monkeypatch.setattr(summaries, "_file_identity", lambda _path: current[1])
    monkeypatch.setattr(summaries, "_pin", lambda _repository, identity: {"input": identity})
    monkeypatch.setattr(summaries, "time", SimpleNamespace(monotonic=lambda: current[0]))
    monkeypatch.setattr(worker, "_prepare", lambda identity, _pin: prepare(current[0], identity))
    worker._run()


def test_coalesces_unchanged_input_and_rate_limits_repairs(tmp_path, monkeypatch):
    worker = summaries.WechatSummaryWorker(tmp_path)
    prepared = []

    def prepare(now, source):
        prepared.append((now, source))
        return True

    _script(worker, monkeypatch, [
        (0, "a", False), (4, "a", False), (5, "a", False),
        (6, "a", True), (7, "a", True), (34, "a", False), (35, "a", False),
        (36, "b", False), (39, "b", False), (41, "b", False), (75, "b", False),
    ], prepare)

    assert prepared == [(5, "a"), (35, "a"), (41, "b")]


def test_failure_backoff_does_not_delay_a_new_publication(tmp_path, monkeypatch):
    worker = summaries.WechatSummaryWorker(tmp_path)
    attempted = []

    def prepare(now, source):
        attempted.append((now, source))
        if source == "a":
            raise ValueError("Synthetic publication failure")
        return True

    _script(worker, monkeypatch, [
        (0, "a", False), (5, "a", False), (10, "a", False), (34, "a", False),
        (35, "a", False), (36, "b", False), (41, "b", False),
    ], prepare)

    assert attempted == [(5, "a"), (35, "a"), (41, "b")]


def test_wake_stays_dormant_and_shutdown_releases_waiters(tmp_path, monkeypatch):
    worker = summaries.WechatSummaryWorker(tmp_path, interval_seconds=0.01)
    entered, release = Event(), Event()

    def prepare(_identity, _pin):
        entered.set()
        return release.wait(2)

    monkeypatch.setattr(summaries, "_file_identity", lambda _path: 1)
    monkeypatch.setattr(summaries, "_pin", lambda _repository, identity: {"input": identity})
    monkeypatch.setattr(worker, "_prepare", prepare)
    worker.wake()
    assert not worker.active
    assert worker.wait_for_publication() == "stopped"

    try:
        worker.start()
        assert entered.wait(2)
        assert worker.active
        assert worker.wait_for_publication(timeout=0) == "running"
        worker.shutdown(wait=False)
        assert worker.wait_for_publication() == "stopped"
    finally:
        release.set()
        worker.shutdown()
    assert not worker.active
