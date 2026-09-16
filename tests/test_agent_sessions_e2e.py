"""Session switching, capacity, and selected controls. Code version: v1.11.0-codex.1."""

import re
from copy import deepcopy
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests import test_sidebar_e2e as fixtures
from app.core.browser_sessions import grok_composer_snapshot
from app.web.app import render_agent_response

disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


@pytest.fixture()
def agent_selection_server_url(tmp_path):
    """Serve one isolated Agent settings store for persistence interaction tests."""
    from app.web.app import create_app

    application = create_app(
        tmp_path / "local-store",
        computer_use_settings_path=tmp_path / "settings" / "computer-use-agent.json",
        computer_use_runtime_root=tmp_path / "computer-use-runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def test_grok_composer_snapshot_uses_one_rendered_provider_scoped_surface(
    disposable_browser,
):
    context = disposable_browser.new_context(viewport={"width": 900, "height": 700})
    page = context.new_page()
    try:
        page.set_content(
            """
            <main>
                <textarea style="display:none" aria-label="Ask Grok anything"></textarea>
                <section id="primary-composer">
                    <div contenteditable="true" role="textbox"
                         aria-label="Ask Grok anything"></div>
                    <button data-testid="chat-submit" aria-label="Send"></button>
                </section>
                <section class="feedback-panel">
                    <div contenteditable="true" role="textbox"
                         aria-label="Ask Grok anything"></div>
                </section>
            </main>
            """
        )
        assert grok_composer_snapshot(page) == {"count": 1}
        page.locator("main").evaluate(
            """main => {
                const duplicate = document.createElement('textarea');
                duplicate.setAttribute('aria-label', 'Ask Grok anything');
                main.append(duplicate);
            }"""
        )
        assert grok_composer_snapshot(page) == {"count": 2}
    finally:
        context.close()


@pytest.mark.parametrize(
    "viewport",
    (
        {"width": 1_007, "height": 1_497},
        {"width": 390, "height": 844},
    ),
    ids=("annotated-desktop", "narrow"),
)
def test_safari_selection_persists_for_source_only_providers_without_edge_fallback(
    disposable_browser,
    agent_selection_server_url,
    viewport,
):
    context = disposable_browser.new_context(viewport=viewport)
    page = context.new_page()
    browser_status_requests = []
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.add_init_script(
        """(() => {
            window.__agentRouteWrites = [];
            const originalReplaceState = history.replaceState.bind(history);
            history.replaceState = (...args) => {
                window.__agentRouteWrites.push(String(args[2] || ''));
                return originalReplaceState(...args);
            };
        })()"""
    )
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)

    def fulfill_agent_status(route):
        headers = route.request.headers
        platform = headers.get("x-cachelikes-agent-platform", "chatgpt")
        browser = headers.get("x-cachelikes-agent-browser", "edge")
        route.fulfill(
            json={
                **base,
                "agent": {
                    "session_id": headers.get("x-cachelikes-agent-session", "new"),
                    "platform": platform,
                    "browser": browser,
                    "running": False,
                    "phase": "idle",
                    "history": [],
                },
                "sessions": [],
                "active_count": 0,
                "can_start": True,
            }
        )

    def fulfill_browser_status(route):
        query = parse_qs(urlsplit(route.request.url).query)
        platform = query.get("platform", [""])[0]
        browser = query.get("browser", [""])[0]
        platform_label = {
            "chatgpt": "ChatGPT",
            "gemini": "Gemini",
            "grok": "Grok",
            "claude": "Claude",
        }[platform]
        conversation_url = {
            "chatgpt": "https://chatgpt.com/c/safari-recent",
            "gemini": "https://gemini.google.com/app/safari-recent",
            "grok": "https://grok.com/c/safari-recent",
            "claude": "https://claude.ai/chat/safari-recent",
        }[platform]
        browser_status_requests.append((platform, browser))
        route.fulfill(
            json={
                "platform": platform,
                "browser": browser,
                "browser_label": browser.title(),
                "logged_in": True,
                "can_download": True,
                "account_name": "Signed in",
                "message": "Ready",
                "agent_sources": {
                    "recent_sessions": [{
                        "id": "safari-recent",
                        "title": f"{platform_label} Safari recent session",
                        "url": conversation_url,
                        "updated_at": "2026-09-14T00:00:00Z",
                    }],
                    "projects": [],
                },
                "agent_execution_supported": (
                    browser != "safari" or platform in {"chatgpt", "grok"}
                ),
                "agent_execution_message": (
                    ""
                    if browser != "safari" or platform in {"chatgpt", "grok"}
                    else f"Safari can browse {platform.title()} Recent sessions here."
                ),
            }
        )

    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(
            json={"platform": "grok", "recent_sessions": [], "projects": []}
        ),
    )

    def ensure_sidebar_open():
        if viewport["width"] > 900:
            return
        shell = page.locator(".app-shell")
        if "is-sidebar-open" not in (shell.get_attribute("class") or "").split():
            page.get_by_role("button", name="Toggle sidebar", exact=True).click()
            expect(shell).to_have_class(re.compile(r"\bis-sidebar-open\b"))

    def ensure_sidebar_closed():
        if viewport["width"] > 900:
            return
        shell = page.locator(".app-shell")
        if "is-sidebar-open" in (shell.get_attribute("class") or "").split():
            page.get_by_role("button", name="Toggle sidebar", exact=True).click()
            expect(shell).to_have_class(re.compile(r"\bis-sidebar-collapsed\b"))

    try:
        page.goto(f"{agent_selection_server_url}/agent/edge/chatgpt")
        ensure_sidebar_open()
        page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
        page.get_by_role("option", name="Grok", exact=True).click()
        page.get_by_role("button", name="Browser: Edge", exact=True).click()
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("platform") == "grok"
            and response.request.post_data_json.get("browser") == "safari"
        ) as preference_response:
            page.get_by_role("option", name="Safari", exact=True).click()

        saved_payload = preference_response.value.request.post_data_json
        assert saved_payload["platform"] == "grok"
        assert saved_payload["browser"] == "safari"
        assert saved_payload["model"] == "grok-build"
        assert saved_payload["preference_client_id"]
        assert saved_payload["preference_revision"] >= 1
        assert not page_errors
        assert page.evaluate(
            """() => ({
                platform: document.querySelector('.agent-platform-combobox input')?.value,
                browser: document.querySelector('.agent-browser-combobox input')?.value,
                path: location.pathname,
                routeWrites: window.__agentRouteWrites,
            })"""
        ) == {
            "platform": "grok",
            "browser": "safari",
            "path": "/agent/safari/grok",
            "routeWrites": ["/agent/edge/grok", "/agent/safari/grok"],
        }
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/safari/grok")
        expect(
            page.locator('#agent_runtime_form input[name="platform"]')
        ).to_have_value("grok")
        expect(page.locator('#agent_runtime_form input[name="browser"]')).to_have_value(
            "safari"
        )
        expect(page.locator(".agent-execution-session-title")).to_have_text(
            "Grok Safari recent session"
        )

        ensure_sidebar_closed()
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("platform") == "grok"
            and response.request.post_data_json.get("browser") == "safari"
            and response.request.post_data_json.get("model") == "grok-auto"
        ) as model_preference_response:
            page.get_by_role("button", name="Model: Build", exact=True).click()
            page.get_by_role("option", name="Grok · Auto", exact=True).click()

        model_payload = model_preference_response.value.request.post_data_json
        assert model_payload["model"] == "grok-auto"
        assert model_payload["preference_client_id"] == saved_payload[
            "preference_client_id"
        ]
        assert model_payload["preference_revision"] > saved_payload[
            "preference_revision"
        ]
        expect(page.get_by_role("button", name="Model: Auto", exact=True)).to_be_visible()

        page.reload(wait_until="domcontentloaded")
        ensure_sidebar_open()
        expect(page.get_by_role("button", name="Web service: Grok", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Browser: Safari", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Model: Auto", exact=True)).to_be_visible()
        page.goto(f"{agent_selection_server_url}/agent", wait_until="domcontentloaded")
        ensure_sidebar_open()
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/safari/grok")

        page.wait_for_function(
            "() => document.querySelector('[data-role=browser-session-account]')?.textContent === 'Signed in'"
        )
        browser_status_requests.clear()
        page.get_by_role("button", name="Web service: Grok", exact=True).click()
        with page.expect_response(
            lambda response: "/api/browser-session?" in response.url
            and "platform=gemini" in response.url
            and "browser=safari" in response.url
        ), page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("platform") == "gemini"
            and response.request.post_data_json.get("browser") == "safari"
        ) as reloaded_preference_response:
            page.get_by_role("option", name="Gemini", exact=True).click()
        reloaded_payload = reloaded_preference_response.value.request.post_data_json
        assert reloaded_payload["preference_client_id"] == saved_payload[
            "preference_client_id"
        ]
        assert reloaded_payload["preference_revision"] > saved_payload[
            "preference_revision"
        ]
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/safari/gemini")
        expect(page.get_by_role("button", name="Browser: Safari", exact=True)).to_be_visible()
        expect(page.locator("#agent_ask_button")).to_be_disabled()
        expect(page.locator(".agent-execution-session-title")).to_have_text(
            "Gemini Safari recent session"
        )
        assert "Safari can browse Gemini Recent sessions here" in (
            page.locator("#agent_ask_button").get_attribute("title") or ""
        )
        assert ("gemini", "safari") in browser_status_requests
        assert ("gemini", "edge") not in browser_status_requests

        browser_status_requests.clear()
        page.get_by_role("button", name="Web service: Gemini", exact=True).click()
        with page.expect_response(
            lambda response: "/api/browser-session?" in response.url
            and "platform=claude" in response.url
            and "browser=safari" in response.url
        ), page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("platform") == "claude"
            and response.request.post_data_json.get("browser") == "safari"
        ):
            page.get_by_role("option", name="Claude", exact=True).click()
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/safari/claude")
        expect(page.get_by_role("button", name="Browser: Safari", exact=True)).to_be_visible()
        expect(page.locator("#agent_ask_button")).to_be_disabled()
        expect(page.locator(".agent-execution-session-title")).to_have_text(
            "Claude Safari recent session"
        )
        assert ("claude", "safari") in browser_status_requests
        assert ("claude", "edge") not in browser_status_requests

        browser_status_requests.clear()
        page.get_by_role("button", name="Web service: Claude", exact=True).click()
        with page.expect_response(
            lambda response: "/api/browser-session?" in response.url
            and "platform=chatgpt" in response.url
            and "browser=safari" in response.url
        ), page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("platform") == "chatgpt"
            and response.request.post_data_json.get("browser") == "safari"
        ):
            page.get_by_role("option", name="ChatGPT", exact=True).click()
        expect(page).to_have_url(f"{agent_selection_server_url}/agent/safari/chatgpt")
        expect(page.locator("#agent_ask_button")).to_be_enabled()
        expect(page.locator(".agent-execution-session-title")).to_have_text(
            "ChatGPT Safari recent session"
        )
        assert ("chatgpt", "safari") in browser_status_requests
        assert ("chatgpt", "edge") not in browser_status_requests
    finally:
        context.close()


