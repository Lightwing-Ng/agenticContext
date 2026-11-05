"""Contact detail stays inside the shared Local resources navigation context.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect

from tests import test_wechat_contact_analysis_e2e as fixtures


contact_analysis_server = fixtures.contact_analysis_server
disposable_browser = fixtures.disposable_browser


def _origin(fixture):
    with sqlite3.connect(fixture.history) as connection:
        message = connection.execute(
            "SELECT id FROM messages WHERE conversation_id=? AND sender_id=? "
            "ORDER BY ordinal DESC LIMIT 1",
            (fixture.session, fixtures.TARGET),
        ).fetchone()[0]
    return (
        "/browser?"
        + urlencode(
            {
                "view": "text",
                "source": "wechat",
                "account": "account:wxid_account_a",
                "chat_kind": "group",
                "session": fixture.session,
                "page": 2,
                "session_page": 3,
                "sort": "oldest",
            }
        )
        + "#wechat_message_"
        + message
    )


def _contact_url(fixture, origin):
    return (
        fixture.url
        + "/browser/wechat/contacts/"
        + fixture.target_id
        + "?"
        + urlencode({"q": "A target", "sort": "newest", "page": 2, "return_to": origin})
    )


def _open_sidebar(page):
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")


def _expect_location(page, predicate):
    page.wait_for_url(predicate)
    assert predicate(page.url)


def _switch_source(page, label):
    _open_sidebar(page)
    page.locator("#browser_filter_form").get_by_role(
        "button", name="Source: " + label, exact=True
    ).click()


@pytest.mark.parametrize("width", [1026, 390])
def test_contact_keeps_shared_source_navigation_sort_and_exact_origin(
    disposable_browser, contact_analysis_server, tmp_path, width
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900}, reduced_motion="reduce"
    )
    context.set_default_timeout(7000)
    context.set_default_navigation_timeout(15000)
    context.add_init_script(
        "if (!sessionStorage.getItem('cachelikes:browser-content-mode:v1')) "
        "sessionStorage.setItem('cachelikes:browser-content-mode:v1', 'media');"
    )
    try:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        origin = _origin(fixture)
        url = _contact_url(fixture, origin)
        page.goto(url, wait_until="domcontentloaded")
        expect(page.locator("[data-contact-analysis-state]")).to_have_attribute(
            "data-contact-analysis-state", "ready"
        )
        assert urlsplit(page.url).path.endswith(fixture.target_id)
        assert "view" not in parse_qs(urlsplit(page.url).query)
        _open_sidebar(page)
        sidebar = page.locator("#browser_sidebar")
        expect(
            sidebar.get_by_role("heading", name="Local resources", exact=True)
        ).to_be_visible()
        expect(sidebar.locator("#browser_view_text")).to_be_checked()
        expect(sidebar.locator("#browser_view_media")).to_have_count(1)
        expect(sidebar.locator("#browser_view_prompts")).to_have_count(1)
        expect(sidebar.locator("#browser_wechat_account")).to_have_value(
            "account:wxid_account_a"
        )
        expect(sidebar.locator("#browser_wechat_kind")).to_have_value("group")
        expect(sidebar.locator('input[name="session"]')).to_have_value(fixture.session)
        page.screenshot(path=str(tmp_path / f"contact-shared-sidebar-{width}.png"))
        page.locator("#sidebar_toggle").click()
        expect(page.locator("#sidebar_toggle")).to_have_attribute(
            "aria-expanded", "false"
        )
        back = page.get_by_role("link", name="Back to conversation", exact=True)
        expect(back).to_have_attribute("title", "Back to conversation")
        expect(back).to_have_text("")
        expect(back.locator(".icon")).to_have_attribute("aria-hidden", "true")
        geometry = back.evaluate("""element => {
            const box = element.getBoundingClientRect();
            const icon = element.querySelector('.icon');
            const glyph = icon.getBoundingClientRect();
            const search = document.querySelector('.wechat-contact-search').getBoundingClientRect();
            return {
                size: [box.width, box.height],
                iconSize: [glyph.width, glyph.height],
                mask: getComputedStyle(icon).maskImage,
                overlap: box.left < search.right && box.right > search.left
                    && box.top < search.bottom && box.bottom > search.top,
                overflow: document.documentElement.scrollWidth > innerWidth,
            };
        }""")
        assert geometry["size"] == [32, 32], geometry
        assert geometry["iconSize"] == [18, 18], geometry
        assert geometry["mask"].endswith('/static/images/arrow.left.svg")'), geometry
        assert not geometry["overlap"] and not geometry["overflow"]
        assert back.get_attribute("href") == origin
        back.focus()
        expect(back).to_be_focused()
        if width == 390:
            back.press("Enter")
        else:
            back.click()
        expect(page).to_have_url(fixture.url + origin)
        page.go_back(wait_until="domcontentloaded")
        expect(page.locator("[data-contact-analysis]")).to_be_visible()
        _open_sidebar(page)
        sort = page.locator('select[name="sort"][form="wechat_contact_filter_form"]')
        expect(sort).to_have_count(1)
        sort.locator("..").locator("[data-browser-filter-select-trigger]").click()
        page.get_by_role("option", name="Oldest first", exact=True).click()
        _expect_location(
            page,
            lambda value: (
                urlsplit(value).path.endswith(fixture.target_id)
                and parse_qs(urlsplit(value).query).get("sort") == ["oldest"]
            ),
        )
        selected = parse_qs(urlsplit(page.url).query)
        assert selected["return_to"] == [origin]
        assert selected["q"] == ["A target"]
        assert "view" not in selected
        assert "page" not in selected
        _switch_source(page, "WeChat")
        page.get_by_role("option", name="Zhihu", exact=True).click()
        _expect_location(
            page,
            lambda value: (
                urlsplit(value).path == "/browser"
                and parse_qs(urlsplit(value).query).get("source") == ["zhihu"]
            ),
        )
        selected = parse_qs(urlsplit(page.url).query)
        assert "session" not in selected and "return_to" not in selected
        expect(page.locator("[data-contact-analysis]")).to_have_count(0)
        _open_sidebar(page)
        expect(page.locator("#browser_answerer_filter")).to_have_count(1)
        _switch_source(page, "Zhihu")
        page.get_by_role("option", name="WeChat", exact=True).click()
        _expect_location(
            page,
            lambda value: (
                urlsplit(value).path == "/browser"
                and parse_qs(urlsplit(value).query).get("source") == ["wechat"]
            ),
        )
        assert errors == []
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert fixture.before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", [1026, 390])
def test_contact_mode_navigation_uses_shared_browser_destination(
    disposable_browser, contact_analysis_server, tmp_path, width
):
    fixture = contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900}, reduced_motion="reduce"
    )
    context.set_default_timeout(7000)
    context.set_default_navigation_timeout(15000)
    try:
        page = context.new_page()
        page.goto(
            _contact_url(fixture, _origin(fixture)), wait_until="domcontentloaded"
        )
        _open_sidebar(page)
        page.locator('label[for="browser_view_media"]').click()
        _expect_location(
            page,
            lambda value: (
                urlsplit(value).path == "/browser"
                and parse_qs(urlsplit(value).query).get("view") == ["media"]
            ),
        )
        selected = parse_qs(urlsplit(page.url).query)
        assert "session" not in selected and "return_to" not in selected
        expect(page.locator("[data-contact-analysis]")).to_have_count(0)
        _open_sidebar(page)
        expect(page.locator("#browser_view_media")).to_be_checked()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()
