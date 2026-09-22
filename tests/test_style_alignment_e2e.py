"""Shared component annotation regressions. Code version: v1.5.0-codex.0."""

import pytest
from playwright.sync_api import expect

from tests import test_sidebar_e2e

disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url


def assert_field_title_contract(locator):
    expect(locator).to_have_css("font-size", "15px")
    expect(locator).to_have_css("font-weight", "400")
    expect(locator).to_have_css("line-height", "normal")
    expect(locator).to_have_css("letter-spacing", "normal")
    expect(locator).to_have_css("color", "rgb(11, 12, 12)")


@pytest.mark.parametrize("width", [1024, 800, 390])
def test_shared_component_annotations(disposable_browser, sidebar_server_url, width):
    context = disposable_browser.new_context(viewport={"width": width, "height": 863})
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens")
        expect(page.locator('[data-style-token-card="workspace-article"]')).to_have_count(0)
        controls = page.locator('.style-token-demo .browser-filter-select-trigger')
        assert controls.count() >= 2
        for control in controls.all():
            expect(control).to_have_css("height", "30px")
        secondary = page.locator('.style-token-secondary-button-demo .secondary-button')
        expect(secondary).to_have_css("font-size", "13px")
        expect(secondary).to_have_css("height", "32px")
        assert secondary.evaluate("e => Math.abs(e.getBoundingClientRect().right - e.closest('.style-token-demo').getBoundingClientRect().right) <= 1")
        assert secondary.evaluate("e => e.getBoundingClientRect().width < e.closest('.style-token-demo').getBoundingClientRect().width")
        closes = page.locator('.style-token-modal-demo > .workspace-modal-close')
        expect(closes).to_have_count(2)
        for close in closes.all():
            surface = close.locator('..')
            expect(surface).to_have_css("padding", "12px")
            expect(close).to_have_css("width", "24px")
            expect(close).to_have_css("height", "24px")
            expect(close).to_have_css("border-radius", "50%")
            geometry = surface.evaluate(
                """node => {
                    const button = node.querySelector('.workspace-modal-close');
                    const icon = node.querySelector('.workspace-modal-icon');
                    const title = node.querySelector('.workspace-modal-title, .notice-floating-banner-heading');
                    const copyNode = node.querySelector('.workspace-modal-copy, .notice-floating-banner-content, .notice-floating-banner-copy, .notice-floating-banner-list');
                    const listNode = node.querySelector('.notice-floating-banner-list');
                    const firstItem = listNode?.querySelector('li');
                    let wrappedLineLefts = [];
                    if (firstItem?.firstChild) {
                        const range = document.createRange();
                        range.selectNodeContents(firstItem.firstChild);
                        wrappedLineLefts = Array.from(range.getClientRects(), rect => rect.left);
                    }
                    return {
                        centerTop: button.offsetTop + (button.offsetHeight / 2),
                        centerLeft: button.offsetLeft + (button.offsetWidth / 2),
                        titleCenterTop: title.offsetTop + (title.offsetHeight / 2),
                        closeLeft: button.offsetLeft,
                        iconLeft: icon.offsetLeft,
                        iconRight: icon.offsetLeft + icon.offsetWidth,
                        titleLeft: title.offsetLeft,
                        iconTop: icon.offsetTop,
                        copyTop: copyNode.offsetTop,
                        copyLeft: copyNode.offsetLeft,
                        listStylePosition: listNode ? getComputedStyle(listNode).listStylePosition : '',
                        wrappedLineLefts,
                        fitsInline: node.scrollWidth <= node.clientWidth,
                    };
                }"""
            )
            assert abs(geometry["centerTop"] - geometry["centerLeft"]) <= 1
            assert abs(geometry["titleCenterTop"] - geometry["centerTop"]) <= 1
            assert abs(geometry["iconLeft"] - geometry["closeLeft"]) <= 1
            assert abs(geometry["iconTop"] - geometry["copyTop"]) <= 1
            assert abs(geometry["titleLeft"] - geometry["iconRight"] - 12) <= 1
            assert abs(geometry["copyLeft"] - geometry["titleLeft"]) <= 1
            assert geometry["fitsInline"]
            if geometry["wrappedLineLefts"]:
                assert geometry["listStylePosition"] == "outside"
                assert len(geometry["wrappedLineLefts"]) >= 2
                assert max(geometry["wrappedLineLefts"]) - min(geometry["wrappedLineLefts"]) <= 1
            page.mouse.move(0, 0)
            expect(close).to_have_css("opacity", "0")
            close.locator('..').hover()
            expect(close).to_have_css("opacity", "1")
            expect(close).to_have_css("color", "rgb(200, 30, 30)")
            page.mouse.move(0, 0)
            close.focus()
            expect(close).to_have_css("opacity", "1")
            close.evaluate("e => e.blur()")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


