"""Known native system templates reuse the centered notice and clock rail.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import json
from threading import Thread
from urllib.parse import urlencode

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from tests import test_sidebar_e2e
from tests.test_wechat_system_notice_markup import (
    ADMIN_DISPLAY,
    ADMIN_RAW,
    REMARK_DISPLAY,
    REMARK_RAW,
    TEMPLATE_DISPLAY,
    TEMPLATE_RAW,
    _system_template_fixture,
)


disposable_browser = test_sidebar_e2e.disposable_browser


@pytest.fixture(
    scope="module",
    params=[
        (TEMPLATE_RAW, TEMPLATE_DISPLAY),
        (REMARK_RAW, REMARK_DISPLAY),
        (ADMIN_RAW, ADMIN_DISPLAY),
    ],
    ids=["invitation", "empty-rename", "administrator"],
)
def system_template_server(tmp_path_factory, request):
    source_text, display_text = request.param
    fixture = _system_template_fixture(
        tmp_path_factory.mktemp("wechat-system-template"), notice_text=source_text
    )
    fixture.source_text = source_text
    fixture.display_text = display_text
    server = make_server("127.0.0.1", 0, fixture.app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fixture.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_known_native_template_is_a_centered_plain_notice_with_original_clock(
    disposable_browser, system_template_server, tmp_path, width, color_scheme
):
    fixture = system_template_server
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 1000},
        color_scheme=color_scheme,
        reduced_motion="reduce",
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(
            fixture.url
            + "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": "account:wxid_account_a",
                    "session": fixture.session,
                }
            ),
            wait_until="domcontentloaded",
        )
        toggle = page.locator("#sidebar_toggle")
        if toggle.get_attribute("aria-expanded") == "true":
            toggle.click()
        page.evaluate("() => document.fonts.ready")
        identifier = fixture.identifiers["template-notice"]
        row = page.locator("#wechat_message_" + identifier)
        row.scroll_into_view_if_needed()
        expect(row).to_have_class("wechat-system-message-row")
        expect(row).to_have_attribute("role", "note")
        expect(row.locator(".wechat-system-notice")).to_have_text(fixture.display_text)
        expect(row.locator("time")).to_have_text("7 Aug 2021 18:59:03")
        expect(row.locator("time")).to_have_attribute(
            "datetime", "2021-08-07T18:59:03+08:00"
        )
        expect(
            row.locator("a, script, iframe, img, video, audio, sysmsg, link_list")
        ).to_have_count(0)
        expect(row.locator(".wechat-message, .browser-chat-message")).to_have_count(0)
        ordinary = page.locator(
            "#wechat_message_" + fixture.identifiers["ordinary-xml-example"]
        )
        expect(ordinary.locator(".wechat-message-text")).to_have_text(fixture.source_text)
        geometry = row.evaluate("""row => {
            const notice = row.querySelector('.wechat-system-notice');
            const clock = row.querySelector('time');
            const box = row.getBoundingClientRect();
            const noticeBox = notice.getBoundingClientRect();
            const clockBox = clock.getBoundingClientRect();
            const style = getComputedStyle(notice);
            const rowStyle = getComputedStyle(row);
            const port = document.querySelector('[data-chat-scrollport]');
            return {
                centered: Math.abs((noticeBox.left + noticeBox.right) / 2 - (box.left + box.right) / 2),
                textAlign: style.textAlign,
                background: rowStyle.backgroundColor,
                border: rowStyle.borderTopWidth,
                shadow: rowStyle.boxShadow,
                clockAbove: clockBox.bottom <= noticeBox.top + 1,
                clockRight: Math.abs(clockBox.right - (box.right - parseFloat(getComputedStyle(clock).marginInlineEnd))),
                clockJustify: getComputedStyle(clock).justifySelf,
                documentOverflow: document.documentElement.scrollWidth - innerWidth,
                listOverflow: port.scrollWidth - port.clientWidth,
                bubbleAncestor: Boolean(row.closest('.browser-chat-message, .wechat-message-row')),
            };
        }""")
        assert geometry["centered"] <= 1
        assert geometry["textAlign"] == "center"
        assert geometry["background"] == "rgba(0, 0, 0, 0)"
        assert geometry["border"] == "0px"
        assert geometry["shadow"] == "none"
        assert geometry["clockAbove"]
        assert geometry["clockRight"] <= 1
        assert geometry["clockJustify"] == "end"
        assert geometry["documentOverflow"] == geometry["listOverflow"] == 0
        assert not geometry["bubbleAncestor"]
        (tmp_path / "system-template-geometry.json").write_text(
            json.dumps(geometry, indent=2) + "\n"
        )
        page.screenshot(path=tmp_path / f"system-template-{width}-{color_scheme}.png")
        assert not errors
        assert fixture.database.read_bytes() == fixture.preserved
    finally:
        context.close()
