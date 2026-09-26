"""Verify Claude discovery against a document that hydrates after navigation."""

# Code version: v1.0.0-codex.0

import pytest
from playwright.sync_api import Browser

from app.core.claude_history import CLAUDE_CHATS_URL, discover_claude_conversations
import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser


@pytest.mark.parametrize(("initial_link_count", "explicit_loading"), [(0, True), (1, True), (1, False)])
def test_discovery_waits_for_rendered_chats(
    disposable_browser: Browser, initial_link_count: int, explicit_loading: bool,
) -> None:
    context = disposable_browser.new_context()
    page = context.new_page()
    initial_links = "".join(
        f'<a href="/chat/session-{index}">Chat {index}</a>'
        for index in range(initial_link_count)
    )
    document = """<!doctype html>
    <html><head><title>Claude</title></head><body>
    <aside>INITIAL_LINKS</aside>
    <main INITIAL_BUSY><h1>Chats and tasks</h1><nav></nav></main>
    <script>
    setTimeout(() => {
        const list = document.querySelector('main nav');
        list.replaceChildren();
        for (let index = 0; index < 7; index += 1) {
            const link = document.createElement('a');
            link.href = '/chat/session-' + index;
            link.textContent = 'Chat ' + index;
            list.append(link);
        }
        document.querySelector('main').setAttribute('aria-busy', 'false');
    }, 2200);
    </script></body></html>""".replace("INITIAL_LINKS", initial_links).replace(
        "INITIAL_BUSY", 'aria-busy="true"' if explicit_loading else "",
    )
    context.route("**/*", lambda route: (
        route.fulfill(status=200, content_type="text/html", body=document)
        if route.request.url == CLAUDE_CHATS_URL
        else route.abort()
    ))
    try:
        conversations = discover_claude_conversations(page)
        assert {item.conversation_id for item in conversations} == {
            f"session-{index}" for index in range(7)
        }
        assert page.locator("main").get_attribute("aria-busy") == "false"
    finally:
        context.close()