def test_agent_preference_save_retries_an_unchanged_failed_payload(
    disposable_browser,
    agent_selection_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    requests = []
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)

    def fulfill_agent_status(route):
        headers = route.request.headers
        route.fulfill(
            json={
                **base,
                "agent": {
                    "session_id": "new",
                    "platform": headers.get("x-cachelikes-agent-platform", "chatgpt"),
                    "browser": headers.get("x-cachelikes-agent-browser", "edge"),
                    "running": False,
                    "phase": "idle",
                    "history": [],
                },
                "sessions": [],
                "active_count": 0,
                "can_start": True,
            }
        )

    def fulfill_preference(route):
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            route.fulfill(status=503, json={"error": "Temporary failure"})
            return
        route.fulfill(json={"settings": {}, "runtime": {}})

    page.route("**/api/agent/status", fulfill_agent_status)
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "platform": "grok",
                "browser": "safari",
                "browser_label": "Safari",
                "logged_in": True,
                "can_download": True,
                "account_name": "Signed in",
                "message": "Ready",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        ),
    )
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(
            json={"platform": "grok", "recent_sessions": [], "projects": []}
        ),
    )
    page.route("**/api/agent/preferences", fulfill_preference)
    try:
        page.goto(f"{agent_selection_server_url}/agent/edge/chatgpt")
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
        ):
            page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
            page.get_by_role("option", name="Grok", exact=True).click()
        page.wait_for_function("() => window.location.pathname === '/agent/edge/grok'")
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
        ):
            page.wait_for_timeout(1_100)
        assert len(requests) >= 2
        assert requests[1] == requests[0]
        assert requests[1]["platform"] == "grok"
        assert requests[1]["browser"] == "edge"
    finally:
        context.close()


def test_agent_preference_save_newer_selection_supersedes_failed_payload(
    disposable_browser,
    agent_selection_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    requests = []
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)

    def fulfill_agent_status(route):
        headers = route.request.headers
        route.fulfill(
            json={
                **base,
                "agent": {
                    "session_id": "new",
                    "platform": headers.get("x-cachelikes-agent-platform", "chatgpt"),
                    "browser": headers.get("x-cachelikes-agent-browser", "edge"),
                    "running": False,
                    "phase": "idle",
                    "history": [],
                },
                "sessions": [],
                "active_count": 0,
                "can_start": True,
            }
        )

    def fulfill_preference(route):
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            route.fulfill(status=503, json={"error": "Temporary failure"})
            return
        route.fulfill(json={"settings": {}, "runtime": {}})

    page.route("**/api/agent/status", fulfill_agent_status)
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "platform": "grok",
                "browser": "safari",
                "browser_label": "Safari",
                "logged_in": True,
                "can_download": True,
                "account_name": "Signed in",
                "message": "Ready",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        ),
    )
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(
            json={"platform": "grok", "recent_sessions": [], "projects": []}
        ),
    )
    page.route("**/api/agent/preferences", fulfill_preference)
    try:
        page.goto(f"{agent_selection_server_url}/agent/edge/chatgpt")
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
        ):
            page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
            page.get_by_role("option", name="Grok", exact=True).click()
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent/preferences")
            and response.request.method == "POST"
            and response.request.post_data_json.get("browser") == "safari"
        ):
            page.get_by_role("button", name="Browser: Edge", exact=True).click()
            page.get_by_role("option", name="Safari", exact=True).click()
        page.wait_for_function("() => window.location.pathname === '/agent/safari/grok'")
        assert len(requests) >= 2
        assert requests[0]["platform"] == "grok"
        assert requests[0]["browser"] == "edge"
        assert requests[1]["platform"] == "grok"
        assert requests[1]["browser"] == "safari"
        assert requests[1]["preference_revision"] > requests[0]["preference_revision"]
    finally:
        context.close()


def test_agent_preference_outbox_restores_selection_and_replays_on_first_reload(
    disposable_browser,
    agent_selection_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    requests = []
    status_selections = []
    browser_selections = []
    base = fixtures._finished_chatgpt_agent_payload()
    base.update(agent={"session_id": "new"}, sessions=[], active_count=0, can_start=True)

    def fulfill_status(route):
        headers = route.request.headers
        status_selections.append(
            (
                headers.get("x-cachelikes-agent-platform"),
                headers.get("x-cachelikes-agent-browser"),
            )
        )
        route.fulfill(json=base)

    def fulfill_browser_status(route):
        query = parse_qs(urlsplit(route.request.url).query)
        browser_selections.append(
            (query.get("platform", [""])[0], query.get("browser", [""])[0])
        )
        route.fulfill(
            json={
                "platform": "grok",
                "browser": "safari",
                "browser_label": "Safari",
                "logged_in": True,
                "can_download": True,
                "account_name": "Signed in",
                "message": "Ready",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        )

    page.route("**/api/agent/status", fulfill_status)
    page.route(
        "**/api/browser-session**",
        fulfill_browser_status,
    )
    page.route(
        "**/api/agent/preferences",
        lambda route: (
            requests.append(route.request.post_data_json),
            route.fulfill(json={"settings": {}, "runtime": {}}),
        ),
    )
    try:
        page.goto(f"{agent_selection_server_url}/agent/edge/chatgpt")
        payload = page.evaluate(
            """() => {
                const payload = {
                    workspace_path: document.querySelector('[name="workspace_path"]').value,
                    operating_system: 'macos',
                    platform: 'grok',
                    browser: 'safari',
                    model: 'grok-build',
                    chatgpt_effort: 'highest_available',
                    preference_client_id: 'outbox-reload-client',
                    preference_revision: 41,
                };
                sessionStorage.setItem(
                    'cachelikes:agent-preference-client-v1',
                    payload.preference_client_id,
                );
                sessionStorage.setItem(
                    'cachelikes:agent-preference-revision-v1',
                    String(payload.preference_revision),
                );
                sessionStorage.setItem(
                    'cachelikes:agent-preference-pending-v1',
                    JSON.stringify(payload),
                );
                return payload;
            }"""
        )

        status_selections.clear()
        browser_selections.clear()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => window.location.pathname === '/agent/safari/grok'")
        expect(page.get_by_role("button", name="Web service: Grok", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Browser: Safari", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Model: Build", exact=True)).to_be_visible()
        page.wait_for_function("() => sessionStorage.getItem('cachelikes:agent-preference-pending-v1') === null")
        assert requests == [payload]
        assert status_selections[0] == ("grok", "safari")
        assert browser_selections[0] == ("grok", "safari")
    finally:
        context.close()


@pytest.mark.parametrize(
    "invalid_model",
    ("grok-retired", "latest_available"),
    ids=("removed-model", "wrong-platform-model"),
)
def test_agent_preference_outbox_discards_invalid_model_without_restoring_or_posting(
    disposable_browser,
    agent_selection_server_url,
    invalid_model,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    requests = []
    base = fixtures._finished_chatgpt_agent_payload()
    base.update(agent={"session_id": "new"}, sessions=[], active_count=0, can_start=True)
    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "platform": "chatgpt",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "Signed in",
                "message": "Ready",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        ),
    )
    page.route(
        "**/api/agent/preferences",
        lambda route: (
            requests.append(route.request.post_data_json),
            route.fulfill(status=409, json={"error": "Choose a supported Grok model."}),
        ),
    )
    try:
        page.goto(f"{agent_selection_server_url}/agent/edge/chatgpt")
        baseline = page.evaluate(
            """() => ({
                workspace_path: document.querySelector('[name="workspace_path"]').value,
                platform: document.querySelector('.agent-platform-combobox input').value,
                browser: document.querySelector('.agent-browser-combobox input').value,
                model: document.querySelector('[data-agent-model-input]').value,
            })"""
        )
        page.evaluate(
            """({workspacePath, model}) => {
                const payload = {
                    workspace_path: workspacePath,
                    operating_system: 'macos',
                    platform: 'grok',
                    browser: 'safari',
                    model,
                    chatgpt_effort: 'highest_available',
                    preference_client_id: 'invalid-outbox-client',
                    preference_revision: 51,
                };
                sessionStorage.setItem(
                    'cachelikes:agent-preference-client-v1',
                    payload.preference_client_id,
                );
                sessionStorage.setItem(
                    'cachelikes:agent-preference-revision-v1',
                    String(payload.preference_revision),
                );
                sessionStorage.setItem(
                    'cachelikes:agent-preference-pending-v1',
                    JSON.stringify(payload),
                );
            }""",
            {"workspacePath": baseline["workspace_path"], "model": invalid_model},
        )

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "() => sessionStorage.getItem('cachelikes:agent-preference-pending-v1') === null"
        )
        page.wait_for_timeout(1_250)
        assert requests == []
        assert page.evaluate(
            """() => ({
                platform: document.querySelector('.agent-platform-combobox input').value,
                browser: document.querySelector('.agent-browser-combobox input').value,
                model: document.querySelector('[data-agent-model-input]').value,
                path: window.location.pathname,
            })"""
        ) == {
            "platform": baseline["platform"],
            "browser": baseline["browser"],
            "model": baseline["model"],
            "path": "/agent/edge/chatgpt",
        }
    finally:
        context.close()


