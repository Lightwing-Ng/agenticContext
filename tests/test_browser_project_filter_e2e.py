"""Isolated Local resources Project filtering. Code version: v1.2.0-codex.0."""

from __future__ import annotations

from collections.abc import Iterator
from threading import Thread
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from playwright.sync_api import Browser, Locator, Page, TimeoutError as PlaywrightTimeoutError, expect
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
    _wait_for_session_table_layout(page, require_source_hit=not opened)


def _wait_for_session_table_layout(page: Page, *, require_source_hit: bool) -> None:
    deadline = time.monotonic() + 30
    last_geometry_error: AssertionError | None = None
    while True:
        remaining_ms = (deadline - time.monotonic()) * 1_000
        if remaining_ms <= 0:
            if last_geometry_error is not None:
                raise last_geometry_error
            raise PlaywrightTimeoutError("Session table layout did not settle within 30,000ms.")
        try:
            geometry = _stable_session_table_geometry(page, timeout_ms=remaining_ms)
        except PlaywrightTimeoutError:
            if last_geometry_error is not None:
                raise last_geometry_error
            raise
        try:
            _assert_session_table_geometry(geometry, require_source_hit=require_source_hit)
        except AssertionError as error:
            # A stable sample can precede the queued table-column measurement.
            last_geometry_error = error
        else:
            return


def _stable_session_table_geometry(page: Page, *, timeout_ms: float) -> dict:
    settled = page.wait_for_function(
        """token => {
            const rect = node => {
                if (!node) return null;
                const box = node.getBoundingClientRect();
                return [box.x, box.y, box.width, box.height];
            };
            const table = document.querySelector('.browser-session-index-shell');
            const header = table?.querySelector('[data-table-header]');
            const body = table?.querySelector('[data-table-body]');
            const scrollport = table?.querySelector('[data-table-scroll]');
            const trigger = document.querySelector(
                '[data-browser-header-filter] [data-browser-source-filter-trigger]');
            const headerCells = Array.from(header?.rows[0]?.cells || [], rect);
            const bodyCells = Array.from(body?.rows[0]?.cells || [], rect);
            const geometry = JSON.stringify([
                rect(document.querySelector('.app-shell')),
                rect(document.querySelector('.sidebar')),
                rect(table), rect(header), rect(body), rect(trigger), headerCells, bodyCells,
            ]);
            const previous = window.__projectFilterLayoutSample;
            const frameTime = document.timeline.currentTime;
            const sameGeometry = previous?.token === token && previous.geometry === geometry;
            const frameAdvanced = Number.isFinite(frameTime)
                && Number.isFinite(previous?.frameTime) && frameTime > previous.frameTime;
            const frames = sameGeometry && Number.isFinite(frameTime)
                ? previous.frames + Number(frameAdvanced) : 0;
            window.__projectFilterLayoutSample = {token, geometry, frames, frameTime};
            // Aria updates before ResizeObserver queues the next header-column sync.
            // The immediate first predicate and each geometry change are baselines.
            if (frames < 3) return false;
            const box = trigger?.getBoundingClientRect();
            const centerX = box ? box.x + box.width / 2 : null;
            const centerY = box ? box.y + box.height / 2 : null;
            const centerInViewport = box && centerX >= 0 && centerX < innerWidth
                && centerY >= 0 && centerY < innerHeight;
            return {
                headerCells, bodyCells,
                hasTable: Boolean(table),
                scrollbarWidth: scrollport ? scrollport.offsetWidth - scrollport.clientWidth : 0,
                triggerHit: Boolean(centerInViewport
                    && trigger.contains(document.elementFromPoint(centerX, centerY))),
            };
        }""",
        arg=uuid4().hex,
        polling="raf",
        timeout=timeout_ms,
    )
    try:
        geometry = settled.json_value()
    finally:
        settled.dispose()
    return geometry


