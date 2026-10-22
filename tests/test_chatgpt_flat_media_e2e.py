"""Exercise the read-only All sessions media expansion in a disposable browser.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests import test_sidebar_e2e as browser_fixtures
from tests.test_chatgpt_flat_media_web import (
    ALL_MEDIA_PATH,
    INDEX_PATH,
    seed_flat_media,
)

disposable_browser = browser_fixtures.disposable_browser


@pytest.fixture()
def flat_media_server_url(tmp_path: Path) -> Iterator[str]:
    from app.web.app import create_app

    root = tmp_path / "local-store"
    seed_flat_media(root)
    application = create_app(
        root,
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
def test_all_media_keyboard_switch_expands_images_opens_details_and_fits_viewport(
    disposable_browser, flat_media_server_url, width,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900}, reduced_motion="reduce",
    )
    try:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{flat_media_server_url}{INDEX_PATH}&page=2&session=stale-origin&session_page=7")
        expect(page.locator("[data-chatgpt-session-card]")).to_have_count(3)
        all_media = page.get_by_role("button", name="All media", exact=True)
        all_media.focus()
        expect(all_media).to_be_focused()
        all_media.press("Enter")
        page.wait_for_url(re.compile(r"media_view=all"))
        query = parse_qs(urlsplit(page.url).query)
        assert query["session_index"] == ["1"]
        for field in ("page", "session", "session_page", "media_id"):
            assert field not in query
        gallery = page.locator(".browser-gallery")
        cards = page.locator("[data-media-id]")
        expect(gallery).to_have_attribute("data-view", "all")
        expect(cards).to_have_count(24)
        expect(page.locator("[data-browser-view][aria-pressed='true']")).to_have_count(1)
        expect(all_media).to_have_attribute("aria-pressed", "true")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        dock = page.get_by_role("group", name="Media view").bounding_box()
        assert dock["x"] >= 0 and dock["x"] + dock["width"] <= width
        expect(cards.first.locator("img[data-preview]")).to_have_js_property("complete", True)
        cards.first.locator("[data-media-open]").click()
        dialog = page.locator("#browser_detail_dialog")
        expect(dialog).to_be_visible()
        expect(dialog.locator("[data-viewer-filename]")).not_to_have_text("—")
        expect(dialog.locator("[data-viewer-media] img")).to_have_js_property("complete", True)
        page.get_by_role("button", name="Close image preview", exact=True).click()
        expect(dialog).not_to_be_visible()

        page.get_by_role("button", name="List view", exact=True).click()
        page.wait_for_url(lambda url: "media_view=all" not in str(url))
        expect(page.locator("[data-chatgpt-session-card]")).to_have_count(3)
        expect(gallery).to_have_attribute("data-view", "list")
        expect(page.get_by_role("button", name="List view", exact=True)).to_have_attribute("aria-pressed", "true")
        # History must restore the route's data mode even after choosing a cover layout.
        page.go_back()
        assert "media_view=all" in page.url
        expect(cards).to_have_count(24)
        expect(gallery).to_have_attribute("data-view", "all")
        expect(all_media).to_have_attribute("aria-pressed", "true")
        expect(page.locator("[data-browser-view][aria-pressed='true']")).to_have_count(1)
        page.go_forward()
        assert "media_view=all" not in page.url
        expect(page.locator("[data-chatgpt-session-card]")).to_have_count(3)
        expect(gallery).to_have_attribute("data-view", "list")
        page.reload()
        expect(gallery).to_have_attribute("data-view", "list")
        assert not errors
    finally:
        context.close()


def test_all_media_keeps_pagination_history_search_sort_and_dock_memory(
    disposable_browser, flat_media_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        page.goto(f"{flat_media_server_url}{ALL_MEDIA_PATH}")
        cards = page.locator("[data-media-id]")
        expect(cards).to_have_count(24)
        first_ids = cards.evaluate_all("items => items.map(item => item.dataset.mediaId)")
        page.locator('[data-pagination-target="2"]').first.click()
        page.wait_for_url(re.compile(r"page=2"))
        assert "media_view=all" in page.url
        expect(cards).to_have_count(3)
        assert not set(first_ids) & set(cards.evaluate_all("items => items.map(item => item.dataset.mediaId)"))
        page.go_back()
        expect(cards).to_have_count(24)
        expect(page.get_by_role("button", name="All media", exact=True)).to_have_attribute("aria-pressed", "true")
        page.go_forward()
        expect(cards).to_have_count(3)

        page.locator('select[name="sort"]').evaluate("""select => {
            select.value = "oldest";
            select.dispatchEvent(new Event("change", {bubbles: true}));
        }""")
        page.wait_for_url(re.compile(r"sort=oldest"))
        assert "media_view=all" in page.url
        search = page.get_by_role("combobox", name="Search cached media", exact=True)
        search.fill("image-02.png")
        search.press("Enter")
        page.wait_for_url(re.compile(r"q=image-02.png"))
        assert "media_view=all" in page.url
        expect(cards).to_have_count(1)
        page.locator('[data-dock-section="settings"]').click()
        page.wait_for_url(re.compile(r"/settings"))
        back = page.locator('[data-dock-section="local-resources"]')
        assert "media_view=all" in back.get_attribute("href")
        back.click()
        page.wait_for_url(re.compile(r"media_view=all"))
        expect(cards).to_have_count(1)
        expect(page.locator(".browser-gallery")).to_have_attribute("data-view", "all")
    finally:
        context.close()
