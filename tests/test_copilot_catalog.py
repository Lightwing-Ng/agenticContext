"""Copilot's observed opaque-cursor catalog traversal and completeness contract."""

# Code version: v1.0.1-codex.0

from unittest.mock import Mock, call, patch

import pytest

from app.core import copilot_history as history


def _chats(start: int, stop: int) -> list[dict]:
    return [
        {"conversationId": f"session-{index}", "chatName": f"Fixture session {index}"}
        for index in range(start, stop)
    ]


def _page(start: int, stop: int, cursor: str) -> dict:
    return {"chats": _chats(start, stop), "syncState": cursor}


def _identities(conversations) -> list[str]:
    return [row["conversation_id"] for row in conversations]


def test_cursor_traversal_reaches_hundreds_and_requires_explicit_empty_terminal_cursor():
    progress = []
    fetch = Mock(side_effect=[
        _page(17, 217, "opaque:second"),
        _page(200, 237, "opaque:third"),
        _page(237, 317, ""),
    ])
    with patch.object(history, "inspect_copilot_page", side_effect=AssertionError("Catalog traversal must not inspect a browser.")), patch.object(
        history, "discover_copilot_conversations", side_effect=AssertionError("Catalog traversal must not read sidebar links.")
    ):
        result = history.traverse_copilot_catalog(
            _page(0, 17, "opaque:first"),
            fetch,
            lambda: False,
            on_progress=progress.append,
        )
    assert isinstance(result, history.CopilotDiscovery)
    assert result.discovery_complete is True
    assert result.expected_sessions is None
    assert result.completion_basis.strip()
    assert _identities(result.conversations) == [f"session-{index}" for index in range(317)]
    assert all(row["conversation_url"] == f"https://copilot.com/chat/conversation/{row['conversation_id']}" for row in result.conversations)
    assert fetch.call_args_list == [call("opaque:first"), call("opaque:second"), call("opaque:third")]
    assert all(isinstance(count, int) for count in progress)
    assert progress == sorted(progress)
    assert {17, 217, 237, 317}.issubset(progress)
    assert progress[-1] == 317


@pytest.mark.parametrize("count", [0, 17])
def test_initial_explicit_terminal_page_needs_no_fetch_and_does_not_invent_account_total(count):
    fetch = Mock(side_effect=AssertionError("A terminal catalog must not fetch another page."))
    result = history.traverse_copilot_catalog(_page(0, count, ""), fetch, lambda: False)
    assert result.discovery_complete is True
    assert len(result.conversations) == count
    assert result.expected_sessions is None
    assert result.completion_basis
    fetch.assert_not_called()


def test_nonempty_cursor_is_passed_unchanged_including_opaque_punctuation():
    cursor = "opaque+/token==:preserve%2Fbytes"
    fetch = Mock(return_value=_page(17, 18, ""))
    result = history.traverse_copilot_catalog(_page(0, 17, cursor), fetch, lambda: False)
    assert result.discovery_complete
    fetch.assert_called_once_with(cursor)


@pytest.mark.parametrize("cursor", [None, False, 0, [], {}, ["next"]])
def test_malformed_cursor_cannot_be_interpreted_as_terminal_exhaustion(cursor):
    fetch = Mock()
    with pytest.raises(history.CopilotDiscoveryIncompleteError):
        history.traverse_copilot_catalog({"chats": _chats(0, 17), "syncState": cursor}, fetch, lambda: False)
    fetch.assert_not_called()


def test_missing_cursor_is_incomplete_even_when_a_page_has_valid_sessions():
    fetch = Mock()
    with pytest.raises(history.CopilotDiscoveryIncompleteError):
        history.traverse_copilot_catalog({"chats": _chats(0, 17)}, fetch, lambda: False)
    fetch.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"syncState": ""}, {"chats": None, "syncState": ""}, {"chats": {}, "syncState": ""}],
)
def test_malformed_catalog_page_never_claims_completion(payload):
    with pytest.raises(history.CopilotDiscoveryIncompleteError):
        history.traverse_copilot_catalog(payload, Mock(), lambda: False)


@pytest.mark.parametrize(
    "invalid_chat",
    [
        None,
        "not a chat",
        {},
        {"conversationId": ""},
        {"conversationId": 123},
        {"conversationId": "bad/id"},
        {"conversationId": "bad?query=1"},
        {"conversationId": "bad#fragment"},
        {"conversationId": "bad/"},
        {"conversationId": "bad "},
    ],
)
def test_malformed_chat_cannot_be_silently_dropped_from_a_complete_catalog(invalid_chat):
    payload = {"chats": [*_chats(0, 17), invalid_chat], "syncState": ""}
    with pytest.raises(history.CopilotDiscoveryIncompleteError):
        history.traverse_copilot_catalog(payload, Mock(), lambda: False)