def _assert_session_table_geometry(geometry: dict, *, require_source_hit: bool) -> None:
    header_cells = geometry["headerCells"]
    body_cells = geometry["bodyCells"]
    if geometry["hasTable"]:
        assert header_cells and body_cells, (
            "The session table must have header and body columns."
        )
    assert len(header_cells) == len(body_cells)
    for index, (header, body) in enumerate(zip(header_cells, body_cells, strict=True)):
        scrollbar = geometry["scrollbarWidth"] if index == len(body_cells) - 1 else 0
        assert abs(header[0] - body[0]) <= 1, "Header/body column left edges differ."
        assert abs(header[2] - body[2] - scrollbar) <= 1, (
            "Header/body column widths differ."
        )
    if require_source_hit:
        assert geometry["triggerHit"], "The settled Source trigger is obstructed."


def test_session_columns_follow_container_query_without_outer_resize(
    disposable_browser: Browser,
    project_browser_server_url: str,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": 1_006, "height": 844}, reduced_motion="reduce",
    )
    context.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
        const style = document.createElement('style');
        style.id = 'session-column-distribution-control';
        style.textContent = `.browser-session-index-table[data-table-body] {
            --browser-session-number-width: 5%; --browser-session-title-width: 48%;
            --browser-session-source-width: 12%; --browser-session-count-width: 15%;
            --browser-session-updated-width: 20%;
        }`;
        document.head.append(style);
    }, {once: true});""")
    page = context.new_page()
    try:
        page.goto(
            f"{project_browser_server_url}/browser?view=text&source=chatgpt&session_view=1"
        )
        _sidebar(page, opened=False)
        before = page.locator(".browser-session-index-shell").evaluate("""shell => {
            const rect = node => {
                const box = node.getBoundingClientRect();
                return [box.x, box.y, box.width, box.height];
            };
            return [shell, shell.querySelector('[data-table-header]'),
                shell.querySelector('[data-table-body]')].map(rect);
        }""")
        # CSSOM changes do not notify a MutationObserver watching body attributes.
        page.evaluate("""() => document.getElementById(
            'session-column-distribution-control').sheet.deleteRule(0)""")
        _wait_for_session_table_layout(page, require_source_hit=True)
        after = page.locator(".browser-session-index-shell").evaluate("""shell => {
            const rect = node => {
                const box = node.getBoundingClientRect();
                return [box.x, box.y, box.width, box.height];
            };
            const body = shell.querySelector('[data-table-body]');
            return {outer: [shell, shell.querySelector('[data-table-header]'), body].map(rect),
                number: getComputedStyle(body).getPropertyValue('--browser-session-number-width').trim(),
                header: shell.querySelector('[data-table-header]').rows[0].cells[0].getBoundingClientRect().width,
                body: body.rows[0].cells[0].getBoundingClientRect().width};
        }""")
        assert before == after["outer"]
        assert after["number"] == "4%"
        assert abs(after["header"] - after["body"]) <= 1
    finally:
        context.close()


def test_shared_table_tracks_measurement_row_lifecycle_and_disconnects(
    disposable_browser: Browser,
    project_browser_server_url: str,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": 1_006, "height": 844}, reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def columns_follow(row_id: str) -> None:
        page.wait_for_function("""rowId => {
            const header = document.querySelector('#column-lifecycle [data-table-header]');
            const row = document.getElementById(rowId);
            return row && Array.from(header.rows[0].cells).every((cell,index) => (
                Math.abs(cell.getBoundingClientRect().width
                    - row.cells[index].getBoundingClientRect().width) <= 1
            ));
        }""", arg=row_id)

    def advance_frames() -> None:
        page.evaluate("""() => new Promise(resolve => {
            let frames = 0;
            const next = () => ++frames === 8 ? resolve() : requestAnimationFrame(next);
            requestAnimationFrame(next);
        })""")

    def redistribute(value: str) -> None:
        page.evaluate("""value => document.getElementById('column-lifecycle-style')
            .sheet.cssRules[0].style.setProperty('--test-column-width',value)""", value)

    try:
        page.goto(
            f"{project_browser_server_url}/browser?view=text&source=chatgpt&session_view=1"
        )
        page.evaluate("""() => {
            const style = document.createElement('style');
            style.id = 'column-lifecycle-style';
            style.textContent = `#column-lifecycle { --test-column-width: 20%; position:relative; width:600px; }
                #column-lifecycle table { width:600px; table-layout:fixed; border-collapse:collapse; }
                #column-lifecycle [data-table-header] { position:absolute; top:0; left:0; }
                #column-lifecycle [data-table-body] { height:80px; }
                #column-lifecycle td:first-child { width:var(--test-column-width); }`;
            document.head.append(style);
            const shell = document.createElement('div');
            // Direct attachment avoids a second controller from automatic shell discovery.
            shell.id = 'column-lifecycle';
            shell.innerHTML = `<table data-table-header><thead><tr><th>No.</th><th>Value</th></tr></thead></table>
                <div data-table-scroll><table data-table-body><tbody>
                    <tr id="lifecycle-first"><td>1</td><td>First</td></tr>
                </tbody></table></div>`;
            document.body.append(shell);
            window.__columnLifecycleCleanup = SHARED_SCROLLABLE_DATA_TABLE.attach(shell);
        }""")
        columns_follow("lifecycle-first")
        outer = page.locator("#column-lifecycle [data-table-body]").evaluate(
            "body => [body.getBoundingClientRect().width, body.getBoundingClientRect().height]"
        )
        page.evaluate("""() => document.querySelector('#column-lifecycle tbody').innerHTML =
            '<tr id="lifecycle-replaced"><td>2</td><td>Replacement</td></tr>'""")
        redistribute("30%")
        columns_follow("lifecycle-replaced")
        page.evaluate("""() => {
            document.getElementById('lifecycle-replaced').hidden = true;
            document.querySelector('#column-lifecycle tbody').insertAdjacentHTML('beforeend',
                '<tr id="lifecycle-visible"><td>3</td><td>Visible</td></tr>');
        }""")
        redistribute("40%")
        columns_follow("lifecycle-visible")
        previous_width = page.locator("#column-lifecycle th").first.evaluate(
            "cell => cell.getBoundingClientRect().width"
        )
        page.evaluate("""() => document.querySelector('#column-lifecycle tbody').innerHTML =
            '<tr data-table-empty-row><td colspan="2">Empty</td></tr>'""")
        advance_frames()
        page.evaluate("""() => document.querySelector('#column-lifecycle tbody').innerHTML =
            '<tr id="lifecycle-summary" data-table-summary-row><td>4</td><td>Summary</td></tr>'""")
        redistribute("50%")
        advance_frames()
        assert page.locator("#column-lifecycle th").first.evaluate(
            "cell => cell.getBoundingClientRect().width"
        ) == previous_width
        # An eligibility-only attribute change must discover the new first measurable row.
        page.locator("#lifecycle-summary").evaluate(
            "row => row.removeAttribute('data-table-summary-row')"
        )
        columns_follow("lifecycle-summary")
        redistribute("60%")
        columns_follow("lifecycle-summary")
        assert page.locator("#column-lifecycle [data-table-body]").evaluate(
            "body => [body.getBoundingClientRect().width, body.getBoundingClientRect().height]"
        ) == outer
        final_width = page.locator("#column-lifecycle th").first.evaluate(
            "cell => cell.getBoundingClientRect().width"
        )
        page.evaluate("window.__columnLifecycleCleanup()")
        redistribute("70%")
        page.evaluate("""() => {
            document.querySelector('#lifecycle-summary td').textContent = 'After cleanup';
            window.dispatchEvent(new Event('resize'));
        }""")
        advance_frames()
        assert page.locator("#column-lifecycle th").first.evaluate(
            "cell => cell.getBoundingClientRect().width"
        ) == final_width
        assert abs(final_width - page.locator("#lifecycle-summary td").first.evaluate(
            "cell => cell.getBoundingClientRect().width"
        )) > 1
        assert not errors
    finally:
        context.close()


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
    ("fault", "message"),
    (
        ("column-offset", "Header/body column left edges"),
        ("obstruction", "Source trigger is obstructed"),
    ),
)
def test_sidebar_layout_readiness_rejects_stable_geometry_defects(
    disposable_browser: Browser,
    project_browser_server_url: str,
    fault: str,
    message: str,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": 1_006, "height": 844},
        reduced_motion="reduce",
    )
    page = context.new_page()
    try:
        page.goto(
            f"{project_browser_server_url}/browser?view=text&source=chatgpt&session_view=1"
        )
        _sidebar(page, opened=False)
        if fault == "column-offset":
            page.add_style_tag(
                content=".browser-session-index-shell > [data-table-header] { transform: translateX(4px); }",
            )
        else:
            page.locator(
                "[data-browser-header-filter] [data-browser-source-filter-trigger]",
            ).evaluate("""trigger => {
                const rect = trigger.getBoundingClientRect();
                const obstruction = document.createElement('div');
                Object.assign(obstruction.style, {
                    position: 'fixed', left: `${rect.x}px`, top: `${rect.y}px`,
                    width: `${rect.width}px`, height: `${rect.height}px`, zIndex: '2147483647',
                });
                document.body.append(obstruction);
            }""")
        # Stability must not conceal a persistent alignment or hit-test defect.
        with pytest.raises(AssertionError, match=message):
            _sidebar(page, opened=False)
    finally:
        context.close()


@pytest.mark.parametrize("deferred_frames", (2, 5))
def test_sidebar_layout_readiness_waits_for_deferred_table_measurement(
    disposable_browser: Browser,
    project_browser_server_url: str,
    deferred_frames: int,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": 1_006, "height": 844},
        reduced_motion="reduce",
    )
    context.add_init_script("""(() => {
        const NativeResizeObserver = window.ResizeObserver;
        const nextFrame = window.requestAnimationFrame.bind(window);
        window.__deferredTableDeliveries = 0;
        window.__deferredTableFrameTimes = [];
        window.ResizeObserver = class extends NativeResizeObserver {
            constructor(callback) {
                super((entries, observer) => {
                    if (entries.some(entry => entry.target.closest('.browser-session-index-shell'))) {
                        // Retain the real table observer and column sync across deferred frames.
                        const deliver = (remaining, frameTimes) => {
                            if (remaining > 0) {
                                nextFrame(frameTime => deliver(
                                    remaining - 1, [...frameTimes, frameTime]));
                                return;
                            }
                            window.__deferredTableDeliveries += 1;
                            window.__deferredTableFrameTimes = frameTimes;
                            callback(entries, observer);
                        };
                        deliver(DEFERRED_FRAMES, []);
                    } else {
                        callback(entries, observer);
                    }
                });
            }
        };
    })();""".replace("DEFERRED_FRAMES", str(deferred_frames)))
    page = context.new_page()
    try:
        page.goto(
            f"{project_browser_server_url}/browser?view=text&source=chatgpt&session_view=1"
        )
        _choose_project(page, "No project")
        expect(page.locator(SESSION_TITLES)).to_have_text(["Ordinary session"])
        deliveries_before = page.evaluate("window.__deferredTableDeliveries")
        _sidebar(page, opened=False)
        assert page.evaluate("window.__deferredTableDeliveries") > deliveries_before
        frame_times = page.evaluate("window.__deferredTableFrameTimes")
        assert len(frame_times) == deferred_frames
        assert all(later > earlier for earlier, later in zip(frame_times, frame_times[1:]))
        page.locator(
            "[data-browser-header-filter] [data-browser-source-filter-trigger]"
        ).click()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.locator(
                '#browser_header_source_filter_options [data-browser-source-filter-option="chatgpt"]'
            ).click()
        assert _query(page)["project"] == [CHATGPT_NO_PROJECT_FILTER]
        expect(page.locator(SESSION_TITLES)).to_have_text(["Ordinary session"])
    finally:
        context.close()


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
        _wait_for_session_table_layout(page, require_source_hit=True)
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
        _wait_for_session_table_layout(page, require_source_hit=True)
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
