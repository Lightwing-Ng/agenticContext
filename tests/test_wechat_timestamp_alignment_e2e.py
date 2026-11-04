"""WeChat timestamps share a rail and keep complete inside/outside bubble states.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests import test_wechat_browser_e2e as fixtures
from tests.test_wechat_disclosure_e2e import (
    _assert_clean_motion,
    _capture_paused_motions,
    _finish_motions,
    _sample_motion,
)


disposable_browser = fixtures.disposable_browser
wechat_server = fixtures.wechat_server


def _open_native_group(page, base_url):
    page.goto(
        base_url
        + "/browser?view=text&source=wechat&account=account:wxid_account_a&chat_kind=group",
        wait_until="networkidle",
    )
    href = page.get_by_role("link", name="Same title", exact=True).get_attribute("href")
    page.goto(base_url + href, wait_until="networkidle")
    toggle = page.locator("#sidebar_toggle")
    if toggle.get_attribute("aria-expanded") == "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(page.locator(".wechat-message-row")).to_have_count(100)
    page.evaluate("() => document.fonts.ready")


def _set_synthetic_variants(page):
    page.locator(".wechat-message-row").evaluate_all(
        """rows => {
        const labels = [
            '4 Jun 2024 01:02:03',
            '24 Sep 2024 23:59:59',
            '7 May 2024 12:00:00',
            '14 Aug 2024 20:26:16',
        ];
        rows.slice(0, 4).forEach((row, index) => {
            row.querySelector('.wechat-time-slot').textContent = labels[index];
            row.querySelector('time.wechat-message-time').textContent = labels[index];
            row.querySelector('.wechat-message-text').textContent = 'A short synthetic message.';
            row.querySelector('.wechat-speaker').textContent = index === 3
                ? 'A very long synthetic contact name with 中文 characters '.repeat(4)
                : 'Contact ' + index;
        });
        const unknown = rows[2].querySelector('.wechat-message');
        unknown.classList.remove('wechat-message--sent', 'wechat-message--received');
        unknown.classList.add('wechat-message--unknown');
        unknown.dataset.wechatDirection = 'unknown';
    }"""
    )


def _rail_geometry(page):
    return page.locator(".wechat-message-row").evaluate_all(
        """rows => {
        const list = document.querySelector('.wechat-message-list');
        const samples = rows.map(row => {
            const card = row.querySelector('.wechat-message');
            const time = row.querySelector(':scope > time.wechat-message-time');
            const slot = card.querySelector('.wechat-time-slot');
            if (!time || !slot) return null;
            const rowBox = row.getBoundingClientRect();
            const cardBox = card.getBoundingClientRect();
            const timeBox = time.getBoundingClientRect();
            const slotBox = slot.getBoundingClientRect();
            const style = getComputedStyle(card);
            return {
                direction: card.dataset.wechatDirection,
                rowLeft: rowBox.left,
                rowRight: rowBox.right,
                rowWidth: rowBox.width,
                cardLeft: cardBox.left,
                cardRight: cardBox.right,
                timeLeft: timeBox.left,
                timeRight: timeBox.right,
                contentLeft: cardBox.left + parseFloat(style.paddingLeft)
                    + parseFloat(style.borderLeftWidth),
                contained: card.classList.contains('wechat-message--time-contained'),
                topError: Math.abs(timeBox.top - slotBox.top),
                heightError: Math.abs(timeBox.height - slotBox.height),
                sentContentRight: cardBox.right - parseFloat(style.paddingRight)
                    - parseFloat(style.borderRightWidth),
                rowPosition: getComputedStyle(row).position,
                slotVisibility: getComputedStyle(slot).visibility,
                slotAriaHidden: slot.getAttribute('aria-hidden'),
                realTimeCount: row.querySelectorAll('time').length,
            };
        });
        return {
            samples,
            listOverflow: list.scrollWidth - list.clientWidth,
            documentOverflow: document.documentElement.scrollWidth - innerWidth,
        };
    }"""
    )


def _assert_rail(page):
    page.wait_for_function(
        """() => [...document.querySelectorAll('.wechat-message-row')].every(row => {
        const time = row.querySelector(':scope > time.wechat-message-time');
        const slot = row.querySelector('.wechat-time-slot');
        return time && slot && Math.abs(time.getBoundingClientRect().top
            - slot.getBoundingClientRect().top) <= 1;
    })"""
    )
    geometry = _rail_geometry(page)
    samples = geometry["samples"]
    assert samples and all(sample is not None for sample in samples)
    rights = [sample["timeRight"] for sample in samples]
    widths = [sample["rowWidth"] for sample in samples]
    assert max(rights) - min(rights) <= 1, geometry
    assert max(widths) - min(widths) <= 1, geometry
    assert max(sample["topError"] for sample in samples) <= 1, geometry
    assert max(sample["heightError"] for sample in samples) <= 1, geometry
    assert geometry["listOverflow"] <= 1, geometry
    assert geometry["documentOverflow"] <= 1, geometry
    assert all(sample["rowPosition"] == "relative" for sample in samples)
    assert all(sample["slotVisibility"] == "hidden" for sample in samples)
    assert all(sample["slotAriaHidden"] == "true" for sample in samples)
    assert all(sample["realTimeCount"] == 1 for sample in samples)
    for sample in samples:
        assert sample["timeLeft"] >= sample["cardRight"] - 1 or (
            sample["timeLeft"] >= sample["contentLeft"] - 1
            and sample["timeRight"] <= sample["sentContentRight"] + 1
        ), sample
    sent = next(sample for sample in samples if sample["direction"] == "sent")
    assert abs(sent["timeRight"] - sent["sentContentRight"]) <= 1, geometry
    return geometry


def _assert_time_is_painted(row):
    assert row.evaluate(
        """row => {
        const time = row.querySelector(':scope > time.wechat-message-time');
        const box = time.getBoundingClientRect();
        const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
        return hit === time || time.contains(hit);
    }"""
    )


@pytest.mark.parametrize("width", [831, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_wechat_timestamp_rail_retains_baselines_through_motion_resize_and_scroll(
    disposable_browser, wechat_server, width, color_scheme
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1254},
        color_scheme=color_scheme,
        reduced_motion="no-preference",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _open_native_group(page, wechat_server)
        _set_synthetic_variants(page)
        geometry = _assert_rail(page)
        assert {sample["direction"] for sample in geometry["samples"]} == {
            "sent",
            "received",
            "unknown",
        }
        short = geometry["samples"][0]
        assert abs(short["cardLeft"] - short["rowLeft"]) <= 1, geometry
        if width > 600:
            assert short["timeLeft"] >= short["cardRight"], geometry
            assert not short["contained"], geometry
        else:
            assert short["contained"], geometry
            assert short["timeLeft"] >= short["contentLeft"] - 1, geometry
            assert short["timeRight"] <= short["sentContentRight"] + 1, geometry

        row = page.locator(".wechat-message-row").first
        card = row.locator(":scope > .wechat-message")
        details = card.locator(":scope > .wechat-provenance")
        summary = details.locator(":scope > summary")
        card.scroll_into_view_if_needed()
        expect(summary).to_have_accessible_name("Source details")
        assert summary.bounding_box()["height"] >= 32
        _assert_time_is_painted(row)
        _capture_paused_motions(card)
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        page.wait_for_function("window.wechatDisclosureMotions.length > 0")
        for fraction in (0, 0.15, 0.5, 1):
            sample = _sample_motion(card, fraction)
            assert sample["shadowError"] <= 1, sample
            _assert_rail(page)
            _assert_time_is_painted(row)
        _finish_motions(card)

        summary.press("Space")
        expect(summary).to_have_attribute("aria-expanded", "false")
        for fraction in (0, 0.2, 0.6):
            sample = _sample_motion(card, fraction)
            assert sample["shadowError"] <= 1, sample
            _assert_rail(page)
            _assert_time_is_painted(row)
        page.set_viewport_size({"width": width + 22, "height": 1174})
        expect(details).not_to_have_attribute("open", "")
        _assert_clean_motion(card)
        _assert_rail(page)
        _assert_time_is_painted(row)

        middle = page.locator(".wechat-message-row").nth(20)
        middle.locator(":scope > .wechat-message").scroll_into_view_if_needed()
        page.locator(".wechat-message-list").evaluate(
            "list => { list.scrollTop += 7; }"
        )
        _assert_rail(page)
        _assert_time_is_painted(middle)
        assert errors == []
    finally:
        context.close()
