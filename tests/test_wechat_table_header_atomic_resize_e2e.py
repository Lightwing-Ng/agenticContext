"""Controller-measured table headers retain body geometry in the first painted frame.

Code version: v1.0.0-codex.0
"""

import pytest

from tests import test_wechat_browser_e2e as conversation_browser


disposable_browser = conversation_browser.disposable_browser
wechat_server = conversation_browser.wechat_server


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_reduced_motion_header_width_updates_match_body_without_interpolation(
    disposable_browser, wechat_server, color_scheme
):
    """Observe actual controller writes before a later frame can hide width interpolation."""
    context = disposable_browser.new_context(
        viewport={"width": 1280, "height": 900},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    try:
        page = context.new_page()
        page.goto(
            wechat_server + "/browser?view=text&source=wechat",
            wait_until="networkidle",
        )
        if page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true":
            page.locator("#sidebar_toggle").click()
        page.evaluate("""async () => {
            await document.fonts.ready;
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const shell = document.querySelector('.wechat-conversations');
            const header = shell.querySelector(':scope > table[data-table-header]');
            const scroll = shell.querySelector(':scope > [data-table-scroll]');
            const body = scroll.querySelector('table[data-table-body]');
            window.wechatHeaderWidthSamples = [];
            window.wechatHeaderWidthObserver = new MutationObserver(() => {
                const scrollbar = scroll.offsetWidth - scroll.clientWidth;
                const bodyWidths = [...body.tBodies[0].rows[0].cells].map((cell, index, cells) =>
                    cell.getBoundingClientRect().width + (index === cells.length - 1 ? scrollbar : 0));
                const headings = [...header.rows[0].cells];
                const assigned = headings.map(cell => parseFloat(cell.style.width));
                // Compare only complete writes for the current body size, excluding an old
                // observer notification delivered after an unrelated ancestor resize.
                if (!assigned.every((width, index) => Math.abs(width - bodyWidths[index]) < 0.05)) return;
                window.wechatHeaderWidthSamples.push({
                    body: bodyWidths,
                    assigned,
                    rendered: headings.map(cell => cell.getBoundingClientRect().width),
                    headerAssigned: parseFloat(header.style.width),
                    headerRendered: header.getBoundingClientRect().width,
                    bodyRendered: body.getBoundingClientRect().width,
                });
            });
            window.wechatHeaderWidthObserver.observe(header, {
                attributes: true, subtree: true, attributeFilter: ['style'],
            });
        }""")
        for width in (1050, 1280, 1000, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            page.evaluate("""() => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
        samples = page.evaluate("""() => {
            window.wechatHeaderWidthObserver.disconnect();
            return window.wechatHeaderWidthSamples;
        }""")
        assert len({round(sample["headerAssigned"]) for sample in samples}) >= 3, samples
        for sample in samples:
            assert sample["headerRendered"] == pytest.approx(
                sample["bodyRendered"], abs=1
            ), sample
            assert sample["rendered"] == pytest.approx(sample["body"], abs=1), sample
    finally:
        context.close()
