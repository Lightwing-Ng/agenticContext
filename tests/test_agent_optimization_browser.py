"""Disposable-browser verification for OpenAI Site tools registration.

Code version: v1.0.3-codex.1
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from threading import Thread

import pytest
from playwright.sync_api import Browser, Error as PlaywrightError, sync_playwright
from werkzeug.serving import BaseWSGIServer, make_server


REGISTER_TOOL_RECORDER = """
window.__registeredSiteTools = [];
Object.defineProperty(document, "modelContext", {
    configurable: true,
    value: {
        async registerTool(definition) {
            window.__registeredSiteTools.push(definition);
        },
    },
});
"""
EXPECTED_TOOL_NAMES = [
    "get_site_capabilities",
    "get_page_context",
    "navigate_to_site_target",
]


@pytest.fixture(scope="module")
def agent_optimization_server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    from app.web.app import create_app

    sandbox = tmp_path_factory.mktemp("cachelikes-agent-optimization-e2e")
    settings_path = sandbox / "settings" / "computer-use-agent.json"
    runtime_root = sandbox / "computer-use-runtime"
    application = create_app(
        sandbox / "local-store",
        computer_use_settings_path=settings_path,
        computer_use_runtime_root=runtime_root,
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    assert application.extensions["computer_use_settings"]._settings_path == settings_path
    assert application.extensions["computer_use_agent_service"]._runtime_root == runtime_root
    server: BaseWSGIServer = make_server("127.0.0.1", 0, application, threaded=True)
    assert server.server_port != 8666
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def agent_optimization_browser(disposable_browser_launch) -> Iterator[Browser]:
    with sync_playwright() as playwright:
        browser_type = playwright.chromium
        if Path(browser_type.executable_path).is_file():
            browser = disposable_browser_launch(browser_type, headless=True)
        else:
            errors = []
            for channel in ("chrome", "msedge"):
                try:
                    browser = disposable_browser_launch(browser_type, channel=channel, headless=True)
                    break
                except PlaywrightError as error:  # pragma: no cover - host inventory
                    errors.append(f"{channel}: {error}")
            else:  # pragma: no cover - host inventory
                raise AssertionError("No disposable Chromium browser is available. " + " | ".join(errors))
        try:
            yield browser
        finally:
            browser.close()


@pytest.mark.integration
@pytest.mark.slow
def test_site_tools_register_execute_and_navigate_in_a_disposable_browser(
    agent_optimization_browser: Browser,
    agent_optimization_server_url: str,
) -> None:
    context = agent_optimization_browser.new_context(viewport={"width": 1_280, "height": 900})
    page = context.new_page()
    page.add_init_script(REGISTER_TOOL_RECORDER)
    try:
        page.goto(f"{agent_optimization_server_url}/settings", wait_until="domcontentloaded")
        page.wait_for_function("window.__registeredSiteTools?.length === 3")
        assert page.evaluate(
            "window.__registeredSiteTools.map(definition => definition.name)"
        ) == EXPECTED_TOOL_NAMES

        page_context = page.evaluate(
            """async () => {
                const tool = window.__registeredSiteTools.find(
                    definition => definition.name === "get_page_context",
                );
                return tool.execute({});
            }"""
        )
        assert page_context["ok"] is True
        assert page_context["data"]["siteId"] == "agentic-context"
        assert page_context["data"]["route"] == "/settings"
        assert page_context["data"]["matchingTarget"]["id"] == "settings"
        assert page_context["verification"]["contentFieldsRead"] == 0

        navigation_result = page.evaluate(
            """async () => {
                const tool = window.__registeredSiteTools.find(
                    definition => definition.name === "navigate_to_site_target",
                );
                return tool.execute({target: "local_resources"});
            }"""
        )
        assert navigation_result["ok"] is True
        assert navigation_result["verification"]["sameOrigin"] is True
        assert navigation_result["effects"]["directPersistedDataMutation"] is False
        assert navigation_result["effects"]["pageLoadMayUseExistingDataFlows"] is True
        page.wait_for_url(f"{agent_optimization_server_url}/browser")
        page.wait_for_function("window.__registeredSiteTools?.length === 3")
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_unsupported_browser_keeps_the_narrow_human_interface_intact(
    agent_optimization_browser: Browser,
    agent_optimization_server_url: str,
) -> None:
    context = agent_optimization_browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    try:
        page.goto(f"{agent_optimization_server_url}/settings", wait_until="domcontentloaded")
        status = page.evaluate("window.SHARED_AGENT_OPTIMIZATION.boot()")

        assert status["status"] == "unsupported"
        assert page.locator("#agent_optimization_manifest").count() == 1
        assert page.locator("#sidebar_toggle").is_visible()
        assert page.locator("main").is_visible()
        assert page_errors == []
    finally:
        context.close()
