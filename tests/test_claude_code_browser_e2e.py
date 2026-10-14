"""Isolated Claude Code cache and Local resources navigation. Code version: v1.0.0-codex.0."""

from collections.abc import Iterator
from threading import Thread
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Page, expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.resource_persistence import CLAUDE_HISTORY_SCHEMA, write_parquet_rows_atomic
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_claude_code_browser import _row


disposable_browser = test_sidebar_e2e.disposable_browser


@pytest.fixture(scope="module")
def claude_code_browser_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    """Serve synthetic web and local transcripts without the user's cache or browsers."""
    from app.web.app import create_app

    sandbox = tmp_path_factory.mktemp("claude-code-browser-e2e")
    root = sandbox / "local-store"
    source_folder = sandbox / "Claude"
    (source_folder / "sessions").mkdir(parents=True)
    for source, text in (("claude", "The web chat prompt."), ("claude_code", "Review the local parser.")):
        write_parquet_rows_atomic(
            root / "llm" / source / "history.parquet",
            [_row(source, "shared-id", text)],
            CLAUDE_HISTORY_SCHEMA,
        )
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig(claude_code_directory=str(source_folder))),
    ):
        application = create_app(
            root,
            computer_use_settings_path=sandbox / "settings/computer-use-agent.json",
            computer_use_runtime_root=sandbox / "computer-use-runtime",
            agent_external_operations_enabled=False,
        )
    application.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    assert server.server_port != 8666
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}", "folder": str(source_folder)}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _set_sidebar(page: Page, *, opened: bool) -> None:
    toggle = page.locator("#sidebar_toggle")
    if (toggle.get_attribute("aria-expanded") == "true") != opened:
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", str(opened).lower())


def _assert_no_horizontal_overflow(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


@pytest.mark.parametrize("width", [1_280, 390])
def test_local_code_source_filter_and_detail_navigation(
    disposable_browser: Browser, claude_code_browser_server: dict[str, str], width: int,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900}, reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    base = claude_code_browser_server["url"]
    try:
        page.goto(f"{base}/browser?view=text&source=claude&session_view=1")
        _set_sidebar(page, opened=True)
        form = page.locator("#browser_filter_form")
        form.locator("[data-browser-source-filter-trigger]").click()
        menu = form.locator("[data-browser-source-filter-menu]")
        expect(menu.get_by_role("option", name="Claude Chat", exact=True)).to_be_visible()
        menu.get_by_role("option", name="Claude Code", exact=True).click()
        page.wait_for_url("**/browser?**source=claude_code**")
        _set_sidebar(page, opened=False)
        expect(page.get_by_role("table", name="Cached Claude Code sessions")).to_be_visible()
        expect(page.locator(".browser-session-table-title")).to_have_text(["Local code session"])
        _assert_no_horizontal_overflow(page)
        page.get_by_role("link", name="Local code session", exact=True).click()
        expect(page.locator("[data-browser-session-actions]")).to_be_visible()
        expect(page.locator("[data-browser-session-open-original]")).to_have_count(0)
        expect(page.get_by_text("Review the local parser.", exact=True).first).to_be_visible()
        page.get_by_role("link", name="Back to all sessions", exact=True).click()
        assert parse_qs(urlsplit(page.url).query)["source"] == ["claude_code"]
        expect(page.locator(".browser-session-table-title")).to_have_text(["Local code session"])
        _set_sidebar(page, opened=True)
        page.locator('label[for="browser_view_prompts"]').click()
        page.wait_for_url("**view=prompts**")
        page.locator('label[for="browser_view_text"]').click()
        page.wait_for_url("**view=text**")
        assert parse_qs(urlsplit(page.url).query)["source"] == ["claude_code"]
        _set_sidebar(page, opened=False)
        expect(page.locator(".browser-session-table-title")).to_have_text(["Local code session"])
        _assert_no_horizontal_overflow(page)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1_280, 390])
def test_local_code_cache_folder_picker_and_dock_memory(
    disposable_browser: Browser, claude_code_browser_server: dict[str, str], width: int,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900}, reduced_motion="reduce",
    )
    context.add_init_script("sessionStorage.setItem('cachelikes:browser-content-mode:v1', 'media');")
    page = context.new_page()
    requests = []
    errors = []
    page.on("request", lambda request: requests.append(request.url))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/browser-session?**", lambda route: route.fulfill(json={"ready": False}))
    base = claude_code_browser_server["url"]
    try:
        page.goto(f"{base}/cache/claude_code")
        _set_sidebar(page, opened=True)
        expect(page.locator("[data-cache-page]")).to_have_attribute("data-cache-page-content-mode", "text")
        expect(page.locator("#start_button")).to_be_enabled()
        expect(page.locator("#start_button")).to_have_text("Read local sessions")
        expect(page.locator("[data-browser-session-panel]")).to_have_count(0)
        expect(page.locator("[data-cache-content-mode-control]")).to_have_count(0)
        expect(page.locator("#claude_code_directory")).to_have_value(claude_code_browser_server["folder"])
        assert not any("/api/browser-session?" in url for url in requests)
        page.get_by_role("button", name="Choose Claude Code session folder", exact=True).click()
        dialog = page.locator("[data-settings-directory-browser]")
        expect(dialog).to_be_visible()
        expect(dialog.locator("[data-directory-browser-path]")).to_have_value(claude_code_browser_server["folder"])
        expect(dialog.locator("[data-directory-browser-list]")).to_contain_text("sessions")
        _assert_no_horizontal_overflow(page)
        page.get_by_role("button", name="Select current folder", exact=True).click()
        expect(dialog).to_be_hidden()
        page.get_by_role("link", name="Browse cached sessions", exact=True).click()
        page.wait_for_url("**/browser?**source=claude_code**")
        _set_sidebar(page, opened=True)
        page.locator('[data-dock-section="cache"]').click()
        page.wait_for_url(f"{base}/cache/claude_code")
        expect(page.locator("#start_button")).to_be_enabled()
        page.locator("[data-cache-source-switcher-trigger]").click()
        expect(page.locator('[data-cache-source-switcher-option="claude"]')).to_contain_text("Claude Chat")
        expect(page.locator('[data-cache-source-switcher-option="claude_code"]')).to_contain_text("Claude Code")
        page.locator('[data-cache-source-switcher-option="claude"]').click()
        page.wait_for_url("**/cache/claude/**")
        page.locator("[data-cache-source-switcher-trigger]").click()
        page.locator('[data-cache-source-switcher-option="claude_code"]').click()
        page.wait_for_url(f"{base}/cache/claude_code")
        expect(page.locator("#start_button")).to_be_enabled()
        expect(page.locator("[data-cache-page]")).to_have_attribute("data-cache-page-content-mode", "text")
        _assert_no_horizontal_overflow(page)
        assert not errors
    finally:
        context.close()
