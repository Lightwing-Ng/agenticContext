"""Copilot extraction, completeness, authenticated readiness, and sync regressions."""

# Code version: v1.3.0-codex.0

from contextlib import nullcontext
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import test_sidebar_e2e
from app.core import copilot_history as history
from app.core.config import CrawlConfig
from app.core.copilot_history_store import CopilotHistoryStore
from app.core.state import TaskSnapshot, TaskState


disposable_browser = test_sidebar_e2e.disposable_browser


def _conversation(identity="session-1"):
    return {
        "conversation_id": identity,
        "conversation_url": f"https://copilot.com/chat/conversation/{identity}",
        "conversation_title": "Fixture session",
    }


def _message(identity, role, text):
    return {"message_id": identity, "role": role, "content_text": text}


def _turn(identity="turn-1"):
    return [
        _message(f"{identity}:user", "user", f"Question {identity}."),
        _message(f"{identity}:assistant", "assistant", f"Answer {identity}."),
    ]


class RenderedPage:
    """Serve deterministic browser snapshots without accessing a signed-in profile."""

    def __init__(self, *, windows=None, links=None, state=None, at_end=True, catalog=None):
        self.url = history.COPILOT_HOME_URL
        self.visited = []
        self.windows = windows or [{"messages": _turn()}]
        self.link_windows = links or [[_conversation()]]
        self.message_round = 0
        self.link_round = 0
        self.account_state = state or {"signed_in": True, "loading": False}
        self.at_end = at_end
        self.catalog = catalog or {"ready": False}

    def goto(self, url, **_kwargs):
        self.url = url
        self.visited.append(url)

    def title(self):
        return "Microsoft Copilot"

    def wait_for_timeout(self, _milliseconds):
        pass

    def evaluate(self, script, options=None):
        if script == history.COPILOT_STATE_SCRIPT:
            return dict(self.account_state)
        if script == history.COPILOT_MESSAGES_SCRIPT:
            result = self.windows[min(self.message_round, len(self.windows) - 1)]
            self.message_round += 1
            return result
        if script == history.COPILOT_LINKS_SCRIPT:
            result = self.link_windows[min(self.link_round, len(self.link_windows) - 1)]
            self.link_round += 1
            return result
        if script == history.COPILOT_CATALOG_SCRIPT:
            return self.catalog
        if script == history.COPILOT_SCROLL_SCRIPT:
            return {"moved": False, "at_end": self.at_end, "height": 1_000, "position": 0 if options["top"] else 1_000}
        raise AssertionError("Unexpected extraction script.")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://copilot.com/chat/conversation/abc-123?tracking=1#fragment", "https://copilot.com/chat/conversation/abc-123"),
        ("https://www.copilot.com:443/chat/conversation/abc-123/", "https://copilot.com/chat/conversation/abc-123"),
        ("http://copilot.com/chat/conversation/abc-123", ""),
        ("https://copilot.com.evil.example/chat/conversation/abc-123", ""),
        ("https://example.com/chat/conversation/abc-123", ""),
        ("https://user:password@copilot.com/chat/conversation/abc-123", ""),
        ("https://copilot.com:444/chat/conversation/abc-123", ""),
        ("https://copilot.com/chat/all", ""),
        ("https://copilot.com/chat/conversation/abc/another", ""),
        ("https://copilot.com/chat/conversation/", ""),
    ],
)
def test_conversation_normalization_has_exact_first_party_boundary(url, expected):
    assert history.normalize_copilot_conversation_url(url) == expected


@pytest.mark.parametrize(
    "state",
    [{"signed_out": True}, {"challenge": True}],
)
def test_semantic_authentication_failures_stop_collection(state):
    page = RenderedPage(state=state)
    with pytest.raises(history.CopilotAccessError):
        history.inspect_copilot_page(page)


def test_redirected_account_page_is_not_treated_as_empty_history():
    page = RenderedPage()
    page.url = "https://login.live.com/"
    with pytest.raises(history.CopilotAccessError, match="sign-in"):
        history.inspect_copilot_page(page)


