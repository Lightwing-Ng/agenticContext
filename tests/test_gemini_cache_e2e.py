"""Isolated browser coverage for Gemini Safari Text and Media navigation.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests import test_sidebar_e2e as fixtures


disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


def _assert_gemini_mode(page: Page, server_url: str, mode: str) -> None:
    expect(page).to_have_url(f"{server_url}/cache/gemini/{mode}/safari")
    expect(page.locator(f'[data-cache-content-mode-option="{mode}"]')).to_have_attribute(
        "aria-checked", "true"
    )
    expect(page.locator('input[name="gemini_browser"]')).to_have_value("safari")
    expect(page.locator('#start_form_gemini input[name="cache_content_mode"]')).to_have_value(mode)
    media_metrics = page.locator("#gemini_media_cached")
    text_metrics = page.locator("#downloaded_tweets")
    media_notice = page.get_by_role("note", name="Gemini media cache notice")
    text_notice = page.get_by_role("note", name="Gemini history cache notice")
    if mode == "media":
        expect(media_metrics).to_be_visible()
        expect(media_notice).to_be_visible()
        expect(media_notice).to_contain_text("Rendered images in Safari")
        expect(text_metrics).to_be_hidden()
        expect(text_notice).to_be_hidden()
    else:
        expect(text_metrics).to_be_visible()
        expect(text_notice).to_be_visible()
        expect(media_metrics).to_be_hidden()
        expect(media_notice).to_be_hidden()
    inactive_mode = "text" if mode == "media" else "media"
    expect(page.locator(f'[data-chatgpt-metric-mode="{inactive_mode}"]:visible')).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(("width", "height"), [(1_280, 900), (390, 844)])
def test_gemini_cache_switches_text_and_media_without_losing_safari(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
    width: int,
    height: int,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=width < 768,
        is_mobile=width < 768,
        reduced_motion="reduce",
    )
    context.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(
            json={
                "platform": "gemini",
                "browser": "safari",
                "browser_label": "Safari",
                "logged_in": True,
                "can_download": True,
                "account_name": "Isolated Gemini account",
                "message": "Safari is ready for Gemini.",
            }
        ),
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(
            f"{sidebar_server_url}/cache/gemini/media/safari", wait_until="domcontentloaded"
        )
        _assert_gemini_mode(page, sidebar_server_url, "media")
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
            expect(toggle).to_have_attribute("aria-expanded", "true")

        page.locator('[data-cache-content-mode-option="text"]').click()
        _assert_gemini_mode(page, sidebar_server_url, "text")
        page.locator('[data-cache-content-mode-option="media"]').click()
        _assert_gemini_mode(page, sidebar_server_url, "media")
        page.reload(wait_until="domcontentloaded")
        _assert_gemini_mode(page, sidebar_server_url, "media")
        expect(page.locator(".browser-session-status-account")).to_have_text(
            "Isolated Gemini account"
        )
        if width == 1_280:
            screenshot_path = (
                Path(__file__).resolve().parents[1]
                / "test-results"
                / "gemini-cache-media-isolated.png"
            )
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
        assert errors == []
    finally:
        context.close()
