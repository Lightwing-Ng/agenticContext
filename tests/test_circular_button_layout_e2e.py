"""Round-action sizing and shared-shell anchor checks. Code version: v1.1.1-codex.0."""

import json
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e

disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url


def _circular_asset_observer(page):
    """Cache bounded asset-event metadata without issuing diagnostic browser RPCs."""
    requests, stylesheets, failed, console = [], [], [], []
    lifecycle = {"domcontentloaded": False, "load": False}

    def resource_name(url):
        path = urlsplit(url).path
        return path.rsplit("/", 1)[-1][:128] if path.startswith("/static/") else ""

    def requested(request):
        name = resource_name(request.url)
        if name and request.resource_type in {"stylesheet", "font"} and len(requests) < 24:
            requests.append({"resource": name, "type": request.resource_type})

    def responded(response):
        name = resource_name(response.url)
        if name.endswith(".css") and len(stylesheets) < 8:
            # These properties read Playwright's cached response initializer.
            headers = response.headers
            raw_length = headers.get("content-length", "")
            length = int(raw_length) if raw_length.isdecimal() and len(raw_length) <= 10 else None
            stylesheets.append({"resource": name, "status": response.status,
                                "mime": headers.get("content-type", "").split(";", 1)[0][:80],
                                "declared_bytes": length})

    def request_failed(request):
        name = resource_name(request.url)
        if name and len(failed) < 16:
            failed.append({"resource": name, "codes": re.findall(r"\bERR_[A-Z0-9_]+\b", str(request.failure)[:500])[:8]})

    def console_error(message):
        name = resource_name(message.location.get("url", ""))
        if message.type == "error" and name and len(console) < 16:
            console.append({"resource": name, "codes": re.findall(r"\bERR_[A-Z0-9_]+\b", message.text[:500])[:8]})

    def dom_ready():
        lifecycle["domcontentloaded"] = True

    def loaded():
        lifecycle["load"] = True

    listeners = (("request", requested), ("response", responded), ("requestfailed", request_failed),
                 ("console", console_error), ("domcontentloaded", dom_ready), ("load", loaded))
    for event, listener in listeners:
        page.on(event, listener)

    def capture():
        try:
            print("Circular action asset failure: " + json.dumps({"document_events": lifecycle,
                "requests": requests, "stylesheets": stylesheets, "failed": failed, "console_codes": console}, separators=(",", ":")))
        except Exception as error:
            print("Circular action asset diagnostic unavailable: " + type(error).__name__)

    def close():
        for event, listener in listeners:
            try:
                page.remove_listener(event, listener)
            except Exception:
                pass

    return capture, close


def _geometry(page):
    return page.evaluate(
        """() => {
            const box = selector => {
                const rect = document.querySelector(selector).getBoundingClientRect();
                return {
                    x: rect.x, y: rect.y, right: rect.right, bottom: rect.bottom,
                    width: rect.width, height: rect.height,
                    centerY: rect.top + rect.height / 2,
                };
            };
            const toggle = box('[data-layout-role="sidebar-toggle"]');
            const title = document.querySelector('[data-layout-role="title-heading"]');
            const text = title.querySelector('h1, h2, .report-heading');
            const range = document.createRange();
            range.selectNodeContents(text);
            const textLines = Array.from(range.getClientRects(), rect => ({
                right: rect.right, left: rect.left, top: rect.top, bottom: rect.bottom,
            }));
            const hit = document.elementFromPoint(
                toggle.x + toggle.width / 2, toggle.centerY
            );
            const coarsePointer = matchMedia('(hover: none) and (pointer: coarse)').matches;
            const touchTargets = coarsePointer ? [
                '[data-layout-role="sidebar-toggle"]',
                '[data-layout-role="global-theme-anchor"]',
            ].map(selector => {
                const node = document.querySelector(selector);
                const rect = node.getBoundingClientRect();
                const style = getComputedStyle(node, '::before');
                const centerX = rect.left + rect.width / 2;
                const centerY = rect.top + rect.height / 2;
                return {
                    width: style.width,
                    height: style.height,
                    edgeHits: [[-21, 0], [21, 0], [0, -21], [0, 21]].map(([x, y]) => {
                        const hit = document.elementFromPoint(centerX + x, centerY + y);
                        return hit === node || node.contains(hit);
                    }),
                };
            }) : [];
            return {
                toggle,
                sidebar: box('[data-layout-role="sidebar-shell"]'),
                theme: box('[data-layout-role="global-theme-anchor"]'),
                title: box('[data-layout-role="title-heading"]'),
                textLines,
                toggleHit: Boolean(hit?.closest('[data-layout-role="sidebar-toggle"]')),
                coarsePointer,
                touchTargets,
                documentOverflow: document.documentElement.scrollWidth - innerWidth,
            };
        }"""
    )