def test_removed_remembered_project_falls_back_to_new_without_request_loop(
    disposable_browser,
    sidebar_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.update(agent={"session_id": "new"}, sessions=[], active_count=0, can_start=True)
    project_session_requests = []
    page.add_init_script(
        """localStorage.setItem(
            'cachelikes:agent-session-selection:v1:chatgpt:edge',
            JSON.stringify({
                version: 1,
                mode: 'project',
                project_url: 'https://chatgpt.com/g/removed-project',
                project_session_url: 'https://chatgpt.com/c/removed-session',
            }),
        );"""
    )
    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "can_download": True,
                "logged_in": True,
                "browser": "edge",
                "platform": "chatgpt",
                "account_name": "Signed in",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        ),
    )
    page.route(
        "**/api/agent/project-sessions**",
        lambda route: (
            project_session_requests.append(route.request.url),
            route.fulfill(json={"recent_sessions": []}),
        ),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(
            page.get_by_role("button", name="Session source: New session", exact=True)
        ).to_be_visible()
        expect(page.get_by_role("button", name="Ask ChatGPT Web", exact=True)).to_be_enabled()
        remembered = page.evaluate(
            """JSON.parse(localStorage.getItem(
                'cachelikes:agent-session-selection:v1:chatgpt:edge'
            ))"""
        )
        assert remembered == {
            "version": 1,
            "mode": "new",
            "project_url": "",
            "project_session_url": "new",
        }
        page.reload(wait_until="domcontentloaded")
        expect(
            page.get_by_role("button", name="Session source: New session", exact=True)
        ).to_be_visible()
        expect(page.get_by_role("button", name="Ask ChatGPT Web", exact=True)).to_be_enabled()
        assert project_session_requests == []
    finally:
        context.close()


def test_force_recheck_bypasses_hanging_cached_request_and_keeps_newest_result(
    disposable_browser,
    sidebar_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1_160, "height": 900})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.update(agent={"session_id": "new"}, sessions=[], active_count=0, can_start=True)
    held_requests = []
    source_requests = []

    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route("**/api/browser-session**", lambda route: held_requests.append(route))
    page.route(
        "**/api/agent/sources**",
        lambda route: (
            source_requests.append(route.request.url),
            route.abort(),
        ),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/grok")
        for _ in range(100):
            if len(held_requests) == 1:
                break
            page.wait_for_timeout(20)
        assert len(held_requests) == 1
        assert "refresh=1" not in held_requests[0].request.url

        page.locator('[data-role="browser-session-recheck"]').dispatch_event("click")
        for _ in range(100):
            if len(held_requests) == 2:
                break
            page.wait_for_timeout(20)
        assert len(held_requests) == 2
        assert "refresh=1" in held_requests[1].request.url

        held_requests[1].fulfill(
            json={
                "can_download": True,
                "logged_in": True,
                "browser": "edge",
                "platform": "grok",
                "account_name": "Fresh User",
                "message": "Fresh readiness",
                "agent_sources": {
                    "recent_sessions": [],
                    "projects": [
                        {"title": "Fresh Project", "url": "https://grok.com/project/fresh"}
                    ],
                },
            }
        )
        account = page.locator('[data-role="browser-session-account"]')
        expect(account).to_have_text("Fresh User")
        expect(
            page.locator(
                '[data-agent-session-list="projects"] '
                '[data-agent-combobox-option="https://grok.com/project/fresh"]'
            )
        ).to_have_count(1)

        held_requests[0].fulfill(
            json={
                "can_download": False,
                "logged_in": False,
                "browser": "edge",
                "platform": "grok",
                "account_name": "",
                "message": "Old readiness",
                "agent_sources": {
                    "recent_sessions": [],
                    "projects": [
                        {"title": "Old Project", "url": "https://grok.com/project/old"}
                    ],
                },
            }
        )
        page.wait_for_timeout(100)
        expect(account).to_have_text("Fresh User")
        expect(
            page.locator(
                '[data-agent-session-list="projects"] '
                '[data-agent-combobox-option="https://grok.com/project/fresh"]'
            )
        ).to_have_count(1)
        expect(
            page.locator(
                '[data-agent-session-list="projects"] '
                '[data-agent-combobox-option="https://grok.com/project/old"]'
            )
        ).to_have_count(0)
        assert source_requests == []
    finally:
        context.close()


@pytest.mark.parametrize(("width", "color_scheme"), [(1024, "light"), (390, "dark")])
def test_agent_response_renders_latex_without_treating_currency_as_math(
    disposable_browser, sidebar_server_url, width, color_scheme,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 959},
        color_scheme=color_scheme,
    )
    page = context.new_page()
    raw_response = (
        "Ordinary currency stays text: $36,646.96\n\n"
        r"\[$14,523.85-\text{已售成本}=$1,755.92\]"
        "\n\n"
        r"Inline \(x^2 + y^2\) and `\[literal code\]`."
    )
    base = fixtures._finished_chatgpt_agent_payload()
    base["agent"].update(
        response=raw_response,
        response_html=str(render_agent_response(raw_response)),
        history=[],
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": {"recent_sessions": [], "projects": []},
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        answer = page.locator("#agent_response_answer")
        expect(answer.locator(".katex-display .katex")).to_have_count(1)
        expect(answer.locator(".katex")).to_have_count(2)
        expect(answer.locator(".katex-mathml math")).to_have_count(2)
        expect(answer.locator(".katex-display .katex-html")).to_contain_text("已售成本")
        expect(answer.locator("code")).to_have_text(r"\[literal code\]")
        expect(answer).to_contain_text("Ordinary currency stays text: $36,646.96")
        assert answer.locator(".katex-error").count() == 0
        geometry = answer.locator(".katex-display").evaluate(
            "element => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth})",
        )
        assert geometry["clientWidth"] <= answer.evaluate("element => element.clientWidth")
        assert geometry["scrollWidth"] >= geometry["clientWidth"]
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize(
    ("width", "height"),
    ((1_007, 1_355), (390, 844)),
)
def test_grok_response_renders_native_markdown_without_provider_markup_leaks(
    disposable_browser,
    sidebar_server_url,
    width,
    height,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme="dark",
        reduced_motion="reduce",
    )
    page = context.new_page()
    raw_response = (
        "**Evidence-ranked longevity options**\n\n"
        "### 1. Rapamycin / Sirolimus\n\n"
        "- **Original use**: transplant immunosuppression.\n"
        "- **Evidence**: animal lifespan results are stronger than human outcomes. "
        '<grok:render card_id="066a7b" card_type="citation_card" '
        'type="render_inline_citation"><argument name="citation_id">81</argument>'
        "</grok:render>\n\n"
        "### 2. Evidence matrix\n\n"
        "| Drug | Original indication | Longevity discussion | Evidence and safety notes |\n"
        "| --- | --- | --- | --- |\n"
        "| Rapamycin | Transplant immunosuppression | Intermittent mTORC1 inhibition | "
        "Human lifespan endpoints remain unproven |\n"
        "| SGLT2 inhibitor | Type 2 diabetes | Cardiometabolic risk reduction | "
        "Benefits come from indicated clinical populations |"
    )
    citations = [
        {
            "card_id": "066a7b",
            "citation_id": "81",
            "url": "https://example.com/source",
            "label": "Example",
        }
    ]
    base = fixtures._finished_chatgpt_agent_payload()
    base["agent"].update(
        platform="grok",
        browser="edge",
        model="grok-build",
        actual_model="Grok Build",
        prompt="Compare longevity prescriptions.",
        response=raw_response,
        response_html=str(
            render_agent_response(
                raw_response,
                provider="grok",
                citations=citations,
            )
        ),
        history=[],
    )
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "can_download": True,
                "logged_in": True,
                "browser": "edge",
                "platform": "grok",
                "agent_sources": {"recent_sessions": [], "projects": []},
            }
        ),
    )
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(
            json={"platform": "grok", "recent_sessions": [], "projects": []}
        ),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/grok")
        answer = page.locator("#agent_response_answer")
        expect(answer).to_contain_text("Evidence-ranked longevity options")

        visible_text = answer.inner_text()
        rendered_markup = answer.inner_html()
        for provider_fragment in ("grok:render", "argument", "citation_id"):
            assert provider_fragment not in visible_text
            assert provider_fragment not in rendered_markup

        heading = answer.locator("h3").first
        paragraph = answer.locator("li").first
        strong = answer.locator("strong").first
        expect(heading).to_have_css("font-size", "20px")
        expect(heading).to_have_css("line-height", "28px")
        expect(strong).to_have_css("font-weight", "600")
        type_scale = answer.evaluate(
            """element => ({
                heading: parseFloat(getComputedStyle(element.querySelector('h3')).fontSize),
                body: parseFloat(getComputedStyle(element.querySelector('li')).fontSize),
            })"""
        )
        assert type_scale["heading"] > type_scale["body"]
        expect(paragraph).to_be_visible()

        citation = answer.locator("a.agent-inline-citation")
        expect(citation).to_have_count(1)
        expect(citation).to_have_text("Example")
        expect(citation).to_have_attribute("href", "https://example.com/source")
        expect(citation).to_have_attribute("target", "_blank")
        rel_tokens = set((citation.get_attribute("rel") or "").split())
        assert {"noopener", "noreferrer", "nofollow"}.issubset(rel_tokens)
        citation_style = citation.evaluate(
            """element => {
                const style = getComputedStyle(element);
                const bounds = element.getBoundingClientRect();
                return {
                    backgroundColor: style.backgroundColor,
                    borderRadius: parseFloat(style.borderRadius),
                    fontSize: style.fontSize,
                    paddingInlineEnd: parseFloat(style.paddingInlineEnd),
                    paddingInlineStart: parseFloat(style.paddingInlineStart),
                    height: bounds.height,
                    protocol: new URL(element.href).protocol,
                    whiteSpace: style.whiteSpace,
                };
            }"""
        )
        assert citation_style["protocol"] == "https:"
        assert citation_style["fontSize"] == "13px"
        assert citation_style["whiteSpace"] == "nowrap"
        assert citation_style["paddingInlineStart"] >= 7
        assert citation_style["paddingInlineEnd"] >= 7
        assert citation_style["borderRadius"] >= citation_style["height"] / 2
        assert citation_style["backgroundColor"] != "rgba(0, 0, 0, 0)"

        table_shell = answer.locator(".agent-markdown-table-shell")
        expect(table_shell).to_have_count(1)
        expect(table_shell.locator("table")).to_have_count(1)
        if width == 390:
            table_geometry = table_shell.evaluate(
                "element => ({clientWidth: element.clientWidth, scrollWidth: element.scrollWidth})"
            )
            assert table_geometry["scrollWidth"] > table_geometry["clientWidth"]
            table_shell.evaluate("element => { element.scrollLeft = element.scrollWidth; }")
            page.wait_for_function(
                """element => (
                    element.scrollLeft >= element.scrollWidth - element.clientWidth - 1
                )""",
                arg=table_shell.element_handle(),
            )

        document_geometry = page.evaluate(
            """() => ({
                clientWidth: document.documentElement.clientWidth,
                scrollWidth: document.documentElement.scrollWidth,
            })"""
        )
        assert document_geometry["scrollWidth"] <= document_geometry["clientWidth"] + 1
        assert not page_errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1138, 390])