def test_readiness_waits_for_loading_and_cooperatively_stops():
    page = RenderedPage()
    with patch.object(history, "inspect_copilot_page", side_effect=[
        {"signed_in": True, "loading": True},
        {"signed_in": True, "loading": False},
    ]):
        assert history.wait_for_copilot_ready(page)["signed_in"]
    with patch.object(history, "inspect_copilot_page") as inspect:
        assert history.wait_for_copilot_ready(page, lambda: True) == {}
    inspect.assert_not_called()


@pytest.mark.parametrize("title", ["Access denied", "How does CAPTCHA work?", "Verify you are human: examples"])
def test_authenticated_conversation_title_cannot_become_a_security_challenge(title):
    page = RenderedPage()
    with patch.object(page, "title", return_value=title):
        assert history.inspect_copilot_page(page)["signed_in"]


def test_readiness_allows_a_bounded_blank_startup_before_authenticated_navigation():
    page = RenderedPage()
    page.url = "about:blank"

    def finish_navigation(_milliseconds):
        page.url = history.COPILOT_HOME_URL

    with patch.object(page, "wait_for_timeout", side_effect=finish_navigation):
        assert history.wait_for_copilot_ready(page, timeout_seconds=1)["signed_in"]


def test_partial_catalog_preserves_unique_sessions_when_continuation_cannot_be_read():
    page = RenderedPage(catalog={
        "ready": True,
        "catalog": {"chats": [
            {"conversationId": "session-1", "chatName": "First"},
            {"conversationId": "session-1", "chatName": "First"},
            {"conversationId": "session-2", "chatName": "Second"},
        ], "syncState": "unread-continuation"},
        "session_id": "fixture-session",
    })
    with patch.object(history, "fetch_copilot_catalog_page", side_effect=RuntimeError("Fixture continuation unavailable.")):
        with pytest.raises(history.CopilotDiscoveryIncompleteError) as incomplete:
            history.discover_copilot_conversations(page, lambda: False)
    conversations = incomplete.value.conversations
    assert page.visited == [history.COPILOT_HISTORY_URL]
    assert [row["conversation_id"] for row in conversations] == ["session-1", "session-2"]
    assert page.link_round == 0


def test_discovery_rejects_missing_catalog_even_when_the_account_is_ready():
    page = RenderedPage(at_end=False, links=[[{"url": _conversation()["conversation_url"], "title": "Fixture"}]])
    with patch.object(history, "COPILOT_READY_TIMEOUT_SECONDS", 0):
        with pytest.raises(RuntimeError, match="incomplete"):
            history.discover_copilot_conversations(page, lambda: False)


def test_recent_sidebar_links_cannot_prove_catalog_exhaustion_after_stable_scroll():
    recent = [
        {"url": _conversation(f"recent-{index}")["conversation_url"], "title": f"Recent session {index}"}
        for index in range(17)
    ]
    page = RenderedPage(links=[recent])
    with patch.object(history, "COPILOT_READY_TIMEOUT_SECONDS", 0):
        with pytest.raises(RuntimeError, match="incomplete|exhaust|history|catalog"):
            history.discover_copilot_conversations(page, lambda: False)
    assert page.link_round == 0


def test_transcript_accumulates_virtualized_windows_with_stable_ids_in_order():
    page = RenderedPage(windows=[
        {"messages": _turn("turn-1")},
        {"messages": _turn("turn-1") + _turn("turn-2")},
        {"messages": _turn("turn-2")},
    ])
    messages = history.collect_copilot_messages(page, _conversation(), lambda: False)
    assert [row["content_text"] for row in messages] == [row["content_text"] for row in _turn("turn-1") + _turn("turn-2")]
    assert [row["role"] for row in messages] == ["user", "assistant", "user", "assistant"]
    assert [row["message_index"] for row in messages] == [0, 1, 2, 3]
    assert [row["turn_index"] for row in messages] == [1, 1, 2, 2]


