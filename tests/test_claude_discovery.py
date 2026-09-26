"""Exercise asynchronous Claude Chats discovery without a live browser."""

# Code version: v1.0.0-codex.0

from unittest.mock import patch

import pytest

from app.core.claude_history import discover_claude_conversations


class _LoadingChatsPage:
    """Expose the loaded conversation list after the initial document shell."""

    url = "https://claude.ai/chats"

    def __init__(self, *, initially_visible: int = 0, loading_until_ms: int = 0) -> None:
        self.elapsed_ms = 0
        self.initially_visible = initially_visible
        self.loading_until_ms = loading_until_ms
        self.dom_reads = 0

    def title(self) -> str:
        return "Claude"

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.elapsed_ms += milliseconds

    def evaluate(self, script: str, *_args):
        if "loginRequired" in script:
            return {"loading": self.elapsed_ms < self.loading_until_ms}
        if 'a[href], [role="link"]' in script:
            self.dom_reads += 1
            count = 2 if self.elapsed_ms >= 1_000 else self.initially_visible
            return [
                {"href": f"https://claude.ai/chat/chat-{index}", "title": f"Chat {index}"}
                for index in range(count)
            ]
        if "scrollHeight" in script:
            return {"moved": False, "scrollTop": 0, "scrollHeight": 400}
        if "document.body" in script:
            return "Chats"
        return None


def test_discovery_waits_for_links_after_initial_unscrollable_shell() -> None:
    page = _LoadingChatsPage()
    with patch("app.core.claude_history.goto_with_retry"):
        conversations = discover_claude_conversations(page)

    assert [item.conversation_id for item in conversations] == ["chat-0", "chat-1"]
    assert page.dom_reads >= 2


def test_discovery_does_not_treat_first_sidebar_link_as_complete_chats_list() -> None:
    page = _LoadingChatsPage(initially_visible=1)
    with patch("app.core.claude_history.goto_with_retry"):
        conversations = discover_claude_conversations(page)

    assert [item.conversation_id for item in conversations] == ["chat-0", "chat-1"]


def test_discovery_waits_for_busy_main_before_accepting_sidebar_links() -> None:
    page = _LoadingChatsPage(initially_visible=1, loading_until_ms=2_000)
    with patch("app.core.claude_history.goto_with_retry"):
        conversations = discover_claude_conversations(page)

    assert len(conversations) == 2
    assert page.elapsed_ms >= 2_000


def test_discovery_deadline_includes_slow_browser_inspection() -> None:
    class _SlowPage(_LoadingChatsPage):
        def evaluate(self, script: str, *_args):
            if "loginRequired" in script:
                self.elapsed_ms += 2_000
            return super().evaluate(script, *_args)

    page = _SlowPage()
    with patch("app.core.claude_history.goto_with_retry"), patch(
        "app.core.claude_history.monotonic", side_effect=lambda: page.elapsed_ms / 1_000,
    ), patch("app.core.claude_history.CLAUDE_READY_TIMEOUT_SECONDS", 1):
        conversations = discover_claude_conversations(page)

    assert conversations == []
    assert page.elapsed_ms == 2_000
    assert page.dom_reads == 0


def test_discovery_stops_while_waiting_for_empty_shell() -> None:
    page = _LoadingChatsPage()
    with patch("app.core.claude_history.goto_with_retry"):
        conversations = discover_claude_conversations(
            page, should_stop=lambda: page.elapsed_ms >= 250,
        )

    assert conversations == []
    assert page.elapsed_ms == 250
    assert page.dom_reads == 1


def test_discovery_does_not_navigate_after_stop_or_human_verification() -> None:
    page = _LoadingChatsPage()
    with patch("app.core.claude_history.goto_with_retry") as navigate:
        assert discover_claude_conversations(page, should_stop=lambda: True) == []
        navigate.assert_not_called()
    with patch("app.core.claude_history.goto_with_retry") as navigate, patch.object(
        page, "title", return_value="Just a moment...",
    ):
        with pytest.raises(RuntimeError, match="human verification"):
            discover_claude_conversations(page)
        navigate.assert_not_called()
    assert page.dom_reads == 0


def test_discovery_fails_closed_when_chats_redirects_to_login() -> None:
    page = _LoadingChatsPage()

    def redirect_to_login(*_args, **_kwargs) -> None:
        page.url = "https://claude.ai/login"

    with patch("app.core.claude_history.goto_with_retry", side_effect=redirect_to_login):
        with pytest.raises(RuntimeError, match="not signed in"):
            discover_claude_conversations(page)
    assert page.dom_reads == 0
    assert page.elapsed_ms == 0
