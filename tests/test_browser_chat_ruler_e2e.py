"""Isolated message ruler behavior and geometry. Code version: v1.1.1-codex.0."""

from collections.abc import Iterator
from pathlib import Path
from threading import Thread

import pytest
from playwright.sync_api import Browser, Page, expect
from werkzeug.serving import BaseWSGIServer, make_server

from app.core.resource_persistence import CHATGPT_HISTORY_SCHEMA, write_parquet_rows_atomic
from tests import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser
seeded_chatgpt_table_browser_server_url = test_sidebar_e2e.seeded_chatgpt_table_browser_server_url


@pytest.fixture()
def seeded_ruler_browser_server_url(tmp_path: Path) -> Iterator[str]:
    """Render enough genuine message markers to measure the neighboring lens effect."""
    from app.web.app import create_app

    root = tmp_path / "local-store"
    rows = []
    for index in range(7):
        role = "user" if index % 2 == 0 else "assistant"
        content = f"Message {index + 1}. " + "Conversation detail. " * 20
        if index == 3:
            content += "[Reference detail](https://example.test/reference)."
        rows.append({
            "schema_version": 1,
            "platform": "chatgpt",
            "conversation_id": "ruler-demo",
            "conversation_url": "https://chatgpt.com/c/ruler-demo",
            "conversation_title": "Ruler message fixture",
            "message_key": f"ruler-demo:{index}:{role}",
            "turn_index": index,
            "message_index": index,
            "role": role,
            "author_label": "You" if role == "user" else "ChatGPT",
            "content_text": content,
            "content_html": "",
            "content_sha256": f"ruler-demo-hash-{index}",
            "source_links": ["https://example.test/reference"] if index == 3 else [],
            "model_label": "",
            "first_seen_at": "2026-09-26T04:59:00Z",
            "last_seen_at": "2026-09-26T04:59:00Z" if index == 1 else "2026-09-26T05:00:00Z",
        })
    write_parquet_rows_atomic(root / "llm" / "chatgpt" / "history.parquet", rows, CHATGPT_HISTORY_SCHEMA)
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


def _ruler_geometry(page: Page) -> dict:
    return page.locator("[data-browser-chat-pane]").evaluate("""pane => {
        const rectangle = node => {
            const box = node.getBoundingClientRect();
            return {x: box.x, y: box.y, width: box.width, height: box.height};
        };
        const markers = [...pane.querySelectorAll('[data-chat-marker]')];
        return {
            markers: markers.map(rectangle),
            messages: [...pane.querySelectorAll('[data-chat-message-id]')].map(rectangle),
            ticks: markers.map(marker => {
                const style = getComputedStyle(marker, '::before');
                const matrix = new DOMMatrixReadOnly(style.transform);
                return {width: parseFloat(style.width), scale: matrix.a, shift: matrix.e};
            }),
            overflow: document.documentElement.scrollWidth - innerWidth,
        };
    }""")


def _wait_for_lens(page: Page, index: int, magnified: bool) -> None:
    page.wait_for_function("""({index, magnified}) => {
        const marker = document.querySelectorAll('[data-chat-marker]')[index];
        const matrix = new DOMMatrixReadOnly(getComputedStyle(marker, '::before').transform);
        return magnified ? matrix.a > 1.59 : Math.abs(matrix.a - 1) < 0.001;
    }""", arg={"index": index, "magnified": magnified})


