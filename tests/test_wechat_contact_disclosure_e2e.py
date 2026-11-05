"""Conflicting contact clocks retain source geometry and native interaction.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect

from tests import test_wechat_contact_analysis_e2e as fixtures
from tests.test_wechat_contact_loading_e2e import ControlledContactResults
from tests.test_wechat_disclosure_e2e import _capture_paused_motions, _finish_motions


disposable_browser = fixtures.disposable_browser
conflicting_duplicate_contact_analysis_server = (
    fixtures.conflicting_duplicate_contact_analysis_server
)


def _contact_url(fixture):
    return (
        fixture.url
        + "/browser/wechat/contacts/"
        + fixture.target_id
        + "?"
        + urlencode({"q": fixture.shared_text, "sort": "oldest"})
    )


def _ready_disclosure(page):
    expect(page.locator("[data-contact-analysis]")).to_have_attribute(
        "data-contact-analysis-state", "ready"
    )
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    page.evaluate("() => document.fonts.ready")
    details = page.locator("details[data-wechat-contact-sources]")
    expect(details).to_have_count(1)
    summary = details.locator(":scope > summary")
    expect(summary).to_have_accessible_name("Show original sources")
    summary.scroll_into_view_if_needed()
    page.wait_for_function("""() => {
        const row = document.querySelector('details[data-wechat-contact-sources]').closest('tr');
        const current = JSON.stringify([...row.cells].map(cell => {
            const box = cell.getBoundingClientRect();
            return [box.left, box.width];
        }));
        const previous = window.__contactDisclosureColumns;
        const stable = previous?.value === current ? previous.stable + 1 : 0;
        window.__contactDisclosureColumns = {value: current, stable};
        return stable >= 3;
    }""")
    return details, summary


def _geometry(details):
    return details.evaluate("""details => {
        const row = details.closest('tr');
        return {
            height: details.getBoundingClientRect().height,
            rowHeight: row.getBoundingClientRect().height,
            columns: [...row.cells].map(cell => cell.getBoundingClientRect().width),
            overflow: document.documentElement.scrollWidth - innerWidth,
        };
    }""")


def _sample(details, fraction):
    details.evaluate(
        """async (details, fraction) => {
        for (const animation of window.wechatDisclosureMotions) {
            if (animation.playState !== 'idle' && animation.playState !== 'finished') {
                animation.currentTime = Number(animation.effect.getTiming().duration) * fraction;
            }
        }
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""",
        fraction,
    )
    return _geometry(details)


def _motion_frames(details):
    return details.evaluate("""details => {
        const motions = window.wechatDisclosureMotions.filter(animation =>
            animation.playState !== 'idle' && animation.playState !== 'finished');
        const height = motions.find(animation => animation.effect.target === details);
        return {
            count: motions.length,
            hasWidth: motions.some(animation => animation.effect.getKeyframes()
                .some(frame => Object.hasOwn(frame, 'width'))),
            durations: motions.map(animation => animation.effect.getTiming().duration),
            heights: height?.effect.getKeyframes().map(frame => ({
                offset: frame.offset, height: parseFloat(frame.height),
            })) || [],
        };
    }""")


def _assert_columns_unchanged(sample, original):
    assert sample["columns"] == pytest.approx(original["columns"], abs=1)
    assert sample["overflow"] <= 1


def _assert_clean(details, *, opened):
    if opened:
        expect(details).to_have_attribute("open", "")
    else:
        expect(details).not_to_have_attribute("open", "")
    state = details.evaluate("""details => {
        const body = details.querySelector(':scope > .ui-collapse-body');
        return {
            height: details.style.height, width: details.style.width,
            overflow: details.style.overflow, transform: body.style.transform,
            inert: body.inert, motion: details.dataset.collapseMotion || '',
            expanded: details.querySelector(':scope > summary').getAttribute('aria-expanded'),
            // Native chevron/focus CSS transitions are separate from captured disclosure WAAPI.
            animations: window.wechatDisclosureMotions.map(animation => ({
                type: animation.constructor.name, state: animation.playState,
                target: animation.effect.target.tagName,
            })),
        };
    }""")
    assert {key: value for key, value in state.items() if key != "animations"} == {
        "height": "",
        "width": "",
        "overflow": "",
        "transform": "",
        "inert": False,
        "motion": "",
        "expanded": None,
    }, state
    assert all(item["state"] in {"idle", "finished"} for item in state["animations"]), (
        state
    )


def _assert_sources_unchanged(fixture):
    assert fixture.before == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    )


@pytest.mark.parametrize("width", [1026, 390])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_contact_sources_spring_without_resizing_columns_and_cleanly_close(
    disposable_browser, conflicting_duplicate_contact_analysis_server, width, scheme
):
    fixture = conflicting_duplicate_contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        color_scheme=scheme,
        reduced_motion="no-preference",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(_contact_url(fixture), wait_until="networkidle")
        details, summary = _ready_disclosure(page)
        closed = _geometry(details)
        _capture_paused_motions(details)
        summary.press("Enter")
        expect(summary).to_have_attribute("aria-expanded", "true")
        frames = _motion_frames(details)
        assert frames["count"] == 2 and not frames["hasWidth"]
        assert frames["durations"] == [620, 620]
        peak_frame = max(frames["heights"], key=lambda frame: frame["height"])
        assert peak_frame["height"] > frames["heights"][-1]["height"] + 1
        start = _sample(details, 0)
        middle = _sample(details, 0.15)
        peak = _sample(details, peak_frame["offset"])
        end = _sample(details, 1)
        assert start["height"] == pytest.approx(closed["height"], abs=1)
        assert start["height"] + 5 < middle["height"] < end["height"]
        assert peak["height"] > end["height"] + 1
        for sample in (start, middle, peak, end):
            _assert_columns_unchanged(sample, closed)
        _finish_motions(details)
        _assert_clean(details, opened=True)
        assert _geometry(details)["height"] == pytest.approx(end["height"], abs=1)
        expect(details.locator("[data-wechat-contact-observation]")).to_have_count(2)
        details.locator("a[data-wechat-contact-source]").first.focus()
        summary.evaluate("node => node.click()")
        expect(summary).to_be_focused()
        assert details.locator(".ui-collapse-body").evaluate("body => body.inert")
        heights = [end["height"]]
        for fraction in (0.15, 0.5, 0.9):
            sample = _sample(details, fraction)
            heights.append(sample["height"])
            _assert_columns_unchanged(sample, closed)
        assert all(after <= before + 0.5 for before, after in zip(heights, heights[1:]))
        _finish_motions(details)
        _assert_clean(details, opened=False)
        assert _geometry(details)["height"] == pytest.approx(closed["height"], abs=1)
        _assert_sources_unchanged(fixture)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1026, 390])
def test_contact_sources_reverse_from_current_height_with_enter_and_space(
    disposable_browser, conflicting_duplicate_contact_analysis_server, width
):
    fixture = conflicting_duplicate_contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000}, reduced_motion="no-preference"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(_contact_url(fixture), wait_until="networkidle")
        details, summary = _ready_disclosure(page)
        closed = _geometry(details)
        _capture_paused_motions(details)
        summary.press("Enter")
        partial = _sample(details, 0.15)
        summary.press("Space")
        expect(summary).to_have_attribute("aria-expanded", "false")
        expect(details).to_have_attribute("open", "")
        reverse = _sample(details, 0)
        assert reverse["height"] == pytest.approx(partial["height"], abs=1)
        closing = [_sample(details, fraction) for fraction in (0.2, 0.5, 0.9)]
        heights = [reverse["height"], *(sample["height"] for sample in closing)]
        assert all(after <= before + 0.5 for before, after in zip(heights, heights[1:]))
        assert closing[-1]["height"] < partial["height"] - 2
        summary.press("Enter")
        expect(summary).to_have_attribute("aria-expanded", "true")
        reopened = _sample(details, 0)
        assert reopened["height"] == pytest.approx(closing[-1]["height"], abs=1)
        assert _motion_frames(details)["count"] == 2
        for sample in (partial, reverse, *closing, reopened):
            _assert_columns_unchanged(sample, closed)
        _finish_motions(details)
        _assert_clean(details, opened=True)
        summary.press("Space")
        _finish_motions(details)
        _assert_clean(details, opened=False)
        _assert_sources_unchanged(fixture)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("width", [1026, 390])
def test_contact_sources_respect_reduced_motion_and_midflight_preference_changes(
    disposable_browser, conflicting_duplicate_contact_analysis_server, width
):
    fixture = conflicting_duplicate_contact_analysis_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000}, reduced_motion="reduce"
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(_contact_url(fixture), wait_until="networkidle")
        details, summary = _ready_disclosure(page)
        _capture_paused_motions(details)
        summary.press("Enter")
        _assert_clean(details, opened=True)
        expect(details.locator(".ui-collapse-body")).to_be_visible()
        summary.press("Space")
        _assert_clean(details, opened=False)
        assert page.evaluate("window.wechatDisclosureMotions.length") == 0
        page.emulate_media(reduced_motion="no-preference")
        page.wait_for_function(
            "!matchMedia('(prefers-reduced-motion: reduce)').matches"
        )
        # A media change can reload font faces and settle active disclosure motion.
        details.evaluate("""details => {
            details.getBoundingClientRect();
            return document.fonts.ready;
        }""")
        summary.press("Enter")
        assert _motion_frames(details)["count"] == 2
        _sample(details, 0.15)
        page.emulate_media(reduced_motion="reduce")
        page.wait_for_function("""() => window.wechatDisclosureMotions.every(animation =>
            animation.playState === 'idle' || animation.playState === 'finished')""")
        _assert_clean(details, opened=True)
        expect(details.locator(".ui-collapse-body")).to_be_visible()
        summary.press("Space")
        _assert_clean(details, opened=False)
        assert page.evaluate("window.wechatDisclosureMotions.length") == 2
        _assert_sources_unchanged(fixture)
        assert not errors
    finally:
        context.close()


def test_contact_sources_adopt_ajax_content_once_and_cancel_detached_animations(
    disposable_browser, conflicting_duplicate_contact_analysis_server
):
    fixture = conflicting_duplicate_contact_analysis_server
    controlled = ControlledContactResults(
        SimpleNamespace(store=fixture.history.parents[2], target_id=fixture.target_id)
    )
    context = disposable_browser.new_context(
        viewport={"width": 390, "height": 1000}, reduced_motion="no-preference"
    )
    page = context.new_page()
    errors, documents = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "request",
        lambda request: (
            documents.append(request.url)
            if request.resource_type == "document"
            else None
        ),
    )
    try:
        with patch(
            "app.core.wechat_contact_analysis.query_contact_analysis",
            side_effect=controlled,
        ):
            response = page.goto(_contact_url(fixture), wait_until="domcontentloaded")
            assert response.status == 202
            expect(page.locator("#cache_wait_modal")).to_be_visible()
            controlled.release.set()
            expect(page.locator("[data-contact-analysis]")).to_have_attribute(
                "data-contact-analysis-state", "ready", timeout=12000
            )
            expect(page.locator("#cache_wait_modal")).to_be_hidden()
            assert controlled.json_requests()
            details, summary = _ready_disclosure(page)
            page.evaluate("""() => {
                const content = document.querySelector('[data-contact-analysis]');
                for (let index = 0; index < 3; index++) {
                    SharedDisclosureMotion.enhance(content, {
                        selector: 'details.wechat-contact-sources',
                    });
                    document.dispatchEvent(new Event('wechat:contact-content-ready'));
                }
            }""")
            _capture_paused_motions(details)
            summary.press("Enter")
            assert _motion_frames(details)["count"] == 2
            _sample(details, 0.15)
            payload_response = page.request.get(
                _contact_url(fixture), headers={"Accept": "application/json"}
            )
            assert payload_response.status == 200
            payload = payload_response.json()
            assert payload["status"] == "ready" and isinstance(payload["html"], str)
            details.evaluate("""details => {
                window.__detachedContactDisclosure = details;
                window.__detachedContactAnimations = [...window.wechatDisclosureMotions];
            }""")
            page.evaluate(
                """html => {
                const fragment = document.createElement('template');
                fragment.innerHTML = html;
                document.querySelector('[data-contact-analysis]').replaceChildren(fragment.content);
                document.dispatchEvent(new Event('wechat:contact-content-ready'));
            }""",
                payload["html"],
            )
            assert page.evaluate("""() => {
                const details = window.__detachedContactDisclosure;
                return !details.isConnected && !details.dataset.collapseMotion
                    && !details.querySelector('summary').hasAttribute('aria-expanded')
                    && !details.querySelector('.ui-collapse-body').inert
                    && window.__detachedContactAnimations.length === 2
                    && window.__detachedContactAnimations.every(animation => animation.playState === 'idle');
            }""")
            details, summary = _ready_disclosure(page)
            _capture_paused_motions(details)
            summary.press("Enter")
            frames = _motion_frames(details)
            assert frames["count"] == 2 and not frames["hasWidth"]
            assert _sample(details, 0.15)["height"] > 5
            _finish_motions(details)
            _assert_clean(details, opened=True)
            assert len(documents) == 1
            _assert_sources_unchanged(fixture)
            assert not errors
    finally:
        controlled.release.set()
        context.close()
