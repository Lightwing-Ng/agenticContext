"""WeChat provenance keeps physical motion, native disclosure, and aligned shadows.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests import test_wechat_browser_e2e as fixtures


disposable_browser = fixtures.disposable_browser
wechat_server = fixtures.wechat_server


def _open_conversation(page, base_url):
    page.goto(
        base_url
        + "/browser?view=text&source=wechat&account=source:xlsx-2&chat_kind=group",
        wait_until="networkidle",
    )
    href = page.get_by_role(
        "link", name="Group Name is not type evidence", exact=True
    ).get_attribute("href")
    page.goto(base_url + href, wait_until="networkidle")
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    card = page.locator(".wechat-message").first
    card.locator(".wechat-message-text").evaluate(
        "node => { node.textContent = 'A short archived message.'; }"
    )
    card.scroll_into_view_if_needed()
    return card, card.locator(".wechat-provenance"), card.locator("summary")


def _capture_paused_motions(card):
    """Pause real browser animations before scheduling can hide the initial frame."""
    card.evaluate("""card => {
        const original = Element.prototype.animate;
        window.wechatDisclosureMotions = [];
        Element.prototype.animate = function (...args) {
            const animation = original.apply(this, args);
            if (this === card || card.contains(this)) {
                animation.pause();
                window.wechatDisclosureMotions.push(animation);
            }
            return animation;
        };
    }""")


def _geometry(card):
    return card.evaluate("""card => {
        const details = card.querySelector('details');
        const summary = details.querySelector('summary');
        const box = card.getBoundingClientRect();
        return {
            width: box.width,
            height: box.height,
            detailsHeight: details.getBoundingClientRect().height,
            summaryHeight: summary.getBoundingClientRect().height,
            overflow: document.documentElement.scrollWidth - innerWidth,
        };
    }""")


def _sample_motion(card, fraction, *, shadows=True):
    return card.evaluate(
        """async (card, options) => {
        const motions = window.wechatDisclosureMotions.filter(animation =>
            animation.playState !== 'idle' && animation.playState !== 'finished');
        for (const animation of motions) {
            animation.currentTime = Number(animation.effect.getTiming().duration) * options.fraction;
        }
        // Let ResizeObserver and the effect-layer geometry each present their update.
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const box = card.getBoundingClientRect();
        const details = card.querySelector('details');
        const effect = card.closest('[data-browser-chat-pane]')
            .querySelector(`[data-chat-effect-for="${card.id}"]`);
        const effectBox = effect?.getBoundingClientRect();
        return {
            width: box.width,
            height: box.height,
            detailsHeight: details.getBoundingClientRect().height,
            summaryHeight: details.querySelector('summary').getBoundingClientRect().height,
            shadowError: options.shadows && effectBox
                ? Math.max(...['left', 'top', 'width', 'height'].map(key =>
                    Math.abs(effectBox[key] - box[key]))) : null,
        };
    }""",
        {"fraction": fraction, "shadows": shadows},
    )


def _finish_motions(card):
    card.evaluate("""() => {
        window.wechatDisclosureMotions.forEach(animation => {
            if (animation.playState !== 'idle') animation.finish();
        });
    }""")
    card.page.wait_for_function("""() => window.wechatDisclosureMotions.every(animation =>
        animation.playState === 'finished' || animation.playState === 'idle')""")


def _assert_clean_motion(card):
    assert card.evaluate("""card => {
        const details = card.querySelector('details');
        return card.style.width === '' && card.style.height === ''
            && details.style.height === '' && details.style.overflow === ''
            && !details.querySelector('summary').hasAttribute('aria-expanded')
            && card.getAnimations({subtree: true}).every(animation =>
                animation.playState === 'idle' || animation.playState === 'finished');
    }""")


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_wechat_provenance_springs_open_without_width_snap_or_chevron(
    disposable_browser, wechat_server, width, color_scheme
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        color_scheme=color_scheme,
        reduced_motion="no-preference",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        card, details, summary = _open_conversation(page, wechat_server)
        expect(summary).to_have_accessible_name("Source details")
        assert summary.bounding_box()["height"] >= 32
        assert (
            summary.evaluate("node => getComputedStyle(node, '::after').display")
            == "none"
        )
        closed = _geometry(card)
        _capture_paused_motions(card)
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        page.wait_for_function("window.wechatDisclosureMotions.length > 0")
        expect(summary).to_have_attribute("aria-expanded", "true")
        start = _sample_motion(card, 0)
        middle = _sample_motion(card, 0.15)
        peak = max(
            (_sample_motion(card, step / 40) for step in range(14, 27, 2)),
            key=lambda sample: sample["detailsHeight"],
        )
        end = _sample_motion(card, 1)
        assert start["width"] == pytest.approx(closed["width"], abs=1)
        assert start["detailsHeight"] == pytest.approx(closed["detailsHeight"], abs=1)
        assert closed["detailsHeight"] + 5 < middle["detailsHeight"]
        assert middle["detailsHeight"] < end["detailsHeight"]
        assert peak["detailsHeight"] > end["detailsHeight"] + 1
        if end["width"] > closed["width"] + 2:
            assert closed["width"] < middle["width"] < end["width"]
        for sample in (start, middle, peak, end):
            assert sample["shadowError"] is not None
            assert sample["shadowError"] <= 1, sample
        _finish_motions(card)
        final = _geometry(card)
        assert final["width"] == pytest.approx(end["width"], abs=1)
        assert final["detailsHeight"] == pytest.approx(end["detailsHeight"], abs=1)
        assert final["overflow"] <= 1
        _assert_clean_motion(card)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_wechat_provenance_reverses_from_current_size_and_closes_monotonically(
    disposable_browser, wechat_server, width
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000}, reduced_motion="no-preference"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        card, details, summary = _open_conversation(page, wechat_server)
        closed = _geometry(card)
        _capture_paused_motions(card)
        summary.press("Space")
        page.wait_for_function("window.wechatDisclosureMotions.length > 0")
        partial = _sample_motion(card, 0.15)
        summary.evaluate("node => node.click()")
        expect(summary).to_have_attribute("aria-expanded", "false")
        expect(details).to_have_attribute("open", "")
        reverse_start = _sample_motion(card, 0)
        assert reverse_start["detailsHeight"] == pytest.approx(
            partial["detailsHeight"], abs=1
        )
        closing = [_sample_motion(card, value / 10) for value in range(1, 10)]
        heights = [reverse_start["detailsHeight"]] + [
            sample["detailsHeight"] for sample in closing
        ]
        assert all(after <= before + 0.5 for before, after in zip(heights, heights[1:]))
        assert heights[-1] < heights[0] - 2
        summary.press("Enter")
        expect(summary).to_have_attribute("aria-expanded", "true")
        _finish_motions(card)
        expect(details).to_have_attribute("open", "")
        _assert_clean_motion(card)
        summary.press("Space")
        expect(summary).to_have_attribute("aria-expanded", "false")
        _finish_motions(card)
        expect(details).not_to_have_attribute("open", "")
        assert _geometry(card)["height"] == pytest.approx(closed["height"], abs=1)
        _assert_clean_motion(card)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_wechat_provenance_respects_reduced_motion_and_midflight_change(
    disposable_browser, wechat_server, width
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000}, reduced_motion="reduce"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        card, details, summary = _open_conversation(page, wechat_server)
        assert (
            summary.evaluate("node => getComputedStyle(node).transitionProperty")
            == "none"
        )
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        expect(details.locator(".ui-collapse-body")).to_be_visible()
        _assert_clean_motion(card)
        summary.press("Space")
        expect(details).not_to_have_attribute("open", "")
        expect(details.locator(".ui-collapse-body")).not_to_be_visible()
        _assert_clean_motion(card)
        page.emulate_media(reduced_motion="no-preference")
        _capture_paused_motions(card)
        summary.press("Enter")
        page.wait_for_function("window.wechatDisclosureMotions.length > 0")
        _sample_motion(card, 0.15)
        page.emulate_media(reduced_motion="reduce")
        expect(details.locator(".ui-collapse-body")).to_be_visible()
        page.wait_for_function("""() => window.wechatDisclosureMotions.every(animation =>
            animation.playState === 'idle' || animation.playState === 'finished')""")
        _assert_clean_motion(card)
        assert _geometry(card)["overflow"] <= 1
        assert not errors
    finally:
        context.close()


def test_wechat_provenance_motion_does_not_require_scroll_timeline(
    disposable_browser, wechat_server
):
    context = disposable_browser.new_context(
        viewport={"width": 812, "height": 1000}, reduced_motion="no-preference"
    )
    context.add_init_script(
        "Object.defineProperty(window, 'ScrollTimeline', {value: undefined, configurable: true});"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        card, details, summary = _open_conversation(page, wechat_server)
        _capture_paused_motions(card)
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        page.wait_for_function("window.wechatDisclosureMotions.length > 0")
        first = _sample_motion(card, 0, shadows=False)
        middle = _sample_motion(card, 0.2, shadows=False)
        assert middle["detailsHeight"] > first["detailsHeight"] + 5
        _finish_motions(card)
        _assert_clean_motion(card)
        assert not errors
    finally:
        context.close()


def test_wechat_provenance_shadows_follow_presented_spring_frames(
    disposable_browser, wechat_server
):
    context = disposable_browser.new_context(
        viewport={"width": 812, "height": 1000}, reduced_motion="no-preference"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        card, details, _ = _open_conversation(page, wechat_server)
        samples = card.evaluate("""async card => {
            const samples = [];
            const pane = card.closest('[data-browser-chat-pane]');
            const effect = pane.querySelector(`[data-chat-effect-for="${card.id}"]`);
            const started = performance.now();
            card.querySelector('summary').click();
            await new Promise(resolve => {
                const sample = () => requestAnimationFrame(() => setTimeout(() => {
                    const animations = card.getAnimations({subtree: true});
                    const running = animations.some(animation => animation.playState === 'running');
                    const actual = card.getBoundingClientRect();
                    const shadow = effect.getBoundingClientRect();
                    if (running) samples.push({
                        height: actual.height,
                        error: Math.max(...['left', 'top', 'width', 'height'].map(key =>
                            Math.abs(actual[key] - shadow[key]))),
                    });
                    if (running && performance.now() - started < 2000) sample();
                    else resolve();
                }, 0));
                sample();
            });
            return samples;
        }""")
        assert len(samples) >= 2, samples
        assert (
            max(sample["height"] for sample in samples)
            > min(sample["height"] for sample in samples) + 5
        )
        assert max(sample["error"] for sample in samples) <= 1, samples
        expect(details).to_have_attribute("open", "")
        _assert_clean_motion(card)
        assert not errors
    finally:
        context.close()
