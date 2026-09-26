"""Isolated Cache activity disclosure coverage. Code version: v1.1.0-codex.0."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url


def _task(
    source: str = "chatgpt",
    *,
    processed: int = 18,
    total: int = 1_428,
    content_mode: str = "media",
    unit: str = "images",
) -> dict:
    return {
        "id": f"{source}:{content_mode}",
        "source": source,
        "label": {"chatgpt": "ChatGPT", "grok": "Grok"}[source],
        "content_mode": content_mode,
        "phase": "running",
        "message": "Caching images from the selected project.",
        "processed": processed,
        "total": total,
        "unit": unit,
    }


def _install_responses(context, activity: dict) -> list[tuple[str, str]]:
    requests: list[tuple[str, str]] = []
    context.on("request", lambda request: requests.append((request.method, request.url)))
    context.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(json={
            "browser": "safari",
            "logged_in": True,
            "can_download": True,
            "account_name": "Signed in",
            "message": "Ready.",
        }),
    )

    def respond(route) -> None:
        route.fulfill(
            status=activity.get("status", 200),
            json={"tasks": activity["tasks"]},
        )

    context.route("**/api/cache/activity**", respond)
    return requests


def _assert_anchor_geometry(page: Page) -> dict:
    geometry = page.locator("[data-cache-activity-trigger]").evaluate("""button => {
        const box = button.getBoundingClientRect();
        const theme = document.querySelector('[data-layout-role="global-theme-anchor"]')
            .getBoundingClientRect();
        const content = document.querySelector('.cache-workspace-content')
            .getBoundingClientRect();
        return {
            centerX: box.x + box.width / 2,
            centerY: box.y + box.height / 2,
            themeCenterX: theme.x + theme.width / 2,
            width: box.width,
            height: box.height,
            themeWidth: theme.width,
            themeHeight: theme.height,
            rightInset: content.right - box.right,
            bottomInset: content.bottom - box.bottom,
            viewportOverflow: document.documentElement.scrollWidth - innerWidth,
            insideContent: box.left >= content.left && box.top >= content.top,
        };
    }""")
    assert abs(geometry["centerX"] - geometry["themeCenterX"]) <= 1, geometry
    assert abs(geometry["rightInset"] - geometry["bottomInset"]) <= 1, geometry
    assert geometry["rightInset"] >= 0 and geometry["bottomInset"] >= 0, geometry
    assert geometry["width"] == geometry["height"] == geometry["themeWidth"], geometry
    assert geometry["height"] == geometry["themeHeight"], geometry
    assert geometry["viewportOverflow"] <= 1 and geometry["insideContent"], geometry
    return geometry


def _settle_disclosure(page: Page) -> None:
    page.locator("[data-cache-activity]").evaluate("""async element => {
        const motions = element.getAnimations({subtree: true})
            .filter(animation => animation.effect.getTiming().iterations !== Infinity);
        await Promise.allSettled(motions.map(animation => animation.finished));
    }""")


def _assert_progress_geometry(row: Locator, expected_fraction: float) -> dict:
    """Verify the rendered fill after the existing width transition has settled."""
    track = row.get_by_role("progressbar")
    expect(track).to_be_visible()
    track.evaluate("""async element => {
        await Promise.allSettled(element.getAnimations({subtree: true})
            .filter(animation => animation.effect.getTiming().iterations !== Infinity)
            .map(animation => animation.finished));
    }""")
    geometry = track.evaluate("""element => {
        const track = element.getBoundingClientRect();
        const fill = element.querySelector('.status-progress-fill').getBoundingClientRect();
        const row = element.closest('[data-cache-activity-task]').getBoundingClientRect();
        return {
            width: track.width, height: track.height, fillWidth: fill.width,
            fillHeight: fill.height, fillLeft: fill.left, fillRight: fill.right,
            left: track.left, right: track.right, rowLeft: row.left, rowRight: row.right,
        };
    }""")
    assert geometry["width"] > 0 and geometry["height"] > 0, geometry
    assert geometry["fillHeight"] == pytest.approx(geometry["height"], abs=1), geometry
    assert geometry["left"] == pytest.approx(geometry["rowLeft"], abs=1), geometry
    assert geometry["right"] == pytest.approx(geometry["rowRight"], abs=1), geometry
    assert geometry["fillLeft"] == pytest.approx(geometry["left"], abs=1), geometry
    assert geometry["fillRight"] <= geometry["right"] + 1, geometry
    assert geometry["fillWidth"] / geometry["width"] == pytest.approx(
        expected_fraction, abs=0.002
    ), geometry
    return geometry


def _assert_panel_geometry(page: Page) -> dict:
    geometry = page.locator("[data-cache-activity-panel]").evaluate("""panel => {
        const box = panel.getBoundingClientRect();
        const owner = document.querySelector('.cache-workspace-content')
            .getBoundingClientRect();
        const style = getComputedStyle(panel);
        return {
            left: box.left, right: box.right, top: box.top, bottom: box.bottom,
            owner: {left: owner.left, right: owner.right, top: owner.top, bottom: owner.bottom},
            viewport: {width: innerWidth, height: innerHeight},
            overflowX: panel.scrollWidth - panel.clientWidth,
            blur: style.backdropFilter,
        };
    }""")
    assert geometry["left"] >= geometry["owner"]["left"] - 1, geometry
    assert geometry["right"] <= geometry["owner"]["right"] + 1, geometry
    assert geometry["top"] >= geometry["owner"]["top"] - 1, geometry
    assert geometry["bottom"] <= geometry["owner"]["bottom"] + 1, geometry
    assert geometry["left"] >= -1 and geometry["top"] >= -1, geometry
    assert geometry["right"] <= geometry["viewport"]["width"] + 1, geometry
    assert geometry["bottom"] <= geometry["viewport"]["height"] + 1, geometry
    assert geometry["overflowX"] <= 1, geometry
    return geometry


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "height", "scheme", "motion", "touch"),
    (
        (1_006, 791, "light", "no-preference", False),
        (768, 791, "light", "reduce", False),
        (1_920, 1_080, "light", "reduce", False),
        (390, 844, "dark", "reduce", True),
        (1_006, 500, "light", "reduce", True),
    ),
)
def test_cache_activity_tracks_all_running_tasks_and_preserves_anchors(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
    width: int,
    height: int,
    scheme: str,
    motion: str,
    touch: bool,
) -> None:
    """Keep running work reachable without moving the standard circular anchor."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=scheme,
        reduced_motion=motion,
        has_touch=touch,
        is_mobile=touch,
    )
    activity = {"tasks": []}
    requests = _install_responses(context, activity)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt/media/safari")
        host = page.locator("[data-cache-activity]")
        trigger = page.locator("[data-cache-activity-trigger]")
        panel = page.locator("[data-cache-activity-panel]")
        expect(host).to_be_hidden()
        expect(trigger).to_have_attribute("aria-expanded", "false")

        activity["tasks"] = [
            _task(),
            _task("grok", processed=714, content_mode="text", unit="conversations"),
        ]
        expect(trigger).to_be_visible(timeout=7_000)
        expect(panel).to_have_attribute("aria-hidden", "true")
        assert panel.evaluate("element => element.inert")
        assert "circular-icon-button" in (trigger.get_attribute("class") or "")
        closed_geometry = _assert_anchor_geometry(page)
        output = Path("test-results")
        output.mkdir(exist_ok=True)
        if motion == "no-preference":
            page.screenshot(path=str(output / "cache-activity-isolated-collapsed-1006x791.png"))
        marker = trigger.locator(".live-marker")
        animation = marker.evaluate("""element => {
            const ring = getComputedStyle(element, '::after');
            return {name: ring.animationName, duration: ring.animationDuration};
        }""")
        if motion == "reduce":
            assert animation["name"] == "none", animation
        else:
            assert animation["name"] == "live-marker-breath", animation
            assert animation["duration"] == "1.8s", animation

        trigger.click()
        expect(trigger).to_have_attribute("aria-expanded", "true")
        expect(panel).to_have_attribute("aria-hidden", "false")
        assert not panel.evaluate("element => element.inert")
        if motion == "no-preference":
            assert host.evaluate("""element => element.getAnimations({subtree: true})
                .some(animation => animation.effect.getTiming().iterations !== Infinity)""")
        _settle_disclosure(page)
        expect(panel).to_have_attribute("role", "dialog")
        expect(panel.get_by_role("heading", name="Running cache tasks")).to_be_visible()
        rows = panel.locator("[data-cache-activity-task]")
        expect(rows).to_have_count(2)
        expect(rows.nth(0)).to_contain_text("ChatGPT")
        expect(rows.nth(1)).to_contain_text("Grok")
        expect(rows.nth(0)).to_contain_text("1,428")
        expect(rows.nth(0)).to_contain_text("18 / 1,428 images processed")
        expect(rows.nth(1)).to_contain_text("Grok · Text")
        expect(rows.nth(1)).to_contain_text("714 / 1,428 sessions processed")
        meters = panel.get_by_role("progressbar")
        expect(meters).to_have_count(2)
        expect(meters.nth(1)).to_have_attribute("aria-valuenow", "50")
        progress_geometry = [
            _assert_progress_geometry(rows.nth(0), 18 / 1_428),
            _assert_progress_geometry(rows.nth(1), 0.5),
        ]
        panel_geometry = _assert_panel_geometry(page)
        opened_geometry = _assert_anchor_geometry(page)
        assert abs(closed_geometry["centerX"] - opened_geometry["centerX"]) <= 1
        assert abs(closed_geometry["centerY"] - opened_geometry["centerY"]) <= 1
        page.screenshot(path=str(output / f"cache-activity-{width}x{height}-{scheme}.png"))
        (output / f"cache-activity-isolated-{width}x{height}-{scheme}-geometry.json").write_text(
            json.dumps({
                "source": "Isolated browser verification with synthetic cache tasks",
                "closed": closed_geometry,
                "opened": opened_geometry,
                "panel": panel_geometry,
                "progress": progress_geometry,
            }, indent=2) + "\n",
            encoding="utf-8",
        )

        first_row = rows.nth(0).element_handle()
        first_meter = meters.nth(0).element_handle()
        assert first_row is not None
        assert first_meter is not None
        activity["tasks"][0] = _task(processed=25)
        activity["tasks"][0]["message"] = "Updated progress: 25 images cached."
        expect(rows.nth(0)).to_contain_text("Updated progress: 25 images cached.", timeout=7_000)
        assert first_row.evaluate("element => element.isConnected")
        assert first_meter.evaluate("element => element.isConnected")
        assert float(meters.nth(0).get_attribute("aria-valuenow")) == pytest.approx(
            25 / 1_428 * 100, abs=0.01
        )
        _assert_progress_geometry(rows.nth(0), 25 / 1_428)
        expect(meters.nth(1)).to_have_attribute("aria-valuenow", "50")
        _assert_progress_geometry(rows.nth(1), 0.5)
        expect(trigger).to_have_attribute("aria-expanded", "true")

        page.keyboard.press("Escape")
        expect(trigger).to_have_attribute("aria-expanded", "false")
        expect(trigger).to_be_focused()
        expect(panel).to_have_attribute("aria-hidden", "true")
        assert panel.evaluate("element => element.inert")
        trigger.click()
        expect(trigger).to_have_attribute("aria-expanded", "true")
        page.locator(".cache-overview-title-card .report-heading").click()
        expect(trigger).to_have_attribute("aria-expanded", "false")

        trigger.click()
        expect(panel).to_be_focused()
        activity["tasks"] = []
        expect(host).to_be_hidden(timeout=7_000)
        expect(rows).to_have_count(0)
        expect(meters).to_have_count(0)
        expect(trigger).to_have_attribute("aria-expanded", "false")
        expect(page.locator('[data-layout-role="global-theme-anchor"]')).to_be_focused()
        assert not errors, errors
        assert all(method != "POST" for method, _url in requests), requests
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_cache_activity_keeps_last_known_work_on_poll_failure_and_escapes_messages(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
) -> None:
    """A network error must not imply completion or inject task text as markup."""
    context = disposable_browser.new_context(
        viewport={"width": 1_006, "height": 791},
        reduced_motion="no-preference",
    )
    task = _task("grok")
    task["message"] = '<img src="invalid" onerror="window.cacheTaskInjected=true">'
    activity = {"tasks": [task]}
    requests = _install_responses(context, activity)
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt/media/safari")
        host = page.locator("[data-cache-activity]")
        trigger = page.locator("[data-cache-activity-trigger]")
        panel = page.locator("[data-cache-activity-panel]")
        expect(trigger).to_be_visible()
        trigger.click()
        row = panel.locator("[data-cache-activity-task]")
        expect(row).to_contain_text(task["message"])
        expect(row.locator("img")).to_have_count(0)
        assert page.evaluate("window.cacheTaskInjected === undefined")
        meter = row.get_by_role("progressbar")
        expect(meter).to_be_visible()
        last_value = meter.get_attribute("aria-valuenow")
        last_value_text = meter.get_attribute("aria-valuetext")
        _assert_progress_geometry(row, 18 / 1_428)

        activity["status"] = 503
        expect(host).to_have_attribute("data-stale", "true", timeout=7_000)
        expect(trigger).to_be_visible()
        expect(row).to_have_count(1)
        expect(meter).to_have_attribute("aria-valuenow", last_value)
        expect(meter).to_have_attribute("aria-valuetext", last_value_text)
        _assert_progress_geometry(row, 18 / 1_428)
        expect(panel).to_contain_text("Status temporarily unavailable.")
        assert trigger.locator(".live-marker").evaluate(
            "element => getComputedStyle(element, '::after').animationName"
        ) == "none"

        activity["status"] = 200
        activity["tasks"] = []
        expect(host).to_be_hidden(timeout=7_000)
        assert all(method != "POST" for method, _url in requests), requests
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("motion", ("no-preference", "reduce"))
def test_cache_activity_progress_follows_discovery_and_count_changes(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
    motion: str,
) -> None:
    """Unknown totals stay honest, while every known total produces a bounded meter."""
    context = disposable_browser.new_context(
        viewport={"width": 768, "height": 791},
        reduced_motion=motion,
    )
    activity = {"tasks": [_task(processed=0, total=0, unit="sessions")]}
    requests = _install_responses(context, activity)
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt/media/safari")
        trigger = page.locator("[data-cache-activity-trigger]")
        expect(trigger).to_be_visible()
        trigger.click()
        _settle_disclosure(page)
        row = page.locator("[data-cache-activity-task]")
        meter = row.get_by_role("progressbar")
        expect(meter).to_be_visible()
        row_handle = row.element_handle()
        meter_handle = meter.element_handle()
        assert row_handle is not None and meter_handle is not None

        for processed, total, fraction in (
            (0, 0, 0.4),
            (0, 90, 0),
            (45, 90, 0.5),
            (120, 90, 1),
            (12, 0, 0.4),
        ):
            activity["tasks"] = [_task(processed=processed, total=total, unit="sessions")]
            expected_text = (
                f"{processed} / {total} sessions processed" if total
                else f"{processed} sessions processed" if processed
                else "Waiting for a work-item total."
            )
            expect(row.locator(".cache-activity-progress")).to_contain_text(
                expected_text, timeout=7_000
            )
            expect(meter).to_have_attribute("aria-valuemin", "0")
            expect(meter).to_have_attribute("aria-valuemax", "100")
            assert meter.get_attribute("aria-label")
            assert row_handle.evaluate("element => element.isConnected")
            assert meter_handle.evaluate("element => element.isConnected")
            if total:
                expect(meter).not_to_have_class(re.compile(r"\bis-indeterminate\b"))
                expect(meter).to_have_attribute("aria-valuenow", str(int(fraction * 100)))
                value_text = meter.get_attribute("aria-valuetext") or ""
                assert expected_text in value_text, value_text
                assert f"{int(fraction * 100)}%" in value_text, value_text
            else:
                expect(meter).to_have_class(re.compile(r"\bis-indeterminate\b"))
                assert meter.get_attribute("aria-valuenow") is None
                assert "%" not in (meter.get_attribute("aria-valuetext") or "")
                assert "%" not in row.inner_text()
                assert meter.locator(".status-progress-fill").evaluate(
                    "element => getComputedStyle(element).animationName"
                ) == "none"
            _assert_progress_geometry(row, fraction)
            _assert_panel_geometry(page)

        assert all(method != "POST" for method, _url in requests), requests
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_cache_activity_list_scrolls_inside_panel_during_open_sidebar_and_viewport_reflow(
    disposable_browser: Browser,
    sidebar_server_url: str,
    macos_host,
) -> None:
    """A long task list keeps its own scrollport and responds to owner geometry."""
    context = disposable_browser.new_context(
        viewport={"width": 1_006, "height": 500},
        reduced_motion="reduce",
    )
    tasks = []
    for index in range(8):
        task = _task("grok" if index % 2 else "chatgpt")
        task["id"] = f"fixture-task-{index}"
        task["message"] = f"Isolated verification task {index + 1}: caching project images."
        tasks.append(task)
    activity = {"tasks": tasks}
    requests = _install_responses(context, activity)
    page = context.new_page()
    evidence = {"source": "Isolated browser verification with synthetic cache tasks"}
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt/media/safari")
        trigger = page.locator("[data-cache-activity-trigger]")
        panel = page.locator("[data-cache-activity-panel]")
        listing = page.locator("[data-cache-activity-tasks]")
        expect(trigger).to_be_visible()
        trigger.click()
        _settle_disclosure(page)
        expect(panel.locator("[data-cache-activity-task]")).to_have_count(8)
        evidence["desktop_short"] = {
            "anchor": _assert_anchor_geometry(page),
            "panel": _assert_panel_geometry(page),
        }
        assert listing.evaluate("element => element.scrollHeight > element.clientHeight")
        before = page.locator(".cache-workspace-content").evaluate(
            "element => ({owner: element.scrollTop, window: scrollY})"
        )
        panel.locator("[data-cache-activity-task]").last.scroll_into_view_if_needed()
        assert listing.evaluate("element => element.scrollTop > 0")
        assert page.locator(".cache-workspace-content").evaluate(
            "element => ({owner: element.scrollTop, window: scrollY})"
        ) == before

        toggle = page.locator("#sidebar_toggle")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        toggle.focus()
        toggle.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(trigger).to_have_attribute("aria-expanded", "true")
        evidence["sidebar_collapsed"] = {
            "anchor": _assert_anchor_geometry(page),
            "panel": _assert_panel_geometry(page),
        }

        page.set_viewport_size({"width": 390, "height": 600})
        page.wait_for_function("""() => {
            const trigger = document.querySelector('[data-cache-activity-trigger]')
                .getBoundingClientRect();
            const theme = document.querySelector('[data-layout-role="global-theme-anchor"]')
                .getBoundingClientRect();
            const owner = document.querySelector('.cache-workspace-content')
                .getBoundingClientRect();
            return Math.abs(trigger.right - theme.right) <= 1
                && Math.abs((owner.right - trigger.right) - (owner.bottom - trigger.bottom)) <= 1
                && trigger.width === theme.width;
        }""")
        expect(trigger).to_have_attribute("aria-expanded", "true")
        evidence["narrow_resized_open"] = {
            "anchor": _assert_anchor_geometry(page),
            "panel": _assert_panel_geometry(page),
        }
        expect(panel.locator("[data-cache-activity-task]")).to_have_count(8)
        assert listing.evaluate("element => element.scrollHeight > element.clientHeight")
        panel.locator("[data-cache-activity-task]").last.scroll_into_view_if_needed()
        assert listing.evaluate("element => element.scrollTop > 0")
        assert page.evaluate("scrollY === 0")
        output = Path("test-results")
        output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / "cache-activity-isolated-long-list-390x600.png"))
        (output / "cache-activity-isolated-geometry.json").write_text(
            json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
        )
        assert all(method != "POST" for method, _url in requests), requests
    finally:
        context.close()
