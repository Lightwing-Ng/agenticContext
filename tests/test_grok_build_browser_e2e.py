"""Isolated Grok Build cache and Local resources navigation. Code version: v1.0.0-codex.0."""

from collections.abc import Iterator
import re
from threading import Thread
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Page, expect
from werkzeug.serving import make_server

from app.core.config import CrawlConfig
from app.core.resource_persistence import GROK_HISTORY_SCHEMA, write_parquet_rows_atomic
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_grok_build_browser import _row


disposable_browser = test_sidebar_e2e.disposable_browser


@pytest.fixture(scope="module")
def grok_build_browser_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    """Serve independent web and local sessions without production data or browsers."""
    from app.web.app import create_app

    sandbox = tmp_path_factory.mktemp("grok_build-browser-e2e")
    root = sandbox / "local-store"
    source_folder = sandbox / "Grok Build"
    (source_folder / "sessions").mkdir(parents=True)
    for source, text in (("grok", "The web chat prompt."), ("grok_build", "Review the local parser.")):
        write_parquet_rows_atomic(
            root / "llm" / source / "history.parquet",
            [_row(source, "shared-id", text)],
            GROK_HISTORY_SCHEMA,
        )
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig(grok_build_directory=str(source_folder))),
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
def test_local_grok_build_filter_detail_and_content_mode_navigation(
    disposable_browser: Browser, grok_build_browser_server: dict[str, str], width: int,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900}, reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    base = grok_build_browser_server["url"]
    try:
        page.goto(f"{base}/browser?view=text&source=grok&session_view=1")
        _set_sidebar(page, opened=True)
        form = page.locator("#browser_filter_form")
        form.locator("[data-browser-source-filter-trigger]").click()
        menu = form.locator("[data-browser-source-filter-menu]")
        expect(menu.get_by_role("option", name="Grok Chat", exact=True)).to_be_visible()
        menu.get_by_role("option", name="Grok Build", exact=True).click()
        page.wait_for_url("**/browser?**source=grok_build**")
        _set_sidebar(page, opened=False)
        expect(page.get_by_role("table", name="Cached Grok Build sessions")).to_be_visible()
        expect(page.locator(".browser-session-table-title")).to_have_text(["Local Grok Build session"])
        _assert_no_horizontal_overflow(page)
        page.get_by_role("link", name="Local Grok Build session", exact=True).click()
        expect(page.locator("[data-browser-session-actions]")).to_be_visible()
        expect(page.locator("[data-browser-session-open-original]")).to_have_count(0)
        expect(page.locator("[data-browser-session-refresh-url]")).to_have_count(1)
        expect(page.locator("[data-browser-session-download-url]")).to_have_count(1)
        expect(page.get_by_text("Review the local parser.", exact=True).first).to_be_visible()
        page.get_by_role("link", name="Back to all sessions", exact=True).click()
        assert parse_qs(urlsplit(page.url).query)["source"] == ["grok_build"]
        expect(page.locator(".browser-session-table-title")).to_have_text(["Local Grok Build session"])
        _set_sidebar(page, opened=True)
        page.locator('label[for="browser_view_prompts"]').click()
        page.wait_for_url("**view=prompts**")
        page.locator('label[for="browser_view_text"]').click()
        page.wait_for_url("**view=text**")
        assert parse_qs(urlsplit(page.url).query)["source"] == ["grok_build"]
        _set_sidebar(page, opened=False)
        expect(page.locator(".browser-session-table-title")).to_have_text(["Local Grok Build session"])
        _assert_no_horizontal_overflow(page)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1_280, 390])
def test_grok_build_cache_folder_picker_and_dock_preserve_local_source(
    disposable_browser: Browser, grok_build_browser_server: dict[str, str], width: int,
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
    base = grok_build_browser_server["url"]
    try:
        page.goto(f"{base}/cache/grok_build")
        _set_sidebar(page, opened=True)
        expect(page.locator("[data-cache-page]")).to_have_attribute("data-cache-page-content-mode", "text")
        expect(page.locator("#start_button")).to_be_enabled()
        expect(page.locator("#start_button")).to_have_text("Read local sessions")
        expect(page.locator("[data-browser-session-panel]")).to_have_count(0)
        expect(page.locator("[data-cache-content-mode-control]")).to_have_count(0)
        expect(page.locator("#grok_build_directory")).to_have_value(grok_build_browser_server["folder"])
        assert not any("/api/browser-session?" in url for url in requests)
        page.get_by_role("button", name="Choose Grok Build session folder", exact=True).click()
        dialog = page.locator("[data-settings-directory-browser]")
        expect(dialog).to_be_visible()
        expect(dialog.locator("[data-directory-browser-path]")).to_have_value(grok_build_browser_server["folder"])
        expect(dialog.locator("[data-directory-browser-list]")).to_contain_text("sessions")
        _assert_no_horizontal_overflow(page)
        page.get_by_role("button", name="Select current folder", exact=True).click()
        expect(dialog).to_be_hidden()
        page.locator('[data-dock-section="agent"]').click()
        page.wait_for_url(re.compile(r"/agent(?:/|$)"))
        _set_sidebar(page, opened=True)
        page.locator('[data-dock-section="cache"]').click()
        page.wait_for_url(f"{base}/cache/grok_build")
        expect(page.locator("#start_button")).to_be_enabled()
        page.get_by_role("link", name="Browse cached sessions", exact=True).click()
        page.wait_for_url("**/browser?**source=grok_build**")
        _set_sidebar(page, opened=True)
        page.locator('[data-dock-section="agent"]').click()
        page.wait_for_url(re.compile(r"/agent(?:/|$)"))
        _set_sidebar(page, opened=True)
        page.locator('[data-dock-section="local-resources"]').click()
        page.wait_for_url("**/browser?**source=grok_build**")
        assert parse_qs(urlsplit(page.url).query)["source"] == ["grok_build"]
        expect(page.locator(".browser-session-table-title")).to_have_text(["Local Grok Build session"])
        _set_sidebar(page, opened=True)
        page.locator('[data-dock-section="cache"]').click()
        page.wait_for_url(f"{base}/cache/grok_build")
        expect(page.locator("#start_button")).to_be_enabled()
        page.locator("[data-cache-source-switcher-trigger]").click()
        expect(page.locator('[data-cache-source-switcher-option="grok"]')).to_contain_text("Grok Chat")
        expect(page.locator('[data-cache-source-switcher-option="grok_build"]')).to_contain_text("Grok Build")
        page.locator('[data-cache-source-switcher-option="grok"]').click()
        page.wait_for_url("**/cache/grok/**")
        page.locator("[data-cache-source-switcher-trigger]").click()
        page.locator('[data-cache-source-switcher-option="grok_build"]').click()
        page.wait_for_url(f"{base}/cache/grok_build")
        expect(page.locator("#start_button")).to_be_enabled()
        expect(page.locator("[data-cache-page]")).to_have_attribute("data-cache-page-content-mode", "text")
        _assert_no_horizontal_overflow(page)
        assert not errors
    finally:
        context.close()
