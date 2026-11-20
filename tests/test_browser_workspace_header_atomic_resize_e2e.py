"""Cache headings and adjacent content retain one responsive coordinate system.

Code version: v1.0.1-codex.0
"""

import pytest

from tests import test_sidebar_e2e as sidebar


disposable_browser = sidebar.disposable_browser
sidebar_server_url = sidebar.sidebar_server_url


@pytest.mark.parametrize("view", ["text", "prompts"])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_workspace_header_and_content_commit_responsive_geometry_together(
    disposable_browser, sidebar_server_url, view, color_scheme
):
    """Audit both sibling cards and their ancestors before layout motion can finish."""
    context = disposable_browser.new_context(
        viewport={"width": 1042, "height": 863},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    try:
        page = context.new_page()
        page.goto(
            sidebar_server_url
            + f"/browser?view={view}&source=chatgpt&kind=all&q=&sort=newest&session_view=1",
            wait_until="networkidle",
        )
        page.evaluate("""async () => {
            await document.fonts.ready;
            const selectors = ['.browser-workspace', '.browser-workspace-header',
                '.browser-summary-card', '.browser-content-card', '.browser-content-toolbar',
                '.browser-heading-row', '.browser-heading-copy', '.browser-summary-card .report-heading'];
            const nodes = new Set();
            for (const selector of selectors) {
                for (let node = document.querySelector(selector); node; node = node.parentElement) nodes.add(node);
            }
            const geometryProperties = new Set(['transform', 'translate', 'width', 'height',
                'min-width', 'max-width', 'min-height', 'max-height', 'top', 'left', 'right',
                'bottom', 'margin-top', 'margin-bottom', 'margin-left', 'margin-right',
                'padding-top', 'padding-bottom', 'padding-left', 'padding-right',
                'inline-size', 'block-size', 'min-inline-size', 'max-inline-size',
                'min-block-size', 'max-block-size', 'margin-inline-start', 'margin-inline-end',
                'margin-block-start', 'margin-block-end', 'padding-inline-start', 'padding-inline-end',
                'padding-block-start', 'padding-block-end', 'gap', 'row-gap', 'column-gap',
                'scale', 'rotate', 'font-size', 'line-height', 'letter-spacing']);
            const centerY = selector => {
                const box = document.querySelector(selector).getBoundingClientRect();
                return box.top + box.height / 2;
            };
            const snapshot = phase => {
                const summary = document.querySelector('.browser-summary-card');
                return {phase, viewport: innerWidth,
                    titleCenter: centerY('.browser-heading-copy'),
                    toggleCenter: centerY('#sidebar_toggle'),
                    themeCenter: centerY('#global_theme_toggle'),
                    summaryPaddingTop: getComputedStyle(summary).paddingTop,
                    summaryWidth: summary.getBoundingClientRect().width,
                    overflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - innerWidth,
                    states: [...nodes].map(node => {
                        const style = getComputedStyle(node);
                        return {tag: node.tagName, classes: node.className,
                            box: node.getBoundingClientRect().toJSON(), padding: style.padding,
                            margin: style.margin, gap: style.gap, transition: style.transition,
                            transform: style.transform};
                    }),
                    geometryTransitions: document.getAnimations().filter(animation =>
                        animation instanceof CSSTransition && nodes.has(animation.effect.target)
                        && geometryProperties.has(animation.transitionProperty)).map(animation => ({
                            tag: animation.effect.target.tagName,
                            classes: animation.effect.target.className,
                            property: animation.transitionProperty,
                            time: animation.currentTime,
                            timing: animation.effect.getComputedTiming(),
                            keyframes: animation.effect.getKeyframes(),
                        })),
                };
            };
            window.browserHeaderAtomicSnapshot = snapshot;
            window.browserHeaderAtomicSamples = [snapshot('initial')];
            window.addEventListener('resize', () => {
                window.browserHeaderAtomicSamples.push(snapshot('resize-event'));
                requestAnimationFrame(() => {
                    window.browserHeaderAtomicSamples.push(snapshot('first-rAF'));
                });
            });
        }""")
        aligned = []
        for width, height in (
            (1042, 863),
            (390, 844),
            (700, 900),
            (1042, 863),
            (390, 844),
            (1042, 863),
        ):
            page.set_viewport_size({"width": width, "height": height})
            page.evaluate("""() => new Promise(resolve =>
                requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
            sidebar._wait_for_global_title_rail(page, ".browser-heading-copy")
            aligned.append(page.evaluate("window.browserHeaderAtomicSnapshot('aligned')"))
        samples = page.evaluate("window.browserHeaderAtomicSamples")
        assert {sample["viewport"] for sample in samples} == {390, 700, 1042}, samples
        for sample in [*samples, *aligned]:
            assert sample["geometryTransitions"] == [], sample
        for sample in aligned:
            assert sample["titleCenter"] == pytest.approx(sample["toggleCenter"], abs=1), sample
            assert sample["titleCenter"] == pytest.approx(sample["themeCenter"], abs=1), sample
            assert sample["summaryPaddingTop"] == ("12px" if sample["viewport"] <= 560 else "10px"), sample
            assert 0 < sample["summaryWidth"] <= 641, sample
            assert sample["overflow"] <= 1, sample
    finally:
        context.close()