def test_switch_sessions_and_stop_only_selected(disposable_browser, sidebar_server_url, width):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    agents = {}
    for key, revision in (("primary", 102), ("second", 101)):
        agents[key] = deepcopy(base["agent"])
        agents[key].update(session_id=key, running=True, phase="running", run_id=key, run_revision=revision,
            session_title=f"Task {key}", prompt=f"Prompt {key}", response=f"Answer {key}", response_html=f"<p>Answer {key}</p>",
            history=[], activity=[{"status": "running", "label": "Read", "detail": key}])
    stopped = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        route.fulfill(json={**base, "agent": agents.get(key, {"session_id": "new"}),
            "sessions": [dict(agent) for agent in agents.values()],
            "active_count": sum(agent["running"] for agent in agents.values()), "concurrency_limit": 2})

    def stop(route):
        key = route.request.headers["x-cachelikes-agent-session"]
        stopped.append(key)
        agents[key].update(running=False, phase="finished")
        route.fulfill(json={**base, "agent": agents[key], "stop_requested": True})

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/stop", stop)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions(),
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        rail = page.locator("[data-agent-execution-sessions]")
        expect(page.locator('[data-agent-new-task]')).to_have_count(0)
        expect(page.locator('[data-agent-new-session]')).to_be_visible()
        expect(rail).to_have_js_property('open', True)
        summary = rail.locator('summary')
        summary.focus()
        summary.press('Enter')
        expect(rail).to_have_js_property('open', False)
        expect(page.locator('[data-agent-execution-session-list]')).to_be_hidden()
        summary.press('Space')
        expect(rail).to_have_js_property('open', True)
        expect(page.locator('[data-agent-execution-session-list]')).to_be_visible()

        expect(rail).to_be_visible()
        expect(rail.locator("[data-agent-session-capacity]")).to_have_text("2")
        row = rail.locator("[data-execution-session-id=primary]")
        expect(row).to_have_css("border-radius", "999px")
        assert abs(row.bounding_box()["height"] - 36) < 0.1
        assert rail.evaluate("e => Boolean(document.querySelector('#agent_runtime_form').compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING)")
        spinner = rail.locator(".suggestion-loading-spinner").first
        expect(spinner).to_have_attribute("aria-label", "Running")
        expect(spinner).to_have_text("")
        assert spinner.evaluate("e => getComputedStyle(e).maskImage.includes('loading.spinner.svg')")
        assert spinner.evaluate("e => getComputedStyle(e).animationName") == "ticker-suggestion-loading"

        page.locator("[data-execution-session-id=primary]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Prompt primary")
        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Prompt second")
        page.reload()
        expect(page.locator("#agent_response_question")).to_have_text("Prompt second")
        expect(page.locator("[data-execution-session-id=second]")).to_have_attribute("aria-pressed", "true")
        if width < 900 and not rail.is_visible():
            page.locator("#sidebar_toggle").click()
        expect(page.locator("#agent_activity_list")).to_contain_text("second")
        expect(page.locator("[data-execution-session-id=second]")).to_have_attribute("aria-pressed", "true")
        page.locator('.agent-session-mode-combobox [data-agent-combobox-trigger]').click()
        page.locator('.agent-session-mode-combobox [data-agent-combobox-option="new"]').click()
        expect(page.locator("[data-agent-session-capacity]")).to_have_text("2")
        expect(page.locator("#agent_response_output")).to_be_hidden()
        ask = page.get_by_role("button", name="Ask ChatGPT Web", exact=True)
        expect(ask).to_be_disabled()
        prompt = page.locator("#agent_prompt_input")
        prompt.fill("A separate draft")
        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Prompt second")
        page.locator('.agent-session-mode-combobox [data-agent-combobox-trigger]').click()
        page.locator('.agent-session-mode-combobox [data-agent-combobox-option="new"]').click()
        expect(prompt).to_have_value("A separate draft")
        page.locator("[data-execution-session-id=second]").click()
        if width < 900:
            page.locator("#sidebar_toggle").click()
        page.get_by_role("button", name="Stop Agent task", exact=True).click()
        expect(page.locator("[data-agent-session-capacity]")).to_have_text("1")
        assert stopped == ["second"]
        assert agents["primary"]["running"]
        if width < 900:
            page.locator("#sidebar_toggle").click()
        geometry = rail.evaluate("e => ({width: e.clientWidth, scroll: e.scrollWidth})")
        assert geometry["scroll"] <= geometry["width"] + 1
        page.locator('.agent-session-mode-combobox [data-agent-combobox-trigger]').click()
        page.locator('.agent-session-mode-combobox [data-agent-combobox-option="new"]').click()
        expect(ask).to_be_enabled()
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1138, 390])
def test_recent_session_switch_preserves_stable_row_nodes(
    disposable_browser, sidebar_server_url, width,
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    agents = {}
    for key, revision in (("primary", 102), ("second", 101)):
        agents[key] = deepcopy(base["agent"])
        agents[key].update(
            session_id=key,
            conversation_url=f"https://chatgpt.com/c/{key}",
            running=False,
            phase="finished",
            run_id=key,
            run_revision=revision,
            session_title=f"Task {key}",
            prompt=f"Prompt {key}",
            response=f"Answer {key}",
            response_html=f"<p>Answer {key}</p>",
            history=[],
            activity=[],
        )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        route.fulfill(json={
            **base,
            "agent": agents.get(key, {"session_id": "new"}),
            "sessions": [dict(agent) for agent in agents.values()],
            "active_count": 0,
            "concurrency_limit": 2,
        })

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "logged_in": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": {"recent_sessions": [], "projects": []},
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json={
        "recent_sessions": [],
        "projects": [],
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        session_list = page.locator("[data-agent-execution-session-list]")
        rows = session_list.locator(":scope > .agent-execution-session-row")
        expect(rows).to_have_count(2)
        initial_height = session_list.evaluate("element => element.getBoundingClientRect().height")
        page.evaluate("""
            () => {
                const list = document.querySelector('[data-agent-execution-session-list]');
                [...list.children].forEach((row, index) => {
                    row.dataset.identityProbe = `stable-${index}`;
                });
                window.__recentSessionChildMutations = [];
                new MutationObserver((records) => {
                    records.forEach((record) => {
                        window.__recentSessionChildMutations.push({
                            added: record.addedNodes.length,
                            removed: record.removedNodes.length,
                        });
                    });
                }).observe(list, {childList: true});
            }
        """)

        second = page.locator("[data-execution-session-id=second]")
        second.click()
        expect(page.locator("#agent_response_question")).to_have_text("Prompt second")
        expect(second).to_have_attribute("aria-pressed", "true")
        assert page.evaluate(
            "document.activeElement === document.querySelector('[data-execution-session-id=second]')"
        )
        assert rows.evaluate_all("elements => elements.map(row => row.dataset.identityProbe)") == [
            "stable-0",
            "stable-1",
        ]
        assert page.evaluate("window.__recentSessionChildMutations") == []
        final_height = session_list.evaluate("element => element.getBoundingClientRect().height")
        assert abs(final_height - initial_height) < 0.1
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1138, 390])
def test_new_session_explains_workspace_block_and_keeps_draft_until_unblocked(
    disposable_browser, sidebar_server_url, width
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    blocked = [True]
    blocked_reason = (
        "This workspace already has an active write-capable Agent task. "
        "Wait for it to finish or stop it before starting another write-capable task."
    )
    completed = {
        **base["agent"],
        "session_id": "selected",
        "run_id": "selected-run",
        "workspace_path": "/tmp/shared-workspace",
        "session_title": "Completed task",
        "running": False,
        "phase": "finished",
    }
    active = {
        **completed,
        "session_id": "active",
        "run_id": "active-run",
        "session_title": "Active writer",
        "running": True,
        "phase": "running",
    }
    page.add_init_script(
        "sessionStorage.setItem('cachelikes:agent-execution-session:edge:chatgpt', 'selected')"
    )

    def status(route):
        session_id = route.request.headers.get("x-cachelikes-agent-session", "selected")
        if not blocked[0]:
            active.update(running=False, phase="finished")
        route.fulfill(json={
            **base,
            "agent": {"session_id": "new"} if session_id == "new" else completed,
            "sessions": [completed, active],
            "active_count": 1 if blocked[0] else 0,
            "can_start": not blocked[0],
            "start_blocked_reason": blocked_reason if blocked[0] else "",
        })

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "logged_in": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions(),
    }))
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        expect(page.locator("[data-execution-session-id=selected]")).to_have_attribute(
            "aria-pressed", "true"
        )
        page.locator("[data-agent-new-session]").click()

        status_copy = page.locator("#agent_response_status")
        ask = page.get_by_role("button", name="Ask ChatGPT Web", exact=True)
        expect(status_copy).to_have_attribute("data-status", "loading")
        expect(status_copy).to_contain_text(f"Waiting · {blocked_reason}")
        expect(ask).to_be_disabled()
        expect(ask).to_have_attribute("title", blocked_reason)
        expect(ask).to_have_attribute("aria-describedby", "agent_response_status")

        prompt = page.locator("#agent_prompt_input")
        prompt.fill("Keep this draft while the active writer finishes.")
        blocked[0] = False
        expect(ask).to_be_enabled(timeout=5_000)
        expect(status_copy).to_have_attribute("data-status", "ready")
        expect(status_copy).to_contain_text("Ready")
        expect(prompt).to_have_value("Keep this draft while the active writer finishes.")
        expect(ask).to_have_attribute("title", "Ask ChatGPT Web")
        expect(ask).not_to_have_attribute("aria-describedby", "agent_response_status")
    finally:
        context.close()


