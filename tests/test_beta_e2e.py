"""Disposable-browser coverage for isolated Beta experiment workflows.

Code version: v0.4.0-codex.1
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Route, expect

from test_sidebar_e2e import disposable_browser as disposable_browser
from test_sidebar_e2e import sidebar_server_url as sidebar_server_url


pytestmark = [pytest.mark.integration, pytest.mark.slow]

EXPERIMENTS = (
    ("idea-collision", "Idea Collision", "Collide ideas"),
    ("context-capsule", "Context Capsule", "Build capsule"),
    ("question-radar", "Question Radar", "Find questions"),
    ("memory-diff", "Memory Diff", "Compare snapshots"),
    ("decision-wind-tunnel", "Decision Wind Tunnel", "Challenge proposal"),
    ("mission-forge", "Mission Forge", "Build mission"),
)
DRAFT_PREFIX = "agenticcontext:beta:v1:draft:"
ZHIHU_EXAMPLE_URL = "https://www.zhihu.com/people/feifeimao/answers?page=2"


@dataclass
class BetaPage:
    context: BrowserContext
    page: Page
    origin: str
    requests: list[str] = field(default_factory=list)
    forbidden_requests: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def open(self, experiment: str) -> None:
        self.page.goto(f"{self.origin}/beta/{experiment}", wait_until="networkidle")
        expect(self.page.locator("[data-beta-root]")).to_have_attribute(
            "data-beta-experiment", experiment
        )


def _zhihu_status(**overrides: object) -> dict[str, object]:
    status: dict[str, object] = {
        "running": False,
        "phase": "idle",
        "message": "Ready to cache this answer collection.",
        "profile_url": ZHIHU_EXAMPLE_URL,
        "author_token": "feifeimao",
        "browser": "chrome",
        "cache_exists": False,
        "expected_answers": 0,
        "processed_answers": 0,
        "cached_answers": 0,
        "pages_processed": 0,
        "duplicates": 0,
        "unavailable_answers": 0,
        "raw_answers": 0,
        "stability_passes": 0,
        "added": 0,
        "changed": 0,
        "removed": 0,
        "unchanged": 0,
        "output_dir": "",
        "last_error": "",
        "recent_answers": [],
    }
    status.update(overrides)
    return status


def _zhihu_archive_page(query: str = "") -> dict[str, object]:
    items = [
        {
            "answer_id": "2197549311",
            "question_title": "育儿遇到的难题真的可以靠报班解决吗？",
            "answer_url": "https://www.zhihu.com/question/495309288/answer/2197549311",
            "excerpt": "报班的消费者是父母而不是孩子。",
            "content_available": True,
            "updated_at": "2021-10-31T05:01:10Z",
            "voteup_count": 554,
            "comment_count": 29,
        },
        {
            "answer_id": "987654321",
            "question_title": '<img src=x onerror="window.zhihuCacheInjected=true">',
            "answer_url": "https://www.zhihu.com/question/200/answer/987654321",
            "excerpt": "Unsafe fixture title stays text.",
            "content_available": True,
            "updated_at": "2025-01-02T00:00:00Z",
            "voteup_count": 8,
            "comment_count": 1,
        },
    ]
    if query:
        items = [item for item in items if query in str(item)]
    return {
        "profile_url": "https://www.zhihu.com/people/feifeimao/answers",
        "author_token": "feifeimao",
        "query": query,
        "page": 1,
        "page_size": 20,
        "page_count": 1,
        "total": len(items),
        "items": items,
    }


def _cached_zhihu_answer(answer_id: str) -> dict[str, object]:
    return {
        **_zhihu_archive_page()["items"][0],
        "answer_id": answer_id,
        "content_text": (
            "报班的消费者是父母而不是孩子，弄清楚这点就知道大部分的报课"
            "其实是为了解决家长的问题而不是为了孩子。"
        ),
        "content_sha256": "a" * 64,
    }


@dataclass
class ZhihuBetaPage(BetaPage):
    api_requests: list[tuple[str, str]] = field(default_factory=list)
    status_queries: list[str] = field(default_factory=list)
    start_payloads: list[dict[str, object]] = field(default_factory=list)
    stop_requests: int = 0
    archive_queries: list[dict[str, list[str]]] = field(default_factory=list)
    answer_reads: list[str] = field(default_factory=list)
    status_snapshots: list[dict[str, object]] = field(default_factory=list)
    current_status: dict[str, object] = field(default_factory=_zhihu_status)
    start_status: dict[str, object] = field(
        default_factory=lambda: _zhihu_status(
            running=True,
            phase="collecting",
            message="Collecting answers exposed to Chrome.",
            expected_answers=333,
            processed_answers=17,
            cached_answers=16,
            pages_processed=1,
            duplicates=1,
        )
    )
    stop_status: dict[str, object] = field(
        default_factory=lambda: _zhihu_status(
            phase="stopped",
            message="Cache run stopped safely.",
            expected_answers=333,
            processed_answers=17,
            cached_answers=16,
            pages_processed=1,
            duplicates=1,
        )
    )
    start_error: tuple[int, str] | None = None


@pytest.fixture()
def zhihu_beta_page(
    disposable_browser: Browser,
    sidebar_server_url: str,
) -> Iterator[ZhihuBetaPage]:
    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 900},
        reduced_motion="reduce",
        color_scheme="light",
    )
    page = context.new_page()
    beta = ZhihuBetaPage(context, page, sidebar_server_url)

    def guard_request(route: Route) -> None:
        request = route.request
        beta.requests.append(request.url)
        parsed = urlsplit(request.url)
        local_origin = urlsplit(sidebar_server_url)
        is_local = (parsed.scheme, parsed.netloc) == (local_origin.scheme, local_origin.netloc)
        is_zhihu_api = is_local and parsed.path.startswith("/api/beta/zhihu-answers-cache/")

        if is_zhihu_api:
            beta.api_requests.append((request.method, request.url))
            if "/answers/" in parsed.path and request.method == "GET":
                answer_id = parsed.path.rsplit("/", 1)[-1]
                beta.answer_reads.append(answer_id)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_cached_zhihu_answer(answer_id)),
                )
                return
            if parsed.path.endswith("/answers") and request.method == "GET":
                query = parse_qs(parsed.query)
                beta.archive_queries.append(query)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_zhihu_archive_page(query.get("q", [""])[0])),
                )
                return
            if parsed.path.endswith("/status") and request.method == "GET":
                beta.status_queries.extend(parse_qs(parsed.query).get("profile_url", []))
                if beta.status_snapshots:
                    beta.current_status = beta.status_snapshots.pop(0)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(beta.current_status),
                )
                return
            if parsed.path.endswith("/start") and request.method == "POST":
                payload = request.post_data_json
                beta.start_payloads.append(payload if isinstance(payload, dict) else {})
                if beta.start_error:
                    status, message = beta.start_error
                    route.fulfill(
                        status=status,
                        content_type="application/json",
                        body=json.dumps({"error": message, "code": "invalid_request"}),
                    )
                    return
                beta.current_status = dict(beta.start_status)
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps({"status": beta.current_status}),
                )
                return
            if parsed.path.endswith("/stop") and request.method == "POST":
                beta.stop_requests += 1
                beta.current_status = dict(beta.stop_status)
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps({"status": beta.current_status}),
                )
                return

        if not is_local or request.method not in {"GET", "HEAD"}:
            beta.forbidden_requests.append(f"{request.method} {request.url}")
            route.abort()
            return
        route.continue_()

    context.route("**/*", guard_request)
    page.on("pageerror", lambda error: beta.errors.append(str(error)))
    page.on(
        "response",
        lambda response: beta.errors.append(f"HTTP {response.status}: {response.url}")
        if response.status >= 400
        and "/api/beta/zhihu-answers-cache/" not in response.url
        else None,
    )
    try:
        yield beta
    finally:
        context.close()
        assert not beta.forbidden_requests, beta.forbidden_requests
        assert not beta.errors, beta.errors


@pytest.fixture()
def beta_page(
    disposable_browser: Browser, sidebar_server_url: str, request: pytest.FixtureRequest
) -> Iterator[BetaPage]:
    context = disposable_browser.new_context(
        viewport={"width": 1_280, "height": 900},
        reduced_motion="reduce",
        color_scheme="light",
        accept_downloads=True,
        java_script_enabled=getattr(request, "param", True),
    )
    page = context.new_page()
    beta = BetaPage(context, page, sidebar_server_url)

    def guard_request(route: Route) -> None:
        request = route.request
        beta.requests.append(request.url)
        if not request.url.startswith(f"{sidebar_server_url}/") or request.method not in {"GET", "HEAD"}:
            beta.forbidden_requests.append(f"{request.method} {request.url}")
            route.abort()
            return
        route.continue_()

    context.route("**/*", guard_request)
    page.on("pageerror", lambda error: beta.errors.append(str(error)))
    page.on(
        "response",
        lambda response: beta.errors.append(f"HTTP {response.status}: {response.url}")
        if response.status >= 400
        else None,
    )
    try:
        yield beta
    finally:
        context.close()
        assert not beta.forbidden_requests, beta.forbidden_requests
        assert not beta.errors, beta.errors


def _run_example(page: Page, action: str) -> None:
    page.get_by_role("button", name="Try example", exact=True).click()
    expect(page.locator("#beta_source")).not_to_have_value("")
    page.get_by_role("button", name=action, exact=True).click()
    expect(page.locator("[data-beta-result]")).to_be_visible()
    expect(page.locator("[data-beta-status]")).to_contain_text("Output ready")
    expect(page.locator("[data-beta-sections] section").first).to_be_visible()
    assert page.locator("[data-beta-sections]").inner_text().strip()
    expect(page.locator("[data-beta-run]")).to_be_enabled()
    expect(page.locator("[data-beta-form]")).not_to_have_attribute("aria-busy", "true")


def _assert_no_horizontal_overflow(page: Page) -> None:
    geometry = page.evaluate(
        """() => ({
            page: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - innerWidth,
            content: [...document.querySelectorAll(
                '.beta-content, .beta-input-card, .beta-result, .beta-zhihu-progress, .beta-zhihu-archive, .beta-zhihu-detail',
            )]
                .filter(element => !element.hidden)
                .map(element => element.scrollWidth - element.clientWidth),
        })"""
    )
    assert geometry["page"] <= 1, geometry
    assert all(overflow <= 1 for overflow in geometry["content"]), geometry


def _assert_beta_title_rail_and_button_material(page: Page) -> None:
    page.wait_for_function(
        """() => {
            const selectors = ['.beta-title .report-heading', '#sidebar_toggle', '#global_theme_toggle'];
            const rects = selectors.map(selector => {
                const rect = document.querySelector(selector)?.getBoundingClientRect();
                return rect ? [rect.x, rect.y, rect.width, rect.height] : null;
            });
            if (rects.some(rect => !rect)) return false;
            const snapshot = JSON.stringify(rects);
            const stable = window.__betaTitleRailSnapshot === snapshot;
            window.__betaTitleRailSnapshot = snapshot;
            return stable;
        }"""
    )
    geometry = page.evaluate(
        """() => {
            const title = document.querySelector('.beta-title .report-heading');
            const range = document.createRange();
            range.selectNodeContents(title);
            const titleRect = range.getBoundingClientRect();
            const railRects = ['.beta-title .report-heading', '#sidebar_toggle', '#global_theme_toggle']
                .map(selector => {
                    const rect = document.querySelector(selector).getBoundingClientRect();
                    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, centerY: rect.y + rect.height / 2 };
                });
            const overlaps = element => {
                const other = element.getBoundingClientRect();
                return titleRect.left < other.right && titleRect.right > other.left
                    && titleRect.top < other.bottom && titleRect.bottom > other.top;
            };
            const buttons = [...document.querySelectorAll(
                '[data-beta-example], [data-beta-import], [data-beta-clear], [data-beta-copy], [data-beta-export]',
            )].map(element => {
                const style = getComputedStyle(element);
                return {
                    text: element.textContent,
                    radius: parseFloat(style.borderTopLeftRadius),
                    shadow: style.boxShadow,
                    background: style.backgroundColor,
                };
            });
            return {
                overlapsToggle: overlaps(document.querySelector('#sidebar_toggle')),
                overlapsTheme: overlaps(document.querySelector('#global_theme_toggle')),
                railRects,
                buttons,
            };
        }"""
    )
    assert not geometry["overlapsToggle"], geometry
    assert not geometry["overlapsTheme"], geometry
    title, toggle, theme = geometry["railRects"]
    assert abs(title["centerY"] - toggle["centerY"]) <= 1, geometry["railRects"]
    assert abs(title["centerY"] - theme["centerY"]) <= 1, geometry["railRects"]
    assert len(geometry["buttons"]) == 5
    for button in geometry["buttons"]:
        assert button["radius"] >= 8, button
        assert button["shadow"] != "none", button
        assert button["background"] not in {"transparent", "rgba(0, 0, 0, 0)"}, button


def _completed_zhihu_status() -> dict[str, object]:
    return _zhihu_status(
        cache_exists=True,
        phase="complete",
        message="Finished caching 328 records. Zhihu reports 333; 5 are not enumerable.",
        expected_answers=333,
        processed_answers=328,
        cached_answers=328,
        pages_processed=17,
        duplicates=98,
        unavailable_answers=5,
        raw_answers=426,
        stability_passes=2,
        added=328,
        changed=0,
        removed=0,
        unchanged=0,
        output_dir="/tmp/agenticcontext-test/beta-store/zhihu/feifeimao",
        recent_answers=[
            {
                "answer_id": "123456789",
                "question_title": "如何建立一个可验证、可恢复的长期知识缓存？",
                "answer_url": "https://www.zhihu.com/question/100/answer/123456789",
                "updated_at": "2026-09-10T04:00:00Z",
                "voteup_count": 12_345,
                "comment_count": 67,
            },
            {
                "answer_id": "987654321",
                "question_title": '<img src=x onerror="window.zhihuCacheInjected=true">',
                "answer_url": "https://www.zhihu.com/question/200/answer/987654321",
                "updated_at": "2025-01-02T00:00:00Z",
                "voteup_count": 8,
                "comment_count": 1,
            },
        ],
    )


def _assert_zhihu_geometry(page: Page, width: int, height: int) -> None:
    _assert_no_horizontal_overflow(page)
    geometry = page.evaluate(
        """() => {
            const profile = document.querySelector('#zhihu_profile_url').getBoundingClientRect();
            const content = document.querySelector('.beta-content').getBoundingClientRect();
            const cards = [...document.querySelectorAll(
                '.beta-zhihu-input-card, .beta-zhihu-progress, .beta-zhihu-archive, .beta-zhihu-detail',
            )].filter(element => !element.hidden).map(element => {
                const rect = element.getBoundingClientRect();
                return {left: rect.left, right: rect.right, width: rect.width};
            });
            return {
                profileWidth: profile.width,
                contentLeft: content.left,
                contentRight: content.right,
                cards,
            };
        }"""
    )
    assert geometry["profileWidth"] <= 385, geometry
    assert all(
        card["left"] >= geometry["contentLeft"] - 1
        and card["right"] <= geometry["contentRight"] + 1
        for card in geometry["cards"]
    ), geometry

    if width <= 900:
        toggle = page.get_by_role("button", name="Toggle sidebar", exact=True)
        if toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
        selected = page.get_by_role("link", name="Zhihu Answers Cache", exact=True)
        selected.scroll_into_view_if_needed()
        expect(selected).to_be_visible()
        page.locator(".sidebar-dock").evaluate(
            "element => Promise.allSettled(element.getAnimations().map(animation => animation.finished))"
        )
        sidebar_geometry = page.evaluate(
            """() => {
                const sidebar = document.querySelector('#app_sidebar').getBoundingClientRect();
                const dock = document.querySelector('.sidebar-dock').getBoundingClientRect();
                const selected = document.querySelector('.beta-nav-zhihu-answers-cache');
                const selectedRect = selected.getBoundingClientRect();
                const hit = document.elementFromPoint(
                    selectedRect.left + selectedRect.width / 2,
                    selectedRect.top + selectedRect.height / 2,
                );
                return {
                    sidebar: {top: sidebar.top, right: sidebar.right, bottom: sidebar.bottom, left: sidebar.left},
                    dockBottomGap: sidebar.bottom - dock.bottom,
                    selectedClearOfDock: selectedRect.bottom <= dock.top + 1,
                    selectedHit: hit === selected || selected.contains(hit),
                };
            }"""
        )
        assert sidebar_geometry["sidebar"]["top"] >= 0, sidebar_geometry
        assert sidebar_geometry["sidebar"]["left"] >= 0, sidebar_geometry
        assert sidebar_geometry["sidebar"]["right"] <= width + 1, sidebar_geometry
        assert sidebar_geometry["sidebar"]["bottom"] <= height + 1, sidebar_geometry
        assert abs(sidebar_geometry["dockBottomGap"] - 10) <= 1, sidebar_geometry
        assert sidebar_geometry["selectedClearOfDock"], sidebar_geometry
        assert sidebar_geometry["selectedHit"], sidebar_geometry


@pytest.mark.parametrize(("experiment", "title", "action"), EXPERIMENTS)
def test_each_beta_experiment_runs_locally_and_loads_engine_on_demand(
    beta_page: BetaPage, experiment: str, title: str, action: str
) -> None:
    beta_page.open(experiment)
    page = beta_page.page
    expect(page.get_by_role("navigation", name="Beta experiments")).to_be_visible()
    expect(page.locator(".beta-nav [aria-current=page]")).to_have_text(title)
    expect(page.locator("[data-beta-result]")).to_be_hidden()
    assert not any("/beta/engines.mjs" in url for url in beta_page.requests)
    assert not any("/beta/zhihu-answers-cache.js" in url for url in beta_page.requests)
    _run_example(page, action)
    assert any("/beta/engines.mjs" in url for url in beta_page.requests)
    _assert_no_horizontal_overflow(page)
    page.locator("#beta_source").fill("Changed evidence invalidates the previous result.")
    expect(page.locator("[data-beta-result]")).to_be_hidden()


@pytest.mark.parametrize("width", (1_024, 390))
@pytest.mark.parametrize("theme", ("light", "dark"))
def test_beta_idea_collision_responsive_layout_and_sidebar(
    beta_page: BetaPage, width: int, theme: str
) -> None:
    page = beta_page.page
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(color_scheme=theme)
    beta_page.open("idea-collision")
    expect(page.locator("#global_theme_toggle")).to_have_attribute("data-effective-theme", theme)
    _assert_no_horizontal_overflow(page)
    _assert_beta_title_rail_and_button_material(page)
    _run_example(page, "Collide ideas")
    _assert_no_horizontal_overflow(page)
    _assert_beta_title_rail_and_button_material(page)
    if theme == "light":
        page.screenshot(path=str(Path(tempfile.gettempdir()) / f"beta-review-result-{width}.png"))
    page.locator(".beta-scrollport").evaluate("element => { element.scrollTop = 0; }")
    if theme == "light":
        page.screenshot(path=str(Path(tempfile.gettempdir()) / f"beta-review-{width}.png"))
    toggle = page.get_by_role("button", name="Toggle sidebar", exact=True)
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    expect(page.get_by_role("navigation", name="Beta experiments")).to_be_visible()
    page.get_by_role("link", name="Question Radar", exact=True).click()
    expect(page).to_have_url(f"{beta_page.origin}/beta/question-radar")
    expect(page.locator(".beta-nav [aria-current=page]")).to_have_text("Question Radar")
    _assert_no_horizontal_overflow(page)
    _assert_beta_title_rail_and_button_material(page)


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    ((1_024, 900, "light"), (390, 844, "dark"), (375, 667, "light")),
)
def test_zhihu_answers_cache_polls_and_renders_a_333_answer_fixture_without_remote_browser_requests(
    zhihu_beta_page: ZhihuBetaPage,
    width: int,
    height: int,
    theme: str,
) -> None:
    page = zhihu_beta_page.page
    page.set_viewport_size({"width": width, "height": height})
    page.emulate_media(color_scheme=theme)
    zhihu_beta_page.status_snapshots = [_zhihu_status()]
    zhihu_beta_page.open("zhihu-answers-cache")
    zhihu_beta_page.status_snapshots = [_completed_zhihu_status()]

    expect(page.locator("#global_theme_toggle")).to_have_attribute("data-effective-theme", theme)
    expect(page.locator(".beta-nav [aria-current=page]")).to_have_text("Zhihu Answers Cache")
    expect(page.locator("#zhihu_profile_url")).to_have_value(ZHIHU_EXAMPLE_URL)
    expect(page.get_by_role("button", name="Use example", exact=True)).to_be_visible()
    selected_browser = page.locator('input[name="browser"]:checked').input_value()
    expect(page.get_by_role("button", name="Cache all answers", exact=True)).to_be_enabled()
    assert any("/static/beta/zhihu-answers-cache.js" in url for url in zhihu_beta_page.requests)
    assert not any("/static/beta/beta.js" in url for url in zhihu_beta_page.requests)

    page.get_by_role("button", name="Cache all answers", exact=True).click()
    expect(page.locator("[data-zhihu-cache-form]")).to_have_attribute("aria-busy", "true")
    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_visible()
    expect(page.locator("[data-zhihu-status]")).to_contain_text("Collecting answers")
    expect(page.locator('[data-zhihu-metric="processed_answers"]')).to_have_text("17")

    expect(page.locator("[data-zhihu-status]")).to_contain_text("Zhihu reports 333")
    expect(page.locator("[data-zhihu-cache-form]")).not_to_have_attribute("aria-busy", "true")
    expect(page.get_by_role("button", name="Cache all answers", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_hidden()
    expect(page.locator('[data-zhihu-metric="expected_answers"]')).to_have_text("333")
    expect(page.locator('[data-zhihu-metric="processed_answers"]')).to_have_text("328")
    expect(page.locator('[data-zhihu-metric="cached_answers"]')).to_have_text("328")
    expect(page.locator('[data-zhihu-metric="unavailable_answers"]')).to_have_text("5")
    expect(page.locator('[data-zhihu-metric="pages_processed"]')).to_have_text("17")
    expect(page.locator('[data-zhihu-metric="duplicates"]')).to_have_text("98")
    expect(page.locator('[data-zhihu-change="added"]')).to_have_text("328")
    expect(page.locator('[data-zhihu-change="changed"]')).to_have_text("0")
    expect(page.locator('[data-zhihu-change="unchanged"]')).to_have_text("0")
    expect(page.locator("[data-zhihu-progress-bar]")).to_have_attribute(
        "aria-valuetext", "328 of 333 answers processed"
    )
    expect(page.locator("[data-zhihu-progress-copy]")).to_contain_text(
        "333, with 5 not enumerable"
    )
    expect(page.locator("[data-zhihu-output-dir]")).to_contain_text("feifeimao")
    expect(page.locator("[data-zhihu-answer-list] li")).to_have_count(2)
    expect(page.locator("[data-zhihu-archive-summary]")).to_have_text("2 cached · @feifeimao")
    expect(page.locator("[data-zhihu-answer-list]")).to_contain_text("554 votes")
    expect(page.locator("[data-zhihu-answer-list]")).to_contain_text("Updated 31 Oct 2021")
    expect(page.locator("[data-zhihu-answer-list]")).to_contain_text("<img src=x")
    assert page.locator("[data-zhihu-answer-list] img").count() == 0
    assert page.evaluate("window.zhihuCacheInjected === undefined")

    page.get_by_role("button", name="View cached copy", exact=True).first.click()
    expect(page.locator("[data-zhihu-detail]")).to_be_visible()
    expect(page.locator("[data-zhihu-detail-title]")).to_have_text(
        "育儿遇到的难题真的可以靠报班解决吗？"
    )
    expect(page.locator("[data-zhihu-detail-status]")).to_contain_text(
        "Verified local body"
    )
    expect(page.locator("[data-zhihu-detail-body]")).to_contain_text(
        "报班的消费者是父母而不是孩子"
    )
    expect(page.locator("[data-zhihu-detail-source]")).to_have_attribute(
        "href", "https://www.zhihu.com/question/495309288/answer/2197549311"
    )
    assert zhihu_beta_page.answer_reads == ["2197549311"]

    page.locator("#zhihu_archive_query").fill("2197549311")
    page.get_by_role("button", name="Search", exact=True).click()
    expect(page.locator("[data-zhihu-answer-list] li")).to_have_count(1)
    expect(page.locator("[data-zhihu-archive-summary]")).to_have_text(
        "1 matches · @feifeimao"
    )

    assert zhihu_beta_page.start_payloads == [
        {"profile_url": ZHIHU_EXAMPLE_URL, "browser": selected_browser}
    ]
    assert ZHIHU_EXAMPLE_URL in zhihu_beta_page.status_queries
    assert sum(method == "GET" for method, _url in zhihu_beta_page.api_requests) >= 2
    assert zhihu_beta_page.archive_queries[-1]["q"] == ["2197549311"]
    assert all(
        urlsplit(url).netloc == urlsplit(zhihu_beta_page.origin).netloc
        for url in zhihu_beta_page.requests
    )
    _assert_zhihu_geometry(page, width, height)


def test_zhihu_answers_cache_stop_restores_controls_and_keeps_partial_counts(
    zhihu_beta_page: ZhihuBetaPage,
) -> None:
    zhihu_beta_page.status_snapshots = [_zhihu_status()]
    zhihu_beta_page.open("zhihu-answers-cache")
    page = zhihu_beta_page.page

    page.get_by_role("button", name="Cache all answers", exact=True).click()
    expect(page.locator("[data-zhihu-cache-form]")).to_have_attribute("aria-busy", "true")
    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Stop", exact=True).click()

    expect(page.locator("[data-zhihu-status]")).to_contain_text("stopped safely")
    expect(page.locator("[data-zhihu-cache-form]")).not_to_have_attribute("aria-busy", "true")
    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_hidden()
    expect(page.get_by_role("button", name="Cache all answers", exact=True)).to_be_enabled()
    expect(page.locator('[data-zhihu-metric="processed_answers"]')).to_have_text("17")
    expect(page.locator('[data-zhihu-metric="cached_answers"]')).to_have_text("16")
    assert zhihu_beta_page.stop_requests == 1


def test_zhihu_answers_cache_surfaces_start_errors_without_leaving_the_form_busy(
    zhihu_beta_page: ZhihuBetaPage,
) -> None:
    zhihu_beta_page.start_error = (400, "Use a Zhihu profile answers URL.")
    zhihu_beta_page.status_snapshots = [_zhihu_status()]
    zhihu_beta_page.open("zhihu-answers-cache")
    page = zhihu_beta_page.page

    page.get_by_role("button", name="Cache all answers", exact=True).click()

    expect(page.locator("[data-zhihu-status]")).to_have_text(
        "Use a Zhihu profile answers URL."
    )
    expect(page.locator("[data-zhihu-status]")).to_have_attribute("data-state", "error")
    expect(page.locator("[data-zhihu-cache-form]")).not_to_have_attribute("aria-busy", "true")
    expect(page.get_by_role("button", name="Cache all answers", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Stop", exact=True)).to_be_hidden()
    assert len(zhihu_beta_page.start_payloads) == 1


@pytest.mark.parametrize(
    "beta_page",
    (True, False),
    indirect=True,
    ids=("module-blocked", "javascript-disabled"),
)
def test_zhihu_answers_cache_without_javascript_cannot_submit_a_profile_url(
    beta_page: BetaPage,
) -> None:
    page = beta_page.page
    page.route("**/static/beta/zhihu-answers-cache.js?*", lambda route: route.abort())
    beta_page.open("zhihu-answers-cache")
    start = page.get_by_role("button", name="Cache all answers", exact=True)
    expect(start).to_be_disabled()
    page.locator("#zhihu_profile_url").fill(ZHIHU_EXAMPLE_URL)
    previous_requests = list(beta_page.requests)
    page.locator("#zhihu_profile_url").press("Enter")
    expect(page).to_have_url(f"{beta_page.origin}/beta/zhihu-answers-cache")
    expect(page.locator("#zhihu_profile_url")).to_have_value(ZHIHU_EXAMPLE_URL)
    assert beta_page.requests == previous_requests


@pytest.mark.parametrize("beta_page", (True, False), indirect=True, ids=("module-blocked", "javascript-disabled"))
def test_beta_unavailable_javascript_never_submits_source_material(beta_page: BetaPage) -> None:
    page = beta_page.page
    page.route("**/static/beta/beta.js?*", lambda route: route.abort())
    beta_page.open("idea-collision")
    expect(page.locator("[data-beta-run]")).to_be_disabled()
    page.locator("#beta_source").fill("Private source fixture must stay in the form.")
    page.locator("#beta_second").fill("Second private fixture.")
    page.locator("#beta_objective").fill("No native query submission")
    previous_requests = list(beta_page.requests)
    page.locator("#beta_objective").press("Enter")
    expect(page).to_have_url(f"{beta_page.origin}/beta/idea-collision")
    expect(page.locator("#beta_source")).to_have_value("Private source fixture must stay in the form.")
    assert beta_page.requests == previous_requests


def test_beta_dock_keeps_cache_destination_and_settings_pill_position(beta_page: BetaPage) -> None:
    page = beta_page.page
    page.add_init_script(
        "sessionStorage.setItem('cachelikes:dock-location:v1:cache', '/cache/grok');"
    )
    beta_page.open("idea-collision")
    assert page.get_by_role("navigation", name="Beta experiments").locator("a").evaluate_all(
        "links => links.map(link => link.textContent.trim())"
    ) == [title for _experiment, title, _action in EXPERIMENTS] + ["Zhihu Answers Cache"]
    dock = page.get_by_role("navigation", name="Workspace sections")
    assert dock.locator("a").evaluate_all(
        "links => links.map(link => link.getAttribute('aria-label'))"
    ) == ["Agent", "Cache", "Local resources", "Beta", "Settings"]
    expect(dock.get_by_role("link", name="Beta", exact=True)).to_have_attribute("href", "/beta")
    expect(dock.get_by_role("link", name="Beta", exact=True)).to_have_attribute("aria-current", "page")
    assert dock.get_by_role("link", name="Cache", exact=True).evaluate(
        "link => link.href"
    ) == f"{beta_page.origin}/cache/grok"
    assert page.evaluate("sessionStorage.getItem('cachelikes:dock-location:v1:cache')") == "/cache/grok"
    dock.get_by_role("link", name="Settings", exact=True).click()
    expect(page).to_have_url(f"{beta_page.origin}/settings")
    expect(dock.get_by_role("link", name="Settings", exact=True)).to_have_attribute("aria-current", "page")
    assert dock.evaluate("element => getComputedStyle(element).getPropertyValue('--active-index').trim()") == "4"
    dock.get_by_role("link", name="Beta", exact=True).click()
    expect(page).to_have_url(f"{beta_page.origin}/beta")
    expect(page.locator("[data-beta-root]")).to_have_attribute("data-beta-experiment", "idea-collision")


def test_beta_drafts_survive_navigation_and_clear_only_current_experiment(beta_page: BetaPage) -> None:
    page = beta_page.page
    beta_page.open("idea-collision")
    page.locator("#beta_source").fill("Draft A must survive another experiment.")
    page.locator("#beta_second").fill("The second source also belongs to A.")
    page.get_by_role("link", name="Question Radar", exact=True).click()
    expect(page.locator("#beta_source")).to_have_value("")
    page.locator("#beta_source").fill("Which question belongs to draft B?")
    page.reload(wait_until="networkidle")
    expect(page.locator("#beta_source")).to_have_value("Which question belongs to draft B?")
    page.get_by_role("link", name="Idea Collision", exact=True).click()
    expect(page.locator("#beta_source")).to_have_value("Draft A must survive another experiment.")
    expect(page.locator("#beta_second")).to_have_value("The second source also belongs to A.")
    page.get_by_role("button", name="Clear draft", exact=True).click()
    expect(page.locator("#beta_source")).to_have_value("")
    expect(page.locator("#beta_second")).to_have_value("")
    assert page.evaluate("key => sessionStorage.getItem(key)", DRAFT_PREFIX + "idea-collision") is None
    page.get_by_role("link", name="Question Radar", exact=True).click()
    expect(page.locator("#beta_source")).to_have_value("Which question belongs to draft B?")
    page.get_by_role("link", name="Idea Collision", exact=True).click()
    expect(page.locator("#beta_source")).to_have_value("")


def test_beta_large_escaped_drafts_survive_navigation_and_reload(beta_page: BetaPage) -> None:
    page = beta_page.page
    beta_page.open("question-radar")
    other_source = "Which question must remain isolated from the large draft?"
    page.locator("#beta_source").fill(other_source)
    page.get_by_role("link", name="Idea Collision", exact=True).click()
    other_key = DRAFT_PREFIX + "question-radar"
    other_saved = page.evaluate("key => sessionStorage.getItem(key)", other_key)
    assert json.loads(other_saved)["source"] == other_source

    escaped_pattern = "\\" * 298 + '"' + "\n"
    source = escaped_pattern * 200
    second = escaped_pattern[::-1] * 200
    page.locator("#beta_source").fill(source)
    page.locator("#beta_second").fill(second)
    page.get_by_role("link", name="Question Radar", exact=True).click()
    saved = page.evaluate("key => sessionStorage.getItem(key)", DRAFT_PREFIX + "idea-collision")
    assert len(saved) > 130_000
    assert json.loads(saved)["source"] == source
    assert json.loads(saved)["second"] == second
    expect(page.locator("#beta_source")).to_have_value(other_source)
    assert page.evaluate("key => sessionStorage.getItem(key)", other_key) == other_saved

    page.get_by_role("link", name="Idea Collision", exact=True).click()
    expect(page.locator("#beta_source")).to_have_value(source)
    expect(page.locator("#beta_second")).to_have_value(second)
    page.reload(wait_until="networkidle")
    expect(page.locator("#beta_source")).to_have_value(source)
    expect(page.locator("#beta_second")).to_have_value(second)
    assert page.evaluate("key => sessionStorage.getItem(key)", other_key) == other_saved


@pytest.mark.parametrize("saved", ("{broken", "null", '["unexpected"]', '{"source":42}'))
def test_beta_ignores_malformed_saved_drafts(beta_page: BetaPage, saved: str) -> None:
    page = beta_page.page
    page.add_init_script(
        f"sessionStorage.setItem({json.dumps(DRAFT_PREFIX + 'question-radar')}, {json.dumps(saved)});"
    )
    beta_page.open("question-radar")
    expect(page.locator("#beta_source")).to_have_value("")
    _run_example(page, "Find questions")


def test_beta_imports_text_and_rejects_oversized_or_binary_files(beta_page: BetaPage) -> None:
    beta_page.open("question-radar")
    page = beta_page.page
    file_input = page.locator("[data-beta-file]")
    source = "What could this imported evidence help us test?\nTODO: preserve the original file."
    file_input.set_input_files({"name": "notes.md", "mimeType": "text/markdown", "buffer": source.encode()})
    expect(page.locator("#beta_source")).to_have_value(source)
    expect(page.locator("[data-beta-status]")).to_contain_text("Imported notes.md locally")
    for name, contents, message in (
        ("oversize.txt", b"x" * 240_001, "no larger than 240 KB"),
        ("long.txt", b"x" * 60_001, "60,000 characters"),
        ("binary.txt", b"text\x00binary", "binary data"),
        ("image.png", b"not a text import", "Choose a .txt, .md, or .json"),
    ):
        file_input.set_input_files({"name": name, "mimeType": "text/plain", "buffer": contents})
        expect(page.locator("[data-beta-status]")).to_contain_text(message)
        expect(page.locator("#beta_source")).to_have_value(source)
    page.get_by_role("button", name="Find questions", exact=True).click()
    expect(page.locator("[data-beta-result]")).to_be_visible()


def test_beta_renders_source_markup_as_text_and_exports_after_clipboard_failure(
    beta_page: BetaPage, tmp_path: Path
) -> None:
    page = beta_page.page
    page.add_init_script(
        """Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText: async () => { throw new Error('Clipboard denied'); } },
        });"""
    )
    beta_page.open("memory-diff")
    markup = '<img src="https://invalid.example/pixel" onerror="window.betaInjected = true">'
    page.locator("#beta_source").fill(markup)
    page.locator("#beta_second").fill("A separate snapshot.")
    page.get_by_role("button", name="Compare snapshots", exact=True).click()
    expect(page.locator("[data-beta-result]")).to_be_visible()
    expect(page.locator("[data-beta-sections]")).to_contain_text(markup)
    assert page.locator("[data-beta-sections] img").count() == 0
    assert page.evaluate("window.betaInjected === undefined")
    page.get_by_role("button", name="Copy", exact=True).click()
    expect(page.locator("[data-beta-status]")).to_contain_text("Clipboard access is unavailable")
    with page.expect_download() as download_info:
        page.get_by_role("button", name="Export .md", exact=True).click()
    download = download_info.value
    assert download.suggested_filename == "beta-memory-diff.md"
    exported = tmp_path / download.suggested_filename
    download.save_as(exported)
    contents = exported.read_text(encoding="utf-8")
    assert markup in contents
    assert "A separate snapshot." in contents
    assert contents.startswith("# ")
