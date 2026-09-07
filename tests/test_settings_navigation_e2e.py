"""Settings cross-document motion. Code version: v1.0.0-codex.1."""

import pytest
from playwright.sync_api import expect
from tests import test_sidebar_e2e as fixtures

disposable_browser = fixtures.disposable_browser
sidebar_server_url = fixtures.sidebar_server_url


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
        page.get_by_role('link', name='Downloads', exact=True).click()
        expect(page).to_have_url(f'{sidebar_server_url}/settings#settings-downloads')
        expect(page.locator('[data-settings-panel=downloads]')).to_be_visible()
        page.wait_for_function('pillFrames.length >= 65')
        transforms = page.evaluate('Array.from(new Set(pillFrames))')
        assert ((len(transforms) > 2) if motion == 'no-preference' else (len(transforms) <= 2)), transforms
    finally:
        context.close()
