"""Navigation readiness must describe the DOM currently committed in Safari."""

# Code version: v1.0.0-codex.0

from unittest.mock import patch

import pytest

from app.core.safari_automation import SafariContext, SafariPage


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