@pytest.mark.parametrize("route", ["/settings/style-tokens", "/agent/tunnel/chatgpt"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(
    ("width", "height", "touch"),
    [
        (1006, 791, False), (1006, 791, True), (1006, 500, False), (901, 791, False),
        (900, 791, True), (820, 1180, True), (390, 844, True),
    ],
)
def test_circular_actions_keep_size_and_shell_anchors(
    disposable_browser, sidebar_server_url, route, scheme, width, height, touch
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=scheme,
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
    )
    page = context.new_page()
    capture_failure, close_observer = _circular_asset_observer(page)
    try:
        page.goto(f"{sidebar_server_url}{route}", wait_until="domcontentloaded")
        toggle = page.locator('[data-layout-role="sidebar-toggle"]')
        theme = page.locator('[data-layout-role="global-theme-anchor"]')
        expected_size = 32
        for control in (toggle, theme):
            expect(control).to_have_css("width", f"{expected_size}px")
            expect(control).to_have_css("height", f"{expected_size}px")
            expect(control.locator(".icon")).to_have_css("width", "18px")
            expect(control.locator(".icon")).to_have_css("height", "18px")

        geometries = []
        for expanded in (False, True, False):
            if toggle.get_attribute("aria-expanded") != str(expanded).lower():
                toggle.click()
            expect(toggle).to_have_attribute("aria-expanded", str(expanded).lower())
            page.mouse.move(width - 1, height - 1)
            toggle.evaluate("node => node.blur()")
            page.wait_for_function(
                """() => {
                    const toggle = document.querySelector('[data-layout-role="sidebar-toggle"]');
                    const rect = toggle.getBoundingClientRect();
                    const key = [rect.x, rect.y, rect.width, rect.height].join(',');
                    const stable = window.__roundActionStableRect === key;
                    window.__roundActionStableRect = key;
                    return stable;
                }"""
            )
            geometry = _geometry(page)
            geometries.append(geometry)
            assert geometry["toggle"]["width"] == expected_size
            assert geometry["toggle"]["height"] == expected_size
            assert geometry["toggle"]["y"] == pytest.approx(20, abs=1)
            assert width - geometry["theme"]["right"] == pytest.approx(20, abs=1)
            assert geometry["toggle"]["centerY"] == pytest.approx(
                geometry["theme"]["centerY"], abs=1
            )
            if len(geometry["textLines"]) == 1:
                assert geometry["title"]["centerY"] == pytest.approx(
                    geometry["theme"]["centerY"], abs=1
                )
            assert geometry["toggleHit"]
            assert geometry["coarsePointer"] is touch
            if touch:
                assert len(geometry["touchTargets"]) == 2
                for target in geometry["touchTargets"]:
                    assert target["width"] == "44px"
                    assert target["height"] == "44px"
                    assert all(target["edgeHits"])
            assert geometry["documentOverflow"] <= 1
            assert all(
                item["right"] <= geometry["theme"]["x"] + 1
                for item in geometry["textLines"]
            )
            if expanded:
                top_clearance = geometry["toggle"]["y"] - geometry["sidebar"]["y"]
                right_clearance = geometry["sidebar"]["right"] - geometry["toggle"]["right"]
                assert top_clearance == pytest.approx(10, abs=1)
                assert right_clearance == pytest.approx(top_clearance, abs=1)
            else:
                assert geometry["title"]["x"] >= geometry["toggle"]["right"] + 7
        assert max(item["toggle"]["y"] for item in geometries) - min(
            item["toggle"]["y"] for item in geometries
        ) <= 1

        if route == "/settings/style-tokens":
            circular = page.locator(".style-token-round-icon-demo")
            expect(circular).to_have_css("width", f"{expected_size}px")
            expect(circular).to_have_css("height", f"{expected_size}px")
            expect(circular.locator(".icon")).to_have_css("width", "18px")
            copies = page.locator(".style-token-copy-button")
            copy_boxes = copies.evaluate_all(
                "nodes => nodes.map(node => ({width: node.getBoundingClientRect().width, right: node.getBoundingClientRect().right}))"
            )
            assert copy_boxes
            assert all(item["width"] == expected_size for item in copy_boxes)
            assert all(abs(item["right"] - geometries[-1]["theme"]["right"]) <= 1 for item in copy_boxes), {
                "copyRights": sorted({item["right"] for item in copy_boxes}),
                "themeRight": geometries[-1]["theme"]["right"],
            }
            if not touch:
                circular.hover()
                expect(circular).to_have_css("color", circular.evaluate(
                    """node => {
                        const probe = document.createElement('span');
                        probe.style.color = 'var(--circular-icon-button-color-hover)';
                        node.append(probe);
                        const expected = getComputedStyle(probe).color;
                        probe.remove();
                        return expected;
                    }"""
                ))
                page.mouse.move(width - 1, height - 1)
            circular.focus()
            circular.press("Shift+Tab")
            page.keyboard.press("Tab")
            expect(circular).to_be_focused()
            color = circular.evaluate(
                """node => {
                    const probe = document.createElement('span');
                    probe.style.color = 'var(--circular-icon-button-color-hover)';
                    node.append(probe);
                    const expected = getComputedStyle(probe).color;
                    probe.remove();
                    return {actual: getComputedStyle(node).color, expected};
                }"""
            )
            expect(circular).to_have_css("color", color["expected"])
        else:
            connection = page.locator("[data-agent-tunnel-reconnect]")
            expect(connection).to_have_class("circular-icon-button")
            expect(connection).to_have_css("width", f"{expected_size}px")
            expect(connection).to_have_css("height", f"{expected_size}px")
    except AssertionError:
        capture_failure()
        raise
    finally:
        close_observer()
        context.close()