def test_touch_dismiss_visibility(disposable_browser, sidebar_server_url):
    context = disposable_browser.new_context(has_touch=True, is_mobile=True, viewport={"width": 390, "height": 863})
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens")
        controls = page.locator('.style-token-modal-demo > .workspace-modal-close')
        assert controls.count() == 2
        for close in controls.all():
            expect(close).to_have_css("opacity", "1")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1024, 390])
def test_runtime_modal_and_banner_use_the_shared_title_body_grid(
    disposable_browser,
    sidebar_server_url,
    width,
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 863})
    page = context.new_page()
    try:
        page.goto(
            f"{sidebar_server_url}/browser?view=media&source=chatgpt"
            "&session_updated=1&session_discovered=2&session_cached=3"
        )
        banner = page.locator("[data-chatgpt-session-refresh-banner]")
        expect(banner).to_be_visible()
        modal = page.locator("#cache_wait_modal .workspace-modal-dialog")
        page.locator("#cache_wait_modal").evaluate("node => { node.hidden = false; }")
        expect(modal).to_be_visible()

        for surface in (banner, modal):
            close = surface.locator(".workspace-modal-close, .notice-close")
            assert close.get_attribute("aria-label")
            geometry = surface.evaluate(
                """node => {
                    const close = node.querySelector('.workspace-modal-close, .notice-close');
                    const icon = node.querySelector('.workspace-modal-icon');
                    const title = node.querySelector('.workspace-modal-title, .notice-floating-banner-heading');
                    const body = node.querySelector('.workspace-modal-copy, .notice-floating-banner-content, .notice-floating-banner-copy, .notice-floating-banner-list');
                    return {
                        centerTop: close.offsetTop + (close.offsetHeight / 2),
                        centerLeft: close.offsetLeft + (close.offsetWidth / 2),
                        titleCenterTop: title.offsetTop + (title.offsetHeight / 2),
                        iconTop: icon.offsetTop,
                        bodyTop: body.offsetTop,
                        bodyLeft: body.offsetLeft,
                        titleLeft: title.offsetLeft,
                        fitsInline: node.scrollWidth <= node.clientWidth,
                    };
                }"""
            )
            assert abs(geometry["centerTop"] - geometry["centerLeft"]) <= 1
            assert abs(geometry["titleCenterTop"] - geometry["centerTop"]) <= 1
            assert abs(geometry["iconTop"] - geometry["bodyTop"]) <= 1
            assert abs(geometry["bodyLeft"] - geometry["titleLeft"]) <= 1
            assert geometry["fitsInline"]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


