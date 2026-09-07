"""Image preview geometry and lifecycle regressions. Code version: v1.0.0-codex.1."""

from threading import Thread

from PIL import Image
import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests import test_sidebar_e2e as fixtures

disposable_browser = fixtures.disposable_browser


@pytest.fixture()
def media_server_url(tmp_path):
    from app.web.app import create_app

    root = tmp_path / "local-store"
    image_path = root / "media" / "chatgpt" / "demo" / "img_file_preview.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (600, 900), "white").save(image_path)
    app = create_app(
        root,
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    app.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width,theme", [(1022, "light"), (733, "light"), (390, "dark")])
def test_image_preview_retains_radius_on_open_resize_and_reopen(
    disposable_browser, media_server_url, width, theme,
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1124}, color_scheme=theme,
    )
    try:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{media_server_url}/browser?view=media&source=chatgpt")
        trigger = page.locator("[data-media-open]").first
        dialog = page.locator("#browser_detail_dialog")
        for viewport_width in (width, width - 20, width):
            page.set_viewport_size({"width": viewport_width, "height": 1124})
            trigger.click()
            expect(dialog).to_be_visible()
            expect(dialog).to_have_attribute("aria-label", "Image preview")
            frame = dialog.locator(".browser-image-player")
            expect(frame).to_have_css("border-top-left-radius", "10px")
            expect(frame).to_have_css("border-top-width", "0px")
            expect(frame).to_have_css("padding", "16px")
            image = frame.locator("img")
            expect(image).to_have_js_property("naturalWidth", 600)
            close = dialog.get_by_role("button", name="Close image preview")
            frame_box = frame.bounding_box()
            close_box = close.bounding_box()
            assert close_box["x"] >= 0
            assert close_box["x"] + close_box["width"] < frame_box["x"]
            assert abs(close_box["y"] - frame_box["y"]) < close_box["height"]
            assert frame_box["x"] + frame_box["width"] <= viewport_width
            close.click()
            expect(dialog).not_to_be_visible()
        assert not errors
    finally:
        context.close()
