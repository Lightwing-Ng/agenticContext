"""Isolated Local resources Project filtering. Code version: v1.1.0-codex.0."""

from __future__ import annotations

from collections.abc import Iterator
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Browser, Locator, Page, expect
from werkzeug.serving import make_server

from app.core.agent_source_cache import AgentSourceCache
from app.core.chat_history_browser import CHATGPT_NO_PROJECT_FILTER
from app.core.resource_persistence import CHATGPT_HISTORY_SCHEMA, write_parquet_rows_atomic
from tests import test_sidebar_e2e


ALPHA_PROJECT = "g-p-" + "a" * 32
BETA_PROJECT = "g-p-" + "b" * 32
SESSION_TITLES = ".browser-session-index-table[data-table-body] .browser-session-table-title"
disposable_browser = test_sidebar_e2e.disposable_browser


@pytest.fixture(scope="module")
def project_browser_server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Publish synthetic history and catalogs without accessing the user's cache."""
    from app.web.app import create_app

    sandbox = tmp_path_factory.mktemp("browser-project-filter-e2e")
    root = sandbox / "local-store"
    cache = AgentSourceCache(root)
    projects = []
    history = []
    for key, name, count in ((ALPHA_PROJECT, "Alpha", 102), (BETA_PROJECT, "Beta", 1)):
        project_url = f"https://chatgpt.com/g/{key}/project"
        projects.append({"id": key, "title": name, "url": project_url})
        sessions = []
        for index in range(count):
            session_id = f"{name.lower()}-{index:03d}"
            title = f"{name} session {index:03d}"
            session_url = f"https://chatgpt.com/c/{session_id}"
            sessions.append({"id": session_id, "title": title, "url": session_url})
            history.append(
                {
                    "schema_version": 1,
                    "platform": "chatgpt",
                    "conversation_id": session_id,
                    "conversation_url": session_url,
                    "conversation_title": title,
                    "message_key": f"{session_id}:0:user",
                    "turn_index": 0,
                    "message_index": 0,
                    "role": "user",
                    "author_label": "You",
                    "content_text": "sharedneedle" if index == 0 else f"Synthetic message {index}",
                    "content_html": "",
                    "content_sha256": f"fixture-{session_id}",
                    "source_links": [],
                    "model_label": "",
                    "first_seen_at": "2026-09-20T10:00:00Z",
                    "last_seen_at": "2026-09-20T10:00:00Z",
                }
            )
        cache.store(
            platform="chatgpt",
            browser="edge",
            source_kind="project-sessions",
            project_url=project_url,
            payload={"sessions": sessions},
        )
    cache.store(
        platform="chatgpt",
        browser="edge",
        source_kind="sources",
        payload={"projects": projects, "recent_sessions": [
            {"id": "ordinary-000", "url": "https://chatgpt.com/c/ordinary-000"},
        ]},
    )
    unassigned = dict(history[0])
    unassigned.update(
        conversation_id="unassigned-000",
        conversation_url="https://chatgpt.com/c/unassigned-000",
        conversation_title="Unassigned session",
        message_key="unassigned-000:0:user",
        content_sha256="fixture-unassigned",
    )
    history.append(unassigned)
    ordinary = dict(unassigned)
    ordinary.update(
        conversation_id="ordinary-000",
        conversation_url="https://chatgpt.com/c/ordinary-000",
        conversation_title="Ordinary session",
        message_key="ordinary-000:0:user",
        content_sha256="fixture-ordinary",
    )
    history.append(ordinary)
    write_parquet_rows_atomic(root / "llm/chatgpt/history.parquet", history, CHATGPT_HISTORY_SCHEMA)
    application = create_app(
        root,
        computer_use_settings_path=sandbox / "settings/computer-use-agent.json",
        computer_use_runtime_root=sandbox / "computer-use-runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    assert server.server_port != 8666
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _sidebar(page: Page, *, opened: bool) -> None:
    toggle = page.locator("#sidebar_toggle")
    if (toggle.get_attribute("aria-expanded") == "true") != opened:
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", str(opened).lower())


def _project_trigger(page: Page) -> Locator:
    return page.locator('[data-shared-select-kind="project"] [data-shared-select-trigger]')


def _choose_project(page: Page, name: str) -> None:
    _sidebar(page, opened=True)
    trigger = _project_trigger(page)
    trigger.click()
    menu = page.locator("#" + trigger.get_attribute("aria-controls"))
    with page.expect_navigation(wait_until="domcontentloaded"):
        menu.get_by_role("option", name=name, exact=True).click()


def _query(page: Page) -> dict[str, list[str]]:
    return parse_qs(urlsplit(page.url).query)


def _assert_no_horizontal_overflow(page: Page) -> None:
    assert page.evaluate(
        "Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)"
        " <= window.innerWidth + 1"
    )


@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_006, 791, False), (390, 844, True), (1_006, 500, True)),
)
def test_project_dropdown_is_usable_and_filters_at_responsive_sizes(
    disposable_browser: Browser,
    project_browser_server_url: str,
    width: int,
    height: int,
    touch: bool,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{project_browser_server_url}/browser?view=text&source=chatgpt&session_view=1")
        _sidebar(page, opened=True)
        native = page.locator("#browser_project_filter")
        expect(native).to_have_attribute("aria-label", "Filter by project")
        expect(native).to_be_hidden()
        trigger = _project_trigger(page)
        expect(trigger).to_be_visible()
        trigger.click()
        menu = page.locator("#" + trigger.get_attribute("aria-controls"))
        expect(menu).to_be_visible()
        option = menu.get_by_role("option", name="Alpha", exact=True)
        expect(option).to_be_visible()
        for control in (trigger, menu, option):
            bounds = control.bounding_box()
            assert bounds is not None
            assert bounds["x"] >= -1
            assert bounds["y"] >= -1
            assert bounds["x"] + bounds["width"] <= width + 1
            assert bounds["y"] + bounds["height"] <= height + 1
        assert option.evaluate(
            "element => { const rect = element.getBoundingClientRect();"
            " return element.contains(document.elementFromPoint("
            "rect.x + rect.width / 2, rect.y + rect.height / 2)); }"
        )
        _assert_no_horizontal_overflow(page)
        with page.expect_navigation(wait_until="domcontentloaded"):
            option.click()
        expect(page.locator("#browser_project_filter")).to_have_value(ALPHA_PROJECT)
        assert _query(page)["project"] == [ALPHA_PROJECT]
        titles = page.locator(SESSION_TITLES)
        expect(titles).to_have_count(100)
        assert all(title.startswith("Alpha session") for title in titles.all_text_contents())
        _assert_no_horizontal_overflow(page)
        _choose_project(page, "Beta")
        expect(page.locator(SESSION_TITLES)).to_have_text(["Beta session 000"])
        assert _query(page)["project"] == [BETA_PROJECT]
        _assert_no_horizontal_overflow(page)
        assert not errors
    finally:
        context.close()


def test_project_filter_survives_navigation_and_clears_other_scopes(
    disposable_browser: Browser, project_browser_server_url: str,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": 1_006, "height": 791}, reduced_motion="reduce",
    )
    page = context.new_page()
    try:
        page.goto(f"{project_browser_server_url}/browser?view=text&source=chatgpt&session_view=1")
        _choose_project(page, "Alpha")
        sort_trigger = page.locator('[data-shared-select-kind="sort"] [data-shared-select-trigger]')
        sort_trigger.click()
        sort_menu = page.locator("#" + sort_trigger.get_attribute("aria-controls"))
        with page.expect_navigation(wait_until="domcontentloaded"):
            sort_menu.get_by_role("option", name="Session title", exact=True).click()
        assert _query(page)["project"] == [ALPHA_PROJECT]
        assert _query(page)["sort"] == ["name"]
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.get_by_role("link", name="Session page 2", exact=True).click()
        expect(page.locator(SESSION_TITLES)).to_have_text(["Alpha session 100", "Alpha session 101"])
        assert _query(page)["project"] == [ALPHA_PROJECT]
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator(SESSION_TITLES).first.click()
        expect(page.locator(".browser-heading-copy h2")).to_have_text("Alpha session 100")
        assert _query(page)["project"] == [ALPHA_PROJECT]
        assert _query(page)["session_page"] == ["2"]
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.get_by_role("link", name="Back to project sessions", exact=True).click()
        assert _query(page)["project"] == [ALPHA_PROJECT]
        assert _query(page)["page"] == ["2"]
        page.locator("#browser_search_input").fill("sharedneedle")
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator("#browser_search_input").press("Enter")
        assert _query(page)["project"] == [ALPHA_PROJECT]
        assert _query(page)["q"] == ["sharedneedle"]
        expect(page.locator(".browser-chat-message-title")).to_have_count(0)
        expect(page.locator(".browser-chat-message")).to_have_count(1)
        expect(page.get_by_role("link", name="Open session", exact=True)).to_have_attribute(
            "href", "https://chatgpt.com/c/alpha-000"
        )
        expect(page.locator(".browser-chat-message-content")).to_have_text(["sharedneedle"])
        page.go_back(wait_until="domcontentloaded")
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator(SESSION_TITLES).first.click()
        assert _query(page).get("session")
        _choose_project(page, "Beta")
        assert "session" not in _query(page)
        expect(page.locator(SESSION_TITLES)).to_have_text(["Beta session 000"])
        source_filter = page.locator("#browser_filter_form [data-browser-source-filter]")
        source_filter.locator("[data-browser-source-filter-trigger]").click()
        with page.expect_navigation(wait_until="domcontentloaded"):
            source_filter.locator('[data-browser-source-filter-option="gemini"]').click()
        assert _query(page)["source"] == ["gemini"]
        assert "project" not in _query(page)
        expect(page.locator("#browser_project_filter")).to_have_count(0)
        scoped_url = (
            f"{project_browser_server_url}/browser?view=text&source=chatgpt"
            f"&session_view=1&project={ALPHA_PROJECT}"
        )
        page.goto(scoped_url)
        page.locator("[data-browser-header-filter] [data-browser-source-filter-trigger]").click()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator(
                '#browser_header_source_filter_options [data-browser-source-filter-option="gemini"]'
            ).click()
        assert _query(page)["source"] == ["gemini"]
        assert "project" not in _query(page)
        expect(page.locator("#browser_project_filter")).to_have_count(0)
        for view in ("media", "prompts"):
            page.goto(scoped_url)
            _sidebar(page, opened=True)
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.locator(f'label[for="browser_view_{view}"]').click()
            assert _query(page)["view"] == [view]
            assert "project" not in _query(page)
            expect(page.locator("#browser_project_filter")).to_have_count(0)
        _assert_no_horizontal_overflow(page)
    finally:
        context.close()


@pytest.mark.parametrize("width", (1_006, 390))
def test_no_project_selection_survives_search_details_and_same_source_selection(
    disposable_browser: Browser, project_browser_server_url: str, width: int,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 844}, reduced_motion="reduce",
        has_touch=width == 390,
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"{project_browser_server_url}/browser?view=text&source=chatgpt&session_view=1")
        _choose_project(page, "No project")
        expect(page.locator("#browser_project_filter")).to_have_value(CHATGPT_NO_PROJECT_FILTER)
        expect(page.locator(SESSION_TITLES)).to_have_text(["Ordinary session"])
        assert _query(page)["project"] == [CHATGPT_NO_PROJECT_FILTER]
        _sidebar(page, opened=False)
        page.locator("[data-browser-header-filter] [data-browser-source-filter-trigger]").click()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator(
                '#browser_header_source_filter_options [data-browser-source-filter-option="chatgpt"]'
            ).click()
        assert _query(page)["project"] == [CHATGPT_NO_PROJECT_FILTER]
        expect(page.locator(SESSION_TITLES)).to_have_text(["Ordinary session"])
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator(SESSION_TITLES).click()
        expect(page.locator(".browser-heading-copy h2")).to_have_text("Ordinary session")
        expect(page.get_by_role("link", name="Back to No project", exact=True)).to_be_visible()
        expect(page.locator("[data-browser-session-scope-remove]")).to_have_attribute(
            "aria-label", "Search chats without a project",
        )
        page.locator("#browser_search_input").fill("sharedneedle")
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator("#browser_search_input").press("Enter")
        assert _query(page)["project"] == [CHATGPT_NO_PROJECT_FILTER]
        expect(page.locator(".browser-chat-message-content")).to_have_text(["sharedneedle"])
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.get_by_role("link", name="Back to No project", exact=True).click()
        assert _query(page)["project"] == [CHATGPT_NO_PROJECT_FILTER]
        expect(page.locator(SESSION_TITLES)).to_have_text(["Ordinary session"])
        expect(page.locator("#browser_search_input")).to_have_attribute(
            "data-browser-search-submit-copy", "Press Enter to search chats without a project.",
        )
        page.locator("[data-browser-header-filter] [data-browser-source-filter-trigger]").click()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator(
                '#browser_header_source_filter_options [data-browser-source-filter-option="gemini"]'
            ).click()
        assert "project" not in _query(page)
        _assert_no_horizontal_overflow(page)
        assert not errors
    finally:
        context.close()