def test_failed_local_session_without_remote_record_has_centered_hover_delete(
    disposable_browser, sidebar_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1026, "height": 1090})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    remote_url = "https://chatgpt.com/c/remote-failure"
    sessions = [
        {"session_id": "orphan", "session_title": "Failed before remote record",
         "running": False, "phase": "failed", "conversation_url": ""},
        {"session_id": "remote", "session_title": "Failed with remote record",
         "running": False, "phase": "failed", "conversation_url": remote_url},
        {"session_id": "complete", "session_title": "Completed local task",
         "running": False, "phase": "finished", "conversation_url": ""},
    ]
    base.update(agent={"session_id": "new"}, sessions=sessions, active_count=0)
    deleted = []

    def delete_session(route):
        deleted.append(route.request.post_data_json)
        route.fulfill(json={"deleted": True, "session_id": "orphan"})

    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route("**/api/agent/session", delete_session)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": {
            "projects": [],
            "recent_sessions": [{"url": remote_url, "title": "Remote failure"}],
        },
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        summary = page.locator("[data-agent-execution-sessions] > summary")
        assert summary.bounding_box()["height"] == pytest.approx(36, abs=0.1)
        expect(page.locator("[data-agent-new-session]")).to_be_visible()
        assert page.locator("[data-agent-execution-sessions]").evaluate(
            "(rail) => rail.contains(document.querySelector('[data-agent-new-session]'))",
        )

        orphan = page.locator("[data-execution-session-id=orphan]")
        remote = page.locator("[data-execution-session-id=remote]")
        complete = page.locator("[data-execution-session-id=complete]")
        expect(orphan.locator("xpath=..").locator(".agent-execution-session-delete")).to_have_count(1)
        expect(remote.locator("xpath=..").locator(".agent-execution-session-delete")).to_have_count(0)
        expect(complete.locator("xpath=..").locator(".agent-execution-session-delete")).to_have_count(0)

        orphan.hover()
        delete_button = orphan.locator("xpath=..").locator(".agent-execution-session-delete")
        expect(delete_button).to_be_visible()
        expect(orphan.locator(".agent-execution-session-state")).to_have_css("opacity", "0")
        centers = orphan.locator("xpath=..").evaluate("""row => {
            const pill = row.querySelector('.agent-execution-session').getBoundingClientRect();
            const action = row.querySelector('.agent-execution-session-delete').getBoundingClientRect();
            return {
                pillRightCenterX: pill.right - pill.height / 2,
                pillCenterY: pill.top + pill.height / 2,
                actionCenterX: action.left + action.width / 2,
                actionCenterY: action.top + action.height / 2,
            };
        }""")
        assert centers["actionCenterX"] == pytest.approx(centers["pillRightCenterX"], abs=0.1)
        assert centers["actionCenterY"] == pytest.approx(centers["pillCenterY"], abs=0.1)

        delete_button.click()
        expect(page.locator("[data-execution-session-id=orphan]")).to_have_count(0)
        assert deleted == [{
            "session_id": "orphan",
            "conversation_url": "",
            "remote_record_absent": True,
        }]
    finally:
        context.close()


@pytest.mark.parametrize(("width", "color_scheme"), [(1024, "light"), (390, "dark")])
def test_agent_sidebar_trailing_controls_share_the_toggle_centerline(
    disposable_browser, sidebar_server_url, width, color_scheme,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1_164},
        color_scheme=color_scheme,
    )
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    project_url = "https://chatgpt.com/g/g-p-demo/project"
    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "logged_in": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": {
            "recent_sessions": [],
            "projects": [{"url": project_url, "title": "Demo project"}],
        },
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json={
        "recent_sessions": [],
        "projects": [{"url": project_url, "title": "Demo project"}],
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        source = page.locator(".agent-session-mode-combobox")
        source.locator("[data-agent-combobox-trigger]").click()
        source.locator('[data-agent-combobox-option="project"]').click()
        expect(page.locator("[data-agent-project-field]")).to_be_visible()

        sidebar_geometry = page.evaluate("""() => {
            const bounds = selector => document.querySelector(selector).getBoundingClientRect();
            const centerX = rect => rect.left + rect.width / 2;
            const centerY = rect => rect.top + rect.height / 2;
            const sidebar = bounds('#agent_sidebar');
            const toggle = bounds('#sidebar_toggle');
            const chooser = bounds('#agent_project_path_choose');
            const summary = document.querySelector('[data-agent-execution-sessions] > summary');
            const summaryRect = summary.getBoundingClientRect();
            const summaryStyle = getComputedStyle(summary);
            const trailingTrackWidth = Number.parseFloat(
                summaryStyle.gridTemplateColumns.split(' ').at(-1),
            );
            const newSession = bounds('[data-agent-new-session]');
            const newSessionIcon = bounds('.agent-new-session-icon');
            return {
                chooserCenterDelta: Math.abs(centerX(chooser) - centerX(toggle)),
                collapseCenterDelta: Math.abs(
                    summaryRect.right - trailingTrackWidth / 2 - centerX(toggle),
                ),
                iconLeftCircleDelta: Math.abs(
                    centerX(newSessionIcon) - (newSession.left + newSession.height / 2),
                ),
                pillChevronCenters: Array.from(
                    document.querySelectorAll('#agent_runtime_form .browser-picker-trigger-chevron'),
                    chevron => centerX(chevron.getBoundingClientRect()),
                ),
                toggleEdgeDelta: Math.abs(
                    centerY(toggle) - sidebar.top - (sidebar.right - centerX(toggle)),
                ),
                newSessionIconMask: getComputedStyle(
                    document.querySelector('.agent-new-session-icon'),
                ).maskImage,
            };
        }""")
        assert sidebar_geometry["toggleEdgeDelta"] <= 1
        assert sidebar_geometry["chooserCenterDelta"] <= 1
        assert sidebar_geometry["collapseCenterDelta"] <= 1
        assert sidebar_geometry["iconLeftCircleDelta"] <= 1
        assert "plus.svg" in sidebar_geometry["newSessionIconMask"]
        assert max(sidebar_geometry["pillChevronCenters"]) - min(
            sidebar_geometry["pillChevronCenters"]
        ) <= 1
    finally:
        context.close()


@pytest.mark.parametrize(("width", "color_scheme"), [(1024, "light"), (390, "dark")])
def test_new_session_inherits_selected_project_without_stopping_existing_task(
    disposable_browser, sidebar_server_url, width, color_scheme,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1_164},
        color_scheme=color_scheme,
    )
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    project_url = "https://chatgpt.com/g/g-p-demo/project"
    conversation_url = "https://chatgpt.com/c/project-session"
    selected_agent = {
        **base["agent"],
        "session_id": "selected",
        "run_id": "selected-run",
        "running": True,
        "phase": "running",
        "workspace_path": "/tmp/demo-project",
        "project_url": project_url,
        "conversation_url": conversation_url,
        "session_title": "Existing project task",
    }
    stopped = []
    submitted = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def status(route):
        session_id = route.request.headers.get("x-cachelikes-agent-session", "new")
        agent = selected_agent if session_id == "selected" else {"session_id": "new"}
        route.fulfill(json={
            **base,
            "agent": agent,
            "sessions": [selected_agent],
            "active_count": 1,
            "can_start": True,
        })

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/preferences", lambda route: route.fulfill(json=base))
    page.route("**/api/agent/stop", lambda route: (stopped.append(True), route.fulfill(json=base)))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True,
        "logged_in": True,
        "browser": "edge",
        "platform": "chatgpt",
        "agent_sources": {
            "recent_sessions": [],
            "projects": [{"url": project_url, "title": "Demo project"}],
        },
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json={
        "recent_sessions": [],
        "projects": [{"url": project_url, "title": "Demo project"}],
    }))
    page.route("**/api/agent/project-sessions**", lambda route: route.fulfill(json={
        "sessions": [{"url": conversation_url, "title": "Existing project task"}],
    }))
    page.route("**/api/agent/chatgpt-session-history**", lambda route: route.fulfill(json={"history": []}))

    def ask(route):
        submitted.append(route.request.post_data_json)
        route.fulfill(json={
            **base,
            "agent": {**base["agent"], "session_id": "created", "running": True, "phase": "running"},
            "sessions": [selected_agent],
            "active_count": 1,
            "can_start": True,
        })

    page.route("**/api/agent/ask", ask)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        source = page.locator(".agent-session-mode-combobox")
        source.locator("[data-agent-combobox-trigger]").click()
        source.locator('[data-agent-combobox-option="project"]').click()
        projects = page.locator('[data-agent-session-list="projects"]')
        projects.locator("[data-agent-combobox-trigger]").click()
        projects.get_by_role("option", name="Demo project", exact=True).click()
        page.locator("[data-execution-session-id=selected]").click()
        expect(page.locator("#agent_response_question")).to_have_text(selected_agent["prompt"])

        prompt = page.locator("#agent_prompt_input")
        prompt.fill("Start a separate task in the same project.")
        new_session = page.locator("[data-agent-new-session]")
        expect(new_session).to_have_class("secondary-button agent-new-session-button")
        placement = new_session.evaluate("""button => {
            const form = document.querySelector('#agent_runtime_form');
            const recent = document.querySelector('[data-agent-execution-sessions]');
            return {
                inForm: form.contains(button),
                inRecent: recent.contains(button),
                beforeList: Boolean(button.compareDocumentPosition(
                    document.querySelector('[data-agent-execution-session-list]'),
                ) & Node.DOCUMENT_POSITION_FOLLOWING),
            };
        }""")
        assert placement == {"inForm": False, "inRecent": True, "beforeList": True}
        alignment = new_session.evaluate("""button => {
            const body = button.closest('.ui-collapse-body');
            const buttonRect = button.getBoundingClientRect();
            const bodyRect = body.getBoundingClientRect();
            return {
                display: getComputedStyle(body).display,
                rightDelta: Math.abs(bodyRect.right - buttonRect.right),
            };
        }""")
        assert alignment["display"] == "grid"
        assert alignment["rightDelta"] <= 1
        new_session.click()

        expect(page.locator('input[name="session_mode"]')).to_have_value("project_new")
        expect(page.locator('input[name="project_url"]')).to_have_value(project_url)
        expect(page.locator('input[name="conversation_url"]')).to_have_value("")
        expect(projects.locator("[data-agent-combobox-selected-label]")).to_have_text("Demo project")
        expect(prompt).to_have_value("")
        expect(page.locator("#agent_response_output")).to_be_hidden()
        expect(page.locator("[data-execution-session-id=selected]")).to_have_attribute("aria-pressed", "false")
        assert not stopped

        page.locator("[data-execution-session-id=selected]").click()
        expect(prompt).to_have_value("Start a separate task in the same project.")
        new_session.click()
        expect(prompt).to_have_value("")
        prompt.fill("Start the inherited project task.")
        if width < 900:
            page.locator("#sidebar_toggle").click()

        ask_button = page.get_by_role("button", name="Ask ChatGPT Web", exact=True)
        expect(ask_button).to_be_enabled()
        with page.expect_response("**/api/agent/ask"):
            ask_button.click()
        expect(page.get_by_role("button", name="Stop Agent task", exact=True)).to_be_visible()
        assert len(submitted) == 1
        assert submitted[0]["session_mode"] == "project_new"
        assert submitted[0]["project_url"] == project_url
        assert submitted[0]["conversation_url"] == ""
        assert submitted[0]["prompt"] == "Start the inherited project task."
        assert not stopped
        assert not errors
    finally:
        context.close()


