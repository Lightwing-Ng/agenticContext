"""Exercise the bounded large-bank refresh allowance and cancellation.

Code version: v1.2.0-codex.0
"""

import stat
import json
from threading import Event
from types import SimpleNamespace

import pytest

from app.core import wechat_mac_refresh_worker as worker
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_mac_refresh_source import RefreshError
from app.core.wechat_mac_refresh_status import (
    ACCOUNT_TIMEOUT_MESSAGE, FAILED_MESSAGE, publication_failure_message,
)
from tests.test_wechat_mac_refresh_worker import Process, SESSION, SUCCESS


@pytest.mark.parametrize(
    ("mode", "size", "at", "succeeds", "account"),
    [
        (stat.S_IFREG, worker.LARGE_BANK_BYTES - 1, 601, False, False),
        (stat.S_IFREG, worker.LARGE_BANK_BYTES, 601, True, False),
        (stat.S_IFREG, worker.LARGE_BANK_BYTES, 2_701, False, False),
        (stat.S_IFLNK, worker.LARGE_BANK_BYTES, 601, False, False),
        (stat.S_IFREG, worker.LARGE_BANK_BYTES, 2_701, True, True),
        (stat.S_IFREG, worker.LARGE_BANK_BYTES, 5_401, False, True),
    ],
)
def test_refresh_budgets_preserve_cooperative_cancellation(
    tmp_path, monkeypatch, mode, size, at, succeeds, account,
):
    metadata = SimpleNamespace(st_mode=mode, st_size=size)
    path = SimpleNamespace(lstat=lambda: metadata)
    monkeypatch.setattr(bank, "mac_recovery_bank_path", lambda _root: path)
    monkeypatch.setattr(worker, "_compatible_python", lambda *_args: "/test/python")
    simulated_time = iter((0, at, at))

    class FinishingProcess(Process):
        def __init__(self):
            super().__init__(running=True)
            self.ready = False

        def poll(self):
            if self.ready:
                self.returncode = 0
            return super().poll()

        def finish(self, _delay):
            self.ready = True

    process = FinishingProcess()
    monkeypatch.setattr(
        worker, "time",
        SimpleNamespace(monotonic=lambda: next(simulated_time), sleep=process.finish),
    )
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_a, **_kw: process)

    scope = "wxid_account_a" if account else SESSION
    if succeeds:
        assert worker.run_compatible_worker(tmp_path, scope, Event(), account=account) == SUCCESS
        assert b"cancel" not in process.stdin.retained
    else:
        error = worker.AccountRefreshDeadlineExceeded if account else RefreshError
        with pytest.raises(error, match="time limit" if account else "interrupted"):
            worker.run_compatible_worker(tmp_path, scope, Event(), account=account)
        assert b"cancel" in process.stdin.retained


def test_publication_failure_can_cross_worker_transport_without_exception_text():
    recognized = publication_failure_message(
        ValueError("A reused baseline requires private owned parent directories.")
    )
    assert recognized == "The staged archive could not be verified in its private directory."
    assert publication_failure_message(ValueError("private/path and source record")) == FAILED_MESSAGE
    result = {**SUCCESS, "state": "failed", "phase": "saving", "message": recognized}
    assert worker._safe_result(json.dumps(result)) == result
    timeout = {**result, "phase": "checking", "message": ACCOUNT_TIMEOUT_MESSAGE}
    assert worker._safe_result(json.dumps(timeout)) == timeout
