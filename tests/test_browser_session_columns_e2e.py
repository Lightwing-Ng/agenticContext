"""Container-sized cached session column contracts. Code version: v1.0.0-codex.0."""

from collections.abc import Iterator
from threading import Thread

import pytest
from playwright.sync_api import Browser, Page, expect
from werkzeug.serving import make_server

from app.core.resource_persistence import (
    CHATGPT_HISTORY_SCHEMA,
    CLAUDE_HISTORY_SCHEMA,
    GEMINI_HISTORY_SCHEMA,
    ZHIHU_HISTORY_SCHEMA,
    write_parquet_rows_atomic,
)
from tests import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser


@pytest.fixture(scope="module")
def session_column_browser_server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Serve long names, identifiers, timestamps, and a four-digit message count."""
    from app.web.app import create_app

    sandbox = tmp_path_factory.mktemp("browser-session-columns")
    root = sandbox / "local-store"
    for source, schema in (
        ("chatgpt", CHATGPT_HISTORY_SCHEMA),
        ("claude", CLAUDE_HISTORY_SCHEMA),
        ("gemini", GEMINI_HISTORY_SCHEMA),
        ("zhihu", ZHIHU_HISTORY_SCHEMA),
    ):
        rows = []
        for index in range(24):
            session_id = f"{source}-{index}-" + "LongUnbrokenSessionIdentifier" * 3
            title = f"{source.title()} session {index}: " + "LongUnbrokenConversationTitle" * 4
            count = 1_024 if source == "chatgpt" and index == 0 else 1
            for message_index in range(count):
                rows.append({
                    "schema_version": 1,
                    "platform": source,
                    "conversation_id": session_id,
                    "conversation_url": f"https://example.test/{source}/{session_id}",
                    "conversation_title": title,
                    "message_key": f"answer:{session_id}" if source == "zhihu" else f"{session_id}:{message_index}",
                    "turn_index": message_index,
                    "message_index": message_index,
                    "role": "answer" if source == "zhihu" else "user",
                    "author_label": f"Research contributor {index} with a long author name" if source == "zhihu" else "You",
                    "content_text": "Session column fixture.",
                    "content_html": "",
                    "content_sha256": f"fixture-{source}-{index}-{message_index}",
                    "source_links": [],
                    "model_label": "",
                    "first_seen_at": "2026-09-26T10:00:00Z",
                    "last_seen_at": f"2026-09-{index + 1:02d}T05:01:02Z",
                })
        write_parquet_rows_atomic(root / f"llm/{source}/history.parquet", rows, schema)
    application = create_app(
        root,
        computer_use_settings_path=sandbox / "settings/computer-use-agent.json",
        computer_use_runtime_root=sandbox / "computer-use-runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, application, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _session_table_geometry(page: Page) -> dict:
    page.evaluate("""async () => {
        await document.fonts.ready;
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")
    return page.locator(".browser-session-index-shell").evaluate("""shell => {
        const header = shell.querySelector('[data-table-header]');
        const body = shell.querySelector('[data-table-body]');
        const scroll = shell.querySelector('[data-table-scroll]');
        const rectangle = element => {
            const box = element.getBoundingClientRect();
            return {left: box.left, right: box.right, top: box.top, bottom: box.bottom,
                width: box.width, height: box.height};
        };
        const numberLabel = document.createRange();
        numberLabel.selectNodeContents(header.rows[0].cells[0]);
        return {
            shell: rectangle(shell), header: rectangle(header), body: rectangle(body),
            clientWidth: scroll.clientWidth,
            scrollHeight: scroll.scrollHeight, clientHeight: scroll.clientHeight,
            headings: [...header.rows[0].cells].map(rectangle),
            numberLabelLines: [...numberLabel.getClientRects()].map(box => ({left: box.left, right: box.right})),
            cells: [...body.rows[0].cells].map(rectangle),
            sources: [...body.querySelectorAll('.browser-session-source-mark')].map(icon => {
                const cell = icon.closest('td');
                const cellBox = cell.getBoundingClientRect();
                const iconBox = icon.getBoundingClientRect();
                return {
                    x: iconBox.left + iconBox.width / 2 - cellBox.left - cellBox.width / 2,
                    y: iconBox.top + iconBox.height / 2 - cellBox.top - cellBox.height / 2,
                    width: iconBox.width, height: iconBox.height,
                };
            }),
            overflow: [shell, header, scroll, body,
                ...body.querySelectorAll('td, .browser-session-table-title, .browser-session-table-id, time')]
                .map(element => ({tag: element.tagName, extra: element.scrollWidth - element.clientWidth})),
            documentOverflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - innerWidth,
        };
    }""")


def _assert_session_columns(page: Page, *, zhihu: bool) -> dict:
    geometry = _session_table_geometry(page)
    container_width = geometry["body"]["width"]
    if container_width > 800:
        percentages = (4, 66, 10, 20) if zhihu else (4, 56, 10, 10, 20)
    elif container_width > 500:
        percentages = (5, 60, 15, 20) if zhihu else (5, 48, 12, 15, 20)
    else:
        percentages = (8, 50, 18, 24) if zhihu else (8, 28, 18, 22, 24)
    assert sum(percentages) == 100
    assert all(value % 2 == 0 or value % 5 == 0 for value in percentages)
    assert percentages[0] == min(percentages) and percentages[1] == max(percentages)
    assert len(geometry["cells"]) == len(percentages), geometry
    assert len(geometry["numberLabelLines"]) == 1, geometry
    number_label = geometry["numberLabelLines"][0]
    assert number_label["left"] >= geometry["headings"][0]["left"], geometry
    assert number_label["right"] <= geometry["headings"][0]["right"], geometry
    assert geometry["body"]["width"] == pytest.approx(geometry["clientWidth"], abs=1), geometry
    assert geometry["header"]["width"] == pytest.approx(geometry["body"]["width"], abs=1), geometry
    assert geometry["body"]["left"] >= geometry["shell"]["left"] - 1, geometry
    assert geometry["body"]["right"] <= geometry["shell"]["right"] + 1, geometry
    for heading, cell, percentage in zip(geometry["headings"], geometry["cells"], percentages, strict=True):
        assert cell["width"] == pytest.approx(container_width * percentage / 100, abs=1), geometry
        assert heading["left"] == pytest.approx(cell["left"], abs=1), geometry
        assert heading["width"] == pytest.approx(cell["width"], abs=1), geometry
    assert all(item["extra"] <= 1 for item in geometry["overflow"]), geometry
    assert geometry["documentOverflow"] <= 1, geometry
    if not zhihu:
        assert geometry["sources"], geometry
        for icon in geometry["sources"]:
            assert abs(icon["x"]) <= 1 and abs(icon["y"]) <= 1, icon
            assert icon["width"] == 24 and icon["height"] == 24, icon
    return geometry


@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_006, 791, False), (1_920, 1_080, False), (390, 844, True), (1_006, 500, False)),
)
@pytest.mark.parametrize("source", ("chatgpt", "zhihu"))
def test_session_columns_fill_container_and_preserve_navigation(
    disposable_browser: Browser,
    session_column_browser_server_url: str,
    width: int,
    height: int,
    touch: bool,
    source: str,
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
        page.goto(f"{session_column_browser_server_url}/browser?view=text&source={source}&session_view=1")
        shell = page.locator(".browser-session-index-shell")
        expect(shell).to_be_visible()
        geometry = _assert_session_columns(page, zhihu=source == "zhihu")
        assert geometry["scrollHeight"] > geometry["clientHeight"], geometry
        scrollport = shell.locator("[data-table-scroll]")
        scrollport.evaluate("node => { node.scrollTop = node.scrollHeight; node.scrollLeft = 1000; }")
        assert scrollport.evaluate("node => node.scrollLeft") == 0
        _assert_session_columns(page, zhihu=source == "zhihu")
        scrollport.evaluate("node => { node.scrollTop = 0; }")
        page.set_viewport_size({"width": 820 if width > 1_000 else 1_006, "height": 791})
        _assert_session_columns(page, zhihu=source == "zhihu")
        page.set_viewport_size({"width": width, "height": height})
        _assert_session_columns(page, zhihu=source == "zhihu")
        if source == "chatgpt":
            expect(shell.locator(".browser-session-table-count").get_by_text("1,024", exact=True)).to_have_count(1)
            trigger = shell.locator("[data-browser-source-filter-trigger]")
            trigger.focus()
            trigger.click()
            expect(page.locator("#browser_header_source_filter_options")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#browser_header_source_filter_options")).to_be_hidden()
            trigger.click()
            page.locator("#browser_header_source_filter_options").get_by_role("option", name="Claude", exact=True).click()
            expect(page.locator(".browser-session-source-mark").first).to_have_attribute("aria-label", "Claude")
            _assert_session_columns(page, zhihu=False)
        page.locator(".browser-session-sort-link").click()
        expect(page.locator(".browser-session-col-updated")).to_have_attribute("aria-sort", "ascending")
        _assert_session_columns(page, zhihu=source == "zhihu")
        title = page.locator(".browser-session-index-table[data-table-body] .browser-session-table-title").first
        destination = title.get_attribute("href")
        title.click()
        assert destination
        expect(page).to_have_url(session_column_browser_server_url + destination)
        assert not errors
    finally:
        context.close()