def test_transcript_preserves_order_when_earlier_messages_are_prepended_late():
    page = RenderedPage(windows=[
        {"messages": _turn("turn-2")},
        {"messages": _turn("turn-1") + _turn("turn-2")},
    ])
    messages = history.collect_copilot_messages(page, _conversation(), lambda: False)
    assert [row["content_text"] for row in messages] == [row["content_text"] for row in _turn("turn-1") + _turn("turn-2")]


def test_missing_provider_ids_cannot_silently_overwrite_a_virtualized_window():
    first = [{**row, "message_id": ""} for row in _turn("turn-1")]
    second = [{**row, "message_id": ""} for row in _turn("turn-2")]
    page = RenderedPage(windows=[{"messages": first}, {"messages": second}])
    with pytest.raises(RuntimeError, match="identity|identities|virtual|position"):
        history.collect_copilot_messages(page, _conversation(), lambda: False)


@pytest.mark.parametrize(
    "payload, error",
    [
        ({"messages": _turn(), "generating": True}, "generating"),
        ({"messages": []}, "no cacheable text"),
        ({"messages": [None]}, "unsupported message"),
        ({"messages": [{"role": "system", "content_text": "Unexpected."}]}, "unsupported message"),
        ({"messages": [{"role": "user", "content_text": ""}]}, "empty transcript"),
    ],
)
def test_incomplete_or_malformed_transcript_cannot_be_returned(payload, error):
    with pytest.raises(RuntimeError, match=error):
        history.collect_copilot_messages(RenderedPage(windows=[payload]), _conversation(), lambda: False)


def test_js_extracts_roles_without_repeating_nested_markup_or_toolbar_text(disposable_browser):
    context = disposable_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        page.set_content('''<main><section data-testid="MessageListContainer">
          <article data-testid="m365-chat-llm-web-ui-chat-message">
            <div data-testid="chatQuestion"><div data-testid="chatOutput">Fixture question.<button>Copy question</button></div></div>
            <div data-testid="copilot-message-div" id="chatMessageResponse-fixture-1">
              <div data-testid="markdown-reply"><p>Fixture answer.</p><a href="https://example.com/source">Source</a><button>Copy answer</button><span aria-hidden="true">Toolbar hint</span></div>
            </div>
          </article>
        </section></main>''')
        payload = page.evaluate(history.COPILOT_MESSAGES_SCRIPT)
        assert [row["role"] for row in payload["messages"]] == ["user", "assistant"]
        assert len({row["message_id"] for row in payload["messages"]}) == 2
        assert payload["messages"][0]["content_text"] == "Fixture question."
        assert "Copy answer" not in payload["messages"][1]["content_text"]
        assert "Toolbar hint" not in payload["messages"][1]["content_text"]
        assert payload["messages"][1]["source_links"] == ["https://example.com/source"]
        assert payload["messages"][1]["message_timestamp"] == ""
    finally:
        context.close()


def test_authentication_script_ignores_sign_in_links_inside_untrusted_chat(disposable_browser):
    context = disposable_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        page.set_content('''<nav data-testid="nav-footer"><img alt="Account avatar"></nav>
          <main><section data-testid="MessageListContainer"><h2>Verify you are human</h2>
          <p>A quoted sign-in example: <a href="https://example.com">Sign in</a></p></section></main>''')
        state = page.evaluate(history.COPILOT_STATE_SCRIPT)
        assert state["signed_in"]
        assert not state["signed_out"]
        assert not state["challenge"]
    finally:
        context.close()


@pytest.mark.parametrize("title", ["Access denied", "How does CAPTCHA work?", "Verify you are human: examples"])
def test_account_script_does_not_classify_session_titles_as_challenges(disposable_browser, title):
    context = disposable_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        page.set_content('''<nav data-testid="nav-footer"><img alt="Account avatar"></nav>
          <main><section data-testid="MessageListContainer">Fixture transcript.</section></main>''')
        page.evaluate("value => document.title = value", title)
        state = page.evaluate(history.COPILOT_STATE_SCRIPT)
        assert state["signed_in"]
        assert not state["challenge"]
    finally:
        context.close()