@pytest.mark.parametrize(
    ("width", "scheme", "touch", "motion"),
    [
        (1024, "light", False, "no-preference"),
        (1024, "dark", False, "reduce"),
        (390, "light", True, "reduce"),
        (390, "dark", True, "no-preference"),
    ],
)
def test_shared_primitive_catalog_geometry_and_states(
    disposable_browser,
    sidebar_server_url,
    width,
    scheme,
    touch,
    motion,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900},
        color_scheme=scheme,
        has_touch=touch,
        is_mobile=touch,
        reduced_motion=motion,
    )
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens", wait_until="domcontentloaded")
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

        circular = page.locator(".style-token-round-icon-demo")
        expect(circular).to_have_class("circular-icon-button settings-round-icon-button style-token-round-icon-demo")
        expect(circular).to_have_css("width", "36px")
        expect(circular).to_have_css("height", "36px")
        circular.focus()
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")
        expect(circular).to_be_focused()
        page.wait_for_timeout(220)
        focused_circular = circular.evaluate(
            """node => ({
                color: getComputedStyle(node).color,
                expected: (() => {
                    const probe = document.createElement('span');
                    probe.style.color = 'var(--circular-icon-button-color-hover)';
                    node.append(probe);
                    const color = getComputedStyle(probe).color;
                    probe.remove();
                    return color;
                })(),
            })"""
        )
        assert focused_circular["color"] == focused_circular["expected"]

        segmented = page.locator("#segmented-control .segmented-control")
        segmented_geometry = segmented.evaluate(
            """node => {
                const owner = node.closest('.style-token-demo').getBoundingClientRect();
                const box = node.getBoundingClientRect();
                const widths = Array.from(node.querySelectorAll('.range-mode-option'), child => child.getBoundingClientRect().width);
                return {
                    centered: Math.abs((box.left + box.width / 2) - (owner.left + owner.width / 2)),
                    compact: box.width < owner.width,
                    widths,
                    transitionDuration: getComputedStyle(node).transitionDuration,
                };
            }"""
        )
        assert segmented_geometry["centered"] <= 1
        assert segmented_geometry["compact"]
        assert max(segmented_geometry["widths"]) - min(segmented_geometry["widths"]) <= 1
        if motion == "reduce":
            assert segmented_geometry["transitionDuration"] in {"0s", "0.001s"}

        metric = page.locator('[data-style-token-card="workspace-metric-value"] strong')
        expect(metric).to_have_text("2,032.15%")
        expect(metric).to_have_attribute("aria-label", "2,032.15%")
        expect(metric.locator(".workspace-metric-value-major")).to_have_text("2,032")
        expect(metric.locator(".workspace-metric-value-minor")).to_have_text(".15")
        expect(metric.locator(".workspace-metric-value-suffix")).to_have_text("%")
        assert metric.locator(":scope > span").evaluate_all(
            "nodes => nodes.every(node => node.getAttribute('aria-hidden') === 'true')"
        )

        monetary_value = page.locator(
            '[data-style-token-demo="scrollable-data-table"] '
            'span[data-numeric-display-value][data-currency-code="USD"]'
        )
        expect(monetary_value).to_have_attribute("aria-label", "$7,089.68")
        expect(
            monetary_value.locator(".workspace-metric-value-major")
        ).to_have_text("$7,089")
        expect(
            monetary_value.locator(".workspace-metric-value-minor")
        ).to_have_text(".68")
        monetary_sizes = monetary_value.evaluate(
            """node => ({
                major: parseFloat(getComputedStyle(node.querySelector('.workspace-metric-value-major')).fontSize),
                minor: parseFloat(getComputedStyle(node.querySelector('.workspace-metric-value-minor')).fontSize),
            })"""
        )
        assert abs(monetary_sizes["minor"] / monetary_sizes["major"] - 0.76) < 0.01

        table_shell = page.locator(".style-token-table-demo-surface")
        table_geometry = table_shell.evaluate(
            """node => {
                const header = node.querySelector(':scope > table[data-table-header]');
                const scroll = node.querySelector(':scope > [data-table-scroll]');
                const body = scroll?.querySelector(':scope > table[data-table-body]');
                const headerCells = Array.from(header?.rows[0]?.cells || [], cell => cell.getBoundingClientRect().width);
                const bodyCells = Array.from(body?.rows[0]?.cells || [], cell => cell.getBoundingClientRect().width);
                return {
                    directHeader: header?.parentElement === node,
                    directScroll: scroll?.parentElement === node,
                    bodyInScroll: body?.parentElement === scroll,
                    headerCells,
                    bodyCells,
                    singleScrollOwner: getComputedStyle(scroll).overflowX === 'auto'
                        && getComputedStyle(node).overflow === 'hidden',
                };
            }"""
        )
        assert table_geometry["directHeader"]
        assert table_geometry["directScroll"]
        assert table_geometry["bodyInScroll"]
        assert table_geometry["singleScrollOwner"]
        assert len(table_geometry["headerCells"]) == len(table_geometry["bodyCells"]) == 5
        for header_width, body_width in zip(
            table_geometry["headerCells"], table_geometry["bodyCells"], strict=True
        ):
            assert abs(header_width - body_width) <= 2

        pagination = page.locator("#pagination [data-style-token-pagination]")
        next_button = pagination.get_by_role("button", name="Next session page")
        next_button.focus()
        page.keyboard.press("Shift+Tab")
        page.keyboard.press("Tab")
        expect(next_button).to_be_focused()
        page.wait_for_timeout(220)
        pagination_state = next_button.evaluate(
            """node => {
                const icon = node.querySelector('.icon');
                const probe = document.createElement('span');
                probe.style.color = 'var(--local-store-pagination-button-color-hover)';
                node.append(probe);
                const result = {
                    color: getComputedStyle(node).color,
                    iconColor: getComputedStyle(icon).backgroundColor,
                    expected: getComputedStyle(probe).color,
                    transitionDuration: getComputedStyle(node).transitionDuration,
                };
                probe.remove();
                return result;
            }"""
        )
        assert pagination_state["color"] == pagination_state["expected"]
        assert pagination_state["iconColor"] == pagination_state["expected"]
        if motion == "reduce":
            assert pagination_state["transitionDuration"].split(",")[0].strip() == "0.001s"
        if not touch:
            page.mouse.move(0, 0)
            page.get_by_role("button", name="Session page 2", exact=True).hover()
            page.wait_for_timeout(220)
            assert page.get_by_role("button", name="Session page 2", exact=True).evaluate(
                "node => getComputedStyle(node).color"
            ) == pagination_state["expected"]
    finally:
        context.close()


