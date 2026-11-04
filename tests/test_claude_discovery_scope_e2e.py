"""Exercise Claude Chats discovery with independent and virtualized scroll owners."""

# Code version: v1.0.0-codex.0

from unittest.mock import patch

import pytest

from app.core.claude_history import (
    CLAUDE_CHATS_URL,
    ClaudeConversationDiscoveryIncompleteError,
    _scroll_claude_navigation,
    discover_claude_conversations,
)
import test_sidebar_e2e


disposable_browser = test_sidebar_e2e.disposable_browser


def _document(*, sidebar: bool, sessions: int = 40):
    aside = (
        '<aside id="sidebar" style="height:160px;width:180px;overflow-y:auto">'
        '<div style="height:1500px"><a href="/chat/sidebar-only">Sidebar session</a></div></aside>'
        if sidebar else ''
    )
    return f'''<!doctype html><html><head><title>Claude</title></head>
    <body style="margin:0;display:flex">{aside}
    <main style="width:600px"><h1>Chats</h1>
      <div id="history" style="height:200px;overflow-y:auto">
        <div style="height:{sessions * 500}px;position:relative">
          <nav id="mounted" style="position:absolute;left:0;right:0"></nav>
        </div>
      </div>
    </main>
    <script>
      const history = document.querySelector('#history');
      const mounted = document.querySelector('#mounted');
      const render = () => {{
        const first = Math.floor(history.scrollTop / 500);
        mounted.style.top = (first * 500) + 'px';
        mounted.replaceChildren();
        for (let index = first; index < Math.min({sessions}, first + 3); index += 1) {{
          const link = document.createElement('a');
          link.href = '/chat/main-' + index;
          link.textContent = 'Fixture session ' + index;
          link.style.cssText = 'display:block;height:500px';
          mounted.append(link);
        }}
      }};
      history.addEventListener('scroll', render);
      render();
    </script></body></html>'''


def _open(disposable_browser, document):
    context = disposable_browser.new_context()
    context.route('**/*', lambda route: (
        route.fulfill(status=200, content_type='text/html', body=document)
        if route.request.url == CLAUDE_CHATS_URL else route.abort()
    ))
    page = context.new_page()
    page.goto(CLAUDE_CHATS_URL)
    return context, page


def test_navigation_scrolls_main_chats_instead_of_earlier_sidebar(disposable_browser) -> None:
    context, page = _open(disposable_browser, _document(sidebar=True))
    try:
        _scroll_claude_navigation(page)
        positions = page.evaluate('''() => ({
            main: document.querySelector('#history').scrollTop,
            sidebar: document.querySelector('#sidebar').scrollTop,
        })''')
        assert positions['main'] > 0 and positions['sidebar'] == 0, positions
    finally:
        context.close()


def test_unexhausted_virtualized_list_cannot_complete_at_round_limit(disposable_browser) -> None:
    context, page = _open(disposable_browser, _document(sidebar=False))
    try:
        with patch('app.core.claude_history.wait_for_cache_scan', return_value=False), pytest.raises(
            ClaudeConversationDiscoveryIncompleteError, match='scroll limit',
        ) as error:
            discover_claude_conversations(page)
        geometry = page.evaluate('''() => ({
            top: document.querySelector('#history').scrollTop,
            height: document.querySelector('#history').scrollHeight,
            client: document.querySelector('#history').clientHeight,
        })''')
        assert 0 < len(error.value.conversations) < 40
        assert geometry['top'] == 12 * 480
        assert geometry['top'] + geometry['client'] < geometry['height']
    finally:
        context.close()


def test_discovery_traverses_main_from_top_and_ignores_sidebar_only_links(disposable_browser) -> None:
    context, page = _open(disposable_browser, _document(sidebar=True, sessions=8))
    try:
        page.evaluate('''() => {
            const list = document.querySelector('#history');
            list.scrollTop = 2000;
            list.dispatchEvent(new Event('scroll'));
        }''')
        with patch('app.core.claude_history.goto_with_retry'), patch(
            'app.core.claude_history.wait_for_cache_scan', return_value=False,
        ):
            conversations = discover_claude_conversations(page)
        assert {item.conversation_id for item in conversations} == {f'main-{index}' for index in range(8)}
        assert page.locator('#sidebar').evaluate('(element) => element.scrollTop') == 0
        assert page.locator('#history').evaluate(
            '(element) => element.scrollTop + element.clientHeight >= element.scrollHeight - 4',
        )
    finally:
        context.close()