def test_late_response_cannot_replace_new_selection(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context(viewport={"width": 1138, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    held = []
    delay_second = [False]

    def payload(key):
        agent = {**base["agent"], "session_id": key, "run_id": key, "run_revision": 100,
            "prompt": key, "response": key, "response_html": f"<p>{key}</p>", "history": []}
        return {**base, "agent": agent, "active_count": 0, "sessions": [
            {"session_id": item, "session_title": item, "running": False, "phase": "finished"}
            for item in ("primary", "second")]}

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        if key == "second" and delay_second[0]:
            held.append(route)
        else:
            route.fulfill(json=payload(key))

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions(),
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        page.locator("[data-execution-session-id=primary]").click()
        expect(page.locator("#agent_response_question")).to_have_text("primary")
        delay_second[0] = True
        page.locator("[data-execution-session-id=second]").click()
        page.locator("[data-execution-session-id=primary]").click()
        expect(page.locator("#agent_response_question")).to_have_text("primary")
        assert held
        for route in held:
            route.fulfill(json=payload("second"))
        expect(page.locator("#agent_response_question")).to_have_text("primary")
        delay_second[0] = False
        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("#agent_response_question")).to_have_text("second")
        page.reload()
        expect(page.locator("#agent_response_question")).to_have_text("second")
        expect(page.locator("[data-execution-session-id=second]")).to_have_attribute("aria-pressed", "true")
    finally:
        context.close()


@pytest.mark.parametrize("decorated_user", [False, True])
def test_identical_chatgpt_response_after_virtualized_turn_replacement(disposable_browser, monkeypatch, decorated_user):
    from app.core import computer_use_agent as agent

    context = disposable_browser.new_context()
    page = context.new_page()
    message = "Continue the unfinished Agent task."

    def turns(user_id, assistant_id, prompt):
        if decorated_user:
            prompt = (
                '<div role="group" aria-label="context.md"><button>context.md</button></div>'
                f'<div data-testid="collapsible-user-message-content">{prompt}</div>'
                '<button data-testid="collapsible-user-message-toggle">Show more</button>'
            )
        return (
            f'<div data-message-author-role="user" data-message-id="{user_id}">{prompt}</div>'
            f'<div data-message-author-role="assistant" data-message-id="{assistant_id}">'
            '<pre><code>{"action":"bodycheck"}</code></pre></div>'
            '<div id="prompt-textarea" contenteditable="true"></div>'
        )

    page.set_content(turns("old-user", "old-assistant", "Previous observation"))
    monkeypatch.setattr(agent, "_submit_chromium_prompt", lambda *_args, **_kwargs:
        page.set_content(turns("new-user", "new-assistant", message)))
    monkeypatch.setattr(agent, "WEB_RESPONSE_MINIMUM_SECONDS", 0)
    monkeypatch.setattr(agent, "WEB_RESPONSE_STABLE_SECONDS", 0)
    try:
        response = agent._submit_and_wait(page, "chromium", message, lambda: False, timeout_seconds=1)
        assert agent.parse_agent_action(response) == {"action": "bodycheck"}
    finally:
        context.close()


def test_chatgpt_receipt_marker_is_scoped_to_the_latest_user_root(
    disposable_browser,
):
    from app.core import computer_use_agent as agent

    context = disposable_browser.new_context()
    page = context.new_page()
    marker = "agent-turn-0123456789abcdef0123456789abcdef"
    other_marker = "agent-turn-fedcba9876543210fedcba9876543210"

    def turn_html(latest_marker: str) -> str:
        return f"""
            <div data-message-author-role="user" data-message-id="old-user">
              Controller turn receipt: {marker}
            </div>
            <div data-message-author-role="assistant" data-message-id="old-assistant">
              Prior response containing {marker}
            </div>
            <div data-message-author-role="user" data-message-id="latest-user">
              Controller turn receipt: {latest_marker}
            </div>
            <div data-message-author-role="assistant" data-message-id="latest-assistant">
              <pre><code>{{"action":"bodycheck"}}</code></pre>
            </div>
            <div id="prompt-textarea" contenteditable="true">{marker}</div>
        """

    try:
        page.set_content(turn_html(other_marker))
        stale_snapshot = agent._provider_turn_snapshot(
            page,
            "chatgpt",
            receipt_marker=marker,
        )
        assert stale_snapshot["latestUserMessageId"] == "latest-user"
        assert stale_snapshot["markerEchoed"] is False

        page.set_content(turn_html(marker))
        current_snapshot = agent._provider_turn_snapshot(
            page,
            "chatgpt",
            receipt_marker=marker,
        )
        assert current_snapshot["latestUserMessageId"] == "latest-user"
        assert current_snapshot["markerEchoed"] is True
        assert current_snapshot["assistantAfterLatestUser"] is True
    finally:
        context.close()


def test_session_catalog_titles_and_global_capacity(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context(viewport={"width": 1161, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    url = "https://chatgpt.com/c/provider-title"
    catalog = fixtures._chatgpt_catalog_sessions({"id": "provider-title", "url": url,
        "title": "Official conversation title", "updated_at": "2026-09-06T00:00:00Z"})
    page.route("**/api/agent/status", lambda route: route.fulfill(json={**base,
        "agent": {**base["agent"], "session_id": "primary", "conversation_url": url},
        "sessions": [{"session_id": "primary", "session_title": "Temporary label",
            "conversation_url": url, "running": False, "phase": "finished"}],
        "active_count": 1, "concurrency_limit": 2}))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": catalog}))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        capacity = page.locator("[data-agent-session-capacity]")
        expect(capacity).to_have_text("1")
        expect(capacity).to_have_attribute("aria-label", "1 active session")
        expect(page.locator(".agent-execution-session-title")).to_have_text("Official conversation title")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1042, 390])
def test_project_filter_keeps_same_title_cross_workspace_active_sessions(
    disposable_browser,
    sidebar_server_url,
    width,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 863},
        reduced_motion="reduce",
    )
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    project_urls = {
        "primary": "https://chatgpt.com/g/g-p-primary/project",
        "second": "https://chatgpt.com/g/g-p-second/project",
    }
    workspaces = {
        "primary": "/tmp/project-alpha",
        "second": "/tmp/project-beta",
    }
    shared_title = "审计这个项目的代码复用状况"
    agents = {
        key: {
            **base["agent"],
            "session_id": key,
            "run_id": key,
            "run_revision": 100,
            "workspace_path": workspaces[key],
            "project_url": project_urls[key],
            "conversation_url": f"https://chatgpt.com/c/{key}",
            "session_mode": "project_session",
            "session_title": shared_title,
            "prompt": shared_title,
            "running": True,
            "phase": "running",
            "finished_at": "",
            "history": [],
        }
        for key in ("primary", "second")
    }
    finished_elsewhere = {
        **agents["second"],
        "session_id": "finished",
        "run_id": "finished",
        "conversation_url": "https://chatgpt.com/c/finished",
        "session_title": "Finished elsewhere",
        "running": False,
        "phase": "finished",
    }
    page.add_init_script(
        "sessionStorage.setItem("
        "'cachelikes:agent-execution-session:edge:chatgpt', 'primary')"
    )

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        route.fulfill(
            json={
                **base,
                "agent": agents.get(key, agents["primary"]),
                "sessions": [*agents.values(), finished_elsewhere],
                "active_count": 2,
                "can_start": False,
                "concurrency_limit": 2,
            }
        )

    catalog = {
        "recent_sessions": [],
        "projects": [
            {"url": project_urls[key], "title": f"Project {key}"}
            for key in ("primary", "second")
        ],
    }
    page.route("**/api/agent/status", status)
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "can_download": True,
                "logged_in": True,
                "browser": "edge",
                "platform": "chatgpt",
                "agent_sources": catalog,
            }
        ),
    )
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog))
    page.route(
        "**/api/agent/project-sessions**",
        lambda route: route.fulfill(json={"sessions": []}),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        capacity = page.locator("[data-agent-session-capacity]")
        expect(capacity).to_have_text("2")
        expect(capacity).to_have_attribute("aria-label", "2 active sessions")
        expect(page.locator(".agent-execution-session-title")).to_have_text(
            [shared_title, shared_title]
        )
        expect(page.locator("[data-execution-session-id=primary]")).to_be_visible()
        expect(page.locator("[data-execution-session-id=second]")).to_be_visible()
        expect(page.locator("[data-execution-session-id=finished]")).to_have_count(0)

        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("[data-agent-project-name]")).to_have_text("project-beta")
        expect(page.locator('input[name="workspace_path"]')).to_have_value(
            workspaces["second"]
        )
        expect(capacity).to_have_text("2")
    finally:
        context.close()


def test_status_disconnect_recovers_selected_run_without_mutation(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context(viewport={"width": 1161, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    disconnected = [False]
    mutations = []
    requested = []
    page.add_init_script("sessionStorage.setItem('cachelikes:agent-execution-session:edge:chatgpt', 'second')")

    def status(route):
        requested.append(route.request.headers.get("x-cachelikes-agent-session"))
        if disconnected[0]:
            route.abort("internetdisconnected")
            return
        route.fulfill(json={**base, "agent": {**base["agent"], "session_id": "second",
            "run_id": "ongoing", "run_revision": 999, "running": True, "phase": "running",
            "prompt": "Ongoing task", "message": "Provider is generating", "history": []},
            "sessions": [{"session_id": "second", "session_title": "Ongoing task", "running": True}],
            "active_count": 1})

    def mutation(route):
        mutations.append(route.request.url)
        route.abort()

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/stop", mutation)
    page.route("**/api/agent/ask", mutation)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions()}))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(page.locator("#agent_response_question")).to_have_text("Ongoing task")
        disconnected[0] = True
        expect(page.locator("#agent_response_status")).to_contain_text("Reconnecting", timeout=10000)
        expect(page.locator("[data-execution-session-id=second]")).to_have_attribute("aria-pressed", "true")
        disconnected[0] = False
        expect(page.locator("#agent_response_status")).to_contain_text("Provider is generating", timeout=10000)
        page.reload()
        expect(page.locator("#agent_response_question")).to_have_text("Ongoing task")
        assert requested and set(requested) == {"second"}
        assert mutations == []
    finally:
        context.close()


