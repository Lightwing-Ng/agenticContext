"""Disposable-browser coverage for saving media prompts and managing saved prompt rows.

Code version: v1.0.0-claude.0
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
import re
from threading import Thread

from PIL import Image
import pytest
from playwright.sync_api import Browser, Locator, Page, expect
from werkzeug.serving import make_server

from app.core.resource_persistence import CHATGPT_HISTORY_SCHEMA, write_parquet_rows_atomic
from tests import test_sidebar_e2e as fixtures

disposable_browser = fixtures.disposable_browser

SHARED_PROMPT = "Draw a lighthouse at dusk."
LONG_PROMPT = " ".join(["Paint a quiet harbor with layered clouds and soft rim light."] * 8)
FIRST_SESSION_PATH = (
    "/browser?view=media&source=chatgpt&session_view=1&session=chatgpt:session:demo-project:first"
)
# Desktop pointer, narrow pointer window, and touch phone.
VIEWPORTS = ((1_006, 791, False), (560, 800, False), (390, 844, True))


def _history_row(conversation_id: str, content_text: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "platform": "chatgpt",
        "conversation_id": conversation_id,
        "conversation_url": f"https://chatgpt.com/c/{conversation_id}",
        "conversation_title": f"Session {conversation_id}",
        "message_key": f"{conversation_id}:user:0",
        "turn_index": 0,
        "message_index": 0,
        "role": "user",
        "author_label": "You",
        "content_text": content_text,
        "content_html": "",
        "content_sha256": f"{conversation_id}-hash",
        "source_links": [],
        "model_label": "",
        "first_seen_at": "2026-08-12T04:59:00Z",
        "last_seen_at": "2026-08-12T05:00:00Z",
    }


@pytest.fixture()
def saved_prompts_server_url(tmp_path: Path) -> Iterator[str]:
    """Serve two saved prompts, one of them annotated, beside unsaved media prompts."""
    from app.core.prompt_store import PromptStore
    from app.web.app import create_app

    root = tmp_path / "local-store"
    write_parquet_rows_atomic(
        root / "llm" / "chatgpt" / "history.parquet",
        [
            _history_row("first", SHARED_PROMPT),
            _history_row("second", SHARED_PROMPT),
            _history_row("third", "Describe a tide table."),
        ],
        CHATGPT_HISTORY_SCHEMA,
    )
    project_dir = root / "media" / "chatgpt" / "demo-project"
    project_dir.mkdir(parents=True)
    entries: dict[str, dict[str, object]] = {}
    # Both images of the first session share its prompt; the second session is not in the text cache.
    for image_number, (filename, conversation_id, prompt) in enumerate(
        (
            ("first-a.png", "first", SHARED_PROMPT),
            ("first-b.png", "first", SHARED_PROMPT),
            ("loose.png", "uncached", LONG_PROMPT),
        ),
        start=1,
    ):
        Image.new("RGB", (320, 240), (40 * image_number, 120, 200)).save(project_dir / filename)
        entries[f"file-{filename}"] = {
            "file_id": f"file-{filename}",
            "relative_path": filename,
            "conversation_url": f"https://chatgpt.com/c/{conversation_id}",
            "conversation_title": f"Session {conversation_id}",
            "prompt_markdown": prompt,
            "created_at": f"2026-08-{image_number:02d}T00:00:00Z",
            "width": 320,
            "height": 240,
        }
    (project_dir / ".chatgpt_catalog.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")

    store = PromptStore(root)
    store.add_pointer(source="chatgpt", conversation_id="second", message_key="second:user:0")
    annotated, _ = store.add_pointer(source="chatgpt", conversation_id="third", message_key="third:user:0")
    store.add_remark(annotated.stable_id, "keep")

    app = create_app(
        root,
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    app.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    assert server.server_port != 8666
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open(browser: Browser, url: str, width: int, height: int, touch: bool) -> tuple[Page, list[str]]:
    context = browser.new_context(
        viewport={"width": width, "height": height},
        has_touch=touch,
        is_mobile=touch,
    )
    page = context.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda error: problems.append(str(error)))

    def dismiss(dialog) -> None:
        problems.append(f"dialog: {dialog.message}")
        dialog.dismiss()

    page.on("dialog", dismiss)
    page.goto(url)
    return page, problems


def _press(control: Locator, touch: bool) -> None:
    if touch:
        control.tap()
    else:
        control.click()


def _has_class(class_name: str) -> re.Pattern[str]:
    return re.compile(rf"(^|\s){re.escape(class_name)}(\s|$)")


def _box(control: Locator) -> dict[str, float]:
    return control.evaluate(
        """node => {
            const box = node.getBoundingClientRect();
            return {left: box.left, right: box.right, top: box.top, bottom: box.bottom,
                    width: box.width, height: box.height, centerY: box.top + box.height / 2};
        }"""
    )


@pytest.mark.parametrize(("width", "height", "touch"), VIEWPORTS)
def test_media_prompt_heading_reveals_the_standard_save_action(
    disposable_browser: Browser, saved_prompts_server_url: str, width: int, height: int, touch: bool,
) -> None:
    page, problems = _open(
        disposable_browser, f"{saved_prompts_server_url}{FIRST_SESSION_PATH}", width, height, touch,
    )
    try:
        save_buttons = page.locator("[data-prompt-add]")
        expect(save_buttons).to_have_count(2)
        first_save = save_buttons.first
        heading = page.locator(".browser-media-prompt-heading").first
        expect(first_save).to_have_attribute("aria-label", "Add as prompt")
        if touch:
            # Touch has no hover, so the action stays visible.
            expect(first_save).to_have_css("opacity", "1")
        else:
            expect(first_save).to_have_css("opacity", "0")
            heading.hover()
            expect(first_save).to_have_css("opacity", "1")

        # The action is the standard 32px Circular icon button inside the heading row.
        assert "circular-icon-button" in (first_save.get_attribute("class") or "")
        button_box, heading_box = _box(first_save), _box(heading)
        assert (button_box["width"], button_box["height"]) == (32, 32), button_box
        assert heading_box["left"] <= button_box["left"] and button_box["right"] <= heading_box["right"] + 1
        assert heading_box["top"] <= button_box["top"] + 1 and button_box["bottom"] <= heading_box["bottom"] + 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

        _press(first_save, touch)
        # Both images carry the same prompt of one conversation, so one save marks them both.
        for button in (save_buttons.nth(0), save_buttons.nth(1)):
            expect(button).to_have_class(_has_class("is-added"))
            expect(button).to_have_attribute("aria-label", "Added as prompt")
            expect(button).to_be_disabled()
        page.mouse.move(2, 2)
        expect(first_save).to_have_css("opacity", "1")

        page.reload()
        expect(page.locator("[data-prompt-add].is-added")).to_have_count(2)

        # The prompt is the first session's cached user turn, saved once and flagged as a duplicate.
        page.goto(f"{saved_prompts_server_url}/browser?view=prompts")
        rows = page.locator("tr[data-prompt-id]")
        expect(rows).to_have_count(3)
        expect(page.locator("[data-prompt-duplicate]:not([hidden])")).to_have_count(2)
        page.goto(
            f"{saved_prompts_server_url}/browser?view=text&source=chatgpt&session_view=1&session=chatgpt:first"
        )
        expect(page.locator("[data-prompt-add]")).to_have_attribute("aria-label", "Added as prompt")
        assert not problems
    finally:
        page.context.close()


@pytest.mark.parametrize(("width", "height", "touch"), VIEWPORTS)
def test_saved_prompt_rows_flag_duplicates_and_toggle_saving(
    disposable_browser: Browser, saved_prompts_server_url: str, width: int, height: int, touch: bool,
) -> None:
    page, problems = _open(
        disposable_browser, f"{saved_prompts_server_url}/browser?view=prompts", width, height, touch,
    )
    try:
        response = page.request.post(
            f"{saved_prompts_server_url}/api/browser/prompts",
            data={"source": "chatgpt", "conversation_id": "first", "message_key": "first:user:0"},
        )
        assert response.ok
        page.reload()
        rows = page.locator("tr[data-prompt-id]")
        expect(rows).to_have_count(3)
        total = page.locator("[data-prompt-total]")
        expect(total).to_have_attribute("aria-label", "3")

        # Address rows by prompt, because their duplicate marks and remarks change below.
        duplicates = page.locator("tr[data-prompt-id]:has([data-prompt-duplicate]:not([hidden]))")
        expect(duplicates).to_have_count(2)
        duplicate_row, twin_row = (
            page.locator(f'tr[data-prompt-id="{duplicates.nth(index).get_attribute("data-prompt-id")}"]')
            for index in range(2)
        )
        annotated_id = page.locator("tr[data-prompt-id]:has([data-prompt-tag])").get_attribute("data-prompt-id")
        annotated_row = page.locator(f'tr[data-prompt-id="{annotated_id}"]')
        mark = duplicate_row.locator("[data-prompt-duplicate]")
        expect(mark).to_have_text("×2")
        expect(mark).to_have_attribute("aria-label", "Duplicate prompt: saved 2 times")
        expect(annotated_row.locator("[data-prompt-duplicate]")).to_be_hidden()
        assert mark.locator(".browser-prompt-duplicate-icon").evaluate(
            "icon => getComputedStyle(icon).maskImage"
        ).endswith('rectangle.on.rectangle.svg")')

        toggle = duplicate_row.locator("[data-prompt-unsave]")
        expect(toggle).to_have_attribute("aria-label", "Remove from saved prompts")
        assert "circular-icon-button" in (toggle.get_attribute("class") or "")
        assert toggle.locator(".browser-prompt-unsave-icon").evaluate(
            "icon => getComputedStyle(icon).maskImage"
        ).endswith('bookmark.slash.fill.svg")')
        if touch:
            expect(toggle).to_have_css("opacity", "1")
        else:
            expect(toggle).to_have_css("opacity", "0")
            duplicate_row.hover()
            expect(toggle).to_have_css("opacity", "1")

        # The toggle and the mark stay inside the Remarks cell at every width.
        cell_box = _box(duplicate_row.locator("td.browser-prompt-table-remarks"))
        input_box = _box(duplicate_row.locator("[data-prompt-remark-input]"))
        toggle_box = _box(toggle)
        mark_box = _box(mark)
        assert (toggle_box["width"], toggle_box["height"]) == (32, 32), toggle_box
        assert input_box["height"] == 30, input_box
        for box in (input_box, toggle_box, mark_box):
            assert cell_box["left"] - 1 <= box["left"] and box["right"] <= cell_box["right"] + 1, (box, cell_box)
        assert abs(toggle_box["right"] - input_box["right"]) <= 1 or toggle_box["left"] >= input_box["right"]
        if touch:
            # A phone keeps the full-width input and puts the always-visible toggle under it.
            assert toggle_box["top"] >= input_box["bottom"], (toggle_box, input_box)
        else:
            assert abs(toggle_box["centerY"] - input_box["centerY"]) <= 1, (toggle_box, input_box)
            assert input_box["width"] >= 56, input_box
        assert mark_box["top"] >= max(input_box["bottom"], toggle_box["bottom"]), (mark_box, toggle_box)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

        _press(toggle, touch)
        expect(duplicate_row).to_have_class(_has_class("is-unsaved"))
        expect(toggle).to_have_attribute("aria-label", "Add as prompt")
        expect(toggle).to_be_enabled()
        expect(duplicate_row.locator("[data-prompt-remark-input]")).to_be_disabled()
        # The remaining copy is no longer a duplicate, and the total follows.
        expect(page.locator("[data-prompt-duplicate]:not([hidden])")).to_have_count(0)
        expect(total).to_have_attribute("aria-label", "2")
        page.mouse.move(2, 2)
        expect(toggle).to_have_css("opacity", "1")
        assert toggle.locator(".browser-prompt-unsave-icon").evaluate(
            "icon => getComputedStyle(icon).maskImage"
        ).endswith('text.bubble.fill.svg")')

        _press(toggle, touch)
        expect(duplicate_row).not_to_have_class(_has_class("is-unsaved"))
        expect(toggle).to_have_attribute("aria-label", "Remove from saved prompts")
        expect(duplicate_row.locator("[data-prompt-remark-input]")).to_be_enabled()
        expect(page.locator("[data-prompt-duplicate]:not([hidden])")).to_have_count(2)
        expect(twin_row.locator("[data-prompt-duplicate]")).to_have_text("×2")
        expect(total).to_have_attribute("aria-label", "3")

        # Removing an annotated prompt keeps its remarks for the undo, then drops the row on reload.
        annotated_toggle = annotated_row.locator("[data-prompt-unsave]")
        if not touch:
            annotated_row.hover()
        _press(annotated_toggle, touch)
        expect(annotated_row).to_have_class(_has_class("is-unsaved"))
        expect(annotated_row.locator("[data-prompt-remark-remove]")).to_be_disabled()
        expect(page.locator("[data-prompt-remark-options] option")).to_have_count(0)
        _press(annotated_toggle, touch)
        expect(annotated_row).not_to_have_class(_has_class("is-unsaved"))
        expect(annotated_row.locator("[data-prompt-tag]")).to_have_text("keep×")
        expect(annotated_row.locator("[data-prompt-remark-remove]")).to_be_enabled()
        expect(page.locator("[data-prompt-remark-options] option")).to_have_count(1)

        _press(annotated_toggle, touch)
        expect(annotated_row).to_have_class(_has_class("is-unsaved"))
        page.reload()
        expect(rows).to_have_count(2)
        expect(page.locator("[data-prompt-tag]")).to_have_count(0)
        expect(total).to_have_attribute("aria-label", "2")
        assert not problems
    finally:
        page.context.close()
