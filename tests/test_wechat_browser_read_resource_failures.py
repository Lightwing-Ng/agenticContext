"""Rejected resource indexes retain safe guidance without admitting a saved page.

Code version: v1.0.0-codex.0
"""

import json
from threading import Event

from bs4 import BeautifulSoup
import pytest

from app.core.chat_history_browser import ChatHistoryPage, query_chat_history
from app.core.wechat_browser_read import WechatBrowserReadService
from tests import test_wechat_browser_read as read_tests
from tests import test_wechat_resources as resources


resource_app = resources.resource_app
_WARNING = "Media and file references need refreshing after the archive changed."
_PRIVATE_PATH = "/private/synthetic-account/unverified-resource-origin"


def _page(status="ready", **metadata):
    return ChatHistoryPage(
        items=(), total_count=7, conversation_count=2, current_page=1, total_pages=1,
        messaging={"resource_index_status": status, "private_path": _PRIVATE_PATH, **metadata},
    )


@pytest.mark.parametrize("status", ["stale", "invalid"])
def test_known_resource_failure_has_safe_guidance_and_never_returns_the_page(tmp_path, status):
    service = WechatBrowserReadService(
        tmp_path, loader=lambda _: _page(status), pin=lambda: "fixed", validator=lambda _: None,
    )
    try:
        page, job = service.acquire({}, wait=False)
        assert page is None
        failed = read_tests._done(service, job["task_id"])
        assert failed["state"] == "failed"
        assert failed["message"] == _WARNING
        assert set(failed) == {"task_id", "generation", "state", "message"}
        assert _PRIVATE_PATH not in json.dumps(failed)
        assert service.acquire({})[0] is None
    finally:
        service.shutdown()


@pytest.mark.parametrize("stage", ["validator", "loader", "projection"])
def test_other_failures_do_not_infer_resource_guidance_from_private_error_text(tmp_path, stage):
    calls = []

    def reject(_):
        raise ValueError(_WARNING + " " + _PRIVATE_PATH)

    def load(parameters):
        calls.append(parameters)
        if stage == "loader":
            reject(parameters)
        return _page(event_projection={"status": "invalid", "private_path": _PRIVATE_PATH})

    service = WechatBrowserReadService(
        tmp_path, loader=load, pin=lambda: "fixed",
        validator=reject if stage == "validator" else lambda _: None,
    )
    try:
        _, job = service.acquire({}, wait=False)
        failed = read_tests._done(service, job["task_id"])
        assert failed["state"] == "failed"
        assert "could not be checked" in failed["message"]
        assert "Media and file references need refreshing" not in failed["message"]
        assert _PRIVATE_PATH not in json.dumps(failed)
        assert service.acquire({})[0] is None
        if stage == "validator":
            assert calls == []
    finally:
        service.shutdown()


@pytest.mark.parametrize("status", ["stale", "invalid"])
def test_running_read_becomes_a_safe_failed_warning_in_poll_and_document(resource_app, status):
    app = resource_app
    service = app.extensions["wechat_browser_read"]
    page = query_chat_history(app.config["TEST_STORE_ROOT"], source="wechat")
    page.messaging.update(resource_index_status=status, private_path=_PRIVATE_PATH)
    entered, release = Event(), Event()

    def blocked_loader(_):
        entered.set()
        assert release.wait(3)
        return page

    service.loader = blocked_loader
    client = app.test_client()
    address = "/browser?view=text&source=wechat"
    try:
        initial = client.get(address)
        assert initial.status_code == 200
        assert entered.wait(1)
        shell = BeautifulSoup(initial.data, "html.parser").select_one("[data-wechat-read-pending]")
        assert shell["data-state"] == "running"
        assert "Media and file references need refreshing" not in initial.get_data(as_text=True)
        status_url = shell["data-status-url"]
        assert client.get(status_url).json["state"] == "running"

        release.set()
        assert read_tests._done(service, shell["data-task-id"])["state"] == "failed"
        polled = client.get(status_url)
        assert polled.status_code == 200 and polled.cache_control.no_store
        assert polled.json["state"] == "failed"
        assert polled.json["message"] == _WARNING
        assert _PRIVATE_PATH not in polled.get_data(as_text=True)

        failed_document = client.get(address)
        assert failed_document.status_code == 200
        soup = BeautifulSoup(failed_document.data, "html.parser")
        failed_shell = soup.select_one("[data-wechat-read-pending]")
        assert failed_shell["data-state"] == "failed"
        assert failed_shell.select_one("[data-wechat-read-copy]").get_text() == _WARNING
        assert soup.select_one(".wechat-conversation-table") is None
        assert soup.select_one("[data-chat-message-id]") is None
        assert _PRIVATE_PATH not in failed_document.get_data(as_text=True)
        assert service.acquire({})[0] is None
    finally:
        release.set()
        app.extensions["runtime_shutdown"]()
