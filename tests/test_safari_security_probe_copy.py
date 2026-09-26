"""Keep Safari challenge recovery copy consistent with owned-window cleanup.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from app.core import browser_sessions
from app.core.config import CrawlConfig


@pytest.mark.parametrize("challenge_after_navigation", (False, True))
def test_safari_chatgpt_challenge_returns_manual_recovery_after_owned_context_closes(
    challenge_after_navigation: bool,
) -> None:
    """Both challenge exits preserve fail-closed status without retaining a window."""
    challenge_url = "https://auth.openai.com/authorize"
    page = MagicMock()
    page.url = "https://chatgpt.com/c/existing" if challenge_after_navigation else challenge_url
    page.title.return_value = ""
    page.content.return_value = ""
    page.locator.return_value.inner_text.return_value = ""
    page.goto.side_effect = lambda *_args, **_kwargs: setattr(page, "url", challenge_url)
    context = SimpleNamespace(
        primary_page=page,
        request=SimpleNamespace(get=Mock()),
        close=Mock(),
    )

    @contextmanager
    def owned_context():
        try:
            yield context
        finally:
            context.close()

    descriptor = browser_sessions.BrowserDescriptor("safari", "Safari", "safari.svg", "safari")
    with patch("app.core.browser_sessions.SafariContext", return_value=owned_context()) as safari:
        result = browser_sessions._probe_chatgpt_session(descriptor, CrawlConfig())

    safari.assert_called_once_with(browser_sessions.CHATGPT_HOME_URL, lock_blocking=False)
    context.close.assert_called_once_with()
    context.request.get.assert_not_called()
    page.evaluate.assert_not_called()
    page.reload.assert_not_called()
    if challenge_after_navigation:
        page.goto.assert_called_once_with(
            browser_sessions.CHATGPT_HOME_URL, wait_until="domcontentloaded", timeout=60_000,
        )
        page.wait_for_load_state.assert_called_once_with("domcontentloaded", 60_000)
    else:
        page.goto.assert_not_called()
        page.wait_for_load_state.assert_not_called()
        page.title.assert_not_called()
        page.content.assert_not_called()

    expected_flags = browser_sessions.human_verification_probe_status("Safari", "ChatGPT")
    expected_flags.pop("message")
    assert {key: result[key] for key in expected_flags} == expected_flags
    assert "Open your existing ChatGPT tab in Safari" in result["message"]
    assert "complete any required verification there manually, then choose Recheck" in result["message"]
    assert "The temporary account-check window has been closed." in result["message"]
    assert "Do not retry, reload, or click the Cloudflare challenge from this app." in result["message"]
    assert "open browser window now" not in result["message"]