@pytest.mark.parametrize("width", [1161, 390])
def test_cross_project_sessions_switch_workspace_and_restore_on_reload(disposable_browser, sidebar_server_url, width):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    workspaces = {"primary": "/tmp/project-alpha", "second": "/tmp/project-beta"}
    catalog = [{"session_id": key, "session_title": key, "workspace_path": path,
                "running": True, "phase": "running"} for key, path in workspaces.items()]
    requests = []

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        path = route.request.headers.get("x-cachelikes-agent-workspace", "")
        requests.append((key, path))
        if key == "new":
            route.fulfill(json={**base, "agent": {"session_id": "new"}, "sessions": catalog,
                                "active_count": 2, "can_start": False})
            return
        matched = path == workspaces[key]
        route.fulfill(json={**base, "sessions": catalog, "active_count": 2,
            "agent": {**base["agent"], "session_id": key, "workspace_path": workspaces[key],
                "running": True, "phase": "running", "run_id": key, "run_revision": 999,
                "prompt": f"Task {key}" if matched else "", "history": []}})

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions()}))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        expect(page.locator(".agent-execution-session")).to_have_count(2)
        page.locator("[data-execution-session-id=second]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Task second")
        expect(page.locator("[data-agent-project-name]")).to_have_text("project-beta")
        page.reload()
        expect(page.locator("#agent_response_question")).to_have_text("Task second")
        expect(page.locator("[data-agent-project-name]")).to_have_text("project-beta")
        assert ("second", workspaces["second"]) in requests
        if width < 900 and not page.locator("[data-execution-session-id=primary]").is_visible():
            page.locator("#sidebar_toggle").click()
        page.locator("[data-execution-session-id=primary]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Task primary")
        expect(page.locator("[data-agent-project-name]")).to_have_text("project-alpha")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1161, 390])
def test_expired_execution_session_recovers_without_mutation(disposable_browser, sidebar_server_url, width):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    requested, mutations = [], []
    page.add_init_script("sessionStorage.setItem('cachelikes:agent-execution-session:edge:chatgpt', 'expired')")

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session")
        requested.append(key)
        if key == "expired":
            route.fulfill(status=404, json={"code": "unknown_agent_session", "error": "Session unavailable"})
        else:
            route.fulfill(json={**base, "agent": {"session_id": "new"}, "sessions": [],
                                "active_count": 0, "can_start": True})

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/ask", lambda route: (mutations.append(route.request.url), route.abort()))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions()}))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(page.get_by_role("button", name="Ask ChatGPT Web", exact=True)).to_be_enabled()
        assert requested[:2] == ["expired", "new"]
        assert page.evaluate("sessionStorage.getItem('cachelikes:agent-execution-session:edge:chatgpt')") == "new"
        assert mutations == []
    finally:
        context.close()


@pytest.mark.parametrize("choice,target,label,scope", [
    ("platform", "grok", "Grok", "edge:grok"),
    ("browser", "chrome", "ChatGPT", "chrome:chatgpt"),
])
def test_route_switch_isolates_worker_and_obeys_global_admission(disposable_browser, sidebar_server_url, choice, target, label, scope):
    context = disposable_browser.new_context(viewport={"width": 1161, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    requested = []
    page.add_init_script("sessionStorage.setItem('cachelikes:agent-execution-session:edge:chatgpt', 'original')")

    def status(route):
        headers = route.request.headers
        platform = headers["x-cachelikes-agent-platform"]
        key = headers["x-cachelikes-agent-session"]
        browser = headers["x-cachelikes-agent-browser"]
        current_scope = f"{browser}:{platform}"
        requested.append((current_scope, key))
        route.fulfill(json={**base, "agent": {"session_id": key},
            "sessions": [{"session_id": "original", "running": True}] if current_scope == "edge:chatgpt" else [],
            "active_count": 1, "can_start": current_scope == "edge:chatgpt"})

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": fixtures._chatgpt_catalog_sessions()}))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=fixtures._chatgpt_catalog_sessions()))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(page.get_by_role("button", name="Ask ChatGPT Web", exact=True)).to_be_enabled()
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-trigger]').click()
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-option="{target}"]').click()
        expect(page.get_by_role("button", name=f"Ask {label} Web", exact=True)).to_be_disabled()
        page.wait_for_function("scope => sessionStorage.getItem('cachelikes:agent-execution-session:' + scope) === 'new'", arg=scope)
        assert (scope, "new") in requested
        assert (scope, "original") not in requested
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-trigger]').click()
        original = "chatgpt" if choice == "platform" else "edge"
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-option="{original}"]').click()
        expect(page.get_by_role("button", name="Ask ChatGPT Web", exact=True)).to_be_enabled()
        assert requested[-1] == ("edge:chatgpt", "original")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1138, 390])
def test_restored_session_loads_bound_history_and_ignores_late_reply(
    disposable_browser, sidebar_server_url, width
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    base.pop("can_start", None)
    page.add_init_script(
        """(() => {
            const key = 'cachelikes:agent-execution-session:edge:chatgpt';
            if (!sessionStorage.getItem(key)) sessionStorage.setItem(key, 'primary');
        })()"""
    )
    held = []
    requested = []
    sessions = [
        {"session_id": key, "session_title": key, "conversation_url": f"https://chatgpt.com/c/{key}",
         "running": False, "phase": "finished"}
        for key in ("primary", "restored", "late", "unavailable")
    ]

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "new")
        agent = {**base["agent"], "session_id": key, "run_id": key, "run_revision": 100,
                 "conversation_url": f"https://chatgpt.com/c/{key}", "conversation_bound": True,
                 "history": [], "prompt": "", "response": "", "response_html": ""}
        route.fulfill(json={**base, "agent": agent, "sessions": sessions, "active_count": 0})

    def history(route):
        from urllib.parse import parse_qs, urlsplit

        key = parse_qs(urlsplit(route.request.url).query)["conversation_url"][0].rsplit("/", 1)[-1]
        requested.append(key)
        if key == "late":
            held.append(route)
        elif key == "unavailable":
            route.fulfill(status=503, json={"error": "History service unavailable"})
        else:
            route.fulfill(json={"history": [{"prompt": f"Question {key}", "response": f"Answer {key}",
                                            "response_html": f"<p>Answer {key}</p>"}]})

    page.route("**/api/agent/status", status)
    page.route("**/api/agent/chatgpt-session-history?*", history)

    def default_worker_document(route):
        import re

        response = route.fetch()
        body = re.sub(r'data-agent-run-id="[^"]*"', 'data-agent-run-id="primary"', response.text())
        body = re.sub(r'data-agent-run-revision="[^"]*"', 'data-agent-run-revision="700"', body)
        route.fulfill(response=response, body=body)

    page.route("**/agent/edge/chatgpt", default_worker_document)
    catalog = fixtures._chatgpt_catalog_sessions()
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": catalog,
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        expect(page.locator("#agent_response_question")).to_have_text("Question primary")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        page.locator("[data-execution-session-id=restored]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Question restored")
        expect(page.locator("#agent_response_answer")).to_contain_text("Answer restored")
        page.locator("[data-execution-session-id=unavailable]").click()
        expect(page.locator("#agent_response_status")).to_contain_text("History unavailable")
        page.locator("[data-execution-session-id=late]").click()
        expect(page.locator("#agent_response_status")).to_contain_text("Loading")
        page.locator("[data-execution-session-id=restored]").click()
        expect(page.locator("#agent_response_question")).to_have_text("Question restored")
        assert held
        held.pop().fulfill(json={"history": [{"prompt": "Wrong question", "response": "Wrong answer",
                                            "response_html": "<p>Wrong answer</p>"}]})
        expect(page.locator("#agent_response_question")).to_have_text("Question restored")
        page.reload()
        expect(page.locator("#agent_response_question")).to_have_text("Question restored")
        assert requested.count("late") == 1
        assert requested.count("unavailable") == 1
        assert requested.count("restored") == 3
    finally:
        context.close()


@pytest.mark.parametrize("choice,target", [("platform", "grok"), ("browser", "chrome")])
@pytest.mark.parametrize("width", [1161, 390])
def test_new_session_drafts_are_isolated_by_route(
    disposable_browser, sidebar_server_url, choice, target, width
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    catalog = fixtures._chatgpt_catalog_sessions()
    page.route("**/api/agent/status", lambda route: route.fulfill(json={
        **base, "agent": {"session_id": "new"}, "sessions": [], "active_count": 0, "can_start": True,
    }))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": catalog,
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        prompt = page.locator("#agent_prompt_input")
        prompt.fill("Draft for Edge ChatGPT")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        trigger = page.locator(f'.agent-{choice}-combobox [data-agent-combobox-trigger]')
        trigger.click()
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-option="{target}"]').click()
        expect(prompt).to_have_value("")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        prompt.fill("Draft for the other route")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        trigger.click()
        original = "chatgpt" if choice == "platform" else "edge"
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-option="{original}"]').click()
        expect(prompt).to_have_value("Draft for Edge ChatGPT")
        trigger.click()
        page.locator(f'.agent-{choice}-combobox [data-agent-combobox-option="{target}"]').click()
        expect(prompt).to_have_value("Draft for the other route")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1138, 390])