def _assert_message_badges(page: Page) -> None:
    articles = page.locator("article.browser-chat-message")
    expect(articles.locator(".browser-chat-message-title")).to_have_count(0)
    expect(articles.get_by_text("Unknown time", exact=True)).to_have_count(0)
    for index in range(articles.count()):
        badge = articles.nth(index).locator(".browser-chat-message-number")
        expect(badge).to_have_text(str(index + 1))
        assert "investment-holdings-allocation-badge" in badge.get_attribute("class")
        geometry = badge.evaluate("""badge => {
            const header = badge.closest('.browser-chat-message-header');
            const article = badge.closest('.browser-chat-message');
            const box = badge.getBoundingClientRect();
            const headerBox = header.getBoundingClientRect();
            const style = getComputedStyle(badge);
            const cardStyle = getComputedStyle(article);
            const time = header.querySelector('.browser-session-message-time');
            const timeBox = time?.getBoundingClientRect();
            const actionsBox = header.querySelector('.browser-session-message-actions')?.getBoundingClientRect();
            const mutedProbe = document.createElement('span');
            mutedProbe.style.color = 'var(--theme-muted)';
            badge.append(mutedProbe);
            const mutedColor = getComputedStyle(mutedProbe).color;
            mutedProbe.remove();
            return {rightGap: headerBox.right - box.right, topGap: box.top - headerBox.top,
                background: style.backgroundColor, color: style.color, mutedColor,
                padding: [cardStyle.paddingTop, cardStyle.paddingRight,
                    cardStyle.paddingBottom, cardStyle.paddingLeft],
                radius: parseFloat(style.borderTopRightRadius), height: box.height,
                timeRightGap: timeBox ? box.right - timeBox.right : null,
                timeBelowGap: timeBox ? timeBox.top - Math.max(box.bottom, actionsBox?.bottom || box.bottom) : null,
                actionsGap: actionsBox ? box.left - actionsBox.right : null,
                timeOverlaps: timeBox && Math.min(box.right, timeBox.right) > Math.max(box.left, timeBox.left)
                    && Math.min(box.bottom, timeBox.bottom) > Math.max(box.top, timeBox.top)};
        }""")
        assert abs(geometry["rightGap"]) <= 1, geometry
        assert abs(geometry["topGap"]) <= 4, geometry
        assert not geometry["timeOverlaps"], geometry
        assert geometry["padding"] == ["8px", "12px", "8px", "12px"], geometry
        assert geometry["color"] == geometry["mutedColor"], geometry
        assert geometry["background"] != "rgba(0, 0, 0, 0)", geometry
        assert geometry["background"] != geometry["color"], geometry
        assert geometry["radius"] < geometry["height"] / 2, geometry
        if geometry["timeRightGap"] is not None:
            assert abs(geometry["timeRightGap"]) <= 1, geometry
            assert geometry["timeBelowGap"] == pytest.approx(4, abs=1), geometry
        if geometry["actionsGap"] is not None:
            assert geometry["actionsGap"] >= 7, geometry


def _scroll_positions(page: Page) -> dict:
    return page.locator("[data-chat-scrollport]").evaluate("""scrollport => {
        const ancestors = [];
        for (let node = scrollport.parentElement; node; node = node.parentElement) {
            ancestors.push({top: node.scrollTop, left: node.scrollLeft});
        }
        return {messageTop: scrollport.scrollTop, ancestors, windowX: scrollX, windowY: scrollY};
    }""")


def _assert_preview_geometry(page: Page) -> None:
    geometry = page.locator("[data-chat-preview]").evaluate("""preview => {
        const rect = element => {
            const box = element.getBoundingClientRect();
            return {left: box.left, right: box.right, top: box.top, bottom: box.bottom};
        };
        const pane = document.querySelector('[data-browser-chat-pane]');
        const style = getComputedStyle(preview);
        return {
            preview: rect(preview), pane: rect(pane),
            viewport: {left: 0, right: innerWidth, top: 0, bottom: innerHeight},
            radii: [style.borderTopLeftRadius, style.borderTopRightRadius,
                style.borderBottomLeftRadius, style.borderBottomRightRadius],
            blur: style.backdropFilter,
            overflowX: preview.scrollWidth - preview.clientWidth,
            overflowY: preview.scrollHeight - preview.clientHeight,
        };
    }""")
    for owner in (geometry["pane"], geometry["viewport"]):
        for edge in ("left", "top"):
            assert geometry["preview"][edge] >= owner[edge] - 1, geometry
        for edge in ("right", "bottom"):
            assert geometry["preview"][edge] <= owner[edge] + 1, geometry
    assert geometry["radii"] == ["10px"] * 4, geometry
    assert "blur(" in geometry["blur"], geometry
    assert geometry["overflowX"] <= 1, geometry
    assert geometry["overflowY"] <= 1, geometry