def test_account_script_does_not_infer_authentication_from_a_link_inside_chat(disposable_browser):
    context = disposable_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        page.set_content('''<main><section data-testid="MessageListContainer">
          <a href="https://copilot.com/chat/conversation/quoted-example">A quoted conversation URL</a>
          </section></main>''')
        assert not page.evaluate(history.COPILOT_STATE_SCRIPT)["signed_in"]
    finally:
        context.close()


def test_history_catalog_ignores_sidebar_anchors_beside_observed_grid_cards(disposable_browser):
    context = disposable_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        document = '''<aside><a href="https://copilot.com/chat/conversation/sidebar-only">Recent sidebar chat</a></aside>
          <section id="llm-web-ui-messageList-scrollable-container">
          <input data-testid="chat-history-search-input" aria-label="Search history">
          <div role="grid"><div role="row">Observed all-chat card without a conversation anchor</div></div>
          </section><script>window.__staticRouterHydrationData = {
            loaderData: {'all-chat-history': {store: {chatLandingPageHistoryList: {
              chats: [{conversationId: 'catalog-fixture', chatName: 'Catalog fixture'}],
              syncState: 'opaque-observed-token'
            }}}}
          };</script>'''
        page.route(history.COPILOT_HISTORY_URL, lambda route: route.fulfill(content_type="text/html", body=document))
        page.goto(history.COPILOT_HISTORY_URL)
        payload = page.evaluate(history.COPILOT_LINKS_SCRIPT)
        assert payload == [{
            "url": "https://copilot.com/chat/conversation/catalog-fixture",
            "title": "Catalog fixture",
        }]
    finally:
        context.close()


@pytest.mark.parametrize(
    "script_name, argument, expected_type",
    [
        ("COPILOT_STATE_SCRIPT", None, dict),
        ("COPILOT_LINKS_SCRIPT", None, list),
        ("COPILOT_CATALOG_SCRIPT", None, dict),
        ("COPILOT_MESSAGES_SCRIPT", None, dict),
        ("COPILOT_SCROLL_SCRIPT", {"kind": "history", "top": False}, dict),
    ],
)
def test_scripts_survive_safari_whitespace_compaction(
    disposable_browser, script_name, argument, expected_type
):
    context = disposable_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        page.set_content('''<nav data-testid="nav-footer"><img alt="Account avatar"></nav>
          <main><a href="https://copilot.com/chat/conversation/fixture">Fixture session</a>
          <section data-testid="MessageListContainer"></section></main>''')
        compact = re.sub(r"\s+", " ", getattr(history, script_name).strip())
        assert isinstance(page.evaluate(compact, argument), expected_type)
    finally:
        context.close()


def _state():
    return TaskState("test", snapshot_factory=lambda version: TaskSnapshot(version))


def _sync_patches(page, conversations, collector):
    return (
        patch.object(history, "browser_descriptors", return_value={"safari": SimpleNamespace(engine="safari")}),
        patch.object(history, "open_copilot_history_page", return_value=nullcontext(page)),
        patch.object(history, "wait_for_copilot_ready", return_value={"signed_in": True}),
        patch.object(history, "discover_copilot_conversations", return_value=history.CopilotDiscovery(
            conversations, True, completion_basis="Fixture catalog exhausted its explicit continuation."
        )),
        patch.object(history, "collect_copilot_messages", side_effect=collector),
        patch.object(history, "wait_for_cache_scan", return_value=False),
    )