def test_execution_session_restores_workspace_and_project(disposable_browser, sidebar_server_url, width):
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    agents = {}
    for key in ("primary", "second"):
        agents[key] = {**base["agent"], "session_id": key, "run_id": key,
            "browser": "edge", "platform": "chatgpt", "workspace_path": f"/tmp/{key}",
            "session_mode": "project_session", "project_url": f"https://chatgpt.com/g/g-p-{key}/project",
            "conversation_url": f"https://chatgpt.com/c/{key}", "conversation_bound": True,
            "session_title": f"Task {key}", "running": False, "phase": "failed"}

    def status(route):
        key = route.request.headers.get("x-cachelikes-agent-session", "primary")
        route.fulfill(json={**base, "agent": agents.get(key, {"session_id": key}),
            "sessions": list(agents.values()), "active_count": 0, "can_start": True})

    page.route("**/api/agent/status", status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": {"recent_sessions": [], "projects": []}}))
    page.route("**/api/agent/project-sessions**", lambda route: route.fulfill(json={"sessions": []}))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        for index, key in enumerate(("second", "primary", "second")):
            if index:
                source = page.locator(".agent-session-mode-combobox")
                source.locator("[data-agent-combobox-trigger]").click()
                source.locator('[data-agent-combobox-option="new"]').click()
            page.locator(f"[data-execution-session-id={key}]").click()
            expect(page.locator('input[name="workspace_path"]')).to_have_value(f"/tmp/{key}")
            expect(page.locator('input[name="project_url"]')).to_have_value(agents[key]["project_url"])
            expect(page.locator('input[name="conversation_url"]')).to_have_value(agents[key]["conversation_url"])
            expect(page.locator('input[name="session_mode"]')).to_have_value("project_session")
        page.reload()
        expect(page.locator('input[name="workspace_path"]')).to_have_value("/tmp/second")
        expect(page.locator('input[name="project_url"]')).to_have_value(agents["second"]["project_url"])
    finally:
        context.close()


@pytest.mark.parametrize("width", [1024, 390])
@pytest.mark.parametrize("phase", ["finished", "failed"])
def test_session_diagnostics_stay_collapsed_and_pager_above_composer(disposable_browser, sidebar_server_url, width, phase):
    color_scheme = "dark" if width == 390 else "light"
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1164},
        color_scheme=color_scheme,
    )
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    agent = base["agent"]
    agent.update(session_id="primary", run_id="selected-run", phase=phase, running=False,
        context_file="/tmp/already-cleaned-context.md", event_chain_state="valid",
        last_error="ChatGPT turn timed out." if phase == "failed" else "",
        error_traceback=(
            "Traceback (most recent call last):\nRuntimeError: ChatGPT turn timed out."
            if phase == "failed" else ""
        ), response="Final answer", response_html="<p>Final answer</p>",
        history=[{"prompt": f"Question {i}", "response": f"Answer {i}",
                  "response_html": f"<p>Answer {i}</p>"} for i in range(1, 57)])
    base.update(sessions=[dict(agent)], active_count=0, can_start=True)
    requests = []

    def doctor(route):
        requests.append(route.request.url)
        route.fulfill(json={"run_id": "selected-run", "status": "attention", "checks": [],
            "events": [{"kind": "page.observation", "detail": "Internal diagnostic"}] * 80,
            "actions": ([{"id": "continue", "label": "Continue timed-out task",
                          "description": "Continue in the same conversation.", "enabled": True}]
                        if phase == "failed" else [])})

    page.route("**/api/agent/status", lambda route: route.fulfill(json=base))
    page.route("**/api/agent/doctor", doctor)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "browser": "edge", "platform": "chatgpt",
        "agent_sources": {"recent_sessions": [], "projects": []}}))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        pager = page.locator("#agent_response_pagination")
        expect(pager).to_be_visible()
        panel = page.locator("#agent_doctor_panel")
        if phase == "finished":
            expect(panel).to_be_hidden()
            assert requests == []
        else:
            expect(panel).to_be_visible()
            expect(panel).to_have_js_property("open", False)
            expect(page.locator("#agent_doctor_status")).to_have_text("Can continue")
            technical = page.locator("#agent_error_record")
            expect(technical).not_to_have_attribute("hidden", "")
            expect(technical).to_have_js_property("open", False)
            expect(page.locator(".agent-workspace-content > #agent_error_record")).to_have_count(0)
            warning_style = panel.evaluate('''element => {
                const panelStyle = getComputedStyle(element);
                const summary = element.querySelector("summary");
                const summaryStyle = getComputedStyle(summary);
                const affordanceStyle = getComputedStyle(summary, "::after");
                return {
                    backgroundColor: panelStyle.backgroundColor,
                    borderColor: panelStyle.borderColor,
                    boxShadow: panelStyle.boxShadow,
                    summaryColor: summaryStyle.color,
                    maskImage: affordanceStyle.maskImage,
                    transform: affordanceStyle.transform,
                };
            }''')
            assert warning_style["backgroundColor"] == "rgba(244, 197, 66, 0.12)"
            assert warning_style["borderColor"] == "rgb(244, 197, 66)"
            assert warning_style["boxShadow"] != "none"
            expected_summary_color = "rgb(244, 197, 66)" if color_scheme == "dark" else "rgb(107, 82, 0)"
            assert warning_style["summaryColor"] == expected_summary_color
            assert warning_style["maskImage"] != "none"
            panel.locator(":scope > summary").click()
            expect(panel).to_have_js_property("open", True)
            expect(page.get_by_role("button", name="Continue timed-out task")).to_be_visible()
            expect(technical).to_be_visible()
            technical.locator(":scope > summary").click()
            expect(technical).to_have_js_property("open", True)
            expect(page.locator("#agent_error_record_content")).to_contain_text(
                "RuntimeError: ChatGPT turn timed out."
            )
            page.wait_for_timeout(220)
            open_transform = panel.locator(":scope > summary").evaluate(
                'summary => getComputedStyle(summary, "::after").transform',
            )
            assert open_transform != warning_style["transform"]
            panel.locator(":scope > summary").click()
            expect(panel).to_have_js_property("open", False)
        bounds = page.evaluate('''() => {
            const pager = document.querySelector('#agent_response_pagination').getBoundingClientRect();
            const composer = document.querySelector('.agent-composer-shell').getBoundingClientRect();
            return {bottom:pager.bottom, top:composer.top};
        }''')
        assert bounds["bottom"] <= bounds["top"]
        assert bounds["top"] - bounds["bottom"] < 100
    finally:
        context.close()


def test_remote_history_does_not_inherit_local_session_doctor(
    disposable_browser,
    sidebar_server_url,
) -> None:
    context = disposable_browser.new_context(viewport={"width": 1024, "height": 1164})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    local_url = "https://chatgpt.com/c/local-failed-session"
    remote_url = "https://chatgpt.com/c/remote-audit-session"
    local_agent = {
        **base["agent"],
        "session_id": "primary",
        "run_id": "local-failed-run",
        "phase": "failed",
        "running": False,
        "conversation_url": local_url,
        "conversation_bound": True,
        "last_error": "Local task failed.",
    }
    doctor_requests = []

    page.route(
        "**/api/agent/status",
        lambda route: route.fulfill(
            json={
                **base,
                "agent": local_agent,
                "sessions": [local_agent],
                "active_count": 0,
                "can_start": True,
            }
        ),
    )

    def doctor(route):
        doctor_requests.append(route.request.url)
        route.fulfill(
            json={
                "run_id": "local-failed-run",
                "status": "attention",
                "checks": [],
                "events": [],
                "actions": [],
            }
        )

    page.route("**/api/agent/doctor", doctor)
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "can_download": True,
                "browser": "edge",
                "platform": "chatgpt",
                "agent_sources": {
                    "recent_sessions": [
                        {"url": remote_url, "title": "Remote audit"},
                    ],
                    "projects": [],
                },
            }
        ),
    )
    page.route(
        "**/api/agent/chatgpt-session-history**",
        lambda route: route.fulfill(
            json={
                "title": "Remote audit",
                "history": [
                    {
                        "prompt": "Review the project architecture.",
                        "response": "Audit completed without local changes.",
                        "response_html": "<p>Audit completed without local changes.</p>",
                    }
                ],
            }
        ),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        panel = page.locator("#agent_doctor_panel")
        expect(panel).to_be_visible()
        assert len(doctor_requests) == 1

        page.locator(
            f'[data-recent-conversation-url="{remote_url}"]'
        ).click()

        expect(page.locator("#agent_response_question")).to_have_text(
            "Review the project architecture."
        )
        expect(panel).to_be_hidden()
        expect(page.locator("#agent_error_record")).to_be_hidden()
        page.wait_for_timeout(250)
        assert len(doctor_requests) == 1
    finally:
        context.close()


@pytest.mark.parametrize('width', [1024, 390])
@pytest.mark.parametrize('project', [False, True])
@pytest.mark.parametrize('start_new', [False, True])
def test_completed_session_accepts_followup_in_composer(disposable_browser, sidebar_server_url, width, project, start_new):
    context = disposable_browser.new_context(viewport={'width': width, 'height': 1100})
    page = context.new_page()
    base = fixtures._finished_chatgpt_agent_payload()
    agent = base['agent']
    agent.update(session_id='primary', run_id='completed-run', phase='finished', running=False,
        workspace_path='/tmp/followup', browser='edge', platform='chatgpt',
        conversation_bound=True, conversation_url='https://chatgpt.com/c/existing',
        project_url='https://chatgpt.com/g/g-p-existing/project' if project else '',
        session_title='Completed task')
    other = {**agent, 'session_id': 'second', 'run_id': 'other-run', 'session_title': 'Other completed task'}
    base.update(sessions=[dict(agent), other], active_count=0, can_start=True)
    submitted = []
    page.route('**/api/agent/status', lambda route: route.fulfill(json={**base, 'agent':
        {'session_id': 'new'} if route.request.headers.get('x-cachelikes-agent-session') == 'new'
        else other if route.request.headers.get('x-cachelikes-agent-session') == 'second' else agent}))
    page.route('**/api/browser-session**', lambda route: route.fulfill(json={
        'can_download': True, 'browser': 'edge', 'platform': 'chatgpt',
        'agent_sources': {'recent_sessions': [], 'projects': []}}))

    def ask(route):
        submitted.append((route.request.headers['x-cachelikes-agent-session'], route.request.post_data_json))
        route.fulfill(json={**base, 'agent': {**other, 'running': True, 'phase': 'running', 'run_id': 'followup'}})

    page.route('**/api/agent/ask', ask)
    try:
        page.goto(f'{sidebar_server_url}/agent/edge/chatgpt')
        if width < 900:
            page.locator('#sidebar_toggle').click()
        page.locator('[data-execution-session-id=second]').click()
        expect(page.locator('[data-execution-session-id=second]')).to_have_attribute('aria-pressed', 'true')
        if start_new:
            page.locator('.agent-session-mode-combobox [data-agent-combobox-trigger]').click()
            page.locator('.agent-session-mode-combobox [data-agent-combobox-option="new"]').click()
        if width < 900:
            page.locator('#sidebar_toggle').click()
        page.get_by_placeholder('Do anything', exact=True).fill('Continue with the next step.')
        page.get_by_role('button', name='Ask ChatGPT Web', exact=True).click()
        expect(page.get_by_role('button', name='Stop Agent task', exact=True)).to_be_visible()
        assert len(submitted) == 1
        session, payload = submitted[0]
        assert session == ('new' if start_new else 'second')
        assert payload['prompt'] == 'Continue with the next step.'
        assert payload['conversation_url'] == ('' if start_new else agent['conversation_url'])
        assert payload['project_url'] == ('' if start_new else agent['project_url'])
        assert payload['workspace_path'] == '/tmp/followup'
        assert payload['session_mode'] == ('new' if start_new else 'project_session' if project else 'recent')
    finally:
        context.close()
