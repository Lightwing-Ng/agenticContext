"""Check bounded scheduling and proof consistency for saved WeChat views.

Code version: v1.0.0-codex.0
"""

from threading import Event
import time

import pytest

from app.core.chat_history_browser import ChatHistoryPage
from app.core import wechat_browser_read
from app.core.wechat_browser_read import WechatBrowserReadService


def _page(**messaging):
    return ChatHistoryPage(
        items=(),
        total_count=1,
        conversation_count=1,
        current_page=1,
        total_pages=1,
        messaging={"resource_index_status": "ready", **messaging},
    )


def _finished(service, job):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = service.status(job["task_id"])
        if status["state"] != "running":
            return status["state"]
        time.sleep(0.002)
    raise AssertionError("The saved-view task did not finish.")


def test_detail_can_finish_while_list_validation_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(wechat_browser_read, "MAX_TASKS", 3)
    list_entered, list_release = Event(), Event()
    detail_entered, detail_release = Event(), Event()
    details = _page()
    following_list = _page()

    def validate(parameters):
        if not parameters:
            list_entered.set()
            assert list_release.wait(5)

    def load(parameters):
        if parameters.get("session"):
            detail_entered.set()
            assert detail_release.wait(5)
            return details
        return following_list

    service = WechatBrowserReadService(
        tmp_path, loader=load, pin=lambda: "fixed", validator=validate,
    )
    try:
        _, first = service.acquire({}, wait=False)
        assert list_entered.wait(1)
        _, next_list = service.acquire({"account": "other"}, wait=False)
        selected = {"session": "selected"}
        _, detail = service.acquire(selected, wait=False)
        assert detail_entered.wait(1)
        with pytest.raises(RuntimeError, match="Several saved views"):
            service.acquire({"session": "over budget"}, wait=False)

        detail_release.set()
        assert _finished(service, detail) == "succeeded"
        assert service.acquire(selected, wait=False)[0] is details
        assert service.status(first["task_id"])["state"] == "running"
        assert service.status(next_list["task_id"])["state"] == "running"
        list_release.set()
        assert _finished(service, first) == "succeeded"
        assert _finished(service, next_list) == "succeeded"
    finally:
        detail_release.set()
        list_release.set()
        service.shutdown()


def test_list_cannot_publish_counts_after_detail_changes_proofs(tmp_path, monkeypatch):
    list_entered, list_release = Event(), Event()
    proofs = []

    def load(parameters):
        if parameters.get("session"):
            proofs.append("selected")
            return _page()
        observed = tuple(proofs)
        list_entered.set()
        assert list_release.wait(5)
        return _page(observed=observed)

    service = WechatBrowserReadService(
        tmp_path, loader=load, pin=lambda: "fixed", validator=lambda _: None,
    )
    monkeypatch.setattr(service, "_proofs", lambda: tuple(proofs))
    try:
        _, stale = service.acquire({}, wait=False)
        assert list_entered.wait(1)
        _, detail = service.acquire({"session": "selected"}, wait=False)
        assert _finished(service, detail) == "succeeded"
        list_release.set()

        assert _finished(service, stale) == "failed"
        assert service.acquire({}, wait=False)[0] is None
        current = service.retry(stale["task_id"])
        assert _finished(service, current) == "succeeded"
        page, _ = service.acquire({}, wait=False)
        assert page.messaging["observed"] == ("selected",)
    finally:
        list_release.set()
        service.shutdown()
