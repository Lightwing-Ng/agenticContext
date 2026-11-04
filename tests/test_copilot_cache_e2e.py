"""Disposable Chromium acceptance for Copilot's text-only provider adapter.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Page, expect

from tests import test_sidebar_e2e as fixtures


disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


def _open_sidebar(page: Page) -> None:
    page.wait_for_load_state("domcontentloaded")
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")


@pytest.mark.parametrize(("width", "height"), [(1_280, 900), (390, 844)])
def test_media_to_copilot_keeps_safari_and_polling_text_only(
    disposable_browser: Browser, sidebar_server_url: str, macos_host, width: int, height: int,
) -> None:
    context = disposable_browser.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce")
    context.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "logged_in": True, "can_download": True, "account_name": "Signed-in test account", "message": "Ready",
    }))
    page = context.new_page()
    status_requests: list[str] = []
    errors: list[str] = []
    page.on("request", lambda request: status_requests.append(request.url) if "/api/cache/copilot/status" in request.url else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt/media/safari", wait_until="domcontentloaded")
        _open_sidebar(page)
        page.get_by_role("button", name="Cache source: ChatGPT Chat", exact=True).click()
        page.get_by_role("option", name="Microsoft Copilot", exact=True).click()
        expect(page).to_have_url(f"{sidebar_server_url}/cache/copilot/text/safari")
        _open_sidebar(page)
        expect(page.locator('input[name="copilot_browser"]')).to_have_value("safari")
        expect(page.locator('[data-cache-content-mode-option]')).to_have_count(0)
        expect(page.locator("#start_button")).to_be_enabled()
        expect(page.locator(".browser-session-status-account")).to_have_text("Signed-in test account")
        page.reload(wait_until="domcontentloaded")
        expect(page).to_have_url(f"{sidebar_server_url}/cache/copilot/text/safari")
        _open_sidebar(page)
        expect(page.locator('[data-cache-page]')).to_have_attribute("data-cache-page-content-mode", "text")
        expect(page.locator('input[name="cache_content_mode"]').first).to_have_value("text")
        page.get_by_role("button", name="Cache source: Microsoft Copilot", exact=True).click()
        page.get_by_role("option", name="ChatGPT Chat", exact=True).click()
        expect(page).to_have_url(f"{sidebar_server_url}/cache/chatgpt/text/safari")
        assert status_requests
        assert all(parse_qs(urlsplit(url).query).get("content_mode") == ["text"] for url in status_requests)
        assert errors == []
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1_280, 390])
def test_copilot_browser_selection_and_dock_preserve_canonical_path(
    disposable_browser: Browser, sidebar_server_url: str, macos_host, width: int,
) -> None:
    context = disposable_browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
    context.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "logged_in": True, "can_download": True, "account_name": "Signed-in test account", "message": "Ready",
    }))
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/cache/copilot/text/safari")
        _open_sidebar(page)
        page.locator('[data-role="browser-picker-trigger"]').click()
        page.locator('[data-browser-option="edge"]').click()
        expect(page).to_have_url(f"{sidebar_server_url}/cache/copilot/text/edge")
        expect(page.locator('input[name="copilot_browser"]')).to_have_value("edge")
        page.get_by_role("link", name="Browse cached sessions", exact=True).click()
        page.wait_for_load_state("domcontentloaded")
        assert parse_qs(urlsplit(page.url).query)["source"] == ["copilot"]
        _open_sidebar(page)
        page.locator('label[for="browser_view_prompts"]').click()
        page.wait_for_url("**view=prompts**")
        _open_sidebar(page)
        page.locator('label[for="browser_view_text"]').click()
        page.wait_for_url("**view=text**")
        assert parse_qs(urlsplit(page.url).query)["source"] == ["copilot"]
        _open_sidebar(page)
        page.locator('label[for="browser_view_media"]').click()
        page.wait_for_url("**view=media**")
        expect(page.locator('[name="source"]')).to_have_value("chatgpt")
        _open_sidebar(page)
        page.locator('[data-dock-section="cache"]').click()
        expect(page).to_have_url(f"{sidebar_server_url}/cache/copilot/text/edge")
        expect(page.locator('[data-cache-content-mode-option]')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize("width", [1_280, 390])
def test_copilot_progress_requires_verified_full_history_after_reload(
    disposable_browser: Browser, sidebar_server_url: str, macos_host, width: int,
) -> None:
    context = disposable_browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
    context.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "logged_in": True, "can_download": True, "account_name": "Signed-in test account", "message": "Ready",
    }))
    snapshot = {
        "running": True, "phase": "collecting", "discovery_complete": False,
        "queued_tweets": 17, "processed_tweets": 17, "discovered_tweets": 17,
        "downloaded_posts": 17, "downloaded_tweets": 74, "progress_unit": "sessions",
        "performance_metrics": {},
    }
    context.route("**/api/cache/copilot/status**", lambda route: route.fulfill(json=snapshot))
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/cache/copilot/text/safari", wait_until="domcontentloaded")
        progress = page.locator("#status_progress")
        label = page.locator("#status_progress_value")
        detail = page.locator("#status_progress_detail")
        expect(label).to_have_text("Scanning")
        expect(progress).to_have_class("status-progress is-indeterminate")
        expect(page.locator("#status_progress[aria-valuenow]")).to_have_count(0)
        expect(detail).to_contain_text("17 of 17 discovered sessions processed.")
        expect(detail).to_contain_text("The full history total is unknown.")

        snapshot.update(running=False, phase="failed", performance_metrics={"expected_sessions": 571})
        page.reload(wait_until="domcontentloaded")
        expect(label).to_have_text("Partial")
        expect(page.locator("#status_progress[aria-valuenow]")).to_have_count(0)
        expect(detail).to_contain_text("Copilot reports 571 total sessions")
        expect(detail).to_contain_text("Full-history completeness is unverified.")
        expect(detail).not_to_contain_text("100%")

        snapshot.update(phase="idle", queued_tweets=0, processed_tweets=0, performance_metrics={})
        page.reload(wait_until="domcontentloaded")
        expect(label).to_have_text("Partial")
        expect(page.locator("#status_progress[aria-valuenow]")).to_have_count(0)
        expect(detail).to_contain_text("17 sessions cached.")
        expect(detail).to_contain_text("The full history total is unknown.")
        expect(page.locator("#downloaded_tweets")).to_have_text("74")

        snapshot.update(
            phase="finished", discovery_complete=True, queued_tweets=571,
            processed_tweets=571, discovered_tweets=571, downloaded_posts=571,
            performance_metrics={"expected_sessions": 571, "sync_complete": True},
        )
        page.reload(wait_until="domcontentloaded")
        expect(label).to_have_text("100%")
        expect(progress).to_have_attribute("aria-valuenow", "100")
        expect(detail).to_contain_text("571 / 571 sessions processed (100%).")
        expect(detail).not_to_contain_text("unverified")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1_280, 390])
def test_copilot_verified_catalog_still_requires_successful_complete_sync(
    disposable_browser: Browser, sidebar_server_url: str, macos_host, width: int,
) -> None:
    context = disposable_browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
    context.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "logged_in": True, "can_download": True, "account_name": "Signed-in test account", "message": "Ready",
    }))
    snapshot = {
        "running": False, "phase": "failed", "discovery_complete": True,
        "queued_tweets": 17, "processed_tweets": 17, "discovered_tweets": 17,
        "downloaded_posts": 16, "downloaded_tweets": 74, "failed_tweets": 1,
        "progress_unit": "sessions", "performance_metrics": {"sync_complete": False},
    }
    context.route("**/api/cache/copilot/status**", lambda route: route.fulfill(json=snapshot))
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/cache/copilot/text/safari", wait_until="domcontentloaded")
        progress = page.locator("#status_progress")
        label = page.locator("#status_progress_value")
        detail = page.locator("#status_progress_detail")
        expect(label).to_have_text("Partial")
        expect(page.locator("#status_progress[aria-valuenow]")).to_have_count(0)
        expect(detail).to_contain_text("17 / 17 sessions processed; 16 sessions cached; 1 session failure.")
        expect(detail).not_to_contain_text("100%")

        snapshot.update(phase="stopped", failed_tweets=0, downloaded_posts=17)
        page.reload(wait_until="domcontentloaded")
        expect(label).to_have_text("Partial")
        expect(page.locator("#status_progress[aria-valuenow]")).to_have_count(0)
        expect(detail).to_contain_text("17 sessions cached; 0 session failures.")

        snapshot.update(phase="finished", performance_metrics={})
        page.reload(wait_until="domcontentloaded")
        expect(label).to_have_text("Partial")
        expect(detail).to_contain_text("Full-history completion is unverified.")
        expect(page.locator("#status_progress[aria-valuenow]")).to_have_count(0)

        snapshot.update(running=True, phase="downloading", processed_tweets=8)
        page.reload(wait_until="domcontentloaded")
        expect(label).to_have_text("47%")
        expect(progress).to_have_attribute("aria-valuenow", "47")

        snapshot.update(queued_tweets=1_000, processed_tweets=999)
        page.reload(wait_until="domcontentloaded")
        expect(label).to_have_text("99%")
        expect(progress).to_have_attribute("aria-valuenow", "99")

        snapshot.update(queued_tweets=17, processed_tweets=17)
        page.reload(wait_until="domcontentloaded")
        expect(label).to_have_text("Verifying")
        expect(page.locator("#status_progress[aria-valuenow]")).to_have_count(0)

        snapshot.update(running=False, phase="finished", performance_metrics={"sync_complete": True})
        page.reload(wait_until="domcontentloaded")
        expect(label).to_have_text("100%")
        expect(progress).to_have_attribute("aria-valuenow", "100")
    finally:
        context.close()
