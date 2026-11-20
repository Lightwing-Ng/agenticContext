"""Known preview dimensions reserve space while lazy image requests are pending.

Code version: v1.0.0-codex.0
"""

from collections.abc import Iterator
import json
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

from PIL import Image
import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests import test_sidebar_e2e as fixtures


disposable_browser = fixtures.disposable_browser


@pytest.fixture(params=[True, False], ids=["known-dimensions", "unknown-dimensions"])
def deferred_preview_server(tmp_path: Path, request) -> Iterator[SimpleNamespace]:
    from app.web.app import create_app

    root = tmp_path / "store"
    project = root / "media/chatgpt/deferred-preview"
    project.mkdir(parents=True)
    Image.new("RGB", (320, 240), (40, 120, 200)).save(project / "preview.png")
    entry = {
        "file_id": "deferred-preview", "relative_path": "preview.png",
        "conversation_url": "https://chatgpt.com/c/deferred-preview",
        "conversation_title": "Synthetic deferred preview",
        "prompt_markdown": "Inspect the layout while this synthetic preview loads.",
        "created_at": "2026-08-12T05:00:00Z",
    }
    if request.param:
        entry.update(width=320, height=240)
    (project / ".chatgpt_catalog.json").write_text(
        json.dumps({"entries": {"deferred-preview": entry}}), encoding="utf-8"
    )
    app = create_app(
        root, computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    app.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    assert server.server_port != 8666
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{server.server_port}", known_dimensions=request.param,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _geometry(card):
    return card.evaluate("""card => {
        const image = card.querySelector('img[data-preview]');
        const rect = node => {
            const box=node.getBoundingClientRect();
            return {left:box.left,right:box.right,top:box.top,bottom:box.bottom,
                width:box.width,height:box.height};
        };
        return {
            image:rect(image), complete:image.complete,
            naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight,
            widthAttribute:image.getAttribute('width'),heightAttribute:image.getAttribute('height'),
            aspectRatio:getComputedStyle(image).aspectRatio,
            action:rect(card.querySelector('[data-prompt-add]')),
            heading:rect(card.querySelector('.browser-media-prompt-heading')),
        };
    }""")


def _assert_action_is_inside_heading(geometry):
    button, heading = geometry["action"], geometry["heading"]
    assert (button["width"], button["height"]) == (32, 32), geometry
    assert heading["left"] <= button["left"] <= button["right"] <= heading["right"] + 1, geometry
    assert heading["top"] <= button["top"] + 1, geometry
    assert button["bottom"] <= heading["bottom"] + 1, geometry


@pytest.mark.parametrize(("width", "touch"), [(1_006, False), (560, False), (390, True)])
def test_lazy_preview_preserves_known_layout_and_keeps_unknown_size_fallback(
    disposable_browser, deferred_preview_server, width, touch
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 844}, has_touch=touch, is_mobile=touch,
    )
    page = context.new_page()
    pending = []

    def hold_preview(route):
        pending.append(route)
        page.evaluate("window.__deferredPreviewRequestHeld = true")

    page.route("**/browser/media/**", hold_preview)
    try:
        page.goto(
            deferred_preview_server.url + "/browser?view=media&source=chatgpt",
            wait_until="load",
        )
        page.evaluate("document.fonts.ready")
        page.wait_for_function("window.__deferredPreviewRequestHeld === true")
        card = page.locator(".browser-media-card").first
        image = card.locator("img[data-preview]")
        expect(image).to_have_attribute("data-preview-loaded", "1")
        before = _geometry(card)
        assert pending and not before["complete"] and before["naturalHeight"] == 0, before
        _assert_action_is_inside_heading(before)
        if deferred_preview_server.known_dimensions:
            assert abs(before["image"]["height"] - before["image"]["width"] * 240 / 320) <= 1, before
            assert (before["widthAttribute"], before["heightAttribute"]) == ("320", "240"), before
        else:
            assert before["widthAttribute"] is None and before["heightAttribute"] is None, before
            assert before["aspectRatio"] == "auto", before
            assert before["image"]["height"] == 0, before

        while pending:
            pending.pop().continue_()
        image.evaluate("image => image.decode()")
        after = _geometry(card)
        assert after["complete"] and (after["naturalWidth"], after["naturalHeight"]) == (320, 240)
        _assert_action_is_inside_heading(after)
        assert abs(after["image"]["height"] - after["image"]["width"] * 240 / 320) <= 1, after
        if deferred_preview_server.known_dimensions:
            assert abs(after["image"]["height"] - before["image"]["height"]) <= 1, (before, after)
            assert abs(after["heading"]["top"] - before["heading"]["top"]) <= 1, (before, after)
    finally:
        while pending:
            pending.pop().abort()
        context.close()
