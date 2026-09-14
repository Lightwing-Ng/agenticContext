"""Rendered provider-response extraction without authenticated browser access.

Code version: v1.0.0-codex.1
"""

from html import escape
import json

import pytest

from app.core.computer_use_agent import _provider_turn_snapshot
from tests import test_sidebar_e2e as fixtures

disposable_browser = fixtures.disposable_browser


@pytest.mark.integration
@pytest.mark.parametrize("platform", ["chatgpt", "grok"])
def test_jury_fenced_vote_preserves_json_escaping_and_excludes_provider_chrome(disposable_browser, platform):
    vote = {"verdict": "misleading", "conclusion": r'The shorthand A\ needs "source" context.'}
    raw = json.dumps(vote)
    context = disposable_browser.new_context()
    page = context.new_page()
    user_attributes = 'data-message-author-role="user"' if platform == "chatgpt" else 'data-role="user"'
    assistant_attributes = 'data-message-author-role="assistant"' if platform == "chatgpt" else 'data-testid="assistant-message"'
    try:
        page.set_content(
            f'<article {user_attributes}>Current receipt current-turn</article>'
            f'<article {assistant_attributes}>Worked for 18s'
            f'<div>json<button>Copy code</button></div><pre><code>{escape(raw)}</code></pre>'
            '<footer>15 sources</footer></article>'
            '<textarea id="prompt-textarea" aria-label="Ask Grok"></textarea>'
        )
        snapshot = _provider_turn_snapshot(
            page, platform, receipt_marker="current-turn", response_object_key="verdict",
        )
        assert snapshot["structuredResponse"] is True
        assert snapshot["markerEchoed"] is True
        assert snapshot["assistantAfterLatestUser"] is True
        assert json.loads(snapshot["text"]) == vote
        assert snapshot["text"] == raw
        assert "Copy code" not in snapshot["text"]
    finally:
        context.close()


@pytest.mark.integration
def test_multiple_fenced_votes_in_one_assistant_are_not_silently_selected(disposable_browser):
    context = disposable_browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            '<article data-role="user">current-turn</article>'
            '<article data-testid="assistant-message">'
            '<pre><code>{"verdict":"misleading"}</code></pre>'
            '<pre><code>{"verdict":"supported"}</code></pre></article>'
            '<textarea aria-label="Ask Grok"></textarea>'
        )
        snapshot = _provider_turn_snapshot(
            page, "grok", receipt_marker="current-turn", response_object_key="verdict",
        )
        assert snapshot["structuredResponse"] is False
    finally:
        context.close()
