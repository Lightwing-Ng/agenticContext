"""Disposable-browser coverage for the ChatGPT Media Sessions index.

Code version: v1.1.2-codex.0
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
from threading import Thread

from PIL import Image
import pytest
from playwright.sync_api import Locator, Page, expect
from werkzeug.serving import make_server

from tests import test_sidebar_e2e as fixtures

disposable_browser = fixtures.disposable_browser

SESSION_COUNT = 26
INDEX_PATH = "/browser?view=media&source=chatgpt&kind=all&q=&sort=newest&session_view=1&session_index=1"
# Landscape, 4:5 portrait, and tall portrait covers; any crop would change the rendered ratio.
LATEST_HEIGHTS = (200, 400, 700)
# This session's catalog omits the image size, so nothing reserves its cover before it loads.
UNSIZED_SESSION = 25


def _latest_width(session_number: int) -> int:
    """Give each session's newest image a distinct width so the shown cover is identifiable."""
    return 300 + session_number


def _latest_size(session_number: int) -> tuple[int, int]:
    return _latest_width(session_number), LATEST_HEIGHTS[session_number % len(LATEST_HEIGHTS)]


@pytest.fixture()
def sessions_server_url(tmp_path: Path) -> Iterator[str]:
    from app.web.app import create_app

    root = tmp_path / "local-store"
    project_dir = root / "media" / "chatgpt" / "demo-project"
    project_dir.mkdir(parents=True)
    entries: dict[str, dict[str, object]] = {}
    base = datetime(2026, 7, 1, tzinfo=UTC)
    for session_number in range(1, SESSION_COUNT + 1):
        image_count = (session_number % 3) + 1
        for image_number in range(1, image_count + 1):
            is_latest = image_number == image_count
            width, height = _latest_size(session_number) if is_latest else (100 + image_number, 400)
            filename = f"s{session_number:02d}-{image_number}.png"
            Image.new("RGB", (width, height), (session_number * 9 % 256, 120, 200)).save(project_dir / filename)
            created_at = base + timedelta(days=session_number, hours=image_number)
            entries[f"file-{filename}"] = {
                "file_id": f"file-{filename}",
                "relative_path": filename,
                "conversation_url": f"https://chatgpt.com/c/session-{session_number:02d}",
                "conversation_title": f"Session {session_number:02d}",
                "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            if session_number != UNSIZED_SESSION:
                entries[f"file-{filename}"].update({"width": width, "height": height})
    (project_dir / ".chatgpt_catalog.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")

    app = create_app(
        root,
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    app.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _column_count(page: Page, selector: str) -> int:
    return page.locator(selector).evaluate(
        "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
    )


def _cover_geometry(card: Locator) -> dict[str, float | str]:
    return card.evaluate(
        """card => {
            const cover = card.querySelector('.browser-session-cover');
            const image = cover.querySelector('img');
            const coverBox = cover.getBoundingClientRect();
            const imageBox = image.getBoundingClientRect();
            return {
                naturalWidth: image.naturalWidth,
                naturalHeight: image.naturalHeight,
                coverLeft: coverBox.left, coverTop: coverBox.top,
                coverRight: coverBox.right, coverBottom: coverBox.bottom,
                coverWidth: coverBox.width, coverHeight: coverBox.height,
                imageLeft: imageBox.left, imageTop: imageBox.top,
                imageRight: imageBox.right, imageBottom: imageBox.bottom,
                imageWidth: imageBox.width, imageHeight: imageBox.height,
                fit: getComputedStyle(image).objectFit,
                titleTop: card.querySelector('.browser-session-card-title').getBoundingClientRect().top,
            };
        }"""
    )


def _assert_uncropped(geometry: dict[str, float | str], cover_width: float | None = None) -> None:
    """The whole image is drawn at the full cover width in its own ratio and inside the cover."""
    natural_ratio = geometry["naturalWidth"] / geometry["naturalHeight"]
    rendered_ratio = geometry["imageWidth"] / geometry["imageHeight"]
    assert abs(rendered_ratio - natural_ratio) <= 0.02 * natural_ratio, geometry
    assert abs(geometry["imageWidth"] - geometry["coverWidth"]) <= 1, geometry
    if cover_width is not None:
        assert round(geometry["coverWidth"]) == cover_width, geometry
    for edge in ("Left", "Top"):
        assert geometry[f"image{edge}"] >= geometry[f"cover{edge}"] - 0.5, geometry
    for edge in ("Right", "Bottom"):
        assert geometry[f"image{edge}"] <= geometry[f"cover{edge}"] + 0.5, geometry
    assert geometry["fit"] == "contain", geometry


def test_sessions_index_shows_each_sessions_latest_image_in_grid_and_list(
    disposable_browser, sessions_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{sessions_server_url}{INDEX_PATH}")

        cards = page.locator("[data-chatgpt-session-card]")
        gallery = page.locator(".browser-session-gallery")
        expect(cards).to_have_count(24)
        expect(gallery).to_have_attribute("data-view", "grid")
        assert _column_count(page, ".browser-session-gallery") == 4
        expect(page.get_by_role("group", name="Media view")).to_be_visible()

        # Sessions are newest first, and each cover is the newest image of its own session.
        first_row = [cards.nth(offset) for offset in range(4)]
        for offset, card in enumerate(first_row):
            session_number = SESSION_COUNT - offset
            expect(card.locator(".browser-session-card-title")).to_have_text(f"Session {session_number:02d}")
            expect(card.locator("img")).to_have_js_property("naturalWidth", _latest_width(session_number))
            image_count = (session_number % 3) + 1
            expect(card.locator(".browser-session-card-count")).to_have_text(
                f"{image_count} image" + ("" if image_count == 1 else "s")
            )
        expect(cards.nth(1).locator("img")).not_to_have_attribute("height", re.compile(r".+"))
        expect(cards.first.locator("img")).to_have_attribute("height", "700")

        # Landscape, portrait, tall, and size-less covers all render whole at the full card width.
        # The row's covers share the tallest image's height, so every title stays aligned.
        geometries = [_cover_geometry(card) for card in first_row]
        for geometry in geometries:
            _assert_uncropped(geometry)
        assert max(g["coverHeight"] for g in geometries) - min(g["coverHeight"] for g in geometries) <= 1
        assert max(g["titleTop"] for g in geometries) - min(g["titleTop"] for g in geometries) <= 1
        tallest = max(geometries, key=lambda g: g["imageHeight"])
        assert abs(tallest["imageHeight"] - tallest["coverHeight"]) <= 1, tallest

        page.get_by_role("button", name="List view").click()
        expect(gallery).to_have_attribute("data-view", "list")
        expect(gallery).to_have_attribute("aria-label", "ChatGPT sessions list")
        assert _column_count(page, ".browser-session-gallery") == 1
        expect(cards.first.locator("img")).to_have_js_property("naturalWidth", _latest_width(SESSION_COUNT))
        for card in first_row:
            _assert_uncropped(_cover_geometry(card), cover_width=112)

        # The layout choice is shared with the media gallery and survives a reload.
        page.reload()
        expect(gallery).to_have_attribute("data-view", "list")
        page.get_by_role("button", name="Grid view").click()
        expect(gallery).to_have_attribute("data-view", "grid")
        assert _column_count(page, ".browser-session-gallery") == 4
        assert not errors
    finally:
        context.close()


def test_session_covers_open_their_session_and_back_link_returns_to_the_same_page(
    disposable_browser, sessions_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 848, "height": 1218})
    try:
        page = context.new_page()
        page.goto(f"{sessions_server_url}{INDEX_PATH}&page=2")
        cards = page.locator("[data-chatgpt-session-card]")
        sessions_link = page.locator("[data-chatgpt-session-index]")
        session_view_button = page.locator("[data-chatgpt-session-view]")

        # 26 sessions leave the two oldest on the second page of covers.
        expect(cards).to_have_count(2)
        expect(sessions_link).to_have_count(0)
        expect(session_view_button).to_have_attribute("aria-pressed", "false")
        expect(cards.first.locator(".browser-session-card-title")).to_have_text("Session 02")

        cards.first.locator("a").click()
        page.wait_for_url(re.compile(r"session=chatgpt:session:demo-project:session-02"))
        expect(page.locator(".browser-session-name-metric strong")).to_have_text("Session 02")
        expect(page.locator(".browser-media-card")).to_have_count((2 % 3) + 1)
        expect(sessions_link).to_have_count(1)
        expect(sessions_link).to_have_text("Back to all sessions")
        expect(sessions_link).to_have_attribute("href", re.compile(r"session_index=1.*page=2"))
        expect(session_view_button).to_have_attribute("aria-pressed", "true")
        assert "session_index" not in page.url

        sessions_link.focus()
        expect(sessions_link).to_be_focused()
        page.keyboard.press("Enter")
        page.wait_for_url(re.compile(r"session_index=1"))
        assert "page=2" in page.url
        expect(cards).to_have_count(2)
        expect(sessions_link).to_have_count(0)
        expect(cards.first.locator(".browser-session-card-title")).to_have_text("Session 02")

        # Session View leaves the index for the one-session-per-page view.
        session_view_button.click()
        page.wait_for_url(lambda url: "session_index" not in url and "session_view=1" in url)
        expect(page.locator("[data-chatgpt-session-card]")).to_have_count(0)
        expect(page.locator(".browser-media-card").first).to_be_visible()
        expect(sessions_link).to_have_text("Back to all sessions")

        # The flat gallery uses the shorter label and the same native index link.
        session_view_button.click()
        page.wait_for_url(re.compile(r"session_view=0"))
        expect(sessions_link).to_have_text("All sessions")
        sessions_link.click()
        page.wait_for_url(re.compile(r"session_index=1"))
        expect(cards).to_have_count(24)
        expect(sessions_link).to_have_count(0)
    finally:
        context.close()


def test_sessions_index_survives_filter_changes_pagination_and_dock_navigation(
    disposable_browser, sessions_server_url,
):
    context = disposable_browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        page.goto(f"{sessions_server_url}{INDEX_PATH}")
        cards = page.locator("[data-chatgpt-session-card]")
        expect(cards).to_have_count(24)
        expect(page.locator('select[name="sort"]')).to_have_value("newest")
        expect(page.locator("label.browser-filter-field", has_text="Session order")).to_be_visible()

        # A sidebar filter change keeps the index instead of dropping back to Session View.
        page.locator('select[name="sort"]').evaluate(
            """select => {
                select.value = "oldest";
                select.dispatchEvent(new Event("change", {bubbles: true}));
            }"""
        )
        page.wait_for_url(re.compile(r"sort=oldest"))
        assert "session_index=1" in page.url
        expect(cards.first.locator(".browser-session-card-title")).to_have_text("Session 01")

        page.locator('[data-pagination-target="2"]').first.click()
        page.wait_for_url(re.compile(r"page=2"))
        assert "session_index=1" in page.url
        expect(cards).to_have_count(2)

        # Leaving for another dock section and returning restores the same index.
        page.locator('[data-dock-section="settings"]').click()
        page.wait_for_url(re.compile(r"/settings"))
        restored = page.locator('[data-dock-section="local-resources"]').get_attribute("href")
        assert restored is not None
        assert "session_index=1" in restored
        assert "sort=oldest" in restored
    finally:
        context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_sessions_index_fits_a_phone_in_grid_and_list(disposable_browser, sessions_server_url, theme):
    context = disposable_browser.new_context(
        viewport={"width": 390, "height": 844},
        color_scheme=theme,
        has_touch=True,
        is_mobile=True,
    )
    try:
        page = context.new_page()
        page.goto(f"{sessions_server_url}{INDEX_PATH}")
        cards = page.locator("[data-chatgpt-session-card]")
        gallery = page.locator(".browser-session-gallery")
        expect(cards).to_have_count(24)
        assert _column_count(page, ".browser-session-gallery") == 2
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        first_box = cards.first.bounding_box()
        assert 0 <= first_box["x"] and first_box["x"] + first_box["width"] <= 390

        for offset in range(2):
            expect(cards.nth(offset).locator("img")).to_have_js_property(
                "naturalWidth", _latest_width(SESSION_COUNT - offset),
            )
            _assert_uncropped(_cover_geometry(cards.nth(offset)))

        page.get_by_role("button", name="List view").click()
        expect(gallery).to_have_attribute("data-view", "list")
        assert _column_count(page, ".browser-session-gallery") == 1
        _assert_uncropped(_cover_geometry(cards.first), cover_width=88)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

        # The back link fits on a phone and remains reachable by touch.
        cards.first.locator("a").tap()
        page.wait_for_url(re.compile(r"session=chatgpt:session:demo-project:session-26"))
        back_link = page.get_by_role("link", name="Back to all sessions")
        expect(back_link).to_be_visible()
        back_box = back_link.bounding_box()
        assert back_box is not None
        assert 0 <= back_box["x"] and back_box["x"] + back_box["width"] <= 390
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        back_link.tap()
        page.wait_for_url(re.compile(r"session_index=1"))
        expect(cards).to_have_count(24)
    finally:
        context.close()