@pytest.mark.parametrize("width", [1024, 390])
def test_field_titles_match_the_agent_reference_at_shared_breakpoints(
    disposable_browser,
    sidebar_server_url,
    width,
):
    context = disposable_browser.new_context(viewport={"width": width, "height": 863})
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens")
        for selector in (
            ".style-token-agent-browser-demo .style-token-component-kicker",
            ".style-token-scrollable-table thead th:nth-child(2)",
            ".style-token-text-input-demo > span:first-child",
        ):
            assert_field_title_contract(page.locator(selector))

        page.goto(
            f"{sidebar_server_url}/browser?view=text&source=all&kind=all"
            "&q=&sort=newest&session_view=1"
        )
        filter_titles = page.locator(".browser-filter-field > span")
        assert filter_titles.count() >= 2
        for title in filter_titles.all():
            assert_field_title_contract(title)
        table_headers = page.locator(".browser-session-table thead th")
        if table_headers.count():
            for header in table_headers.all():
                assert_field_title_contract(header)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


@pytest.mark.parametrize("width", [1024, 390])
@pytest.mark.parametrize("logged_in", [None, False])
def test_account_probe_failure_can_recheck_without_signing_in(disposable_browser, sidebar_server_url, width, logged_in):
    context = disposable_browser.new_context(viewport={"width": width, "height": 863})
    page = context.new_page()
    requests = []
    def probe(route):
        requests.append(route.request.url)
        ready = len(requests) > 1
        route.fulfill(json={
            "platform": "chatgpt", "browser": "edge", "browser_label": "Edge",
            "logged_in": True if ready else logged_in, "can_download": ready,
            "message": "Ready" if ready else "Could not verify: net::ERR_CONNECTION_CLOSED",
            "agent_sources": {"recent_sessions": [], "projects": []},
        })
    page.route("**/api/browser-session**", probe)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        if width < 900:
            page.get_by_role("button", name="Toggle sidebar", exact=True).click()
        message = page.locator('[data-role="browser-session-message"]')
        retry = page.get_by_role("button", name="Recheck", exact=True)
        expect(message).to_contain_text("ERR_CONNECTION_CLOSED")
        login = page.locator('[data-role="browser-session-login"]')
        if logged_in is None:
            expect(login).to_be_hidden()
        else:
            expect(login).to_be_visible()
            assert login.evaluate("e => Math.abs(e.getBoundingClientRect().right - e.parentElement.getBoundingClientRect().right) <= 1")
        expect(retry).to_be_visible()
        assert retry.evaluate("e => Math.abs(e.getBoundingClientRect().right - e.parentElement.getBoundingClientRect().right) <= 1")
        retry.click()
        expect(retry).to_be_hidden()
        expect(message).to_be_hidden()
        expect(page.locator('[data-role="browser-session-checkmark"]')).to_have_attribute("data-status-state", "ready")
        assert any("refresh=1" in url for url in requests)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()