@pytest.mark.parametrize(
    ("width", "height", "touch"),
    ((1_006, 791, False), (390, 844, True), (1_006, 500, True)),
)
def test_message_ruler_preview_navigation_and_scroll_containment(
    disposable_browser: Browser,
    seeded_chatgpt_table_browser_server_url: str,
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
        page.goto(
            f"{seeded_chatgpt_table_browser_server_url}/browser"
            "?view=text&source=chatgpt&session_view=1",
        )
        page.locator(".browser-session-index-table .browser-session-table-title").first.click()
        pane = page.locator("[data-browser-chat-pane]")
        scrollport = pane.locator("[data-chat-scrollport]")
        articles = scrollport.locator("article.browser-chat-message")
        expect(articles).to_have_count(2)
        ruler = pane.get_by_role("navigation", name="Message navigation", exact=True)
        markers = ruler.locator("button[data-chat-marker]")
        expect(markers).to_have_count(2)
        expect(ruler).to_be_visible()
        for index in range(2):
            article_id = articles.nth(index).get_attribute("id")
            assert article_id
            expect(markers.nth(index)).to_have_attribute("data-chat-target", article_id)
            expect(articles.nth(index)).to_have_attribute("data-chat-number", str(index + 1))
        assistant = articles.nth(1)
        expect(assistant.locator(".browser-chat-role-mark")).to_be_visible()
        expect(assistant.locator(".browser-chat-message-role")).to_have_count(0)
        author_id = assistant.get_attribute("aria-labelledby")
        author = page.locator("#" + author_id)
        expect(author).to_have_class("browser-chat-role-label")
        expect(author).to_have_text("ChatGPT")
        hidden_author = author.bounding_box()
        assert hidden_author is not None
        assert hidden_author["width"] <= 1 and hidden_author["height"] <= 1
        expect(author).to_have_css("clip-path", "inset(50%)")
        expect(articles.first.locator(".browser-chat-message-role")).to_have_text("You")
        _assert_message_badges(page)

        contained = pane.evaluate("""pane => {
            const inside = (child, owner) => {
                const box = child.getBoundingClientRect();
                const boundary = owner.getBoundingClientRect();
                return box.left >= boundary.left - 1 && box.right <= boundary.right + 1
                    && box.top >= boundary.top - 1 && box.bottom <= boundary.bottom + 1;
            };
            return {
                pane: inside(pane, pane.closest('.browser-text-summary-card')),
                scrollport: inside(pane.querySelector('[data-chat-scrollport]'), pane),
                ruler: inside(pane.querySelector('.browser-chat-ruler'), pane),
                overflow: document.documentElement.scrollWidth - innerWidth,
            };
        }""")
        assert contained["pane"] and contained["scrollport"] and contained["ruler"], contained
        assert contained["overflow"] <= 1, contained
        expect(markers.first).to_have_attribute("aria-current", "true")

        preview = pane.get_by_role("tooltip")
        markers.last.hover()
        expect(preview).to_be_visible()
        expect(preview.locator("[data-chat-preview-label]")).to_contain_text("ChatGPT")
        expect(preview.locator("[data-chat-preview-copy]")).to_contain_text("Metadata field")
        assert preview.locator("[data-chat-preview-copy]").evaluate("node => node.childElementCount") == 0
        _assert_preview_geometry(page)

        markers.first.focus()
        expect(preview).to_be_visible()
        expect(preview.locator("[data-chat-preview-label]")).to_contain_text("You")
        expect(preview.locator("[data-chat-preview-copy]")).to_have_text("Please review the metadata table.")
        _assert_preview_geometry(page)
        keyboard_scroll = _scroll_positions(page)
        markers.first.press("ArrowDown")
        expect(markers.last).to_be_focused()
        expect(preview.locator("[data-chat-preview-label]")).to_contain_text("ChatGPT")
        assert _scroll_positions(page) == keyboard_scroll
        markers.last.press("Home")
        expect(markers.first).to_be_focused()
        page.keyboard.press("Escape")
        expect(preview).to_be_hidden()
        expect(markers.first).to_be_focused()
        scrollport.evaluate("""async element => {
            element.scrollTop = 1;
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        }""")
        expect(preview).to_be_hidden()
        scrollport.evaluate("element => { element.scrollTop = 0; }")

        before = _scroll_positions(page)
        markers.last.click()
        expect(markers.last).to_have_attribute("aria-current", "true")
        page.wait_for_function("document.querySelector('[data-chat-scrollport]').scrollTop > 0")
        after = _scroll_positions(page)
        assert after["messageTop"] > before["messageTop"]
        assert after["ancestors"] == before["ancestors"]
        assert (after["windowX"], after["windowY"]) == (before["windowX"], before["windowY"])

        scrollport.evaluate("element => { element.scrollTop = 0; }")
        expect(markers.first).to_have_attribute("aria-current", "true")
        scrollport.evaluate("element => { element.scrollTop = element.scrollHeight; }")
        expect(markers.last).to_have_attribute("aria-current", "true")
        markers.first.click()
        expect(markers.first).to_have_attribute("aria-current", "true")
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize(("width", "height"), ((1_006, 791), (1_006, 500)))
def test_message_ruler_dense_magnification_preserves_hit_targets_and_layout(
    disposable_browser: Browser,
    seeded_ruler_browser_server_url: str,
    width: int,
    height: int,
) -> None:
    context = disposable_browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    try:
        page.goto(f"{seeded_ruler_browser_server_url}/browser?view=text&source=chatgpt&session_view=1")
        page.locator(".browser-session-index-table .browser-session-table-title").first.click()
        markers = page.locator("[data-chat-marker]")
        expect(markers).to_have_count(7)
        expect(markers.first).to_have_attribute("aria-current", "true")
        _assert_message_badges(page)
        expect(page.locator("article.browser-chat-message").nth(0).locator(".browser-session-message-time")).to_have_count(1)
        expect(page.locator("article.browser-chat-message").nth(1).locator(".browser-session-message-time")).to_have_count(0)
        linked_message = page.locator("article.browser-chat-message").nth(3)
        expect(linked_message.get_by_role("link", name="Reference detail", exact=True)).to_have_count(1)
        expect(linked_message.locator(".browser-chat-message-links")).to_have_count(0)

        resting = _ruler_geometry(page)
        assert [tick["width"] for tick in resting["ticks"]] == [22, 12, 18, 12, 18, 12, 18]
        for index in range(1, 7):
            assert resting["markers"][index]["y"] - resting["markers"][index - 1]["y"] == 8
        assert all(tick["scale"] == 1 for tick in resting["ticks"]), resting
        before_scroll = _scroll_positions(page)
        center = resting["markers"][3]
        page.mouse.move(center["x"] + center["width"] / 2, center["y"] + center["height"] / 2)
        _wait_for_lens(page, 3, True)
        magnified = _ruler_geometry(page)
        scales = [tick["scale"] for tick in magnified["ticks"]]
        assert 1.59 <= scales[3] <= 1.61, magnified
        assert scales[3] > scales[2] > scales[1] >= scales[0] >= 1, magnified
        assert scales[2] == pytest.approx(scales[4], abs=0.01), magnified
        assert scales[1] == pytest.approx(scales[5], abs=0.01), magnified
        assert magnified["ticks"][3]["shift"] < 0, magnified
        assert magnified["markers"] == resting["markers"], magnified
        assert magnified["messages"] == resting["messages"], magnified
        assert [tick["width"] for tick in magnified["ticks"]] == [tick["width"] for tick in resting["ticks"]]
        assert _scroll_positions(page) == before_scroll
        assert magnified["overflow"] <= 1
        _assert_preview_geometry(page)

        page.mouse.move(center["x"] + center["width"] / 2, center["y"] + center["height"] / 2 + 3)
        page.wait_for_function("""() => {
            const markers = document.querySelectorAll('[data-chat-marker]');
            const scale = index => new DOMMatrixReadOnly(getComputedStyle(markers[index], '::before').transform).a;
            return scale(4) > scale(2) + 0.03;
        }""")
        assert _ruler_geometry(page)["markers"] == resting["markers"]
        page.mouse.move(1, 1)
        _wait_for_lens(page, 3, False)
        assert all(tick["scale"] == pytest.approx(1, abs=0.001) for tick in _ruler_geometry(page)["ticks"])

        markers.nth(3).focus()
        markers.nth(3).press("ArrowDown")
        expect(markers.nth(4)).to_be_focused()
        _wait_for_lens(page, 4, True)
        assert _scroll_positions(page) == before_scroll
        assert _ruler_geometry(page)["messages"] == resting["messages"]
        page.emulate_media(reduced_motion="reduce")
        _wait_for_lens(page, 4, False)
        assert all(tick["scale"] == pytest.approx(1) for tick in _ruler_geometry(page)["ticks"])
        markers.nth(4).press("Enter")
        expect(markers.nth(4)).to_have_attribute("aria-current", "true")
        assert _scroll_positions(page)["messageTop"] > before_scroll["messageTop"]
        page.emulate_media(reduced_motion="no-preference")
        markers.nth(3).hover()
        _wait_for_lens(page, 3, True)
        markers.nth(3).click()
        page.mouse.move(1, 1)
        _wait_for_lens(page, 3, False)
        assert all(tick["scale"] == pytest.approx(1, abs=0.001) for tick in _ruler_geometry(page)["ticks"])
    finally:
        context.close()


def test_message_ruler_keeps_touch_spacing_without_hover_magnification(
    disposable_browser: Browser,
    seeded_ruler_browser_server_url: str,
) -> None:
    context = disposable_browser.new_context(
        viewport={"width": 390, "height": 844},
        has_touch=True,
        is_mobile=True,
    )
    page = context.new_page()
    try:
        page.goto(f"{seeded_ruler_browser_server_url}/browser?view=text&source=chatgpt&session_view=1")
        page.locator(".browser-session-index-table .browser-session-table-title").first.click()
        markers = page.locator("[data-chat-marker]")
        expect(markers).to_have_count(7)
        _assert_message_badges(page)
        expect(page.locator("article.browser-chat-message").first.locator(".browser-session-message-time")).to_have_count(1)
        resting = _ruler_geometry(page)
        for index in range(1, 7):
            assert resting["markers"][index]["y"] - resting["markers"][index - 1]["y"] == 24
        markers.nth(3).hover()
        expect(page.locator("[data-chat-preview]")).to_be_visible()
        hovered = _ruler_geometry(page)
        assert all(tick["scale"] == pytest.approx(1) for tick in hovered["ticks"]), hovered
        assert hovered["markers"] == resting["markers"]
        assert hovered["messages"] == resting["messages"]
        assert hovered["overflow"] <= 1
        markers.nth(3).tap()
        expect(markers.nth(3)).to_have_attribute("aria-current", "true")
        _assert_preview_geometry(page)
    finally:
        context.close()
