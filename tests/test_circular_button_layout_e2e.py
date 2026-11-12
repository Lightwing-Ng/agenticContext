"""Round-action sizing and shared-shell anchor checks. Code version: v1.1.2-codex.0."""

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e

disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url


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
    try:
        page.goto(f"{sidebar_server_url}{route}", wait_until="domcontentloaded")
        toggle = page.locator('[data-layout-role="sidebar-toggle"]')
        theme = page.locator('[data-layout-role="global-theme-anchor"]')
        title_rail = page.locator('[data-layout-role="title-rail"]')
        expect(title_rail).to_have_css("transition-property", "none")
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
    finally:
        context.close()


@pytest.mark.parametrize("width", [390, 820, 1006, 1080, 1081])
def test_global_notice_preserves_top_action_hit_regions(
    disposable_browser, sidebar_server_url, width
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 791},
        has_touch=True,
        is_mobile=True,
    )
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/browser?view=text", wait_until="domcontentloaded")
        toggle = page.locator("#sidebar_toggle")
        theme = page.locator("#global_theme_toggle")
        notice = page.locator("[data-chatgpt-session-refresh-banner]")
        if toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        page.wait_for_timeout(500)
        notice.evaluate(
            "node => { node.hidden = false; node.setAttribute('aria-hidden', 'false'); }"
        )
        expect(notice).to_be_visible()
        for _ in range(13):
            top_controls_clear = page.evaluate(
                """() => ['#sidebar_toggle', '#global_theme_toggle'].every(selector => {
                    const node = document.querySelector(selector);
                    const rect = node.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    return [[0, 0], [-20, 0], [20, 0], [0, -20], [0, 20]]
                        .every(([dx, dy]) => {
                            const hit = document.elementFromPoint(centerX + dx, centerY + dy);
                            return hit === node || node.contains(hit);
                        });
                })"""
            )
            assert top_controls_clear
            page.wait_for_timeout(60)

        for expanded in (True, False):
            expected = str(expanded).lower()
            if toggle.get_attribute("aria-expanded") != expected:
                toggle.click()
            expect(toggle).to_have_attribute("aria-expanded", expected)
            page.wait_for_timeout(500)
            geometry = page.evaluate(
                """() => {
                    const target = document.querySelector('#sidebar_toggle');
                    const notice = document.querySelector('[data-chatgpt-session-refresh-banner]');
                    const rect = target.getBoundingClientRect();
                    const banner = notice.getBoundingClientRect();
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    return {
                        centerX, centerY, noticeTop: banner.top,
                        edgeHits: [[0, 0], [-20, 0], [20, 0], [0, -20], [0, 20]]
                            .map(([dx, dy]) => {
                                const hit = document.elementFromPoint(centerX + dx, centerY + dy);
                                return hit === target || target.contains(hit);
                            }),
                    };
                }"""
            )
            assert all(geometry["edgeHits"]), geometry
            if width <= 1080:
                assert geometry["noticeTop"] >= geometry["centerY"] + 22 + 4 + 28 - 1
            page.touchscreen.tap(geometry["centerX"] + 20, geometry["centerY"])
            expect(toggle).to_have_attribute("aria-expanded", str(not expanded).lower())

        theme_geometry = theme.evaluate(
            """node => {
                const rect = node.getBoundingClientRect();
                const centerX = rect.left + rect.width / 2;
                const centerY = rect.top + rect.height / 2;
                return {
                    centerX, centerY,
                    edgeHits: [[0, 0], [-20, 0], [20, 0], [0, -20], [0, 20]]
                        .map(([dx, dy]) => {
                            const hit = document.elementFromPoint(centerX + dx, centerY + dy);
                            return hit === node || node.contains(hit);
                        }),
                };
            }"""
        )
        assert all(theme_geometry["edgeHits"]), theme_geometry
        page.touchscreen.tap(theme_geometry["centerX"] - 20, theme_geometry["centerY"])
        expect(page.locator("html")).to_have_attribute("data-theme-override", "dark")

        notice.evaluate(
            """node => {
                const second = node.cloneNode(true);
                second.removeAttribute('data-chatgpt-session-refresh-banner');
                second.setAttribute('data-test-stacked-notice', '');
                second.querySelector('.notice-close').addEventListener('click', () => {
                    second.setAttribute('data-test-dismiss-hit', 'true');
                });
                node.after(second);
            }"""
        )
        second = page.locator("[data-test-stacked-notice]")
        page.wait_for_timeout(800)
        first_rect = notice.bounding_box()
        second_rect = second.bounding_box()
        assert second_rect["y"] - first_rect["y"] == pytest.approx(88, abs=1)
        for banner in (notice, second):
            dismiss = banner.locator(".notice-close")
            assert dismiss.evaluate(
                """node => {
                    const rect = node.getBoundingClientRect();
                    const hit = document.elementFromPoint(
                        rect.left + rect.width / 2, rect.top + rect.height / 2,
                    );
                    return hit === node || node.contains(hit);
                }"""
            )
        second.locator(".notice-close").click()
        expect(second).to_have_attribute("data-test-dismiss-hit", "true")

        close = notice.locator(".notice-close")
        expect(close).to_be_visible()
        close.click()
        expect(notice).to_be_hidden()
    finally:
        context.close()


@pytest.mark.parametrize(
    ("route", "width", "height", "touch"),
    [
        ("/agent/edge/chatgpt", 1280, 801, False),
        ("/agent/edge/claude", 1280, 801, False),
        ("/agent/edge/gemini", 1280, 801, False),
        ("/agent/edge/grok", 1280, 801, False),
        ("/agent/edge/chatgpt", 390, 844, True),
    ],
)
def test_agent_recent_sessions_scrolls_above_the_fixed_dock(
    disposable_browser, sidebar_server_url, route, width, height, touch
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
    )
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}{route}", wait_until="domcontentloaded")
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        scroller = page.locator("#agent_sidebar [data-layout-role='sidebar-scrollport']")
        details = page.locator("#agent_sidebar [data-agent-execution-sessions]")
        summary = details.locator("summary")
        expect(scroller).to_have_count(1)
        expect(details).to_have_attribute("open", "")
        summary.scroll_into_view_if_needed()
        geometry = page.evaluate(
            """() => {
                const scroller = document.querySelector(
                    '#agent_sidebar [data-layout-role="sidebar-scrollport"]'
                );
                const summary = document.querySelector(
                    '#agent_sidebar [data-agent-execution-sessions] summary'
                );
                const dock = document.querySelector('.sidebar-dock');
                const scrollRect = scroller.getBoundingClientRect();
                const summaryRect = summary.getBoundingClientRect();
                const dockRect = dock.getBoundingClientRect();
                const x = summaryRect.left + summaryRect.width / 2;
                const y = summaryRect.top + summaryRect.height / 2;
                const hit = document.elementFromPoint(x, y);
                return {
                    scrollBottom: scrollRect.bottom,
                    dockTop: dockRect.top,
                    summaryCenterY: y,
                    hitSummary: hit === summary || summary.contains(hit),
                };
            }"""
        )
        assert geometry["scrollBottom"] <= geometry["dockTop"] + 1, geometry
        assert geometry["summaryCenterY"] < geometry["scrollBottom"], geometry
        assert geometry["hitSummary"], geometry
        summary.click()
        assert details.get_attribute("open") is None
    finally:
        context.close()