def test_sync_persists_complete_sessions_idempotently_and_preserves_failed_sessions(tmp_path: Path):
    existing = CopilotHistoryStore(tmp_path)
    existing.replace_conversation(_conversation("session-2"), _turn("old"))
    prior_rows = existing.rows
    conversations = [_conversation(), _conversation("session-2")]
    state = _state()
    patches = _sync_patches(RenderedPage(), conversations, [_turn(), ValueError("Fixture partial transcript.")])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = history.sync_copilot_history(state, CrawlConfig(copilot_browser="safari"), lambda: False, tmp_path)
    expected = {"sessions": 2, "messages": 2, "added_or_changed": 2, "unchanged": 0, "failed": 1, "stopped": False}
    assert {key: result[key] for key in expected} == expected
    reloaded = CopilotHistoryStore(tmp_path)
    assert reloaded.cached_conversations == 2
    assert [row for row in reloaded.rows if row["conversation_id"] == "session-2"] == prior_rows
    before = (reloaded.path.read_bytes(), reloaded.path.stat().st_mtime_ns)
    patches = _sync_patches(RenderedPage(), conversations, [_turn(), _turn("old")])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = history.sync_copilot_history(state, CrawlConfig(copilot_browser="safari"), lambda: False, tmp_path)
    assert result["unchanged"] == 2
    assert result["added_or_changed"] == 0
    assert state.snapshot()["discovered_images"] == result["messages"]
    assert (reloaded.path.read_bytes(), reloaded.path.stat().st_mtime_ns) == before


def test_sync_refuses_unproven_shrink_of_previously_cached_session(tmp_path: Path):
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _turn("turn-1") + _turn("turn-2"))
    before = store.path.read_bytes()
    patches = _sync_patches(RenderedPage(), [_conversation()], [_turn("turn-2")])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = history.sync_copilot_history(_state(), CrawlConfig(copilot_browser="safari"), lambda: False, tmp_path)
    assert result["failed"] == 1
    assert store.path.read_bytes() == before


def _legacy_directory(identities, *, status="valid"):
    return {
        "status": status, "sessions": len(identities) if status == "valid" else None,
        "conversation_ids": identities, "catalog_complete": False, "cursor_chain_proven": False,
    }


def test_complete_current_web_sync_cannot_erase_known_legacy_scope_on_repeat(tmp_path: Path):
    conversations = [_conversation(), _conversation("session-2")]
    legacy = _legacy_directory([f"Legacy{index}" for index in range(5)])
    for attempt in range(2):
        state = _state()
        patches = _sync_patches(RenderedPage(), conversations, [_turn(), _turn()])
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patch.object(
            history, "read_copilot_legacy_catalog", return_value=legacy,
        ):
            result = history.sync_copilot_history(state, CrawlConfig(copilot_browser="safari"), lambda: False, tmp_path)
        assert result["sessions"] == 2 and result["messages"] == 4
        assert result["discovered_sessions"] == 5 and result["expected_sessions"] is None
        assert not result["discovery_complete"] and not result["sync_complete"]
        assert "legacy" in result["scope_reason"]
        assert state.snapshot()["queued_tweets"] == 5
        assert not state.snapshot()["discovery_complete"]
        assert CopilotHistoryStore(tmp_path).cached_conversations == 2
        assert result["unchanged"] == 2 * attempt


def test_invalid_legacy_evidence_keeps_current_collection_partial(tmp_path: Path):
    patches = _sync_patches(RenderedPage(), [_conversation()], [_turn()])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patch.object(
        history, "read_copilot_legacy_catalog", return_value=_legacy_directory([], status="invalid"),
    ):
        result = history.sync_copilot_history(_state(), CrawlConfig(copilot_browser="safari"), lambda: False, tmp_path)
    assert result["sessions"] == 1 and CopilotHistoryStore(tmp_path).cached_messages == 2
    assert not result["sync_complete"] and not result["discovery_complete"]
    assert "cannot be verified" in result["scope_reason"]


def test_preserved_legacy_session_does_not_block_collecting_current_web_sessions(tmp_path: Path):
    legacy_conversation = {
        "conversation_id": "LegacyOne", "conversation_title": "Preserved legacy fixture",
        "conversation_url": "https://copilot.microsoft.com/chats/LegacyOne",
    }
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(legacy_conversation, _turn("old"))
    preserved = store.rows
    patches = _sync_patches(RenderedPage(), [_conversation()], [_turn()])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patch.object(
        history, "read_copilot_legacy_catalog", return_value=_legacy_directory(["LegacyOne"]),
    ):
        result = history.sync_copilot_history(_state(), CrawlConfig(copilot_browser="safari"), lambda: False, tmp_path)
    rows = CopilotHistoryStore(tmp_path).rows
    assert [row for row in rows if row["conversation_id"] == "LegacyOne"] == preserved
    assert CopilotHistoryStore(tmp_path).cached_conversations == 2
    assert result["sessions"] == 1 and not result["sync_complete"]


