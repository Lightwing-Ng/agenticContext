"""Keep saved Browser scope authoritative while Tunnel performs no provider reads.

Code version: v1.0.0-codex.0
"""

from dataclasses import replace
from threading import Thread
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.web.app import create_app
from tests import test_sidebar_e2e as fixtures


disposable_browser = fixtures.disposable_browser


@pytest.fixture
def saved_chrome_server(tmp_path):
    application = create_app(
        tmp_path / "store",
        computer_use_settings_path=tmp_path / "settings" / "agent.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    settings = application.extensions["computer_use_settings"]
    settings.update(replace(settings.settings, browser="chrome", platform="chatgpt"))
    application.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", settings
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        application.extensions["runtime_shutdown"]()


@pytest.mark.parametrize("width", [1138, 390])
def test_tunnel_preserves_saved_browser_and_rejects_a_foreign_browser_snapshot(
    disposable_browser, saved_chrome_server, width,
):
    """Saved Chrome preferences must not be reset to make an Edge task appear local."""
    url, settings = saved_chrome_server
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    context.route(
        "**/*",
        lambda route: route.continue_()
        if route.request.url.startswith(url + "/") else route.abort(),
    )
    page.add_init_script(
        "sessionStorage.setItem('cachelikes:agent-execution-session:chrome:chatgpt', 'primary')"
    )
    base = fixtures._finished_chatgpt_agent_payload()
    snapshot = {
        **base["agent"], "session_id": "primary", "browser": "edge",
        "workspace_path": settings.settings.workspace_path,
        "run_id": "foreign-edge-run", "run_revision": 1,
        "history": [], "prompt": "", "response": "", "response_html": "",
    }
    requested_browsers, browser_reads, errors = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def status(route):
        requested_browsers.append(route.request.headers["x-cachelikes-agent-browser"])
        route.fulfill(json={
            **base, "agent": dict(snapshot), "sessions": [dict(snapshot)],
            "active_count": 0,
        })

    def reject_browser_read(route):
        browser_reads.append(urlsplit(route.request.url).path)
        route.fulfill(status=503, json={"error": "Tunnel must not read the provider browser."})

    page.route("**/api/agent/status", status)
    for path in (
        "/api/browser-session", "/api/agent/sources", "/api/agent/project-sessions",
        "/api/agent/chatgpt-session-history",
    ):
        page.route(f"**{path}?*", reject_browser_read)
    try:
        with page.expect_response(lambda response: response.url.endswith("/api/agent/status")):
            page.goto(f"{url}/agent/tunnel/chatgpt")
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        expect(page.locator("#agent_connection_tunnel")).to_be_checked()
        expect(page.locator(".agent-browser-combobox [data-agent-combobox-input]")).to_have_value("chrome")
        assert page.locator(".agent-page").get_attribute("data-agent-run-id") != "foreign-edge-run"
        snapshot.update(browser="chrome", run_id="matching-chrome-run", run_revision=2)
        expect(page.locator(".agent-page")).to_have_attribute("data-agent-run-id", "matching-chrome-run")
        assert settings.settings.browser == "chrome"
        assert requested_browsers and set(requested_browsers) == {"chrome"}
        assert browser_reads == []
        assert errors == []
    finally:
        context.close()
