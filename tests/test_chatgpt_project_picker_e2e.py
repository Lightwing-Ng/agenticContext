"""Saved ChatGPT project identity and icon browser regressions.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from html import escape
import re
from urllib.parse import unquote

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Route, expect

from test_sidebar_e2e import disposable_browser as disposable_browser
from test_sidebar_e2e import sidebar_server_url as sidebar_server_url


PROJECT_ID = "0123456789abcdef0123456789abcdef"
PROJECT_URL = f"https://chatgpt.com/g/g-p-{PROJECT_ID}-studio-current"
PROJECT = {
    "id": PROJECT_ID,
    "title": "Studio project",
    "icon": "dumbbell",
    "icon_color": "#FF2F92",
    "url": PROJECT_URL,
    "updated_at": "2026-09-26T00:00:00Z",
}


def _open_saved_project(
    browser: Browser,
    server_url: str,
    saved_url: str,
    projects: list[dict[str, str]],
    *,
    width: int = 1_006,
    height: int = 791,
    touch: bool = False,
) -> tuple[BrowserContext, Page]:
    context = browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
    )
    context.add_init_script(
        'sessionStorage.setItem("cachelikes:browser-content-mode:v1", "media");'
    )
    context.route(
        "**/api/agent/chatgpt-sources**",
        lambda route: route.fulfill(json={
            "platform": "chatgpt",
            "browser_label": "Safari",
            "recent_sessions": [],
            "projects": projects,
            "limit": 20,
        }),
    )
    context.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(json={
            "platform": "chatgpt",
            "browser": "safari",
            "browser_label": "Safari",
            "logged_in": True,
            "can_download": True,
            "account_name": "Signed in",
            "message": "Ready.",
        }),
    )

    def saved_form_response(route: Route) -> None:
        response = route.fetch()
        body = response.text()
        for field, value in (
            ("chatgpt_project_url", saved_url),
            ("chatgpt_project_name", PROJECT["title"]),
        ):
            body, count = re.subn(
                rf'(<input\b[^>]*\bname="{field}"[^>]*\bvalue=")[^"]*(")',
                lambda match: match[1] + escape(value, quote=True) + match[2],
                body,
            )
            assert count == 1
        route.fulfill(response=response, body=body)

    page = context.new_page()
    page.route(f"{server_url}/cache/chatgpt/media/safari", saved_form_response)
    try:
        page.goto(f"{server_url}/cache/chatgpt/media/safari", wait_until="domcontentloaded")
        if touch:
            page.locator("#sidebar_toggle").click()
        trigger = page.locator("[data-chatgpt-project-trigger]")
        expect(trigger).to_be_enabled()
        expect(trigger).not_to_have_attribute("aria-busy", "true")
        trigger.scroll_into_view_if_needed()
        return context, page
    except BaseException:
        context.close()
        raise


def _assert_custom_project_selected(page: Page) -> None:
    trigger = page.locator("[data-chatgpt-project-trigger]")
    icon = trigger.locator("[data-chatgpt-project-selected-icon]")
    expect(icon).to_be_visible()
    source = icon.get_attribute("src") or ""
    assert source.startswith("data:image/svg+xml;charset=utf-8,")
    svg = unquote(source.split(",", 1)[1])
    assert 'viewBox="0 0 21 20"' in svg
    assert 'color="#FF2F92"' in svg
    assert '<path fill="currentColor" d="M17.794 7.667' in svg
    expect(trigger).to_have_text(PROJECT["title"])
    expect(page.locator('[name="chatgpt_project_url"]')).to_have_value(PROJECT_URL)
    expect(page.locator('[name="chatgpt_project_name"]')).to_have_value(PROJECT["title"])
    expect(page.locator("[data-chatgpt-project-fallback]")).to_have_count(0)
    options = page.locator("[data-chatgpt-project-option]")
    expect(options).to_have_count(2)
    selected = options.filter(has=page.locator("img"))
    expect(selected).to_have_count(1)
    expect(selected).to_have_attribute("aria-selected", "true")
    assert selected.locator("img").get_attribute("src") == source
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_006, 791, False), (390, 844, True), (1_006, 500, False)),
)
@pytest.mark.parametrize(
    "saved_url",
    (
        f"{PROJECT_URL}/project",
        f"https://chatgpt.com/g/g-p-{PROJECT_ID}-studio-before-rename/project",
    ),
    ids=("project-suffix", "renamed-slug"),
)
def test_saved_project_restores_catalog_icon_by_stable_identity(
    disposable_browser: Browser,
    sidebar_server_url: str,
    saved_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    """Restore a saved project and let the current catalog refresh its URL and icon."""
    context, page = _open_saved_project(
        disposable_browser,
        sidebar_server_url,
        saved_url,
        [PROJECT],
        width=width,
        height=height,
        touch=touch,
    )
    try:
        _assert_custom_project_selected(page)
        trigger = page.locator("[data-chatgpt-project-trigger]")
        trigger.click()
        all_media = page.locator(
            '[data-chatgpt-project-option][data-chatgpt-project-url=""]'
        )
        expect(all_media).to_be_visible()
        expect(all_media.locator("img")).to_have_count(0)
        all_media.click()
        expect(trigger).to_have_text("All generated media")
        expect(page.locator('[name="chatgpt_project_url"]')).to_have_value("")
        expect(trigger.locator("[data-chatgpt-project-icon-shell]")).to_be_hidden()
        assert trigger.locator("[data-chatgpt-project-selected-icon]").get_attribute("src") is None
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_project_catalog_aliases_do_not_duplicate_the_saved_project(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Collapse repeated catalog aliases for one stable provider project ID."""
    aliases = [
        PROJECT,
        {**PROJECT, "url": f"{PROJECT_URL}/project"},
        {**PROJECT, "url": f"https://chatgpt.com/g/g-p-{PROJECT_ID}-old-slug"},
        {**PROJECT, "url": f"https://www.chatgpt.com:443/g/g-p-{PROJECT_ID}-studio/project"},
    ]
    context, page = _open_saved_project(
        disposable_browser,
        sidebar_server_url,
        f"{PROJECT_URL}/project",
        aliases,
    )
    try:
        _assert_custom_project_selected(page)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    "saved_url",
    (
        "https://chatgpt.com/g/g-p-fedcba9876543210fedcba9876543210-studio/project",
        PROJECT_URL.replace("https://", "http://"),
        PROJECT_URL.replace("chatgpt.com", "chatgpt.com.example.test"),
        PROJECT_URL.replace("chatgpt.com", "account@chatgpt.com"),
        PROJECT_URL.replace("chatgpt.com", "chatgpt.com:444"),
        f"{PROJECT_URL}/c/unrelated-conversation",
        f"{PROJECT_URL}/project/extra",
    ),
    ids=("different-id", "http", "other-host", "credentials", "port", "chat", "extra-path"),
)
def test_same_title_does_not_replace_a_distinct_or_invalid_saved_project(
    disposable_browser: Browser,
    sidebar_server_url: str,
    saved_url: str,
) -> None:
    """Do not borrow a namesake's icon for a distinct project or invalid project URL."""
    context, page = _open_saved_project(
        disposable_browser,
        sidebar_server_url,
        saved_url,
        [PROJECT],
    )
    try:
        expect(page.locator('[name="chatgpt_project_url"]')).to_have_value(saved_url)
        fallback = page.locator("[data-chatgpt-project-fallback]")
        expect(fallback).to_have_count(1)
        expect(fallback).to_have_attribute("aria-selected", "true")
        expect(page.locator("[data-chatgpt-project-option]")).to_have_count(3)
        catalog_option = page.locator(
            f'[data-chatgpt-project-option][data-chatgpt-project-url="{PROJECT_URL}"]'
        )
        expect(catalog_option).to_have_attribute("aria-selected", "false")
        icon = page.locator("[data-chatgpt-project-selected-icon]")
        expect(icon).to_have_attribute("src", "/static/images/chatgpt-project-terminal.svg")
    finally:
        context.close()