def test_sync_stop_during_discovery_preserves_existing_rows(tmp_path: Path):
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _turn())
    before = store.path.read_bytes()
    stopped = False

    def discover(_page, _should_stop, _on_progress):
        nonlocal stopped
        stopped = True
        return history.CopilotDiscovery([_conversation()], False)

    patches = _sync_patches(RenderedPage(), [_conversation()], [_turn()])
    with patches[0], patches[1], patches[2], patches[4] as collect, patches[5], patch.object(
        history, "discover_copilot_conversations", side_effect=discover
    ):
        result = history.sync_copilot_history(_state(), CrawlConfig(copilot_browser="safari"), lambda: stopped, tmp_path)
    assert result["stopped"]
    assert result["sessions"] == 0
    assert store.path.read_bytes() == before
    collect.assert_not_called()


def test_sync_stop_during_transcript_does_not_persist_partial_messages(tmp_path: Path):
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(_conversation(), _turn())
    before = store.path.read_bytes()
    stopped = False

    def collect(_page, _conversation, _should_stop):
        nonlocal stopped
        stopped = True
        return _turn()[:1]

    patches = _sync_patches(RenderedPage(), [_conversation()], collect)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        result = history.sync_copilot_history(_state(), CrawlConfig(copilot_browser="safari"), lambda: stopped, tmp_path)
    assert result["stopped"]
    assert result["sessions"] == 0
    assert store.path.read_bytes() == before


def test_sync_authentication_loss_aborts_instead_of_skipping_to_other_sessions(tmp_path: Path):
    patches = _sync_patches(
        RenderedPage(),
        [_conversation(), _conversation("session-2")],
        history.CopilotAccessError("Fixture authentication lost."),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4] as collect, patches[5]:
        with pytest.raises(history.CopilotAccessError, match="authentication lost"):
            history.sync_copilot_history(_state(), CrawlConfig(copilot_browser="safari"), lambda: False, tmp_path)
    assert collect.call_count == 1
    assert not CopilotHistoryStore(tmp_path).path.exists()


def test_incomplete_catalog_discovery_preserves_all_74_existing_messages(tmp_path: Path):
    store = CopilotHistoryStore(tmp_path)
    store.replace_conversation(
        _conversation("cached-before-discovery"),
        [
            _message(f"original-{index}", "user" if index % 2 == 0 else "assistant", f"Preserved fixture message {index}.")
            for index in range(74)
        ],
    )
    before = (store.path.read_bytes(), store.path.stat().st_mtime_ns)
    recent = [
        {"url": _conversation(f"recent-{index}")["conversation_url"], "title": f"Recent session {index}"}
        for index in range(17)
    ]
    page = RenderedPage(links=[recent])
    with patch.object(history, "browser_descriptors", return_value={"safari": SimpleNamespace(engine="safari")}), patch.object(
        history, "open_copilot_history_page", return_value=nullcontext(page)
    ), patch.object(history, "COPILOT_READY_TIMEOUT_SECONDS", 0), patch.object(
        history, "collect_copilot_messages", return_value=_turn()
    ) as collect, patch.object(history, "wait_for_cache_scan", return_value=False):
        with pytest.raises(RuntimeError, match="incomplete|exhaust|history|catalog"):
            history.sync_copilot_history(_state(), CrawlConfig(copilot_browser="safari"), lambda: False, tmp_path)
    collect.assert_not_called()
    assert (store.path.read_bytes(), store.path.stat().st_mtime_ns) == before
    assert CopilotHistoryStore(tmp_path).cached_messages == 74
