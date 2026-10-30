"""Disposable Chromium navigation for provider-separated OpenClaw archives.

Code version: v1.0.1-codex.0
"""

from collections.abc import Iterator
from pathlib import Path
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


disposable_browser = test_sidebar_e2e.disposable_browser
SCREENSHOT_ROOT = Path(__file__).resolve().parents[1] / "test-results"


@pytest.fixture(scope="module")
def openclaw_browser_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Serve synthetic history on an ephemeral port without production access."""
    from app.web.app import create_app

    sandbox = tmp_path_factory.mktemp("openclaw-browser-e2e")
    root = sandbox / "local-store"
    for source, label in (
        ("chatgpt_openclaw", "ChatGPT"),
        ("gemini_openclaw", "Gemini"),
        ("doubao_openclaw", "Doubao"),
    ):
        write_parquet_rows_atomic(
            root / "llm" / source / "history.parquet",
            [{
                "schema_version": 1, "platform": source,
                "conversation_id": "synthetic-session", "conversation_url": "",
                "conversation_title": f"Synthetic {label} archive",
                "message_key": "synthetic-message", "turn_index": 0, "message_index": 0,
                "role": "user", "author_label": "You",
                "content_text": f"Synthetic {label} message for isolated browser verification.",
                "content_html": "", "content_sha256": "synthetic", "source_links": [],
                "model_label": "Synthetic model", "first_seen_at": "2026-10-01T00:00:00Z",
                "last_seen_at": "2026-10-01T00:00:00Z",
            }],
            CLAUDE_HISTORY_SCHEMA,
        )
    with patch("app.web.app.SavedConfigStore.load", return_value=SavedConfigStore(CrawlConfig())):
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
        yield f"http://127.0.0.1:{server.server_port}"
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


@pytest.mark.parametrize("width,height", [(1_280, 900), (390, 844)])
def test_archive_selector_browse_and_reader_remain_separate(
    disposable_browser: Browser, openclaw_browser_server: str, width: int, height: int,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height}, reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    requests = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append(request.url))
    try:
        page.goto(f"{openclaw_browser_server}/cache/chatgpt_openclaw")
        _set_sidebar(page, opened=True)
        expect(page.locator("#start_button")).to_have_text("Read archived sessions")
        expect(page.locator("[data-browser-session-panel]")).to_have_count(0)
        page.get_by_role("button", name="Cache source: ChatGPT OpenClaw", exact=True).click()
        expect(page.get_by_role("option", name="ChatGPT", exact=True)).to_be_visible()
        expect(page.get_by_role("option", name="ChatGPT OpenClaw", exact=True)).to_be_visible()
        expect(page.get_by_role("option", name="Doubao OpenClaw", exact=True)).to_have_count(1)
        _assert_no_horizontal_overflow(page)
        SCREENSHOT_ROOT.mkdir(exist_ok=True)
        page.screenshot(path=str(SCREENSHOT_ROOT / f"openclaw-cache-selector-{width}.png"), full_page=True)
        page.get_by_role("option", name="Gemini OpenClaw", exact=True).click()
        expect(page).to_have_url(f"{openclaw_browser_server}/cache/gemini_openclaw")
        expect(page.get_by_role("link", name="Browse cached sessions", exact=True)).to_have_count(0)
        page.locator('[data-dock-section="local-resources"]').click()
        page.wait_for_url("**/browser?**")
        _set_sidebar(page, opened=True)
        form = page.locator("#browser_filter_form")
        form.locator("[data-browser-source-filter-trigger]").click()
        form.get_by_role("option", name="Gemini OpenClaw", exact=True).click()
        page.wait_for_url("**/browser?**source=gemini_openclaw**")
        _set_sidebar(page, opened=False)
        expect(page.get_by_role("table", name="Cached Gemini OpenClaw sessions")).to_be_visible()
        expect(page.locator(".browser-session-table-title")).to_have_text(["Synthetic Gemini archive"])
        page.get_by_role("link", name="Synthetic Gemini archive", exact=True).click()
        expect(page.get_by_role("heading", name="Synthetic Gemini archive", exact=True)).to_be_visible()
        expect(page.get_by_role("article", name="You", exact=True)).to_be_visible()
        expect(page.get_by_text("Synthetic Gemini message for isolated browser verification.", exact=True).first).to_be_visible()
        expect(page.locator("[data-browser-session-open-original]")).to_have_count(0)
        expect(page.locator("[data-browser-session-download-url]")).to_have_count(1)
        _assert_no_horizontal_overflow(page)
        page.screenshot(path=str(SCREENSHOT_ROOT / f"openclaw-synthetic-reader-{width}.png"), full_page=True)
        page.get_by_role("link", name="Back to all sessions", exact=True).click()
        assert parse_qs(urlsplit(page.url).query)["source"] == ["gemini_openclaw"]
        _set_sidebar(page, opened=True)
        form = page.locator("#browser_filter_form")
        form.get_by_role("button", name="Source: Gemini OpenClaw", exact=True).click()
        form.get_by_role("option", name="Doubao OpenClaw", exact=True).click()
        page.wait_for_url("**/browser?**source=doubao_openclaw**")
        _set_sidebar(page, opened=False)
        expect(page.locator(".browser-session-table-title")).to_have_text(["Synthetic Doubao archive"])
        _assert_no_horizontal_overflow(page)
        _set_sidebar(page, opened=True)
        page.locator('[data-dock-section="cache"]').click()
        expect(page).to_have_url(f"{openclaw_browser_server}/cache/gemini_openclaw")
        expect(page.locator("#start_button")).to_have_text("Read archived sessions")
        assert not any("/api/browser-session?" in url for url in requests)
        assert not errors
    finally:
        context.close()
