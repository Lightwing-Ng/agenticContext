"""Disposable-browser E2E coverage for the responsive sidebar and language boundaries.

Code version: v1.41.3-codex.1
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from io import BytesIO
import json
from pathlib import Path
import re
from threading import Thread
from urllib.parse import unquote

import pytest
from PIL import Image, ImageChops
from playwright.sync_api import (
    Browser,
    BrowserContext,
    BrowserType,
    Error as PlaywrightError,
    Page,
    Playwright,
    expect,
    sync_playwright,
)
from werkzeug.serving import BaseWSGIServer, make_server

from app.core.computer_use_agent import (
    _ProviderSessionBinding,
    _chatgpt_retry_control,
    _provider_turn_snapshot,
    _select_web_model,
    _submit_chromium_prompt,
    _submit_chromium_web_prompt,
    load_computer_use_settings,
    parse_agent_action,
)
from app.core.gemini_downloader import inspect_gemini_session
from app.core.resource_persistence import (
    CHATGPT_HISTORY_SCHEMA,
    ZHIHU_HISTORY_SCHEMA,
    write_parquet_rows_atomic,
)


OVERLAY_VIEWPORTS = (
    ("iPhone SE", 375, 667),
    ("iPhone 15 Pro", 393, 852),
    ("Narrow layout breakpoint", 560, 844),
    ("iPad mini portrait", 744, 1_133),
    ("iPad portrait", 768, 1_024),
    ("iPad Air portrait", 820, 1_180),
    ("11-inch iPad Pro portrait", 834, 1_194),
)
DESKTOP_VIEWPORTS = (
    ("iPad landscape and compact desktop", 1_024, 768),
    ("wide desktop", 1_512, 982),
)


@pytest.fixture(scope="module")
def sidebar_server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    from app.web.app import create_app

    sandbox = tmp_path_factory.mktemp("cachelikes-sidebar-e2e")
    settings_path = sandbox / "settings" / "computer-use-agent.json"
    runtime_root = sandbox / "computer-use-runtime"
    application = create_app(
        sandbox / "local-store",
        computer_use_settings_path=settings_path,
        computer_use_runtime_root=runtime_root,
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    assert application.extensions["computer_use_settings"]._settings_path == settings_path
    assert application.extensions["computer_use_agent_service"]._runtime_root == runtime_root
    server: BaseWSGIServer = make_server("127.0.0.1", 0, application, threaded=True)
    assert server.server_port != 8666
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.fixture()
def seeded_chatgpt_browser_server_url(tmp_path: Path) -> Iterator[str]:
    from app.web.app import create_app

    root = tmp_path / "local-store"
    write_parquet_rows_atomic(
        root / "llm" / "chatgpt" / "history.parquet",
        [
            {
                "schema_version": 1,
                "platform": "chatgpt",
                "conversation_id": "chatgpt-wrap-demo",
                "conversation_url": "https://chatgpt.com/c/chatgpt-wrap-demo",
                "conversation_title": "ChatGPT timestamp wrapping",
                "message_key": "chatgpt-wrap-demo:0:user",
                "turn_index": 0,
                "message_index": 0,
                "role": "user",
                "author_label": "You",
                "content_text": "A timestamp layout regression fixture.",
                "content_html": "",
                "content_sha256": "chatgpt-wrap-demo-hash",
                "source_links": [],
                "model_label": "",
                "first_seen_at": "2026-08-12T04:59:00Z",
                "last_seen_at": "2026-08-12T05:00:00Z",
            }
        ],
        CHATGPT_HISTORY_SCHEMA,
    )
    application = create_app(
        root,
        computer_use_settings_path=tmp_path / "settings" / "computer-use-agent.json",
        computer_use_runtime_root=tmp_path / "computer-use-runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    server: BaseWSGIServer = make_server("127.0.0.1", 0, application, threaded=True)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


@pytest.fixture()
def seeded_zhihu_browser_server_url(tmp_path: Path) -> Iterator[str]:
    """Serve one long Zhihu answer from an isolated formal cache."""

    from app.web.app import create_app

    root = tmp_path / "local-store"
    paragraphs = [
        f"Cached paragraph {index}: " + ("complete answer text " * 12)
        for index in range(1, 14)
    ]
    paragraphs[-1] += "Full answer final sentence."
    write_parquet_rows_atomic(
        root / "llm" / "zhihu" / "history.parquet",
        [
            {
                "schema_version": 1,
                "platform": "zhihu",
                "conversation_id": "2197549311",
                "conversation_url": (
                    "https://www.zhihu.com/question/495309288/answer/2197549311"
                ),
                "conversation_title": "Fixture Zhihu question",
                "message_key": "answer:2197549311",
                "turn_index": 1,
                "message_index": 0,
                "role": "answer",
                "author_label": "肥肥猫",
                "content_text": "\n\n".join(paragraphs),
                "content_html": "",
                "content_sha256": "fixture-zhihu-answer-hash",
                "source_links": [
                    "https://www.zhihu.com/question/495309288/answer/2197549311",
                    "https://www.zhihu.com/people/feifeimao",
                ],
                "model_label": "",
                "first_seen_at": "2021-10-30T11:37:00Z",
                "last_seen_at": "2021-10-31T05:01:00Z",
            },
            {
                "schema_version": 1,
                "platform": "zhihu",
                "conversation_id": "2200000000",
                "conversation_url": (
                    "https://www.zhihu.com/question/495309289/answer/2200000000"
                ),
                "conversation_title": "Other fixture question",
                "message_key": "answer:2200000000",
                "turn_index": 1,
                "message_index": 0,
                "role": "answer",
                "author_label": "Other Author",
                "content_text": "Other cached answer.",
                "content_html": "",
                "content_sha256": "fixture-other-zhihu-answer-hash",
                "source_links": [
                    "https://www.zhihu.com/question/495309289/answer/2200000000",
                    "https://www.zhihu.com/people/other-author",
                ],
                "model_label": "",
                "first_seen_at": "2021-10-29T11:37:00Z",
                "last_seen_at": "2021-10-29T12:01:00Z",
            },
            {
                "schema_version": 1,
                "platform": "zhihu",
                "conversation_id": "2180000000",
                "conversation_url": (
                    "https://www.zhihu.com/question/495309290/answer/2180000000"
                ),
                "conversation_title": "Earlier fixture question",
                "message_key": "answer:2180000000",
                "turn_index": 1,
                "message_index": 0,
                "role": "answer",
                "author_label": "肥肥猫",
                "content_text": "Earlier complete cached answer.",
                "content_html": "",
                "content_sha256": "fixture-earlier-zhihu-answer-hash",
                "source_links": [
                    "https://www.zhihu.com/question/495309290/answer/2180000000",
                    "https://www.zhihu.com/people/feifeimao",
                ],
                "model_label": "",
                "first_seen_at": "2021-10-28T11:37:00Z",
                "last_seen_at": "2021-10-28T12:01:00Z",
            },
        ],
        ZHIHU_HISTORY_SCHEMA,
    )
    application = create_app(
        root,
        computer_use_settings_path=tmp_path / "settings" / "computer-use-agent.json",
        computer_use_runtime_root=tmp_path / "computer-use-runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    server: BaseWSGIServer = make_server("127.0.0.1", 0, application, threaded=True)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def _launch_disposable_browser(playwright: Playwright) -> Browser:
    browser_type: BrowserType = playwright.chromium
    managed_executable = Path(browser_type.executable_path)
    if managed_executable.is_file():
        return browser_type.launch(headless=True)

    launch_errors: list[str] = []
    for channel in ("chrome", "msedge"):
        try:
            return browser_type.launch(channel=channel, headless=True)
        except PlaywrightError as error:  # pragma: no cover - depends on host browser inventory
            launch_errors.append(f"{channel}: {error}")
    raise AssertionError(
        "A Playwright-managed Chromium, Chrome, or Edge executable is required. "
        + " | ".join(launch_errors)
    )


@pytest.fixture(scope="module")
def disposable_browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        browser = _launch_disposable_browser(playwright)
        try:
            yield browser
        finally:
            browser.close()


def _open_page(
    browser: Browser,
    url: str,
    width: int,
    height: int,
    *,
    touch: bool,
    init_script: str | None = None,
    reduced_motion: str | None = "reduce",
) -> tuple[Page, BrowserContext]:
    context_options = {
        "viewport": {"width": width, "height": height},
        "has_touch": touch,
        "is_mobile": touch,
    }
    if reduced_motion is not None:
        context_options["reduced_motion"] = reduced_motion
    context = browser.new_context(
        **context_options,
    )
    page = context.new_page()
    if init_script:
        page.add_init_script(init_script)
    page.goto(url, wait_until="domcontentloaded")
    return page, context


def _wait_for_global_title_rail(page: Page, title_selector: str) -> None:
    page.wait_for_function(
        """selector => {
            const centerY = element => {
                if (!(element instanceof HTMLElement)) return null;
                const rect = element.getBoundingClientRect();
                return rect.top + (rect.height / 2);
            };
            const title = centerY(document.querySelector(selector));
            const toggle = centerY(document.querySelector("#sidebar_toggle"));
            const theme = centerY(document.querySelector("#global_theme_toggle"));
            return title !== null
                && toggle !== null
                && theme !== null
                && Math.abs(title - toggle) <= 1
                && Math.abs(title - theme) <= 1;
        }""",
        arg=title_selector,
    )


def _assert_hidden_backdrop(page: Page) -> None:
    backdrop = page.locator("#sidebar_backdrop")
    expect(backdrop).to_be_hidden()
    expect(backdrop).to_have_attribute("hidden", "")
    assert backdrop.evaluate("element => getComputedStyle(element).display") == "none"
    assert backdrop.evaluate("element => getComputedStyle(element).pointerEvents") == "none"


def _assert_toggle_hit_target(page: Page) -> None:
    page.wait_for_function(
        """() => {
            const toggle = document.querySelector("#sidebar_toggle");
            if (!(toggle instanceof HTMLElement)) return false;
            const rect = toggle.getBoundingClientRect();
            if (rect.width < 44 || rect.height < 44) return false;
            const hit = document.elementFromPoint(
                rect.left + (rect.width / 2),
                rect.top + (rect.height / 2),
            );
            const rectKey = [rect.left, rect.top, rect.width, rect.height]
                .map(value => value.toFixed(3))
                .join(",");
            const previousRectKey = window.__cachelikesStableToggleRect || "";
            window.__cachelikesStableToggleRect = rectKey;
            return previousRectKey === rectKey && Boolean(hit?.closest("#sidebar_toggle"));
        }"""
    )


def _tap_toggle_center(page: Page, toggle) -> None:
    box = toggle.bounding_box()
    assert box is not None
    page.touchscreen.tap(box["x"] + (box["width"] / 2), box["y"] + (box["height"] / 2))


def _assert_agent_session_source_menu_is_hit_testable(page: Page) -> None:
    """Verify the source menu stays above both the inline list and the Dock."""
    trigger = page.locator(".agent-session-mode-combobox [data-agent-combobox-trigger]")
    trigger.click()
    project_option = page.locator(
        '.agent-session-mode-combobox [data-agent-combobox-option="project"]'
    )
    expect(project_option).to_be_visible()
    hit_test = project_option.evaluate(
        """option => {
            const rect = option.getBoundingClientRect();
            const point = {
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
            };
            const hit = document.elementFromPoint(point.x, point.y);
            const modeMenu = option.closest('.agent-session-mode-combobox')
                ?.querySelector('[data-agent-combobox-menu]');
            const dock = document.querySelector('.sidebar-dock');
            const modeMenuBox = modeMenu?.getBoundingClientRect();
            const dockBox = dock?.getBoundingClientRect();
            return {
                hitIsOption: hit === option || option.contains(hit),
                modeMenuAboveDock: modeMenuBox && dockBox
                    ? modeMenuBox.bottom <= dockBox.top + 1
                    : false,
                modeMenuZIndex: getComputedStyle(
                    option.closest('.agent-session-mode-combobox'),
                ).zIndex,
            };
        }"""
    )
    assert hit_test["modeMenuAboveDock"], hit_test
    assert hit_test["hitIsOption"], hit_test
    trigger.click()


def _decode_screenshot(png_bytes: bytes) -> Image.Image:
    with Image.open(BytesIO(png_bytes)) as image:
        return image.convert("RGBA")


@pytest.mark.integration
@pytest.mark.slow
def test_cache_source_switcher_reuses_the_complete_registry_across_cache_pages(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Verify every cache sidebar exposes the same complete source menu in Chromium."""
    expected_sources = ["chatgpt", "claude", "gemini", "grok", "x", "zhihu"]
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/chatgpt",
        1_280,
        900,
        touch=False,
    )
    try:
        for page_source in ("chatgpt", "claude", "gemini", "grok", "zhihu"):
            if page_source != "chatgpt":
                page.goto(f"{sidebar_server_url}/cache/{page_source}", wait_until="domcontentloaded")

            aside = page.locator("xpath=/html/body/main/div/aside")
            expect(aside).to_have_count(1)
            options = aside.locator("[data-cache-source-switcher-option]")
            expect(options).to_have_count(len(expected_sources))
            assert options.evaluate_all(
                "elements => elements.map(element => element.dataset.cacheSourceSwitcherOption)"
            ) == expected_sources
            source_trigger = aside.locator("[data-cache-source-switcher-trigger]")
            browser_trigger = aside.locator('[data-role="browser-picker-trigger"]')
            assert source_trigger.evaluate("element => element.getBoundingClientRect().height") == 36
            assert browser_trigger.evaluate("element => element.getBoundingClientRect().height") == 36
            expect(page.locator('[data-dock-section="cache"]')).to_have_class(re.compile(r"\bis-active\b"))
            expect(page.locator('[data-dock-section="agent"]')).not_to_have_class(re.compile(r"\bis-active\b"))
            expected_paths = [
                "/cache/chatgpt",
                "/cache/claude",
                "/cache/gemini",
                "/cache/grok",
                "/cache/x",
                "/cache/zhihu",
            ]
            assert options.evaluate_all(
                "elements => elements.map(element => element.dataset.cacheSourceSwitcherPath)"
            ) == expected_paths
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_cache_status_stays_in_the_progress_panel_without_a_floating_banner(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep Cache status in the existing progress panel without duplicating it as a banner."""
    route = f"{sidebar_server_url}/cache/chatgpt"
    page, context = _open_page(
        disposable_browser,
        route,
        1_280,
        900,
        touch=False,
    )
    try:
        for width, height in ((1_280, 900), (715, 899), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            page.goto(route, wait_until="domcontentloaded")
            expect(page.locator("#status_banner")).to_have_count(0)
            expect(page.locator('#message[data-status-field="message"]')).to_have_count(1)
            expect(page.locator('#message[data-status-field="message"]')).to_be_visible()
            status_card = page.locator(
                '[data-browser-session-panel] .browser-session-status-card'
            )
            expect(status_card).to_have_count(1)
            assert status_card.evaluate(
                "element => getComputedStyle(element).borderWidth"
            ) == "0px"
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_cache_events_card_stays_inside_content_scrollport_when_viewport_has_room(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep the empty Events surface inside the named scrollport on a tall desktop viewport."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/chatgpt",
        1_017,
        1_354,
        touch=False,
    )
    try:
        geometry = page.evaluate(
            """() => {
                const rect = selector => {
                    const element = document.querySelector(selector);
                    if (!element) return null;
                    const box = element.getBoundingClientRect();
                    return {top: box.top, bottom: box.bottom};
                };
                const scrollport = document.querySelector(
                    '[data-layout-role="content-scrollport"]',
                );
                return {
                    overview: rect('#overview'),
                    activity: rect('#activity'),
                    scrollport: rect('[data-layout-role="content-scrollport"]'),
                    scrollHeight: scrollport?.scrollHeight ?? 0,
                    clientHeight: scrollport?.clientHeight ?? 0,
                    documentOverflow: Math.max(
                        document.documentElement.scrollHeight,
                        document.body.scrollHeight,
                    ) - document.documentElement.clientHeight,
                };
            }"""
        )
        assert geometry["overview"] is not None
        assert geometry["activity"] is None
        assert geometry["scrollport"] is not None
        assert geometry["scrollHeight"] == geometry["clientHeight"]
        assert geometry["documentOverflow"] <= 1
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_cache_title_rail_stays_aligned_and_clear_when_the_sidebar_collapses(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Match the sibling title rail at desktop size in both sidebar states."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/chatgpt",
        1_024,
        900,
        touch=False,
    )
    try:
        toggle = page.locator("#sidebar_toggle")
        title = page.locator(".cache-overview-title-card .report-heading")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(title).to_have_text("ChatGPT cache overview")

        def read_geometry() -> dict[str, float]:
            return page.evaluate(
                """() => {
                    const toggle = document.querySelector("#sidebar_toggle").getBoundingClientRect();
                    const title = document.querySelector(
                        ".cache-overview-title-card .report-heading",
                    ).getBoundingClientRect();
                    const theme = document.querySelector("#global_theme_toggle").getBoundingClientRect();
                    const centerY = rect => rect.top + (rect.height / 2);
                    return {
                        titleCenterDelta: Math.abs(centerY(title) - centerY(toggle)),
                        themeCenterDelta: Math.abs(centerY(title) - centerY(theme)),
                        toggleGap: title.left - toggle.right,
                    };
                }"""
            )

        expanded = read_geometry()
        assert expanded["titleCenterDelta"] <= 1
        assert expanded["themeCenterDelta"] <= 1
        assert expanded["toggleGap"] >= 12

        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        page.wait_for_function(
            """() => {
                const toggle = document.querySelector("#sidebar_toggle").getBoundingClientRect();
                const title = document.querySelector(
                    ".cache-overview-title-card .report-heading",
                ).getBoundingClientRect();
                return title.left - toggle.right >= 12;
            }"""
        )
        collapsed = read_geometry()
        assert collapsed["titleCenterDelta"] <= 1
        assert collapsed["themeCenterDelta"] <= 1
        assert collapsed["toggleGap"] >= 12
        assert not page.evaluate(
            "Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) "
            "> document.documentElement.clientWidth"
        )

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function(
            """() => {
                const titleCard = document.querySelector(".cache-overview-title-card");
                return window.matchMedia("(max-width: 560px)").matches
                    && getComputedStyle(titleCard).paddingInlineEnd === "68px";
            }"""
        )
        expect(toggle).to_have_attribute("aria-expanded", "false")
        narrow = page.evaluate(
            """() => {
                const rectFor = selector => {
                    const rect = document.querySelector(selector).getBoundingClientRect();
                    return {left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom};
                };
                const overlaps = (left, right) => !(
                    left.right <= right.left
                    || left.left >= right.right
                    || left.bottom <= right.top
                    || left.top >= right.bottom
                );
                const toggle = rectFor("#sidebar_toggle");
                const titleElement = document.querySelector(
                    ".cache-overview-title-card .report-heading",
                );
                const title = rectFor(".cache-overview-title-card .report-heading");
                const theme = rectFor("#global_theme_toggle");
                const titleCard = rectFor(".cache-overview-title-card");
                const titleRow = rectFor(".cache-overview-title-card > .report-heading-row");
                const titleCardStyle = getComputedStyle(
                    document.querySelector(".cache-overview-title-card"),
                );
                const titleStyle = getComputedStyle(titleElement);
                const pageStyle = getComputedStyle(document.querySelector(".page"));
                return {
                    viewport: {
                        innerWidth: window.innerWidth,
                        clientWidth: document.documentElement.clientWidth,
                        devicePixelRatio: window.devicePixelRatio,
                    },
                    compactMediaMatches: window.matchMedia("(max-width: 560px)").matches,
                    pageClearance: pageStyle.getPropertyValue("--sidebar-toggle-quick-action-clearance"),
                    globalQuickActionsRight: pageStyle.getPropertyValue("--global-quick-actions-right"),
                    titleCard,
                    titleCardPaddingInlineEnd: titleCardStyle.paddingInlineEnd,
                    titleCardPaddingInlineStart: titleCardStyle.paddingInlineStart,
                    titleRow,
                    title,
                    titleWidth: titleElement.getBoundingClientRect().width,
                    titleMinWidth: titleStyle.minWidth,
                    titleMaxWidth: titleStyle.maxWidth,
                    theme,
                    toggle,
                    titleOverlapsTheme: overlaps(title, theme),
                    titleOverlapsToggle: overlaps(title, toggle),
                    toggleGap: title.left - toggle.right,
                    horizontalOverflow: Math.max(
                        document.documentElement.scrollWidth,
                        document.body.scrollWidth,
                    ) > document.documentElement.clientWidth,
                };
            }"""
        )
        narrow_debug = (
            f"viewport={narrow['viewport']}; titleCard={narrow['titleCard']}; "
            f"compact-media={narrow['compactMediaMatches']}; "
            f"page-clearance={narrow['pageClearance']}; "
            f"global-right={narrow['globalQuickActionsRight']}; "
            f"titleRow={narrow['titleRow']}; title={narrow['title']}; "
            f"theme={narrow['theme']}; toggle={narrow['toggle']}; "
            f"padding-inline={narrow['titleCardPaddingInlineStart']}/"
            f"{narrow['titleCardPaddingInlineEnd']}; "
            f"title-width={narrow['titleWidth']}; "
            f"title-min-width={narrow['titleMinWidth']}; "
            f"title-max-width={narrow['titleMaxWidth']}"
        )
        assert not narrow["titleOverlapsTheme"], narrow_debug
        assert not narrow["titleOverlapsToggle"], narrow_debug
        assert narrow["toggleGap"] >= 12, narrow_debug
        assert not narrow["horizontalOverflow"], narrow_debug
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_title_rail_stays_aligned_with_global_anchors_across_viewports(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep the Agent heading on the shared title rail without a stale status chip."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/agent/edge/chatgpt",
        1_280,
        720,
        touch=False,
        init_script="""
            (() => {
                const originalFetch = window.fetch.bind(window);
                window.fetch = (input, init) => {
                    const requestUrl = typeof input === "string" ? input : input?.url;
                    if (requestUrl && new URL(requestUrl, window.location.href).pathname === "/api/agent/status") {
                        return Promise.reject(new Error("Agent status polling is disabled for this layout test."));
                    }
                    return originalFetch(input, init);
                };
            })();
        """,
    )

    def read_geometry() -> dict[str, object]:
        return page.evaluate(
            """() => {
                const centerY = selector => {
                    const element = document.querySelector(selector);
                    if (!(element instanceof HTMLElement)) return null;
                    const rect = element.getBoundingClientRect();
                    return rect.top + (rect.height / 2);
                };
                const summary = document.querySelector(".agent-summary-card");
                if (!(summary instanceof HTMLElement)) return null;
                const summaryRect = summary.getBoundingClientRect();
                const summaryStyle = getComputedStyle(summary);
                const controlHeight = selector => {
                    const element = document.querySelector(selector);
                    return element instanceof HTMLElement
                        ? element.getBoundingClientRect().height
                        : null;
                };
                return {
                    headingCenterY: centerY("[data-agent-heading]"),
                    toggleCenterY: centerY("#sidebar_toggle"),
                    themeCenterY: centerY("#global_theme_toggle"),
                    summaryHeight: summaryRect.height,
                    summaryOverflow: summaryStyle.overflow,
                    summaryBackgroundColor: summaryStyle.backgroundColor,
                    summaryBorderWidth: summaryStyle.borderWidth,
                    summaryBorderRadius: summaryStyle.borderRadius,
                    summaryBoxShadow: summaryStyle.boxShadow,
                    summaryBackdropFilter: summaryStyle.backdropFilter,
                    chipCount: document.querySelectorAll("#agent_phase_chip").length,
                    readinessCount: document.querySelectorAll(".agent-readiness").length,
                    primaryControlHeights: [
                        controlHeight(".agent-platform-combobox [data-agent-combobox-trigger]"),
                        controlHeight(".agent-session-mode-combobox [data-agent-combobox-trigger]"),
                    ],
                    horizontalOverflow: Math.max(
                        document.documentElement.scrollWidth,
                        document.body.scrollWidth,
                    ) > document.documentElement.clientWidth,
                };
            }"""
        )

    try:
        _wait_for_global_title_rail(page, "[data-agent-heading]")
        desktop = read_geometry()
        assert desktop is not None
        assert abs(desktop["headingCenterY"] - desktop["toggleCenterY"]) <= 1
        assert abs(desktop["headingCenterY"] - desktop["themeCenterY"]) <= 1
        assert desktop["summaryHeight"] < 120
        assert desktop["summaryOverflow"] == "visible"
        assert desktop["summaryBackgroundColor"] == "rgba(0, 0, 0, 0)"
        assert desktop["summaryBorderWidth"] == "0px"
        assert desktop["summaryBorderRadius"] == "0px"
        assert desktop["summaryBoxShadow"] == "none"
        assert desktop["summaryBackdropFilter"] == "none"
        assert desktop["chipCount"] == 0
        assert desktop["readinessCount"] == 0
        assert desktop["primaryControlHeights"] == [36, 36]
        assert not desktop["horizontalOverflow"]

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function(
            "() => window.matchMedia('(max-width: 560px)').matches"
        )
        _wait_for_global_title_rail(page, "[data-agent-heading]")
        narrow = read_geometry()
        assert narrow is not None
        assert abs(narrow["headingCenterY"] - narrow["toggleCenterY"]) <= 1
        assert abs(narrow["headingCenterY"] - narrow["themeCenterY"]) <= 1
        assert narrow["summaryHeight"] < 120
        assert narrow["summaryOverflow"] == "visible"
        assert narrow["summaryBackgroundColor"] == "rgba(0, 0, 0, 0)"
        assert narrow["summaryBorderWidth"] == "0px"
        assert narrow["summaryBorderRadius"] == "0px"
        assert narrow["summaryBoxShadow"] == "none"
        assert narrow["summaryBackdropFilter"] == "none"
        assert narrow["chipCount"] == 0
        assert narrow["readinessCount"] == 0
        assert narrow["primaryControlHeights"] == [36, 36]
        assert not narrow["horizontalOverflow"]
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_browser_title_rail_stays_aligned_with_global_anchors_across_viewports(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep the Cached text browser heading on the shared top anchor rail."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/browser?view=text&source=all&kind=all&q=&sort=newest&session_view=1",
        1_280,
        900,
        touch=False,
    )

    def read_geometry() -> dict[str, object]:
        return page.evaluate(
            """() => {
                const centerY = selector => {
                    const element = document.querySelector(selector);
                    if (!(element instanceof HTMLElement)) return null;
                    const rect = element.getBoundingClientRect();
                    return rect.top + (rect.height / 2);
                };
                const summary = document.querySelector(".browser-summary-card");
                if (!(summary instanceof HTMLElement)) return null;
                const summaryStyle = getComputedStyle(summary);
                return {
                    titleCenterY: centerY(".browser-heading-copy"),
                    sidebarCenterY: centerY("#browser_sidebar .hero h1"),
                    toggleCenterY: centerY("#sidebar_toggle"),
                    themeCenterY: centerY("#global_theme_toggle"),
                    summaryWidth: summary.getBoundingClientRect().width,
                    summaryPaddingTop: summaryStyle.paddingTop,
                    summaryOverflow: summaryStyle.overflow,
                    horizontalOverflow: Math.max(
                        document.documentElement.scrollWidth,
                        document.body.scrollWidth,
                    ) > document.documentElement.clientWidth,
                };
            }"""
        )

    try:
        _wait_for_global_title_rail(page, ".browser-heading-copy")
        desktop = read_geometry()
        assert desktop is not None
        assert abs(desktop["titleCenterY"] - desktop["sidebarCenterY"]) <= 1
        assert abs(desktop["titleCenterY"] - desktop["toggleCenterY"]) <= 1
        assert abs(desktop["titleCenterY"] - desktop["themeCenterY"]) <= 1
        assert abs(desktop["summaryWidth"] - 640) <= 1
        assert desktop["summaryPaddingTop"] == "10px"
        assert desktop["summaryOverflow"] == "visible"
        assert not desktop["horizontalOverflow"]

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function("() => window.matchMedia('(max-width: 560px)').matches")
        _wait_for_global_title_rail(page, ".browser-heading-copy")
        narrow = read_geometry()
        assert narrow is not None
        assert abs(narrow["titleCenterY"] - narrow["toggleCenterY"]) <= 1
        assert abs(narrow["titleCenterY"] - narrow["themeCenterY"]) <= 1
        assert 0 < narrow["summaryWidth"] <= 640
        assert narrow["summaryPaddingTop"] == "12px"
        assert narrow["summaryOverflow"] == "visible"
        assert not narrow["horizontalOverflow"]
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_text_browser_omits_redundant_per_page_metric(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep the text summary focused on totals rather than the current page size."""
    route = f"{sidebar_server_url}/browser?view=text&source=all&kind=all&q=&sort=newest&session_view=1"
    page, context = _open_page(
        disposable_browser,
        route,
        1_018,
        900,
        touch=False,
    )
    try:
        for width, height in ((1_018, 900), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            page.goto(route, wait_until="domcontentloaded")
            labels = page.locator(".browser-text-metric-grid .metric-label")
            expect(labels).to_have_count(3)
            assert labels.all_text_contents() == ["Sessions", "Messages", "Projects"]
            body_text = page.locator("body").inner_text()
            assert "Sessions shown" not in body_text
            assert "Messages shown" not in body_text
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((992, 1_203, False), (390, 844, True)),
)
def test_zhihu_text_browser_shows_answerers_and_complete_answers(
    disposable_browser: Browser,
    seeded_zhihu_browser_server_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    """Render Zhihu as answerers whose detail pages contain complete answers."""

    index_url = (
        f"{seeded_zhihu_browser_server_url}/browser?view=text&source=zhihu"
        "&q=&sort=newest"
    )
    page, context = _open_page(
        disposable_browser,
        index_url,
        width,
        height,
        touch=touch,
    )
    try:
        table = page.locator(".browser-session-index-table")
        expect(table).to_have_count(1)
        assert table.locator("thead th").all_inner_texts() == [
            "No.",
            "Answerer",
            "Answers",
            "Last updated ↓",
        ]
        expect(table.locator(".browser-session-table-source")).to_have_count(0)
        assert table.locator(".browser-session-table-title").all_inner_texts() == [
            "肥肥猫",
            "Other Author",
        ]
        assert table.locator(".browser-session-table-count").all_inner_texts() == ["2", "1"]
        expect(table.locator(".browser-session-table-id")).to_have_count(0)
        expect(page.locator(".browser-clear-link")).to_have_count(0)
        assert page.locator(".browser-text-metric-grid .metric-label").all_inner_texts() == [
            "Answerers",
            "Answers",
        ]
        assert page.locator(".browser-text-metric-grid strong").all_inner_texts() == ["2", "3"]
        expect(page.get_by_role("heading", name="Zhihu answerers", exact=True)).to_be_visible()
        expect(page.locator('[name="session_view"]')).to_have_value("1")
        expect(page.locator('[name="sort"]')).to_have_value("newest")
        answerer_trigger = page.get_by_role(
            "button",
            name="Filter by answerer: All answerers",
        )
        if not answerer_trigger.is_visible():
            page.locator("#sidebar_toggle").click()
            expect(answerer_trigger).to_be_visible()
        expect(answerer_trigger).to_have_count(1)
        assert answerer_trigger.evaluate(
            """element => ({
                height: element.getBoundingClientRect().height,
                radius: getComputedStyle(element).borderRadius,
                overflow: element.scrollWidth - element.clientWidth,
            })"""
        ) == {"height": 30, "radius": "999px", "overflow": 0}
        answerer_trigger.click()
        answerer_options = page.get_by_role("option")
        expect(answerer_options).to_have_count(3)
        assert answerer_options.all_inner_texts() == [
            "All answerers",
            "Other Author",
            "肥肥猫",
        ]
        page.get_by_role("option", name="肥肥猫", exact=True).click()
        page.wait_for_url(
            re.compile(r"[?&]answerer=%E8%82%A5%E8%82%A5%E7%8C%AB(?:&|$)")
        )
        expect(table.locator("tbody tr")).to_have_count(1)
        expect(table.locator(".browser-session-table-title")).to_have_text("肥肥猫")
        expect(table.locator(".browser-session-table-count")).to_have_text("2")
        assert table.evaluate("element => element.scrollWidth - element.clientWidth") == 0
        assert page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        ) == 0
        if width <= 900 and page.locator("#sidebar_toggle").get_attribute(
            "aria-expanded"
        ) == "true":
            page.locator("#sidebar_toggle").click()

        table.locator(".browser-session-table-title").click()
        assert page.locator(
            ".browser-session-detail-table thead th"
        ).all_inner_texts() == [
            "No.",
            "Time",
            "Question",
            "Answer",
        ]
        assert page.locator(".browser-text-metric-grid .metric-label").all_inner_texts() == [
            "Answerer",
            "Answers",
        ]
        assert page.locator(".browser-text-metric-grid strong").all_inner_texts() == [
            "肥肥猫",
            "2",
        ]
        expect(page.locator('[data-browser-session-tag]')).to_have_text("肥肥猫 ×")
        expect(page.get_by_role("link", name="Back to all answerers", exact=True)).to_be_visible()
        assert page.locator(".browser-session-question-link").all_inner_texts() == [
            "Fixture Zhihu question",
            "Earlier fixture question",
        ]
        message = page.locator("[data-browser-session-message-source]")
        expect(message.first).to_contain_text("Full answer final sentence.")
        expect(message.nth(1)).to_contain_text("Earlier complete cached answer.")
        layout = message.first.evaluate(
            """element => {
                const scroller = document.querySelector('.browser-content-card');
                scroller.scrollTop = scroller.scrollHeight;
                return {
                    maxHeight: getComputedStyle(element).maxHeight,
                    clientHeight: element.clientHeight,
                    scrollHeight: element.scrollHeight,
                    atBottom: scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop <= 1,
                    toggleCount: document.querySelectorAll('[data-browser-session-message-toggle]').length,
                    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                };
            }"""
        )
        assert layout == {
            "maxHeight": "none",
            "clientHeight": layout["scrollHeight"],
            "scrollHeight": layout["scrollHeight"],
            "atBottom": True,
            "toggleCount": 0,
            "overflow": 0,
        }
        page.locator("[data-browser-session-scope-remove]").click()
        page.wait_for_function(
            "!new URLSearchParams(window.location.search).has('session')"
        )
        search_scope = page.evaluate(
            """() => {
                const params = new URLSearchParams(window.location.search);
                return {
                    source: params.get('source'),
                    sessionView: params.get('session_view'),
                    session: params.get('session'),
                    answerer: params.get('answerer'),
                };
            }"""
        )
        assert search_scope == {
            "source": "zhihu",
            "sessionView": "1",
            "session": None,
            "answerer": "",
        }
        expect(page.get_by_role("heading", name="Zhihu answerers", exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_browser_filter_actions_stack_standard_buttons_across_viewports(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep the browser filter actions on separate rows at desktop and narrow widths."""
    route = f"{sidebar_server_url}/browser?view=text&session_view=1&source=all&sort=newest&q="
    page, context = _open_page(
        disposable_browser,
        route,
        1_280,
        900,
        touch=False,
    )
    try:
        for width, height in ((1_280, 900), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            page.goto(route, wait_until="domcontentloaded")
            actions = page.locator(".browser-filter-actions > :is(a, button)")
            expect(actions).to_have_count(2)
            expect(page.locator(".browser-chatgpt-media-link")).to_have_count(0)
            geometry = actions.evaluate_all(
                "elements => {"
                "  const containerRect = elements[0].parentElement.getBoundingClientRect();"
                "  return {"
                "    containerRight: containerRect.right,"
                "    buttons: elements.map(element => {"
                "      const rect = element.getBoundingClientRect();"
                "      return {top: rect.top, height: rect.height, right: rect.right};"
                "    }),"
                "  };"
                "}"
            )
            assert geometry["buttons"][1]["top"] >= geometry["buttons"][0]["top"] + geometry["buttons"][0]["height"] - 1, (
                width,
                geometry,
            )
            for button in geometry["buttons"]:
                assert abs(button["right"] - geometry["containerRight"]) <= 1, (
                    width,
                    geometry,
                )
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_browser_message_timestamps_keep_two_rows_across_viewports(
    disposable_browser: Browser,
    seeded_chatgpt_browser_server_url: str,
) -> None:
    """Keep the date and clock on separate rendered rows at every supported width."""
    page, context = _open_page(
        disposable_browser,
        f"{seeded_chatgpt_browser_server_url}/browser?view=text&source=chatgpt&sort=newest&session_view=1",
        1_280,
        900,
        touch=False,
    )
    try:
        session = page.locator(".browser-session-index-table a").first
        expect(session).to_be_visible()
        detail_url = session.get_attribute("href")
        assert detail_url

        for width, height in ((1_280, 900), (715, 899), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            page.goto(
                f"{seeded_chatgpt_browser_server_url}{detail_url}",
                wait_until="domcontentloaded",
            )
            timestamp = page.locator(
                ".browser-session-detail-table time.browser-session-message-time"
            )
            expect(timestamp).to_have_count(1)
            expect(timestamp).to_be_visible()
            geometry = timestamp.evaluate(
                "element => {"
                "  const style = getComputedStyle(element);"
                "  return {"
                "    display: style.display,"
                "    whiteSpace: style.whiteSpace,"
                "    lines: [...element.children].map(child => {"
                "      const rect = child.getBoundingClientRect();"
                "      return {text: child.textContent.trim(), top: rect.top};"
                "    }),"
                "  };"
                "}"
            )
            assert geometry["display"] == "inline-grid", (width, geometry)
            assert geometry["whiteSpace"] == "normal", (width, geometry)
            assert [line["text"] for line in geometry["lines"]] == [
                "12/08/2026",
                "13:00:00 (HKT)",
            ], (width, geometry)
            assert geometry["lines"][1]["top"] > geometry["lines"][0]["top"], (
                width,
                geometry,
            )
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("route", "target_selector", "title_selector"),
    (
        (
            "/cache/chatgpt",
            ".cache-workspace-content",
            ".cache-overview-title-card > .report-heading-row",
        ),
        (
            "/browser",
            ".browser-content-card",
            ".browser-summary-card > .report-heading-row",
        ),
        (
            "/settings/style-tokens",
            ".style-token-shell",
            ".settings-summary-card > .report-heading-row",
        ),
        (
            "/agent",
            ".agent-workspace-grid",
            ".agent-summary-card > .report-heading-row",
        ),
        (
            "/settings",
            ".settings-category-shell",
            "#settings_workspace .workspace-summary-card > .report-heading-row",
        ),
    ),
)
def test_sidebar_gel_motion_is_content_only_across_product_surfaces(
    disposable_browser: Browser,
    sidebar_server_url: str,
    route: str,
    target_selector: str,
    title_selector: str,
) -> None:
    """Keep shared soft-body motion expressive without animating title geometry."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}{route}",
        1_024,
        900,
        touch=False,
        reduced_motion="no-preference",
    )
    try:
        target = page.locator(target_selector)
        title = page.locator(title_selector)
        expect(target).to_be_visible()
        expect(title).to_be_visible()
        expect(page.locator("#sidebar_toggle")).to_have_attribute("aria-expanded", "true")
        expect(target).to_have_attribute("data-sidebar-gel-content", "")
        page.wait_for_load_state("networkidle")
        page.evaluate("() => document.fonts.ready")

        expanded_baseline = target.evaluate(
            "element => { const rect = element.getBoundingClientRect(); "
            "return {left: rect.left, top: rect.top, width: rect.width}; }"
        )

        def sample_motion() -> dict[str, object]:
            return page.evaluate(
                """async ([targetSelector, titleSelector]) => {
                    const toggle = document.querySelector("#sidebar_toggle");
                    const shell = document.querySelector(".app-shell");
                    const content = document.querySelector(targetSelector);
                    const title = document.querySelector(titleSelector);
                    if (!(toggle instanceof HTMLElement)
                        || !(shell instanceof HTMLElement)
                        || !(content instanceof HTMLElement)
                        || !(title instanceof HTMLElement)) return null;

                    const frames = [];
                    const startedAt = performance.now();
                    toggle.click();
                    await new Promise(resolve => {
                        const sample = () => {
                            const transform = getComputedStyle(content).transform;
                            const matrix = transform === "none"
                                ? new DOMMatrixReadOnly()
                                : new DOMMatrixReadOnly(transform);
                            const contentRect = content.getBoundingClientRect();
                            const titleRect = title.getBoundingClientRect();
                            const toggleRect = toggle.getBoundingClientRect();
                            frames.push({
                                animationNames: content.getAnimations()
                                    .map(animation => animation.animationName || ""),
                                className: shell.className,
                                contentGap: contentRect.top - titleRect.bottom,
                                documentOverflow: Math.max(
                                    document.documentElement.scrollWidth,
                                    document.body.scrollWidth,
                                ) - document.documentElement.clientWidth,
                                offsetWidth: content.offsetWidth,
                                scaleX: matrix.a,
                                scaleY: matrix.d,
                                titleToggleGap: titleRect.left - toggleRect.right,
                                translateX: matrix.e,
                            });
                            if (performance.now() - startedAt >= 760) {
                                resolve();
                                return;
                            }
                            requestAnimationFrame(sample);
                        };
                        requestAnimationFrame(sample);
                    });
                    const finalRect = content.getBoundingClientRect();
                    return {
                        finalAnimationNames: content.getAnimations()
                            .map(animation => animation.animationName || ""),
                        finalClassName: shell.className,
                        finalRect: {
                            left: finalRect.left,
                            top: finalRect.top,
                            width: finalRect.width,
                        },
                        finalTransform: getComputedStyle(content).transform,
                        frames,
                    };
                }""",
                [target_selector, title_selector],
            )

        closing = sample_motion()
        assert closing is not None
        assert any(
            "is-sidebar-closing" in frame["className"]
            for frame in closing["frames"]
        )
        assert any(
            "workspace-sidebar-gel-close" in frame["animationNames"]
            for frame in closing["frames"]
        )
        assert max(abs(frame["translateX"]) for frame in closing["frames"]) > 8
        assert max(abs(frame["scaleX"] - 1) for frame in closing["frames"]) > 0.01
        assert max(abs(frame["scaleY"] - 1) for frame in closing["frames"]) > 0.01
        assert max(frame["documentOverflow"] for frame in closing["frames"]) <= 1
        assert min(frame["contentGap"] for frame in closing["frames"]) >= 0
        assert min(frame["titleToggleGap"] for frame in closing["frames"]) >= 11.5
        assert (
            max(frame["offsetWidth"] for frame in closing["frames"])
            - min(frame["offsetWidth"] for frame in closing["frames"])
        ) <= 1
        assert "is-sidebar-animating" not in closing["finalClassName"]
        assert closing["finalTransform"] == "none"
        assert not any(
            str(name).startswith("workspace-sidebar-gel-")
            for name in closing["finalAnimationNames"]
        )

        opening = sample_motion()
        assert opening is not None
        assert any(
            "workspace-sidebar-gel-open" in frame["animationNames"]
            for frame in opening["frames"]
        )
        assert max(frame["documentOverflow"] for frame in opening["frames"]) <= 1
        assert min(frame["contentGap"] for frame in opening["frames"]) >= 0
        assert min(frame["titleToggleGap"] for frame in opening["frames"]) >= 11.5
        assert "is-sidebar-animating" not in opening["finalClassName"]
        assert opening["finalTransform"] == "none"
        for key in ("left", "top", "width"):
            assert abs(opening["finalRect"][key] - expanded_baseline[key]) <= 1
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "height", "touch", "reduced_motion"),
    (
        (1_024, 900, False, "reduce"),
        (390, 844, True, "no-preference"),
    ),
)
def test_sidebar_gel_motion_respects_reduced_motion_and_overlay_gates(
    disposable_browser: Browser,
    sidebar_server_url: str,
    width: int,
    height: int,
    touch: bool,
    reduced_motion: str,
) -> None:
    """Prevent even a transient soft-body class outside the desktop motion contract."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/settings/style-tokens",
        width,
        height,
        touch=touch,
        reduced_motion=reduced_motion,
    )
    try:
        state = page.locator("#sidebar_toggle").evaluate(
            """toggle => {
                const shell = document.querySelector(".app-shell");
                const content = document.querySelector(".style-token-shell");
                toggle.click();
                return {
                    animationNames: content?.getAnimations()
                        .map(animation => animation.animationName || "") || [],
                    className: shell?.className || "",
                };
            }"""
        )
        assert "is-sidebar-animating" not in state["className"]
        assert not any(
            str(name).startswith("workspace-sidebar-gel-")
            for name in state["animationNames"]
        )
        assert page.evaluate(
            "Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) "
            "- document.documentElement.clientWidth"
        ) <= 1
    finally:
        context.close()


def test_style_tokens_component_catalog_is_interactive_and_responsive(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Exercise the shared component lab at desktop and narrow widths."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/settings/style-tokens",
        1_280,
        720,
        touch=False,
    )
    try:
        cards = page.locator("[data-style-token-card]")
        expect(cards).to_have_count(21)
        assert page.evaluate(
            "document.documentElement.scrollWidth === document.documentElement.clientWidth"
        )
        assert len(
            page.locator("[data-style-token-card]").first.evaluate(
                "element => getComputedStyle(element).gridTemplateColumns.split(' ')"
            )
        ) == 2
        assert page.locator("[data-style-token-agent-browser-menu]").is_hidden()

        resizer = page.locator("[data-style-token-resizer]")
        demo = page.locator(".style-token-demo").first
        resizer_box = resizer.bounding_box()
        initial_demo_width = demo.bounding_box()["width"]
        page.mouse.move(
            resizer_box["x"] + (resizer_box["width"] / 2),
            resizer_box["y"] + (resizer_box["height"] / 2),
        )
        page.mouse.down()
        page.mouse.move(resizer_box["x"] + 40, resizer_box["y"] + (resizer_box["height"] / 2))
        page.mouse.up()
        assert demo.bounding_box()["width"] > initial_demo_width

        refresh_button = page.locator("[data-style-token-secondary-button]")
        refresh_geometry = refresh_button.evaluate(
            "element => ({ width: element.getBoundingClientRect().width, previewWidth: element.parentElement.getBoundingClientRect().width, demoWidth: element.closest('[data-style-token-demo]').getBoundingClientRect().width })"
        )
        assert refresh_geometry["width"] <= refresh_geometry["previewWidth"] + 1
        assert refresh_geometry["previewWidth"] < refresh_geometry["demoWidth"]
        assert refresh_button.get_attribute("data-style-token-secondary-button-use-icon") == "false"
        assert refresh_button.evaluate(
            "element => element.getBoundingClientRect().height"
        ) == 32

        tag_typography = page.locator("[data-style-token-prompt-tag]").evaluate(
            "element => { const style = getComputedStyle(element); return { fontSize: style.fontSize, fontWeight: style.fontWeight }; }"
        )
        assert tag_typography == {"fontSize": "12px", "fontWeight": "500"}

        metric_label = page.locator(
            '[data-style-token-card="workspace-metric-value"] .metric-label'
        )
        expect(metric_label).to_have_text("Total trades")
        assert metric_label.evaluate(
            "element => { const style = getComputedStyle(element); return { fontSize: style.fontSize, fontWeight: style.fontWeight, lineHeight: style.lineHeight, color: style.color }; }"
        ) == {
            "fontSize": "15px",
            "fontWeight": "400",
            "lineHeight": "normal",
            "color": "rgb(11, 12, 12)",
        }

        sort_label_weight = page.locator(
            '#shared-select-filter [data-style-token-shared-filter-label]'
        ).evaluate("element => getComputedStyle(element).fontWeight")
        assert sort_label_weight == "400"

        agent_trigger = page.locator("[data-style-token-agent-browser-trigger]")
        assert agent_trigger.evaluate(
            "element => element.getBoundingClientRect().height"
        ) == 36
        selected_agent_option_radius = page.locator(
            '[data-style-token-agent-browser-option="edge"]'
        ).evaluate("element => getComputedStyle(element).borderRadius")
        assert selected_agent_option_radius == "999px"

        period_trigger = page.locator(
            "#shared-select-dropdown [data-style-token-shared-filter-trigger]"
        )
        period_trigger.focus()
        period_trigger.press("ArrowDown")
        period_option = page.locator(
            '#shared-select-dropdown [data-style-token-shared-filter-option="1y"]'
        )
        assert period_option.evaluate(
            "element => element.getBoundingClientRect().height"
        ) == 36
        assert period_option.locator(".trade-strategy-dropdown-text").inner_text() == "1 year"
        assert period_option.locator(".trade-strategy-dropdown-text").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )
        page.keyboard.press("End")
        page.keyboard.press("Enter")
        expect(page.locator("#shared-select-dropdown select")).to_have_value("max")

        tune_button = page.locator("[data-style-token-strategy-tune-button]")
        tune_panel = page.locator("[data-style-token-strategy-tuning-panel]")
        expect(tune_button).to_have_attribute("aria-pressed", "true")
        expect(tune_button).to_have_attribute("aria-expanded", "true")
        expect(tune_panel).to_be_visible()
        assert tune_button.evaluate(
            "element => getComputedStyle(element).width"
        ) == "30px"
        assert tune_button.locator(".icon").evaluate(
            "element => getComputedStyle(element).width"
        ) == "14px"
        assert tune_panel.evaluate(
            "element => getComputedStyle(element).padding"
        ) == "10px"
        page.locator(".style-token-strategy-tuning-label").click()
        expect(tune_panel).to_be_visible()
        tune_button.click()
        expect(tune_button).to_have_attribute("aria-pressed", "false")
        expect(tune_button).to_have_attribute("aria-expanded", "false")
        expect(tune_panel).to_be_hidden()
        tune_button.click()
        expect(tune_panel).to_be_visible()

        agent_trigger.press("ArrowDown")
        page.keyboard.press("End")
        page.keyboard.press("Enter")
        expect(page.locator("[data-style-token-agent-browser-input]")).to_have_value(
            "chrome"
        )

        action_right = page.locator("#global_theme_toggle").evaluate(
            "element => element.getBoundingClientRect().right"
        )
        copy_rights = page.locator(".style-token-copy-button").evaluate_all(
            "elements => elements.map(element => element.getBoundingClientRect().right)"
        )
        assert all(abs(right - action_right) <= 1 for right in copy_rights)

        active_icon = page.locator(
            ".settings-category-nav-item.is-active .settings-category-nav-icon"
        )
        assert active_icon.evaluate(
            "element => getComputedStyle(element).backgroundColor"
        ) == "rgb(0, 85, 204)"
        icon_shell = page.locator(
            ".settings-category-nav-item.is-active .settings-category-nav-icon-shell"
        )
        assert icon_shell.evaluate(
            "element => getComputedStyle(element).backgroundColor"
        ) == "rgba(0, 0, 0, 0)"
        sidebar_style = page.locator("#app_sidebar").evaluate(
            "element => { const style = getComputedStyle(element); return { backgroundColor: style.backgroundColor, paddingTop: style.paddingTop }; }"
        )
        assert sidebar_style["paddingTop"] == "10px"
        assert "0.62" in sidebar_style["backgroundColor"]

        beta_icon = page.locator('[data-dock-section="beta"] .dock-icon')
        if beta_icon.count():
            assert "sparkles.2.svg" in beta_icon.evaluate(
                "element => getComputedStyle(element).maskImage"
            )

        prompt_tag = page.locator("[data-style-token-prompt-tag]")
        page.locator("[data-style-token-prompt-tag-remove]").click()
        expect(prompt_tag).to_have_class(re.compile(r"style-token-dismissible-hidden"))
        expect(prompt_tag).not_to_have_class(
            re.compile(r"style-token-dismissible-hidden"),
            timeout=2_000,
        )

        page.locator("[data-style-token-text-input-clear]").click()
        expect(page.locator("[data-style-token-text-input]")).to_have_value("")

        action_package = page.locator("[data-style-token-action-package]")
        action_package_style = action_package.evaluate(
            "element => { const style = getComputedStyle(element); return { borderRadius: style.borderRadius, boxShadow: style.boxShadow, backdropFilter: style.backdropFilter }; }"
        )
        assert action_package_style["borderRadius"] == "10px"
        assert action_package_style["boxShadow"] != "none"
        assert "blur" in action_package_style["backdropFilter"]

        live_control = page.locator("[data-style-token-action-package-live]")
        live_marker = page.locator("[data-action-package-live-marker]")
        expect(live_marker).to_be_hidden()
        live_control.check()
        expect(live_marker).to_be_visible()
        live_control.uncheck()
        expect(live_marker).to_be_hidden()

        execution_option = page.locator(
            "#settings-execution-option .settings-general-option"
        )
        execution_option_style = execution_option.evaluate(
            "element => { const style = getComputedStyle(element); return { display: style.display, gridTemplateColumns: style.gridTemplateColumns, gap: style.gap, padding: style.padding, borderRadius: style.borderRadius, transition: style.transition }; }"
        )
        assert execution_option_style["display"] == "grid"
        grid_columns = execution_option_style["gridTemplateColumns"].split()
        assert len(grid_columns) == 2
        assert all(column.endswith("px") for column in grid_columns)
        assert float(grid_columns[0][:-2]) < float(grid_columns[1][:-2])
        assert execution_option_style["gap"] == "12px"
        assert execution_option_style["padding"] == "14px 16px"
        assert execution_option_style["borderRadius"] == "10px"
        assert "background-color" in execution_option_style["transition"]
        assert page.locator(
            "#settings-execution-option .settings-general-option-title"
        ).inner_text() == "Update existing cache entries"
        assert page.locator(
            "#settings-execution-option .settings-general-option-desc"
        ).inner_text() == (
            "When enabled, refresh existing metadata as well as newly discovered items."
        )
        assert page.locator("#global-theme-toggle [data-style-token-theme-toggle-label]").count() == 0
        assert page.locator("#pagination .style-token-component-kicker").count() == 0
        assert page.locator("#scrollable-data-table .style-token-component-kicker").count() == 0
        assert page.locator("#settings-execution-option legend").count() == 0
        assert page.locator("#tooltip .chart-tooltip-title").evaluate(
            "element => getComputedStyle(element).fontWeight"
        ) == "500"

        action_button = page.locator("[data-style-token-action-button]")
        action_button.click()
        expect(action_button).to_be_disabled()
        expect(action_button).to_be_enabled(timeout=2_000)

        table_filter = page.locator("[data-style-token-table-filter-trigger]")
        table_filter.click()
        page.locator('[data-style-token-table-filter-option="buy"]').click()
        expect(page.locator("[data-style-token-table-filter-summary]")).to_have_text(
            "5 filtered of 12 total"
        )
        expect(page.locator("[data-style-token-table-pagination]")).to_be_hidden()

        token_control = page.locator(
            '[data-style-token-name="--settings-round-icon-button-size"]'
        ).first
        expect(token_control).to_have_attribute("data-style-token-value", "36")
        token_control.locator('[data-style-token-stepper="up"]').click()
        expect(token_control).to_have_attribute("data-style-token-value", "37")
        assert page.locator("[data-style-token-shell]").evaluate(
            "element => element.style.getPropertyValue('--settings-round-icon-button-size')"
        ) == "37px"
    finally:
        context.close()

    narrow_page, narrow_context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/settings/style-tokens",
        390,
        844,
        touch=True,
    )
    try:
        assert narrow_page.evaluate(
            "document.documentElement.scrollWidth === document.documentElement.clientWidth"
        )
        assert narrow_page.locator("[data-style-token-card]").first.evaluate(
            "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
        ) == 1
        expect(narrow_page.locator("[data-style-token-resizer]")).to_be_hidden()
        narrow_tuning = narrow_page.locator("[data-style-token-strategy-tuning]")
        expect(narrow_tuning.locator("[data-style-token-strategy-tuning-panel]")).to_be_visible()
        assert narrow_tuning.evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )
        narrow_metric_label = narrow_page.locator(
            '[data-style-token-card="workspace-metric-value"] .metric-label'
        )
        assert narrow_metric_label.evaluate(
            "element => { const style = getComputedStyle(element); return { fontSize: style.fontSize, fontWeight: style.fontWeight, lineHeight: style.lineHeight, color: style.color }; }"
        ) == {
            "fontSize": "15px",
            "fontWeight": "400",
            "lineHeight": "normal",
            "color": "rgb(11, 12, 12)",
        }
        title_left = narrow_page.locator(
            ".settings-summary-card .report-heading"
        ).bounding_box()["x"]
        toggle_box = narrow_page.locator("#sidebar_toggle").bounding_box()
        assert title_left >= toggle_box["x"] + toggle_box["width"]
    finally:
        narrow_context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_shared_segmented_controls_shrink_wrap_and_center(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep every Cache reuse of the shared blue pill compact and centered."""
    cases = (
        (
            "/settings/style-tokens",
            "#segmented-control .range-mode-shell",
            ".style-token-demo",
        ),
        (
            "/browser?view=text&session_view=1&q=&source=chatgpt&sort=newest",
            ".browser-content-mode-control",
            "#browser_filter_form",
        ),
        (
            "/cache/chatgpt",
            "[data-cache-content-mode]",
            ".cache-page-content-mode-section",
        ),
    )
    for width, height, touch in ((1_280, 900, False), (390, 844, True)):
        page, context = _open_page(
            disposable_browser,
            f"{sidebar_server_url}{cases[0][0]}",
            width,
            height,
            touch=touch,
        )
        try:
            for route, control_selector, owner_selector in cases:
                page.goto(f"{sidebar_server_url}{route}", wait_until="domcontentloaded")
                geometry = page.evaluate(
                    """({controlSelector, ownerSelector}) => {
                        const control = document.querySelector(controlSelector);
                        const owner = control?.closest(ownerSelector);
                        if (!(control instanceof HTMLElement) || !(owner instanceof HTMLElement)) {
                            return null;
                        }
                        const controlRect = control.getBoundingClientRect();
                        const ownerRect = owner.getBoundingClientRect();
                        const optionWidths = Array.from(
                            control.querySelectorAll('.segmented-control-option, .range-mode-option'),
                        ).map(option => option.getBoundingClientRect().width);
                        return {
                            centerDelta: Math.abs(
                                (controlRect.left + (controlRect.width / 2))
                                - (ownerRect.left + (ownerRect.width / 2)),
                            ),
                            compact: controlRect.width < ownerRect.width - 1,
                            horizontalOverflow: document.documentElement.scrollWidth
                                - document.documentElement.clientWidth,
                            optionWidths,
                        };
                    }""",
                    {"controlSelector": control_selector, "ownerSelector": owner_selector},
                )
                assert geometry is not None, (width, route)
                assert geometry["compact"], (width, route, geometry)
                assert geometry["centerDelta"] <= 1, (width, route, geometry)
                assert len(geometry["optionWidths"]) > 1, (width, route, geometry)
                assert (
                    max(geometry["optionWidths"]) - min(geometry["optionWidths"])
                ) <= 1, (width, route, geometry)
                assert geometry["horizontalOverflow"] <= 1, (width, route, geometry)
        finally:
            context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("page_source", ("chatgpt", "claude", "gemini", "grok", "x", "zhihu"))
def test_cache_source_switcher_click_matrix_stays_within_expected_destinations(
    disposable_browser: Browser,
    sidebar_server_url: str,
    page_source: str,
) -> None:
    """Verify every source option lands on its intentional local destination."""
    expected_paths = {
        "chatgpt": {
            "chatgpt": "/cache/chatgpt",
            "claude": "/cache/claude",
            "gemini": "/cache/gemini",
            "grok": "/cache/grok",
            "x": "/cache/x",
            "zhihu": "/cache/zhihu",
        },
        "gemini": {
            "chatgpt": "/cache/chatgpt",
            "claude": "/cache/claude",
            "gemini": "/cache/gemini",
            "grok": "/cache/grok",
            "x": "/cache/x",
            "zhihu": "/cache/zhihu",
        },
        "grok": {
            "chatgpt": "/cache/chatgpt",
            "claude": "/cache/claude",
            "gemini": "/cache/gemini",
            "grok": "/cache/grok",
            "x": "/cache/x",
            "zhihu": "/cache/zhihu",
        },
        "x": {
            "chatgpt": "/cache/chatgpt",
            "claude": "/cache/claude",
            "gemini": "/cache/gemini",
            "grok": "/cache/grok",
            "x": "/cache/x",
            "zhihu": "/cache/zhihu",
        },
        "claude": {
            "chatgpt": "/cache/chatgpt",
            "claude": "/cache/claude",
            "gemini": "/cache/gemini",
            "grok": "/cache/grok",
            "x": "/cache/x",
            "zhihu": "/cache/zhihu",
        },
        "zhihu": {
            "chatgpt": "/cache/chatgpt",
            "claude": "/cache/claude",
            "gemini": "/cache/gemini",
            "grok": "/cache/grok",
            "x": "/cache/x",
            "zhihu": "/cache/zhihu",
        },
    }[page_source]
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/{page_source}",
        1_280,
        900,
        touch=False,
    )
    try:
        for target_source, expected_path in expected_paths.items():
            page.goto(f"{sidebar_server_url}/cache/{page_source}", wait_until="domcontentloaded")
            if page_source != "x":
                page.locator('[data-cache-content-mode-option="text"]').click()
                page.goto(f"{sidebar_server_url}/cache/{page_source}", wait_until="domcontentloaded")
                expect(page.locator('[data-cache-content-mode-option="text"]')).to_have_attribute(
                    "aria-checked",
                    "true",
                )
            if target_source == "x" and page_source != "x":
                page.locator('[data-cache-content-mode-option="media"]').click()
                assert page.locator('[data-cache-source-switcher-option="x"]').evaluate(
                    "element => !element.hidden"
                )
            page.locator("[data-cache-source-switcher-trigger]").click()
            page.locator(
                f'[data-cache-source-switcher-option="{target_source}"]'
            ).click()
            expect(page).to_have_url(re.compile(rf"{re.escape(expected_path)}$"))
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("page_source", ("chatgpt", "claude", "gemini", "grok", "x", "zhihu"))
def test_cache_dock_click_preserves_the_current_cache_source(
    disposable_browser: Browser,
    sidebar_server_url: str,
    page_source: str,
) -> None:
    """Verify the second Dock item never falls back to another Cache source."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/{page_source}",
        1_280,
        900,
        touch=False,
    )
    try:
        page.get_by_role("link", name="Cache", exact=True).click()
        expect(page).to_have_url(re.compile(rf"/cache/{page_source}$"))
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_cache_sidebars_reuse_the_chatgpt_base_contract(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Verify all provider sidebars reuse ChatGPT's shared control structure."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/chatgpt",
        1_280,
        900,
        touch=False,
    )
    try:
        cache_provider_labels = {
            "chatgpt": "ChatGPT",
            "claude": "Claude",
            "gemini": "Gemini",
            "grok": "Grok",
            "zhihu": "Zhihu",
        }
        for page_source in ("chatgpt", "claude", "gemini", "grok", "zhihu"):
            if page_source != "chatgpt":
                page.goto(f"{sidebar_server_url}/cache/{page_source}", wait_until="domcontentloaded")

            aside = page.locator("xpath=/html/body/main/div/aside")
            expect(aside).to_have_count(1)
            expect(aside.locator(":scope > .hero")).to_have_count(1)
            expect(aside.locator(":scope > .cache-page-content-mode-section")).to_have_count(1)
            expect(aside.locator("[data-cache-source-switcher]")).to_have_count(1)
            expect(aside.locator("[data-cache-source-switcher-option]")).to_have_count(6)
            expect(aside.locator("[data-browser-session-panel]")).to_have_count(1)
            expect(aside.locator(".browser-session-panel-label")).to_have_text("Authorized browser")
            expect(aside.locator(".cache-settings-link")).to_have_count(1)
            expected_settings_label = (
                f"Open {cache_provider_labels[page_source]} settings"
                if page_source in cache_provider_labels
                else "Open shared cache settings"
            )
            expect(aside.locator(".cache-settings-link")).to_have_text(expected_settings_label)
            expect(aside.locator("[data-cache-action-row]")).to_have_count(1)
            expect(aside.locator("#start_button")).to_have_count(1)
            expect(aside.locator("#stop_button")).to_have_count(1)
            if page_source == "gemini":
                for field_name in (
                    "gemini_max_conversations",
                    "gemini_scroll_pause_seconds",
                    "gemini_stale_round_limit",
                ):
                    expect(aside.locator(f"#{field_name}")).to_have_count(0)
                expect(aside.locator('#start_form_gemini input[name="gemini_browser"]')).to_have_count(1)
                expect(aside.locator('#start_form_gemini input[name="cache_content_mode"]')).to_have_value("text")
                expect(aside.locator('#start_form_gemini input:not([type="hidden"])')).to_have_count(0)
                expect(aside.locator(".cache-settings-link")).to_have_attribute(
                    "href",
                    "/settings#settings-llm",
                )
            if page_source == "grok":
                expect(aside.locator(".cache-secondary-action")).to_have_count(0)
            if page_source == "zhihu":
                author_input = aside.locator('[name="zhihu_author_url"]')
                expect(author_input).to_have_count(1)
                expect(author_input).to_have_class(re.compile(r"\bshared-select-text-input\b"))
                assert author_input.evaluate(
                    """element => ({
                        height: element.getBoundingClientRect().height,
                        radius: getComputedStyle(element).borderRadius,
                        overflow: element.scrollWidth - element.clientWidth,
                    })"""
                ) == {"height": 30, "radius": "999px", "overflow": 0}
                expect(aside.locator("#zhihu_author_url_help")).to_have_count(0)
                expect(aside.locator(".cache-settings-link")).to_have_attribute(
                    "href",
                    "/settings#settings-downloads",
                )
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_280, 900, False), (390, 844, True)),
)
def test_agent_response_pagination_is_immersed_but_keeps_interactive_effects(
    disposable_browser: Browser,
    sidebar_server_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    """Verify one immersed glass pagination surface without clipping its interactions."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/agent",
        width,
        height,
        touch=touch,
        init_script="""
            (() => {
                const originalFetch = window.fetch.bind(window);
                window.fetch = (input, init) => {
                    const requestUrl = typeof input === "string" ? input : input?.url;
                    if (requestUrl) {
                        const pathname = new URL(requestUrl, window.location.href).pathname;
                        if (pathname === "/api/agent/status" || pathname === "/api/browser-session") {
                            return Promise.reject(new Error("Live status polling is disabled for this layout test."));
                        }
                    }
                    return originalFetch(input, init);
                };
            })();
        """,
    )
    try:
        contract = page.evaluate(
            """() => {
                const pagination = document.querySelector("#agent_response_pagination");
                const output = document.querySelector("#agent_response_output");
                const answer = document.querySelector("#agent_response_answer");
                const card = output?.closest(".agent-response-card");
                const task = card?.closest(".agent-task-card");
                const composer = task?.querySelector(".agent-prompt-form");
                const answerContent = answer?.querySelector("[data-agent-response-answer-content]");
                if (!pagination || !output || !answer || !card || !task || !composer || !answerContent) return null;

                output.hidden = false;
                pagination.hidden = false;
                pagination.replaceChildren();
                const indicator = document.createElement("span");
                indicator.className = "local-store-pagination-indicator";
                indicator.setAttribute("aria-hidden", "true");
                pagination.append(indicator);
                const ellipsis = document.createElement("span");
                ellipsis.className = "local-store-page-ellipsis";
                ellipsis.setAttribute("aria-hidden", "true");
                const dots = document.createElement("span");
                dots.className = "local-store-page-ellipsis-dots";
                ellipsis.append(dots);
                pagination.append(ellipsis);
                for (let page = 1; page <= 5; page += 1) {
                    const button = document.createElement("button");
                    button.className = `local-store-page-button${page === 1 ? " is-active" : ""}`;
                    button.textContent = String(page);
                    pagination.append(button);
                }
                pagination.classList.add("is-animated");

                const readPosition = () => {
                    const paginationRect = pagination.getBoundingClientRect();
                    const composerRect = composer.getBoundingClientRect();
                    return {
                        paginationBottom: paginationRect.bottom,
                        composerTop: composerRect.top,
                        composerGap: composerRect.top - paginationRect.bottom,
                    };
                };
                answerContent.textContent = "Short answer";
                const shortPosition = readPosition();
                answerContent.textContent = Array.from(
                    {length: 160},
                    (_, index) => `Response line ${index + 1}`,
                ).join(" ");
                answer.scrollTop = answer.scrollHeight;
                const longPosition = readPosition();

                const read = (element) => {
                    const style = window.getComputedStyle(element);
                    return {
                        overflow: style.overflow,
                        overflowX: style.overflowX,
                        overflowY: style.overflowY,
                        position: style.position,
                        zIndex: style.zIndex,
                    };
                };
                return {
                    ancestors: [task, pagination].map(read),
                    responseOutput: read(output),
                    answer: read(answer),
                    paginationParentIsAnswerShell: pagination.parentElement === answer.parentElement,
                    paginationWidth: pagination.getBoundingClientRect().width,
                    shortPosition,
                    longPosition,
                    indicatorVisible: window.getComputedStyle(indicator).opacity === "1",
                    paginationSurface: {
                        background: window.getComputedStyle(pagination).background,
                        borderWidth: window.getComputedStyle(pagination).borderWidth,
                        boxShadow: window.getComputedStyle(pagination).boxShadow,
                        padding: window.getComputedStyle(pagination).padding,
                    },
                };
            }""",
        )
        assert contract is not None
        assert contract["paginationWidth"] > 0
        assert contract["indicatorVisible"]
        assert not contract["paginationSurface"]["background"].startswith("rgba(0, 0, 0, 0)")
        assert contract["paginationSurface"]["borderWidth"] == "1px"
        assert contract["paginationSurface"]["boxShadow"] != "none"
        assert contract["paginationSurface"]["padding"] == "4px"
        assert all(item["overflow"] == "visible" for item in contract["ancestors"])
        assert contract["ancestors"][-1]["position"] == "absolute"
        assert contract["ancestors"][-1]["zIndex"] == "2"
        assert contract["responseOutput"]["overflow"] == "visible"
        assert contract["answer"]["overflowX"] == "hidden"
        assert contract["answer"]["overflowY"] == "auto"
        assert contract["paginationParentIsAnswerShell"]
        assert contract["shortPosition"]["composerGap"] == pytest.approx(10, abs=1)
        assert abs(
            contract["shortPosition"]["composerGap"]
            - contract["longPosition"]["composerGap"]
        ) <= 1
        assert contract["longPosition"]["paginationBottom"] <= contract["longPosition"]["composerTop"]

        ellipsis = page.locator("#agent_response_pagination .local-store-page-ellipsis")
        expect(ellipsis).to_have_count(1)
        ellipsis.hover()
        hover_state = ellipsis.evaluate(
            "element => ({background: getComputedStyle(element).background, boxShadow: getComputedStyle(element).boxShadow})"
        )
        assert hover_state["background"] != "rgba(0, 0, 0, 0)"
        assert hover_state["boxShadow"] != "none"
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_280, 900, False), (390, 844, True)),
)
def test_agent_doctor_actions_keep_spatial_effects_visible(
    disposable_browser: Browser,
    sidebar_server_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    """Verify Doctor action shadows escape the panel at desktop and narrow widths."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/agent/edge/chatgpt",
        width,
        height,
        touch=touch,
        init_script="""
            (() => {
                const originalFetch = window.fetch.bind(window);
                window.fetch = (input, init) => {
                    const requestUrl = typeof input === "string" ? input : input?.url;
                    if (requestUrl) {
                        const pathname = new URL(requestUrl, window.location.href).pathname;
                        if (
                            pathname === "/api/agent/status"
                            || pathname === "/api/browser-session"
                            || pathname === "/api/agent/doctor"
                        ) {
                            return Promise.reject(new Error("Live Agent polling is disabled for this layout test."));
                        }
                    }
                    return originalFetch(input, init);
                };
            })();
        """,
    )
    try:
        contract = page.evaluate(
            """() => {
                const panel = document.querySelector("#agent_doctor_panel");
                const content = document.querySelector("#agent_doctor_panel .agent-doctor-content");
                const actions = document.querySelector("#agent_doctor_actions");
                if (!panel || !content || !actions) return null;

                panel.hidden = false;
                panel.open = true;
                actions.replaceChildren();
                for (const label of [
                    "Continue interrupted task",
                    "Clean up temporary context",
                    "Open provider conversation",
                    "Start a new task",
                ]) {
                    const button = document.createElement("button");
                    button.type = "button";
                    button.className = "secondary-button agent-doctor-action";
                    button.textContent = label;
                    actions.append(button);
                }

                const read = element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return {
                        className: String(element.className || ""),
                        overflow: style.overflow,
                        overflowX: style.overflowX,
                        overflowY: style.overflowY,
                        boxShadow: style.boxShadow,
                        widthCss: style.width,
                        rect: {
                            left: rect.left,
                            top: rect.top,
                            right: rect.right,
                            bottom: rect.bottom,
                            width: rect.width,
                            height: rect.height,
                        },
                    };
                };
                const ancestors = [];
                for (let node = actions; node && node !== document.body; node = node.parentElement) {
                    ancestors.push(read(node));
                }
                const settingsLink = document.querySelector("[data-agent-llm-settings-link]");
                return {
                    panel: read(panel),
                    content: read(content),
                    actions: read(actions),
                    buttons: [...actions.children].map(read),
                    settingsLink: settingsLink ? read(settingsLink) : null,
                    ancestors,
                    documentOverflow: document.documentElement.scrollWidth
                        - document.documentElement.clientWidth,
                };
            }"""
        )
        assert contract is not None
        assert contract["panel"]["overflow"] == "visible"
        assert contract["content"]["overflow"] == "visible"
        assert contract["actions"]["overflow"] == "visible"
        assert len(contract["buttons"]) == 4
        assert all(button["rect"]["width"] > 0 for button in contract["buttons"])
        assert all(button["boxShadow"] != "none" for button in contract["buttons"])
        button_widths = [button["rect"]["width"] for button in contract["buttons"]]
        assert max(button_widths) - min(button_widths) > 8
        assert all(
            button["rect"]["width"] < contract["actions"]["rect"]["width"] - 8
            for button in contract["buttons"]
        )
        assert contract["settingsLink"] is None
        assert all("secondary-button" in button["className"] for button in contract["buttons"])
        assert all(
            ancestor["overflowX"] == "visible" and ancestor["overflowY"] == "visible"
            for ancestor in contract["ancestors"][:4]
        ), contract["ancestors"]
        assert contract["documentOverflow"] <= 1
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_prompt_composer_stays_compact_until_expanded(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep a long Agent task readable without opening the Composer by default."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/agent",
        1_280,
        900,
        touch=False,
        init_script="""
            (() => {
                const originalFetch = window.fetch.bind(window);
                window.fetch = (input, init) => {
                    const requestUrl = typeof input === "string" ? input : input?.url;
                    if (requestUrl && new URL(requestUrl, window.location.href).pathname === "/api/agent/status") {
                        return Promise.reject(new Error("Agent status polling is disabled for this layout test."));
                    }
                    return originalFetch(input, init);
                };
            })();
        """,
    )
    try:
        prompt = page.locator("#agent_prompt_input")
        toggle = page.locator("[data-agent-composer-overflow-toggle]")
        expect(prompt).to_have_attribute("rows", "2")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_have_attribute("aria-label", "Expand question or task")
        expect(toggle).to_be_hidden()
        compact = prompt.evaluate(
            "element => ({height: element.clientHeight, weight: getComputedStyle(element).fontWeight, resize: getComputedStyle(element).resize})"
        )
        assert compact["height"] > 0
        assert compact["weight"] == "400"
        assert compact["resize"] == "none"
        control_heights = page.evaluate(
            """() => ({
                model: document.querySelector('.agent-model-trigger')?.getBoundingClientRect().height,
                effort: document.querySelector('.agent-effort-trigger')?.getBoundingClientRect().height,
            })"""
        )
        assert control_heights == {"model": 32, "effort": 32}

        effort_label = page.locator(".agent-effort-trigger-label")
        expect(effort_label).to_have_count(1)
        assert effort_label.evaluate("element => getComputedStyle(element).fontSize") == "15px"

        effort = page.locator(".agent-effort-trigger")
        effort_menu = page.locator(".agent-effort-dropdown")
        effort.click()
        expect(effort_menu).to_be_visible()
        effort_dropdown_geometry = page.evaluate(
            """() => {
                const trigger = document.querySelector('.agent-effort-trigger').getBoundingClientRect();
                const menu = document.querySelector('.agent-effort-dropdown').getBoundingClientRect();
                const style = getComputedStyle(document.querySelector('.agent-effort-dropdown'));
                return {
                    menuBottom: menu.bottom,
                    triggerTop: trigger.top,
                    position: style.position,
                };
            }"""
        )
        assert effort_dropdown_geometry["position"] == "absolute"
        assert effort_dropdown_geometry["menuBottom"] <= effort_dropdown_geometry["triggerTop"]
        effort.click()
        expect(effort_menu).to_be_hidden()

        prompt.fill("Short task.")
        expect(toggle).to_be_hidden()

        prompt.fill("\n".join(f"Task line {line}" for line in range(1, 9)))
        collapsed = prompt.evaluate(
            "element => ({height: element.clientHeight, scrollHeight: element.scrollHeight})"
        )
        assert collapsed["height"] == compact["height"]
        assert collapsed["scrollHeight"] > collapsed["height"]
        expect(toggle).to_be_visible()
        toggle_geometry = toggle.evaluate(
            """element => {
                const shell = element.closest('.agent-composer-shell').getBoundingClientRect();
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return {
                    position: style.position,
                    top: style.top,
                    right: style.right,
                    topOffset: rect.top - shell.top,
                    rightOffset: shell.right - rect.right,
                };
            }"""
        )
        assert toggle_geometry["position"] == "absolute"
        assert toggle_geometry["top"] == "12px"
        assert toggle_geometry["right"] == "12px"
        assert toggle_geometry["topOffset"] == 13
        assert toggle_geometry["rightOffset"] == 13

        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(toggle).to_have_attribute("aria-label", "Collapse question or task")
        expanded = prompt.evaluate("element => ({height: element.clientHeight, scrollHeight: element.scrollHeight})")
        assert expanded["height"] > collapsed["height"]
        assert expanded["height"] >= min(expanded["scrollHeight"], 360)

        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        final_height = prompt.evaluate("element => element.clientHeight")
        assert final_height == compact["height"]
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_008, 1_085, False), (390, 844, True), (320, 844, True)),
)
def test_chatgpt_effort_footer_keeps_the_fifteen_pixel_label_on_one_line(
    disposable_browser: Browser,
    sidebar_server_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    """Keep the ChatGPT effort controls compact, styled, and readable."""
    catalog_payload = _chatgpt_catalog_sessions()
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": catalog_payload,
    }
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route(
        "**/api/agent/status",
        lambda route: route.fulfill(json=_finished_chatgpt_agent_payload()),
    )
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(json=browser_status),
    )
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(json=catalog_payload),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        effort = page.locator(".agent-effort-trigger")
        model = page.locator(".agent-model-trigger")
        refresh = page.locator("[data-agent-effort-refresh]")
        submit = page.locator("#agent_ask_button")
        expect(effort).to_be_visible()
        expect(model).to_be_visible()
        expect(refresh).to_have_count(0)
        expect(submit).to_be_visible()
        expect(model.locator(".agent-model-trigger-label")).to_have_text("Latest")
        geometry = page.evaluate(
            """() => {
                const rect = selector => {
                    const element = document.querySelector(selector);
                    const value = element?.getBoundingClientRect();
                    return value && {
                        left: value.left,
                        right: value.right,
                        top: value.top,
                        bottom: value.bottom,
                        width: value.width,
                        height: value.height,
                    };
                };
                const label = document.querySelector('.agent-effort-trigger-label');
                const labelRect = label?.getBoundingClientRect();
                const labelStyle = label && getComputedStyle(label);
                const protectedZone = selector => {
                    const trigger = document.querySelector(selector);
                    const triggerRect = trigger?.getBoundingClientRect();
                    const labelRect = trigger?.querySelector('.trade-strategy-trigger-label')?.getBoundingClientRect();
                    const chevronRect = trigger?.querySelector('.browser-picker-trigger-chevron')?.getBoundingClientRect();
                    return {
                        labelRight: labelRect?.right,
                        chevronLeft: chevronRect?.left,
                        chevronRight: chevronRect?.right,
                        triggerRight: triggerRect?.right,
                    };
                };
                return {
                    shell: rect('.agent-composer-shell'),
                    footer: rect('.agent-composer-footer'),
                    effort: rect('.agent-effort-trigger'),
                    model: rect('.agent-model-trigger'),
                    effortProtectedZone: protectedZone('.agent-effort-trigger'),
                    modelProtectedZone: protectedZone('.agent-model-trigger'),
                    submit: rect('#agent_ask_button'),
                    labelFontSize: labelStyle?.fontSize,
                    labelLineHeight: labelStyle?.lineHeight,
                    labelWhiteSpace: labelStyle?.whiteSpace,
                    labelHeight: labelRect?.height,
                    horizontalOverflow: Math.max(
                        document.documentElement.scrollWidth,
                        document.body.scrollWidth,
                    ) - document.documentElement.clientWidth,
                };
            }"""
        )
        assert geometry["labelFontSize"] == "15px"
        assert geometry["labelWhiteSpace"] == "nowrap"
        assert geometry["labelHeight"] <= float(geometry["labelLineHeight"][:-2]) + 1
        assert geometry["effort"]["height"] == 32
        assert geometry["model"]["height"] == 32
        assert geometry["model"]["width"] < 190
        assert geometry["submit"]["height"] == 32
        assert geometry["horizontalOverflow"] <= 1
        submit_center_x = (geometry["submit"]["left"] + geometry["submit"]["right"]) / 2
        submit_center_y = (geometry["submit"]["top"] + geometry["submit"]["bottom"]) / 2
        assert abs(
            geometry["shell"]["right"] - submit_center_x
            - (geometry["shell"]["bottom"] - submit_center_y)
        ) <= 1
        if width > 560:
            assert abs(
                geometry["submit"]["right"] - geometry["footer"]["right"]
            ) <= 1
            assert geometry["model"]["left"] > geometry["footer"]["left"]
            for selector in ("model", "effort", "submit"):
                assert abs(
                    geometry[selector]["top"] - geometry["footer"]["top"]
                ) <= 1
        for protected_zone in (
            geometry["effortProtectedZone"],
            geometry["modelProtectedZone"],
        ):
            assert protected_zone["chevronLeft"] - protected_zone["labelRight"] >= 8
            assert protected_zone["triggerRight"] - protected_zone["chevronRight"] >= 8
        effort.click()
        effort_menu = page.locator(".agent-effort-dropdown")
        expect(effort_menu).to_be_visible()
        menu_geometry = page.evaluate(
            """() => {
                const menu = document.querySelector('.agent-effort-dropdown')?.getBoundingClientRect();
                const trigger = document.querySelector('.agent-effort-trigger')?.getBoundingClientRect();
                const options = [...document.querySelectorAll('.agent-effort-dropdown .trade-strategy-dropdown-text')].map(text => ({
                    label: text.textContent.trim(),
                    clientWidth: text.clientWidth,
                    scrollWidth: text.scrollWidth,
                }));
                return {
                    menuLeft: menu?.left,
                    menuBottom: menu?.bottom,
                    menuRight: menu?.right,
                    menuWidth: menu?.width,
                    triggerTop: trigger?.top,
                    options,
                };
            }"""
        )
        assert menu_geometry["menuBottom"] <= geometry["effort"]["top"] + 1
        assert menu_geometry["menuLeft"] >= -1
        assert menu_geometry["menuRight"] <= width + 1
        assert menu_geometry["menuWidth"] > geometry["effort"]["width"] + 1
        assert all(option["scrollWidth"] <= option["clientWidth"] + 1 for option in menu_geometry["options"])
        effort.click()
        expect(effort_menu).to_be_hidden()
        if width <= 560:
            assert geometry["model"]["bottom"] <= geometry["effort"]["top"]
            assert geometry["effort"]["right"] <= geometry["submit"]["left"]
            non_chatgpt = page.evaluate(
                """() => {
                    const effortField = document.querySelector('[data-agent-effort-field]');
                    const footer = document.querySelector('.agent-composer-footer');
                    const model = document.querySelector('.agent-model-trigger');
                    const submit = document.querySelector('#agent_ask_button');
                    if (!(effortField instanceof HTMLElement)) return null;
                    effortField.hidden = true;
                    return {
                        footerDisplay: getComputedStyle(footer).display,
                        modelTop: model.getBoundingClientRect().top,
                        submitTop: submit.getBoundingClientRect().top,
                    };
                }"""
            )
            assert non_chatgpt is not None
            assert non_chatgpt["footerDisplay"] == "flex"
            assert abs(non_chatgpt["modelTop"] - non_chatgpt["submitTop"]) <= 1
        else:
            assert abs(geometry["model"]["top"] - geometry["effort"]["top"]) <= 1
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_model_and_sidebar_service_triggers_follow_typography_contract(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Verify Agent sidebar labels preserve their scoped typography and wrapping contracts."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/agent",
        1_280,
        900,
        touch=False,
    )
    try:
        main_button = page.locator(
            "xpath=/html/body/main/div/section/div[2]/article/form/label/span/span/span[1]/button"
        )
        sidebar_button = page.locator(
            "xpath=/html/body/main/div/aside/form/div[2]/label/div/button"
        )
        expect(main_button).to_have_count(1)
        expect(sidebar_button).to_have_count(1)

        platform_button = page.locator(
            ".agent-platform-combobox [data-agent-combobox-trigger]"
        )
        session_source_button = page.locator(
            ".agent-session-mode-combobox [data-agent-combobox-trigger]"
        )
        expect(platform_button).to_have_count(1)
        expect(session_source_button).to_have_count(1)
        assert platform_button.evaluate("element => element.getBoundingClientRect().height") == 36
        assert session_source_button.evaluate("element => element.getBoundingClientRect().height") == 36

        typography = page.evaluate(
            """([main, sidebar]) => {
                const readLabel = (button) => {
                    const label = button?.querySelector("[data-agent-combobox-selected-label]");
                    if (!label) return null;
                    const style = window.getComputedStyle(label);
                    return {
                        fontFamily: style.fontFamily,
                        fontSize: style.fontSize,
                        fontWeight: style.fontWeight,
                        lineHeight: style.lineHeight,
                    };
                };
                return [readLabel(main), readLabel(sidebar)];
            }""",
            [main_button.element_handle(), sidebar_button.element_handle()],
        )
        main_typography, sidebar_typography = typography
        assert main_typography is not None
        assert sidebar_typography is not None
        assert main_typography["fontFamily"] == sidebar_typography["fontFamily"]
        assert main_typography["fontSize"] == "15px"
        assert main_typography["lineHeight"] == "21.75px"
        assert sidebar_typography["fontSize"] == "15px"
        assert sidebar_typography["lineHeight"] == "21.75px"
        assert main_typography["fontWeight"] == "400"
        assert sidebar_typography["fontWeight"] == "400"
        sidebar_trigger_sizes = page.locator(
            "#agent_runtime_form .agent-combobox-trigger [data-agent-combobox-selected-label]"
        ).evaluate_all("elements => elements.map(element => getComputedStyle(element).fontSize)")
        assert sidebar_trigger_sizes
        assert set(sidebar_trigger_sizes) == {"15px"}
        project_label = page.locator(".agent-runtime-form > label.field > .field-label")
        project_name = page.locator("[data-agent-project-name]")
        expect(project_label).to_have_count(1)
        expect(project_name).to_have_count(1)
        expect(project_label).to_have_text(re.compile(r"^Current project:\s+\S+$"))
        expect(project_name).to_be_visible()
        assert page.locator(
            '.agent-runtime-form > label.field > .field-help[data-agent-project-name]'
        ).count() == 0
        assert project_name.evaluate(
            "element => element.parentElement.classList.contains('field-label')"
        )
        browser_label = page.locator(
            ".agent-runtime-form .agent-connect-fields > .field:nth-child(2) > .field-label"
        )
        expect(browser_label).to_have_text("Browser")
        for width, height in ((1_280, 900), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            label_layout = project_label.evaluate(
                """element => {
                    const style = getComputedStyle(element);
                    return {
                        whiteSpace: style.whiteSpace,
                        documentOverflow: Math.max(
                            document.documentElement.scrollWidth,
                            document.body.scrollWidth,
                        ) > document.documentElement.clientWidth,
                    };
                }"""
            )
            assert label_layout["whiteSpace"] == "normal"
            assert not label_layout["documentOverflow"]
            assert browser_label.evaluate("element => getComputedStyle(element).fontWeight") == "400"
        assert project_name.evaluate("element => getComputedStyle(element).fontSize") == "17px"
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_browser_session_status_reuses_account_typography_for_terminal_and_cache(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Verify Agent and Cache status surfaces reuse the same non-bold status typography."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/agent",
        1_280,
        900,
        touch=False,
    )
    try:
        account = page.locator(
            "xpath=/html/body/main/div/aside/form/div[1]/label[2]/div/div[2]/div/div/div/strong"
        )
        terminal_label = page.locator(
            "xpath=/html/body/main/div/aside/form/div[1]/label[2]/div/div[2]/div/div/p/span[2]"
        )
        expect(account).to_have_count(1)
        expect(terminal_label).to_have_count(1)

        agent_typography = page.evaluate(
            """([accountElement, terminalElement]) => {
                const read = (element) => {
                    const style = window.getComputedStyle(element);
                    return {
                        fontFamily: style.fontFamily,
                        fontSize: style.fontSize,
                        fontWeight: style.fontWeight,
                        lineHeight: style.lineHeight,
                        textAlign: style.textAlign,
                    };
                };
                return [read(accountElement), read(terminalElement)];
            }""",
            [account.element_handle(), terminal_label.element_handle()],
        )
        assert agent_typography[0] == agent_typography[1]
        assert agent_typography[0]["fontWeight"] == "400"
        assert agent_typography[0]["textAlign"] == "left"

        page.goto(f"{sidebar_server_url}/cache/chatgpt", wait_until="domcontentloaded")
        cache_account = page.locator("aside .browser-session-status-account")
        expect(cache_account).to_have_count(1)
        cache_typography = cache_account.evaluate(
            "element => { const style = getComputedStyle(element); return {fontFamily: style.fontFamily, fontSize: style.fontSize, fontWeight: style.fontWeight, lineHeight: style.lineHeight, textAlign: style.textAlign}; }"
        )
        assert cache_typography == agent_typography[0]
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("width", (1_018, 390))
@pytest.mark.parametrize("source", ("claude", "grok"))
def test_cache_browser_session_failure_message_matches_account_typography_and_hangs_after_status_icon(
    disposable_browser: Browser,
    sidebar_server_url: str,
    width: int,
    source: str,
) -> None:
    """Align every failure-copy line with the account text, never under the status icon."""
    browser_status = {
        "can_download": False,
        "account_name": "Security verification required",
        "message": "Edge could not verify an available Claude message composer." if source == "claude" else (
            "Grok showed a Cloudflare security verification page in Edge, so the "
            "signed-in account could not be verified."
        ),
    }
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1_294 if width > 900 else 844},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    try:
        page.goto(f"{sidebar_server_url}/cache/{source}", wait_until="domcontentloaded")
        if width <= 900:
            page.locator("#sidebar_toggle").click()
        account = page.locator(".browser-session-status-account")
        message = page.locator(
            '.browser-session-status-message[data-role="browser-session-message"]'
        )
        status_icon = page.locator(
            '.browser-session-status-item .browser-session-status-checkmark[data-status-state="error"]'
        )
        expect(account).to_have_count(1)
        expect(message).to_be_visible()
        expect(status_icon).to_be_visible()

        layout = page.evaluate(
            """() => {
                const account = document.querySelector('.browser-session-status-account');
                const message = document.querySelector('.browser-session-status-message[data-role="browser-session-message"]');
                const icon = document.querySelector('.browser-session-status-item .browser-session-status-checkmark[data-status-state="error"]');
                const item = document.querySelector('.browser-session-status-item');
                const card = document.querySelector('.browser-session-status-card');
                const readTypography = (element) => {
                    const style = getComputedStyle(element);
                    return {
                        fontFamily: style.fontFamily,
                        fontSize: style.fontSize,
                        fontWeight: style.fontWeight,
                        lineHeight: style.lineHeight,
                        textAlign: style.textAlign,
                    };
                };
                const messageStyle = getComputedStyle(message);
                const itemStyle = getComputedStyle(item);
                const range = document.createRange();
                range.selectNodeContents(message);
                const lines = Array.from(range.getClientRects());
                return {
                    accountTypography: readTypography(account),
                    messageTypography: readTypography(message),
                    messageMarginTop: messageStyle.marginTop,
                    messagePaddingInlineStart: messageStyle.paddingInlineStart,
                    messageTextIndent: messageStyle.textIndent,
                    iconRight: icon.getBoundingClientRect().right,
                    accountLeft: account.getBoundingClientRect().left,
                    itemGap: parseFloat(itemStyle.columnGap || itemStyle.gap),
                    messageRight: message.getBoundingClientRect().right,
                    cardRight: card.getBoundingClientRect().right,
                    lineLefts: lines.map(line => line.left),
                };
            }"""
        )
        assert layout["accountTypography"] == layout["messageTypography"]
        assert layout["messageMarginTop"] == "0px"
        assert layout["messagePaddingInlineStart"] == "26px"
        assert layout["messageTextIndent"] == "0px"
        assert len(layout["lineLefts"]) >= 2
        assert all(abs(left - layout["accountLeft"]) <= 1 for left in layout["lineLefts"])
        assert abs(layout["accountLeft"] - (layout["iconRight"] + layout["itemGap"])) <= 1
        assert layout["messageRight"] <= layout["cardRight"] + 1
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("width", (1_018, 390))
@pytest.mark.parametrize("source", ("claude", "gemini", "chatgpt"))
def test_cache_notice_flows_without_overlap_and_keeps_polling(
    disposable_browser: Browser, sidebar_server_url: str, width: int, source: str,
) -> None:
    """Keep source notices below current progress without losing live status updates."""
    context = disposable_browser.new_context(viewport={"width": width, "height": 1_294}, reduced_motion="reduce")
    page = context.new_page()
    polls = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def status_response(route):
        polls.append(True)
        route.fulfill(json={"phase": "idle", "running": False, "message": f"Status refresh {len(polls)}"})

    page.route(f"**/api/cache/{source}/status*", status_response)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={"can_download": False}))
    try:
        page.goto(f"{sidebar_server_url}/cache/{source}", wait_until="domcontentloaded")
        expect(page.locator("#message")).to_have_text("Status refresh 2", timeout=10_000)
        expect(page.locator("#overview .summary-list, #output_dir, [data-output-directory-open]")).to_have_count(0)
        notice = page.locator("#overview .notice-inline-banner")
        if source == "chatgpt":
            expect(notice).to_have_count(0)
            expect(page.locator("#overview .cache-summary-metrics")).to_be_visible()
            expect(page.locator("#overview .cache-run-progress")).to_be_visible()
            expect(page.locator("#status_progress")).to_be_visible()
            expect(page.locator("#overview .progress-metric-grid")).to_be_hidden()
            assert not errors
            return
        expect(notice).to_be_visible()
        geometry = notice.evaluate("""element => {
            const rect = node => {
                const r = node.getBoundingClientRect();
                return {left:r.left, right:r.right, top:r.top, bottom:r.bottom};
            };
            return {
                notice: rect(element),
                label: rect(element.querySelector('.notice-floating-label')),
                title: rect(element.querySelector('.notice-floating-title')),
                chip: rect(element.querySelector('.status-chip')),
                progress: rect(document.querySelector('#overview .cache-run-progress')),
                overflow: element.scrollWidth - element.clientWidth,
                pageOverflow: document.documentElement.scrollWidth - innerWidth,
            };
        }""")
        assert geometry["label"]["bottom"] <= geometry["title"]["top"]
        assert geometry["title"]["bottom"] <= geometry["notice"]["bottom"]
        assert geometry["chip"]["bottom"] <= geometry["notice"]["bottom"]
        assert geometry["progress"]["bottom"] <= geometry["notice"]["top"]
        if width > 560:
            assert geometry["title"]["right"] <= geometry["chip"]["left"]
        else:
            assert geometry["title"]["bottom"] <= geometry["chip"]["top"]
        assert geometry["overflow"] <= 1
        assert geometry["pageOverflow"] <= 1
        assert not errors
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("width", (1_018, 390))
def test_shared_dropdown_gel_tracks_and_reduced_motion(
    disposable_browser: Browser, sidebar_server_url: str, width: int,
) -> None:
    """Verify real menu physics, final geometry, transparent tracks, and motion opt-out."""
    page, context = _open_page(
        disposable_browser, f"{sidebar_server_url}/agent/edge/chatgpt",
        width, 1_294 if width > 900 else 844, touch=False,
        reduced_motion="no-preference",
    )
    try:
        trigger = page.locator(".agent-session-mode-combobox [data-agent-combobox-trigger]")
        menu = page.get_by_role("listbox", name="Choose a session source", exact=True)
        if width <= 900:
            page.locator("#sidebar_toggle").click()
        trigger.click()
        expect(menu).to_be_visible()
        motion = menu.evaluate("""element => {
            const animation = element.getAnimations()[0];
            animation.pause();
            animation.currentTime = 204;
            const matrix = new DOMMatrix(getComputedStyle(element).transform);
            const rebound = matrix.a;
            animation.finish();
            const settled = new DOMMatrix(getComputedStyle(element).transform);
            return {name: animation.animationName, rebound,
                scale: settled.a, shift: settled.e,
                track: getComputedStyle(element, '::-webkit-scrollbar-track').backgroundColor,
                thumb: getComputedStyle(element, '::-webkit-scrollbar-thumb').backgroundColor};
        }""")
        assert motion["name"] == "browser-pagination-range-gel-in-below"
        assert motion["rebound"] > 1
        assert motion["scale"] == 1
        assert motion["shift"] == 0
        assert motion["track"] == "rgba(0, 0, 0, 0)"
        assert motion["thumb"] != "rgba(0, 0, 0, 0)"

        menu.get_by_role("option", name="Projects", exact=True).click()
        trigger.click()
        expect(menu).to_be_visible()
        assert menu.evaluate("e => getComputedStyle(e).animationName") == "browser-pagination-range-gel-in"
        page.emulate_media(reduced_motion="reduce")
        assert menu.evaluate("e => getComputedStyle(e).animationName") == "none"
        trigger.press("Escape")
        expect(menu).to_be_hidden()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("width", (1_018, 390))
def test_dropdown_chevrons_use_trigger_text_color_in_dark_theme(
    disposable_browser: Browser, sidebar_server_url: str, width: int,
) -> None:
    """Keep every rendered dropdown chevron the same color as its trigger text."""
    page, context = _open_page(
        disposable_browser, f"{sidebar_server_url}/cache/chatgpt",
        width, 900 if width > 900 else 844, touch=False,
    )
    try:
        page.emulate_media(color_scheme="dark")
        chevrons = page.evaluate("""() => Array.from(
            document.querySelectorAll('.browser-picker-trigger-chevron')
        ).map(element => {
            const trigger = element.closest('button');
            const arrowStyle = getComputedStyle(element);
            const triggerStyle = trigger && getComputedStyle(trigger);
            return {
                arrowColor: arrowStyle.color,
                arrowBackground: arrowStyle.backgroundColor,
                triggerColor: triggerStyle?.color,
                maskImage: arrowStyle.maskImage,
                webkitMaskImage: arrowStyle.webkitMaskImage,
            };
        })""")
        assert chevrons
        for chevron in chevrons:
            assert chevron["arrowColor"] == chevron["triggerColor"]
            assert chevron["arrowBackground"] == chevron["triggerColor"]
            assert "data:image/svg+xml" in chevron["maskImage"]
            assert "data:image/svg+xml" in chevron["webkitMaskImage"]
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("width", (1_018, 390))
def test_cache_metric_cards_blend_into_the_overview_in_dark_theme(
    disposable_browser: Browser, sidebar_server_url: str, width: int,
) -> None:
    """Keep Foundation metric cards visually transparent inside the overview surface."""
    page, context = _open_page(
        disposable_browser, f"{sidebar_server_url}/cache/chatgpt",
        width, 900 if width > 900 else 844, touch=False,
    )
    try:
        page.emulate_media(color_scheme="dark")
        cards = page.evaluate("""() => Array.from(
            document.querySelectorAll('#overview .foundation-metric-card')
        ).map(element => {
            const style = getComputedStyle(element);
            return {
                backgroundColor: style.backgroundColor,
                borderWidth: style.borderWidth,
                borderRadius: style.borderRadius,
                boxShadow: style.boxShadow,
                backdropFilter: style.backdropFilter,
                webkitBackdropFilter: style.webkitBackdropFilter || style.backdropFilter,
            };
        })""")
        assert cards
        for card in cards:
            assert card["backgroundColor"] == "rgba(0, 0, 0, 0)"
            assert card["borderWidth"] == "0px"
            assert card["borderRadius"] == "0px"
            assert card["boxShadow"] == "none"
            assert card["backdropFilter"] == "none"
            assert card["webkitBackdropFilter"] == "none"
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("source_key", "settings_category"),
    (("x", "downloads"), ("gemini", "llm"), ("claude", "llm"), ("zhihu", "downloads")),
)
def test_cache_shared_settings_link_opens_the_expected_category(
    disposable_browser: Browser,
    sidebar_server_url: str,
    source_key: str,
    settings_category: str,
) -> None:
    """Verify each Cache settings link leaves the source form and opens its category."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/{source_key}",
        1_280,
        900,
        touch=False,
    )
    try:
        settings_link = page.locator(".cache-settings-link")
        expect(settings_link).to_have_count(1)
        expect(settings_link).to_have_class(re.compile(r"\bsecondary-button\b"))
        expect(settings_link).to_have_attribute(
            "href",
            f"/settings#settings-{settings_category}",
        )
        expect(page.locator("#start_form section")).to_have_count(0)
        assert settings_link.evaluate("element => !element.closest('form')")
        for width, height in ((1_018, 1_294), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            alignment = settings_link.evaluate("""element => {
                const parent = element.parentElement;
                const rect = parent.getBoundingClientRect();
                const style = getComputedStyle(parent);
                return {
                    right: element.getBoundingClientRect().right,
                    contentRight: rect.right - parseFloat(style.paddingRight)
                        - parseFloat(style.borderRightWidth),
                };
            }""")
            assert abs(alignment["right"] - alignment["contentRight"]) <= 1
        if source_key == "gemini":
            for field_name in (
                "gemini_max_conversations",
                "gemini_scroll_pause_seconds",
                "gemini_stale_round_limit",
            ):
                expect(page.locator(f"#{field_name}")).to_have_count(0)

        settings_link.click()
        page.wait_for_url(re.compile(rf"/settings#settings-{settings_category}$"))

        expect(page.locator("[data-settings-category-shell]")).to_have_attribute(
            "data-active-category",
            settings_category,
        )
        expect(page.locator(f"#settings-{settings_category}")).to_be_visible()
        expect(page.locator(f'[data-settings-category="{settings_category}"]')).to_have_class(
            re.compile(r"\bis-active\b")
        )
        if source_key == "gemini":
            for field_name in (
                "gemini_max_conversations",
                "gemini_scroll_pause_seconds",
                "gemini_stale_round_limit",
            ):
                expect(page.locator(f"#{field_name}")).to_be_visible()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "height", "touch"),
    (
        (1_034, 1_170, False),
        (390, 844, True),
    ),
)
def test_settings_reuse_shared_primary_and_numeric_control_contracts(
    disposable_browser: Browser,
    sidebar_server_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    """Verify Settings controls share the annotated visual and layout contracts."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/settings#settings-llm",
        width,
        height,
        touch=touch,
    )
    try:
        expect(page.locator("#settings")).to_have_count(0)
        expect(page.locator("#settings_workspace .workspace-kicker")).to_have_count(0)
        expect(page.locator("#settings_workspace .workspace-summary-card h2")).to_have_text(
            "Configuration center"
        )
        expect(page.locator("#settings_sidebar .hero h1")).to_have_text("Settings")
        expect(page.locator("#chatgpt_startup_timeout_seconds")).to_have_count(1)
        for field_name in (
            "gemini_max_conversations",
            "gemini_scroll_pause_seconds",
            "gemini_stale_round_limit",
        ):
            expect(page.locator(f"#{field_name}")).to_be_visible()
        assert page.locator("#chatgpt_startup_timeout_seconds").evaluate(
            "element => getComputedStyle(element).fontWeight"
        ) == "300"

        terminal_button = page.locator("[data-agent-terminal-authorization-button]")
        expect(terminal_button).to_have_count(1)
        assert terminal_button.evaluate(
            "element => getComputedStyle(element).fontWeight"
        ) == "500"

        page.goto(
            f"{sidebar_server_url}/settings#settings-cloud",
            wait_until="domcontentloaded",
        )
        expect(page.locator("#settings-cloud")).to_be_visible()
        expect(page.locator("#settings_workspace .workspace-kicker")).to_have_count(0)
        expect(page.locator("#shadow_backup_phase")).to_have_count(0)
        expect(page.locator("[data-shadow-backup-status-copy]")).to_have_count(1)
        sync_button = page.locator("#shadow_backup_sync_now")
        expect(sync_button).to_have_count(1)
        assert sync_button.evaluate(
            "element => getComputedStyle(element).fontWeight"
        ) == "500"

        page.goto(
            f"{sidebar_server_url}/settings#settings-maintenance",
            wait_until="domcontentloaded",
        )
        expect(page.locator("#settings-maintenance")).to_have_count(0)
        expect(page.locator("#settings-browser")).to_be_visible()
        expect(page.locator('[data-settings-category="maintenance"]')).to_have_count(0)
        expect(page.locator("#reset_button, #reset_chatgpt_button")).to_have_count(0)

        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth"
        )
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "height", "touch"),
    (
        (1_280, 900, False),
        (390, 844, True),
    ),
)
def test_settings_reuse_shared_content_control_and_effect_boundaries(
    disposable_browser: Browser,
    sidebar_server_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    """Verify the 640px/384px maxima and keep physical cards unclipped."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/settings#settings-agent",
        width,
        height,
        touch=touch,
    )
    try:
        expect(page.locator("#settings-agent")).to_be_visible()
        page.wait_for_function(
            """() => getComputedStyle(document.documentElement)
                .getPropertyValue("--layout-content-width").trim() === '640px'"""  # noqa: E501
        )
        geometry = page.evaluate(
            """() => {
                const rectWidth = selector => document.querySelector(selector)
                    ?.getBoundingClientRect().width ?? 0;
                const action = document.querySelector(".settings-agent-terminal-action");
                const scrollport = document.querySelector("[data-settings-content-scrollport]");
                const ancestorOverflow = [];
                for (let node = action; node && node !== scrollport; node = node.parentElement) {
                    const style = getComputedStyle(node);
                    ancestorOverflow.push({
                        selector: node.id || node.className || node.tagName,
                        x: style.overflowX,
                        y: style.overflowY,
                    });
                }
                return {
                    contentToken: getComputedStyle(document.documentElement)
                        .getPropertyValue("--layout-content-width").trim(),
                    controlToken: getComputedStyle(document.documentElement)
                        .getPropertyValue("--layout-control-width").trim(),
                    heading: rectWidth("#settings_workspace .workspace-summary-card > .report-heading-row"),
                    panel: rectWidth("#settings-agent"),
                    field: rectWidth("#settings-agent .field"),
                    action: rectWidth(".settings-agent-terminal-action"),
                    actionOverflow: action ? getComputedStyle(action).overflow : "missing",
                    actionShadow: action ? getComputedStyle(action).boxShadow : "none",
                    shellOverflow: getComputedStyle(
                        document.querySelector("#settings_workspace .workspace-summary-card")
                    ).overflow,
                    scrollportOverflow: scrollport
                        ? getComputedStyle(scrollport).overflow
                        : "missing",
                    scrollportBleed: action && scrollport
                        ? action.getBoundingClientRect().left
                            - scrollport.getBoundingClientRect().left
                        : 0,
                    ancestorOverflow,
                    documentOverflow: document.documentElement.scrollWidth
                        - document.documentElement.clientWidth,
                };
            }"""
        )

        assert geometry["contentToken"] == "640px"
        assert geometry["controlToken"] == "384px"
        expected_content_width = min(640, geometry["panel"])
        expected_control_width = min(384, geometry["panel"])
        assert abs(geometry["heading"] - geometry["panel"]) <= 1
        assert abs(geometry["action"] - expected_content_width) <= 1
        assert abs(geometry["field"] - expected_control_width) <= 1
        assert geometry["actionOverflow"] == "visible"
        assert geometry["actionShadow"] != "none"
        assert geometry["shellOverflow"] == "visible"
        assert geometry["scrollportOverflow"] == "hidden auto"
        assert geometry["scrollportBleed"] >= 47
        assert all(
            item["x"] == "visible" and item["y"] == "visible"
            for item in geometry["ancestorOverflow"]
        ), geometry["ancestorOverflow"]
        assert geometry["documentOverflow"] <= 1

        page.goto(
            f"{sidebar_server_url}/settings/style-tokens",
            wait_until="domcontentloaded",
        )
        style_geometry = page.evaluate(
            """() => ({
                heading: document.querySelector(".settings-shell-style-tokens > .settings-summary-card")
                    ?.getBoundingClientRect().width ?? 0,
                workspace: document.querySelector("#workspace_panel")
                    ?.getBoundingClientRect().width ?? 0,
                shellOverflow: getComputedStyle(
                    document.querySelector(".settings-workspace-header.settings-shell-style-tokens")
                ).overflow,
                cardOverflow: getComputedStyle(
                    document.querySelector(".settings-shell-style-tokens .style-token-card")
                ).overflow,
                documentOverflow: document.documentElement.scrollWidth
                    - document.documentElement.clientWidth,
            })"""
        )
        assert style_geometry["heading"] <= min(640, style_geometry["workspace"]) + 1
        if width >= 901:
            assert abs(style_geometry["heading"] - 640) <= 1
        assert style_geometry["shellOverflow"] == "visible"
        assert style_geometry["cardOverflow"] == "visible"
        assert style_geometry["documentOverflow"] <= 1
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_cache_action_row_switches_stop_visibility_with_running_state(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Verify the primary Cache action uses the idle and running layouts."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/grok",
        1_280,
        900,
        touch=False,
        init_script="sessionStorage.setItem('cachelikes:browser-content-mode:v1', 'media');",
    )
    try:
        action_row = page.locator("[data-cache-action-row]")
        stop_form = page.locator(".cache-action-row .sidebar-form-stop")
        start_button = page.locator("#start_button")
        expect(action_row).to_have_attribute("data-action-running", "false")
        expect(stop_form).to_be_hidden()
        expect(start_button).to_have_text("Start")
        idle_start_right = start_button.evaluate(
            """button => {
                return button.getBoundingClientRect().right;
            }"""
        )
        assert idle_start_right >= action_row.evaluate(
            "row => row.getBoundingClientRect().right - 2"
        )

        def fulfill_running_status(route) -> None:
            response = route.fetch()
            payload = response.json()
            payload["running"] = True
            route.fulfill(response=response, json=payload)

        page.route("**/api/cache/grok/status?content_mode=media", fulfill_running_status)
        page.reload(wait_until="domcontentloaded")
        expect(action_row).to_have_attribute("data-action-running", "true")
        expect(page.locator(".cache-action-row .sidebar-form-start")).to_be_hidden()
        expect(stop_form).to_be_visible()
        stop_button = stop_form.locator("#stop_button")
        expect(stop_button).to_have_class(re.compile(r"\bdanger-button\b"))
        stop_border = stop_button.evaluate(
            "button => ({width: getComputedStyle(button).borderWidth, "
            "color: getComputedStyle(button).borderColor})"
        )
        assert stop_border == {"width": "0px", "color": "rgba(0, 0, 0, 0)"}
        running_stop_right = stop_button.evaluate(
            "button => button.getBoundingClientRect().right"
        )
        assert abs(running_stop_right - idle_start_right) <= 2
        page.unroute("**/api/cache/grok/status?content_mode=media", fulfill_running_status)
        expect(action_row).to_have_attribute("data-action-running", "false")
        expect(start_button).to_be_visible()
        expect(stop_form).to_be_hidden()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_simplified_chinese_language_boundary_runs_in_real_browser(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Verify the language boundary after startup and dynamic DOM mutations."""
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/x",
        1_280,
        900,
        touch=False,
        init_script="""
            document.addEventListener("DOMContentLoaded", () => {
                const button = document.createElement("button");
                button.id = "language-rendering-startup-button";
                button.textContent = "首屏简体中文";
                document.body.append(button);
            }, {once: true});
        """,
    )
    try:
        expect(page.locator('script[src*="language-rendering.js"]')).to_have_count(1)
        expect(page.locator("#language-rendering-startup-button")).to_have_attribute(
            "lang",
            "zh-CN",
        )

        # This is the production browser-session trigger shape: the visible
        # label is nested inside the button named by the original issue.
        trigger = page.locator('[data-role="browser-picker-trigger"]')
        expect(trigger).to_have_count(1)
        selected_label = trigger.locator('[data-role="browser-picker-selected-label"]')
        selected_label.evaluate("element => { element.textContent = '简体中文按钮'; }")
        expect(selected_label).to_have_attribute("lang", "zh-CN")
        assert selected_label.evaluate("element => element.matches(':lang(zh-CN)')")

        page.evaluate(
            """() => {
                const button = document.createElement("button");
                button.id = "language-rendering-dynamic-button";
                button.textContent = "动态简体中文";
                document.body.append(button);

                const attributeButton = document.createElement("button");
                attributeButton.id = "language-rendering-attribute-button";
                attributeButton.textContent = "English fallback";
                document.body.append(attributeButton);
                attributeButton.setAttribute("aria-label", "简体中文标签");
                attributeButton.setAttribute("title", "简体中文标题");

                const input = document.createElement("input");
                input.id = "language-rendering-input";
                document.body.append(input);
                input.value = "简体中文输入";
                input.dispatchEvent(new Event("input", {bubbles: true}));

                const traditional = document.createElement("span");
                traditional.id = "language-rendering-traditional-boundary";
                traditional.lang = "zh-Hant";
                traditional.textContent = "繁體中文保留边界";
                document.body.append(traditional);
                traditional.textContent = "后续繁體中文仍保留边界";

                const english = document.createElement("button");
                english.id = "language-rendering-english-only";
                english.textContent = "English only";
                document.body.append(english);

                const sourceIdentity = document.createElement("button");
                sourceIdentity.id = "language-rendering-source-identity";
                sourceIdentity.textContent = "啓 啟 天后 吳 吴";
                document.body.append(sourceIdentity);
            }""",
        )

        assert page.locator("#language-rendering-dynamic-button").text_content() == "动态简体中文"
        expect(page.locator("#language-rendering-dynamic-button")).to_have_attribute(
            "lang",
            "zh-CN",
        )
        expect(page.locator("#language-rendering-attribute-button")).to_have_attribute(
            "lang",
            "zh-CN",
        )
        expect(page.locator("#language-rendering-input")).to_have_attribute("lang", "zh-CN")
        expect(page.locator("#language-rendering-traditional-boundary")).to_have_attribute(
            "lang",
            "zh-Hant",
        )
        assert page.locator("#language-rendering-traditional-boundary").text_content() == (
            "后续繁體中文仍保留边界"
        )
        assert page.locator("#language-rendering-source-identity").text_content() == "啓 啟 天后 吳 吴"
        assert page.locator("#language-rendering-english-only").get_attribute("lang") is None

        page.locator("#language-rendering-english-only").evaluate(
            "element => { element.textContent = '后续动态简体中文'; }"
        )
        expect(page.locator("#language-rendering-english-only")).to_have_attribute(
            "lang",
            "zh-CN",
        )

        page.goto(f"{sidebar_server_url}/agent", wait_until="domcontentloaded")
        session_mode_trigger = page.locator(
            "xpath=/html/body/main/div/aside/form/div[2]/label/div/button"
        )
        expect(session_mode_trigger).to_have_count(1)
        # Keep the production session-list shape while isolating the language
        # fixture from the Agent poller, which legitimately re-renders its live controls.
        page.evaluate(
            """() => {
                const fixture = document.createElement("div");
                fixture.id = "language-rendering-agent-session-fixture";
                const createTrigger = (source, label) => {
                    const trigger = source.cloneNode(true);
                    trigger.querySelector("[data-agent-combobox-selected-label]").textContent = label;
                    return trigger;
                };
                const recentList = document.createElement("div");
                recentList.dataset.agentSessionList = "recent";
                const recentOption = document.createElement("button");
                recentOption.type = "button";
                recentOption.textContent = "简体中文最近会话";
                recentList.append(recentOption);
                fixture.append(
                    createTrigger(
                        document.querySelector(
                            ".agent-session-mode-combobox [data-agent-combobox-trigger]"
                        ),
                        "简体中文会话标题",
                    ),
                    recentList,
                );
                document.body.append(fixture);
            }""",
        )
        fixture = page.locator("#language-rendering-agent-session-fixture")
        session_mode_trigger = fixture.locator("[data-agent-combobox-trigger]").nth(0)
        session_mode_label = session_mode_trigger.locator("[data-agent-combobox-selected-label]")
        recent_session_option = fixture.locator('[data-agent-session-list="recent"] button')
        expect(session_mode_label).to_have_attribute("lang", "zh-CN")
        expect(session_mode_trigger).to_contain_text("简体中文会话标题")
        expect(recent_session_option).to_have_attribute("lang", "zh-CN")
        expect(recent_session_option).to_contain_text("简体中文最近会话")

        page.evaluate(
            """() => {
                const host = document.createElement("div");
                host.id = "language-rendering-glyph-fixture";
                host.style.cssText = "font-family: sans-serif; font-size: 64px; line-height: 1;";
                const createSample = (id, language) => {
                    const sample = document.createElement("span");
                    sample.id = id;
                    sample.style.cssText = "display: inline-block; white-space: nowrap;";
                    if (language) sample.lang = language;
                    sample.textContent = "骨直着令";
                    host.append(sample);
                };
                createSample("language-rendering-glyph-target", "");
                createSample("language-rendering-glyph-simplified", "zh-CN");
                document.body.append(host);
            }""",
        )
        expect(page.locator("#language-rendering-glyph-target")).to_have_attribute("lang", "zh-CN")
        page.evaluate("() => document.fonts.ready")
        target_glyph = _decode_screenshot(
            page.locator("#language-rendering-glyph-target").screenshot()
        )
        simplified_glyph = _decode_screenshot(
            page.locator("#language-rendering-glyph-simplified").screenshot()
        )
        assert target_glyph.size == simplified_glyph.size
        assert ImageChops.difference(target_glyph, simplified_glyph).getbbox() is None

        for page_index, entry_point in enumerate(("/cache/x", "/browser", "/settings", "/agent")):
            page.goto(f"{sidebar_server_url}{entry_point}", wait_until="domcontentloaded")
            expect(page.locator('script[src*="language-rendering.js"]')).to_have_count(1)
            marker_id = f"language-rendering-entry-point-{page_index}"
            page.evaluate(
                """markerId => {
                    const button = document.createElement("button");
                    button.id = markerId;
                    button.textContent = "全局简体中文入口";
                    document.body.append(button);
                }""",
                marker_id,
            )
            expect(page.locator(f"#{marker_id}")).to_have_attribute("lang", "zh-CN")
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(("device_name", "width", "height"), OVERLAY_VIEWPORTS)
def test_overlay_sidebar_is_touch_safe_across_phone_and_ipad_portraits(
    disposable_browser: Browser,
    sidebar_server_url: str,
    device_name: str,
    width: int,
    height: int,
) -> None:
    page, context = _open_page(
        disposable_browser,
        sidebar_server_url,
        width,
        height,
        touch=True,
    )
    try:
        toggle = page.locator("#sidebar_toggle")
        sidebar = page.locator(".sidebar")
        backdrop = page.locator("#sidebar_backdrop")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(page.locator(".app-shell")).to_have_class(re.compile(r"\bis-sidebar-collapsed\b"))
        _assert_hidden_backdrop(page)
        assert sidebar.evaluate("element => getComputedStyle(element).pointerEvents") == "none"

        closed_geometry = toggle.evaluate(
            """toggle => {
                const rect = toggle.getBoundingClientRect();
                return {height: rect.height, left: rect.left, top: rect.top, width: rect.width};
            }"""
        )
        closed_theme_geometry = page.locator("#global_theme_toggle").evaluate(
            """theme => {
                const rect = theme.getBoundingClientRect();
                return {top: rect.top, right: rect.right};
            }"""
        )
        assert closed_geometry["width"] >= 44, device_name
        assert closed_geometry["height"] >= 44, device_name
        assert closed_geometry["left"] >= 0, device_name
        assert closed_geometry["top"] >= 0, device_name
        _assert_toggle_hit_target(page)
        assert toggle.evaluate(
            "element => element.parentElement?.classList.contains('page')"
        ), device_name

        _tap_toggle_center(page, toggle)
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(backdrop).to_be_visible()
        expect(backdrop).not_to_have_attribute("hidden", "")
        assert sidebar.evaluate("element => getComputedStyle(element).pointerEvents") == "auto"
        _assert_toggle_hit_target(page)
        page.wait_for_function(
            """() => {
                const dock = document.querySelector(".sidebar-dock");
                if (!(dock instanceof HTMLElement)) return false;
                const matrix = new DOMMatrix(getComputedStyle(dock).transform);
                return matrix.a > 0.999 && matrix.d > 0.999
                    && Number.parseFloat(getComputedStyle(dock).opacity) > 0.999;
            }"""
        )

        layout = page.evaluate(
            """() => {
                const toggle = document.querySelector("#sidebar_toggle").getBoundingClientRect();
                const title = document.querySelector(".sidebar .hero h1").getBoundingClientRect();
                const dock = document.querySelector(".sidebar-dock").getBoundingClientRect();
                const actions = document.querySelector(".global-quick-actions").getBoundingClientRect();
                const theme = document.querySelector("#global_theme_toggle").getBoundingClientRect();
                const sidebar = document.querySelector(".sidebar").getBoundingClientRect();
                const centerX = rect => rect.left + (rect.width / 2);
                const centerY = rect => rect.top + (rect.height / 2);
                const overlaps = (left, right) => !(
                    left.right <= right.left
                    || left.left >= right.right
                    || left.bottom <= right.top
                    || left.top >= right.bottom
                );
                return {
                    metricsFitWithoutOverlap: (() => {
                        const grid = document.querySelector('.cache-summary-metrics');
                        const bounds = grid.getBoundingClientRect();
                        const cards = [...grid.children].filter(node => !node.hidden)
                            .map(node => node.getBoundingClientRect());
                        return cards.length > 0 && cards.every((rect, index) =>
                            rect.width > 0 && rect.height > 0
                            && rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1
                            && rect.top >= bounds.top - 1 && rect.bottom <= bounds.bottom + 1
                            && cards.slice(index + 1).every(other => !overlaps(rect, other)));
                    })(),
                    dockOverlapsToggle: overlaps(dock, toggle),
                    actionsOverlapToggle: overlaps(actions, toggle),
                    titleOverlapsToggle: overlaps(title, toggle),
                    sidebarTopGap: sidebar.top,
                    sidebarLeftGap: sidebar.left,
                    sidebarBottomGap: window.innerHeight - sidebar.bottom,
                    dockCenterDelta: Math.abs(centerX(dock) - centerX(sidebar)),
                    dockBottomGap: sidebar.bottom - dock.bottom,
                    toggleRightGap: sidebar.right - toggle.right,
                    toggleTop: toggle.top,
                    themeTop: theme.top,
                    themeRightGap: window.innerWidth - theme.right,
                    titleCenterDelta: Math.abs(centerY(title) - centerY(toggle)),
                    horizontalOverflow: Math.max(
                        document.documentElement.scrollWidth,
                        document.body.scrollWidth,
                    ) > document.documentElement.clientWidth,
                    sidebarInsideViewport: sidebar.left >= 0
                        && sidebar.top >= 0
                        && sidebar.right <= window.innerWidth
                        && sidebar.bottom <= window.innerHeight,
                };
            }"""
        )
        assert layout["metricsFitWithoutOverlap"], device_name
        assert not layout["dockOverlapsToggle"], device_name
        assert not layout["actionsOverlapToggle"], device_name
        assert not layout["titleOverlapsToggle"], device_name
        assert not layout["horizontalOverflow"], device_name
        assert layout["sidebarInsideViewport"], device_name
        for key in ("sidebarTopGap", "sidebarLeftGap", "sidebarBottomGap", "dockBottomGap", "toggleRightGap"):
            assert abs(layout[key] - 10) <= 1, f"{device_name}: {key}={layout[key]}"
        assert layout["dockCenterDelta"] <= 1, device_name
        assert abs(layout["toggleTop"] - 20) <= 1, device_name
        assert abs(layout["themeTop"] - 20) <= 1, device_name
        assert abs(layout["themeRightGap"] - 20) <= 1, device_name
        assert layout["titleCenterDelta"] <= 1, device_name
        assert abs(closed_geometry["top"] - layout["toggleTop"]) <= 1, device_name
        assert abs(closed_theme_geometry["top"] - layout["themeTop"]) <= 1, device_name

        backdrop_hit = page.evaluate(
            """({x, y}) => document.elementFromPoint(x, y)?.id""",
            {"x": width - 2, "y": height / 2},
        )
        assert backdrop_hit == "sidebar_backdrop", device_name
        page.touchscreen.tap(width - 2, height / 2)
        expect(toggle).to_have_attribute("aria-expanded", "false")
        _assert_hidden_backdrop(page)

        _tap_toggle_center(page, toggle)
        expect(toggle).to_have_attribute("aria-expanded", "true")
        _assert_toggle_hit_target(page)
        _tap_toggle_center(page, toggle)
        expect(toggle).to_have_attribute("aria-expanded", "false")
        _assert_hidden_backdrop(page)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_ipad_touch_toggle_does_not_move_its_hit_target_during_overlay_motion(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    page, context = _open_page(
        disposable_browser,
        sidebar_server_url,
        820,
        1_180,
        touch=True,
        reduced_motion=None,
    )
    try:
        toggle = page.locator("#sidebar_toggle")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        transition = toggle.evaluate(
            """element => ({
                pointerCoarse: matchMedia('(pointer: coarse)').matches,
                transitionProperty: getComputedStyle(element).transitionProperty,
            })"""
        )
        assert transition["pointerCoarse"]
        assert "transform" not in {
            value.strip() for value in transition["transitionProperty"].split(",")
        }

        _tap_toggle_center(page, toggle)
        expect(toggle).to_have_attribute("aria-expanded", "true")
        _assert_toggle_hit_target(page)
        _tap_toggle_center(page, toggle)
        expect(toggle).to_have_attribute("aria-expanded", "false")
        _assert_hidden_backdrop(page)
        _assert_toggle_hit_target(page)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(("device_name", "width", "height"), DESKTOP_VIEWPORTS)
def test_desktop_widths_keep_the_sidebar_in_the_docked_contract(
    disposable_browser: Browser,
    sidebar_server_url: str,
    device_name: str,
    width: int,
    height: int,
) -> None:
    page, context = _open_page(
        disposable_browser,
        sidebar_server_url,
        width,
        height,
        touch=False,
    )
    try:
        toggle = page.locator("#sidebar_toggle")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        _assert_hidden_backdrop(page)
        assert page.locator(".sidebar").evaluate(
            "element => getComputedStyle(element).position"
        ) != "fixed", device_name
        assert not page.evaluate(
            "Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) "
            "> document.documentElement.clientWidth"
        ), device_name
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_sidebar_state_remains_consistent_across_overlay_transitions(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    page, context = _open_page(
        disposable_browser,
        sidebar_server_url,
        393,
        852,
        touch=True,
    )
    try:
        toggle = page.locator("#sidebar_toggle")
        backdrop = page.locator("#sidebar_backdrop")
        expect(toggle).to_have_attribute("aria-expanded", "false")

        _tap_toggle_center(page, toggle)
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(backdrop).to_be_visible()

        page.set_viewport_size({"width": 1_024, "height": 768})
        expect(toggle).to_have_attribute("aria-expanded", "true")
        _assert_hidden_backdrop(page)

        page.set_viewport_size({"width": 820, "height": 1_180})
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(backdrop).to_be_visible()
        _assert_toggle_hit_target(page)

        _tap_toggle_center(page, toggle)
        expect(toggle).to_have_attribute("aria-expanded", "false")
        _assert_hidden_backdrop(page)

        page.set_viewport_size({"width": 1_512, "height": 982})
        expect(toggle).to_have_attribute("aria-expanded", "false")
        _assert_hidden_backdrop(page)

        page.set_viewport_size({"width": 768, "height": 1_024})
        expect(toggle).to_have_attribute("aria-expanded", "false")
        _assert_hidden_backdrop(page)
        _assert_toggle_hit_target(page)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("platform", "platform_label", "session_url", "selected_model"),
    (
        ("gemini", "Gemini", "https://gemini.google.com/app/gemini-recent-session", "gemini-3.1-pro"),
        ("gemini", "Gemini", "https://gemini.google.com/app/gemini-recent-session", "gemini-3.8-flash"),
        ("grok", "Grok", "https://grok.com/c/grok-recent-session", "grok-build"),
    ),
)
def test_agent_recent_provider_sessions_submit_agentic_task_target(
    disposable_browser: Browser,
    sidebar_server_url: str,
    platform: str,
    platform_label: str,
    session_url: str,
    selected_model: str,
) -> None:
    """Verify Gemini and Grok serialize a selected recent session into Agent execution."""
    captured_ask_payloads: list[dict[str, str]] = []
    source_requests: list[str] = []
    history_requests: list[str] = []

    def agent_payload(selected_platform: str) -> dict[str, object]:
        return {
            "can_start": True,
            "runtime": {
                "ready": True,
                "host_operating_system": "macos",
                "message": "Computer Use is ready on this Mac.",
                "terminal_execution": {
                    "ready": True,
                    "status_label": "Granted",
                    "message": "Terminal execution is available.",
                },
            },
            "agent": {
                "running": False,
                "phase": "idle",
                "message": "Ready to use a signed-in Web AI session.",
                "prompt": "",
                "response": "",
                "response_html": "",
                "history": [],
                "activity": [],
                "conversation_url": "",
                "project_url": "",
                "session_title": "",
                "session_mode": "new",
                "platform": selected_platform,
                "model": "gemini-3.1-pro" if selected_platform == "gemini" else "grok-build",
                "finished_at": "",
            },
        }

    def fulfill_agent_status(route) -> None:
        route.fulfill(json=agent_payload(platform))

    def fulfill_browser_status(route) -> None:
        browser_id = "chrome" if "browser=chrome" in route.request.url else "edge"
        route.fulfill(
            json={
                "platform": platform,
                "browser": browser_id,
                "browser_label": browser_id.title(),
                "logged_in": True,
                "can_download": True,
                "account_name": f"{platform_label} account",
                "message": f"{browser_id.title()} is ready for {platform_label} Web.",
            }
        )

    def fulfill_preferences(route) -> None:
        payload = route.request.post_data_json or {}
        route.fulfill(json=agent_payload(str(payload.get("platform") or platform)))

    def fulfill_sources(route) -> None:
        source_requests.append(route.request.url)
        route.fulfill(
            json={
                "platform": platform,
                "browser_label": "Edge",
                "recent_sessions": [
                    {
                        "id": f"{platform}-recent-session-{index}",
                        "title": f"{platform_label} earlier session {index}",
                        "url": f"{session_url}-{index}",
                        "updated_at": "2026-08-14T04:00:00Z",
                    }
                    for index in range(19)
                ] + [
                    {
                        "id": f"{platform}-recent-session-tail",
                        "title": f"{platform_label} selected session",
                        "url": session_url,
                        "updated_at": "2026-08-14T04:00:00Z",
                    }
                ],
                "projects": [],
                "limit": 20,
            }
        )

    def fulfill_ask(route) -> None:
        captured_ask_payloads.append(route.request.post_data_json or {})
        route.fulfill(json=agent_payload(platform))

    def fulfill_grok_history(route) -> None:
        history_requests.append(route.request.url)
        route.fulfill(
            json={
                "conversation_url": session_url,
                "title": f"{platform_label} selected session",
                "history": [{
                    "prompt": "What changed?",
                    "response": "The selected recent session is now visible.",
                    "response_html": "<p>The selected recent session is now visible.</p>",
                }],
                "limit": 100,
            }
        )

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route("**/api/agent/preferences", fulfill_preferences)
    page.route("**/api/agent/sources**", fulfill_sources)
    page.route("**/api/agent/ask", fulfill_ask)
    if platform == "grok":
        page.route("**/api/agent/grok-session-history**", fulfill_grok_history)
    try:
        page.goto(f"{sidebar_server_url}/agent", wait_until="domcontentloaded")
        page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
        page.locator(
            f'.agent-platform-combobox [data-agent-combobox-option="{platform}"]'
        ).click()
        expect(page.get_by_role("button", name=f"Web service: {platform_label}", exact=True)).to_be_visible()
        expect(page.locator("[data-agent-provider-settings-label]")).to_have_text(
            f"Open {platform_label} settings"
        )

        recent_option = page.locator(
            f'[data-recent-conversation-url="{session_url}"]'
        )
        expect(recent_option).to_have_count(1)
        disclosure = page.locator("details[data-agent-execution-sessions]")
        summary = disclosure.locator("summary")
        expect(disclosure).to_have_attribute("open", "")
        summary.click()
        expect(recent_option).not_to_be_visible()
        summary.press("Enter")
        expect(recent_option).to_be_visible()
        # Browsing recent sessions without selecting one starts a new conversation.
        expect(page.locator("#agent_ask_button")).to_be_enabled()
        page.locator("#agent_prompt_input").fill("Start a fresh conversation")
        with page.expect_response(re.compile(r"/api/agent/ask$")):
            page.locator("#agent_ask_button").click()
        expect(page.locator("#agent_ask_button")).to_be_enabled()
        assert captured_ask_payloads[-1]["session_mode"] == "new"
        assert captured_ask_payloads[-1]["conversation_url"] == ""
        captured_ask_payloads.clear()
        if platform == "grok":
            recent_option.click()
            expect(page.locator("#agent_response_status")).to_have_attribute("data-status", "ready")
            expect(page.locator("#agent_response_question")).to_have_text("What changed?")
            expect(page.locator("[data-agent-response-answer-content]")).to_contain_text(
                "The selected recent session is now visible."
            )
            assert len(history_requests) == 1
        else:
            recent_option.click()

        expect(page.locator('[data-agent-prompt-session-mode]')).to_have_value("recent")
        expect(page.locator('[data-agent-prompt-conversation-url]')).to_have_value(session_url)
        expect(page.locator('[data-agent-prompt-session-title]')).to_have_value(
            f"{platform_label} selected session"
        )
        _assert_agent_session_source_menu_is_hit_testable(page)
        expect(page.locator("#agent_ask_button")).to_be_enabled()

        page.locator(".agent-model-combobox [data-agent-combobox-trigger]").click()
        page.locator(
            f'.agent-model-combobox [data-agent-combobox-option="{selected_model}"]'
        ).click()
        expect(page.locator("[data-agent-model-input]")).to_have_value(selected_model)

        page.locator('[data-agent-prompt-input]').fill(f"Inspect the {platform_label} task workspace.")
        with page.expect_response(re.compile(r"/api/agent/ask$")):
            page.locator("#agent_ask_button").click()
        assert len(captured_ask_payloads) == 1
        assert captured_ask_payloads[0]["model"] == selected_model
        assert captured_ask_payloads[0]["platform"] == platform
        assert captured_ask_payloads[0]["session_mode"] == "recent"
        assert captured_ask_payloads[0]["conversation_url"] == session_url
        assert captured_ask_payloads[0]["session_title"] == f"{platform_label} selected session"
        assert any(f"platform={platform}" in url for url in source_requests)
        for width in (390, 1024):
            page.set_viewport_size({"width": width, "height": 1164})
            if page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "false":
                page.locator("#sidebar_toggle").click()
            expect(summary).to_be_visible()
            summary.click()
            expect(recent_option).not_to_be_visible()
            expect(page.locator('[data-agent-prompt-conversation-url]')).to_have_value(session_url)
            summary.press("Space")
            expect(recent_option).to_be_visible()
            assert summary.evaluate("el => getComputedStyle(el, '::after').width") == "12px"
            assert summary.evaluate("el => getComputedStyle(el).fontSize") == "15px"
    finally:
        context.close()




@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("platform", "platform_label", "project_url"),
    (
        ("chatgpt", "ChatGPT", "https://chatgpt.com/g/g-p-chatgpt-project/project"),
        ("gemini", "Gemini", "https://gemini.google.com/notebook/gemini-project"),
        ("grok", "Grok", "https://grok.com/project/grok-project?tab=conversations"),
    ),
)
def test_agent_provider_projects_submit_agentic_task_target(
    disposable_browser: Browser,
    sidebar_server_url: str,
    platform: str,
    platform_label: str,
    project_url: str,
) -> None:
    """Verify provider-native project containers serialize as one Project choice."""
    captured_ask_payloads: list[dict[str, str]] = []
    source_requests: list[str] = []
    project_session_requests: list[str] = []

    def agent_payload(selected_platform: str) -> dict[str, object]:
        return {
            "can_start": True,
            "runtime": {
                "ready": True,
                "host_operating_system": "macos",
                "message": "Computer Use is ready on this Mac.",
                "terminal_execution": {
                    "ready": True,
                    "status_label": "Granted",
                    "message": "Terminal execution is available.",
                },
            },
            "agent": {
                "running": False,
                "phase": "idle",
                "message": "Ready to use a signed-in Web AI session.",
                "prompt": "",
                "response": "",
                "response_html": "",
                "history": [],
                "activity": [],
                "conversation_url": "",
                "project_url": "",
                "session_title": "",
                "session_mode": "new",
                "platform": selected_platform,
                "model": (
                    "gpt-5.6-sol"
                    if selected_platform == "chatgpt"
                    else "gemini-3.1-pro"
                    if selected_platform == "gemini"
                    else "grok-build"
                ),
                "finished_at": "",
            },
        }

    def fulfill_agent_status(route) -> None:
        route.fulfill(json=agent_payload(platform))

    def fulfill_browser_status(route) -> None:
        browser_id = "chrome" if "browser=chrome" in route.request.url else "edge"
        route.fulfill(
            json={
                "platform": platform,
                "browser": browser_id,
                "browser_label": browser_id.title(),
                "logged_in": True,
                "can_download": True,
                "account_name": f"{platform_label} account",
                "message": f"{browser_id.title()} is ready for {platform_label} Web.",
            }
        )

    def fulfill_preferences(route) -> None:
        payload = route.request.post_data_json or {}
        route.fulfill(json=agent_payload(str(payload.get("platform") or platform)))

    def fulfill_sources(route) -> None:
        source_requests.append(route.request.url)
        route.fulfill(
            json={
                "platform": platform,
                "browser_label": "Edge",
                "recent_sessions": [],
                "projects": [
                    {
                        "id": f"{platform}-project",
                        "title": f"{platform_label} project",
                        "url": project_url,
                        "updated_at": "2026-08-14T04:00:00Z",
                        **(
                            {"icon": "brain", "icon_color": "#3A83F7"}
                            if platform == "chatgpt"
                            else {}
                        ),
                    }
                ],
                "limit": 20,
            }
        )

    def fulfill_project_sessions(route) -> None:
        project_session_requests.append(route.request.url)
        route.fulfill(
            json={
                "platform": platform,
                "project_url": project_url,
                "sessions": [],
                "limit": 20,
            }
        )

    def fulfill_ask(route) -> None:
        captured_ask_payloads.append(route.request.post_data_json or {})
        route.fulfill(json=agent_payload(platform))

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 900},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route("**/api/agent/preferences", fulfill_preferences)
    page.route("**/api/agent/sources**", fulfill_sources)
    page.route("**/api/agent/project-sessions**", fulfill_project_sessions)
    page.route("**/api/agent/ask", fulfill_ask)
    try:
        page.goto(f"{sidebar_server_url}/agent", wait_until="domcontentloaded")
        page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
        page.locator(
            f'.agent-platform-combobox [data-agent-combobox-option="{platform}"]'
        ).click()
        expect(page.get_by_role("button", name=f"Web service: {platform_label}", exact=True)).to_be_visible()

        page.locator(".agent-session-mode-combobox [data-agent-combobox-trigger]").click()
        page.locator(
            '.agent-session-mode-combobox [data-agent-combobox-option="project"]'
        ).click()
        expect(
            page.locator(
                ".agent-session-mode-combobox [data-agent-combobox-selected-icon]"
            )
        ).to_have_attribute("src", re.compile(r"/static/images/folder\.fill\.svg$"))
        collection_label = "Notebooks" if platform == "gemini" else "Projects"
        expect(page.locator('.agent-session-mode-combobox [data-agent-combobox-selected-label]')).to_have_text(collection_label)
        expect(page.locator('[data-agent-project-field] > .field-label')).to_have_text(collection_label)
        project_option = page.locator(
            f'[data-agent-session-list="projects"] [data-agent-combobox-option="{project_url}"]'
        )
        expect(project_option).to_have_count(1)
        page.locator('[data-agent-session-list="projects"] [data-agent-combobox-trigger]').click()
        expect(project_option).to_be_visible()
        project_option.click()

        if platform == "chatgpt":
            project_icon_shells = page.locator(
                ".agent-session-mode-combobox .browser-picker-selected-icon-shell, "
                '[data-agent-session-list="projects"] .browser-picker-selected-icon-shell'
            )
            for width, height in ((1_280, 900), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                expect(project_icon_shells).to_have_count(2)
                expect(project_icon_shells.first).to_be_visible()
                expect(
                    page.locator(
                        '[data-agent-session-list="projects"] [data-agent-combobox-selected-icon]'
                    )
                ).to_have_attribute("src", re.compile(r"^data:image/svg\+xml"))
                expect(project_option).to_have_attribute(
                    "data-agent-combobox-icon-name", "brain"
                )
                icon_svg = project_option.locator("img").evaluate(
                    "image => decodeURIComponent(image.src.split(',')[1])"
                )
                assert '<path fill="currentColor"' in icon_svg
                assert '<text' not in icon_svg
                assert '#3A83F7' in icon_svg
                icon_centers = project_icon_shells.evaluate_all(
                    "nodes => nodes.map(node => { "
                    "const rect = node.getBoundingClientRect(); "
                    "return rect.left + rect.width / 2; "
                    "})"
                )
                assert max(icon_centers) - min(icon_centers) <= 1
            page.set_viewport_size({"width": 1_280, "height": 900})

        expect(page.locator('[data-agent-prompt-session-mode]')).to_have_value("project_new")
        expect(page.locator('[data-agent-prompt-project-url]')).to_have_value(project_url)
        expect(page.locator('[data-agent-prompt-conversation-url]')).to_have_value("")
        expect(page.locator("#agent_ask_button")).to_be_enabled()

        page.locator('[data-agent-prompt-input]').fill(f"Inspect the {platform_label} project workspace.")
        with page.expect_response(re.compile(r"/api/agent/ask$")):
            page.locator("#agent_ask_button").click()
        assert len(captured_ask_payloads) == 1
        assert captured_ask_payloads[0]["platform"] == platform
        assert captured_ask_payloads[0]["session_mode"] == "project_new"
        assert captured_ask_payloads[0]["project_url"] == project_url
        assert captured_ask_payloads[0]["conversation_url"] == ""
        assert any(f"platform={platform}" in url for url in source_requests)
        assert any("project_url=" in url for url in project_session_requests)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_project_session_selection_loads_grok_response_immediately(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Show loading immediately, then render the selected Grok project session."""
    project_url = "https://grok.com/project/grok-project?tab=conversations"
    session_url = "https://grok.com/project/grok-project?chat=grok-session"
    history_requests: list[str] = []
    project_session_requests: list[str] = []

    def agent_payload() -> dict[str, object]:
        return {
            "can_start": True,
            "runtime": {
                "ready": True,
                "host_operating_system": "macos",
                "message": "Computer Use is ready on this Mac.",
                "terminal_execution": {
                    "ready": True,
                    "status_label": "Granted",
                    "message": "Terminal execution is available.",
                },
            },
            "agent": {
                "running": False,
                "phase": "idle",
                "message": "Ready to use a signed-in Web AI session.",
                "prompt": "",
                "response": "",
                "response_html": "",
                "history": [],
                "activity": [],
                "conversation_url": "",
                "project_url": "",
                "session_title": "",
                "session_mode": "new",
                "platform": "grok",
                "model": "grok-build",
                "finished_at": "",
            },
        }

    def fulfill_agent_status(route) -> None:
        route.fulfill(json=agent_payload())

    def fulfill_browser_status(route) -> None:
        route.fulfill(
            json={
                "platform": "grok",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "Grok account",
                "message": "Edge is ready for Grok Web.",
            }
        )

    def fulfill_preferences(route) -> None:
        route.fulfill(json=agent_payload())

    def fulfill_sources(route) -> None:
        route.fulfill(
            json={
                "platform": "grok",
                "browser_label": "Edge",
                "recent_sessions": [],
                "projects": [{
                    "id": "grok-project",
                    "title": "Grok project",
                    "url": project_url,
                    "updated_at": "2026-09-02T01:00:00Z",
                }],
                "limit": 20,
            }
        )

    def fulfill_project_sessions(route) -> None:
        project_session_requests.append(route.request.url)
        route.fulfill(
            json={
                "platform": "grok",
                "project_url": project_url,
                "sessions": [{
                    "id": "grok-session",
                    "title": "Renamed project session",
                    "url": session_url,
                    "updated_at": "2026-09-02T01:05:00Z",
                }],
                "limit": 20,
            }
        )

    def fulfill_history(route) -> None:
        history_requests.append(route.request.url)
        route.fulfill(
            json={
                "conversation_url": session_url,
                "title": "Renamed project session",
                "history": [{
                    "prompt": "What changed?",
                    "response": "The selected session is now visible.",
                    "response_html": "<p>The selected session is now visible.</p>",
                    "started_at": "2026-09-02T01:00:00Z",
                    "finished_at": "2026-09-02T01:00:02Z",
                }],
                "limit": 100,
            }
        )

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 900},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route("**/api/agent/preferences", fulfill_preferences)
    page.route("**/api/agent/sources**", fulfill_sources)
    page.route("**/api/agent/project-sessions**", fulfill_project_sessions)
    page.route("**/api/agent/grok-session-history**", fulfill_history)
    try:
        page.goto(f"{sidebar_server_url}/agent", wait_until="domcontentloaded")
        page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
        page.locator('.agent-platform-combobox [data-agent-combobox-option="grok"]').click()
        page.locator(".agent-session-mode-combobox [data-agent-combobox-trigger]").click()
        page.locator('.agent-session-mode-combobox [data-agent-combobox-option="project"]').click()

        project_option = page.locator(
            f'[data-agent-session-list="projects"] [data-agent-combobox-option="{project_url}"]'
        )
        expect(project_option).to_have_count(1)
        page.locator('[data-agent-session-list="projects"] [data-agent-combobox-trigger]').click()
        project_option.click()
        session_option = page.locator(
            f'[data-agent-execution-session-list] [data-recent-conversation-url="{session_url}"]'
        )
        expect(session_option).to_have_count(1)
        expect(page.locator('[data-agent-project-session-field]')).to_have_count(0)
        session_option.click()
        expect(page.locator("#agent_response_status")).to_have_attribute("data-status", "ready")
        expect(page.locator("#agent_response_question")).to_have_text("What changed?")
        expect(page.locator("[data-agent-response-answer-content]")).to_contain_text(
            "The selected session is now visible."
        )
        assert len(history_requests) == 1
        assert "conversation_url=" in history_requests[0]

        page.reload(wait_until="domcontentloaded")
        expect(page.locator("input[data-agent-session-mode]")).to_have_value("project")
        expect(page.locator("[data-agent-project-url]")).to_have_value(project_url)
        expect(page.locator('input[name="conversation_url"]')).to_have_value(session_url)
        expect(page.locator("[data-agent-session-list=\"projects\"] [data-agent-combobox-selected-label]")).to_have_text(
            "Grok project"
        )
        expect(session_option).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#agent_response_question")).to_have_text("What changed?")
        assert len(project_session_requests) == 2
        assert all("refresh=1" not in url for url in project_session_requests)
        assert len(history_requests) == 2
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_connection_selection_survives_cache_navigation(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/agent",
        1_280,
        900,
        touch=False,
    )
    try:
        browser_trigger = page.get_by_role("button", name="Browser: Edge", exact=True)
        assert browser_trigger.evaluate(
            "element => element.getBoundingClientRect().height"
        ) == 36
        browser_trigger.click()
        with page.expect_response(re.compile(r"/api/agent/preferences$")):
            page.get_by_role("option", name="Chrome", exact=True).click()
        expect(page.locator('#agent_runtime_form input[name="browser"]')).to_have_value(
            "chrome"
        )
        expect(page.get_by_role("button", name="Browser: Chrome", exact=True)).to_be_visible()

        page.get_by_role("link", name="Cache", exact=True).click()
        expect(page).to_have_url(re.compile(r"/cache/chatgpt$"))
        expect(page.locator('[data-dock-section="cache"]')).to_have_attribute("aria-current", "page")
        expect(page.locator('[data-dock-section="agent"]')).not_to_have_attribute("aria-current", "page")
        page.get_by_role("link", name="Agent", exact=True).click()

        expect(page.get_by_role("button", name="Browser: Chrome", exact=True)).to_be_visible()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_browser_text_media_switch_defaults_to_text_and_remembers_selection(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/browser",
        1_280,
        900,
        touch=False,
    )
    try:
        text_input = page.locator("#browser_view_text")
        media_input = page.locator("#browser_view_media")
        expect(text_input).to_be_checked()
        expect(media_input).not_to_be_checked()
        expect(page.locator(".browser-content-mode-control")).to_have_attribute(
            "data-segmented-active-index",
            "0",
        )

        page.locator('label[for="browser_view_media"]').click()
        expect(page).to_have_url(re.compile(r"/browser\?view=media"))
        expect(media_input).to_be_checked()
        expect(page.locator(".browser-content-mode-control")).to_have_attribute(
            "data-segmented-active-index",
            "1",
        )

        page.goto(f"{sidebar_server_url}/agent")
        page.goto(f"{sidebar_server_url}/browser")
        expect(page).to_have_url(re.compile(r"/browser\?view=media"))
        expect(page.locator("#browser_view_media")).to_be_checked()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("source_key", ("chatgpt", "grok", "gemini", "claude"))
@pytest.mark.parametrize("width", (1_280, 390))
def test_cache_sidebar_text_media_switcher_defaults_to_text(
    disposable_browser: Browser,
    sidebar_server_url: str,
    source_key: str,
    width: int,
) -> None:
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/{source_key}",
        width,
        900,
        touch=False,
    )
    try:
        if width < 901:
            page.locator("#sidebar_toggle").click()
        mode_control = page.locator("[data-cache-content-mode]")
        text_option = page.locator('[data-cache-content-mode-option="text"]')
        media_option = page.locator('[data-cache-content-mode-option="media"]')
        expect(mode_control).to_be_visible()
        expect(mode_control).to_have_attribute("data-segmented-active-index", "0")
        expect(text_option).to_have_attribute("aria-checked", "true")
        expect(media_option).to_have_attribute("aria-checked", "false")
        source_options = page.locator("[data-cache-source-switcher-option]")
        x_source_option = page.locator('[data-cache-source-switcher-option="x"]')
        expect(x_source_option).to_be_hidden()
        assert source_options.evaluate_all(
            "elements => elements.filter(element => !element.hidden).map(element => element.dataset.cacheSourceSwitcherOption)"
        ) == ["chatgpt", "claude", "gemini", "grok", "zhihu"]
        if source_key == "chatgpt":
            expect(page.locator("#start_form_chatgpt > label")).to_have_count(0)
            expect(page.locator("[data-chatgpt-media-config]")).to_be_hidden()
            expect(page.locator('[name="chatgpt_project_url"]')).to_be_disabled()

        media_option.click()
        expect(page).to_have_url(re.compile(rf"/cache/{source_key}$"))
        expect(page.locator('[data-cache-content-mode-option="media"]')).to_have_attribute(
            "aria-checked",
            "true",
        )
        expect(page.locator("[data-cache-content-mode]")).to_have_attribute(
            "data-segmented-active-index",
            "1",
        )
        assert x_source_option.evaluate("element => !element.hidden")
        assert source_options.evaluate_all(
            "elements => elements.filter(element => !element.hidden).map(element => element.dataset.cacheSourceSwitcherOption)"
        ) == ["chatgpt", "claude", "gemini", "grok", "x", "zhihu"]
        if source_key == "chatgpt":
            expect(page.locator("#start_form_chatgpt > label")).to_have_count(0)
            expect(page.locator("[data-chatgpt-media-config]")).to_be_visible()
            expect(page.locator('[name="chatgpt_project_url"]')).to_be_enabled()

        page.locator('[data-cache-content-mode-option="text"]').click()
        if source_key == "chatgpt":
            expect(page).to_have_url(re.compile(r"/cache/chatgpt$"))
            expect(page.locator("[data-chatgpt-media-config]")).to_be_hidden()
            expect(page.locator("[data-chatgpt-content-mode-input]")).to_have_value("text")
            expect(page.locator('[name="chatgpt_project_url"]')).to_be_disabled()
        else:
            expect(page).to_have_url(re.compile(rf"/cache/{source_key}$"))
            expect(page.locator('#start_button')).to_have_text("Start")
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_280, 900, False), (390, 844, True)),
)
def test_chatgpt_media_uses_agent_recent_project_picker(
    disposable_browser: Browser,
    sidebar_server_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    """Choose a ChatGPT project from the live Agent source catalog contract."""
    project_url = "https://chatgpt.com/g/g-p-demo-project/project"
    catalog_payload = {
        "platform": "chatgpt",
        "browser_label": "Edge",
        "recent_sessions": [],
        "projects": [
            {
                "id": "demo-project",
                "title": "Demo project",
                "icon": "currency-dollar",
                "icon_color": "#53B559",
                "url": project_url,
                "updated_at": "2026-09-02T00:00:00Z",
            },
        ],
        "limit": 20,
    }
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
    )
    page = context.new_page()
    catalog_requests = []
    def catalog_route(route):
        catalog_requests.append(route.request.url)
        route.fulfill(json=catalog_payload)
    page.route("**/api/agent/chatgpt-sources**", catalog_route)
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt", wait_until="domcontentloaded")
        expect(page.locator('input[name="chatgpt_project_url"][type="url"]')).to_have_count(0)
        if touch:
            page.locator("#sidebar_toggle").click()

        page.locator('[data-cache-content-mode-option="media"]').click()
        picker = page.locator("[data-chatgpt-project-picker]")
        trigger = page.locator("[data-chatgpt-project-trigger]")
        project_option = page.locator(
            f'[data-chatgpt-project-option][data-chatgpt-project-url="{project_url}"]'
        )
        expect(picker).to_be_visible()
        expect(project_option).to_have_count(1)

        trigger.click()
        expect(page.locator("[data-chatgpt-project-menu]")).to_be_visible()
        project_option.click()
        expect(page.locator('[name="chatgpt_project_url"]')).to_have_value(project_url)
        expect(page.locator('[name="chatgpt_project_name"]')).to_have_value("Demo project")
        expect(trigger).to_have_text("Demo project")
        expect(project_option).to_have_attribute("aria-selected", "true")
        icon = trigger.locator("[data-chatgpt-project-selected-icon]")
        expect(icon).to_be_visible()
        assert "%2353B559" in icon.get_attribute("src")
        assert icon.get_attribute("src") == project_option.locator("img").get_attribute("src")
        trigger.click()
        trigger.click()
        assert len(catalog_requests) == 1
        assert "refresh=1" not in catalog_requests[0]
        expect(page.locator("#chatgpt_project_url_help")).to_have_count(0)
        expect(page.locator('[name="chatgpt_scan_wait_seconds"]')).to_have_count(0)
        expect(page.locator('#overview > .workspace-kicker')).to_have_count(0)
        expect(page.locator("#activity")).to_have_count(0)
        expect(page.locator('[aria-label="ChatGPT sync notice"]')).to_have_count(0)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_gemini_cache_source_switcher_opens_chatgpt_cache_page(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/gemini",
        1_512,
        982,
        touch=False,
    )
    try:
        page.locator("[data-cache-source-switcher-trigger]").click()
        page.locator('[data-cache-source-switcher-option="chatgpt"]').click()
        expect(page).to_have_url(re.compile(r"/cache/chatgpt$"))
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("device_name", "width", "height", "touch"),
    (
        ("iPhone 15 Pro", 393, 852, True),
        ("wide desktop", 1_512, 982, False),
    ),
)
def test_gemini_source_mark_preserves_full_color_at_target_viewports(
    disposable_browser: Browser,
    sidebar_server_url: str,
    device_name: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    page, context = _open_page(
        disposable_browser,
        f"{sidebar_server_url}/cache/gemini",
        width,
        height,
        touch=touch,
    )
    try:
        mark = page.locator(
            ".cache-source-switcher-trigger .cache-source-mark.is-full-color"
        )
        expect(mark).to_have_count(1)
        rendering = mark.evaluate(
            """element => {
                const before = getComputedStyle(element, "::before");
                return {
                    backgroundImage: before.backgroundImage,
                    height: before.height,
                    maskImage: before.maskImage,
                    width: before.width,
                };
            }"""
        )
        assert "Google_Gemini_logo_2025_symbol.svg" in rendering["backgroundImage"], device_name
        assert rendering["maskImage"] == "none", device_name
        assert rendering["width"] == rendering["height"] == "16px", device_name
    finally:
        context.close()


FINISHED_SNAPSHOT_URL = "https://chatgpt.com/c/6a8d4fce-d1e8-83ee-9996-68e9ef114ef0"
AGENTIC_TROUBLESHOOTING_URL = "https://chatgpt.com/c/6a8d310f-7af4-83e8-acb4-6e3e825e984f"


def _finished_chatgpt_agent_payload() -> dict[str, object]:
    return {
        "can_start": True,
        "runtime": {
            "ready": True,
            "host_operating_system": "macos",
            "message": "Computer Use is ready on this Mac.",
            "terminal_execution": {
                "ready": True,
                "status_label": "Granted",
                "message": "Terminal execution is available.",
            },
        },
        "agent": {
            "running": False,
            "paused": False,
            "phase": "finished",
            "message": "GPT-5.6 Sol completed the project task after local bodycheck.",
            "prompt": "",
            "response": "Read-only inspection finished.",
            "response_html": "<p>Read-only inspection finished.</p>",
            "history": [],
            "activity": [],
            "conversation_url": FINISHED_SNAPSHOT_URL,
            "project_url": "",
            "session_title": "Reused model verification",
            "session_mode": "recent",
            "platform": "chatgpt",
            "browser": "edge",
            "workspace_path": load_computer_use_settings().workspace_path,
            "model": "gpt-5.6-sol",
            "model_verified": True,
            "actual_model": "GPT-5.6 Sol",
            "bodycheck_passed": True,
            "started_at": "2026-08-25T09:02:38Z",
            "finished_at": "2026-08-25T09:03:57Z",
        },
    }


def _chatgpt_catalog_sessions(*sessions: dict[str, str]) -> dict[str, object]:
    return {
        "platform": "chatgpt",
        "browser_label": "Edge",
        "recent_sessions": list(sessions),
        "projects": [],
        "limit": 20,
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    "unsupported_copy",
    (
        "Gemini isn’t currently supported in your country. Stay tuned!",
        "Gemini 目前不支持你所在的地区。敬请期待！",
        "Gemini 目前不支援你所在的地區。敬請期待！",
    ),
)
def test_gemini_session_dom_marks_a_signed_in_region_unavailable_page(
    disposable_browser: Browser,
    unsupported_copy: str,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            f"""
            <button aria-label="Google Account: Demo account">Account</button>
            <main>{unsupported_copy}</main>
            """
        )

        snapshot = inspect_gemini_session(page)

        assert snapshot["accountLabel"] == "Google Account: Demo account"
        assert snapshot["signedOut"] is False
        assert snapshot["unsupportedRegion"] is True
        assert snapshot["hasComposer"] is False
    finally:
        context.close()


@pytest.mark.integration
def test_gemini_session_dom_does_not_treat_conversation_copy_as_a_region_failure(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button aria-label="Google Account: Demo account">Account</button>
            <main>Gemini 目前不支持你所在的地区。敬请期待！</main>
            <textarea placeholder="Ask Gemini"></textarea>
            """
        )

        snapshot = inspect_gemini_session(page)

        assert snapshot["hasComposer"] is True
        assert snapshot["unsupportedRegion"] is False
    finally:
        context.close()


@pytest.mark.integration
def test_gemini_session_dom_rejects_an_anonymous_composer_shell(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <header><button>Sign in</button></header>
            <nav><a href="https://gemini.google.com/app/anonymous-shell">Recent activity</a></nav>
            <textarea placeholder="Ask Gemini"></textarea>
            """
        )

        snapshot = inspect_gemini_session(page)

        assert snapshot["conversationLinks"] == 1
        assert snapshot["hasComposer"] is True
        assert snapshot["hasAuthAction"] is True
        assert snapshot["signedOut"] is True
    finally:
        context.close()


@pytest.mark.integration
def test_gemini_session_dom_ignores_a_conversation_sign_in_decoy(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button aria-label="Google Account: Demo account">Account</button>
            <model-response><button>Sign in</button></model-response>
            <textarea placeholder="Ask Gemini"></textarea>
            """
        )

        snapshot = inspect_gemini_session(page)

        assert snapshot["hasAuthAction"] is False
        assert snapshot["signedOut"] is False
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize("width", (1_138, 390))
def test_gemini_model_picker_keeps_both_versions_selectable(
    disposable_browser: Browser, sidebar_server_url: str, width: int,
) -> None:
    """Exercise the shared model picker with Gemini at desktop and narrow widths."""
    payload = _finished_chatgpt_agent_payload()
    payload["agent"].update(
        running=False, phase="idle", platform="gemini", model="gemini-3.1-pro",
        history=[], activity=[], response="", response_html="", finished_at="",
    )
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 959}, reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", lambda route: route.fulfill(json=payload))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "can_download": True, "logged_in": True, "browser": "edge", "platform": "gemini",
    }))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json={
        "platform": "gemini", "recent_sessions": [], "projects": [],
    }))
    page.route("**/api/agent/preferences", lambda route: route.fulfill(json=payload))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/gemini")
        trigger = page.locator(".agent-model-combobox [data-agent-combobox-trigger]")
        for model, label in (("gemini-3.8-flash", "3.8 Flash"), ("gemini-3.1-pro", "3.1 Pro")):
            trigger.click()
            option = page.locator(f'.agent-model-combobox [data-agent-combobox-option="{model}"]')
            expect(option).to_be_visible()
            option.click()
            expect(page.locator("[data-agent-model-input]")).to_have_value(model)
            expect(trigger).to_contain_text(label)
            expect(trigger).to_have_attribute("aria-expanded", "false")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    (
        "primary_text",
        "primary_style",
        "sublabel_text",
        "add_selected_class",
        "marker_style",
        "nested_popup",
        "expected",
        "expected_option_clicks",
    ),
    (
        pytest.param(
            "3.1 Pro", "", "Advanced reasoning", True, "", False, True, 1, id="exact-proof"
        ),
        pytest.param(
            "3.1 Pro", "", "Advanced reasoning", True, None, False, False, 1, id="class-only"
        ),
        pytest.param(
            "3.1 Pro", "", "Advanced reasoning", False, "", False, False, 1, id="marker-only"
        ),
        pytest.param(
            "3.1 Pro", "", "Advanced reasoning", True, "opacity: 0", False, False, 1, id="hidden-marker"
        ),
        pytest.param(
            "", "", "3.1 Pro", True, "", False, False, 0, id="subtitle-only"
        ),
        pytest.param(
            "3.1 Pro", "display: none", "Advanced reasoning", True, "", False, False, 0, id="hidden-primary"
        ),
        pytest.param(
            "Mode picker 3.1 Pro", "", "Advanced reasoning", True, "", False, False, 0, id="wrapper-primary"
        ),
        pytest.param(
            "Gemini 3.1 Pro", "", "Advanced reasoning", True, "", False, False, 0, id="full-brand-primary"
        ),
        pytest.param(
            "3.1 Pro", "", "Advanced reasoning", True, "", True, False, 0, id="nested-popup"
        ),
    ),
)
@pytest.mark.parametrize("model_label", ("3.1 Pro", "3.8 Flash"))
@pytest.mark.parametrize("selected_label", ("Selected", "已选中", "已選取"))
def test_gemini_model_dom_selection_requires_exact_controlled_selected_proof(
    disposable_browser: Browser,
    primary_text: str,
    primary_style: str,
    sublabel_text: str,
    add_selected_class: bool,
    marker_style: str | None,
    nested_popup: bool,
    expected: bool,
    expected_option_clicks: int,
    model_label: str,
    selected_label: str,
) -> None:
    primary_text = primary_text.replace("3.1 Pro", model_label)
    sublabel_text = sublabel_text.replace("3.1 Pro", model_label)
    model = "gemini-" + model_label.lower().replace(" ", "-")
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        option_markup = f"""
            <button id="pro-option" role="menuitem">
                <span class="label" style="{primary_style}">{primary_text}</span>
                <span class="sublabel">{sublabel_text}</span>
            </button>
        """
        if nested_popup:
            option_markup = f'<div role="menu">{option_markup}</div>'
        page.set_content(
            f"""
            <button
                id="mode-picker"
                aria-label="Open mode picker, currently Flash"
                aria-haspopup="true"
                aria-expanded="false"
                aria-controls="mode-menu"
            >Flash</button>
            <div id="mode-menu" role="menu" hidden>
                <button id="flash-option" role="menuitem">
                    <span class="label">3.7 Flash</span>
                    <span class="sublabel">All-around help</span>
                </button>
                {option_markup}
            </div>
            <script>
                window.selectionAudit = {{triggerClicks: 0, optionClicks: 0}};
                const trigger = document.querySelector('#mode-picker');
                const menu = document.querySelector('#mode-menu');
                const option = document.querySelector('#pro-option');
                trigger.addEventListener('click', () => {{
                    window.selectionAudit.triggerClicks += 1;
                    const opening = menu.hidden;
                    menu.hidden = !opening;
                    trigger.setAttribute('aria-expanded', String(opening));
                }});
                option.addEventListener('click', () => {{
                    window.selectionAudit.optionClicks += 1;
                    trigger.setAttribute('aria-label', 'Open mode picker, currently Pro');
                    trigger.textContent = 'Pro';
                    menu.hidden = true;
                    trigger.setAttribute('aria-expanded', 'false');
                    if ({str(add_selected_class).lower()}) {{
                        option.classList.add('selected');
                    }}
                    if ({str(marker_style is not None).lower()}) {{
                        const marker = document.createElement('span');
                        marker.setAttribute('aria-label', {json.dumps(selected_label)});
                        marker.setAttribute('style', {json.dumps(marker_style or '')});
                        marker.textContent = '✓';
                        option.prepend(marker);
                    }}
                }});
            </script>
            """
        )

        assert (
            _select_web_model(page, "chromium", "gemini", model)
            is expected
        )
        assert page.evaluate("window.selectionAudit.optionClicks") == expected_option_clicks
        assert page.locator("#mode-picker").get_attribute("aria-expanded") == "false"
        assert page.locator("#mode-menu").is_hidden()
    finally:
        context.close()


@pytest.mark.integration
def test_gemini_model_dom_selection_rejects_the_anonymous_model_menu(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button
                id="mode-picker"
                aria-label="Open mode picker, currently Flash-Lite"
                aria-haspopup="true"
                aria-expanded="false"
                aria-controls="mode-menu"
            >Flash-Lite</button>
            <div id="mode-menu" role="menu" hidden>
                <gem-menu-item class="selected" role="menuitem">
                    <gem-icon aria-label="Selected"></gem-icon>
                    <span class="label">3.5 Flash-Lite</span>
                </gem-menu-item>
                <gem-menu-item id="pro-option" role="menuitem">
                    <span class="label">3.1 Pro</span>
                    <span class="sublabel">Advanced reasoning</span>
                </gem-menu-item>
                <gem-menu-item role="menuitem">
                    <span class="label">Sign in for all models</span>
                </gem-menu-item>
            </div>
            <script>
                window.selectionAudit = {triggerClicks: 0, optionClicks: 0};
                const trigger = document.querySelector('#mode-picker');
                const menu = document.querySelector('#mode-menu');
                trigger.addEventListener('click', () => {
                    window.selectionAudit.triggerClicks += 1;
                    const opening = menu.hidden;
                    menu.hidden = !opening;
                    trigger.setAttribute('aria-expanded', String(opening));
                });
                document.querySelector('#pro-option').addEventListener('click', () => {
                    window.selectionAudit.optionClicks += 1;
                });
            </script>
            """
        )
        observation: dict[str, object] = {}

        assert (
            _select_web_model(
                page,
                "chromium",
                "gemini",
                "gemini-3.1-pro",
                observation,
            )
            is False
        )
        assert observation["reason"] == "signed-out"
        assert page.evaluate("window.selectionAudit.optionClicks") == 0
        assert page.locator("#mode-picker").get_attribute("aria-expanded") == "false"
        assert page.locator("#mode-menu").is_hidden()
    finally:
        context.close()


@pytest.mark.integration
def test_gemini_model_dom_selection_waits_for_delayed_hydration(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <textarea placeholder="Ask Gemini"></textarea>
            <button aria-label="Navigation">Navigation</button>
            <script>
                window.selectionAudit = {
                    mounted: false,
                    triggerClicks: 0,
                    optionClicks: 0,
                };
                const mountModelControl = () => {
                    const trigger = document.createElement('button');
                    trigger.id = 'mode-picker';
                    trigger.setAttribute('aria-label', 'Open mode picker, currently Flash');
                    trigger.setAttribute('aria-haspopup', 'true');
                    trigger.setAttribute('aria-expanded', 'false');
                    trigger.setAttribute('aria-controls', 'mode-menu');
                    trigger.textContent = 'Flash';

                    const menu = document.createElement('div');
                    menu.id = 'mode-menu';
                    menu.setAttribute('role', 'menu');
                    menu.hidden = true;

                    const option = document.createElement('button');
                    option.id = 'pro-option';
                    option.setAttribute('role', 'menuitem');
                    option.innerHTML = `
                        <span class="label">3.1 Pro</span>
                        <span class="sublabel">Advanced reasoning</span>
                    `;
                    menu.append(option);

                    trigger.addEventListener('click', () => {
                        window.selectionAudit.triggerClicks += 1;
                        const opening = menu.hidden;
                        menu.hidden = !opening;
                        trigger.setAttribute('aria-expanded', String(opening));
                    });
                    option.addEventListener('click', () => {
                        window.selectionAudit.optionClicks += 1;
                        option.classList.add('selected');
                        const marker = document.createElement('span');
                        marker.setAttribute('aria-label', 'Selected');
                        marker.textContent = '✓';
                        option.prepend(marker);
                        trigger.setAttribute('aria-label', 'Open mode picker, currently Pro');
                        trigger.textContent = 'Pro';
                        menu.hidden = true;
                        trigger.setAttribute('aria-expanded', 'false');
                    });

                    document.body.append(trigger, menu);
                    window.selectionAudit.mounted = true;
                };
                window.setTimeout(mountModelControl, 500);
            </script>
            """
        )

        observation: dict[str, object] = {}
        assert (
            _select_web_model(
                page,
                "chromium",
                "gemini",
                "gemini-3.1-pro",
                observation,
            )
            is True
        )
        assert page.evaluate("window.selectionAudit") == {
            "mounted": True,
            "triggerClicks": 3,
            "optionClicks": 1,
        }
        assert observation["observed"] == "3.1 pro"
        assert page.locator("#mode-picker").get_attribute("aria-expanded") == "false"
        assert page.locator("#mode-menu").is_hidden()
    finally:
        context.close()


@pytest.mark.integration
def test_web_model_failure_diagnostic_excludes_dom_text(
    disposable_browser: Browser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.computer_use_agent as computer_use_agent

    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <title>Confidential flight acquisition</title>
            <textarea placeholder="Ask Gemini"></textarea>
            <button>Project confidential-alpha</button>
            """
        )
        monkeypatch.setattr(computer_use_agent, "WEB_MODEL_CONTROL_WAIT_ATTEMPTS", 1)
        monkeypatch.setattr(computer_use_agent, "WEB_MODEL_CONTROL_POLL_SECONDS", 0)
        observation: dict[str, object] = {}

        assert (
            _select_web_model(
                page,
                "chromium",
                "gemini",
                "gemini-3.1-pro",
                observation,
            )
            is False
        )
        serialized = json.dumps(observation, ensure_ascii=False)
        assert "Confidential" not in serialized
        assert "confidential" not in serialized
        assert observation["visible_buttons"] == []
        assert observation["diagnostic"] == {
            "ready_state": "complete",
            "visible_button_count": 1,
            "visible_composer_count": 1,
            "semantic_trigger_count": 0,
            "visible_menu_count": 0,
        }
    finally:
        context.close()


@pytest.mark.integration
def test_gemini_model_dom_selection_accepts_a_remounted_controlled_menu(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button
                id="mode-picker"
                aria-label="Open mode picker, currently Flash"
                aria-haspopup="true"
                aria-expanded="false"
                aria-controls="mode-menu"
            >Flash</button>
            <script>
                window.selectionAudit = {mounts: 0, optionClicks: 0};
                let selected = false;
                const trigger = document.querySelector('#mode-picker');
                const removeMenu = () => {
                    document.querySelector('#mode-menu')?.remove();
                    trigger.setAttribute('aria-expanded', 'false');
                };
                const mountMenu = () => {
                    window.selectionAudit.mounts += 1;
                    const menu = document.createElement('div');
                    menu.id = 'mode-menu';
                    menu.setAttribute('role', 'menu');
                    const option = document.createElement('button');
                    option.id = 'pro-option';
                    option.setAttribute('role', 'menuitem');
                    if (selected) option.classList.add('selected');
                    option.innerHTML = `
                        ${selected ? '<span aria-label="Selected">✓</span>' : ''}
                        <span class="label">3.1 Pro</span>
                        <span class="sublabel">Advanced reasoning</span>
                    `;
                    option.addEventListener('click', () => {
                        window.selectionAudit.optionClicks += 1;
                        selected = true;
                        trigger.setAttribute('aria-label', 'Open mode picker, currently Pro');
                        trigger.textContent = 'Pro';
                        removeMenu();
                    });
                    menu.append(option);
                    document.body.append(menu);
                    trigger.setAttribute('aria-expanded', 'true');
                };
                trigger.addEventListener('click', () => {
                    if (document.querySelector('#mode-menu')) removeMenu();
                    else mountMenu();
                });
            </script>
            """
        )

        assert _select_web_model(page, "chromium", "gemini", "gemini-3.1-pro") is True
        assert page.evaluate("window.selectionAudit") == {"mounts": 2, "optionClicks": 1}
        assert page.locator("#mode-menu").count() == 0
        assert page.locator("#mode-picker").get_attribute("aria-expanded") == "false"
    finally:
        context.close()


@pytest.mark.integration
def test_gemini_model_dom_selection_accepts_a_remounted_controlled_trigger(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <div id="trigger-host"></div>
            <script>
                window.selectionAudit = {
                    triggerMounts: 0,
                    triggerClicks: 0,
                    menuMounts: 0,
                    optionClicks: 0,
                };
                let selected = false;
                const host = document.querySelector('#trigger-host');
                const removeMenu = () => document.querySelector('#mode-menu')?.remove();
                const mountTrigger = (expanded) => {
                    window.selectionAudit.triggerMounts += 1;
                    const trigger = document.createElement('button');
                    trigger.id = 'mode-picker';
                    trigger.setAttribute(
                        'aria-label',
                        `Open mode picker, currently ${selected ? 'Pro' : 'Flash'}`
                    );
                    trigger.setAttribute('aria-haspopup', 'true');
                    trigger.setAttribute('aria-expanded', String(expanded));
                    trigger.setAttribute('aria-controls', 'mode-menu');
                    trigger.textContent = selected ? 'Pro' : 'Flash';
                    trigger.addEventListener('click', () => {
                        window.selectionAudit.triggerClicks += 1;
                        if (document.querySelector('#mode-menu')) {
                            removeMenu();
                            mountTrigger(false);
                        } else {
                            mountMenu();
                            mountTrigger(true);
                        }
                    });
                    host.replaceChildren(trigger);
                };
                const mountMenu = () => {
                    window.selectionAudit.menuMounts += 1;
                    const menu = document.createElement('div');
                    menu.id = 'mode-menu';
                    menu.setAttribute('role', 'menu');
                    const option = document.createElement('button');
                    option.id = 'pro-option';
                    option.setAttribute('role', 'menuitem');
                    if (selected) option.classList.add('selected');
                    option.innerHTML = `
                        ${selected ? '<span aria-label="Selected">✓</span>' : ''}
                        <span class="label">3.1 Pro</span>
                        <span class="sublabel">Advanced reasoning</span>
                    `;
                    option.addEventListener('click', () => {
                        window.selectionAudit.optionClicks += 1;
                        selected = true;
                        removeMenu();
                        mountTrigger(false);
                    });
                    menu.append(option);
                    document.body.append(menu);
                };
                mountTrigger(false);
            </script>
            """
        )

        assert _select_web_model(page, "chromium", "gemini", "gemini-3.1-pro") is True
        assert page.evaluate("window.selectionAudit") == {
            "triggerMounts": 5,
            "triggerClicks": 3,
            "menuMounts": 2,
            "optionClicks": 1,
        }
        assert page.locator("#mode-menu").count() == 0
        assert page.locator("#mode-picker").get_attribute("aria-expanded") == "false"
    finally:
        context.close()


@pytest.mark.integration
def test_gemini_model_dom_selection_rejects_ambiguous_controlled_triggers(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button
                class="mode-picker"
                aria-label="Open mode picker, currently Flash"
                aria-haspopup="true"
                aria-expanded="false"
                aria-controls="mode-menu"
            >Flash</button>
            <button
                class="mode-picker"
                aria-label="Open mode picker, currently Flash"
                aria-haspopup="true"
                aria-expanded="false"
                aria-controls="mode-menu"
            >Flash</button>
            <div id="mode-menu" role="menu" hidden>
                <button role="menuitem">
                    <span class="label">3.1 Pro</span>
                </button>
            </div>
            <script>
                window.triggerClicks = 0;
                document.querySelectorAll('.mode-picker').forEach((trigger) => {
                    trigger.addEventListener('click', () => {
                        window.triggerClicks += 1;
                    });
                });
            </script>
            """
        )

        assert _select_web_model(page, "chromium", "gemini", "gemini-3.1-pro") is False
        assert page.evaluate("window.triggerClicks") == 0
        assert page.locator("#mode-menu").is_hidden()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("controls_id", "surface_id", "surface_role", "expected_trigger_clicks"),
    (
        ("", "mode-menu", "menu", 0),
        ("missing-menu", "mode-menu", "menu", 1),
        ("mode-menu", "mode-menu", "", 0),
    ),
)
def test_gemini_model_dom_selection_rejects_an_invalid_controlled_surface(
    disposable_browser: Browser,
    controls_id: str,
    surface_id: str,
    surface_role: str,
    expected_trigger_clicks: int,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        controls_attribute = f'aria-controls="{controls_id}"' if controls_id else ""
        role_attribute = f'role="{surface_role}"' if surface_role else ""
        page.set_content(
            f"""
            <button
                id="mode-picker"
                aria-label="Open mode picker, currently Flash"
                aria-haspopup="true"
                aria-expanded="false"
                {controls_attribute}
            >Flash</button>
            <div id="{surface_id}" {role_attribute} hidden>
                <button role="menuitem"><span class="label">3.1 Pro</span></button>
            </div>
            <script>
                window.triggerClicks = 0;
                document.querySelector('#mode-picker').addEventListener('click', () => {{
                    window.triggerClicks += 1;
                }});
            </script>
            """
        )

        assert _select_web_model(page, "chromium", "gemini", "gemini-3.1-pro") is False
        assert page.evaluate("window.triggerClicks") == expected_trigger_clicks
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize("failed_close_stage", ["selection", "verification"])
def test_gemini_model_dom_selection_fails_closed_when_menu_closure_is_unproved(
    disposable_browser: Browser,
    failed_close_stage: str,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            f"""
            <button
                id="mode-picker"
                aria-label="Open mode picker, currently Flash"
                aria-haspopup="true"
                aria-expanded="false"
                aria-controls="mode-menu"
            >Flash</button>
            <div id="mode-menu" role="menu" hidden>
                <button id="pro-option" role="menuitem">
                    <span class="label">3.1 Pro</span>
                    <span class="sublabel">Advanced reasoning</span>
                </button>
            </div>
            <script>
                const failedStage = {json.dumps(failed_close_stage)};
                let selectionMade = false;
                let verificationOpened = false;
                const trigger = document.querySelector('#mode-picker');
                const menu = document.querySelector('#mode-menu');
                const option = document.querySelector('#pro-option');
                trigger.addEventListener('click', () => {{
                    if (failedStage === 'verification' && verificationOpened && !menu.hidden) return;
                    const opening = menu.hidden;
                    menu.hidden = !opening;
                    trigger.setAttribute('aria-expanded', String(opening));
                    if (opening && selectionMade) verificationOpened = true;
                }});
                option.addEventListener('click', () => {{
                    selectionMade = true;
                    option.classList.add('selected');
                    const marker = document.createElement('span');
                    marker.setAttribute('aria-label', 'Selected');
                    marker.textContent = '✓';
                    option.prepend(marker);
                    trigger.setAttribute('aria-label', 'Open mode picker, currently Pro');
                    trigger.textContent = 'Pro';
                    if (failedStage !== 'selection') {{
                        menu.hidden = true;
                        trigger.setAttribute('aria-expanded', 'false');
                    }}
                }});
            </script>
            """
        )

        assert _select_web_model(page, "chromium", "gemini", "gemini-3.1-pro") is False
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("platform", "model", "label"),
    (
        ("claude", "claude-auto", "Auto"),
    ),
)
def test_provider_model_dom_selection_accepts_a_semantic_model_trigger(
    disposable_browser: Browser,
    platform: str,
    model: str,
    label: str,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            f"""
            <button
                id="model-selector"
                aria-label="Model select"
                aria-haspopup="menu"
                aria-expanded="false"
            >{label}</button>
            """
        )

        assert _select_web_model(page, "chromium", platform, model) is True
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("platform", "model"),
    (("grok", "grok-build"), ("claude", "claude-auto")),
)
def test_auto_model_dom_selection_rejects_an_unrelated_auto_popup(
    disposable_browser: Browser,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    model: str,
) -> None:
    import app.core.computer_use_agent as computer_use_agent

    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button
                id="playback-trigger"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="playback-options"
            >Auto</button>
            <div id="playback-options" role="menu" hidden>
                <button role="menuitem">Auto</button>
                <button role="menuitem">1×</button>
            </div>
            <script>
                window.unrelatedAutoClicks = 0;
                document.querySelector('#playback-trigger').addEventListener('click', () => {
                    window.unrelatedAutoClicks += 1;
                    document.querySelector('#playback-options').hidden = false;
                });
            </script>
            """
        )
        monkeypatch.setattr(computer_use_agent, "WEB_MODEL_CONTROL_WAIT_ATTEMPTS", 1)
        monkeypatch.setattr(computer_use_agent, "GROK_MODEL_CONTROL_WAIT_ATTEMPTS", 1)

        assert _select_web_model(page, "chromium", platform, model) is False
        assert page.evaluate("window.unrelatedAutoClicks") == 0
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("platform", "model", "decoy_id"),
    (
        ("grok", "grok-build", "modern-theme"),
        ("grok", "grok-build", "breakfast-options"),
        ("claude", "claude-auto", "modern-theme"),
        ("claude", "claude-auto", "octopus-picker"),
    ),
)
def test_auto_model_dom_selection_rejects_metadata_substring_decoys(
    disposable_browser: Browser,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    model: str,
    decoy_id: str,
) -> None:
    import app.core.computer_use_agent as computer_use_agent

    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            f"""
            <button
                id="{decoy_id}"
                aria-haspopup="menu"
                aria-expanded="false"
            >Auto</button>
            """
        )
        monkeypatch.setattr(computer_use_agent, "WEB_MODEL_CONTROL_WAIT_ATTEMPTS", 1)
        monkeypatch.setattr(computer_use_agent, "GROK_MODEL_CONTROL_WAIT_ATTEMPTS", 1)

        assert _select_web_model(page, "chromium", platform, model) is False
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("platform", "model", "decoy_id", "decoy_label"),
    (
        ("grok", "grok-build", "grok-options", "Modern theme"),
        ("claude", "claude-auto", "claude-options", "Octopus picker"),
    ),
)
def test_auto_model_dom_selection_rejects_label_substring_decoys(
    disposable_browser: Browser,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    model: str,
    decoy_id: str,
    decoy_label: str,
) -> None:
    import app.core.computer_use_agent as computer_use_agent

    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            f"""
            <button
                id="{decoy_id}"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="decoy-options"
            >{decoy_label}</button>
            <div id="decoy-options" role="menu" hidden>
                <button id="decoy-auto" role="menuitem">Auto</button>
            </div>
            <script>
                window.decoyClicks = 0;
                const trigger = document.querySelector('#{decoy_id}');
                trigger.addEventListener('click', () => {{
                    window.decoyClicks += 1;
                    document.querySelector('#decoy-options').hidden = false;
                }});
                document.querySelector('#decoy-auto').addEventListener('click', () => {{
                    trigger.textContent = 'Auto';
                }});
            </script>
            """
        )
        monkeypatch.setattr(computer_use_agent, "WEB_MODEL_CONTROL_WAIT_ATTEMPTS", 1)
        monkeypatch.setattr(computer_use_agent, "GROK_MODEL_CONTROL_WAIT_ATTEMPTS", 1)

        assert _select_web_model(page, "chromium", platform, model) is False
        assert page.evaluate("window.decoyClicks") == 0
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("aria_checked", "data_state", "trigger_label", "expected", "reason"),
    (
        ("true", "checked", "Build Beta", True, ""),
        ("true", "unchecked", "Build Beta", False, "model-selection-proof-conflict"),
        ("false", "checked", "Build Beta", False, "model-selection-proof-conflict"),
        ("true", "checked", "Auto", False, "model-readback-mismatch"),
        ("false", "unchecked", "Auto", False, "model-readback-mismatch"),
    ),
)
def test_grok_model_dom_selection_requires_current_radix_contract_and_dual_proof(
    disposable_browser: Browser,
    aria_checked: str,
    data_state: str,
    trigger_label: str,
    expected: bool,
    reason: str,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            f"""
            <button id="bare-build">Build</button>
            <button id="build-plan" aria-label="SuperGrok Build plan">Build</button>
            <button
                id="model-select-trigger"
                aria-label="Model select"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="model-surface"
            >Auto</button>
            <div id="model-surface" role="menu" hidden>
                <button
                    id="build-option"
                    role="menuitemradio"
                    aria-label="Build"
                    aria-checked="false"
                    data-state="unchecked"
                >Build</button>
            </div>
            <script>
                window.selectionAudit = {{
                    bareBuildClicks: 0,
                    buildPlanClicks: 0,
                    optionClicks: 0,
                }};
                const trigger = document.querySelector('#model-select-trigger');
                const surface = document.querySelector('#model-surface');
                const option = document.querySelector('#build-option');
                document.querySelector('#bare-build').addEventListener('click', () => {{
                    window.selectionAudit.bareBuildClicks += 1;
                }});
                document.querySelector('#build-plan').addEventListener('click', () => {{
                    window.selectionAudit.buildPlanClicks += 1;
                }});
                trigger.addEventListener('click', () => {{
                    const opening = trigger.getAttribute('aria-expanded') !== 'true';
                    trigger.setAttribute('aria-expanded', String(opening));
                    surface.hidden = !opening;
                }});
                option.addEventListener('click', () => {{
                    window.selectionAudit.optionClicks += 1;
                    surface.hidden = true;
                    trigger.setAttribute('aria-expanded', 'false');
                    option.setAttribute('aria-checked', {json.dumps(aria_checked)});
                    option.setAttribute('data-state', {json.dumps(data_state)});
                    trigger.textContent = {json.dumps(trigger_label)};
                }});
            </script>
            """
        )
        observation: dict[str, object] = {}
        assert (
            _select_web_model(
                page,
                "chromium",
                "grok",
                "grok-build",
                observation,
            )
            is expected
        )
        assert page.evaluate("window.selectionAudit") == {
            "bareBuildClicks": 0,
            "buildPlanClicks": 0,
            "optionClicks": 1,
        }
        if reason:
            assert observation["reason"] == reason
        assert page.get_attribute("#model-select-trigger", "aria-expanded") == "false"
        assert page.locator("#model-surface").is_hidden()
    finally:
        context.close()


@pytest.mark.integration
def test_grok_build_selection_accepts_nested_controlled_menu_and_exact_aria_label(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button
                id="model-select-trigger"
                aria-label="Model select"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="model-menu"
            >Auto</button>
            <div id="model-menu" role="menu" hidden>
                <div class="nested-options">
                    <button
                        id="build-plan"
                        role="menuitemradio"
                        aria-label="SuperGrok Build plan"
                        aria-checked="false"
                        data-state="unchecked"
                    >
                        <span class="label">Build</span>
                        <small>Upgrade your SuperGrok plan</small>
                    </button>
                    <button
                        id="build-option"
                        role="menuitemradio"
                        aria-label="Build"
                        aria-checked="false"
                        data-state="unchecked"
                    >
                        <span class="label">Build</span>
                        <small>Use Build mode for agentic tasks</small>
                    </button>
                </div>
            </div>
            <script>
                window.selectionAudit = {planClicks: 0, optionClicks: 0};
                const trigger = document.querySelector('#model-select-trigger');
                const menu = document.querySelector('#model-menu');
                const option = document.querySelector('#build-option');
                trigger.addEventListener('click', () => {
                    const opening = trigger.getAttribute('aria-expanded') !== 'true';
                    trigger.setAttribute('aria-expanded', String(opening));
                    menu.hidden = !opening;
                });
                document.querySelector('#build-plan').addEventListener('click', () => {
                    window.selectionAudit.planClicks += 1;
                });
                option.addEventListener('click', () => {
                    window.selectionAudit.optionClicks += 1;
                    option.setAttribute('aria-checked', 'true');
                    option.setAttribute('data-state', 'checked');
                    trigger.textContent = 'Build Beta';
                    trigger.setAttribute('aria-expanded', 'false');
                    menu.hidden = true;
                });
            </script>
            """
        )

        selected = _select_web_model(
            page,
            "chromium",
            "grok",
            "grok-build",
        )

        assert selected is True
        assert page.evaluate("window.selectionAudit") == {
            "planClicks": 0,
            "optionClicks": 1,
        }
    finally:
        context.close()


@pytest.mark.integration
def test_grok_build_selection_accepts_an_already_selected_option(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button
                id="model-select-trigger"
                aria-label="Model select"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="model-menu"
            >Build Beta</button>
            <div id="model-menu" role="menu" hidden>
                <button
                    id="build-option"
                    role="menuitemradio"
                    aria-label="Build"
                    aria-checked="true"
                    data-state="checked"
                >Build</button>
            </div>
            <script>
                window.optionClicks = 0;
                const trigger = document.querySelector('#model-select-trigger');
                const menu = document.querySelector('#model-menu');
                trigger.addEventListener('click', () => {
                    const opening = trigger.getAttribute('aria-expanded') !== 'true';
                    trigger.setAttribute('aria-expanded', String(opening));
                    menu.hidden = !opening;
                });
                document.querySelector('#build-option').addEventListener('click', () => {
                    window.optionClicks += 1;
                });
            </script>
            """
        )

        assert _select_web_model(page, "chromium", "grok", "grok-build") is True
        assert page.evaluate("window.optionClicks") == 0
        assert page.get_attribute("#model-select-trigger", "aria-expanded") == "false"
        assert page.locator("#model-menu").is_hidden()
    finally:
        context.close()


@pytest.mark.integration
def test_grok_build_selection_dismisses_only_the_two_known_onboarding_dialogs(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button
                id="model-select-trigger"
                aria-label="Model select"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="model-menu"
            >Auto</button>
            <div id="model-menu" role="menu" hidden>
                <button
                    id="build-option"
                    role="menuitemradio"
                    aria-label="Build"
                    aria-checked="false"
                    data-state="unchecked"
                >Build</button>
            </div>
            <div id="first-promo" role="dialog" aria-label="Meet Grok Bot">
                <button id="first-dismiss">Dismiss</button>
            </div>
            <script>
                window.selectionAudit = {
                    dismissClicks: 0,
                    optionClicks: 0,
                    triggerClicks: 0,
                    untrustedClicks: 0,
                };
                const trigger = document.querySelector('#model-select-trigger');
                const menu = document.querySelector('#model-menu');
                const option = document.querySelector('#build-option');
                trigger.addEventListener('click', (event) => {
                    window.selectionAudit.triggerClicks += 1;
                    if (!event.isTrusted) window.selectionAudit.untrustedClicks += 1;
                    const opening = trigger.getAttribute('aria-expanded') !== 'true';
                    trigger.setAttribute('aria-expanded', String(opening));
                    menu.hidden = !opening;
                });
                document.querySelector('#first-dismiss').addEventListener('click', (event) => {
                    window.selectionAudit.dismissClicks += 1;
                    if (!event.isTrusted) window.selectionAudit.untrustedClicks += 1;
                    document.querySelector('#first-promo').remove();
                    document.body.insertAdjacentHTML(
                        'beforeend',
                        '<div id="second-promo" role="dialog" aria-label="Introducing Build Mode">'
                            + '<button id="second-dismiss">Dismiss</button></div>'
                    );
                    document.querySelector('#second-dismiss').addEventListener('click', (event) => {
                        window.selectionAudit.dismissClicks += 1;
                        if (!event.isTrusted) window.selectionAudit.untrustedClicks += 1;
                        document.querySelector('#second-promo').remove();
                    });
                });
                option.addEventListener('click', (event) => {
                    window.selectionAudit.optionClicks += 1;
                    if (!event.isTrusted) window.selectionAudit.untrustedClicks += 1;
                    option.setAttribute('aria-checked', 'true');
                    option.setAttribute('data-state', 'checked');
                    trigger.textContent = 'Build Beta';
                    trigger.setAttribute('aria-expanded', 'false');
                    menu.hidden = true;
                });
            </script>
            """
        )

        assert _select_web_model(page, "chromium", "grok", "grok-build") is True
        assert page.evaluate("window.selectionAudit") == {
            "dismissClicks": 2,
            "optionClicks": 1,
            "triggerClicks": 3,
            "untrustedClicks": 0,
        }
        assert page.locator('[role="dialog"]').count() == 0
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("dialog_role", "aria_modal", "dialog_label", "dismiss_disabled"),
    (
        ("dialog", "false", "Upgrade to SuperGrok", False),
        ("dialog", "false", "Meet Grok Bot", True),
        ("alertdialog", "false", "Upgrade to SuperGrok", False),
        ("none", "true", "Upgrade to SuperGrok", False),
    ),
)
def test_grok_build_selection_rejects_unknown_or_non_actionable_dialogs(
    disposable_browser: Browser,
    dialog_role: str,
    aria_modal: str,
    dialog_label: str,
    dismiss_disabled: bool,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        disabled = "disabled" if dismiss_disabled else ""
        page.set_content(
            f"""
            <button
                id="model-select-trigger"
                aria-label="Model select"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="model-menu"
            >Auto</button>
            <div id="model-menu" role="menu" hidden>
                <button
                    id="build-option"
                    role="menuitemradio"
                    aria-label="Build"
                    aria-checked="false"
                    data-state="unchecked"
                >Build</button>
            </div>
            <div role="{dialog_role}" aria-modal="{aria_modal}" aria-label="{dialog_label}">
                <button id="dismiss" {disabled}>Dismiss</button>
            </div>
            <script>
                window.selectionAudit = {{dismissClicks: 0, triggerClicks: 0}};
                document.querySelector('#dismiss').addEventListener('click', () => {{
                    window.selectionAudit.dismissClicks += 1;
                }});
                document.querySelector('#model-select-trigger').addEventListener('click', () => {{
                    window.selectionAudit.triggerClicks += 1;
                }});
            </script>
            """
        )

        assert _select_web_model(page, "chromium", "grok", "grok-build") is False
        assert page.evaluate("window.selectionAudit") == {
            "dismissClicks": 0,
            "triggerClicks": 0,
        }
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("auto_aria_checked", "auto_data_state", "expected_option_clicks"),
    (("true", "unchecked", 0), ("true", "checked", 1)),
)
def test_grok_build_selection_rejects_conflicting_or_duplicate_radio_proof(
    disposable_browser: Browser,
    auto_aria_checked: str,
    auto_data_state: str,
    expected_option_clicks: int,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            f"""
            <button
                id="model-select-trigger"
                aria-label="Model select"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="model-menu"
            >Auto</button>
            <div id="model-menu" role="menu" hidden>
                <button
                    id="auto-option"
                    role="menuitemradio"
                    aria-label="Auto"
                    aria-checked="{auto_aria_checked}"
                    data-state="{auto_data_state}"
                >Auto</button>
                <button
                    id="build-option"
                    role="menuitemradio"
                    aria-label="Build"
                    aria-checked="false"
                    data-state="unchecked"
                >Build</button>
            </div>
            <script>
                window.optionClicks = 0;
                const trigger = document.querySelector('#model-select-trigger');
                const menu = document.querySelector('#model-menu');
                const build = document.querySelector('#build-option');
                trigger.addEventListener('click', () => {{
                    const opening = trigger.getAttribute('aria-expanded') !== 'true';
                    trigger.setAttribute('aria-expanded', String(opening));
                    menu.hidden = !opening;
                }});
                build.addEventListener('click', () => {{
                    window.optionClicks += 1;
                    build.setAttribute('aria-checked', 'true');
                    build.setAttribute('data-state', 'checked');
                    trigger.textContent = 'Build Beta';
                    trigger.setAttribute('aria-expanded', 'false');
                    menu.hidden = true;
                }});
            </script>
            """
        )
        observation: dict[str, object] = {}

        assert (
            _select_web_model(
                page,
                "chromium",
                "grok",
                "grok-build",
                observation,
            )
            is False
        )
        assert observation["reason"] == "model-selection-proof-conflict"
        assert page.evaluate("window.optionClicks") == expected_option_clicks
        assert page.get_attribute("#model-select-trigger", "aria-expanded") == "false"
        assert page.locator("#model-menu").is_hidden()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("variant", "expected_reason"),
    (
        ("duplicate-trigger", "model-control-ambiguous"),
        ("duplicate-surface", "model-surface-ambiguous"),
        ("duplicate-choice", "model-option-ambiguous"),
        ("unrelated-menu", "model-surface-not-found"),
    ),
)
def test_grok_build_selection_binds_exactly_one_controlled_menu_and_choice(
    disposable_browser: Browser,
    variant: str,
    expected_reason: str,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        trigger = """
            <button
                id="model-select-trigger"
                aria-label="Model select"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="model-menu"
            >Auto</button>
        """
        if variant == "duplicate-trigger":
            trigger += trigger
        choice = """
            <button
                class="build-option"
                role="menuitemradio"
                aria-label="Build"
                aria-checked="false"
                data-state="unchecked"
            >Build</button>
        """
        surface_role = "listbox" if variant == "unrelated-menu" else "menu"
        surface_choices = "" if variant == "unrelated-menu" else choice
        if variant == "duplicate-choice":
            surface_choices += choice
        surface = f"""
            <div id="model-menu" role="{surface_role}" hidden>{surface_choices}</div>
        """
        if variant == "duplicate-surface":
            surface += surface
        unrelated = (
            f'<div id="unrelated-menu" role="menu">{choice}</div>'
            if variant == "unrelated-menu"
            else ""
        )
        page.set_content(
            f"""
            {trigger}
            {surface}
            {unrelated}
            <script>
                window.selectionAudit = {{triggerClicks: 0, choiceClicks: 0}};
                document.querySelectorAll('#model-select-trigger').forEach((button) => {{
                    button.addEventListener('click', () => {{
                        window.selectionAudit.triggerClicks += 1;
                        const opening = button.getAttribute('aria-expanded') !== 'true';
                        button.setAttribute('aria-expanded', String(opening));
                        document.querySelectorAll('[id="model-menu"]').forEach((menu) => {{
                            menu.hidden = !opening;
                        }});
                    }});
                }});
                document.querySelectorAll('.build-option').forEach((button) => {{
                    button.addEventListener('click', () => {{
                        window.selectionAudit.choiceClicks += 1;
                    }});
                }});
            </script>
            """
        )
        observation: dict[str, object] = {}

        assert (
            _select_web_model(
                page,
                "chromium",
                "grok",
                "grok-build",
                observation,
            )
            is False
        )
        assert observation["reason"] == expected_reason
        assert page.evaluate("window.selectionAudit.choiceClicks") == 0
    finally:
        context.close()


@pytest.mark.integration
def test_grok_build_selection_rebinds_a_remounted_radix_menu(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button
                id="model-select-trigger"
                aria-label="Model select"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="model-menu"
            >Auto</button>
            <script>
                window.selectionAudit = {
                    menuMounts: 0,
                    optionClicks: 0,
                    triggerClicks: 0,
                    selected: false,
                };
                const trigger = document.querySelector('#model-select-trigger');
                const unmount = () => document.querySelector('#model-menu')?.remove();
                const mount = () => {
                    unmount();
                    window.selectionAudit.menuMounts += 1;
                    const menu = document.createElement('div');
                    menu.id = 'model-menu';
                    menu.setAttribute('role', 'menu');
                    const option = document.createElement('button');
                    option.setAttribute('role', 'menuitemradio');
                    option.setAttribute('aria-label', 'Build');
                    option.setAttribute(
                        'aria-checked',
                        String(window.selectionAudit.selected)
                    );
                    option.setAttribute(
                        'data-state',
                        window.selectionAudit.selected ? 'checked' : 'unchecked'
                    );
                    option.textContent = 'Build';
                    option.addEventListener('click', () => {
                        window.selectionAudit.optionClicks += 1;
                        window.selectionAudit.selected = true;
                        trigger.textContent = 'Build Beta';
                        trigger.setAttribute('aria-expanded', 'false');
                        unmount();
                    });
                    menu.append(option);
                    document.body.append(menu);
                };
                trigger.addEventListener('click', () => {
                    window.selectionAudit.triggerClicks += 1;
                    const opening = trigger.getAttribute('aria-expanded') !== 'true';
                    trigger.setAttribute('aria-expanded', String(opening));
                    if (opening) mount();
                    else unmount();
                });
            </script>
            """
        )

        assert _select_web_model(page, "chromium", "grok", "grok-build") is True
        assert page.evaluate("window.selectionAudit") == {
            "menuMounts": 2,
            "optionClicks": 1,
            "triggerClicks": 3,
            "selected": True,
        }
        assert page.locator("#model-menu").count() == 0
        assert page.get_attribute("#model-select-trigger", "aria-expanded") == "false"
    finally:
        context.close()


@pytest.mark.integration
def test_grok_build_selection_fails_before_an_extra_click_on_a_late_paywall(
    disposable_browser: Browser,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            """
            <button
                id="model-select-trigger"
                aria-label="Model select"
                aria-haspopup="menu"
                aria-expanded="false"
                aria-controls="model-menu"
            >Auto</button>
            <div id="model-menu" role="menu" hidden>
                <button
                    id="build-option"
                    role="menuitemradio"
                    aria-label="Build"
                    aria-checked="false"
                    data-state="unchecked"
                >Build</button>
            </div>
            <script>
                window.selectionAudit = {triggerClicks: 0, optionClicks: 0};
                const trigger = document.querySelector('#model-select-trigger');
                const menu = document.querySelector('#model-menu');
                const option = document.querySelector('#build-option');
                trigger.addEventListener('click', () => {
                    window.selectionAudit.triggerClicks += 1;
                    const opening = trigger.getAttribute('aria-expanded') !== 'true';
                    trigger.setAttribute('aria-expanded', String(opening));
                    menu.hidden = !opening;
                });
                option.addEventListener('click', () => {
                    window.selectionAudit.optionClicks += 1;
                    option.setAttribute('aria-checked', 'true');
                    option.setAttribute('data-state', 'checked');
                    trigger.textContent = 'Build Beta';
                    trigger.setAttribute('aria-expanded', 'false');
                    menu.hidden = true;
                    document.body.insertAdjacentHTML(
                        'beforeend',
                        '<div role="alertdialog" aria-modal="true" '
                            + 'aria-label="Upgrade to SuperGrok">Upgrade</div>'
                    );
                });
            </script>
            """
        )
        observation: dict[str, object] = {}

        assert (
            _select_web_model(
                page,
                "chromium",
                "grok",
                "grok-build",
                observation,
            )
            is False
        )
        assert observation["reason"] == "blocking-dialog"
        assert page.evaluate("window.selectionAudit") == {
            "triggerClicks": 1,
            "optionClicks": 1,
        }
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("platform", "body"),
    (
        (
            "gemini",
            """
            <user-query><div data-test-id="user-query-content">prompt</div></user-query>
            <model-response>
                <message-content>
                    <div class="model-response-text"><pre><code>{"action":"bodycheck"}</code></pre></div>
                </message-content>
                <div class="response-actions"><button>Copy</button></div>
            </model-response>
            <rich-textarea>
                <div contenteditable="true" aria-label="Enter a prompt"></div>
            </rich-textarea>
            """,
        ),
        (
            "grok",
            """
            <article data-role="user">prompt</article>
            <article data-testid="assistant-message">
                <div data-testid="response-content"><pre><code>{"action":"bodycheck"}</code></pre></div>
                <div data-testid="response-actions"><button>Copy</button></div>
            </article>
            <div data-testid="user-composer"><textarea aria-label="Ask Grok"></textarea></div>
            """,
        ),
    ),
)
def test_provider_turn_snapshot_uses_canonical_outer_turn_roots(
    disposable_browser: Browser,
    platform: str,
    body: str,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(body)

        snapshot = _provider_turn_snapshot(page, platform)

        assert snapshot["count"] == 1
        assert snapshot["userCount"] == 1
        assert snapshot["latestUserText"] == "prompt"
        assert snapshot["assistantAfterLatestUser"] is True
        assert snapshot["text"].startswith("```json\n")
        assert parse_agent_action(snapshot["text"])["action"] == "bodycheck"
        assert "Copy" not in snapshot["text"]
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize("platform", ["gemini", "grok"])
def test_provider_turn_snapshot_rejects_assistant_before_latest_user(
    disposable_browser: Browser,
    platform: str,
) -> None:
    assistant = (
        '<model-response><pre><code>{"action":"final","summary":"stale"}</code></pre></model-response>'
        if platform == "gemini"
        else '<article data-testid="assistant-message"><pre><code>{"action":"final","summary":"stale"}</code></pre></article>'
    )
    user = (
        "<user-query>new prompt</user-query>"
        if platform == "gemini"
        else '<article data-role="user">new prompt</article>'
    )
    composer = (
        '<rich-textarea><div contenteditable="true" aria-label="Enter a prompt"></div></rich-textarea>'
        if platform == "gemini"
        else '<textarea aria-label="Ask Grok"></textarea>'
    )
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(f"{assistant}{user}{composer}")

        snapshot = _provider_turn_snapshot(page, platform)

        assert snapshot["assistantAfterLatestUser"] is False
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("receipt_case", "expects_receipt"),
    [
        pytest.param("composer", False, id="marker-only-in-composer"),
        pytest.param("assistant", False, id="marker-only-in-assistant-message"),
        pytest.param("stale-user", False, id="marker-only-in-earlier-user-message"),
        pytest.param("latest-user", True, id="marker-in-latest-visible-user-message"),
    ],
)
def test_grok_submission_receipt_requires_marker_in_latest_visible_user_message(
    disposable_browser: Browser,
    receipt_case: str,
    expects_receipt: bool,
) -> None:
    marker = "agent-transfer-e2e-receipt-marker"
    bodies = {
        "composer": (
            f'<div data-testid="user-composer" contenteditable="true">{marker}</div>'
        ),
        "assistant": (
            f'<article data-message-author-role="assistant">{marker}</article>'
        ),
        "stale-user": (
            f'<article data-role="user">Earlier prompt {marker}</article>'
            '<article data-role="user">Latest prompt without a receipt</article>'
        ),
        "latest-user": (
            '<article data-role="user">Earlier prompt without a receipt</article>'
            f'<article data-role="user">Latest prompt {marker}</article>'
            '<article data-role="user" hidden>Hidden later prompt without a receipt</article>'
        ),
    }
    conversation_url = "https://grok.com/c/receipt-contract-e2e"
    context = disposable_browser.new_context()
    page = context.new_page()
    page.route(
        conversation_url,
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=f"<!doctype html><html><body>{bodies[receipt_case]}</body></html>",
        ),
    )
    try:
        page.goto(conversation_url, wait_until="domcontentloaded")
        binding = _ProviderSessionBinding(
            page,
            "grok",
            conversation_url,
            "recent",
        )
        binding.submission_marker = marker

        receipt_url = binding._current_submission_receipt_url()

        assert receipt_url == (conversation_url if expects_receipt else "")
    finally:
        context.close()


@pytest.mark.integration
def test_fresh_grok_send_atomically_rejects_an_old_conversation_target(
    disposable_browser: Browser,
) -> None:
    actual_url = "https://grok.com/c/old"
    expected_url = "https://grok.com/"
    context = disposable_browser.new_context()
    page = context.new_page()
    page.route(
        actual_url,
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="""
                <!doctype html>
                <html>
                    <body>
                        <form>
                            <textarea aria-label="Ask Grok"></textarea>
                            <button id="send" type="button" aria-label="Send">Send</button>
                        </form>
                        <script>
                            window.sendClickAudit = 0;
                            document.querySelector('#send').addEventListener('click', () => {
                                window.sendClickAudit += 1;
                            });
                        </script>
                    </body>
                </html>
            """,
        ),
    )
    try:
        page.goto(actual_url, wait_until="domcontentloaded")

        with pytest.raises(RuntimeError, match="selected provider tab changed"):
            _submit_chromium_web_prompt(
                page,
                "grok",
                "Start a fresh agentic task",
                lambda: False,
                expected_target_url=expected_url,
                session_mode="new",
            )

        assert page.url == actual_url
        assert page.evaluate("window.sendClickAudit") == 0
    finally:
        context.close()


@pytest.mark.integration
def test_fresh_grok_send_accepts_the_expected_root_landing(
    disposable_browser: Browser,
) -> None:
    landing_url = "https://grok.com/"
    prompt = "Start a fresh agentic task"
    context = disposable_browser.new_context()
    page = context.new_page()
    page.route(
        landing_url,
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="""
                <!doctype html>
                <html>
                    <body>
                        <form>
                            <textarea aria-label="Ask Grok"></textarea>
                            <button id="send" type="button" aria-label="Send">Send</button>
                        </form>
                        <script>
                            window.sendClickAudit = 0;
                            const composer = document.querySelector('textarea');
                            document.querySelector('#send').addEventListener('click', () => {
                                window.sendClickAudit += 1;
                                const userMessage = document.createElement('article');
                                userMessage.setAttribute('data-role', 'user');
                                userMessage.textContent = composer.value;
                                document.body.append(userMessage);
                                composer.value = '';
                                composer.dispatchEvent(new Event('input', {bubbles: true}));
                            });
                        </script>
                    </body>
                </html>
            """,
        ),
    )
    try:
        page.goto(landing_url, wait_until="domcontentloaded")

        _submit_chromium_web_prompt(
            page,
            "grok",
            prompt,
            lambda: False,
            expected_target_url=landing_url,
            session_mode="new",
        )

        assert page.url == landing_url
        assert page.evaluate("window.sendClickAudit") == 1
        assert page.locator("textarea").input_value() == ""
        expect(page.locator('[data-role="user"]')).to_have_text(prompt)
    finally:
        context.close()


@pytest.mark.integration
def test_grok_send_uses_visible_composer_and_nearest_bounded_semantic_scope(
    disposable_browser: Browser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.computer_use_agent as computer_use_agent

    monkeypatch.setattr(computer_use_agent, "CHROMIUM_SEND_BUTTON_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(
        computer_use_agent,
        "CHROMIUM_SUBMISSION_ACCEPT_TIMEOUT_SECONDS",
        2,
    )
    landing_url = "https://grok.com/"
    prompt = "Use the visible composer"
    receipt_marker = "agent-turn-" + ("d" * 32)
    wire_prompt = f"{prompt}\n\nController turn receipt: {receipt_marker}"
    context = disposable_browser.new_context()
    page = context.new_page()
    page.route(
        landing_url,
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="""
                <!doctype html>
                <html>
                    <body>
                        <button id="unrelated-send" type="button" aria-label="Send">Send</button>
                        <div role="dialog" aria-label="Feedback">
                            <textarea id="feedback-composer" aria-label="Feedback"></textarea>
                            <button id="feedback-submit" type="button">Submit</button>
                        </div>
                        <main id="composer-scope">
                            <textarea id="hidden-composer" hidden></textarea>
                            <div><section><div>
                                <div
                                    id="visible-composer"
                                    contenteditable="true"
                                    role="textbox"
                                    aria-label="Ask Grok anything"
                                ></div>
                            </div></section></div>
                            <button
                                id="provider-send"
                                type="button"
                                aria-label="Send"
                                data-testid="chat-submit"
                            >Send</button>
                        </main>
                        <script>
                            window.sendAudit = {provider: 0, unrelated: 0, feedback: 0};
                            window.injectTrailingComposerText = false;
                            const buildComposer = document.querySelector('#visible-composer');
                            const renderBuildText = (value) => {
                                buildComposer.replaceChildren(
                                    ...value.split('\\n').map((line) => {
                                        const paragraph = document.createElement('p');
                                        if (line) paragraph.textContent = line;
                                        else paragraph.append(document.createElement('br'));
                                        return paragraph;
                                    })
                                );
                            };
                            let pendingBuildText = '';
                            buildComposer.addEventListener('beforeinput', (event) => {
                                if (typeof event.data === 'string') {
                                    pendingBuildText = event.data;
                                }
                            });
                            buildComposer.addEventListener('input', () => {
                                if (!pendingBuildText) return;
                                const value = pendingBuildText;
                                pendingBuildText = '';
                                renderBuildText(value);
                                if (window.injectTrailingComposerText) {
                                    buildComposer.append(document.createTextNode(' unexpected'));
                                }
                            });
                            document.querySelector('#unrelated-send').addEventListener('click', () => {
                                window.sendAudit.unrelated += 1;
                            });
                            document.querySelector('#provider-send').addEventListener('click', () => {
                                window.sendAudit.provider += 1;
                                const composer = document.querySelector('#visible-composer');
                                const message = document.createElement('article');
                                message.setAttribute('data-role', 'user');
                                message.textContent = [...composer.children]
                                    .map((paragraph) => paragraph.textContent || '')
                                    .join('\\n');
                                document.querySelector('#composer-scope').append(message);
                                composer.replaceChildren();
                            });
                            document.querySelector('#feedback-submit').addEventListener('click', () => {
                                window.sendAudit.feedback += 1;
                            });
                        </script>
                    </body>
                </html>
            """,
        ),
    )
    try:
        page.goto(landing_url, wait_until="domcontentloaded")
        assert page.evaluate("window.sendAudit") == {
            "provider": 0,
            "unrelated": 0,
            "feedback": 0,
        }

        accepted = _submit_chromium_web_prompt(
            page,
            "grok",
            wire_prompt,
            lambda: False,
            expected_target_url=landing_url,
            session_mode="new",
            baseline_snapshot={
                "url": landing_url,
                "count": 0,
                "userCount": 0,
                "latestUserText": "",
                "text": "",
            },
            submission_receipt_marker=receipt_marker,
        )

        assert accepted is True
        assert page.evaluate("window.sendAudit") == {
            "provider": 1,
            "unrelated": 0,
            "feedback": 0,
        }
        assert page.locator("#hidden-composer").input_value() == ""
        assert page.locator("#visible-composer").inner_text() == ""
        assert page.locator("#feedback-composer").input_value() == ""
        expect(page.locator('[data-role="user"]')).to_have_text(wire_prompt)

        page.evaluate("window.injectTrailingComposerText = true")
        rejected_marker = "agent-turn-" + ("e" * 32)
        rejected_prompt = (
            "Reject an ambiguous composer"
            f"\n\nController turn receipt: {rejected_marker}"
        )
        with pytest.raises(RuntimeError, match="did not preserve"):
            _submit_chromium_web_prompt(
                page,
                "grok",
                rejected_prompt,
                lambda: False,
                expected_target_url=landing_url,
                session_mode="new",
                baseline_snapshot=_provider_turn_snapshot(page, "grok"),
                submission_receipt_marker=rejected_marker,
            )
        assert page.evaluate("window.sendAudit") == {
            "provider": 1,
            "unrelated": 0,
            "feedback": 0,
        }
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_finished_snapshot_does_not_auto_select_recent_chatgpt_session(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    captured_ask_payloads: list[dict[str, str]] = []
    source_requests: list[str] = []
    catalog_payload = _chatgpt_catalog_sessions(
        {
            "id": "qqqm-session",
            "title": "比较 QQQM 与 QQQ",
            "url": FINISHED_SNAPSHOT_URL,
            "updated_at": "2026-08-25T09:03:57Z",
        },
        {
            "id": "agentic-troubleshooting",
            "title": "Agentic Troubleshooting",
            "url": AGENTIC_TROUBLESHOOTING_URL,
            "updated_at": "2026-08-26T01:00:00Z",
        },
    )

    def fulfill_agent_status(route) -> None:
        route.fulfill(json=_finished_chatgpt_agent_payload())

    def fulfill_browser_status(route) -> None:
        route.fulfill(
            json={
                "platform": "chatgpt",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "ChatGPT account",
                "message": "Edge is ready for ChatGPT Web.",
                "agent_sources": catalog_payload,
            }
        )

    def fulfill_preferences(route) -> None:
        route.fulfill(json=_finished_chatgpt_agent_payload())

    def fulfill_sources(route) -> None:
        source_requests.append(route.request.url)
        route.fulfill(json=catalog_payload)

    def fulfill_ask(route) -> None:
        captured_ask_payloads.append(route.request.post_data_json or {})
        route.fulfill(json=_finished_chatgpt_agent_payload())

    def fulfill_history(route) -> None:
        route.fulfill(json={"title": "", "history": []})

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route("**/api/agent/preferences", fulfill_preferences)
    page.route("**/api/agent/sources**", fulfill_sources)
    page.route("**/api/agent/ask", fulfill_ask)
    page.route("**/api/agent/chatgpt-session-history**", fulfill_history)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        expect(page.locator(".agent-session-mode-combobox [data-agent-combobox-selected-label]")).to_have_text(
            "New session"
        )
        expect(page.locator("[data-agent-prompt-session-mode]")).to_have_value("new")
        expect(page.locator("[data-agent-prompt-conversation-url]")).to_have_value("")
        expect(page.locator("[data-agent-prompt-session-title]")).to_have_value("")
        expect(page.locator("#agent_conversation_link")).to_have_attribute("href", FINISHED_SNAPSHOT_URL)
        expect(
            page.locator(
                f'[data-recent-conversation-url="{FINISHED_SNAPSHOT_URL}"]'
            )
        ).to_have_count(1)
        page.wait_for_timeout(500)
        assert source_requests == [], (
            "An initial finished snapshot must consume bootstrapped sources without "
            "starting a second browser collection."
        )

        expect(page.locator("#agent_ask_button")).to_be_enabled()
        page.locator("[data-agent-prompt-input]").fill("Inspect the workspace without changing files.")
        with page.expect_request(re.compile(r"/api/agent/ask$")):
            page.locator("#agent_ask_button").click()
        assert captured_ask_payloads
        payload = captured_ask_payloads[0]
        assert payload["session_mode"] == "new"
        assert payload.get("conversation_url", "") == ""
        assert payload.get("session_title", "") == ""
        assert payload["prompt"] == "Inspect the workspace without changing files."

    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_incomplete_chatgpt_effort_catalog_hides_stale_snapshot_options(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Incomplete browser payloads and Agent snapshots cannot add provider options."""
    agent_payload = _finished_chatgpt_agent_payload()
    agent_payload["agent"].update(
        {
            "available_efforts": ["Expired subscription tier"],
            "thinking_effort": "Expired subscription tier",
        }
    )
    incomplete_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web; live effort catalog unavailable.",
        "agent_sources_error": "Live source catalog unavailable.",
        "available_efforts": ["Stale live tier"],
        "thinking_effort": "Stale live tier",
        "effort_catalog_complete": False,
    }

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route(
        "**/api/agent/status",
        lambda route: route.fulfill(json=agent_payload),
    )
    browser_requests = []

    def bootstrap(route):
        browser_requests.append(route.request.url)
        route.fulfill(json=incomplete_status)

    page.route("**/api/browser-session**", bootstrap)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        expect(page.locator(".browser-session-status-account")).to_have_text("ChatGPT account")
        effort_options = page.locator(
            ".agent-effort-dropdown [data-agent-combobox-option]"
        )
        expect(effort_options).to_have_count(1)
        expect(effort_options).to_have_attribute(
            "data-agent-combobox-option",
            "highest_available",
        )
        expect(page.locator("[data-agent-effort-input]")).to_have_value(
            "highest_available"
        )
        for _ in range(4):
            page.get_by_role("button", name="Option: Highest available", exact=True).click()
        assert len(browser_requests) == 1
        refresh_options = page.locator("[data-agent-effort-refresh]")
        expect(refresh_options).to_have_count(0)
        assert [text.strip() for text in effort_options.all_text_contents()] == [
            "Highest available"
        ]
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("freshness_kind", "expect_provider_options"),
    (
        ("server_cache", True),
        ("stale_cache", True),
        ("unknown", False),
        ("live_browser", True),
    ),
)
def test_complete_chatgpt_effort_catalog_accepts_verified_browser_session_provenance(
    disposable_browser: Browser,
    sidebar_server_url: str,
    freshness_kind: str,
    expect_provider_options: bool,
) -> None:
    """Expose complete provider labels from verified browser-session provenance only."""
    agent_payload = _finished_chatgpt_agent_payload()
    agent_payload["agent"].update(
        {
            "available_efforts": ["Saved snapshot label"],
            "thinking_effort": "Saved snapshot label",
        }
    )
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": _chatgpt_catalog_sessions(),
        "available_efforts": ["Live first", "Live maximum"],
        "thinking_effort": "Live maximum",
        "effort_catalog_complete": True,
        "browser_session_freshness": {
            "kind": freshness_kind,
            "cache_status": {
                "live_browser": "refreshed",
                "server_cache": "hit",
                "stale_cache": "stale",
                "unknown": "hit",
            }[freshness_kind],
            "cached_at": "2026-08-31T00:00:00Z",
            "age_seconds": 0 if freshness_kind == "live_browser" else 30,
        },
    }
    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", lambda route: route.fulfill(json=agent_payload))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        effort_options = page.locator(
            ".agent-effort-dropdown [data-agent-combobox-option]"
        )
        expected_values = (
            ["highest_available", "Live first", "Live maximum"]
            if expect_provider_options
            else ["highest_available"]
        )
        expect(effort_options).to_have_count(len(expected_values))
        assert effort_options.evaluate_all(
            "options => options.map((option) => option.dataset.agentComboboxOption)"
        ) == expected_values
        assert "Saved snapshot label" not in effort_options.all_text_contents()
        if expect_provider_options:
            expect(page.locator("[data-agent-effort-field]")).to_have_attribute(
                "data-agent-effort-catalog-freshness",
                freshness_kind,
            )
            page.locator(
                ".agent-effort-combobox [data-agent-combobox-trigger]"
            ).click()
            page.locator(
                '.agent-effort-dropdown [data-agent-combobox-option="Live first"]'
            ).click()
            expect(page.locator("[data-agent-effort-input]")).to_have_value("Live first")
        else:
            assert page.locator("[data-agent-effort-field]").get_attribute(
                "data-agent-effort-catalog-freshness"
            ) is None
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_client_cached_chatgpt_effort_catalog_reuses_options_without_a_refresh_button(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """A verified client cache survives reload without another browser probe."""
    browser_status_requests: list[str] = []
    cached_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Cached but formerly live ChatGPT status.",
        "agent_sources": _chatgpt_catalog_sessions(),
        "model_catalog_complete": True,
        "model_options": [{"key": "live:latest", "label": "Latest"}],
        "actual_model": "Latest",
        "available_efforts": ["Old live maximum"],
        "thinking_effort": "Old live maximum",
        "effort_catalog_complete": True,
        "browser_session_freshness": {
            "kind": "live_browser",
            "cache_status": "refreshed",
            "cached_at": "2026-08-31T00:00:00Z",
            "age_seconds": 0,
        },
    }
    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="no-preference",
    )
    page = context.new_page()
    cache_key = "cachelikes:browser-session:v8:agent:chatgpt:edge"
    page.add_init_script(
        f"sessionStorage.setItem({json.dumps(cache_key)}, JSON.stringify({{"
        f"cached_at: Date.now(), payload: {json.dumps(cached_status)}}}));"
    )
    page.route("**/api/agent/status", lambda route: route.fulfill(json=_finished_chatgpt_agent_payload()))
    def fulfill_browser_status(route) -> None:
        browser_status_requests.append(route.request.url)
        route.fulfill(json=cached_status)

    page.route("**/api/browser-session**", fulfill_browser_status)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        effort_options = page.locator(
            ".agent-effort-dropdown [data-agent-combobox-option]"
        )
        expect(effort_options).to_have_count(2)
        assert effort_options.evaluate_all(
            "options => options.map((option) => option.dataset.agentComboboxOption)"
        ) == ["highest_available", "Old live maximum"]
        expect(page.locator("[data-agent-effort-field]")).to_have_attribute(
            "data-agent-effort-catalog-freshness",
            "client_cache",
        )
        assert browser_status_requests == []

        expect(page.locator("[data-agent-effort-refresh]")).to_have_count(0)
        page.reload(wait_until="domcontentloaded")
        expect(effort_options).to_have_count(2)
        assert browser_status_requests == []
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_running_chatgpt_agent_locks_model_and_effort_controls(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Prevent runtime model and effort changes while an Agent task is running."""
    agent_payload = _finished_chatgpt_agent_payload()
    agent_payload["agent"].update(
        {
            "running": True,
            "phase": "running",
            "run_id": "running-composer-lock",
            "run_revision": 1,
            "started_at": "2026-09-02T08:00:00Z",
            "finished_at": "",
            "activity": [],
        }
    )
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": _chatgpt_catalog_sessions(),
        "available_efforts": ["Live first", "Live maximum"],
        "thinking_effort": "Live maximum",
        "effort_catalog_complete": True,
        "browser_session_freshness": {
            "kind": "live_browser",
            "cache_status": "refreshed",
            "cached_at": "2026-09-02T08:00:00Z",
            "age_seconds": 0,
        },
    }
    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", lambda route: route.fulfill(json=agent_payload))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        model = page.locator(".agent-model-trigger")
        effort = page.locator(".agent-effort-trigger")
        refresh = page.locator("[data-agent-effort-refresh]")
        expect(model).to_be_disabled()
        expect(effort).to_be_disabled()
        expect(refresh).to_have_count(0)
        expect(model).to_have_attribute("aria-expanded", "false")
        expect(effort).to_have_attribute("aria-expanded", "false")
        expect(page.locator(".agent-model-dropdown")).to_be_hidden()
        expect(page.locator(".agent-effort-dropdown")).to_be_hidden()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_reenables_loaded_project_selector_after_run_finishes(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Unlock a loaded Project selector after a running Agent snapshot finishes."""
    project_url = "https://chatgpt.com/g/g-p-project-selector/project"
    catalog_payload = {
        **_chatgpt_catalog_sessions(),
        "projects": [{
            "id": "project-selector",
            "title": "Project selector regression",
            "url": project_url,
            "updated_at": "2026-09-03T00:00:00Z",
            "icon": "terminal",
            "icon_color": "#3A83F7",
        }],
    }
    finished_payload = _finished_chatgpt_agent_payload()
    finished_payload["agent"].update(
        {
            "run_id": "project-selector-lock",
            "run_revision": 2,
            "started_at": "2026-09-03T08:00:00Z",
            "finished_at": "2026-09-03T08:01:00Z",
        }
    )
    running_payload = _finished_chatgpt_agent_payload()
    running_payload["agent"].update(
        {
            "running": True,
            "phase": "running",
            "run_id": "project-selector-lock",
            "run_revision": 1,
            "started_at": "2026-09-03T08:00:00Z",
            "finished_at": "",
            "activity": [],
        }
    )
    def fulfill_agent_status(route) -> None:
        route.fulfill(json=finished_payload)

    def fulfill_browser_status(route) -> None:
        route.fulfill(
            json={
                "platform": "chatgpt",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "ChatGPT account",
                "message": "Edge is ready for ChatGPT Web.",
                "agent_sources": catalog_payload,
            }
        )

    def fulfill_project_sessions(route) -> None:
        route.fulfill(json={"platform": "chatgpt", "project_url": project_url, "sessions": []})

    def fulfill_ask(route) -> None:
        route.fulfill(json=running_payload)

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 900},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route("**/api/agent/project-sessions**", fulfill_project_sessions)
    page.route("**/api/agent/ask", fulfill_ask)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        page.locator(".agent-session-mode-combobox [data-agent-combobox-trigger]").click()
        page.locator(
            '.agent-session-mode-combobox [data-agent-combobox-option="project"]'
        ).click()
        project_trigger = page.locator(
            '[data-agent-session-list="projects"] [data-agent-combobox-trigger]'
        )
        project_option = page.locator(
            f'[data-agent-session-list="projects"] [data-agent-combobox-option="{project_url}"]'
        )
        expect(project_option).to_have_count(1)
        expect(project_trigger).to_be_enabled()
        project_trigger.click()
        project_option.click()
        page.locator("[data-agent-prompt-input]").fill("Run the Project selector regression.")
        expect(page.locator("#agent_ask_button")).to_be_enabled()
        with page.expect_request(re.compile(r"/api/agent/ask$")):
            page.locator("#agent_ask_button").click()
        expect(project_trigger).to_be_disabled()
        expect(project_trigger).to_be_enabled()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_restores_chatgpt_project_by_stable_id_after_slug_change(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Replace remembered Project aliases with the current catalog URL."""
    project_id = "g-p-6a978edb95308191a53d2bb113154c10"
    old_project_url = f"https://chatgpt.com/g/{project_id}-antigravity/project"
    current_project_url = f"https://chatgpt.com/g/{project_id}-worthward/project"
    old_conversation_url = f"https://chatgpt.com/g/{project_id}-antigravity/c/session-1"
    catalog_payload = {
        **_chatgpt_catalog_sessions(),
        "projects": [{
            "id": project_id,
            "title": "worthward",
            "url": current_project_url,
            "updated_at": "2026-09-09T00:00:00Z",
            "icon": "currency-dollar",
            "icon_color": "#53B559",
        }],
    }
    status_payload = _finished_chatgpt_agent_payload()
    status_payload["sessions"] = [{
        "session_id": "old-project-session",
        "running": False,
        "phase": "finished",
        "workspace_path": load_computer_use_settings().workspace_path,
        "project_url": old_project_url,
        "conversation_url": old_conversation_url,
        "session_title": "Old slug task remains visible",
        "updated_at": "2026-09-09T00:00:00Z",
    }]
    project_session_requests: list[str] = []

    def fulfill_browser_status(route) -> None:
        route.fulfill(
            json={
                "platform": "chatgpt",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "ChatGPT account",
                "message": "Edge is ready for ChatGPT Web.",
                "agent_sources": catalog_payload,
            }
        )

    def fulfill_project_sessions(route) -> None:
        project_session_requests.append(route.request.url)
        route.fulfill(
            json={
                "platform": "chatgpt",
                "project_url": current_project_url,
                "sessions": [],
            }
        )

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 900},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    remembered_key = "cachelikes:agent-session-selection:v1:chatgpt:edge"
    remembered_value = {
        "version": 1,
        "mode": "project",
        "project_url": old_project_url,
        "project_session_url": "new",
    }
    context.add_init_script(
        "window.localStorage.setItem("
        f"{json.dumps(remembered_key)}, {json.dumps(json.dumps(remembered_value))});"
    )
    page = context.new_page()
    page.route("**/api/agent/status", lambda route: route.fulfill(json=status_payload))
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route("**/api/agent/project-sessions**", fulfill_project_sessions)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        expect(page.locator("input[data-agent-session-mode]")).to_have_value("project")
        expect(page.locator("[data-agent-project-url]")).to_have_value(current_project_url)
        expect(
            page.locator('[data-agent-session-list="projects"] [data-agent-combobox-trigger]')
        ).to_contain_text("worthward")
        expect(page.get_by_text("Old slug task remains visible", exact=True)).to_be_visible()
        page.wait_for_function(
            """({key, currentProjectUrl}) => {
                const value = JSON.parse(window.localStorage.getItem(key) || '{}');
                return value.project_url === currentProjectUrl;
            }""",
            arg={
                "key": remembered_key,
                "currentProjectUrl": current_project_url,
            },
        )
        assert project_session_requests
        assert any(
            current_project_url in unquote(request)
            for request in project_session_requests
        )
        assert page.locator(
            f'[data-agent-session-list="projects"] '
            f'[data-agent-combobox-option="{old_project_url}"]'
        ).count() == 0
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("same_project", "response_status"),
    ((True, 200), (True, 503), (False, 200), (False, 503)),
)
def test_agent_project_session_inflight_response_uses_stable_project_identity(
    disposable_browser: Browser,
    sidebar_server_url: str,
    same_project: bool,
    response_status: int,
) -> None:
    """Settle an old-slug response only while the same stable Project remains selected."""
    project_id = "g-p-6a978edb95308191a53d2bb113154c10"
    other_project_id = "g-p-7b089fec06419202b64e3cc224265d21"
    old_project_url = (
        f"https://chatgpt.com/g/{project_id}-old-slug/project?view=legacy#old-fragment"
    )
    current_project_url = (
        f"https://chatgpt.com/g/{project_id}-current-slug/project?view=current#new-fragment"
    )
    other_project_url = (
        f"https://chatgpt.com/g/{other_project_id}-other/project?view=current#new-fragment"
    )
    selected_project_url = current_project_url if same_project else other_project_url
    session_url = f"https://chatgpt.com/g/{project_id}-current-slug/c/inflight-session"
    legacy_session_url = (
        f"https://chatgpt.com/g/{project_id}-old-slug/c/inflight-session"
        "?view=legacy#old-fragment"
    )
    catalog_payload = {
        **_chatgpt_catalog_sessions(),
        "recent_sessions": [{
            "id": "canonical-session",
            "title": "Canonical conversation title",
            "url": session_url,
            "updated_at": "2026-09-09T00:00:30Z",
        }],
        "projects": [{
            "id": project_id,
            "title": "Old slug project",
            "url": old_project_url,
            "updated_at": "2026-09-09T00:00:00Z",
        }],
    }
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": catalog_payload,
    }
    status_payload = _finished_chatgpt_agent_payload()
    status_payload["sessions"] = [{
        "session_id": "query-drift-local-session",
        "running": False,
        "phase": "finished",
        "workspace_path": load_computer_use_settings().workspace_path,
        "project_url": "",
        "conversation_url": legacy_session_url,
        "session_title": "Legacy conversation title",
        "updated_at": "2026-09-09T00:00:00Z",
    }]
    pending_project_routes = []

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 900},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route(
        "**/api/agent/status",
        lambda route: route.fulfill(json=status_payload),
    )
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog_payload))
    page.route(
        "**/api/agent/project-sessions**",
        lambda route: pending_project_routes.append(route),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        project_option = page.locator(
            f'[data-agent-session-list="projects"] '
            f'[data-agent-combobox-option="{old_project_url}"]'
        )
        expect(project_option).to_have_count(1)
        execution_list = page.locator("[data-agent-execution-session-list]")
        expect(execution_list.locator("button")).to_have_count(1)
        expect(
            execution_list.locator(
                '[data-execution-session-id="query-drift-local-session"]'
            )
        ).to_have_count(1)
        expect(
            execution_list.locator(f'[data-recent-conversation-url="{session_url}"]')
        ).to_have_count(0)

        page.locator(".agent-session-mode-combobox [data-agent-combobox-trigger]").click()
        page.locator(
            '.agent-session-mode-combobox [data-agent-combobox-option="project"]'
        ).click()
        expect(project_option).to_have_count(1)
        page.locator(
            '[data-agent-session-list="projects"] [data-agent-combobox-trigger]'
        ).click()
        with page.expect_request(re.compile(r"/api/agent/project-sessions\?")):
            project_option.click()
        assert len(pending_project_routes) == 1

        page.locator("[data-agent-project-url]").evaluate(
            "(input, value) => { input.value = value; }",
            selected_project_url,
        )
        expect(page.locator("[data-agent-project-url]")).to_have_value(selected_project_url)

        if response_status == 200:
            pending_project_routes[0].fulfill(
                json={
                    "platform": "chatgpt",
                    "project_url": old_project_url,
                    "sessions": [{
                        "id": "inflight-session",
                        "title": "Stable in-flight session",
                        "url": session_url,
                        "updated_at": "2026-09-09T00:01:00Z",
                    }],
                }
            )
        else:
            pending_project_routes[0].fulfill(
                status=response_status,
                json={"error": "Project session lookup failed."},
            )

        session_option = execution_list.locator(
            f'[data-recent-conversation-url="{session_url}"]'
        )
        if same_project and response_status == 200:
            expect(session_option).to_have_count(0)
            expect(execution_list.locator("button")).to_have_count(1)
            expect(execution_list.locator("button")).to_contain_text(
                "Stable in-flight session"
            )
            expect(execution_list).not_to_contain_text("Loading recent sessions…")
        elif same_project:
            expect(session_option).to_have_count(0)
            expect(execution_list).to_contain_text("No recent sessions.")
            expect(execution_list).not_to_contain_text("Loading recent sessions…")
        else:
            expect(session_option).to_have_count(0)
            expect(execution_list).to_contain_text("Loading recent sessions…")
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_chatgpt_retry_control_targets_only_provider_error_boundary(
    disposable_browser: Browser,
) -> None:
    """Reject unrelated Retry controls before handling the provider error."""
    context = disposable_browser.new_context(
        viewport={"width": 1_024, "height": 768},
        reduced_motion="reduce",
    )
    page = context.new_page()
    try:
        page.set_content(
            """
            <style>button, #prompt-textarea { display: block; width: 160px; height: 32px; }</style>
            <nav><button id="nav-retry">Retry</button></nav>
            <div role="dialog"><p>Something went wrong</p><button id="dialog-retry">Retry</button></div>
            <article data-testid="conversation-turn-history">
              <div data-message-author-role="assistant">
                <section data-testid="response-error-history">
                  <p>There was an error generating a response.</p>
                  <button id="history-retry">Try again</button>
                </section>
              </div>
            </article>
            <section data-testid="tool-error">
              <p>There was an error generating a response.</p><button id="tool-retry">Retry</button>
            </section>
            <section data-testid="page-error">
              <p>Something went wrong</p><button id="page-retry">Try again</button>
            </section>
            <div id="prompt-textarea" contenteditable="true"></div>
            <script>
              window.retryClicks = [];
              for (const button of document.querySelectorAll('button')) {
                button.onclick = () => window.retryClicks.push(button.id);
              }
            </script>
            """
        )
        composer_ready = _chatgpt_retry_control(
            page,
            "",
            click=True,
            require_composer_absent=True,
        )
        assert composer_ready["clicked"] is False
        assert page.evaluate("window.retryClicks") == []

        page.locator("#prompt-textarea").evaluate("element => element.remove()")
        pre_submit = _chatgpt_retry_control(
            page,
            "",
            click=True,
            require_composer_absent=True,
        )
        assert pre_submit["clicked"] is True
        assert pre_submit["label"] == "Try again"
        assert page.evaluate("window.retryClicks") == ["page-retry"]

        page.set_content(
            """
            <style>button { display: block; width: 160px; height: 32px; }</style>
            <article data-testid="conversation-turn-old">
              <div data-message-author-role="user">Old request</div>
              <div data-message-author-role="assistant">
                <section data-testid="response-error-old">
                  <p>There was an error generating a response.</p>
                  <button id="old-response-retry">Try again</button>
                </section>
              </div>
            </article>
            <article data-testid="conversation-turn-user">
              <div data-message-author-role="user">Current request</div>
            </article>
            <article data-testid="conversation-turn-assistant">
              <div data-message-author-role="assistant">
                <section data-testid="tool-error">
                  <p>There was an error generating a response.</p>
                  <button id="tool-retry">Retry</button>
                </section>
                <section data-testid="response-error-current">
                  <p>Something went wrong</p>
                  <button id="current-response-retry">Try again</button>
                </section>
              </div>
            </article>
            <button id="regenerate">Regenerate</button>
            <script>
              window.retryClicks = [];
              for (const button of document.querySelectorAll('button')) {
                button.onclick = () => window.retryClicks.push(button.id);
              }
            </script>
            """
        )
        response_retry = _chatgpt_retry_control(
            page,
            "",
            click=True,
            require_after_latest_user=True,
        )
        assert response_retry["clicked"] is True
        assert page.evaluate("window.retryClicks") == ["current-response-retry"]

        page.set_content(
            """
            <style>button { display: block; width: 160px; height: 32px; }</style>
            <article><div data-message-author-role="user">Current request</div></article>
            <section role="alert">
              <p>Something went wrong</p><button id="global-response-retry">Try again</button>
            </section>
            <script>
              window.retryClicks = [];
              document.querySelector('button').onclick = () => window.retryClicks.push('global');
            </script>
            """
        )
        global_response_retry = _chatgpt_retry_control(
            page,
            "",
            click=True,
            require_after_latest_user=True,
        )
        assert global_response_retry["clicked"] is True
        assert page.evaluate("window.retryClicks") == ["global"]

        page.set_content(
            """
            <style>button { display: block; width: 160px; height: 32px; }</style>
            <article><div data-message-author-role="user">Current request</div></article>
            <section role="alert">
              <p>Something went wrong</p><button id="retry-one">Try again</button>
            </section>
            <section role="alert">
              <p>Something went wrong</p><button id="retry-two">Retry</button>
            </section>
            <script>
              window.retryClicks = [];
              for (const button of document.querySelectorAll('button')) {
                button.onclick = () => window.retryClicks.push(button.id);
              }
            </script>
            """
        )
        ambiguous_response_retry = _chatgpt_retry_control(
            page,
            "",
            click=True,
            require_after_latest_user=True,
        )
        assert ambiguous_response_retry["clicked"] is False
        assert ambiguous_response_retry["ambiguous"] is True
        assert page.evaluate("window.retryClicks") == []
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_chatgpt_atomic_send_accepts_project_slug_redirect_in_browser(
    disposable_browser: Browser,
) -> None:
    """Exercise the stable-ID target guard across two Project slug aliases."""
    project_id = "g-p-6a978edb95308191a53d2bb113154c10"
    expected_target_url = f"https://chatgpt.com/g/{project_id}/project"
    current_target_url = f"https://chatgpt.com/g/{project_id}-worthward/project"
    context = disposable_browser.new_context(
        viewport={"width": 1_024, "height": 768},
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route(
        "https://chatgpt.com/**",
        lambda route: route.fulfill(
            content_type="text/html",
            body="""
                <style>button, #prompt-textarea { display: block; width: 160px; height: 32px; }</style>
                <div id="prompt-textarea" contenteditable="true"></div>
                <button data-testid="send-button" aria-label="Send prompt">Send</button>
                <script>
                  window.sendClicks = 0;
                  document.querySelector('button').onclick = () => {
                    window.sendClicks += 1;
                    document.querySelector('#prompt-textarea').textContent = '';
                  };
                </script>
            """,
        ),
    )
    try:
        page.goto(current_target_url, wait_until="domcontentloaded")
        _submit_chromium_prompt(
            page,
            "Inspect the project",
            lambda: False,
            expected_target_url=expected_target_url,
        )
        assert page.evaluate("window.sendClicks") == 1
        assert page.url == current_target_url
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    "current_target_url",
    (
        "https://chatgpt.com/c/project-session",
        (
            "https://chatgpt.com/g/"
            "g-p-11111111111111111111111111111111-other/c/project-session"
        ),
    ),
)
def test_chatgpt_atomic_controls_reject_project_scope_drift_in_browser(
    disposable_browser: Browser,
    current_target_url: str,
) -> None:
    """Keep Send and Retry inside the selected stable Project identity."""
    project_id = "g-p-6a978edb95308191a53d2bb113154c10"
    expected_target_url = (
        f"https://chatgpt.com/g/{project_id}-worthward/c/project-session"
    )
    context = disposable_browser.new_context(
        viewport={"width": 1_024, "height": 768},
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route(
        "https://chatgpt.com/**",
        lambda route: route.fulfill(
            content_type="text/html",
            body="""
                <style>button, #prompt-textarea { display: block; width: 160px; height: 32px; }</style>
                <div id="prompt-textarea" contenteditable="true"></div>
                <button data-testid="send-button" aria-label="Send prompt">Send</button>
                <section role="alert">
                  <p>Something went wrong</p><button id="retry">Try again</button>
                </section>
                <script>
                  window.sendClicks = 0;
                  window.retryClicks = 0;
                  document.querySelector('[data-testid="send-button"]').onclick = () => {
                    window.sendClicks += 1;
                  };
                  document.querySelector('#retry').onclick = () => {
                    window.retryClicks += 1;
                  };
                </script>
            """,
        ),
    )
    try:
        page.goto(current_target_url, wait_until="domcontentloaded")
        with pytest.raises(RuntimeError, match="tab changed before the prompt"):
            _submit_chromium_prompt(
                page,
                "Inspect the project",
                lambda: False,
                expected_target_url=expected_target_url,
            )
        with pytest.raises(RuntimeError, match="tab changed before its provider retry"):
            _chatgpt_retry_control(
                page,
                expected_target_url,
                click=True,
            )
        assert page.evaluate("window.sendClicks") == 0
        assert page.evaluate("window.retryClicks") == 0
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_chatgpt_atomic_send_rejects_browser_composer_drift(
    disposable_browser: Browser,
) -> None:
    """Never click Send when the live composer no longer holds the filled message."""
    target_url = "https://chatgpt.com/c/composer-drift"
    context = disposable_browser.new_context(
        viewport={"width": 1_024, "height": 768},
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route(
        "https://chatgpt.com/**",
        lambda route: route.fulfill(
            content_type="text/html",
            body="""
                <style>button, #prompt-textarea { display: block; width: 160px; height: 32px; }</style>
                <div id="prompt-textarea" contenteditable="true"></div>
                <button data-testid="send-button" aria-label="Send prompt">Send</button>
                <script>
                  window.sendClicks = 0;
                  const composer = document.querySelector('#prompt-textarea');
                  composer.addEventListener('input', () => {
                    composer.textContent = 'tampered in page';
                  }, {once: true});
                  document.querySelector('button').onclick = () => {
                    window.sendClicks += 1;
                  };
                </script>
            """,
        ),
    )
    try:
        page.goto(target_url, wait_until="domcontentloaded")
        with pytest.raises(RuntimeError, match="composer changed before Send"):
            _submit_chromium_prompt(
                page,
                "Inspect the project",
                lambda: False,
                expected_target_url=target_url,
            )
        assert page.evaluate("window.sendClicks") == 0
        assert page.locator("#prompt-textarea").inner_text() == "tampered in page"
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_foreign_running_agent_poll_keeps_only_neutral_stop_state(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """A later global running snapshot cannot leak another project's task data."""
    foreign_workspace = "/tmp/foreign-agent-workspace"
    sentinels = (
        "FOREIGN_AGENT_PROMPT_SENTINEL",
        "FOREIGN_AGENT_RESPONSE_SENTINEL",
        "FOREIGN_AGENT_ERROR_SENTINEL",
        "FOREIGN_AGENT_ACTIVITY_SENTINEL",
        foreign_workspace,
    )
    payload = _finished_chatgpt_agent_payload()
    payload["agent"] = {
        **payload["agent"],
        "running": True,
        "paused": False,
        "phase": "running",
        "workspace_path": foreign_workspace,
        "prompt": sentinels[0],
        "response": sentinels[1],
        "response_html": f"<p>{sentinels[1]}</p>",
        "last_error": sentinels[2],
        "error_traceback": sentinels[2],
        "activity": [
            {
                "status": "running",
                "label": "Read",
                "detail": sentinels[3],
                "meta": "Turn 1",
            }
        ],
        "history": [
            {
                "prompt": sentinels[0],
                "response": sentinels[1],
                "response_html": f"<p>{sentinels[1]}</p>",
            }
        ],
        "message": "FOREIGN_AGENT_MESSAGE_SENTINEL",
        "conversation_url": "https://chatgpt.com/c/foreign-agent-sentinel",
    }
    status_requests = 0

    def fulfill_agent_status(route) -> None:
        nonlocal status_requests
        status_requests += 1
        route.fulfill(json=payload)

    def fulfill_browser_status(route) -> None:
        route.fulfill(
            json={
                "platform": "chatgpt",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "ChatGPT account",
                "message": "Edge is ready for ChatGPT Web.",
            }
        )

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        page.wait_for_function(
            """() => window.performance.getEntriesByType('resource').some((entry) =>
                String(entry.name || '').includes('/api/agent/status')
            )"""
        )
        assert status_requests >= 1
        expect(page.locator("#agent_ask_button")).to_have_attribute(
            "data-agent-action", "stop"
        )
        expect(page.locator("#agent_ask_button")).to_have_attribute(
            "aria-label", "Stop Agent task"
        )
        expect(page.locator("#agent_response_status")).to_have_attribute(
            "data-status", "running"
        )
        expect(page.locator("#agent_response_status")).to_contain_text(
            "An Agent task is running in another project."
        )
        expect(page.locator("#agent_response_output")).to_be_hidden()
        activity_panel = page.locator("#agent_activity_panel")
        expect(activity_panel).to_be_visible()
        expect(activity_panel).to_have_js_property("open", False)
        expect(page.locator("#agent_activity_list > .agent-activity-item")).to_have_count(0)
        expect(page.locator("#agent_error_record")).to_be_hidden()
        expect(page.locator("[data-agent-workspace-input]")).not_to_have_value(
            foreign_workspace
        )
        body_text = page.locator("body").inner_text()
        for sentinel in sentinels:
            assert sentinel not in body_text
        assert "FOREIGN_AGENT_MESSAGE_SENTINEL" not in body_text
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(("width", "height"), ((1_024, 863), (390, 844)))
def test_agent_response_scrollports_keep_actions_and_last_line_inside(
    disposable_browser: Browser,
    sidebar_server_url: str,
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    """Check actual glyphs, shared centers, scroll ownership, and bottom clearance."""
    payload = _finished_chatgpt_agent_payload()
    response_html = "<pre><code>" + "Long code line\n" * 180 + "Last line</code></pre>"
    payload["agent"].update({
        "prompt": "Long question text. " * 200,
        "response": "Long code line\n" * 180 + "Last line",
        "response_html": response_html,
        "history": [
            {"prompt": "Long question text. " * 200, "response": str(index), "response_html": response_html}
            for index in range(3)
        ],
    })
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height}, reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", lambda route: route.fulfill(json=payload))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json={
        "platform": "chatgpt", "browser": "edge", "logged_in": True,
        "can_download": True, "agent_sources": _chatgpt_catalog_sessions(),
    }))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        expect(page.locator(".agent-response-answer-content")).to_contain_text("Last line")
        page.evaluate("() => document.fonts.ready")
        cdp = context.new_cdp_session(page)
        cdp.send("DOM.enable")
        cdp.send("CSS.enable")
        document = cdp.send("DOM.getDocument")
        node = cdp.send("DOM.querySelector", {
            "nodeId": document["root"]["nodeId"], "selector": ".agent-model-trigger-label",
        })
        fonts = cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node["nodeId"]})["fonts"]
        assert any(font["postScriptName"] == "UniversNextforHSBC-Regular" for font in fonts), fonts
        assert all(font["postScriptName"] != "UniversNextforHSBC-Bold" for font in fonts), fonts
        read_layout = """() => {
            const selectors = {
                theme: '#global_theme_toggle', questionToggle: '.agent-response-question-header button',
                copy: '[data-agent-response-copy]', answer: '#agent_response_answer',
                output: '#agent_response_output', pagination: '#agent_response_pagination',
                composer: '.agent-composer-shell', questionBox: '#agent_response_question_scroll',
                question: '#agent_response_question', code: '.agent-response-answer-content pre code',
                pre: '.agent-response-answer-content pre', content: '.agent-response-answer-content',
            };
            return Object.fromEntries(Object.entries(selectors).map(([key, selector]) => {
                const e = document.querySelector(selector), r = e.getBoundingClientRect();
                return [key, {left:r.left,right:r.right,top:r.top,bottom:r.bottom,
                    height:r.height, center:r.left+r.width/2,scrollTop:e.scrollTop}];
            }));
        }"""
        before = page.evaluate(read_layout)
        for action in ("questionToggle", "copy"):
            assert before[action]["center"] == pytest.approx(before["theme"]["center"], abs=1), before
        assert before["answer"]["bottom"] == pytest.approx(before["output"]["bottom"], abs=1)
        assert before["answer"]["height"] > 100
        assert before["answer"]["bottom"] >= before["composer"]["bottom"] - 1
        assert before["question"]["bottom"] <= before["questionBox"]["bottom"] + 1
        assert before["pagination"]["bottom"] < before["composer"]["top"]
        material = page.evaluate("""() => {
            const shell = getComputedStyle(document.querySelector('.agent-composer-shell'));
            const pager = getComputedStyle(document.querySelector('.agent-response-pagination'));
            return {padding: shell.padding, background: shell.background, pager: pager.background,
                blur: shell.backdropFilter, pagerBlur: pager.backdropFilter,
                parent: getComputedStyle(document.querySelector('.agent-task-card')).backgroundColor};
        }""")
        assert material["padding"] == "8px"
        assert material["background"] == material["pager"]
        assert material["blur"] == material["pagerBlur"]
        assert material["parent"] == "rgba(0, 0, 0, 0)"
        page.locator("#agent_response_question").hover()
        page.mouse.wheel(0, 600)
        page.locator("#agent_response_answer").focus()
        page.keyboard.press("End")
        page.wait_for_function("() => document.querySelector('#agent_response_answer').scrollTop > 0")
        page.evaluate("() => { const e=document.querySelector('#agent_response_answer'); e.scrollTop=e.scrollHeight; }")
        after = page.evaluate(read_layout)
        for action in ("questionToggle", "copy"):
            assert after[action]["top"] == pytest.approx(before[action]["top"], abs=1)
        assert after["question"]["scrollTop"] > 0
        assert after["code"]["bottom"] <= after["pre"]["bottom"]
        assert after["pre"]["bottom"] <= after["content"]["bottom"]
        assert after["content"]["bottom"] < after["pagination"]["top"]
        assert after["code"]["right"] <= after["answer"]["right"]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"agent-response-{width}.png"))
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_response_copy_uses_raw_history_text_and_the_global_action_rail(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Copy raw answers across history pages without shifting the global action rail."""
    older_raw_response = "Older source **Markdown**\n\nKeep this exact text."
    latest_raw_response = "Latest source `payload`\n\nKeep this exact text too."
    agent_payload = _finished_chatgpt_agent_payload()
    agent_payload["agent"].update(
        {
            "prompt": "Latest prompt",
            "response": latest_raw_response,
            "response_html": "<p>Rendered latest answer only.</p>",
            "history": [
                {
                    "prompt": "Older prompt",
                    "response": older_raw_response,
                    "response_html": "<p>Rendered older answer only.</p>",
                    "started_at": "2026-08-31T00:00:00Z",
                    "finished_at": "2026-08-31T00:01:00Z",
                },
                {
                    "prompt": "Latest prompt",
                    "response": latest_raw_response,
                    "response_html": "<p>Rendered latest answer only.</p>",
                    "started_at": "2026-08-31T00:02:00Z",
                    "finished_at": "2026-08-31T00:03:00Z",
                },
            ],
        }
    )
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": _chatgpt_catalog_sessions(),
    }
    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 900},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.add_init_script(
        """
        Object.defineProperty(navigator, "clipboard", {
            configurable: true,
            value: {
                writeText: async (value) => {
                    window.__agentResponseCopiedText = value;
                },
            },
        });
        """
    )
    page.route("**/api/agent/status", lambda route: route.fulfill(json=agent_payload))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        answer = page.locator("[data-agent-response-answer-content]")
        copy_button = page.locator("[data-agent-response-copy]")
        expect(answer).to_have_text("Rendered latest answer only.")
        expect(copy_button).to_be_visible()

        copy_button.click()
        page.wait_for_function(
            "expected => window.__agentResponseCopiedText === expected",
            arg=latest_raw_response,
        )
        expect(copy_button).to_have_attribute("aria-label", "Answer copied")

        page.get_by_role("button", name="Conversation page 1", exact=True).click()
        expect(answer).to_have_text("Rendered older answer only.")
        copy_button.click()
        page.wait_for_function(
            "expected => window.__agentResponseCopiedText === expected",
            arg=older_raw_response,
        )

        for width, height in ((1_280, 900), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            page.evaluate(
                """() => new Promise(resolve => {
                    requestAnimationFrame(() => requestAnimationFrame(resolve));
                })"""
            )
            page.wait_for_function(
                """() => {
                    const copy = document.querySelector("[data-agent-response-copy]");
                    const theme = document.querySelector("#global_theme_toggle");
                    const answer = document.querySelector("[data-agent-response-answer]");
                    if (!(copy instanceof HTMLElement)
                        || !(theme instanceof HTMLElement)
                        || !(answer instanceof HTMLElement)) return false;
                    const copyRect = copy.getBoundingClientRect();
                    const themeRect = theme.getBoundingClientRect();
                    const answerRect = answer.getBoundingClientRect();
                    return Math.abs(copyRect.right - themeRect.right) <= 1
                        && copyRect.top >= answerRect.top
                        && copyRect.left >= answerRect.left
                        && copyRect.right <= answerRect.right + 1
                        && document.documentElement.scrollWidth <= window.innerWidth;
                }"""
            )
            rail = page.evaluate(
                """() => {
                    const copy = document.querySelector("[data-agent-response-copy]");
                    const theme = document.querySelector("#global_theme_toggle");
                    const answer = document.querySelector("[data-agent-response-answer]");
                    if (!(copy instanceof HTMLElement)
                        || !(theme instanceof HTMLElement)
                        || !(answer instanceof HTMLElement)) return null;
                    const copyRect = copy.getBoundingClientRect();
                    const themeRect = theme.getBoundingClientRect();
                    const answerRect = answer.getBoundingClientRect();
                    return {
                        answer: {left: answerRect.left, right: answerRect.right, top: answerRect.top},
                        copy: {left: copyRect.left, right: copyRect.right, top: copyRect.top},
                        theme: {right: themeRect.right},
                        hasHorizontalOverflow: document.documentElement.scrollWidth > window.innerWidth,
                    };
                }"""
            )
            assert rail is not None
            assert abs(rail["copy"]["right"] - rail["theme"]["right"]) <= 1, (width, rail)
            assert rail["copy"]["top"] >= rail["answer"]["top"]
            assert rail["copy"]["left"] >= rail["answer"]["left"]
            assert rail["copy"]["right"] <= rail["answer"]["right"] + 1
            assert not rail["hasHorizontalOverflow"]
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_response_action_rail_survives_a_short_crowded_viewport(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep Safari, question expansion, and copy actions separated in a short view."""
    payload = _finished_chatgpt_agent_payload()
    payload["agent"].update(
        {
            "prompt": "请检查这个项目使用了哪些机器学习算法？" * 18,
            "response": "回答内容。" * 160,
            "response_html": f"<p>{'回答内容。' * 160}</p>",
        }
    )
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": _chatgpt_catalog_sessions(),
    }
    context = disposable_browser.new_context(
        viewport={"width": 390, "height": 400},
        has_touch=True,
        is_mobile=True,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", lambda route: route.fulfill(json=payload))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        question_toggle = page.locator(
            ".agent-response-question-header .agent-response-overflow-toggle"
        )
        expect(question_toggle).to_be_visible()
        expect(page.locator(".agent-response-answer-shell > .agent-response-overflow-toggle")).to_have_count(0)
        expect(page.locator("[data-agent-open-conversation]")).to_be_visible()
        expect(page.locator("[data-agent-response-copy]")).to_be_visible()

        layout = page.evaluate(
            """() => {
                const selector = {
                    toolbar: '.agent-response-toolbar',
                    header: '#agent_response_question_header',
                    question: '[data-agent-response-question]',
                    safari: '[data-agent-open-conversation]',
                    expand: '.agent-response-question-header .agent-response-overflow-toggle',
                    copy: '[data-agent-response-copy]',
                    theme: '#global_theme_toggle',
                    };
                const rect = value => {
                    const element = document.querySelector(value);
                    if (!(element instanceof HTMLElement)) return null;
                    const box = element.getBoundingClientRect();
                    return {left: box.left, right: box.right, top: box.top, bottom: box.bottom, height: box.height};
                };
                const boxes = Object.fromEntries(
                    Object.entries(selector).map(([key, value]) => [key, rect(value)]),
                );
                const overlap = (left, right) => left && right
                    && left.left < right.right
                    && left.right > right.left
                    && left.top < right.bottom
                    && left.bottom > right.top;
                return {
                    boxes,
                    headerClientHeight: document.querySelector('#agent_response_question_header')?.clientHeight,
                    questionLineHeight: Number.parseFloat(getComputedStyle(
                        document.querySelector('[data-agent-response-question]'),
                    ).lineHeight),
                    overlaps: {
                        safariExpand: overlap(boxes.safari, boxes.expand),
                        expandCopy: overlap(boxes.expand, boxes.copy),
                        questionExpand: overlap(boxes.question, boxes.expand),
                    },
                    horizontalOverflow: Math.max(
                        document.documentElement.scrollWidth,
                        document.body.scrollWidth,
                    ) - document.documentElement.clientWidth,
                };
            }"""
        )
        assert layout["headerClientHeight"] >= 36
        assert layout["headerClientHeight"] >= layout["questionLineHeight"]
        assert layout["boxes"]["toolbar"]["height"] >= layout["boxes"]["safari"]["height"]
        assert not any(layout["overlaps"].values()), layout
        assert layout["boxes"]["safari"]["right"] == pytest.approx(
            layout["boxes"]["expand"]["right"], abs=1
        )
        assert layout["boxes"]["expand"]["right"] == pytest.approx(
            layout["boxes"]["copy"]["right"], abs=1
        )
        for action in ("safari", "expand", "copy"):
            assert layout["boxes"][action]["right"] == pytest.approx(
                layout["boxes"]["theme"]["right"], abs=1
            ), layout
        assert layout["horizontalOverflow"] <= 1

        page.set_viewport_size({"width": 1_159, "height": 863})
        # Chromium can lay out the fixed container before its resized touch button.
        page.wait_for_function(
            """() => {
                const container = document.querySelector('.global-quick-actions').getBoundingClientRect();
                const button = document.querySelector('#global_theme_toggle').getBoundingClientRect();
                return Math.abs(button.width - container.width) <= 0.1
                    && Math.abs(button.right - container.right) <= 0.1;
            }""",
            timeout=5_000,
        )
        desktop_rail = page.evaluate(
            """() => {
                const selectors = {
                    theme: '#global_theme_toggle',
                    safari: '[data-agent-open-conversation]',
                    expand: '.agent-response-question-header .agent-response-overflow-toggle',
                    copy: '[data-agent-response-copy]',
                };
                const rect = selector => {
                    const element = document.querySelector(selector);
                    if (!(element instanceof HTMLElement)) return null;
                    const box = element.getBoundingClientRect();
                    return {right: box.right, top: box.top, bottom: box.bottom};
                };
                return Object.fromEntries(
                    Object.entries(selectors).map(([key, selector]) => [key, rect(selector)]),
                );
            }"""
        )
        assert all(desktop_rail[key] is not None for key in ("theme", "safari", "expand", "copy"))
        for action in ("safari", "expand", "copy"):
            assert desktop_rail[action]["right"] == pytest.approx(
                desktop_rail["theme"]["right"], abs=1
            ), desktop_rail
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_response_scroll_stays_at_the_bottom_during_status_refresh(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep a user's bottom position when a refreshed answer grows in place."""
    initial_lines = "\n".join(f"Response line {line}" for line in range(1, 161))
    updated_lines = f"{initial_lines}\nResponse line 161"
    initial_payload = _finished_chatgpt_agent_payload()
    initial_payload["agent"].update(
        {
            "prompt": "Keep the answer scroll position stable.",
            "response": initial_lines,
            "response_html": f"<pre>{initial_lines}</pre>",
        }
    )
    updated_payload = _finished_chatgpt_agent_payload()
    updated_payload["agent"].update(
        {
            "prompt": "Keep the answer scroll position stable.",
            "response": updated_lines,
            "response_html": f"<pre>{updated_lines}</pre>",
        }
    )
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": _chatgpt_catalog_sessions(),
    }
    status_requests = 0

    def fulfill_agent_status(route) -> None:
        nonlocal status_requests
        status_requests += 1
        route.fulfill(json=initial_payload if status_requests == 1 else updated_payload)

    context = disposable_browser.new_context(
        viewport={"width": 1_008, "height": 1_085},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        answer_content = page.locator("[data-agent-response-answer-content]")
        page.wait_for_function(
            """() => {
                const answer = document.querySelector('[data-agent-response-answer]');
                return answer && answer.scrollHeight > answer.clientHeight + 100;
            }"""
        )
        page.evaluate(
            """() => {
                const answer = document.querySelector('[data-agent-response-answer]');
                answer.scrollTop = answer.scrollHeight;
            }"""
        )
        expect(answer_content).to_contain_text("Response line 160")
        page.wait_for_function(
            """() => window.performance.getEntriesByType('resource').filter((entry) =>
                String(entry.name || '').includes('/api/agent/status')
            ).length >= 2"""
        )
        expect(answer_content).to_contain_text("Response line 161")
        scroll_state = page.evaluate(
            """() => {
                const answer = document.querySelector('[data-agent-response-answer]');
                const style = getComputedStyle(answer);
                return {
                    atBottom: answer.scrollHeight - answer.clientHeight - answer.scrollTop <= 1,
                    overflowAnchor: style.overflowAnchor,
                    scrollTop: answer.scrollTop,
                    scrollHeight: answer.scrollHeight,
                    clientHeight: answer.clientHeight,
                };
            }"""
        )
        assert scroll_state["atBottom"]
        assert scroll_state["scrollTop"] > 0
        assert scroll_state["overflowAnchor"] == "none"
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_browser_status_retries_a_fresh_negative_cache_and_force_refreshes(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    browser_status_requests: list[str] = []
    negative_status = {
        "platform": "gemini",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": False,
        "can_download": False,
        "account_name": "",
        "message": "Edge is not signed in to Gemini.",
    }
    ready_status = {
        **negative_status,
        "logged_in": True,
        "can_download": True,
        "account_name": "Gemini account",
        "message": "Edge verified an authenticated Gemini Web session.",
    }

    def fulfill_browser_status(route) -> None:
        browser_status_requests.append(route.request.url)
        route.fulfill(json=ready_status)

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    cache_key = "cachelikes:browser-session:v8:agent:gemini:edge"
    page.add_init_script(
        f"sessionStorage.setItem({json.dumps(cache_key)}, JSON.stringify({{"
        f"cached_at: Date.now(), payload: {json.dumps(negative_status)}}}));"
    )
    page.route(
        "**/api/agent/status",
        lambda route: route.fulfill(json=_finished_chatgpt_agent_payload()),
    )
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(
            json={
                "platform": "gemini",
                "browser_label": "Edge",
                "recent_sessions": [],
                "projects": [],
                "limit": 20,
            }
        ),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/gemini", wait_until="domcontentloaded")
        expect(page.locator(".agent-readiness")).to_have_count(0)
        expect(page.locator("#agent_ask_button")).to_be_enabled()
        assert len(browser_status_requests) == 1

        page.evaluate(
            """async () => {
                const root = document.querySelector('[data-agent-browser-session]');
                const controller = window.CACHELIKES_BROWSER_SESSION_STATUS.init(root, {
                    platform: 'gemini',
                    browserId: 'edge',
                    scope: 'agent',
                });
                await controller.refresh();
            }"""
        )
        assert len(browser_status_requests) == 2
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("logged_in", (False, True))
def test_agent_browser_status_login_action_matches_probe_state(
    disposable_browser: Browser,
    sidebar_server_url: str,
    logged_in: bool,
) -> None:
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": logged_in,
        "can_download": logged_in,
        "account_name": "ChatGPT account" if logged_in else "",
        "message": (
            "Edge is ready for ChatGPT Web."
            if logged_in
            else "Edge is not signed in to ChatGPT."
        ),
        "agent_sources": _chatgpt_catalog_sessions(),
    }
    login_requests: list[dict[str, object]] = []

    def fulfill_browser_session(route) -> None:
        if route.request.method == "POST":
            login_requests.append(route.request.post_data_json or {})
            route.fulfill(
                json={
                    "opened": True,
                    "platform": "chatgpt",
                    "browser": "edge",
                    "message": "Edge opened for sign-in.",
                }
            )
            return
        route.fulfill(json=browser_status)

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route(
        "**/api/agent/status",
        lambda route: route.fulfill(json=_finished_chatgpt_agent_payload()),
    )
    page.route("**/api/browser-session**", fulfill_browser_session)
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(json=_chatgpt_catalog_sessions()),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        login_button = page.locator('[data-role="browser-session-login"]')
        if logged_in:
            expect(login_button).to_be_hidden()
        else:
            expect(login_button).to_be_visible()
            expect(login_button).to_have_text("Open Edge to sign in")
            login_button.click()
            expect(login_button).to_be_enabled()
            assert login_requests == [{"platform": "chatgpt", "browser": "edge"}]
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_status_stays_objective_while_browser_verification_is_pending(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Show a neutral checking state until the browser probe has completed."""
    pending_payload = _finished_chatgpt_agent_payload()
    pending_payload["agent"] = {
        **pending_payload["agent"],
        "phase": "",
        "message": "",
        "response": "",
        "response_html": "",
        "conversation_url": "",
        "started_at": "",
        "finished_at": "",
    }
    browser_status_requests: list[str] = []

    def hold_browser_status(route) -> None:
        browser_status_requests.append(route.request.url)
        # Keep the probe unresolved so the page remains in its verification state.

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", lambda route: route.fulfill(json=pending_payload))
    page.route("**/api/browser-session**", hold_browser_status)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        response_status = page.locator("#agent_response_status")
        response_status_copy = page.locator("[data-agent-response-status-copy]")
        response_status_spinner = page.locator("[data-agent-response-status-spinner]")
        expect(response_status).to_be_visible()
        expect(response_status).to_have_attribute("data-status", "loading")
        expect(response_status_spinner).to_be_visible()
        expect(response_status_copy).to_contain_text("Checking")
        expect(response_status_copy).not_to_contain_text("Unavailable")
        assert len(browser_status_requests) == 1
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_project_path_prefers_trailing_directories_without_overflow(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep the useful end of a long project path visible in the current-project input."""
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
    }
    long_path = "/Users/lightwing/Desktop/agenticContext/projects/ABC/DEF"
    context = disposable_browser.new_context(
        viewport={"width": 1_024, "height": 768},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route(
        "**/api/agent/status",
        lambda route: route.fulfill(json=_finished_chatgpt_agent_payload()),
    )
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(
            json={
                "platform": "chatgpt",
                "browser_label": "Edge",
                "recent_sessions": [],
                "projects": [],
                "limit": 20,
            }
        ),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        project_path = page.locator("#agent_project_path")
        project_path.fill(long_path)
        geometry = project_path.evaluate(
            """input => {
                const style = getComputedStyle(input);
                return {
                    direction: style.direction,
                    textAlign: style.textAlign,
                    textOverflow: style.textOverflow,
                    value: input.value,
                    documentOverflow: Math.max(
                        document.documentElement.scrollWidth,
                        document.body.scrollWidth,
                    ) > document.documentElement.clientWidth,
                };
            }"""
        )
        assert geometry["value"] == long_path
        assert geometry["direction"] == "rtl"
        assert geometry["textAlign"] == "left"
        assert geometry["textOverflow"] == "ellipsis"
        assert not geometry["documentOverflow"]
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_agent_bootstrap_replaces_ready_cache_without_catalog(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Avoid a second source collection when an older ready status lacks bootstrap data."""
    browser_status_requests: list[str] = []
    source_requests: list[str] = []
    session_url = "https://chatgpt.com/c/bootstrap-cache-session"
    catalog_payload = {
        "platform": "chatgpt",
        "browser_label": "Edge",
        "recent_sessions": [
            {
                "id": "bootstrap-cache-session",
                "title": "Bootstrap cache session",
                "url": session_url,
                "updated_at": "2026-08-30T00:00:00Z",
            }
        ],
        "projects": [],
        "limit": 20,
    }
    fresh_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": catalog_payload,
    }

    def fulfill_browser_status(route) -> None:
        browser_status_requests.append(route.request.url)
        route.fulfill(json=fresh_status)

    def fulfill_sources(route) -> None:
        source_requests.append(route.request.url)
        route.fulfill(json=catalog_payload)

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    cache_key = "cachelikes:browser-session:v8:agent:chatgpt:edge"
    cached_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Cached ChatGPT status",
        "agent_sources_error": "Stale catalog failure",
    }
    page.add_init_script(
        f"sessionStorage.setItem({json.dumps(cache_key)}, "
        f"JSON.stringify({{cached_at: Date.now(), payload: {json.dumps(cached_status)}}}));"
    )
    page.route(
        "**/api/agent/status",
        lambda route: route.fulfill(json=_finished_chatgpt_agent_payload()),
    )
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route("**/api/agent/sources**", fulfill_sources)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        expect(
            page.locator(
                f'[data-agent-execution-session-list] '
                f'[data-recent-conversation-url="{session_url}"]'
            )
        ).to_have_count(1)
        assert len(browser_status_requests) == 1
        assert source_requests == []
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_fresh_grok_bootstrap_supersedes_a_stale_cached_catalog_error(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    source_requests: list[str] = []
    session_url = "https://grok.com/c/fresh-bootstrap-session"
    catalog_payload = {
        "platform": "grok",
        "browser_label": "Edge",
        "recent_sessions": [
            {
                "id": "fresh-bootstrap-session",
                "title": "Fresh Grok session",
                "url": session_url,
                "updated_at": "2026-08-26T12:00:00Z",
            }
        ],
        "projects": [],
        "limit": 20,
    }
    stale_status = {
        "platform": "grok",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "Grok account",
        "message": "Cached Grok status",
        "agent_sources_error": "Stale catalog failure",
    }
    fresh_status = {
        **stale_status,
        "message": "Edge verified an authenticated Grok Web session.",
        "agent_sources_error": "",
        "agent_sources": catalog_payload,
    }
    agent_payload = _finished_chatgpt_agent_payload()
    agent_payload["agent"] = {
        **agent_payload["agent"],
        "platform": "grok",
        "model": "grok-build",
        "actual_model": "Build Beta",
        "conversation_url": session_url,
    }

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    cache_key = "cachelikes:browser-session:v8:agent:grok:edge"
    cache_entry = json.dumps(
        {"cached_at": 0, "payload": stale_status},
        ensure_ascii=False,
    )
    page.add_init_script(
        f"sessionStorage.setItem({json.dumps(cache_key)}, {json.dumps(cache_entry)});"
        "const value = JSON.parse(sessionStorage.getItem("
        f"{json.dumps(cache_key)}));"
        "value.cached_at = Date.now() - 360000;"
        f"sessionStorage.setItem({json.dumps(cache_key)}, JSON.stringify(value));"
    )
    def fulfill_sources(route) -> None:
        source_requests.append(route.request.url)
        route.fulfill(json=catalog_payload)

    page.route("**/api/agent/status", lambda route: route.fulfill(json=agent_payload))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=fresh_status))
    page.route("**/api/agent/sources**", fulfill_sources)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/grok", wait_until="domcontentloaded")
        fresh_option = page.locator(
            f'[data-agent-execution-session-list] '
            f'[data-recent-conversation-url="{session_url}"]'
        )
        expect(fresh_option).to_have_count(1)
        expect(fresh_option).to_contain_text("Fresh Grok session")
        page.wait_for_timeout(300)
        assert source_requests == []
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_stale_chatgpt_probe_failure_cannot_overwrite_grok_ready_state(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    pending_chatgpt_routes = []
    browser_session_requests: list[str] = []
    grok_ready_message = "Edge verified Grok after the provider switch."
    stale_chatgpt_error = "The superseded ChatGPT probe failed."
    grok_catalog = {
        "platform": "grok",
        "browser_label": "Edge",
        "recent_sessions": [],
        "projects": [],
        "limit": 20,
    }
    finished_chatgpt_payload = _finished_chatgpt_agent_payload()
    finished_chatgpt_payload["agent"]["prompt"] = "STALE_CHATGPT_PROMPT_SENTINEL"

    def fulfill_browser_status(route) -> None:
        request_url = route.request.url
        browser_session_requests.append(request_url)
        if "platform=chatgpt" in request_url:
            pending_chatgpt_routes.append(route)
            return
        assert "platform=grok" in request_url
        route.fulfill(
            json={
                "platform": "grok",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "Grok account",
                "message": grok_ready_message,
                "agent_sources": grok_catalog,
            }
        )

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route(
        "**/api/agent/status",
        lambda route: route.fulfill(json=finished_chatgpt_payload),
    )
    page.route("**/api/browser-session**", fulfill_browser_status)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        assert len(pending_chatgpt_routes) == 1
        expect(page.locator("#agent_response_output")).to_be_visible()
        expect(page.locator("[data-agent-response-answer-content]")).to_have_text(
            "Read-only inspection finished."
        )

        page.get_by_role("button", name="Web service: ChatGPT", exact=True).click()
        page.locator(
            '.agent-platform-combobox [data-agent-combobox-option="grok"]'
        ).click()

        expect(page.get_by_role("button", name="Web service: Grok", exact=True)).to_be_visible()
        expect(page.locator(".agent-readiness")).to_have_count(0)
        expect(page.locator("#agent_ask_button")).to_be_enabled()
        expect(page.locator("#agent_phase_chip")).to_have_count(0)
        expect(page.locator("#agent_response_output")).to_be_hidden()
        expect(page.locator("[data-agent-response-question]")).to_be_empty()
        expect(page.locator("[data-agent-response-answer-content]")).to_be_empty()
        expect(page.locator("[data-agent-prompt-input]")).to_have_value("")
        activity_panel = page.locator("#agent_activity_panel")
        expect(activity_panel).to_be_visible()
        expect(activity_panel).to_have_js_property("open", False)
        expect(page.locator("#agent_activity_list > .agent-activity-item")).to_have_count(0)
        assert len(browser_session_requests) == 2
        assert "platform=grok" in browser_session_requests[-1]

        with page.expect_response(
            lambda response: "platform=chatgpt" in response.url and response.status == 409
        ):
            pending_chatgpt_routes[0].fulfill(
                status=409,
                json={"error": stale_chatgpt_error},
            )
        page.evaluate("() => new Promise((resolve) => setTimeout(resolve, 0))")

        expect(page.get_by_role("button", name="Web service: Grok", exact=True)).to_be_visible()
        expect(page.locator(".agent-readiness")).to_have_count(0)
        expect(page.locator("#agent_ask_button")).to_be_enabled()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_running_agent_status_shows_elapsed_turn_count_and_activity_time(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep live Agent telemetry readable with a two-line running status."""
    started_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload = _finished_chatgpt_agent_payload()
    payload["agent"] = {
        **payload["agent"],
        "running": True,
        "phase": "running",
        "finished_at": "",
        "started_at": started_at,
        "turn_count": 3,
        "activity": [
            {
                "status": "running",
                "label": "Read",
                "detail": "app/services/very-long-agent-activity-path-that-must-wrap-cleanly.py",
                "meta": "Turn 3",
                "timestamp": "2026-09-02T08:00:00Z",
            }
        ],
        "message": "Controller observation sent; waiting for the next ChatGPT action.",
    }
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
    }
    context = disposable_browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
        is_mobile=True,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", lambda route: route.fulfill(json=payload))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        status = page.locator("#agent_response_status")
        status_copy = page.locator("[data-agent-response-status-copy]")
        activity_meta = page.locator("#agent_activity_list .agent-activity-meta").first
        expect(status).to_contain_text("Working")
        expect(status).to_contain_text("3 turns")
        expect(status_copy.locator("br")).to_have_count(1)
        expect(status_copy.locator("[data-agent-response-status-leading]")).to_have_text(
            re.compile(r"^Working · \d{2}:\d{2}:\d{2} · 3 turns$")
        )
        expect(status_copy.locator("[data-agent-response-status-detail]")).to_have_text(
            "Controller observation sent; waiting for the next ChatGPT action."
        )
        expect(activity_meta).to_have_text("Turn 3 · 16:00:00")
        status_text_before = status_copy.text_content()
        page.wait_for_function(
            """previous => document.querySelector('[data-agent-response-status-copy]')?.textContent !== previous""",
            arg=status_text_before,
        )
        layout = page.evaluate(
            """() => {
                const copy = document.querySelector('[data-agent-response-status-copy]');
                const leading = copy?.querySelector('[data-agent-response-status-leading]');
                const statusDetail = copy?.querySelector('[data-agent-response-status-detail]');
                const spinner = document.querySelector('[data-agent-response-status-spinner]');
                const activityDetail = document.querySelector('#agent_activity_list .agent-activity-detail');
                const read = element => element ? {
                    clientHeight: element.clientHeight,
                    clientWidth: element.clientWidth,
                    lineHeight: Number.parseFloat(getComputedStyle(element).lineHeight),
                    scrollWidth: element.scrollWidth,
                } : null;
                return {
                    copy: read(copy),
                    statusLeadingLeft: leading?.getBoundingClientRect().left || null,
                    statusDetailLeft: statusDetail?.getBoundingClientRect().left || null,
                    spinnerRight: spinner?.getBoundingClientRect().right || null,
                    detail: read(activityDetail),
                };
            }"""
        )
        assert layout["statusLeadingLeft"] == pytest.approx(layout["statusDetailLeft"], abs=1)
        assert layout["statusLeadingLeft"] >= layout["spinnerRight"]
        assert layout["copy"]["clientHeight"] > layout["copy"]["lineHeight"]
        assert layout["copy"]["clientHeight"] <= layout["copy"]["lineHeight"] * 2 + 2
        assert layout["copy"]["scrollWidth"] <= layout["copy"]["clientWidth"] + 1
        assert layout["detail"]["scrollWidth"] <= layout["detail"]["clientWidth"] + 1
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_observed_agent_completion_does_not_refresh_sources(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    source_requests: list[str] = []
    status_requests = 0
    finished_payload = _finished_chatgpt_agent_payload()
    running_payload = {
        **finished_payload,
        "agent": {
            **finished_payload["agent"],
            "running": True,
            "phase": "running",
            "finished_at": "",
            "conversation_url": "",
        },
    }
    catalog_payload = _chatgpt_catalog_sessions()

    def fulfill_agent_status(route) -> None:
        nonlocal status_requests
        status_requests += 1
        route.fulfill(json=running_payload if status_requests == 1 else finished_payload)

    def fulfill_browser_status(route) -> None:
        route.fulfill(
            json={
                "platform": "chatgpt",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "ChatGPT account",
                "message": "Edge is ready for ChatGPT Web.",
            }
        )

    def fulfill_sources(route) -> None:
        source_requests.append(route.request.url)
        route.fulfill(json=catalog_payload)

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route("**/api/agent/sources**", fulfill_sources)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        response_status = page.locator("#agent_response_status")
        response_status_spinner = page.locator("[data-agent-response-status-spinner]")
        expect(response_status).to_be_visible()
        expect(response_status).to_have_attribute("data-status", "running")
        expect(response_status_spinner).to_be_visible()
        expect(response_status).to_have_attribute("data-status", "finished")
        page.wait_for_timeout(2_800)
        assert status_requests >= 2
        assert source_requests == []
        expect(response_status).to_contain_text("Finished")
        expect(response_status_spinner).to_be_hidden()
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_successful_agent_completion_collapses_activity_without_erasing_a_new_draft(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Keep completed tool turns available on demand while returning focus to the answer."""
    completed_prompt = " ".join(
        [
            "Review the Agent implementation and report the completed verification.",
            "Preserve the exact source evidence, current tests, and known limitations.",
        ]
        * 12
    )
    activity = [
        {
            "status": "complete",
            "label": ("Read", "Replace", "Run")[(turn - 1) % 3],
            "detail": f"tests/e2e/critical-flows-{turn}.spec.mjs",
            "meta": f"Turn {turn}",
        }
        for turn in range(1, 70)
    ]
    finished_payload = _finished_chatgpt_agent_payload()
    finished_agent = {
        **finished_payload["agent"],
        "run_id": "run-hydrated-finished",
        "run_revision": 7,
        "prompt": completed_prompt,
        "response": "Completed Agent answer.",
        "response_html": "<p>Completed Agent answer.</p>",
        "history": [
            {
                "prompt": completed_prompt,
                "response": "Completed Agent answer.",
                "response_html": "<p>Completed Agent answer.</p>",
            }
        ],
        "activity": activity,
    }
    finished_payload["agent"] = finished_agent
    running_payload = {
        **finished_payload,
        "agent": {
            **finished_agent,
            "running": True,
            "phase": "running",
            "finished_at": "",
            "conversation_url": "",
        },
    }
    catalog_payload = _chatgpt_catalog_sessions()
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": catalog_payload,
    }
    status_requests = 0

    def fulfill_agent_status(route) -> None:
        nonlocal status_requests
        status_requests += 1
        route.fulfill(json=running_payload if status_requests == 1 else finished_payload)

    context = disposable_browser.new_context(
        viewport={"width": 1_008, "height": 1_085},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(json=browser_status),
    )
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(json=catalog_payload),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        activity_panel = page.locator("#agent_activity_panel")
        prompt = page.locator("#agent_prompt_input")
        expect(activity_panel).to_have_count(1)
        expect(activity_panel).to_have_js_property("open", True)
        expect(page.locator("#agent_activity_list > .agent-activity-item")).to_have_count(69)
        page.evaluate(
            """value => {
                document.querySelector('#agent_prompt_input').value = value;
            }""",
            completed_prompt,
        )

        page.wait_for_function(
            """expectedPrompt => {
                const panel = document.querySelector('#agent_activity_panel');
                const prompt = document.querySelector('#agent_prompt_input');
                const question = document.querySelector('[data-agent-response-question]');
                return panel instanceof HTMLDetailsElement
                    && !panel.open
                    && prompt instanceof HTMLTextAreaElement
                    && prompt.value === ''
                    && question?.textContent === expectedPrompt;
            }""",
            arg=completed_prompt,
        )
        expect(page.locator("#agent_activity_list")).to_be_hidden()

        question_layout = page.evaluate(
            """() => {
                const header = document.querySelector('#agent_response_question_header');
                const question = document.querySelector('[data-agent-response-question]');
                const output = document.querySelector('#agent_response_output');
                const composer = document.querySelector('#agent_prompt_form');
                const answer = document.querySelector('#agent_response_answer');
                const headerRect = header?.getBoundingClientRect();
                const outputRect = output?.getBoundingClientRect();
                const composerRect = composer?.getBoundingClientRect();
                return {
                    headerClientHeight: header?.clientHeight,
                    questionClientHeight: question?.clientHeight,
                    questionScrollHeight: question?.scrollHeight,
                    questionClientWidth: question?.clientWidth,
                    questionScrollWidth: question?.scrollWidth,
                    questionFontWeight: question ? getComputedStyle(question).fontWeight : null,
                    outputBottom: outputRect?.bottom,
                    composerBottom: composerRect?.bottom,
                    composerHeight: composerRect?.height,
                    answerBottomPadding: answer ? Number.parseFloat(getComputedStyle(answer).paddingBottom) : null,
                    headerBottom: headerRect?.bottom,
                    horizontalOverflow: Math.max(
                        document.documentElement.scrollWidth,
                        document.body.scrollWidth,
                    ) - document.documentElement.clientWidth,
                };
            }"""
        )
        assert question_layout["questionScrollHeight"] > question_layout["questionClientHeight"]
        assert question_layout["questionScrollWidth"] <= question_layout["questionClientWidth"] + 1
        assert question_layout["questionFontWeight"] == "500"
        assert question_layout["outputBottom"] == pytest.approx(question_layout["composerBottom"], abs=1)
        assert question_layout["answerBottomPadding"] >= question_layout["composerHeight"] + 12
        assert question_layout["horizontalOverflow"] <= 1

        activity_panel.locator("summary").click()
        expect(activity_panel).to_have_js_property("open", True)
        expect(page.locator("#agent_activity_list")).to_be_visible()
        activity_motion = page.locator("#agent_activity_list").evaluate(
            "element => ({animationName: getComputedStyle(element).animationName})"
        )
        assert activity_motion["animationName"] == "agent-activity-gel-open"
        prompt.fill("A new task draft")
        page.wait_for_function(
            """() => window.performance.getEntriesByType('resource').filter((entry) =>
                String(entry.name || '').includes('/api/agent/status')
            ).length >= 3"""
        )
        assert status_requests >= 3
        expect(activity_panel).to_have_js_property("open", True)
        expect(prompt).to_have_value("A new task draft")
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("edit_same_text", (False, True))
def test_hydrated_running_agent_handles_the_first_finished_status_without_losing_a_same_text_draft(
    disposable_browser: Browser,
    sidebar_server_url: str,
    edit_same_text: bool,
) -> None:
    """Use the SSR run identity when the first client status already reports completion."""
    completed_prompt = "Verify the first completed Agent status after hydration."
    finished_payload = _finished_chatgpt_agent_payload()
    finished_agent = {
        **finished_payload["agent"],
        "run_id": "run-hydrated-finished",
        "run_revision": 7,
        "prompt": completed_prompt,
        "response": "Hydrated completion answer.",
        "response_html": "<p>Hydrated completion answer.</p>",
        "history": [
            {
                "prompt": completed_prompt,
                "response": "Hydrated completion answer.",
                "response_html": "<p>Hydrated completion answer.</p>",
            }
        ],
        "activity": [
            {
                "status": "complete",
                "label": "Read",
                "detail": "tests/test_sidebar_e2e.py",
                "meta": "Turn 1",
            }
        ],
    }
    finished_payload["agent"] = finished_agent
    run_identity = str(finished_agent["run_id"])
    catalog_payload = _chatgpt_catalog_sessions()
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": catalog_payload,
    }
    pending_status_routes = []

    def hydrate_running_markup(route) -> None:
        response = route.fetch()
        body = response.text()
        body, running_replacements = re.subn(
            (
                r'data-agent-running="[^"]*" data-agent-run-id="[^"]*" '
                r'data-agent-run-revision="[^"]*" data-agent-started-at="[^"]*"'
            ),
            (
                f'data-agent-running="true" data-agent-run-id="{run_identity}" '
                f'data-agent-run-revision="{finished_agent["run_revision"]}" '
                f'data-agent-started-at="{finished_agent["started_at"]}"'
            ),
            body,
            count=1,
        )
        assert running_replacements == 1
        body = body.replace(
            'data-agent-prompt-input required></textarea>',
            f'data-agent-prompt-input required>{completed_prompt}</textarea>',
            1,
        )
        body, activity_replacements = re.subn(
            r'<details class="agent-activity-panel" id="agent_activity_panel">',
            '<details class="agent-activity-panel" id="agent_activity_panel" open>',
            body,
            count=1,
        )
        assert activity_replacements == 1
        route.fulfill(response=response, body=body)

    context = disposable_browser.new_context(
        viewport={"width": 1_008, "height": 1_085},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/agent/edge/chatgpt", hydrate_running_markup)
    page.route("**/api/agent/status", lambda route: pending_status_routes.append(route))
    page.route(
        "**/api/browser-session**",
        lambda route: route.fulfill(json=browser_status),
    )
    page.route(
        "**/api/agent/sources**",
        lambda route: route.fulfill(json=catalog_payload),
    )
    try:
        with page.expect_request(
            lambda request: "/api/agent/status" in request.url,
        ):
            page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        prompt = page.locator("#agent_prompt_input")
        activity_panel = page.locator("#agent_activity_panel")
        expect(prompt).to_have_value(completed_prompt)
        expect(activity_panel).to_have_js_property("open", True)
        assert len(pending_status_routes) == 1
        if edit_same_text:
            prompt.fill(completed_prompt)
        pending_status_routes[0].fulfill(json=finished_payload)

        expected_prompt = completed_prompt if edit_same_text else ""
        page.wait_for_function(
            """expected => {
                const page = document.querySelector('[data-agent-route-prefix]');
                const panel = document.querySelector('#agent_activity_panel');
                const prompt = document.querySelector('#agent_prompt_input');
                const question = document.querySelector('[data-agent-response-question]');
                return page?.dataset.agentRunning === 'false'
                    && panel instanceof HTMLDetailsElement
                    && !panel.open
                    && prompt instanceof HTMLTextAreaElement
                    && prompt.value === expected.prompt
                    && question?.textContent === expected.question;
            }""",
            arg={"prompt": expected_prompt, "question": completed_prompt},
        )
        expect(page.locator("#agent_activity_list > .agent-activity-item")).to_have_count(1)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_late_prior_run_status_cannot_overwrite_the_new_running_agent_draft(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """Reject a delayed terminal snapshot from a prior Agent run."""
    base_payload = _finished_chatgpt_agent_payload()
    prior_agent = {
        **base_payload["agent"],
        "run_id": "run-prior",
        "run_revision": 41,
        "started_at": "2026-08-26T10:00:00Z",
        "finished_at": "",
        "running": True,
        "phase": "running",
        "prompt": "Prior run prompt.",
        "response": "",
        "response_html": "",
        "activity": [{"status": "running", "label": "Prior running event", "detail": "", "meta": ""}],
    }
    current_agent = {
        **prior_agent,
        "run_id": "run-current",
        "run_revision": 42,
        "prompt": "Current run prompt.",
        "response": "",
        "response_html": "",
        "activity": [{"status": "running", "label": "Current running event", "detail": "", "meta": ""}],
    }
    prior_payload = {**base_payload, "agent": prior_agent}
    prior_terminal_payload = {
        **base_payload,
        "agent": {
            **prior_agent,
            "running": False,
            "phase": "finished",
            "finished_at": "2026-08-26T10:00:01Z",
            "response": "Prior terminal answer.",
            "response_html": "<p>Prior terminal answer.</p>",
            "activity": [{"status": "complete", "label": "Prior terminal event", "detail": "", "meta": ""}],
        },
    }
    current_payload = {**base_payload, "agent": current_agent}
    catalog_payload = _chatgpt_catalog_sessions()
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": catalog_payload,
    }
    status_requests = 0

    def fulfill_agent_status(route) -> None:
        nonlocal status_requests
        status_requests += 1
        route.fulfill(
            json=(
                prior_payload
                if status_requests == 1
                else current_payload
                if status_requests == 2
                else prior_terminal_payload
            )
        )

    context = disposable_browser.new_context(
        viewport={"width": 1_008, "height": 1_085},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog_payload))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        prompt = page.locator("#agent_prompt_input")
        activity_panel = page.locator("#agent_activity_panel")
        page.wait_for_function(
            """() => document.querySelector('[data-agent-route-prefix]')?.dataset.agentRunId === 'run-prior'"""
        )
        expect(activity_panel).to_have_js_property("open", True)
        expect(page.locator("#agent_activity_list")).to_contain_text("Prior running event")

        page.wait_for_function(
            """() => window.performance.getEntriesByType('resource').filter((entry) =>
                String(entry.name || '').includes('/api/agent/status')
            ).length >= 2"""
        )
        expect(page.locator("[data-agent-route-prefix]")).to_have_attribute("data-agent-run-id", "run-current")
        expect(page.locator("[data-agent-route-prefix]")).to_have_attribute("data-agent-run-revision", "42")
        expect(page.locator("#agent_activity_list")).to_contain_text("Current running event")
        prompt.fill("Keep this current-run draft.")

        page.wait_for_function(
            """() => window.performance.getEntriesByType('resource').filter((entry) =>
                String(entry.name || '').includes('/api/agent/status')
            ).length >= 3"""
        )
        assert status_requests >= 3
        expect(page.locator("[data-agent-route-prefix]")).to_have_attribute("data-agent-run-id", "run-current")
        expect(page.locator("[data-agent-route-prefix]")).to_have_attribute("data-agent-running", "true")
        expect(activity_panel).to_have_js_property("open", True)
        expect(page.locator("#agent_activity_list")).to_contain_text("Current running event")
        expect(prompt).to_have_value("Keep this current-run draft.")
        page.wait_for_function(
            """() => window.performance.getEntriesByType('resource').filter((entry) =>
                String(entry.name || '').includes('/api/agent/status')
            ).length >= 4""",
            timeout=1_800,
        )
        assert status_requests >= 4
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_superseding_finished_run_never_clears_an_idle_local_draft(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """A terminal snapshot from another run must not mutate an unsent draft."""
    idle_draft = "Keep this unsent idle draft."
    base_payload = _finished_chatgpt_agent_payload()
    prior_agent = {
        **base_payload["agent"],
        "run_id": "run-prior",
        "started_at": "2026-08-26T09:00:00Z",
        "finished_at": "2026-08-26T09:01:00Z",
        "prompt": "Prior completed prompt.",
        "response": "Prior completed answer.",
        "response_html": "<p>Prior completed answer.</p>",
    }
    later_agent = {
        **prior_agent,
        "run_id": "run-later",
        "started_at": "2026-08-26T10:00:00Z",
        "finished_at": "2026-08-26T10:01:00Z",
        "prompt": idle_draft,
        "response": "Later completed answer.",
        "response_html": "<p>Later completed answer.</p>",
    }
    later_running_agent = {
        **later_agent,
        "running": True,
        "phase": "running",
        "finished_at": "",
    }
    prior_payload = {**base_payload, "agent": prior_agent}
    later_running_payload = {**base_payload, "agent": later_running_agent}
    later_payload = {**base_payload, "agent": later_agent}
    catalog_payload = _chatgpt_catalog_sessions()
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": catalog_payload,
    }
    status_requests = 0

    def fulfill_agent_status(route) -> None:
        nonlocal status_requests
        status_requests += 1
        route.fulfill(
            json=(
                prior_payload
                if status_requests == 1
                else later_running_payload
                if status_requests == 2
                else later_payload
            )
        )

    context = disposable_browser.new_context(
        viewport={"width": 1_008, "height": 1_085},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog_payload))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        prompt = page.locator("#agent_prompt_input")
        page.wait_for_function(
            """() => document.querySelector('[data-agent-route-prefix]')?.dataset.agentRunId === 'run-prior'"""
        )
        prompt.fill(idle_draft)

        page.wait_for_function(
            """() => window.performance.getEntriesByType('resource').filter((entry) =>
                String(entry.name || '').includes('/api/agent/status')
            ).length >= 3"""
        )
        assert status_requests >= 3
        expect(page.locator("[data-agent-route-prefix]")).to_have_attribute("data-agent-run-id", "run-later")
        expect(prompt).to_have_value(idle_draft)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_rejected_ask_keeps_the_draft_when_an_old_finished_status_arrives(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """A failed Ask must clear its pending marker before the next status poll."""
    rejected_prompt = "Keep this draft after the rejected Ask request."
    finished_payload = _finished_chatgpt_agent_payload()
    finished_payload["agent"] = {
        **finished_payload["agent"],
        "run_id": "run-old-finished",
        "prompt": "Old finished prompt.",
        "response": "Old finished answer.",
        "response_html": "<p>Old finished answer.</p>",
    }
    catalog_payload = _chatgpt_catalog_sessions()
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": catalog_payload,
    }
    status_requests = 0

    def fulfill_agent_status(route) -> None:
        nonlocal status_requests
        status_requests += 1
        route.fulfill(json=finished_payload)

    context = disposable_browser.new_context(
        viewport={"width": 1_008, "height": 1_085},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog_payload))
    page.route(
        "**/api/agent/ask",
        lambda route: route.fulfill(status=409, json={"error": "Ask request was rejected."}),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        prompt = page.locator("#agent_prompt_input")
        ask = page.locator("#agent_ask_button")
        expect(ask).to_be_enabled()
        prompt.fill(rejected_prompt)
        with page.expect_response(
            lambda response: "/api/agent/ask" in response.url and response.status == 409
        ):
            ask.click()

        page.wait_for_function(
            """() => window.performance.getEntriesByType('resource').filter((entry) =>
                String(entry.name || '').includes('/api/agent/status')
            ).length >= 2"""
        )
        assert status_requests >= 2
        expect(prompt).to_have_value(rejected_prompt)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_successful_ask_acknowledges_a_distinct_same_second_run_id(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    """The Ask response is authoritative when two run starts share one second."""
    submitted_prompt = "Accept this same-second Agent run acknowledgement."
    base_payload = _finished_chatgpt_agent_payload()
    prior_agent = {
        **base_payload["agent"],
        "run_id": "run-prior-same-second",
        "started_at": "2026-08-26T10:00:00Z",
        "finished_at": "2026-08-26T10:00:01Z",
        "prompt": "Prior completed prompt.",
        "response": "Prior completed answer.",
        "response_html": "<p>Prior completed answer.</p>",
    }
    acknowledged_agent = {
        **prior_agent,
        "run_id": "run-acknowledged-same-second",
        "finished_at": "2026-08-26T10:00:02Z",
        "prompt": submitted_prompt,
        "response": "Acknowledged current answer.",
        "response_html": "<p>Acknowledged current answer.</p>",
    }
    prior_payload = {**base_payload, "agent": prior_agent}
    acknowledged_payload = {**base_payload, "agent": acknowledged_agent}
    catalog_payload = _chatgpt_catalog_sessions()
    browser_status = {
        "platform": "chatgpt",
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "ChatGPT account",
        "message": "Edge is ready for ChatGPT Web.",
        "agent_sources": catalog_payload,
    }

    context = disposable_browser.new_context(
        viewport={"width": 1_008, "height": 1_085},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", lambda route: route.fulfill(json=prior_payload))
    page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_status))
    page.route("**/api/agent/sources**", lambda route: route.fulfill(json=catalog_payload))
    page.route("**/api/agent/ask", lambda route: route.fulfill(json=acknowledged_payload))
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        page.wait_for_function(
            """() => document.querySelector('[data-agent-route-prefix]')?.dataset.agentRunId === 'run-prior-same-second'"""
        )
        prompt = page.locator("#agent_prompt_input")
        ask = page.locator("#agent_ask_button")
        expect(ask).to_be_enabled()
        prompt.fill(submitted_prompt)
        with page.expect_response(
            lambda response: "/api/agent/ask" in response.url and response.status == 200
        ):
            ask.click()

        page.wait_for_function(
            """expected => {
                const agentPage = document.querySelector('[data-agent-route-prefix]');
                const prompt = document.querySelector('#agent_prompt_input');
                const question = document.querySelector('[data-agent-response-question]');
                return agentPage?.dataset.agentRunId === expected.runId
                    && agentPage.dataset.agentRunning === 'false'
                    && prompt instanceof HTMLTextAreaElement
                    && prompt.value === ''
                    && question?.textContent === expected.prompt;
            }""",
            arg={"runId": "run-acknowledged-same-second", "prompt": submitted_prompt},
        )
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_missing_snapshot_url_is_not_synthesized_into_session_catalog(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    def fulfill_agent_status(route) -> None:
        route.fulfill(json=_finished_chatgpt_agent_payload())

    def fulfill_browser_status(route) -> None:
        route.fulfill(
            json={
                "platform": "chatgpt",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "ChatGPT account",
                "message": "Edge is ready for ChatGPT Web.",
            }
        )

    def fulfill_preferences(route) -> None:
        route.fulfill(json=_finished_chatgpt_agent_payload())

    def fulfill_sources(route) -> None:
        route.fulfill(
            json=_chatgpt_catalog_sessions(
                {
                    "id": "agentic-troubleshooting",
                    "title": "Agentic Troubleshooting",
                    "url": AGENTIC_TROUBLESHOOTING_URL,
                    "updated_at": "2026-08-26T01:00:00Z",
                }
            )
        )

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route("**/api/agent/preferences", fulfill_preferences)
    page.route("**/api/agent/sources**", fulfill_sources)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        expect(page.locator("[data-agent-prompt-session-mode]")).to_have_value("new")
        expect(page.locator("[data-agent-prompt-conversation-url]")).to_have_value("")
        expect(
            page.locator(
                f'[data-agent-execution-session-list] '
                f'[data-recent-conversation-url="{FINISHED_SNAPSHOT_URL}"]'
            )
        ).to_have_count(0)
        expect(
            page.locator(
                f'[data-agent-execution-session-list] '
                f'[data-recent-conversation-url="{AGENTIC_TROUBLESHOOTING_URL}"]'
            )
        ).to_have_count(1)
        expect(page.locator("[data-agent-prompt-conversation-url]")).to_have_value("")
        expect(page.locator("#agent_conversation_link")).to_have_attribute("href", FINISHED_SNAPSHOT_URL)
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
def test_explicit_agentic_troubleshooting_session_is_the_only_reused_target(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> None:
    captured_ask_payloads: list[dict[str, str]] = []

    def fulfill_agent_status(route) -> None:
        route.fulfill(json=_finished_chatgpt_agent_payload())

    def fulfill_browser_status(route) -> None:
        route.fulfill(
            json={
                "platform": "chatgpt",
                "browser": "edge",
                "browser_label": "Edge",
                "logged_in": True,
                "can_download": True,
                "account_name": "ChatGPT account",
                "message": "Edge is ready for ChatGPT Web.",
            }
        )

    def fulfill_preferences(route) -> None:
        route.fulfill(json=_finished_chatgpt_agent_payload())

    def fulfill_sources(route) -> None:
        route.fulfill(
            json=_chatgpt_catalog_sessions(
                {
                    "id": "qqqm-session",
                    "title": "比较 QQQM 与 QQQ",
                    "url": FINISHED_SNAPSHOT_URL,
                    "updated_at": "2026-08-25T09:03:57Z",
                },
                {
                    "id": "agentic-troubleshooting",
                    "title": "Agentic Troubleshooting",
                    "url": AGENTIC_TROUBLESHOOTING_URL,
                    "updated_at": "2026-08-26T01:00:00Z",
                },
            )
        )

    def fulfill_history(route) -> None:
        route.fulfill(
            json={
                "title": "Agentic Troubleshooting",
                "history": [],
            }
        )

    def fulfill_ask(route) -> None:
        captured_ask_payloads.append(route.request.post_data_json or {})
        route.fulfill(json=_finished_chatgpt_agent_payload())

    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 720},
        has_touch=False,
        is_mobile=False,
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.route("**/api/agent/status", fulfill_agent_status)
    page.route("**/api/browser-session**", fulfill_browser_status)
    page.route("**/api/agent/preferences", fulfill_preferences)
    page.route("**/api/agent/sources**", fulfill_sources)
    page.route("**/api/agent/chatgpt-session-history**", fulfill_history)
    page.route("**/api/agent/ask", fulfill_ask)
    try:
        page.goto(f"{sidebar_server_url}/agent/edge/chatgpt", wait_until="domcontentloaded")
        expect(page.locator("[data-agent-prompt-session-mode]")).to_have_value("new")
        page.locator(
            f'[data-agent-execution-session-list] '
            f'[data-recent-conversation-url="{AGENTIC_TROUBLESHOOTING_URL}"]'
        ).click()
        expect(page.locator("[data-agent-prompt-session-mode]")).to_have_value("recent")
        expect(page.locator("[data-agent-prompt-conversation-url]")).to_have_value(
            AGENTIC_TROUBLESHOOTING_URL
        )
        expect(page.locator("[data-agent-prompt-session-title]")).to_have_value(
            "Agentic Troubleshooting"
        )
        page.locator("[data-agent-prompt-input]").fill("Continue the existing troubleshooting session.")
        with page.expect_response(re.compile(r"/api/agent/ask$")):
            page.locator("#agent_ask_button").click()
        assert captured_ask_payloads
        payload = captured_ask_payloads[0]
        assert payload["session_mode"] == "recent"
        assert payload["conversation_url"] == AGENTIC_TROUBLESHOOTING_URL
        assert payload["session_title"] == "Agentic Troubleshooting"
        assert payload["conversation_url"] != FINISHED_SNAPSHOT_URL
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize('width', [1_021, 375])
def test_agent_bootstrap_discovers_models_efforts_and_restores_markdown_once(
    disposable_browser: Browser,
    sidebar_server_url: str,
    width: int,
) -> None:
    from app.core.agent_model_catalog import chatgpt_live_catalog
    from app.web.app import render_agent_response

    raw = json.dumps({'action': 'final', 'summary': '## Restored result\n\n**Verified**\n\n- One launch'})
    payload = _finished_chatgpt_agent_payload()
    payload['agent'].update(response=raw, response_html=str(render_agent_response(raw)), history=[])
    status = {
        'platform': 'chatgpt', 'browser': 'edge', 'can_download': True,
        'account_name': 'ChatGPT account', 'browser_label': 'Edge',
        'agent_sources': _chatgpt_catalog_sessions(),
        'model_verified': True, 'model_catalog_complete': True,
        'actual_model': 'GPT-12 Nebula',
        'model_options': chatgpt_live_catalog(['GPT-5.6 Sol', 'GPT-12 Nebula']),
        'available_efforts': ['Measured', 'Exhaustive'],
        'thinking_effort': 'Exhaustive', 'effort_catalog_complete': True,
        'browser_session_freshness': {
            'kind': 'live_browser', 'cache_status': 'miss',
            'cached_at': '2026-09-05T00:00:00Z', 'age_seconds': 0,
        },
    }
    requests = []
    context = disposable_browser.new_context(viewport={'width': width, 'height': 863})
    page = context.new_page()
    page.route('**/api/agent/status', lambda route: route.fulfill(json=payload))

    def bootstrap(route):
        requests.append(route.request.url)
        route.fulfill(json=status)

    page.route('**/api/browser-session**', bootstrap)
    try:
        page.goto(f'{sidebar_server_url}/agent/edge/chatgpt', wait_until='domcontentloaded')
        expect(page.get_by_role('button', name='Model: GPT-12 Nebula', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='Option: Exhaustive', exact=True)).to_be_visible()
        expect(page.locator('#agent_response_answer h2')).to_have_text('Restored result')
        expect(page.locator('#agent_response_answer strong')).to_have_text('Verified')
        expect(page.locator('[data-agent-effort-input]')).to_have_value('highest_available')
        assert len(requests) == 1
        assert 'refresh=1' in requests[0]
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.reload(wait_until='domcontentloaded')
        expect(page.get_by_role('button', name='Model: GPT-12 Nebula', exact=True)).to_be_visible()
        assert len(requests) == 1
    finally:
        context.close()


@pytest.mark.integration
def test_chatgpt_latest_alias_with_combined_model_and_power_menu(disposable_browser: Browser) -> None:
    """Replay the observed Latest/Select model menu with its hidden ARIA slider."""
    from app.core.computer_use_agent import _select_chatgpt_model

    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content('''
            <form data-type="unified-composer">
              <div id="prompt-textarea" contenteditable="true">Ask ChatGPT</div>
              <button type="button" class="__composer-pill" aria-haspopup="menu"
                aria-controls="power-menu" aria-expanded="false">Medium</button>
            </form>
            <div role="menu" id="power-menu" hidden>
              <div data-testid="composer-intelligence-picker-content" data-model-selection-view="true">
                <div role="menuitem" tabindex="0" aria-label="Select model">Medium</div>
                <div data-testid="composer-model-picker-slider-simple-view">
                  <div role="menuitem" tabindex="0" aria-label="Power" aria-describedby="announcement">
                    <div data-model-reasoning-effort-slider>
                      <span role="slider" tabindex="-1" aria-hidden="true" aria-valuemin="0"
                        aria-valuemax="3" aria-valuenow="1" style="display:block;width:100px;height:20px"></span>
                    </div>
                  </div>
                  <span id="announcement">Medium, 2 of 4.</span>
                </div>
                <div data-testid="composer-model-picker-slider-advanced-view" inert>
                  <div role="menuitemradio" aria-checked="true">Latest</div>
                  <div role="menuitemradio" aria-checked="false">GPT-5.6 Sol</div>
                  <div role="menuitemradio" aria-checked="false" aria-disabled="true">GPT-99 Example</div>
                </div>
              </div>
            </div>
            <script>
              const trigger = document.querySelector('button');
              const menu = document.querySelector('[role=menu]');
              const simple = document.querySelector('[data-testid$=simple-view]');
              const advanced = document.querySelector('[data-testid$=advanced-view]');
              trigger.onclick = () => {
                menu.hidden = !menu.hidden;
                trigger.setAttribute('aria-expanded', String(!menu.hidden));
              };
              document.querySelector('[aria-label="Select model"]').onclick = () => {
                advanced.inert = !advanced.inert;
                simple.inert = !advanced.inert;
              };
              document.querySelector('[role=slider]').onkeydown = event => {
                const slider = event.currentTarget;
                let value = Number(slider.getAttribute('aria-valuenow'));
                if (event.key === 'Home') value = 0;
                else if (event.key === 'End') value = 3;
                else if (event.key === 'ArrowRight') value = Math.min(3, value + 1);
                else if (event.key === 'ArrowLeft') value = Math.max(0, value - 1);
                else return;
                slider.setAttribute('aria-valuenow', value);
                const label = ['Light', 'Medium', 'High', 'Maximum'][value];
                document.querySelector('#announcement').textContent = `${label}, ${value + 1} of 4.`;
                trigger.textContent = label;
                event.preventDefault();
              };
            </script>
        ''')
        observation = {}
        assert _select_chatgpt_model(page, 'chromium', 'latest_available', observation), observation
        assert observation['observed'] == 'Latest'
        assert observation['available_efforts'] == ['Light', 'Medium', 'High', 'Maximum']
        assert observation['thinking_effort'] == 'Maximum'
        assert [option['label'] for option in observation['model_options']] == ['Latest', 'GPT-5.6 Sol']
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize("width", [1_024, 390])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_modal_reuses_the_unmodified_frosted_material(disposable_browser, sidebar_server_url, width, scheme):
    context = disposable_browser.new_context(viewport={"width": width, "height": 863}, color_scheme=scheme)
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/settings/style-tokens", wait_until="domcontentloaded")
        material = page.locator(".workspace-modal-dialog.style-token-modal-demo").evaluate(
            """node => {
                const probe = document.createElement('div');
                probe.style.cssText = 'background:var(--frosted-glass-background);backdrop-filter:var(--frosted-glass-blur)';
                node.append(probe);
                const result = {
                    background: getComputedStyle(node).background,
                    expected: getComputedStyle(probe).background,
                    blur: getComputedStyle(node).backdropFilter,
                    expectedBlur: getComputedStyle(probe).backdropFilter,
                    extraLayer: getComputedStyle(node, '::after').content,
                };
                probe.remove();
                return result;
            }"""
        )
        assert material["background"] == material["expected"]
        assert material["blur"] == material["expectedBlur"]
        assert material["extraLayer"] == "none"
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("width", (1138, 390))
def test_cache_annotations_keep_motion_icons_and_remaining_space(
    disposable_browser: Browser, sidebar_server_url: str, width: int,
) -> None:
    """Measure the annotated marks, loading gap, and responsive event scrollport."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 959}, reduced_motion="no-preference",
    )
    page = context.new_page()
    try:
        page.goto(f"{sidebar_server_url}/cache/chatgpt", wait_until="domcontentloaded")
        if width <= 900:
            page.locator("#sidebar_toggle").click()
        marker = page.locator("#phase_chip")
        rings = marker.evaluate("""element => {
            const core = getComputedStyle(element);
            const outer = getComputedStyle(element, '::before');
            const inner = getComputedStyle(element, '::after');
            return {core: core.width, outer: outer.width, inner: inner.width,
                duration: outer.animationDuration, delay: inner.animationDelay,
                start: outer.transform, shadow: core.boxShadow};
        }""")
        assert (rings["core"], rings["outer"], rings["inner"]) == ("6px", "20px", "14px")
        assert rings["duration"] == "1.8s"
        assert rings["delay"] == "0.9s"
        assert rings["shadow"] != "none"
        page.wait_for_timeout(250)
        assert marker.evaluate("el => getComputedStyle(el, '::before').transform") != rings["start"]
        assert page.locator(".cache-settings-link").evaluate(
            "el => getComputedStyle(el).marginBottom"
        ) == "8px"
        # Reveal only the loading fixture; it must not dispatch a remote catalog request.
        page.evaluate("""() => {
            document.querySelector('[data-chatgpt-media-config]').hidden = false;
            document.querySelector('[data-chatgpt-project-spinner]').hidden = false;
        }""")
        gap = page.evaluate("""() => {
            const label = document.querySelector('[data-chatgpt-project-selected-label]').getBoundingClientRect();
            const spinner = document.querySelector('[data-chatgpt-project-spinner]').getBoundingClientRect();
            return spinner.left - label.right;
        }""")
        assert 8 <= gap <= 16
        if width <= 900:
            page.locator("#sidebar_toggle").click()
        expect(page.locator("#activity")).to_have_count(0)
        page.goto(f"{sidebar_server_url}/agent", wait_until="domcontentloaded")
        if width <= 900:
            page.locator("#sidebar_toggle").click()
        page.locator('.agent-session-mode-combobox [data-agent-combobox-trigger]').click()
        option = page.locator('.agent-session-mode-combobox [data-agent-combobox-option="new"]')
        expect(option).to_be_visible()
        delta = option.evaluate("""el => {
            const a = el.querySelector('img').getBoundingClientRect();
            const b = el.querySelector('.trade-strategy-dropdown-check').getBoundingClientRect();
            return Math.abs(a.y + a.height / 2 - b.y - b.height / 2);
        }""")
        assert delta <= 1
        status = page.locator('#agent_response_status')
        status.evaluate("el => el.dataset.status = 'finished'")
        assert status.locator('.agent-response-status-dot').evaluate(
            "el => getComputedStyle(el).maskImage"
        ).endswith('checkmark.circle.fill.svg")')
        assert status.locator('.agent-response-status-dot').evaluate("""el => {
            const reference = document.createElement('span');
            reference.style.color = 'var(--theme-success-strong)';
            el.append(reference);
            const matches = getComputedStyle(el).backgroundColor === getComputedStyle(reference).color;
            reference.remove();
            return matches;
        }""")
    finally:
        context.close()


@pytest.fixture()
def annotated_resources_server_url(tmp_path: Path) -> Iterator[str]:
    from app.core.prompt_store import PromptStore
    from app.core.resource_persistence import GEMINI_HISTORY_SCHEMA
    from app.web.app import create_app

    root = tmp_path / "local-store"
    rows = []
    for index in range(205):
        rows.append({
            "schema_version": 1, "platform": "chatgpt", "conversation_id": "long-session",
            "conversation_url": "https://chatgpt.com/c/long-session", "conversation_title": "Long session",
            "message_key": f"long-session:{index}", "turn_index": index, "message_index": index,
            "role": "user", "author_label": "You", "content_text": "crosspage needle" if index == 204 else f"Message {index}",
            "content_html": "", "content_sha256": str(index), "source_links": [], "model_label": "",
            "first_seen_at": "2026-08-12T06:00:00Z", "last_seen_at": "2026-08-12T05:00:00Z",
        })
    write_parquet_rows_atomic(root / "llm/chatgpt/history.parquet", rows, CHATGPT_HISTORY_SCHEMA)
    other = dict(rows[-1], platform="gemini", conversation_id="other", conversation_title="Other session",
                 conversation_url="https://gemini.google.com/app/other", message_key="other:0")
    write_parquet_rows_atomic(root / "llm/gemini/history.parquet", [other], GEMINI_HISTORY_SCHEMA)
    store = PromptStore(root)
    prompt, _ = store.add_pointer(source="chatgpt", conversation_id="long-session", message_key="long-session:204")
    for remark in ("Research", "A longer standard tag", "Follow up"):
        store.add_remark(prompt.stable_id, remark)
    app = create_app(root, agent_external_operations_enabled=False,
                     computer_use_settings_path=tmp_path / "settings.json",
                     computer_use_runtime_root=tmp_path / "runtime")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1138, 1024, 390])
def test_resource_annotations_search_scope_toolbar_and_remark_bounds(
    disposable_browser: Browser, annotated_resources_server_url: str, width: int,
) -> None:
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    try:
        page.goto(annotated_resources_server_url + "/browser?view=text&source=chatgpt&session=chatgpt:long-session&page=2")
        expect(page.locator('[data-browser-session-tag]')).to_have_text("This session ×")
        assert page.locator('[aria-label="Cached text totals"] .metric-label').all_text_contents() == ["Sessions", "Messages", "Projects"]
        geometry = page.evaluate("""() => {
            const box = selector => {const r = document.querySelector(selector).getBoundingClientRect(); return {x:r.x, y:r.y, right:r.right, width:r.width};};
            return {rail: box('.browser-content-toolbar'), back: box('.browser-session-back-link'),
                    search: box('.browser-search-field'), next: box('.browser-session-neighbor-nav'),
                    share: box('.browser-session-page-export-button')};
        }""")
        assert abs(geometry["rail"]["x"] - geometry["back"]["x"]) <= 1
        assert geometry["share"]["x"] >= geometry["next"]["right"]
        assert geometry["search"]["right"] <= geometry["rail"]["right"] + 1
        assert geometry["search"]["width"] >= 180
        expect(page.locator('.browser-heading-row [data-browser-session-actions]')).to_have_count(0)
        drawer = page.locator('.browser-content-toolbar [data-browser-session-actions]')
        expect(drawer).to_have_count(1)
        drawer.locator('.browser-session-full-export-button').focus()
        expect(drawer.locator('.browser-session-open-original-button')).to_have_css("opacity", "1")
        layout = drawer.evaluate("""root => {
            const share = root.querySelector('.browser-session-full-export-button').getBoundingClientRect();
            const safari = root.querySelector('.browser-session-open-original-button').getBoundingClientRect();
            return {share: share.x, safari: safari.x, right: safari.right,
                    gradient: getComputedStyle(root.querySelector('.browser-session-safari-icon')).backgroundImage};
        }""")
        assert layout["safari"] > layout["share"]
        assert layout["right"] <= width
        assert layout["gradient"] == "none"
        expect(page.locator('[data-browser-search-focus]')).to_have_count(0)
        page.locator('#browser_search_input').click()
        expect(page.locator('#browser_search_input')).to_be_focused()
        page.locator('#browser_search_input').fill("crosspage needle")
        page.locator('#browser_search_input').press("Enter")
        expect(page.locator('[data-chat-message-id]')).to_have_count(1)
        expect(page.locator('[data-browser-session-tag]')).to_be_visible()
        expect(page.locator('.browser-session-detail-table')).to_contain_text("crosspage needle")
        assert "page=2" not in page.url
        page.locator('[data-browser-session-scope-remove]').click()
        expect(page.locator('[data-browser-session-tag]')).to_have_count(0)
        expect(page.locator('[data-chat-message-id]')).to_have_count(2)
        assert "source=all" in page.url and "session=" not in page.url
        page.goto(annotated_resources_server_url + "/browser?view=text&session_view=1")
        expect(page.locator('.browser-session-table tbody tr').first.locator('td').nth(4).locator('.browser-session-message-time-date')).to_be_visible()
        expect(page.locator('.browser-session-table tbody tr').first.locator('td').nth(4).locator('.browser-session-message-time-clock')).to_be_visible()
        page.goto(annotated_resources_server_url + "/browser?view=prompts")
        assert page.locator('[aria-label="Saved prompt totals"] .metric-label').all_text_contents() == ["Saved prompts", "Sources"]
        bounds = page.locator('[data-prompt-remark-input]').evaluate("""input => {
            const r = input.getBoundingClientRect(); const c = input.closest('td').getBoundingClientRect();
            return {radius: getComputedStyle(input).borderRadius, left:r.left, right:r.right, cellLeft:c.left, cellRight:c.right};
        }""")
        assert bounds["radius"] == "10px"
        assert bounds["left"] >= bounds["cellLeft"] and bounds["right"] <= bounds["cellRight"]
        assert page.locator('[data-prompt-tag]').count() == 3
        expect(page.locator('.browser-result-summary')).to_have_count(0)
        assert page.locator('[data-prompt-remark-input]').evaluate("""input => {
            const tags = input.closest('[data-prompt-remarks]').querySelector('[data-prompt-tags]');
            return input.getBoundingClientRect().bottom <= tags.getBoundingClientRect().top
                && getComputedStyle(input).fontFamily === getComputedStyle(document.body).fontFamily;
        }""")
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize('width', [1_021, 375])
def test_agent_bootstrap_replaces_incomplete_cache_before_picker_clicks(
    disposable_browser: Browser, sidebar_server_url: str, width: int,
) -> None:
    from app.core.agent_model_catalog import chatgpt_live_catalog

    status = {
        'platform': 'chatgpt', 'browser': 'edge', 'can_download': True,
        'account_name': 'ChatGPT account', 'browser_label': 'Edge',
        'model_catalog_complete': True, 'actual_model': 'Latest',
        'model_options': chatgpt_live_catalog(['Latest', 'GPT-5.6 Sol']),
        'available_efforts': ['Instant'], 'effort_catalog_complete': False,
        'effort_catalog_error': 'effort-range-changed',
        'browser_session_freshness': {
            'kind': 'stale_cache', 'cache_status': 'stale',
            'cached_at': '2026-09-05T00:00:00Z',
        },
    }
    requests = []
    context = disposable_browser.new_context(viewport={'width': width, 'height': 863})
    page = context.new_page()
    page.add_init_script(
        'sessionStorage.setItem("cachelikes:browser-session:v8:agent:chatgpt:edge",'
        f'JSON.stringify({{cached_at: Date.now(), payload: {json.dumps(status)}}}));'
    )
    page.route('**/api/agent/status', lambda route: route.fulfill(json=_finished_chatgpt_agent_payload()))

    def bootstrap(route):
        requests.append(route.request.url)
        if 'refresh=1' in route.request.url:
            status.update(available_efforts=['Instant', 'Medium', 'High', 'Extra High'],
                          effort_catalog_complete=True, model_verified=True)
            status['browser_session_freshness'].update(kind='live_browser', cache_status='refreshed')
        route.fulfill(json=status)

    page.route('**/api/browser-session**', bootstrap)
    try:
        page.goto(f'{sidebar_server_url}/agent/edge/chatgpt', wait_until='domcontentloaded')
        expect(page.get_by_role('button', name='Option: Extra High', exact=True)).to_be_visible()
        assert len(requests) == 1
        page.get_by_role('button', name='Model: Latest', exact=True).click()
        expect(page.get_by_role('option', name='Latest', exact=True)).to_have_count(1)
        expect(page.get_by_role('option', name='ChatGPT · Latest', exact=True)).to_have_count(0)
        page.get_by_role('option', name='Latest', exact=True).click()
        expect(page.locator('[data-agent-model-input]')).to_have_value('live:latest')
        page.get_by_role('button', name='Option: Extra High', exact=True).click()
        page.get_by_role('option', name='Extra High', exact=True).click()
        expect(page.locator('[data-agent-effort-input]')).to_have_value('Extra High')
        expect(page.get_by_role('button', name='Option: Extra High', exact=True)).to_be_visible()
        assert len(requests) == 1
        assert 'refresh=1' in requests[0]
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    finally:
        context.close()


@pytest.mark.integration
@pytest.mark.parametrize('width', [1_138, 375])
@pytest.mark.parametrize('phase', ['finished', 'failed', 'running'])
def test_agent_activity_completion_icon_contract(disposable_browser, sidebar_server_url, width, phase):
    payload = _finished_chatgpt_agent_payload()
    payload['agent'].update(
        phase=phase, running=phase == 'running',
        activity=[{'status': 'complete', 'label': 'Read', 'detail': 'AGENTS.md'}],
    )
    if phase == 'running':
        payload['agent']['finished_at'] = ''
    if phase == 'failed':
        payload['agent']['error'] = 'Fixture failure'
    context = disposable_browser.new_context(viewport={'width': width, 'height': 959})
    page = context.new_page()
    page.route('**/api/agent/status', lambda route: route.fulfill(json=payload))
    page.route('**/api/browser-session**', lambda route: route.fulfill(json={
        'platform': 'chatgpt', 'browser': 'edge', 'can_download': True,
        'account_name': 'ChatGPT account',
    }))
    try:
        page.goto(f'{sidebar_server_url}/agent/edge/chatgpt', wait_until='domcontentloaded')
        expect(page.locator('#agent_response_status')).to_have_attribute('data-status', phase)
        panel = page.locator('#agent_activity_panel')
        expect(page.locator('.agent-response-toolbar #agent_activity_panel')).to_have_count(1)
        expect(panel.locator('summary #agent_response_status')).to_have_count(1)
        expect(page.locator('.agent-activity-live')).to_have_count(0)
        icons = page.locator('.agent-activity-status').first.evaluate("e => getComputedStyle(e).maskImage")
        assert 'checkmark.circle.svg' in icons
        if panel.evaluate('e => e.open'):
            panel.locator('summary').click()
        expect(panel).to_have_js_property('open', False)
        expect(page.locator('.agent-response-toolbar #agent_activity_current')).to_be_visible()
        expect(page.locator('#agent_activity_current')).to_contain_text('AGENTS.md')
        expect(page.locator('#agent_activity_current > li')).to_have_count(1)
        panel.locator('summary').click()
        expect(panel).to_have_js_property('open', True)
        expect(page.locator('#agent_activity_list')).to_be_visible()
        expect(page.locator('#agent_activity_current')).to_be_hidden()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_cache_text_metrics_and_grok_runtime_boundary(
    disposable_browser: Browser, seeded_chatgpt_browser_server_url: str, width: int,
) -> None:
    context = disposable_browser.new_context(viewport={"width": width, "height": 900})
    context.add_init_script("if (!sessionStorage.getItem('cachelikes:browser-content-mode:v1')) sessionStorage.setItem('cachelikes:browser-content-mode:v1', 'text')")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{seeded_chatgpt_browser_server_url}/cache/chatgpt", wait_until="networkidle")
        expect(page.locator('#cached_sessions')).to_have_text("1")
        expect(page.locator('#cached_messages')).to_have_text("1")
        expect(page.locator('#downloaded_images')).not_to_be_visible()
        assert page.locator('[data-cache-runtime-mode]').first.input_value() == "text"
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.goto(f"{seeded_chatgpt_browser_server_url}/cache/grok", wait_until="networkidle")
        expect(page.locator('#start_button')).to_have_text("Start")
        assert page.locator('[data-cache-runtime-mode]').first.input_value() == "text"
        expect(page.locator('#cached_messages')).to_be_visible()
        expect(page.locator('#downloaded_images')).not_to_be_visible()
        assert page.locator('.sidebar-form-stop [data-cache-runtime-mode]').input_value() == "text"
        page.evaluate("sessionStorage.setItem('cachelikes:browser-content-mode:v1', 'media')")
        page.goto(f"{seeded_chatgpt_browser_server_url}/cache/chatgpt", wait_until="networkidle")
        expect(page.locator('#downloaded_images')).to_be_visible()
        expect(page.locator('#cached_messages')).not_to_be_visible()
        assert page.locator('[data-chatgpt-content-mode-input]').input_value() == "media"
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("width", [982, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_cache_overview_groups_unique_metrics_and_keeps_run_progress_current(
    disposable_browser: Browser, sidebar_server_url: str, width: int, color_scheme: str,
    tmp_path: Path,
) -> None:
    """Keep source/mode totals unique and verify polling, geometry, and log paging."""
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 959}, color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    snapshot = {
        "phase": "stopped", "running": False, "message": "Cache run stopped.",
        "cached_sessions": 636, "cached_messages": 16_193,
        "downloaded_posts": 636, "downloaded_tweets": 16_193,
        "downloaded_images": 1_234, "downloaded_videos": 12,
        "queued_tweets": 2_100, "processed_tweets": 420,
        "discovered_tweets": 2_100, "progress_unit": "sessions",
        "recent_events": [f"[2026-09-06T04:37:05Z] Cached resource {index}." for index in range(25)],
    }
    page.route("**/api/cache/*/status?*", lambda route: route.fulfill(json=snapshot))
    try:
        for source, mode in (
            ("chatgpt", "text"), ("chatgpt", "media"), ("grok", "text"),
            ("grok", "media"), ("claude", "text"), ("gemini", "text"), ("x", "media"),
        ):
            page.add_init_script(
                f"sessionStorage.setItem('cachelikes:browser-content-mode:v1', {json.dumps(mode)})"
            )
            page.goto(f"{sidebar_server_url}/cache/{source}", wait_until="networkidle")
            expect(page.locator("#phase_value")).to_have_text("stopped")
            fields = page.locator('#overview [data-status-format="number"]:visible').evaluate_all(
                "nodes => nodes.map(node => node.dataset.statusField)"
            )
            assert len(fields) == len(set(fields)), (source, mode, fields)
            metric_rows = page.locator(".cache-summary-metrics .metric-card:visible").evaluate_all(
                "nodes => nodes.map(node => ({top: node.getBoundingClientRect().top, "
                "valueTop: node.querySelector('strong').getBoundingClientRect().top}))"
            )
            for left, right in zip(metric_rows, metric_rows[1:]):
                if abs(left["top"] - right["top"]) <= 1:
                    assert abs(left["valueTop"] - right["valueTop"]) <= 1
            geometry = page.evaluate("""() => {
                const rect = selector => document.querySelector(selector).getBoundingClientRect();
                const metrics = rect('.cache-summary-metrics');
                const progress = rect('.cache-run-progress');
                const bar = rect('#status_progress');
                const phase = rect('#phase_value');
                const heading = rect('#cache_progress_heading');
                return {
                    overflow: document.documentElement.scrollWidth - innerWidth,
                    metricsBottom: metrics.bottom, progressTop: progress.top,
                    barWidth: bar.width, progressWidth: progress.width,
                    headingRight: heading.right, phaseLeft: phase.left,
                };
            }""")
            assert geometry["overflow"] <= 1
            assert geometry["metricsBottom"] <= geometry["progressTop"]
            assert abs(geometry["barWidth"] - geometry["progressWidth"]) <= 1
            assert geometry["headingRight"] <= geometry["phaseLeft"]
            expect(page.locator("#activity")).to_have_count(0)
            if source == "chatgpt":
                page.screenshot(path=str(tmp_path / f"cache-{mode}-{color_scheme}-{width}.png"))
        page.goto(f"{sidebar_server_url}/cache/chatgpt", wait_until="networkidle")
        snapshot.update(phase="failed", message="Cache run failed.")
        expect(page.locator("#phase_value")).to_have_attribute("data-phase", "failed", timeout=6_000)
        expect(page.locator("#message")).to_have_text("Cache run failed.")
        expect(page.locator("#recent_events_body")).to_have_count(0)
        assert errors == []
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_all_cached_messages_reuse_the_numbered_frosted_table(
    disposable_browser: Browser, seeded_chatgpt_browser_server_url: str, width: int,
) -> None:
    """Keep the ungrouped Text view readable in the existing session table."""
    page, context = _open_page(
        disposable_browser,
        f"{seeded_chatgpt_browser_server_url}/browser?view=text&session_view=0&source=all&sort=newest&q=",
        width, 959, touch=False,
    )
    try:
        table = page.get_by_role("table", name="Cached messages", exact=True)
        expect(table).to_be_visible()
        expect(table.locator("th")).to_have_text(["No.", "Time", "Role", "Message"])
        expect(table.locator("tbody .browser-session-table-number")).to_have_text("1")
        expect(table.locator(".browser-session-table-message")).to_have_text(
            "A timestamp layout regression fixture."
        )
        expect(page.locator(".browser-chat-message-role, .browser-chat-message-title")).to_have_count(0)
        geometry = table.evaluate("""table => {
            const header = table.querySelector('th');
            const content = table.querySelector('.browser-session-table-message');
            return {
                position: getComputedStyle(header).position,
                blur: getComputedStyle(header).backdropFilter,
                contentHeight: content.getBoundingClientRect().height,
                lineHeight: parseFloat(getComputedStyle(content).lineHeight),
                bodyOverflow: document.documentElement.scrollWidth - innerWidth,
            };
        }""")
        assert geometry["position"] == "sticky"
        assert "blur(" in geometry["blur"]
        assert geometry["contentHeight"] + 1 >= geometry["lineHeight"]
        assert geometry["bodyOverflow"] <= 1
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_text_source_selection_survives_global_search_form_submission(
    disposable_browser: Browser, seeded_chatgpt_browser_server_url: str, width: int,
) -> None:
    """Only explicit global searches may reset the selected chat source."""
    page, context = _open_page(
        disposable_browser,
        seeded_chatgpt_browser_server_url + "/browser?view=text&session_view=0&source=all&sort=newest&q=",
        width, 959, touch=False,
    )
    try:
        if width < 901:
            page.locator("#sidebar_toggle").click()
        page.locator("[data-browser-source-filter-trigger]").click()
        menu = page.locator("#browser_source_filter_options")
        expect(menu).to_be_visible()
        option_geometry = menu.locator("[data-browser-source-filter-option]").evaluate_all("""options =>
            options.map(option => {
                const label = option.querySelector('.trade-strategy-dropdown-text');
                return {
                    columns: getComputedStyle(option).gridTemplateColumns.split(' ').length,
                    height: option.getBoundingClientRect().height,
                    labelClientWidth: label.clientWidth,
                    labelScrollWidth: label.scrollWidth,
                };
            })
        """)
        assert all(item["columns"] == 3 for item in option_geometry)
        assert all(item["height"] <= 40 for item in option_geometry)
        assert all(
            item["labelClientWidth"] >= item["labelScrollWidth"]
            for item in option_geometry
        )
        page.locator("[data-browser-source-filter-trigger]").click()
        for source, label in [("claude", "Claude"), ("gemini", "Gemini"), ("grok", "Grok"), ("chatgpt", "ChatGPT")]:
            page.locator("[data-browser-source-filter-trigger]").click()
            page.locator(f'[data-browser-source-filter-option="{source}"]').click()
            expect(page).to_have_url(re.compile(rf"[?&]source={source}(?:&|$)"))
            expect(page.locator("[data-browser-source-filter-trigger]")).to_have_attribute("aria-label", f"Source: {label}")
        page.locator("#browser_search_input").fill("timestamp")
        page.locator("#browser_search_input").press("Enter")
        expect(page).to_have_url(re.compile(r"[?&]source=all(?:&|$)"))
        expect(page.get_by_role("table", name="Cached messages", exact=True)).to_contain_text("timestamp layout")
    finally:
        context.close()


@pytest.mark.parametrize("mode", ["media", "prompts"])
def test_returning_to_text_restores_session_table(
    disposable_browser: Browser, annotated_resources_server_url: str, mode: str,
) -> None:
    """Do not inherit another content mode's flattened message-view flag."""
    context = disposable_browser.new_context(viewport={"width": 1024, "height": 1332})
    page = context.new_page()
    try:
        page.goto(annotated_resources_server_url + f"/browser?view={mode}&session_view=0&source=all&sort=oldest")
        page.locator('label[for="browser_view_text"]').click()
        expect(page.locator('#browser_view_text')).to_be_checked()
        expect(page.locator('.browser-session-table:not(.browser-session-detail-table)')).to_be_visible()
        assert "session_view=1" in page.url
        assert "sort=oldest" in page.url
        expect(page.locator('.browser-session-detail-table')).to_have_count(0)
        page.locator('.browser-session-table-title').first.click()
        expect(page.locator('.browser-session-detail-table')).to_be_visible()
    finally:
        context.close()


@pytest.mark.parametrize("width", [1024, 390])
def test_session_header_source_sort_and_two_line_time(
    disposable_browser: Browser, annotated_resources_server_url: str, width: int,
) -> None:
    context = disposable_browser.new_context(viewport={"width": width, "height": 1332})
    page = context.new_page()
    try:
        page.goto(annotated_resources_server_url + "/browser?view=text&source=all&session_view=1&sort=newest&q=crosspage%20needle")
        table = page.locator('.browser-session-index-table')
        expect(table.locator('th[aria-sort="descending"]')).to_contain_text("Last updated")
        stamp = table.locator('.browser-session-message-time').first
        geometry = stamp.evaluate("""node => {
            const date=node.querySelector('.browser-session-message-time-date').getBoundingClientRect();
            const clock=node.querySelector('.browser-session-message-time-clock').getBoundingClientRect();
            const table=node.closest('table').getBoundingClientRect();
            return {dateBottom:date.bottom, clockTop:clock.top, right:clock.right, tableRight:table.right};
        }""")
        assert geometry['clockTop'] >= geometry['dateBottom']
        assert geometry['right'] <= geometry['tableRight'] + 1
        assert geometry['tableRight'] <= width
        table.locator('.browser-session-sort-link').click()
        expect(page.locator('th[aria-sort="ascending"]')).to_be_visible()
        assert 'sort=oldest' in page.url
        trigger = page.locator('[data-browser-header-filter] [data-browser-source-filter-trigger]')
        expect(trigger).to_have_css("height", "22px")
        trigger.click()
        menu = page.locator('#browser_header_source_filter_options')
        expect(menu).to_be_visible()
        assert menu.evaluate('element => element.parentElement === document.body')
        menu.locator('[data-browser-source-filter-option="chatgpt"]').click()
        assert 'source=chatgpt' in page.url and 'sort=oldest' in page.url
        expect(page.locator('#browser_search_input')).to_have_value('crosspage needle')
        expect(page.locator('#browser_filter_form [data-browser-source-filter-input]')).to_have_value('chatgpt')
        page.locator('.browser-session-sort-link').click()
        expect(page.locator('th[aria-sort="descending"]')).to_be_visible()
        assert 'source=chatgpt' in page.url
    finally:
        context.close()


@pytest.mark.parametrize('width,motion', [(1024, 'no-preference'), (390, 'reduce')])
def test_cache_progress_reuses_training_track_and_lifecycle(disposable_browser, sidebar_server_url, width, motion):
    context = disposable_browser.new_context(viewport={'width': width, 'height': 959}, reduced_motion=motion)
    page = context.new_page()
    state = {'phase': 'idle', 'running': False, 'queued_tweets': 0, 'processed_tweets': 0,
             'message': 'Ready', 'progress_unit': 'sessions', 'recent_events': []}
    page.route('**/api/cache/*/status?*', lambda route: route.fulfill(json=state))
    try:
        page.goto(sidebar_server_url + '/cache/chatgpt', wait_until='networkidle')
        track = page.locator('#status_progress')
        fill = page.locator('#status_progress_fill')
        expect(track).to_have_css('height', '6px')
        expect(track).to_have_class(re.compile('is-unavailable'))
        expect(track).not_to_have_attribute('aria-valuenow', re.compile('.+'))
        assert page.locator('#status_progress_value').evaluate('(node) => node.getBoundingClientRect().bottom <= document.querySelector("#status_progress").getBoundingClientRect().top')
        state.update(phase='collecting', running=True)
        expect(track).to_have_class(re.compile('is-indeterminate'), timeout=6000)
        expect(fill).to_have_css('animation-name', 'none')
        state.update(queued_tweets=10, processed_tweets=4)
        expect(track).to_have_attribute('aria-valuenow', '40', timeout=6000)
        expect(page.locator('#status_progress_value')).to_have_text('40%')
        state.update(phase='failed', running=False)
        expect(page.locator('#phase_value')).to_have_attribute('data-phase', 'failed', timeout=6000)
        expect(track).to_have_attribute('aria-valuenow', '40')
        expect(page.locator('#status_progress_value')).to_have_text('Failed at 40%')
        expect(page.locator('#status_progress_detail')).to_have_text(
            '4 / 10 sessions processed before failure (40%); 6 not processed.'
        )
        state.update(phase='completed', processed_tweets=10)
        expect(track).to_have_attribute('aria-valuenow', '100', timeout=6000)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        context.close()


@pytest.mark.parametrize("width", [1012, 390])
def test_zhihu_cached_answer_metric_tracks_live_progress_without_overstating_failure(
    disposable_browser: Browser,
    sidebar_server_url: str,
    width: int,
) -> None:
    context = disposable_browser.new_context(viewport={"width": width, "height": 959})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    state = {
        "phase": "downloading",
        "running": True,
        "message": "Reading Zhihu answers.",
        "downloaded_posts": 328,
        "discovered_tweets": 1_177,
        "queued_tweets": 1_177,
        "processed_tweets": 120,
        "progress_unit": "answers",
    }
    page.route("**/api/cache/zhihu/status?*", lambda route: route.fulfill(json=state))
    try:
        page.goto(f"{sidebar_server_url}/cache/zhihu", wait_until="networkidle")
        cached_answers = page.locator("#downloaded_posts")
        expect(cached_answers).to_have_text("328")

        state.update(processed_tweets=820)
        expect(cached_answers).to_have_text("820", timeout=6_000)
        state.update(processed_tweets=873)
        expect(cached_answers).to_have_text("873", timeout=6_000)

        state.update(phase="failed", running=False, message="Previous archive preserved.")
        expect(cached_answers).to_have_text("328", timeout=6_000)
        expect(page.locator("#phase_value")).to_have_attribute("data-phase", "failed")
        expect(page.locator("#status_progress_value")).to_have_text("Failed at 74%")
        expect(page.locator("#status_progress_detail")).to_have_text(
            "873 / 1,177 answers processed before failure (74%); 304 not processed."
        )

        state.update(
            phase="finished",
            message="Finished Zhihu answer cache.",
            downloaded_posts=1_334,
            queued_tweets=1_006,
            processed_tweets=1_006,
        )
        expect(cached_answers).to_have_text("1,334", timeout=6_000)
        expect(page.locator("#phase_value")).to_have_attribute("data-phase", "finished")
        expect(page.locator("#status_progress_value")).to_have_text("100%")
        expect(page.locator("#status_progress_detail")).to_have_text(
            "1,006 / 1,006 available answers processed (100%). "
            "Zhihu reported 1,177 total, but 171 were not exposed by either verified pagination pass."
        )
        expect(page.locator("#progress_queued_tweets")).to_have_text("1,006")
        expect(page.locator("#progress_processed_tweets")).to_have_text("1,006")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert errors == []
    finally:
        context.close()
