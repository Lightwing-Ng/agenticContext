"""Pending saved views retain honest content and bounded document navigation.

Code version: v1.6.1-codex.0
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
import sqlite3
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.wechat_mac_refresh import WechatMacRefreshService
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from app.web import local_resource_routes
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e as browser_fixtures
from tests.test_wechat_archive import make_archive


disposable_browser = browser_fixtures.disposable_browser
_STATUS_PATTERN = "**/api/browser/wechat/read/**"
_REFRESH_STATUS_URL = "/api/browser/wechat/refresh/synthetic-account-refresh-identifier"


class ControlledRead:
    """Gate only synthetic page loading while the actual read-job service runs."""

    def __init__(self, original):
        self.original = original
        self.phases = []
        self.reset()

    def reset(self):
        self.phase = SimpleNamespace(release=Event(), entered=Event(), fail=False)
        self.phases.append(self.phase)
        return self.phase

    def __call__(self, *args, **kwargs):
        phase = self.phase
        phase.entered.set()
        if not phase.release.wait(timeout=30):
            raise TimeoutError("Synthetic saved-view read was not released.")
        if phase.fail:
            raise ValueError("Synthetic private source failure must remain hidden.")
        return self.original(*args, **kwargs)

    def release_all(self):
        for phase in self.phases:
            phase.release.set()


@pytest.fixture
def read_server(tmp_path, monkeypatch, request):
    archive = make_archive(tmp_path / "archive", repeated=getattr(request, "param", 6))
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    history = wechat_archive_path(store)
    registry = contact_registry_path(store)
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
    with sqlite3.connect(history) as connection:
        session = connection.execute(
            "SELECT id FROM conversations WHERE account_key=? AND kind='group' LIMIT 1",
            ("account:wxid_account_a",),
        ).fetchone()[0]
    before = history.read_bytes(), registry.read_bytes()
    controlled = ControlledRead(local_resource_routes.query_chat_history)
    monkeypatch.setattr(local_resource_routes, "query_chat_history", controlled)
    monkeypatch.setattr(
        "app.web.app.SavedConfigStore.load",
        classmethod(lambda _cls: SavedConfigStore(CrawlConfig())),
    )
    application = create_app(
        store,
        computer_use_settings_path=tmp_path / "agent.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    # Source-pin behavior has separate core tests; this fixture fixes only its own input pin.
    application.extensions["wechat_browser_read"]._pin_override = lambda: "synthetic-browser-input-v1"
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}",
            controlled=controlled, session=session, application=application,
        )
    finally:
        controlled.release_all()
        application.extensions["runtime_shutdown"]()
        server.shutdown()
        thread.join(timeout=5)
        assert (history.read_bytes(), registry.read_bytes()) == before


class ReadStatusScenario:
    """Keep genuine task coordinates around controlled status transport outcomes."""

    def __init__(self):
        self.requests = []
        self.state = "running"
        self.status = None
        self.mismatch = None
        self.hold_next = False
        self.held = []

    def __call__(self, route):
        self.requests.append((route.request.method, route.request.url))
        if self.hold_next and route.request.method == "GET":
            self.hold_next = False
            self.held.append(route)
            return
        self.fulfill(route)

    def fulfill(self, route):
        response = route.fetch()
        payload = response.json()
        if route.request.method == "GET":
            if self.state is not None:
                payload["state"] = self.state
            if self.mismatch == "task":
                payload["task_id"] = "another-synthetic-task-identifier"
            elif self.mismatch == "generation":
                payload["generation"] += 1
        route.fulfill(response=response, json=payload, status=self.status or response.status)


def _install_poll_probe(page):
    """Advance actual controller timers without waiting through their backoff."""
    page.clock.install()
    page.clock.pause_at(datetime.now(UTC) + timedelta(seconds=1))
    page.add_init_script("""(() => {
        const probe = window.__wechatReadProbe = {
            attempts: 0, states: [], delays: [], domContentLoaded: false,
        };
        document.addEventListener('DOMContentLoaded', () => {
            probe.domContentLoaded = true;
        }, {once: true});
        const fetch = window.fetch.bind(window);
        window.fetch = async (input, options) => {
            const status = String(input).includes('/api/browser/wechat/read/')
                && (options?.method || 'GET').toUpperCase() === 'GET';
            if (status) probe.attempts++;
            const response = await fetch(input, options);
            if (status) {
                const value = await response.clone().json().catch(() => ({}));
                probe.states.push(value.state || 'invalid');
            }
            return response;
        };
        const timeout = window.setTimeout.bind(window);
        window.setTimeout = (callback, delay, ...args) => {
            if (delay <= 1_000 && new Error().stack.includes('wechat-browser-read.js')) {
                probe.delays.push(Number(delay));
            }
            return timeout(callback, delay, ...args);
        };
    })();""")


def _open_pending(browser, fixture, *, width=1280, detail=False, scenario=None, legacy_timeout=False):
    context = browser.new_context(
        viewport={"width": width, "height": 1157 if width == 836 else 937},
        reduced_motion="reduce",
    )
    page = context.new_page()
    documents, errors = [], []
    page.on("request", lambda incoming: documents.append(incoming.url)
            if incoming.resource_type == "document" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    _install_poll_probe(page)
    if legacy_timeout:
        page.add_init_script("Object.defineProperty(AbortSignal, 'timeout', { value: undefined });")
    scenario = scenario or ReadStatusScenario()
    page.route(_STATUS_PATTERN, scenario)
    parameters = {
        "view": "text", "source": "wechat",
        "account": "account:wxid_account_a", "chat_kind": "group",
        "sort": "oldest", "q": "",
    }
    if detail:
        parameters.update(session=fixture.session, session_page="7", page="2")
    page.goto(fixture.url + "/browser?" + urlencode(parameters), wait_until="domcontentloaded")
    expect(page.locator("[data-wechat-read-pending]")).to_be_visible()
    expect(page.get_by_role("region", name="Saved chat history")).to_have_attribute("aria-busy", "true")
    expect(page.locator("#cache_wait_modal")).to_be_hidden()
    page.wait_for_function("window.__wechatReadProbe.states.length >= 1")
    assert page.evaluate("window.__wechatReadProbe.attempts") == 1
    # The status route can answer before the independent loader starts.
    assert fixture.controlled.phase.entered.wait(1), "The synthetic saved-view loader did not start."

    def close():
        try:
            while scenario.held:
                scenario.held.pop().abort()
            page.unroute_all(behavior="wait")
        finally:
            context.close()

    return SimpleNamespace(
        close=close, page=page, scenario=scenario,
        documents=documents, errors=errors, original_url=page.url,
    )


def _advance_to_next_poll(page):
    attempts = page.evaluate("window.__wechatReadProbe.attempts")
    page.wait_for_function(
        "count => window.__wechatReadProbe.delays.length >= count", arg=attempts
    )
    delay = page.evaluate("window.__wechatReadProbe.delays.at(-1)")
    assert 0 < delay <= 1_000
    page.clock.run_for(delay)
    return attempts


def _request_next_poll(page, *, wait_response=True):
    attempts = _advance_to_next_poll(page)
    page.wait_for_function(
        "count => window.__wechatReadProbe.attempts >= count", arg=attempts + 1
    )
    if wait_response:
        page.wait_for_function(
            "count => window.__wechatReadProbe.states.length >= count", arg=attempts + 1
        )


def _finish_pending_read(page, fixture):
    task = page.locator("[data-wechat-read-pending]").get_attribute("data-task-id")
    fixture.controlled.phase.release.set()
    service = fixture.application.extensions["wechat_browser_read"]
    deadline = monotonic() + 5
    while service.status(task)["state"] == "running":
        assert monotonic() < deadline, "The synthetic saved view did not finish."
        Event().wait(0.01)
    assert service.status(task)["state"] == "succeeded"
    with page.expect_navigation(wait_until="domcontentloaded"), page.expect_request(_STATUS_PATTERN):
        _advance_to_next_poll(page)
    expect(page.locator("[data-wechat-read-pending]")).to_have_count(0)


@pytest.mark.parametrize("width", [1280, 832, 390])
def test_selected_account_refresh_keeps_search_right_after_saved_view_fails(
    disposable_browser, read_server, monkeypatch, width
):
    monkeypatch.setattr(WechatMacRefreshService, "account_available", lambda *_args: True)
    opened = _open_pending(disposable_browser, read_server, width=width)
    page = opened.page
    try:
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        refresh = page.get_by_role("button", name="Refresh WeChat account", exact=True)
        expect(refresh).to_be_enabled()
        opened.scenario.state = "failed"
        _request_next_poll(page)
        expect(page.locator("[data-wechat-read-retry]")).to_be_visible()
        expect(page.locator("[data-wechat-exchange-import-trigger]")).to_have_count(0)
        geometry = refresh.evaluate("""node => {
            const toolbar = node.parentElement;
            const search = node.nextElementSibling.querySelector('.browser-search-control');
            const refreshBounds = node.getBoundingClientRect();
            const searchBounds = search.getBoundingClientRect();
            const glyph = node.querySelector('.icon');
            return {
                rightOffset: toolbar.getBoundingClientRect().right - searchBounds.right,
                gap: searchBounds.left - refreshBounds.right,
                gapToken: parseFloat(getComputedStyle(toolbar).columnGap),
                mask: getComputedStyle(glyph).maskImage,
                overflow: document.documentElement.scrollWidth - innerWidth,
            };
        }""")
        assert geometry["rightOffset"] == pytest.approx(0, abs=1)
        assert geometry["gap"] == pytest.approx(geometry["gapToken"], abs=1)
        assert geometry["mask"].endswith('/square.and.arrow.down.on.square.svg")')
        assert geometry["overflow"] <= 1
        assert opened.errors == []
    finally:
        opened.close()


def test_account_refresh_from_pending_view_posts_only_account_and_keeps_the_url(
    disposable_browser, read_server, monkeypatch
):
    monkeypatch.setattr(WechatMacRefreshService, "account_available", lambda *_args: True)
    opened = _open_pending(disposable_browser, read_server)
    page = opened.page
    try:
        refresh = page.get_by_role("button", name="Refresh WeChat account", exact=True)
        endpoint = read_server.url + refresh.get_attribute("data-wechat-refresh-url")
        requests = []

        def refresh_account(route):
            requests.append((route.request.method, route.request.post_data_json))
            route.fulfill(json={"state": "succeeded", "added": 1234, "partial": True})

        page.route(endpoint, refresh_account)
        with page.expect_navigation(wait_until="domcontentloaded"):
            refresh.click(no_wait_after=True)
        assert page.url == opened.original_url
        assert requests == [("POST", {"account_id": "wxid_account_a"})]
        banner = page.locator("[data-wechat-refresh-banner]")
        expect(banner.locator("[data-wechat-refresh-title]")).to_have_text("WeChat account partly refreshed")
        expect(banner.locator("[data-wechat-refresh-copy]")).to_contain_text("Added 1,234 messages.")
        assert opened.errors == []
    finally:
        opened.close()


@pytest.mark.parametrize("result", ["failed", "expired", "interrupted", "publication-unknown"])
def test_account_refresh_result_stays_visible_when_retrying_the_saved_view(
    disposable_browser, read_server, monkeypatch, result
):
    monkeypatch.setattr(WechatMacRefreshService, "account_available", lambda *_args: True)
    opened = _open_pending(disposable_browser, read_server)
    page = opened.page
    try:
        opened.scenario.state = "failed"
        _request_next_poll(page)
        retry = page.get_by_role("button", name="Try again", exact=True)
        expect(retry).to_be_enabled()
        refresh = page.locator("[data-wechat-account-refresh]")
        endpoint = read_server.url + refresh.get_attribute("data-wechat-refresh-url")
        refresh_methods, status_methods = [], []
        failure = "WeChat account refresh timed out while checking changes."

        def begin_refresh(route):
            refresh_methods.append(route.request.method)
            route.fulfill(status=202, json={"state": "running", "status_url": _REFRESH_STATUS_URL})

        def read_refresh_status(route):
            status_methods.append(route.request.method)
            if result == "expired":
                route.fulfill(status=404, json={"error": "The refresh status was lost."})
            elif result == "failed":
                route.fulfill(json={"state": "failed", "message": failure})
            else:
                route.fulfill(json={
                    "state": "interrupted", "publication_unknown": result == "publication-unknown",
                })

        page.route(endpoint, begin_refresh)
        page.route("**" + _REFRESH_STATUS_URL, read_refresh_status)
        with page.expect_response(endpoint):
            refresh.click()
        expect(page.get_by_role("dialog", name="Refreshing WeChat account")).to_be_visible()
        expect(retry).to_be_disabled()
        page.wait_for_function("history.state?.agenticContextWechatPendingRefreshV1")
        if result != "failed":
            page.reload(wait_until="domcontentloaded")
            expect(refresh).to_have_attribute("aria-busy", "true")
        page.clock.run_for(1_000)
        banner = page.locator("[data-wechat-refresh-banner]")
        expect(banner).to_be_visible()
        title = banner.locator("[data-wechat-refresh-title]")
        copy = banner.locator("[data-wechat-refresh-copy]")
        if result == "failed":
            expect(title).to_have_text("Unable to refresh WeChat account")
            expect(copy).to_have_text(failure)
            assert "error" in banner.get_attribute("class").split()
        elif result == "interrupted":
            expect(title).to_have_text("WeChat account refresh interrupted")
            expect(copy).to_have_text("The local service stopped before this refresh finished. You can try again.")
            assert "notice" in banner.get_attribute("class").split()
        else:
            expect(title).to_have_text("WeChat account refresh status unavailable")
            expect(copy).to_contain_text("The local service could not confirm the result.")
            assert "notice" in banner.get_attribute("class").split()
        message = copy.inner_text()
        assert refresh_methods == ["POST"]
        assert status_methods == ["GET"]
        assert page.evaluate("history.state?.agenticContextWechatPendingRefreshV1") is None
        expect(retry).to_be_enabled()
        with page.expect_navigation(wait_until="domcontentloaded"):
            retry.click()
        expect(copy).to_have_text(message)
        assert page.url == opened.original_url
        banner.get_by_role("button", name="Close refresh result").press("Enter")
        page.reload(wait_until="domcontentloaded")
        expect(banner).to_be_hidden()
        assert refresh_methods == ["POST"]
        assert status_methods == ["GET"]
        assert opened.errors == []
    finally:
        opened.close()


def test_legacy_lost_account_refresh_status_is_restored_as_an_uncertain_outcome(
    disposable_browser, read_server
):
    opened = _open_pending(disposable_browser, read_server)
    page = opened.page
    try:
        page.evaluate("""() => history.replaceState({
            ...history.state,
            agenticContextWechatRefreshV1: {
                url: location.href, version: 1, account: true,
                error: "The refresh status was lost. The local service may have restarted. "
                    + "Reload this conversation to check for updates before trying again.",
            },
        }, "", location.href)""")
        page.reload(wait_until="domcontentloaded")
        banner = page.locator("[data-wechat-refresh-banner]")
        expect(banner.locator("[data-wechat-refresh-title]")).to_have_text("WeChat account refresh status unavailable")
        expect(banner.locator("[data-wechat-refresh-copy]")).to_contain_text("The local service could not confirm the result.")
        assert "notice" in banner.get_attribute("class").split()
        saved = page.evaluate("history.state.agenticContextWechatRefreshV1")
        assert saved["interrupted"] is True
        assert saved["publicationUnknown"] is True
        assert "error" not in saved
        assert opened.errors == []
    finally:
        opened.close()


def test_saved_view_and_account_refresh_poll_without_abort_signal_timeout(
    disposable_browser, read_server, monkeypatch
):
    monkeypatch.setattr(WechatMacRefreshService, "account_available", lambda *_args: True)
    opened = _open_pending(disposable_browser, read_server, legacy_timeout=True)
    page = opened.page
    try:
        refresh = page.locator("[data-wechat-account-refresh]")
        endpoint = read_server.url + refresh.get_attribute("data-wechat-refresh-url")
        methods = []

        def begin(route):
            methods.append(route.request.method)
            route.fulfill(status=202, json={"state": "running", "status_url": _REFRESH_STATUS_URL})

        def read_refresh_status(route):
            methods.append(route.request.method)
            route.fulfill(json={"state": "interrupted", "publication_unknown": False})

        page.route(endpoint, begin)
        page.route("**" + _REFRESH_STATUS_URL, read_refresh_status)
        refresh.click()
        page.wait_for_function("history.state?.agenticContextWechatPendingRefreshV1")
        with page.expect_response("**" + _REFRESH_STATUS_URL):
            page.clock.fast_forward(1_000)

        expect(page.locator("[data-wechat-refresh-title]")).to_have_text("WeChat account refresh interrupted")
        assert methods == ["POST", "GET"]
        assert all(method == "GET" for method, _url in opened.scenario.requests)
        assert opened.errors == []
    finally:
        opened.close()


def test_account_refresh_reconnects_after_a_transport_outage_without_replaying_the_post(
    disposable_browser, read_server, monkeypatch
):
    monkeypatch.setattr(WechatMacRefreshService, "account_available", lambda *_args: True)
    opened = _open_pending(disposable_browser, read_server)
    page = opened.page
    try:
        refresh = page.locator("[data-wechat-account-refresh]")
        endpoint = read_server.url + refresh.get_attribute("data-wechat-refresh-url")
        refresh_methods, status_methods = [], []

        def begin_refresh(route):
            refresh_methods.append(route.request.method)
            route.fulfill(status=202, json={"state": "running", "status_url": _REFRESH_STATUS_URL})

        def read_refresh_status(route):
            status_methods.append(route.request.method)
            if len(status_methods) <= 3:
                route.fulfill(status=503, json={"error": "Temporarily unavailable."})
            else:
                route.fulfill(json={"state": "interrupted", "publication_unknown": False})

        def advance_to_poll(milliseconds):
            with page.expect_response("**" + _REFRESH_STATUS_URL):
                page.clock.fast_forward(milliseconds)

        page.route(endpoint, begin_refresh)
        page.route("**" + _REFRESH_STATUS_URL, read_refresh_status)
        refresh.click()
        page.wait_for_function("history.state?.agenticContextWechatPendingRefreshV1")
        advance_to_poll(1_000)
        advance_to_poll(9_000)
        expect(refresh).to_have_attribute("aria-busy", "true")
        assert refresh_methods == ["POST"]
        advance_to_poll(22_000)

        banner = page.locator("[data-wechat-refresh-banner]")
        title = banner.locator("[data-wechat-refresh-title]")
        expect(title).to_have_text("WeChat account refresh status unavailable")
        expect(refresh).to_be_enabled()
        assert page.evaluate("history.state.agenticContextWechatPendingRefreshV1.statusUrl") == _REFRESH_STATUS_URL
        refresh.click()
        advance_to_poll(1_000)
        expect(title).to_have_text("WeChat account refresh interrupted")
        assert refresh_methods == ["POST"]
        assert status_methods == ["GET"] * 4
        assert page.evaluate("history.state?.agenticContextWechatPendingRefreshV1") is None
        assert opened.errors == []
    finally:
        opened.close()


@pytest.mark.parametrize("width", [1280, 836, 390])
@pytest.mark.parametrize("detail", [False, True])
def test_pending_view_shows_no_messages_false_zero_totals_or_exports(
    disposable_browser, read_server, width, detail
):
    opened = _open_pending(disposable_browser, read_server, width=width, detail=detail)
    page = opened.page
    try:
        metrics = page.locator(".browser-metric-grid")
        expect(metrics).to_have_attribute("aria-hidden", "true")
        expect(metrics.locator(".wechat-read-metric")).to_have_count(3)
        expect(metrics.locator("[data-numeric-display-value]")).to_have_count(0)
        search = page.locator("[data-browser-search-input]")
        expect(search).to_have_attribute("data-browser-search-scope", "session" if detail else "global")
        account = page.locator("#browser_wechat_account")
        expect(account).to_have_value("account:wxid_account_a")
        expect(account.locator("option:checked")).to_contain_text("wxid_account_a")
        expect(account.locator('option[value="account:wxid_account_b"]')).to_have_count(1)
        pending = page.locator("[data-wechat-read-pending]")
        status = pending.get_by_role("status")
        expect(status).to_have_text(
            "Checking saved records before displaying this view.", use_inner_text=True
        )
        expect(pending.locator("[data-wechat-read-title]")).to_be_hidden()
        assert status.evaluate("node => node.closest('[aria-busy]') === null")
        assert metrics.locator(".wechat-read-metric").first.evaluate(
            "node => getComputedStyle(node, '::after').animationName"
        ) == "none"
        if detail:
            expect(page.locator(".browser-wechat-session")).to_have_count(1)
            expect(page.get_by_role("heading", name="WeChat conversation", exact=True)).to_be_visible()
            expect(page.locator("[data-browser-session-tag]")).to_contain_text("This session")
            expect(pending.locator(".wechat-message-list")).to_have_attribute("inert", "")
            expect(pending.locator(".wechat-read-bubble")).to_have_count(6)
            assert pending.locator(".wechat-read-kind").bounding_box()["width"] > 40
            back = page.locator(".wechat-conversation-back").get_attribute("href")
            assert parse_qs(urlsplit(back).query) == {
                "view": ["text"], "source": ["wechat"], "account": ["account:wxid_account_a"],
                "chat_kind": ["group"], "sort": ["oldest"], "page": ["7"],
            }
        else:
            expect(page.get_by_role("heading", name="WeChat conversations", exact=True)).to_be_visible()
            expect(pending.locator(".wechat-conversations")).to_have_attribute("inert", "")
            expect(pending.locator("[data-table-body] tr")).to_have_count(8)
        expect(page.locator("[data-chat-message-id], tr[data-chat-session-id]")).to_have_count(0)
        expect(page.locator("button.wechat-exchange-import-button, .wechat-conversation-share")).to_have_count(0)
        expect(page.get_by_role("link", name="Export conversation as CSV", exact=True)).to_have_count(0)
        expect(page.get_by_text("No cached text found.", exact=True)).to_have_count(0)
        expect(page.get_by_text("No text messages match this search.", exact=True)).to_have_count(0)
        expect(page.get_by_text("No WeChat conversations match these filters.", exact=True)).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert opened.documents == [opened.original_url]
        assert opened.errors == []
    finally:
        opened.close()


def test_pending_read_leaves_search_focus_and_polls_without_a_dialog(
    disposable_browser, read_server
):
    opened = _open_pending(disposable_browser, read_server, width=390)
    page = opened.page
    try:
        search = page.locator("[data-browser-search-input]")
        search.focus()
        _request_next_poll(page)
        _request_next_poll(page)
        expect(search).to_be_focused()
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        expect(page.get_by_role("region", name="Saved chat history")).to_have_attribute("aria-busy", "true")
        assert all(method == "GET" for method, _url in opened.scenario.requests)
        assert not read_server.controlled.phase.release.is_set()
        assert opened.documents == [opened.original_url]
        assert opened.errors == []
    finally:
        opened.close()


def test_ready_reloads_the_exact_url_once_even_if_the_same_task_remains_pending(
    disposable_browser, read_server
):
    opened = _open_pending(disposable_browser, read_server, detail=True)
    page = opened.page
    try:
        page.evaluate("history.replaceState({unrelated: {value: 'preserved'}}, '', location.href)")
        opened.scenario.state = "succeeded"
        with page.expect_navigation(wait_until="domcontentloaded"), page.expect_request(_STATUS_PATTERN):
            _advance_to_next_poll(page)
        expect(page.locator("[data-wechat-read-retry]")).to_be_visible()
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        page.clock.run_for(30_000)
        assert opened.documents == [opened.original_url, opened.original_url]
        assert page.url == opened.original_url
        assert page.evaluate("history.state.unrelated") == {"value": "preserved"}
        assert opened.errors == []
    finally:
        opened.close()


def test_restored_pending_page_resumes_the_inline_skeleton(
    disposable_browser, read_server
):
    opened = _open_pending(disposable_browser, read_server)
    page = opened.page
    try:
        opened.scenario.hold_next = True
        _request_next_poll(page, wait_response=False)
        assert len(opened.scenario.held) == 1
        page.evaluate("""() => {
            window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
            window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
        }""")
        page.wait_for_function("window.__wechatReadProbe.states.length === 2")
        assert page.evaluate("window.__wechatReadProbe.attempts") == 3
        expect(page.get_by_role("region", name="Saved chat history")).to_have_attribute("aria-busy", "true")
        expect(page.locator("#cache_wait_modal")).to_be_hidden()

        # A pre-restore response cannot complete the replacement polling run.
        opened.scenario.state = "succeeded"
        opened.scenario.fulfill(opened.scenario.held.pop())
        page.wait_for_function("window.__wechatReadProbe.states.length === 3")
        assert opened.documents == [opened.original_url]
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        assert page.evaluate("window.__wechatReadProbe.delays") == [250, 250]
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.clock.run_for(250)
        expect(page.locator("[data-wechat-read-retry]")).to_be_visible()
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        page.clock.run_for(30_000)
        assert opened.documents == [opened.original_url, opened.original_url]
        assert page.url == opened.original_url
        assert opened.errors == []
    finally:
        opened.close()


def _assert_conversation_search_axis(page):
    search = page.locator(".browser-search-control").bounding_box()
    theme = page.locator("#global_theme_toggle").bounding_box()
    assert search is not None and theme is not None
    assert search["x"] + search["width"] - search["height"] / 2 == pytest.approx(
        theme["x"] + theme["width"] / 2, abs=1
    )


@pytest.mark.parametrize("width", [1280, 735, 390])
@pytest.mark.parametrize("detail", [False, True])
def test_successful_read_reloads_into_the_same_layout_with_validated_records(
    disposable_browser, read_server, monkeypatch, width, detail
):
    paginated_detail = detail and width == 735
    if paginated_detail:
        read_server.controlled.original = partial(read_server.controlled.original, page_size=1)
        refresh_service = read_server.application.extensions["wechat_mac_refresh"]
        monkeypatch.setattr(refresh_service, "available", lambda *_args: True)
    scenario = ReadStatusScenario()
    scenario.state = None
    opened = _open_pending(disposable_browser, read_server, width=width, detail=detail, scenario=scenario)
    page = opened.page
    result_selector = ".wechat-message-list" if detail else ".wechat-conversations"
    layout = (
        "[data-layout-role=title-rail]", ".browser-content-toolbar",
        ".browser-search-control", ".browser-metric-grid", result_selector,
    )

    try:
        # Fallback fonts can change the normal line height of metric labels.
        page.evaluate("document.fonts.ready")
        if detail:
            _assert_conversation_search_axis(page)
        pending_boxes = [page.locator(selector).bounding_box() for selector in layout]
        _finish_pending_read(page, read_server)
        expect(page.locator(".browser-metric-grid [data-numeric-display-value]")).to_have_count(3)
        records = page.locator("[data-chat-message-id]" if detail else "[data-chat-session-id]")
        expect(records.first).to_be_visible()
        expect(page.locator("[data-wechat-conversation-refresh]")).to_have_count(
            1 if paginated_detail else 0
        )
        expect(page.get_by_role("navigation", name="Message pages")).to_have_count(
            1 if paginated_detail else 0
        )
        page.evaluate("document.fonts.ready")
        if detail:
            _assert_conversation_search_axis(page)
        for selector, pending in zip(layout, pending_boxes, strict=True):
            ready = page.locator(selector).bounding_box()
            assert pending is not None and ready is not None, selector
            assert ready["x"] == pytest.approx(pending["x"], abs=1), selector
            assert ready["width"] == pytest.approx(pending["width"], abs=1), selector
            if selector != result_selector or (detail and width == 735):
                assert ready["y"] == pytest.approx(pending["y"], abs=1), selector
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        # A same-URL reload can expose parsed body content before deferred scripts finish.
        page.wait_for_function("window.__wechatReadProbe.domContentLoaded")
        assert page.evaluate("history.state?.agenticContextWechatReadReloadV1 || null") is None, {
            "errors": opened.errors,
            "lifecycle": page.evaluate("window.__wechatReadProbe"),
            "ready_state": page.evaluate("document.readyState"),
        }
        page.clock.run_for(30_000)
        assert opened.documents == [opened.original_url, opened.original_url]
        assert page.url == opened.original_url
        assert opened.errors == []
    finally:
        opened.close()


@pytest.mark.parametrize("read_server", [101], indirect=True)
def test_manual_reload_of_ready_conversation_keeps_messages_without_loading_again(disposable_browser, read_server):
    scenario = ReadStatusScenario()
    scenario.state = None
    opened = _open_pending(disposable_browser, read_server, detail=True, scenario=scenario)
    page = opened.page
    try:
        _finish_pending_read(page, read_server)
        messages = page.locator("[data-chat-message-id]")
        expect(messages.first).to_be_visible()
        message_ids = messages.evaluate_all("rows => rows.map(row => row.dataset.chatMessageId)")
        blocked = read_server.controlled.reset()
        for _ in range(2):
            page.reload(wait_until="domcontentloaded")
            assert page.url == opened.original_url
            expect(page.locator("[data-wechat-read-pending]")).to_have_count(0)
            expect(messages.first).to_be_visible()
            assert messages.evaluate_all("rows => rows.map(row => row.dataset.chatMessageId)") == message_ids
        assert not blocked.entered.is_set()
        assert opened.documents == [opened.original_url] * 4
    finally:
        opened.close()


@pytest.mark.parametrize("read_server", [101], indirect=True)
@pytest.mark.parametrize(("width", "size"), [(626, 30), (390, 28), (320, 24)])
def test_pending_pagination_matches_ready_page_button_geometry(
    disposable_browser, read_server, width, size
):
    scenario = ReadStatusScenario()
    scenario.state = None
    opened = _open_pending(disposable_browser, read_server, width=width, detail=True, scenario=scenario)
    page = opened.page
    geometry = """nodes => nodes.map(node => {
        const {width, height} = node.getBoundingClientRect();
        const {borderRadius, cornerShape} = getComputedStyle(node);
        return {width, height, borderRadius, cornerShape};
    })"""
    try:
        pending = page.locator(".wechat-read-pagination")
        expect(pending).to_be_visible()
        expect(pending).to_have_attribute("aria-hidden", "true")
        pending_box = pending.bounding_box()
        pending_slots = pending.locator(".navigation-skeleton-line").evaluate_all(geometry)
        assert pending_slots

        _finish_pending_read(page, read_server)
        ready = page.get_by_role("navigation", name="Message pages", exact=True)
        expect(ready.get_by_role("link", name="Message page 2", exact=True)).to_have_attribute(
            "aria-current", "page"
        )
        ready_box = ready.bounding_box()
        ready_slots = ready.locator(".local-store-page-button")
        expect(ready_slots).to_have_count(2)
        ready_shapes = ready_slots.evaluate_all(geometry)
        assert all(slot == ready_shapes[0] for slot in pending_slots)
        assert ready_shapes[0]["width"] == ready_shapes[0]["height"] == size
        assert ready_shapes[0]["borderRadius"] == "50%"
        assert pending_box["y"] == pytest.approx(ready_box["y"], abs=1)
        assert pending_box["height"] == ready_box["height"]
        assert pending_box["x"] + pending_box["width"] / 2 == pytest.approx(
            ready_box["x"] + ready_box["width"] / 2, abs=1
        )
        assert opened.errors == []
    finally:
        opened.close()


@pytest.mark.parametrize("detail", [False, True])
def test_failed_read_requires_manual_retry_and_then_starts_a_fresh_task(
    disposable_browser, read_server, detail
):
    scenario = ReadStatusScenario()
    scenario.state = None
    opened = _open_pending(
        disposable_browser, read_server, width=735 if detail else 1280,
        detail=detail, scenario=scenario,
    )
    page = opened.page
    try:
        original_task = page.locator("[data-wechat-read-pending]").get_attribute("data-task-id")
        read_server.controlled.phase.fail = True
        read_server.controlled.phase.release.set()
        for _ in range(5):
            if page.locator("[data-wechat-read-retry]").is_visible():
                break
            _request_next_poll(page)
        expect(page.locator("[data-wechat-read-retry]")).to_be_visible()
        expect(page.locator("[data-wechat-read-pending]")).to_have_attribute("data-state", "failed")
        if detail:
            _assert_conversation_search_axis(page)
        expect(page.locator("[data-wechat-read-placeholder]:not([hidden])")).to_have_count(0)
        expect(page.locator("[data-wechat-read-pending] [aria-busy=true]")).to_have_count(0)
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        assert opened.documents == [opened.original_url]
        assert "Synthetic private source failure" not in page.locator("body").inner_text()
        page.evaluate("""() => {
            window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
            window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
        }""")
        next_phase = read_server.controlled.reset()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator("[data-wechat-read-retry]").click()
        expect(page.get_by_role("region", name="Saved chat history")).to_have_attribute("aria-busy", "true")
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        expect(page.locator("[data-wechat-read-pending]")).not_to_have_attribute(
            "data-task-id", original_task
        )
        assert next_phase.entered.wait(1), "The retried saved-view loader did not start."
        assert len([method for method, _url in scenario.requests if method == "POST"]) == 1
        assert opened.documents == [opened.original_url, opened.original_url]
        assert opened.errors == []
    finally:
        opened.close()


def test_ready_read_yields_to_committed_conversation_back_navigation(
    disposable_browser, read_server
):
    opened = _open_pending(disposable_browser, read_server, detail=True)
    page = opened.page
    try:
        opened.scenario.hold_next = True
        _request_next_poll(page, wait_response=False)
        assert len(opened.scenario.held) == 1
        back = page.locator(".wechat-conversation-back")
        destination = back.evaluate("link => link.href")
        back.evaluate("link => link.click()")
        expect(page.locator("html")).to_have_attribute("data-navigation-target", "browser-text")
        opened.scenario.state = "succeeded"
        opened.scenario.fulfill(opened.scenario.held.pop())
        page.wait_for_function("window.__wechatReadProbe.states.includes('succeeded')")
        assert page.evaluate("history.state?.agenticContextWechatReadReloadV1 || null") is None
        assert opened.documents == [opened.original_url]
        opened.scenario.state = "running"
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.clock.run_for(120)
        assert opened.documents == [opened.original_url, destination]
        assert page.url == destination
        assert opened.errors == []
    finally:
        opened.close()


def test_ready_response_for_an_old_url_cannot_reload_the_new_url(
    disposable_browser, read_server
):
    opened = _open_pending(disposable_browser, read_server)
    page = opened.page
    try:
        opened.scenario.hold_next = True
        _request_next_poll(page, wait_response=False)
        assert len(opened.scenario.held) == 1
        new_url = opened.original_url + "&read_test_navigation=changed"
        page.evaluate("url => history.replaceState(history.state, '', url)", new_url)
        opened.scenario.state = "succeeded"
        opened.scenario.fulfill(opened.scenario.held.pop())
        page.wait_for_function("window.__wechatReadProbe.states.includes('succeeded')")
        page.clock.run_for(30_000)
        assert page.url == new_url
        assert opened.documents == [opened.original_url]
        assert opened.errors == []
    finally:
        opened.close()


@pytest.mark.parametrize("mismatch", ["task", "generation"])
def test_mismatched_status_coordinates_cannot_trigger_a_ready_reload(
    disposable_browser, read_server, mismatch
):
    opened = _open_pending(disposable_browser, read_server)
    page = opened.page
    try:
        opened.scenario.state = "succeeded"
        opened.scenario.mismatch = mismatch
        _request_next_poll(page)
        expect(page.locator("[data-wechat-read-retry]")).to_be_visible()
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        page.clock.run_for(30_000)
        assert opened.documents == [opened.original_url]
        assert opened.errors == []
    finally:
        opened.close()


def test_status_transport_failure_budget_stops_polling_without_a_reload(
    disposable_browser, read_server
):
    scenario = ReadStatusScenario()
    scenario.status = 503
    opened = _open_pending(disposable_browser, read_server, scenario=scenario)
    page = opened.page
    try:
        retry = page.locator("[data-wechat-read-retry]")
        for _ in range(4):
            _request_next_poll(page)
        expect(retry).to_be_hidden()
        assert len(scenario.requests) == 5
        with page.expect_response(_STATUS_PATTERN):
            page.clock.fast_forward(30_000)
        expect(retry).to_be_visible()
        expect(page.locator("#cache_wait_modal")).to_be_hidden()
        assert len(scenario.requests) == 6
        page.clock.run_for(30_000)
        assert len(scenario.requests) == 6
        assert opened.documents == [opened.original_url]
        assert opened.errors == []
    finally:
        opened.close()


def test_expired_saved_view_retries_the_exact_document_without_posting_the_old_task(
    disposable_browser, read_server
):
    opened = _open_pending(disposable_browser, read_server, detail=True)
    page, scenario = opened.page, opened.scenario
    try:
        scenario.status = 404
        _request_next_poll(page)
        retry = page.locator("[data-wechat-read-retry]")
        expect(retry).to_be_visible()
        expect(page.locator("[data-wechat-read-copy]")).to_contain_text("The saved-view task expired.")
        assert [method for method, _url in scenario.requests] == ["GET", "GET"]
        assert opened.documents == [opened.original_url]
        scenario.status = None
        with page.expect_navigation(wait_until="domcontentloaded") as navigation:
            retry.click()
        assert navigation.value.request.method == "GET"
        assert opened.documents == [opened.original_url, opened.original_url]
        assert all(method == "GET" for method, _url in scenario.requests)
        assert opened.errors == []
    finally:
        opened.close()
