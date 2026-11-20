"""A completed refresh follows new messages after the verified conversation reloads.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

import sqlite3
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.web import local_resource_routes
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_wechat_refresh_e2e as refresh_fixtures
from tests.test_wechat_archive import make_archive
from tests.test_wechat_browser_read_e2e import ControlledRead

disposable_browser = refresh_fixtures.disposable_browser
refresh_keys = refresh_fixtures.refresh_keys
_REFRESH_STATUS_URL = "/api/browser/wechat/refresh/synthetic-conversation-refresh-identifier"


@pytest.mark.parametrize(
    ("initial_count", "requested_page", "query", "fragment", "follows_latest"),
    [
        pytest.param(100, "1", "", "", True, id="explicit-last-page"),
        pytest.param(200, None, "", "", True, id="unpaginated-conversation"),
        pytest.param(200, "1", "", "", False, id="explicit-earlier-page"),
        pytest.param(200, None, "group", "", False, id="search"),
        pytest.param(200, None, "", "bookmark", False, id="message-anchor"),
    ],
)
def test_refresh_restores_the_result_after_the_saved_view_is_ready(
    tmp_path, monkeypatch, disposable_browser, refresh_keys,
    initial_count, requested_page, query, fragment, follows_latest,
):
    updated_count = initial_count + 19
    original = make_archive(tmp_path / "original", repeated=initial_count)
    updated = make_archive(tmp_path / "updated", repeated=updated_count)
    store = tmp_path / "store"
    import_wechat_archive(original, store)
    account = "account:wxid_account_a"
    with sqlite3.connect(wechat_archive_path(store)) as connection:
        session = connection.execute(
            "SELECT id FROM conversations WHERE account_key=? AND kind='group'", (account,)
        ).fetchone()[0]
    controlled = ControlledRead(local_resource_routes.query_chat_history)
    controlled.phase.release.set()
    monkeypatch.setattr(local_resource_routes, "query_chat_history", controlled)
    monkeypatch.setattr(
        "app.web.app.SavedConfigStore.load",
        classmethod(lambda _cls: SavedConfigStore(CrawlConfig())),
    )
    app = create_app(
        store,
        computer_use_settings_path=tmp_path / "agent.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = disposable_browser.new_context(
        viewport={"width": 390, "height": 760}, reduced_motion="reduce"
    )
    try:
        page = context.new_page()
        scope = {
            "view": "text", "source": "wechat", "account": account, "chat_kind": "group",
            "session": session, "session_page": "7", "session_view": "1", "sort": "newest",
            "q": query,
        }
        if requested_page is not None:
            scope["page"] = requested_page
        url = f"http://127.0.0.1:{server.server_port}/browser?" + urlencode(scope)
        page.goto(f"{url}#{fragment}" if fragment else url)
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        refresh = page.get_by_role("button", name="Refresh chat history", exact=True)
        expect(refresh).to_be_visible()
        messages = page.locator("[data-chat-scrollport] [data-chat-message-id]")
        expect(messages).to_have_count(100)
        previous_last_message = messages.last.get_attribute("data-chat-message-id")
        page.evaluate("history.replaceState({unrelated: 'preserved'}, '', location.href)")
        page.route("**/api/browser/wechat/refresh", lambda route: route.fulfill(
            status=202,
            json={"state": "running", "status_url": _REFRESH_STATUS_URL},
        ))
        phase = controlled.reset()

        def complete_refresh(route):
            import_wechat_archive(updated, store)
            route.fulfill(json={
                "state": "succeeded", "added": 19, "total": updated_count, "partial": True,
                "skipped_parts": 1, "unavailable_parts": 2, "excluded": 3,
            })

        page.route("**" + _REFRESH_STATUS_URL, complete_refresh)
        refresh.click()
        expect(page.locator("[data-wechat-read-pending]")).to_be_visible()
        assert phase.entered.wait(timeout=5)
        assert page.evaluate("history.state.agenticContextWechatRefreshV1.added") == 19
        expected_scope = dict(scope)
        if follows_latest:
            expected_scope["page"] = str(initial_count // 100 + 1)
        destination = urlsplit(page.url)
        assert parse_qs(destination.query, keep_blank_values=True) == {
            key: [value] for key, value in expected_scope.items()
        }
        assert destination.fragment == fragment

        phase.release.set()
        expect(messages).to_have_count(19 if follows_latest else 100)
        message_ids = messages.evaluate_all(
            "nodes => nodes.map(node => node.dataset.chatMessageId)"
        )
        assert (previous_last_message in message_ids) != follows_latest
        banner = page.locator("[data-wechat-refresh-banner]")
        expect(banner.locator("[data-wechat-refresh-title]")).to_have_text(
            "Chat history partly refreshed"
        )
        expect(banner.locator("[data-wechat-refresh-copy]")).to_have_text(
            f"Added 19 messages. The conversation now contains {updated_count} messages. "
            "1 source could not be verified. 2 additional sources need verified keys. "
            "3 records could not be processed."
        )
        if follows_latest:
            page.wait_for_function("""() => {
                const port = document.querySelector('[data-chat-scrollport]');
                return port.scrollTop > 0 && port.scrollHeight - port.clientHeight - port.scrollTop < 2;
            }""")
        assert page.evaluate("history.state") == {"unrelated": "preserved"}
    finally:
        controlled.release_all()
        context.close()
        app.extensions["runtime_shutdown"]()
        server.shutdown()
        thread.join(timeout=5)
