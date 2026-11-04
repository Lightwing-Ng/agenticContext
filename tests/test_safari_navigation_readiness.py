"""Navigation readiness must describe the DOM currently committed in Safari."""

# Code version: v1.1.0-codex.0

from unittest.mock import MagicMock, patch

import pytest

from app.core.safari_automation import SafariBusyError, SafariContext, SafariPage, safari_navigation_matches


def test_context_lock_contention_exposes_transient_busy_error_without_retaining_handle() -> None:
    context = SafariContext("https://x.com/home", lock_blocking=False)
    handle = MagicMock()
    with patch("app.core.safari_automation.retry_pending_safari_context_cleanup", return_value=(0, 0)), patch(
        "app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH",
    ) as lock_path, patch("app.core.safari_automation.lock_file", side_effect=BlockingIOError), pytest.raises(
        SafariBusyError, match="another active browser task",
    ):
        lock_path.open.return_value = handle
        context._acquire_context_lock()

    handle.close.assert_called_once_with()
    assert context._context_lock_handle is None


@pytest.mark.parametrize("target,current", (
    ("https://x.com/signed_in/likes", "https://x.com/i/history/likes"),
    ("https://x.com/SIGNED_IN/likes/", "https://x.com/i/history/likes/"),
    ("https://x.com/signed_in/likes?source=profile", "https://x.com/i/history/likes?source=profile"),
))
def test_navigation_accepts_x_account_likes_history_redirect(target: str, current: str) -> None:
    assert safari_navigation_matches(target, current)


@pytest.mark.parametrize("target,current", (
    ("http://x.com/signed_in/likes", "http://x.com/i/history/likes"),
    ("https://x.com/signed_in/likes", "http://x.com/i/history/likes"),
    ("https://foreign.example/signed_in/likes", "https://foreign.example/i/history/likes"),
    ("https://x.com/signed_in/likes", "https://foreign.example/i/history/likes"),
    ("https://www.x.com/signed_in/likes", "https://www.x.com/i/history/likes"),
    ("https://x.com/signed_in/likes", "https://www.x.com/i/history/likes"),
    ("https://x.com:8443/signed_in/likes", "https://x.com:8443/i/history/likes"),
    ("https://x.com/signed_in/likes", "https://x.com:443/i/history/likes"),
    ("https://someone@x.com/signed_in/likes", "https://someone@x.com/i/history/likes"),
    ("https://x.com/signed_in/likes", "https://someone@x.com/i/history/likes"),
    ("https://x.com/i/likes", "https://x.com/i/history/likes"),
    ("https://x.com/signed_in/with_replies", "https://x.com/i/history/likes"),
    ("https://x.com/signed_in/likes", "https://x.com/i/timeline/likes"),
    ("https://x.com/signed_in/likes", "https://x.com/i/history/bookmarks"),
    ("https://x.com/signed_in/likes", "https://x.com/i/history/likes/extra"),
    ("https://x.com/signed_in/likes/extra", "https://x.com/i/history/likes"),
    ("https://x.com/handle_is_too_long/likes", "https://x.com/i/history/likes"),
))
def test_navigation_rejects_unrelated_or_untrusted_x_likes_redirects(target: str, current: str) -> None:
    assert not safari_navigation_matches(target, current)


def test_goto_accepts_committed_x_likes_redirect_without_reopening_legacy_route() -> None:
    target = "https://x.com/signed_in/likes"
    current = "https://x.com/i/history/likes"
    page = SafariPage(SafariContext(target), window_id=123)
    with patch.object(page, "_run_in_window", return_value=f"{current}\n{current}\ncomplete") as run, patch.object(
        page, "_keep_in_background",
    ) as background:
        page.goto(target, timeout=1_000)

    assert run.call_count == 1
    assert "set URL of targetTab" not in run.call_args.args[0]
    background.assert_called_once_with()


@pytest.mark.parametrize("native_url,document_url,expected_state", (
    ("https://grok.com/", "about:blank", ""),
    ("https://grok.com/", "https://chatgpt.com/", ""),
    ("https://grok.com/", "https://grok.com/files", ""),
    ("https://grok.com/?page=2", "https://grok.com/?page=1", ""),
    ("https://grok.com/#new", "https://grok.com/#old", ""),
    ("https://grok.com", "https://grok.com/", "complete"),
    ("https://grok.com/files", "https://grok.com/files", "complete"),
    ("about:blank", "about:blank", "complete"),
))
def test_navigation_rejects_readiness_from_a_different_document(
    native_url: str, document_url: str, expected_state: str,
) -> None:
    page = SafariPage(SafariContext("https://grok.com/"), window_id=123)
    with patch.object(page, "_run_in_window", return_value=f"{native_url}\n{document_url}\ncomplete"):
        state = page._read_navigation_state()
    assert state == {"href": document_url, "nativeHref": native_url, "readyState": expected_state}


def test_goto_waits_for_dom_commit_when_native_url_is_already_the_target() -> None:
    page = SafariPage(SafariContext("https://grok.com/"), window_id=123)
    with patch.object(page, "_run_in_window", side_effect=[
        "https://grok.com/\nabout:blank\ncomplete",
        "",
        "https://grok.com/\nabout:blank\ncomplete",
        "https://grok.com/\nhttps://grok.com/\ninteractive",
    ]) as run, patch.object(page, "_keep_in_background") as background, patch(
        "app.core.safari_automation.time.sleep"
    ):
        page.goto("https://grok.com/", timeout=1_000)

    assert run.call_count == 4
    assert run.call_args_list[1].args[0] == 'set URL of targetTab to "https://grok.com/"'
    background.assert_called_once_with()


@pytest.mark.parametrize("raw", ("https://grok.com/", "https://grok.com/\ncomplete", "\n\ncomplete"))
def test_navigation_does_not_infer_missing_document_location(raw: str) -> None:
    page = SafariPage(SafariContext("https://grok.com/"), window_id=123)
    with patch.object(page, "_run_in_window", return_value=raw):
        assert page._read_navigation_state()["readyState"] == ""
