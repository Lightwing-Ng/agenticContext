"""Settings navigation and annotation regressions. Code version: v1.3.0-codex.0."""

import pytest
from playwright.sync_api import expect
from tests import test_sidebar_e2e as fixtures

disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url
seeded_chatgpt_browser_server_url = fixtures.seeded_chatgpt_browser_server_url


@pytest.mark.parametrize("width", [1280, 390])
def test_date_formats_save_and_reach_server_and_browser_rendering(
    disposable_browser,
    monkeypatch,
    tmp_path,
    request,
    width,
):
    monkeypatch.setenv("AGENTIC_CONTEXT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    server = request.getfixturevalue("seeded_chatgpt_browser_server_url")
    context = disposable_browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    try:
        page.goto(f"{server}/settings")
        if width < 900:
            page.locator("#sidebar_toggle").click()
        page.get_by_role("link", name="General", exact=True).click()
        general = page.get_by_role("region", name="Date format")
        expect(general).to_be_visible()
        expect(general.get_by_role("button", name="Full: D Mmm yyyy", exact=True)).to_be_visible()
        full = general.get_by_role("button", name="Full: D Mmm yyyy", exact=True)
        full.press("Enter")
        menu = general.get_by_role("listbox", name="Full")
        expect(menu).to_be_visible()
        menu.get_by_role("option", name="yyyy年mm月dd日").click()
        expect(general.get_by_role("button", name="Full: yyyy年mm月dd日")).to_be_visible()
        expect(general.get_by_text("2026年12月08日", exact=True)).to_be_visible()

        general.get_by_role("button", name="Compact: dd/mm/yyyy", exact=True).press("Enter")
        general.get_by_role("listbox", name="Compact").get_by_role("option", name="yyyy/mm/dd").click()
        expect(general.get_by_role("button", name="Compact: yyyy/mm/dd")).to_be_visible()
        expect(general.get_by_text("2026/12/08", exact=True)).to_be_visible()

        page.goto(f"{server}/browser?view=text&source=chatgpt&sort=newest&session_view=0")
        timestamp = page.locator(".browser-chat-message-header time.browser-session-message-time")
        expect(timestamp).to_contain_text("2026/08/12")
        expect(timestamp).to_have_attribute("datetime", "2026-08-12T05:00:00Z")
        assert page.evaluate("APP_DATES.date('2026-08-12')") == "2026年08月12日"
        assert page.evaluate(
            "APP_DATES.dateTime('2026-08-11T20:00:00Z', {timeZone: 'Asia/Hong_Kong', compact: true})"
        ) == "2026/08/12 04:00:00"
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
    finally:
        context.close()


@pytest.mark.parametrize('width,motion', [(1024, 'no-preference'), (390, 'no-preference'), (1024, 'reduce')])
def test_style_tokens_pill_continues_across_documents(disposable_browser, sidebar_server_url, width, motion):
    context = disposable_browser.new_context(viewport={'width': width, 'height': 1100}, reduced_motion=motion)
    page = context.new_page()
    page.add_init_script('''
        window.pillFrames = [];
        function sample() {
            const nav = document.querySelector('.settings-category-nav');
            if (nav) pillFrames.push(getComputedStyle(nav, '::before').transform);
            if (pillFrames.length < 70) requestAnimationFrame(sample);
        }
        requestAnimationFrame(sample);
    ''')
    try:
        page.goto(f'{sidebar_server_url}/settings#settings-cloud')
        if width < 900:
            page.locator('#sidebar_toggle').click()
        page.get_by_role('link', name='Style tokens', exact=True).click()
        expect(page).to_have_url(f'{sidebar_server_url}/settings/style-tokens')
        expect(page.locator('.settings-category-nav-item-style-tokens')).to_have_attribute('aria-current', 'page')
        page.wait_for_function('pillFrames.length >= 65')
        transforms = page.evaluate('Array.from(new Set(pillFrames))')
        assert ((len(transforms) > 2) if motion == 'no-preference' else (len(transforms) <= 2)), transforms
        page.get_by_label('Settings categories').get_by_role(
            'link', name='Cache', exact=True
        ).click()
        expect(page).to_have_url(f'{sidebar_server_url}/settings#settings-downloads')
        expect(page.locator('[data-settings-panel=downloads]')).to_be_visible()
        page.wait_for_function('pillFrames.length >= 65')
        transforms = page.evaluate('Array.from(new Set(pillFrames))')
        assert ((len(transforms) > 2) if motion == 'no-preference' else (len(transforms) <= 2)), transforms
    finally:
        context.close()


@pytest.mark.parametrize('width', [994, 390])
def test_settings_annotations_share_compact_navigation_and_prompt_type(
    disposable_browser,
    sidebar_server_url,
    width,
):
    context = disposable_browser.new_context(
        viewport={'width': width, 'height': 863},
        reduced_motion='reduce',
    )
    page = context.new_page()
    try:
        page.goto(f'{sidebar_server_url}/settings#settings-agent')
        if width < 900:
            page.locator('#sidebar_toggle').click()

        settings_nav = page.get_by_label('Settings categories')
        expect(settings_nav.get_by_role('link', name='Browser & accounts', exact=True)).to_be_visible()
        expect(settings_nav.get_by_role('link', name='Cache', exact=True)).to_be_visible()
        expect(page.get_by_text('Configure one category at a time.')).to_have_count(0)
        expect(page.get_by_text('Control concurrency, file limits,')).to_have_count(0)

        rows = page.locator('.settings-category-nav-item')
        assert rows.count() == 7
        assert rows.evaluate_all(
            "elements => elements.map(element => element.getBoundingClientRect().height)"
        ) == [36] * 7
        prompt_families = page.locator('.settings-agent-system-prompt').evaluate_all(
            "elements => elements.map(element => getComputedStyle(element).fontFamily)"
        )
        assert len(prompt_families) == 2
        assert all('Univers Next for HSBC' in family for family in prompt_families)
        assert all('monospace' not in family.lower() for family in prompt_families)

        page.goto(f'{sidebar_server_url}/settings/style-tokens')
        if width < 900:
            page.locator('#sidebar_toggle').click()
        active_geometry = page.locator('.settings-category-nav').evaluate(
            """nav => {
                const active = nav.querySelector('.settings-category-nav-item.is-active');
                const pill = getComputedStyle(nav, '::before');
                return {
                    activeHeight: active.getBoundingClientRect().height,
                    activeOffset: active.offsetTop - 2,
                    pillHeight: Number.parseFloat(pill.height),
                    pillOffset: new DOMMatrix(pill.transform).m42,
                    bodyOverflow: document.documentElement.scrollWidth
                        - document.documentElement.clientWidth,
                };
            }"""
        )
        assert active_geometry == {
            'activeHeight': 36,
            'activeOffset': 264,
            'pillHeight': 36,
            'pillOffset': 264,
            'bodyOverflow': 0,
        }
    finally:
        context.close()
