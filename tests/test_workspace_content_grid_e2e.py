"""Rendered coverage for the shared twelve-track workspace grid.

Code version: v1.0.0-codex.0
"""

import pytest
from playwright.sync_api import Browser

from tests import test_sidebar_e2e
from tests import test_wechat_contact_analysis_e2e


disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url
contact_analysis_server = test_wechat_contact_analysis_e2e.contact_analysis_server


ROUTES = (
    "/",
    *(f"/cache/{key}" for key in (
        "chatgpt", "chatgpt_openclaw", "claude", "claude_code",
        "claude_openclaw", "codex", "copilot", "doubao_openclaw",
        "gemini", "gemini_openclaw", "grok", "grok_build",
        "grok_openclaw", "other_openclaw", "unknown_openclaw",
        "x", "zhihu", "zhipu_openclaw",
    )),
    "/browser?view=media&source=chatgpt",
    "/browser?view=text&source=chatgpt",
    "/browser?view=prompts&source=chatgpt",
    "/agent/tunnel/chatgpt",
    "/agent/tunnel/claude",
    "/agent/tunnel/gemini",
    "/jury",
    "/iphone",
    "/settings",
    "/settings/style-tokens",
    *(f"/beta/{key}" for key in (
        "idea-collision", "context-capsule", "question-radar", "memory-diff",
        "decision-wind-tunnel", "mission-forge", "echo-atlas", "curiosity-trail",
    )),
)


@pytest.mark.integration
@pytest.mark.parametrize("width", (390, 834, 1_512))
def test_workspace_routes_keep_twelve_tracks_with_sidebar_states(
    disposable_browser: Browser, sidebar_server_url: str, width: int
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 982}, reduced_motion="reduce"
    )
    page = context.new_page()
    try:
        for route in ROUTES:
            response = page.goto(f"{sidebar_server_url}{route}", wait_until="domcontentloaded")
            assert response is not None and response.status == 200, route
            page.locator('[data-layout-role="content-grid"]').first.wait_for()
            for sidebar_open in (False, True):
                if sidebar_open:
                    page.locator('[data-layout-role="sidebar-toggle"]').click()
                geometry = page.evaluate(
                    """() => {
                        const grid = [...document.querySelectorAll('[data-layout-role="content-grid"]')]
                            .find(element => element.getClientRects().length &&
                                getComputedStyle(element).display === 'grid');
                        if (!grid) return {error: 'No visible content grid', candidates:
                            [...document.querySelectorAll('[data-layout-role="content-grid"]')]
                                .map(element => ({tag: element.tagName, display: getComputedStyle(element).display,
                                    rects: element.getClientRects().length}))};
                        const style = getComputedStyle(grid);
                        const tracks = style.gridTemplateColumns.split(/\\s+/).filter(Boolean);
                        const rect = grid.getBoundingClientRect();
                        const scrollport = grid.closest('[data-layout-role="content-scrollport"]');
                        return {
                            tracks: tracks.length,
                            columns: style.gridTemplateColumns,
                            gap: style.columnGap,
                            rowGap: style.rowGap,
                            width: rect.width,
                            overflow: document.documentElement.scrollWidth - innerWidth,
                            scrollOwner: Boolean(scrollport),
                            scrollOverflow: scrollport ? getComputedStyle(scrollport).overflowY : null,
                        };
                    }"""
                )
                assert "error" not in geometry, (route, width, sidebar_open, geometry)
                assert geometry["tracks"] == 12, (route, width, sidebar_open, geometry)
                assert geometry["gap"] == "12px", (route, width, geometry)
                assert geometry["rowGap"] == "12px", (route, width, geometry)
                assert geometry["width"] > 0, (route, width, geometry)
                assert geometry["overflow"] <= 1, (route, width, sidebar_open, geometry)
                if route != "/jury":
                    assert geometry["scrollOwner"], (route, width, geometry)
    finally:
        context.close()


