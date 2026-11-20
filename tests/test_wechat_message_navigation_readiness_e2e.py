"""Conversation geometry requires deferred messaging initialization.

Code version: v1.0.1-codex.0
"""

from urllib.parse import urljoin

import pytest

from tests import test_wechat_browser_e2e as conversation_browser


disposable_browser = conversation_browser.disposable_browser
wechat_server = conversation_browser.wechat_server


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_fonts_ready_precedes_deferred_message_geometry(
    disposable_browser, wechat_server, color_scheme
):
    """A held deferred asset exposes the font-ready navigation race without sleeps."""
    context = disposable_browser.new_context(
        viewport={"width": 390, "height": 778},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    held = []
    try:
        page = context.new_page()
        page.goto(
            wechat_server
            + "/browser?view=text&source=wechat&account=account:wxid_account_a&chat_kind=group",
            wait_until="networkidle",
        )
        conversation_url = page.get_by_role(
            "link", name="Same title", exact=True
        ).get_attribute("href")
        page.route("**/browser-messaging.js?*", lambda route: held.append(route))
        page.goto(urljoin(wechat_server, conversation_url), wait_until="commit")
        page.wait_for_function("document.readyState === 'interactive'")
        page.evaluate("document.fonts.ready")

        before = page.locator("[data-chat-scrollport]").evaluate("""list => ({
            ready: document.readyState,
            fonts: document.fonts.status,
            height: list.scrollHeight,
            rows: list.querySelectorAll(':scope > .wechat-message-row').length,
            clocks: list.querySelectorAll(':scope > .wechat-message-row > .wechat-message-time').length,
            slots: list.querySelectorAll('.wechat-time-slot').length,
        })""")
        assert len(held) == 1
        assert before["ready"] == "interactive"
        assert before["fonts"] == "loaded"
        assert before["rows"] == 100
        assert before["clocks"] == before["slots"] == 0

        # DOMContentLoaded follows deferred scripts, unlike document.fonts.ready.
        with page.expect_event("domcontentloaded"):
            held.pop().continue_()
        page.wait_for_load_state("domcontentloaded")
        if page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true":
            page.locator("#sidebar_toggle").click()

        after = page.locator("[data-chat-scrollport]").evaluate("""list => ({
            height: list.scrollHeight,
            clocks: list.querySelectorAll(':scope > .wechat-message-row > .wechat-message-time').length,
            slots: list.querySelectorAll('.wechat-time-slot').length,
        })""")
        assert after["clocks"] == after["slots"] == before["rows"]
        assert after["height"] < before["height"]

        frames = page.locator(".wechat-message--sent").nth(20).evaluate("""async card => {
            const list = card.closest('[data-chat-scrollport]');
            list.scrollTop += card.getBoundingClientRect().top
                - (list.getBoundingClientRect().bottom - 32);
            const snapshot = () => ({
                height: list.scrollHeight,
                top: card.getBoundingClientRect().top,
                body: card.querySelector('.wechat-message-text').getBoundingClientRect().top,
                bottom: list.getBoundingClientRect().bottom,
            });
            const frames = [snapshot()];
            for (let index = 0; index < 4; index += 1) {
                await new Promise(requestAnimationFrame);
                frames.push(snapshot());
            }
            return frames;
        }""")
        assert all(frame == frames[0] for frame in frames), frames
        assert all(frame["body"] >= frame["bottom"] for frame in frames), frames
    finally:
        for route in held:
            route.abort()
        context.close()
