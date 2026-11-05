"""Forwarded disclosures reuse measured springs without losing native interaction.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from tests import test_wechat_forwarded_e2e as forwarded_fixtures
from tests import test_wechat_nested_forwarded_e2e as nested_fixtures


disposable_browser = forwarded_fixtures.disposable_browser
forwarded_server = forwarded_fixtures.forwarded_server
forwarded_activity_store = nested_fixtures.forwarded_activity_store
nested_server = nested_fixtures.nested_server


def _settle(card):
    card.evaluate("""async card => {
        await new Promise(requestAnimationFrame);
        const ancestors = [];
        for (let element = card; element; element = element.parentElement) ancestors.push(element);
        // The shell's sidebar gel may still transform the entire message subtree.
        const effects = new Set([...card.getAnimations({subtree: true}),
            ...ancestors.flatMap(element => element.getAnimations())]);
        await Promise.allSettled([...effects].filter(animation =>
            animation.timeline === document.timeline
            && Number.isFinite(animation.effect.getTiming().iterations))
            .map(animation => animation.finished));
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")


def _capture(card):
    """Pause actual WAAPI effects before a busy scheduler can hide first frames."""
    card.evaluate("""card => {
        const animate = Element.prototype.animate;
        window.forwardedMotions = [];
        Element.prototype.animate = function (...args) {
            const animation = animate.apply(this, args);
            if (this === card || card.contains(this)) {
                animation.pause();
                window.forwardedMotions.push(animation);
            }
            return animation;
        };
    }""")


def _sample(disclosure, fraction):
    return disclosure.evaluate(
        """async (details, fraction) => {
            const active = window.forwardedMotions.filter(animation =>
                animation.playState !== 'idle' && animation.playState !== 'finished');
            for (const animation of active) {
                animation.currentTime = Number(animation.effect.getTiming().duration) * fraction;
            }
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const card = details.parentElement;
            const box = card.getBoundingClientRect();
            const shadow = card.closest('[data-browser-chat-pane]')
                .querySelector(`[data-chat-effect-for="${card.id}"]`)?.getBoundingClientRect();
            const row = card.parentElement;
            const time = row.querySelector(':scope > time')?.getBoundingClientRect();
            const slot = card.querySelector(':scope > details.wechat-provenance .wechat-time-slot')
                ?.getBoundingClientRect();
            return {
                height: details.getBoundingClientRect().height,
                layoutHeight: Number.parseFloat(getComputedStyle(details).height),
                cardWidth: box.width,
                cardHeight: box.height,
                cardTransform: getComputedStyle(card).transform,
                summaryHeight: details.querySelector(':scope > summary').getBoundingClientRect().height,
                durations: active.map(animation => Number(animation.effect.getTiming().duration)),
                shadowError: shadow ? Math.max(...['left','top','width','height'].map(key =>
                    Math.abs(shadow[key] - box[key]))) : null,
                clockError: time && slot ? Math.abs(time.top - slot.top) : null,
                overflow: document.documentElement.scrollWidth - innerWidth,
            };
        }""",
        fraction,
    )


def _finish(card):
    card.evaluate("""() => {
        window.forwardedMotions.forEach(animation => {
            if (animation.playState !== 'idle') animation.finish();
        });
    }""")
    card.page.wait_for_function("""() => window.forwardedMotions.every(animation =>
        animation.playState === 'idle' || animation.playState === 'finished')""")
    # Finished promises clear motion state before the effects observer paints its cleanup.
    card.evaluate("""() => new Promise(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)))""")


def _assert_clean(disclosure):
    state = disclosure.evaluate("""details => {
        const body = details.querySelector(':scope > .ui-collapse-body');
        return {motion: details.getAttribute('data-collapse-motion'),
            expanded: details.querySelector(':scope > summary').getAttribute('aria-expanded'),
            inert: body.inert, height: details.style.height, overflow: details.style.overflow,
            width: details.parentElement.style.width,
            animations: (window.forwardedMotions || [])
                .filter(animation => animation.playState !== 'idle' && animation.playState !== 'finished')
                .map(animation => ({state: animation.playState,
                    target: animation.effect.target?.outerHTML?.slice(0, 100),
                    duration: animation.effect.getTiming().duration}))};
    }""")
    assert state == {
        "motion": None,
        "expanded": None,
        "inert": False,
        "height": "",
        "overflow": "",
        "width": "",
        "animations": [],
    }


def _child_effects(disclosure):
    """Read the painted effect clip separately from a child's unchanged card box."""
    return disclosure.evaluate(r"""details => {
        const clip = details.getBoundingClientRect();
        const pane = details.closest('[data-browser-chat-pane]');
        return [...details.querySelectorAll('.browser-chat-message')]
            .filter(card => !card.closest('details:not([open])'))
            .map(card => {
                const box = card.getBoundingClientRect();
                const effect = pane.querySelector(`[data-chat-effect-for="${card.id}"]`);
                const clipped = effect ? getComputedStyle(effect).clipPath : null;
                const intersection = {left: Math.max(box.left, clip.left),
                    right: Math.min(box.right, clip.right),
                    top: Math.max(box.top, clip.top), bottom: Math.min(box.bottom, clip.bottom)};
                const outside = intersection.right <= intersection.left || intersection.bottom <= intersection.top;
                const partial = !outside && (box.top < clip.top - 1 || box.bottom > clip.bottom + 1
                    || box.left < clip.left - 1 || box.right > clip.right + 1);
                let painted = null;
                const match = clipped?.match(/^inset\(([^)]+)\)$/);
                if (effect && match) {
                    const values = match[1].split(/\s+/).map(Number.parseFloat);
                    if (values.every(Number.isFinite) && values.length >= 1 && values.length <= 4) {
                        const [top, right = top, bottom = top, left = right] = values;
                        const rect = effect.getBoundingClientRect();
                        painted = {left: rect.left + left, right: rect.right - right,
                            top: rect.top + top, bottom: rect.bottom - bottom};
                    }
                }
                return {id: card.id, hidden: effect?.hidden, clipPath: clipped,
                    outside, partial, painted,
                    clip: {left: clip.left, right: clip.right, top: clip.top, bottom: clip.bottom}};
            });
    }""")


def _assert_active_child_clips(frames):
    assert any(item["outside"] for frame in frames for item in frame)
    assert any(item["partial"] for frame in frames for item in frame)
    for frame in frames:
        for item in frame:
            if item["outside"]:
                assert item["hidden"] is True, item
            if item["partial"]:
                assert item["hidden"] is False, item
                assert item["painted"] is not None, item
                assert item["painted"]["left"] >= item["clip"]["left"] - 1, item
                assert item["painted"]["right"] <= item["clip"]["right"] + 1, item
                assert item["painted"]["top"] >= item["clip"]["top"] - 1, item
                assert item["painted"]["bottom"] <= item["clip"]["bottom"] + 1, item


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_forwarded_spring_opens_closes_and_reverses_from_presented_geometry(
    disposable_browser, forwarded_server, tmp_path, width, color_scheme
):
    fixture = forwarded_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        color_scheme=color_scheme,
        reduced_motion="no-preference",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        card, details = forwarded_fixtures.open_forwarded(page, fixture)
        _settle(card)
        summary = details.locator(":scope > summary")
        expect(summary).to_have_text("Chat history")
        closed = details.bounding_box()["height"]
        closed_layout = details.evaluate(
            "details => Number.parseFloat(getComputedStyle(details).height)"
        )
        _capture(card)
        summary.press("Enter")
        expect(details).to_have_attribute("data-collapse-motion", "open")
        expect(summary).to_have_attribute("aria-expanded", "true")
        first = _sample(details, 0)
        first_effects = _child_effects(details)
        middle = _sample(details, 0.15)
        middle_effects = _child_effects(details)
        (tmp_path / "active-child-effects.json").write_text(
            json.dumps([first_effects, middle_effects]) + "\n"
        )
        _assert_active_child_clips([first_effects, middle_effects])
        peak = max(
            (_sample(details, value / 40) for value in range(14, 27, 2)),
            key=lambda sample: sample["height"],
        )
        end = _sample(details, 1)
        (tmp_path / "opening-dimensions.json").write_text(
            json.dumps(
                {
                    "closed": closed,
                    "closed_layout": closed_layout,
                    "frames": [first, middle, peak, end],
                }
            )
            + "\n"
        )
        assert first["height"] == pytest.approx(closed, abs=1)
        assert closed + 5 < middle["height"] < end["height"]
        assert peak["height"] > end["height"] + 1
        assert first["durations"] and set(first["durations"]) == {620}
        for sample in (first, middle, peak, end):
            assert sample["cardTransform"] == "none", sample
            assert sample["shadowError"] is not None and sample["shadowError"] <= 1
            assert sample["clockError"] is not None and sample["clockError"] <= 1
        _finish(card)
        _assert_clean(details)
        settled_effects = _child_effects(details)
        assert all(
            item["hidden"] is False and item["clipPath"] == "none"
            for item in settled_effects
        ), settled_effects
        children = details.locator("[data-wechat-embedded-row]")
        expect(children).to_have_count(2)
        for child, text in zip(children.all(), forwarded_fixtures.ORIGINALS):
            expect(child.locator(".wechat-message-text")).to_have_text(text)
        page.screenshot(path=tmp_path / f"forwarded-open-{width}-{color_scheme}.png")

        summary.press("Space")
        expect(summary).to_have_attribute("aria-expanded", "false")
        close_start = _sample(details, 0)
        partial = _sample(details, 0.15)
        assert closed < partial["height"] < close_start["height"]
        # Reverse while the content is still visible; this must not restart at an endpoint.
        summary.evaluate("node => node.click()")
        expect(summary).to_have_attribute("aria-expanded", "true")
        reverse_start = _sample(details, 0)
        assert reverse_start["height"] == pytest.approx(partial["height"], abs=1)
        assert reverse_start["cardWidth"] == pytest.approx(partial["cardWidth"], abs=1)
        _finish(card)
        summary.press("Space")
        expect(details).to_have_attribute("open", "")
        heights = [_sample(details, value / 10)["height"] for value in range(11)]
        assert all(right <= left + 0.5 for left, right in zip(heights, heights[1:]))
        _finish(card)
        expect(details).not_to_have_attribute("open", "")
        assert details.bounding_box()["height"] == pytest.approx(closed, abs=1)
        _assert_clean(details)
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
        assert fixture.history.read_bytes() == fixture.preserved
        assert not errors
        (tmp_path / "motion-geometry.json").write_text(
            json.dumps({"open": [first, middle, peak, end], "closing_heights": heights})
            + "\n"
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_forwarded_reduced_motion_settles_immediately_even_when_changed_midflight(
    disposable_browser, forwarded_server, width
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000}, reduced_motion="reduce"
    )
    page = context.new_page()
    try:
        card, details = forwarded_fixtures.open_forwarded(page, forwarded_server)
        summary = details.locator(":scope > summary")
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        expect(details.locator(":scope > .wechat-forwarded-body")).to_be_visible()
        _assert_clean(details)
        summary.press("Space")
        expect(details).not_to_have_attribute("open", "")
        _assert_clean(details)
        page.emulate_media(reduced_motion="no-preference")
        _capture(card)
        summary.press("Enter")
        expect(details).to_have_attribute("data-collapse-motion", "open")
        _sample(details, 0.15)
        page.emulate_media(reduced_motion="reduce")
        expect(details).to_have_attribute("open", "")
        page.wait_for_function("""() => window.forwardedMotions.every(animation =>
            animation.playState === 'idle' || animation.playState === 'finished')""")
        _assert_clean(details)
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_nested_forwarded_springs_follow_parent_shadow_and_retain_outer_clock_rail(
    disposable_browser, nested_server, tmp_path, width
):
    fixture = nested_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1100}, reduced_motion="no-preference"
    )
    page = context.new_page()
    try:
        card = nested_fixtures._open(page, fixture)
        _settle(card)
        _capture(card)
        outer = card.locator(":scope > details[data-wechat-forwarded]")
        outer.locator(":scope > summary").press("Enter")
        expect(outer).to_have_attribute("data-collapse-motion", "open")
        _finish(card)
        child = page.locator("#wechat_message_" + fixture.parent + "_forwarded_1")
        nested = child.locator(":scope > details[data-wechat-forwarded]")
        nested.locator(":scope > summary").press("Enter")
        expect(nested).to_have_attribute("data-collapse-motion", "open")
        first, middle, end = (_sample(nested, value) for value in (0, 0.15, 1))
        assert first["height"] + 5 < middle["height"] < end["height"]
        assert set(first["durations"]) == {620}
        for sample in (first, middle, end):
            assert sample["shadowError"] is not None and sample["shadowError"] <= 1
            assert sample["clockError"] is not None and sample["clockError"] <= 1
        _finish(card)
        deepest = page.locator(
            "#wechat_message_" + fixture.parent + "_forwarded_1_forwarded_1"
        ).locator(":scope > details[data-wechat-forwarded]")
        deepest.locator(":scope > summary").press("Enter")
        _finish(card)
        nested_fixtures._assert_clock_rail(page, fixture)
        expect(
            card.locator(":scope > .wechat-forwarded > .wechat-forwarded-body")
        ).to_be_visible()
        assert fixture.history.read_bytes() == fixture.preserved
        page.screenshot(path=tmp_path / f"nested-motion-{fixture.kind}-{width}.png")
        (tmp_path / "nested-motion-geometry.json").write_text(
            json.dumps({"first": first, "middle": middle, "end": end}) + "\n"
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("nested_server", ["native"], indirect=True)
def test_opening_a_child_during_parent_motion_settles_only_its_ancestor(
    disposable_browser, nested_server, width
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1100}, reduced_motion="no-preference"
    )
    page = context.new_page()
    try:
        card = nested_fixtures._open(page, nested_server)
        _settle(card)
        _capture(card)
        outer = card.locator(":scope > details[data-wechat-forwarded]")
        outer.locator(":scope > summary").press("Enter")
        _sample(outer, 0.15)
        child = page.locator("#wechat_message_" + nested_server.parent + "_forwarded_1")
        inner = child.locator(":scope > details[data-wechat-forwarded]")
        # A nested activation can arrive before its ancestor has finished unfolding.
        inner.locator(":scope > summary").evaluate("node => node.click()")
        expect(inner).to_have_attribute("data-collapse-motion", "open")
        expect(outer).not_to_have_attribute("data-collapse-motion", "open")
        first, middle = (_sample(inner, value) for value in (0, 0.15))
        assert middle["height"] > first["height"] + 5
        _finish(card)
        expect(inner).to_have_attribute("open", "")
        dimensions = inner.evaluate("""details => {
            const inner = details.getBoundingClientRect();
            const outer = details.parentElement.closest('details[data-wechat-forwarded]')
                .getBoundingClientRect();
            return {innerBottom: inner.bottom, outerBottom: outer.bottom,
                overflow: document.documentElement.scrollWidth - innerWidth,
                viewport: {innerWidth, clientWidth: document.documentElement.clientWidth,
                    scrollWidth: document.documentElement.scrollWidth,
                    bodyClientWidth: document.body.clientWidth,
                    bodyScrollWidth: document.body.scrollWidth},
                phase: 'after-finished-state-and-two-render-frames',
                outsideRight: [...document.querySelectorAll('body *')].map(node => {
                    const box = node.getBoundingClientRect();
                    return {tag: node.tagName, id: node.id,
                        classes: typeof node.className === 'string' ? node.className : '',
                        left: box.left, right: box.right, width: box.width,
                        transform: getComputedStyle(node).transform};
                }).filter(box => box.width > 0 && box.right > innerWidth + 1).slice(0, 12),
                animations: document.getAnimations().filter(animation =>
                    animation.playState !== 'idle' && animation.playState !== 'finished')
                    .map(animation => ({state: animation.playState,
                        target: animation.effect?.target?.id,
                        timeline: animation.timeline?.constructor.name,
                        currentTime: String(animation.currentTime)}))};
        }""")
        assert dimensions["innerBottom"] <= dimensions["outerBottom"] + 1, dimensions
        assert dimensions["overflow"] <= 1, dimensions
        _assert_clean(inner)
        assert nested_server.history.read_bytes() == nested_server.preserved
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("nested_server", ["native"], indirect=True)
def test_deep_fragment_immediately_reveals_a_branch_while_its_ancestor_is_closing(
    disposable_browser, nested_server, width
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1100}, reduced_motion="no-preference"
    )
    page = context.new_page()
    try:
        card = nested_fixtures._open(page, nested_server)
        _settle(card)
        _capture(card)
        outer = card.locator(":scope > details[data-wechat-forwarded]")
        outer.locator(":scope > summary").press("Enter")
        _finish(card)
        outer.locator(":scope > summary").press("Space")
        _sample(outer, 0.15)
        expect(outer.locator(":scope > summary")).to_have_attribute(
            "aria-expanded", "false"
        )
        identifier = "wechat_message_" + nested_server.parent + "_forwarded_1" * 3
        page.evaluate("identifier => { location.hash = identifier; }", identifier)
        target = page.locator("#" + identifier)
        expect(target).to_be_visible()
        expect(card.locator("details[data-wechat-forwarded][open]")).to_have_count(3)
        page.wait_for_function(
            """id => {
                let ancestor = document.getElementById(id)?.parentElement
                    ?.closest('details[data-wechat-forwarded]');
                while (ancestor) {
                    if (!ancestor.open || ancestor.hasAttribute('data-collapse-motion')
                        || ancestor.querySelector(':scope > .ui-collapse-body').inert) return false;
                    ancestor = ancestor.parentElement.closest('details[data-wechat-forwarded]');
                }
                return true;
            }""",
            arg=identifier,
            timeout=5000,
        )
        sibling = page.locator(
            "#wechat_message_" + nested_server.parent + "_forwarded_2"
        ).locator(":scope > details[data-wechat-forwarded]")
        expect(sibling).not_to_have_attribute("open", "")
        _assert_clean(outer)
        nested_fixtures._assert_clock_rail(page, nested_server)
        assert nested_server.history.read_bytes() == nested_server.preserved
    finally:
        context.close()