@pytest.mark.integration
def test_beta_textarea_uses_continuous_corner_shape(
    disposable_browser: Browser, sidebar_server_url: str
) -> None:
    context = disposable_browser.new_context(viewport={"width": 834, "height": 982})
    page = context.new_page()
    try:
        response = page.goto(
            f"{sidebar_server_url}/beta/idea-collision", wait_until="domcontentloaded"
        )
        assert response is not None and response.status == 200
        shape = page.locator(".beta-page textarea.text-input-control").first.evaluate(
            "element => getComputedStyle(element).cornerShape"
        )
        assert shape in {"squircle", "superellipse(2)"}
    finally:
        context.close()


@pytest.mark.integration
def test_visible_painted_rounded_surfaces_use_continuous_corners(
    disposable_browser: Browser, sidebar_server_url: str
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": 834, "height": 982}, color_scheme="dark"
    )
    page = context.new_page()
    failures: dict[str, list[dict[str, str]]] = {}
    try:
        for route in ROUTES:
            response = page.goto(f"{sidebar_server_url}{route}", wait_until="domcontentloaded")
            assert response is not None and response.status == 200, route
            missing = page.evaluate(
                """() => {
                    const good = new Set(['squircle', 'superellipse(2)']);
                    const visible = s => s.display !== 'none' && s.visibility !== 'hidden' &&
                        Number(s.opacity) > 0;
                    const painted = (el, s) =>
                        !['transparent', 'rgba(0, 0, 0, 0)'].includes(s.backgroundColor) ||
                        s.backgroundImage !== 'none' || s.boxShadow !== 'none' ||
                        s.backdropFilter !== 'none' ||
                        (parseFloat(s.borderTopWidth) > 0 && s.borderTopStyle !== 'none') ||
                        (parseFloat(s.outlineWidth) > 0 && s.outlineStyle !== 'none') ||
                        ['hidden', 'clip'].includes(s.overflow) ||
                        ['IMG', 'VIDEO', 'CANVAS'].includes(el.tagName);
                    const misses = [];
                    for (const el of document.querySelectorAll('body *')) {
                        if (!el.getClientRects().length) continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width < 2 || rect.height < 2) continue;
                        for (const pseudo of ['', '::before', '::after']) {
                            const s = getComputedStyle(el, pseudo || undefined);
                            if (!visible(s)) continue;
                            if (pseudo && (s.content === 'none' || s.content === 'normal')) continue;
                            const width = pseudo ? parseFloat(s.width) : rect.width;
                            const height = pseudo ? parseFloat(s.height) : rect.height;
                            const side = Math.min(width, height);
                            const corners = [s.borderTopLeftRadius, s.borderTopRightRadius,
                                s.borderBottomRightRadius, s.borderBottomLeftRadius];
                            const radii = corners.map(value => value.includes('%') ?
                                parseFloat(value) * side / 100 : parseFloat(value));
                            if (!radii.some(radius => Number.isFinite(radius) && radius > 0)) continue;
                            if (Number.isFinite(side) &&
                                radii.every(radius => radius >= side / 2 - 0.5)) continue;
                            if (!painted(el, s) || good.has(s.cornerShape)) continue;
                            misses.push({selector: el.tagName.toLowerCase() +
                                (el.id ? '#' + el.id : '') +
                                (typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '') + pseudo,
                                radius: s.borderRadius, shape: s.cornerShape});
                        }
                    }
                    return misses.slice(0, 30);
                }"""
            )
            if missing:
                failures[route] = missing
        assert not failures, failures
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize("scheme", ("light", "dark"))
def test_open_menu_and_modal_keep_continuous_clipping_surfaces(
    disposable_browser: Browser, sidebar_server_url: str, scheme: str
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": 834, "height": 982}, color_scheme=scheme
    )
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens", wait_until="domcontentloaded")
        trigger = page.locator("[data-style-token-shared-filter-trigger]").first
        trigger.click()
        menu = page.locator("[data-style-token-shared-filter-menu]").first
        assert menu.is_visible()
        shape = menu.evaluate("element => getComputedStyle(element).cornerShape")
        assert shape in {"squircle", "superellipse(2)"}
        metric = page.locator(".style-token-metric-card-demo").first
        metric.scroll_into_view_if_needed()
        metric_style = metric.evaluate(
            "element => ({radius: getComputedStyle(element).borderTopLeftRadius, "
            "shape: getComputedStyle(element).cornerShape})"
        )
        assert metric_style["radius"] != "0px"
        assert metric_style["shape"] in {"squircle", "superellipse(2)"}
        page.evaluate("location.hash = '#circular-icon-button'")
        target = page.locator("#circular-icon-button")
        pseudo = target.evaluate(
            """element => {
                const style = getComputedStyle(element, '::after');
                return {opacity: style.opacity, radius: style.borderTopLeftRadius,
                    shape: style.cornerShape};
            }"""
        )
        assert pseudo["opacity"] == "1"
        assert pseudo["radius"] != "0px"
        assert pseudo["shape"] in {"squircle", "superellipse(2)"}

        page.goto(f"{sidebar_server_url}/cache/chatgpt", wait_until="domcontentloaded")
        page.locator("#cache_wait_modal").evaluate("element => element.hidden = false")
        dialog = page.locator("#cache_wait_modal .workspace-modal-dialog")
        assert dialog.is_visible()
        shape = dialog.evaluate("element => getComputedStyle(element).cornerShape")
        assert shape in {"squircle", "superellipse(2)"}
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize("width", (390, 834, 1_512))
def test_workspace_content_grid_aligns_with_title_rail(
    disposable_browser: Browser, sidebar_server_url: str, width: int
) -> None:
    routes = (
        "/cache/codex", "/browser?view=media&source=chatgpt",
        "/agent/tunnel/chatgpt", "/jury", "/iphone", "/settings",
        "/settings/style-tokens", "/beta/idea-collision",
    )
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 982}, reduced_motion="reduce"
    )
    page = context.new_page()
    failures = []
    try:
        for route in routes:
            page.goto(f"{sidebar_server_url}{route}", wait_until="domcontentloaded")
            for sidebar_open in (False, True):
                if sidebar_open:
                    page.locator('[data-layout-role="sidebar-toggle"]').click()
                title_role = "title-heading" if route == "/settings" else "title-rail"
                expected_inset = (
                    12 if route.startswith(("/cache/", "/browser?"))
                    else 48 if route.startswith("/agent/") else 0
                )
                delta = page.evaluate(
                    """role => {
                        const title = document.querySelector(`[data-layout-role="${role}"]`);
                        const grid = document.querySelector('[data-layout-role="content-grid"]');
                        return grid.getBoundingClientRect().left -
                            title.getBoundingClientRect().left;
                    }""",
                    title_role,
                )
                if abs(delta - expected_inset) > 1:
                    failures.append((route, sidebar_open, delta, expected_inset))
        assert not failures, (width, failures)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize("width", (390, 834, 1_512))
def test_wechat_contact_analysis_uses_the_content_grid(
    disposable_browser: Browser, contact_analysis_server: object, width: int
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 982}, color_scheme="dark"
    )
    page = context.new_page()
    try:
        response = page.goto(
            f"{contact_analysis_server.url}/browser/wechat/contacts/"
            f"{contact_analysis_server.target_id}",
            wait_until="domcontentloaded",
        )
        assert response is not None and response.status == 200
        geometry = page.locator('[data-layout-role="content-grid"]').first.evaluate(
            """grid => {
                const style = getComputedStyle(grid);
                const table = document.querySelector('.wechat-contact-table-shell');
                return {
                    tracks: style.gridTemplateColumns.split(/\\s+/).filter(Boolean).length,
                    gap: style.columnGap,
                    rowGap: style.rowGap,
                    overflow: document.documentElement.scrollWidth - innerWidth,
                    tableShape: table ? getComputedStyle(table).cornerShape : null,
                };
            }"""
        )
        assert geometry["tracks"] == 12
        assert geometry["gap"] == "12px"
        assert geometry["rowGap"] == "12px"
        assert geometry["overflow"] <= 1
        assert geometry["tableShape"] in {"squircle", "superellipse(2)"}
    finally:
        context.close()
