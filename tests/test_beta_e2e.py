"""Disposable-browser coverage for isolated Beta experiment workflows.

Code version: v0.5.0-codex.1
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile

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
            content: [...document.querySelectorAll('.beta-content, .beta-input-card, .beta-result')]
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
    ) == [title for _experiment, title, _action in EXPERIMENTS]
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
