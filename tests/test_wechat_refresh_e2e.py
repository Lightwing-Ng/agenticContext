"""WeChat refresh actions retain shared geometry, scope, and honest progress.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import pytest
from playwright.sync_api import expect

from tests import test_wechat_browser_e2e as wechat_fixtures


disposable_browser = wechat_fixtures.disposable_browser
wechat_server = wechat_fixtures.wechat_server


@pytest.fixture
def refresh_keys(monkeypatch):
    """Expose only synthetic availability; no test reads a real database key."""
    from app.core.wechat_mac_refresh import WechatMacRefreshService

    state = {"available": True}
    monkeypatch.setattr(
        WechatMacRefreshService,
        "available",
        lambda *_args, **_kwargs: state["available"],
    )
    return state


def _open_chat(browser, server, *, width=1280, kind="group", official=False):
    context = browser.new_context(
        viewport={"width": width, "height": 937}, reduced_motion="reduce"
    )
    page = context.new_page()
    account = "account:wxid_account_b" if official else "account:wxid_account_a"
    if kind == "unclassified":
        account = "source:xlsx-3"
    parameters = {
        "view": "text",
        "source": "wechat",
        "account": account,
        "chat_kind": kind,
        "sort": "oldest",
        "session_view": "1",
    }
    page.goto(server + "/browser?" + urlencode(parameters), wait_until="networkidle")
    expect(page.locator("[data-wechat-conversation-refresh]")).to_have_count(0)
    title = "Official fixture" if official else "Same title"
    link = (
        page.locator("tr[data-chat-session-id] a[href*='session=']").first
        if kind == "unclassified"
        else page.get_by_role("link", name=title, exact=True)
    )
    destination = urlsplit(urljoin(server, link.get_attribute("href")))
    values = parse_qs(destination.query)
    values.update(session_page=["7"], page=["2"])
    page.goto(
        destination._replace(query=urlencode(values, doseq=True)).geturl(),
        wait_until="networkidle",
    )
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    return context, page


@pytest.mark.parametrize(
    ("available", "kind", "official", "expected"),
    [
        (True, "direct", False, True),
        (True, "group", False, True),
        (False, "direct", False, False),
        (False, "group", False, False),
        (True, "direct", True, False),
        (True, "unclassified", False, False),
    ],
)
def test_refresh_is_visible_only_for_configured_private_and_group_chats(
    disposable_browser, wechat_server, refresh_keys, available, kind, official, expected
):
    refresh_keys["available"] = available
    context, page = _open_chat(
        disposable_browser, wechat_server, kind=kind, official=official
    )
    try:
        refresh = page.get_by_role("button", name="Refresh chat history", exact=True)
        expect(refresh).to_have_count(1 if expected else 0)
        if expected:
            expect(refresh).to_be_visible()
        expect(page.get_by_role("link", name="Export conversation as CSV", exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 677, 390, 320])
def test_refresh_keeps_shared_geometry_and_does_not_open_the_export_menu(
    disposable_browser, wechat_server, refresh_keys, width
):
    context, page = _open_chat(disposable_browser, wechat_server, width=width)
    try:
        refresh = page.get_by_role("button", name="Refresh chat history", exact=True)
        share = page.get_by_role("link", name="Export conversation as CSV", exact=True)
        menu = page.get_by_role("menu", name="Export conversation", exact=True)
        refresh.hover()
        expect(menu).to_be_hidden()
        refresh.focus()
        expect(refresh).to_be_focused()
        expect(menu).to_be_hidden()
        geometry = page.evaluate("""async () => {
            await document.fonts.ready;
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const bounds = selector => {
                const box = document.querySelector(selector).getBoundingClientRect();
                return {left: box.left, right: box.right, top: box.top,
                    width: box.width, height: box.height,
                    x: box.left + box.width / 2, y: box.top + box.height / 2};
            };
            return {
                refresh: bounds('[data-wechat-conversation-refresh]'),
                glyph: bounds('.wechat-conversation-refresh-icon'),
                share: bounds('.wechat-conversation-share'),
                theme: bounds('#global_theme_toggle'),
                search: bounds('.browser-search-control'),
                scopeRemove: bounds('.browser-search-session-tag > button'),
                input: bounds('#browser_search_input'),
                inputPadding: parseFloat(getComputedStyle(document.getElementById('browser_search_input')).paddingLeft)
                    + parseFloat(getComputedStyle(document.getElementById('browser_search_input')).paddingRight),
                gap: parseFloat(getComputedStyle(document.documentElement)
                    .getPropertyValue('--layout-global-action-gap')),
                mask: getComputedStyle(document.querySelector('.wechat-conversation-refresh-icon')).maskImage,
                overflow: document.documentElement.scrollWidth - innerWidth,
            };
        }""")
        assert [geometry["refresh"]["width"], geometry["refresh"]["height"]] == [32, 32]
        assert [geometry["glyph"]["width"], geometry["glyph"]["height"]] == [18, 18]
        assert geometry["mask"].endswith('/square.and.arrow.down.on.square.svg")')
        assert geometry["share"]["left"] - geometry["refresh"]["right"] == pytest.approx(
            geometry["gap"], abs=1
        ), geometry
        assert geometry["refresh"]["y"] == pytest.approx(geometry["share"]["y"], abs=1), geometry
        assert geometry["share"]["x"] == pytest.approx(geometry["theme"]["x"], abs=1), geometry
        assert geometry["search"]["right"] <= geometry["refresh"]["left"] - 8, geometry
        assert geometry["scopeRemove"]["left"] >= geometry["search"]["left"], geometry
        assert geometry["scopeRemove"]["right"] <= geometry["input"]["left"], geometry
        assert geometry["input"]["width"] > geometry["inputPadding"], geometry
        assert geometry["input"]["right"] <= geometry["search"]["right"], geometry
        assert geometry["overflow"] <= 1, geometry
        page.keyboard.press("Tab")
        expect(share).to_be_focused()
        if width <= 900:
            page.locator("#sidebar_toggle").click()
            expect(page.locator("#sidebar_toggle")).to_have_attribute("aria-expanded", "true")
            expect(refresh).to_be_hidden()
            expect(share).to_be_hidden()
            expect(page.locator("#global_theme_toggle")).to_be_visible()
            page.locator("#sidebar_toggle").click()
            expect(refresh).to_be_visible()
            expect(share).to_be_visible()
    finally:
        context.close()


def test_refresh_shows_immediate_progress_and_sends_only_one_pending_request(
    disposable_browser, wechat_server, refresh_keys
):
    context, page = _open_chat(disposable_browser, wechat_server)
    pending = []
    try:
        refresh = page.locator("[data-wechat-conversation-refresh]")
        endpoint = refresh.get_attribute("data-wechat-refresh-url")
        session_id = refresh.get_attribute("data-wechat-session-id")
        page.route(urljoin(wechat_server, endpoint), lambda route: pending.append(route))
        refresh.click(no_wait_after=True)
        expect(refresh).to_be_disabled()
        expect(refresh).to_have_attribute("aria-busy", "true")
        expect(page.locator("#cache_wait_modal")).to_be_visible()
        expect(page.locator("#cache_wait_modal_title")).to_have_text("Refreshing chat history")
        refresh.evaluate("button => button.dispatchEvent(new MouseEvent('click', {bubbles:true}))")
        assert len(pending) == 1
        request = pending[0].request
        assert request.method == "POST"
        assert request.post_data_json == {"session_id": session_id}
        pending.pop().fulfill(status=503, json={"error": "The test source is unavailable."})
        expect(refresh).to_be_enabled()
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
    finally:
        for route in pending:
            route.abort()
        context.close()


@pytest.mark.parametrize("stage", ["start", "status"])
def test_refresh_failure_restores_the_action_and_shows_a_shared_error_notice(
    disposable_browser, wechat_server, refresh_keys, stage
):
    context, page = _open_chat(disposable_browser, wechat_server)
    try:
        refresh = page.get_by_role("button", name="Refresh chat history", exact=True)
        endpoint = refresh.get_attribute("data-wechat-refresh-url")
        original_url = page.url
        error = "The synthetic source could not be refreshed."
        if stage == "start":
            page.route(urljoin(wechat_server, endpoint), lambda route: route.fulfill(
                status=409, json={"error": error}
            ))
        else:
            page.route(urljoin(wechat_server, endpoint), lambda route: route.fulfill(
                status=202,
                json={"state": "running", "status_url": "/api/test-wechat-refresh/status"},
            ))
            page.route("**/api/test-wechat-refresh/status", lambda route: route.fulfill(
                json={"state": "failed", "message": error}
            ))
        refresh.click()
        banner = page.locator("[data-wechat-refresh-banner]")
        expect(banner).to_be_visible()
        classes = banner.get_attribute("class").split()
        assert "error" in classes and "notice" not in classes
        expect(banner.locator("[data-wechat-refresh-copy]")).to_have_text(error)
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        expect(refresh).to_be_enabled()
        expect(refresh).not_to_have_attribute("aria-busy", "true")
        expect(refresh).to_have_attribute("title", "Refresh chat history")
        assert page.url == original_url
        banner.hover()
        banner.get_by_role("button", name="Close refresh result", exact=True).click()
        expect(banner).to_be_hidden()
    finally:
        context.close()


def test_refresh_reloads_the_same_scope_and_preserves_contact_history_for_one_notice(
    disposable_browser, wechat_server, refresh_keys
):
    context, page = _open_chat(disposable_browser, wechat_server, kind="direct")
    try:
        refresh = page.get_by_role("button", name="Refresh chat history", exact=True)
        endpoint = refresh.get_attribute("data-wechat-refresh-url")
        original_url = page.url
        page.evaluate("""() => {
            const port = document.querySelector('[data-chat-scrollport]');
            const message = port.querySelector('[data-chat-message-id][id]');
            window.history.replaceState({
                unrelated: {value: 'preserved'},
                agenticContextWechatContactReturnV1: {
                    version: 1, url: window.location.href,
                    scrollTop: port.scrollTop, scrollLeft: port.scrollLeft,
                    documentX: scrollX, documentY: scrollY,
                    anchorId: message.id,
                    anchorOffset: message.getBoundingClientRect().top - port.getBoundingClientRect().top,
                    openDetails: [], openForwarded: [], focusedLinkId: '',
                },
            }, '', window.location.href);
        }""")
        page.route(urljoin(wechat_server, endpoint), lambda route: route.fulfill(
            status=202,
            json={"state": "running", "status_url": "/api/test-wechat-refresh/status"},
        ))
        reads = []

        def status(route):
            reads.append(route.request.url)
            route.fulfill(json={
                "state": "running" if len(reads) == 1 else "succeeded",
                "added": 1234, "total": 5678, "partial": True,
            })

        page.route("**/api/test-wechat-refresh/status", status)
        with page.expect_navigation(wait_until="domcontentloaded"):
            refresh.click(no_wait_after=True)
        assert page.url == original_url
        banner = page.locator("[data-wechat-refresh-banner]")
        expect(banner).to_be_visible()
        expect(banner.locator("[data-wechat-refresh-title]")).to_have_text("Chat history partly refreshed")
        expect(banner.locator("[data-wechat-refresh-copy]")).to_have_text(
            "Added 1,234 messages. The conversation now contains 5,678 messages. "
            "Some sources could not be refreshed."
        )
        state = page.evaluate("window.history.state")
        assert state["unrelated"] == {"value": "preserved"}
        assert state["agenticContextWechatContactReturnV1"]["url"] == original_url
        assert state["agenticContextWechatContactReturnV1"]["anchorId"]
        assert "agenticContextWechatRefreshV1" not in state
        assert len(reads) == 2
        page.reload(wait_until="networkidle")
        expect(page.locator("[data-wechat-refresh-banner]")).to_be_hidden()
        assert page.url == original_url
    finally:
        context.close()
