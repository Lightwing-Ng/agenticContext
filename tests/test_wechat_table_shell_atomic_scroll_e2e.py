"""Reduced Motion keeps the complete table coordinate system atomic.

Code version: v1.2.0-codex.0
"""

import pytest

from tests import test_wechat_browser_e2e as conversation_browser


disposable_browser = conversation_browser.disposable_browser
wechat_server = conversation_browser.wechat_server


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_table_transform_restore_and_scroll_share_one_coordinate_system(
    disposable_browser, wechat_server, color_scheme
):
    """Sample every structural node before a later frame can hide interpolation."""
    context = disposable_browser.new_context(
        viewport={"width": 390, "height": 900},
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
        samples = page.locator(".wechat-conversations").evaluate("""async shell => {
            await document.fonts.ready;
            const header = shell.querySelector(':scope > [data-table-header]');
            const scroll = shell.querySelector(':scope > [data-table-scroll]');
            const body = scroll.querySelector('[data-table-body]');
            const ancestors = [];
            for (let node = shell.parentElement; node; node = node.parentElement) ancestors.push(node);
            const nodes = [shell, header, scroll, body, ...ancestors,
                ...header.querySelectorAll('th, td'),
                ...body.querySelectorAll(':scope > tbody > tr > :is(th, td)')];
            const geometryProperties = new Set(['transform', 'translate', 'width', 'height',
                'min-width', 'max-width', 'min-height', 'max-height', 'top', 'left', 'right',
                'bottom', 'margin-top', 'margin-bottom', 'margin-left', 'margin-right',
                'padding-top', 'padding-bottom', 'padding-left', 'padding-right',
                'inline-size', 'block-size', 'min-inline-size', 'max-inline-size',
                'min-block-size', 'max-block-size', 'margin-inline-start', 'margin-inline-end',
                'margin-block-start', 'margin-block-end', 'padding-inline-start', 'padding-inline-end',
                'padding-block-start', 'padding-block-end', 'gap', 'row-gap', 'column-gap',
                'scale', 'rotate', 'font-size', 'line-height', 'letter-spacing']);
            const snapshot = phase => ({
                phase,
                shellTransform: getComputedStyle(shell).transform,
                shellWidth: shell.getBoundingClientRect().width,
                headerLeft: header.getBoundingClientRect().left,
                scrollLeft: scroll.scrollLeft,
                scrollMaximum: scroll.scrollWidth - scroll.clientWidth,
                headings: [...header.rows[0].cells].map(cell => cell.getBoundingClientRect().width),
                cells: [...body.tBodies[0].rows[0].cells].map(cell => cell.getBoundingClientRect().width),
                structuralStates: nodes.map(node => ({
                    tag: node.tagName, classes: node.className,
                    transition: getComputedStyle(node).transition,
                    transform: getComputedStyle(node).transform,
                })),
                geometryTransitions: document.getAnimations().filter(animation =>
                    animation instanceof CSSTransition && nodes.includes(animation.effect.target)
                    && geometryProperties.has(animation.transitionProperty)).map(animation => ({
                        tag: animation.effect.target.tagName,
                        classes: animation.effect.target.className,
                        property: animation.transitionProperty,
                        time: animation.currentTime,
                        keyframes: animation.effect.getKeyframes(),
                    })),
            });
            window.wechatTableShellSnapshot = snapshot;
            window.wechatTableShellResizeSamples = [];
            window.addEventListener('resize', () => {
                window.wechatTableShellResizeSamples.push(snapshot('resize-event'));
            });
            const framePair = () => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const output = [snapshot('initial')];
            shell.style.transform = 'scale(0.9)';
            window.dispatchEvent(new Event('resize'));
            output.push(snapshot('scale-assigned'));
            await framePair();
            shell.style.removeProperty('transform');
            window.dispatchEvent(new Event('resize'));
            output.push(snapshot('restore-assigned'));
            await framePair();
            output.push(snapshot('restored-before-scroll'));
            if (scroll.scrollWidth > scroll.clientWidth) {
                await new Promise(resolve => {
                    scroll.addEventListener('scroll', resolve, {once: true});
                    scroll.scrollLeft = scroll.scrollWidth;
                });
            }
            output.push(snapshot('first-scroll-event'));
            return output;
        }""")
        for width in (700, 390):
            page.set_viewport_size({"width": width, "height": 900})
            samples.append(page.evaluate("""async () => {
                await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                return window.wechatTableShellSnapshot('resized');
            }"""))
        samples.extend(page.evaluate("window.wechatTableShellResizeSamples"))
        for sample in samples:
            assert sample["geometryTransitions"] == [], sample
            # Resize delivery precedes the controller's scheduled column synchronization.
            if sample["phase"] != "resize-event":
                assert sample["headings"] == pytest.approx(sample["cells"], abs=1), sample
        restored = next(sample for sample in samples if sample["phase"] == "restored-before-scroll")
        scrolled = next(sample for sample in samples if sample["phase"] == "first-scroll-event")
        assert restored["shellTransform"] == "none", restored
        assert scrolled["scrollLeft"] > 0, scrolled
        assert scrolled["headerLeft"] == pytest.approx(
            restored["headerLeft"] - scrolled["scrollLeft"], abs=1
        ), {"restored": restored, "scrolled": scrolled}
    finally:
        context.close()