def test_repeated_cursor_fails_with_partial_catalog_instead_of_looping():
    fetch = Mock(return_value=_page(17, 34, "same-cursor"))
    with pytest.raises(history.CopilotDiscoveryIncompleteError) as incomplete:
        history.traverse_copilot_catalog(_page(0, 17, "same-cursor"), fetch, lambda: False)
    assert set(_identities(incomplete.value.conversations)) >= {f"session-{index}" for index in range(17)}
    fetch.assert_called_once_with("same-cursor")


def test_cyclic_cursor_fails_before_fetching_an_already_consumed_cursor_again():
    fetch = Mock(side_effect=[_page(17, 34, "cursor-b"), _page(34, 51, "cursor-a")])
    with pytest.raises(history.CopilotDiscoveryIncompleteError) as incomplete:
        history.traverse_copilot_catalog(_page(0, 17, "cursor-a"), fetch, lambda: False)
    assert set(_identities(incomplete.value.conversations)) >= {f"session-{index}" for index in range(17)}
    assert fetch.call_args_list == [call("cursor-a"), call("cursor-b")]


def test_two_consecutive_pages_without_new_unique_sessions_fail_closed():
    fetch = Mock(side_effect=[_page(0, 17, "cursor-b"), _page(0, 17, "cursor-c")])
    with pytest.raises(history.CopilotDiscoveryIncompleteError) as incomplete:
        history.traverse_copilot_catalog(_page(0, 17, "cursor-a"), fetch, lambda: False)
    assert len(incomplete.value.conversations) == 17
    assert fetch.call_args_list == [call("cursor-a"), call("cursor-b")]


def test_new_sessions_reset_the_consecutive_no_growth_counter():
    fetch = Mock(side_effect=[
        _page(0, 0, "cursor-b"),
        _page(17, 34, "cursor-c"),
        _page(0, 0, "cursor-d"),
        _page(34, 51, ""),
    ])
    result = history.traverse_copilot_catalog(_page(0, 17, "cursor-a"), fetch, lambda: False)
    assert result.discovery_complete
    assert len(result.conversations) == 51
    assert fetch.call_count == 4


def test_empty_terminal_page_finishes_after_nonempty_cursor_without_new_chats():
    fetch = Mock(return_value=_page(0, 0, ""))
    result = history.traverse_copilot_catalog(_page(0, 17, "cursor-a"), fetch, lambda: False)
    assert result.discovery_complete
    assert len(result.conversations) == 17
    assert result.expected_sessions is None


@pytest.mark.parametrize("response", [None, {"chats": _chats(17, 34)}, {"chats": None, "syncState": ""}])
def test_malformed_later_page_retains_previously_discovered_catalog(response):
    fetch = Mock(return_value=response)
    with pytest.raises(history.CopilotDiscoveryIncompleteError) as incomplete:
        history.traverse_copilot_catalog(_page(0, 17, "cursor-a"), fetch, lambda: False)
    assert set(_identities(incomplete.value.conversations)) >= {f"session-{index}" for index in range(17)}


def test_fetch_exception_is_wrapped_with_the_partial_catalog():
    fetch = Mock(side_effect=ValueError("Fixture provider response could not be decoded."))
    with pytest.raises(history.CopilotDiscoveryIncompleteError) as incomplete:
        history.traverse_copilot_catalog(_page(0, 17, "cursor-a"), fetch, lambda: False)
    assert _identities(incomplete.value.conversations) == [f"session-{index}" for index in range(17)]


def test_scan_bound_cannot_be_reported_as_provider_exhaustion():
    fetch = Mock(return_value=_page(17, 34, "cursor-b"))
    with patch.object(history, "COPILOT_SCAN_LIMIT", 2):
        with pytest.raises(history.CopilotDiscoveryIncompleteError) as incomplete:
            history.traverse_copilot_catalog(_page(0, 17, "cursor-a"), fetch, lambda: False)
    assert len(incomplete.value.conversations) == 34
    fetch.assert_called_once_with("cursor-a")


@pytest.mark.parametrize("cursor", ["cursor-a", ""])
def test_stop_before_fetch_cannot_return_a_complete_catalog(cursor):
    fetch = Mock()
    result = history.traverse_copilot_catalog(_page(0, 17, cursor), fetch, lambda: True)
    assert result.discovery_complete is False
    assert result.expected_sessions is None
    assert result.completion_basis == ""
    fetch.assert_not_called()


@pytest.mark.parametrize("cursor", ["cursor-b", ""])
def test_stop_after_a_page_is_fetched_preserves_partial_state_without_completing(cursor):
    stopped = False

    def fetch(_cursor):
        nonlocal stopped
        stopped = True
        return _page(17, 34, cursor)

    result = history.traverse_copilot_catalog(_page(0, 17, "cursor-a"), fetch, lambda: stopped)
    assert result.discovery_complete is False
    assert result.expected_sessions is None
    assert result.completion_basis == ""
    assert set(_identities(result.conversations)) >= {f"session-{index}" for index in range(17)}
