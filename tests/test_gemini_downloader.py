"""Focused tests for Gemini session history Parquet persistence."""

# Code version: v1.10.5-codex.1

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode

import pyarrow.parquet as pq
import pytest

from app.core.browser_sessions import TRANSIENT_BROWSER_ERROR_MARKERS
from app.core.gemini_downloader import (
    GEMINI_TRANSIENT_NAVIGATION_MARKERS,
    GeminiConversationLink,
    GeminiHistoryStore,
    GeminiNoCacheableMessagesError,
    _build_gemini_history_rpc_page_request,
    _decode_gemini_history_rpc_payloads,
    _fetch_gemini_conversation_rpc_payloads,
    _gemini_message_timestamps_from_rpc_payloads,
    _gemini_links_and_cursor_from_rpc_payload,
    _open_gemini_sidebar,
    _prepare_gemini_page_for_rendering,
    _read_gemini_conversation_links,
    _scroll_gemini_conversation_navigation,
    _wait_for_gemini_ready,
    build_gemini_initial_snapshot,
    gemini_discovery_checkpoint_path,
    gemini_conversation_id,
    gemini_history_path,
    inspect_gemini_bot_check,
    is_gemini_conversation_url,
    normalize_gemini_conversation_url,
    load_gemini_discovery_checkpoint,
    save_gemini_discovery_checkpoint,
    wait_for_gemini_bot_check_clear,
    collect_gemini_conversation_links,
)
from app.core.config import CrawlConfig
from app.core.state import TaskSnapshot, TaskState
from app.core.resource_persistence import GEMINI_HISTORY_SCHEMA
from app.core.safari_automation import SafariPage


def test_gemini_navigation_reuses_the_shared_transient_error_contract() -> None:
    assert GEMINI_TRANSIENT_NAVIGATION_MARKERS is TRANSIENT_BROWSER_ERROR_MARKERS
    assert "ERR_CONNECTION_TIMED_OUT" in GEMINI_TRANSIENT_NAVIGATION_MARKERS


def _messages(user_text: str = "Hello", assistant_text: str = "Hi there") -> list[dict[str, object]]:
    return [
        {
            "conversation_title": "Demo chat",
            "turn_index": 0,
            "message_index": 0,
            "role": "user",
            "author_label": "You",
            "content_text": user_text,
            "content_html": f"<p>{user_text}</p>",
            "source_links": [],
            "model_label": "",
        },
        {
            "conversation_title": "Demo chat",
            "turn_index": 0,
            "message_index": 1,
            "role": "assistant",
            "author_label": "Gemini",
            "content_text": assistant_text,
            "content_html": f"<p>{assistant_text}</p>",
            "source_links": ["https://example.com/source", "https://example.com/source"],
            "model_label": "Gemini Flash",
        },
    ]


def test_open_gemini_sidebar_expands_the_independent_recents_list() -> None:
    class Page:
        def __init__(self) -> None:
            self.scripts: list[str] = []
            self.waits: list[int] = []
            self.responses = [
                True,
                {"buttonFound": True, "expanded": False, "links": 0},
                {"buttonFound": True, "expanded": True, "links": 30},
            ]

        def evaluate(self, script):
            self.scripts.append(script)
            return self.responses.pop(0)

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = Page()

    _open_gemini_sidebar(page)

    assert len(page.scripts) == 3
    assert "open sidebar" in page.scripts[0]
    assert "chats-expandable-section" in page.scripts[1]
    assert "recentsButton" in page.scripts[2]
    assert page.waits == [1_000, 500]


def test_gemini_history_rpc_response_exposes_links_and_cursor() -> None:
    payload = [
        None,
        "next-page-token",
        [
            ["c_abc123", "First session"],
            ["c_def456", "Second session"],
        ],
    ]
    body = json.dumps([["wrb.fr", "MaZiqc", json.dumps(payload)]])

    decoded = _decode_gemini_history_rpc_payloads(body)
    links, cursor = _gemini_links_and_cursor_from_rpc_payload(decoded[0])

    assert cursor == "next-page-token"
    assert links == [
        GeminiConversationLink("abc123", "https://gemini.google.com/app/abc123", "First session"),
        GeminiConversationLink("def456", "https://gemini.google.com/app/def456", "Second session"),
    ]


def test_gemini_conversation_rpc_response_exposes_source_turn_timestamps() -> None:
    payload = [
        [
            [
                ["c_conversation", "r_user"],
                ["c_conversation", "r_assistant"],
                [["Question", None, None, None, [[]]]],
                [["r_assistant", ["Answer"]]],
                [1_780_555_237, 509_580_00],
            ],
            [
                ["c_conversation", "r_user_2"],
                ["c_conversation", "r_assistant_2"],
                [["Question 2", None, None, None, [[]]]],
                [["r_assistant_2", ["Answer 2"]]],
                [1_780_554_365, 682_736_000],
            ],
        ],
        None,
        None,
        "continuation",
    ]

    timestamps = _gemini_message_timestamps_from_rpc_payloads([payload], "conversation")

    assert timestamps == {
        0: "2026-06-04T06:40:37.050958Z",
        1: "2026-06-04T06:26:05.682736Z",
    }


def test_safari_conversation_rpc_uses_current_google_token_and_target_source_path() -> None:
    class Page(SafariPage):
        def __init__(self) -> None:
            self.scripts: list[str] = []

        def evaluate(self, expression: str, argument=None):
            self.scripts.append(expression)
            if "({ conversationId, stateKey })" in expression:
                return {"started": True}
            return {"state": "done", "ok": True, "status": 200, "length": 0, "error": ""}

        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    page = Page()
    conversation = GeminiConversationLink(
        "conversation-source-time",
        "https://gemini.google.com/app/conversation-source-time",
        "Source time",
    )

    assert _fetch_gemini_conversation_rpc_payloads(page, conversation) == []
    assert "window.WIZ_global_data?.SNlM0e" in page.scripts[0]
    assert "endpoint.searchParams.set('source-path', '/app/' + conversationId)" in page.scripts[0]


def test_gemini_history_rpc_next_page_preserves_auth_fields() -> None:
    batch = [[["MaZiqc", "[39,null,[0,null,1]]", None, "generic"]]]
    request = SimpleNamespace(
        url=(
            "https://gemini.google.com/_/BardChatUi/data/batchexecute"
            "?rpcids=MaZiqc&_reqid=123"
        ),
        post_data=urlencode({"f.req": json.dumps(batch), "at": "csrf-token"}),
    )

    url, body = _build_gemini_history_rpc_page_request(request, "cursor-value")
    fields = parse_qs(body)
    updated_batch = json.loads(fields["f.req"][0])

    assert "rpcids=MaZiqc" in url
    assert "_reqid=123" in url
    assert fields["at"] == ["csrf-token"]
    assert json.loads(updated_batch[0][0][1]) == [1_000, "cursor-value", [0, None, 1]]


def test_gemini_discovery_checkpoint_round_trips_and_rejects_stale_data(
    tmp_path: Path,
) -> None:
    links = [
        GeminiConversationLink(
            "abc123",
            "https://gemini.google.com/app/abc123",
            "First session",
        ),
        GeminiConversationLink(
            "def456",
            "https://gemini.google.com/app/def456",
            "Second session",
        ),
    ]

    checkpoint_path = save_gemini_discovery_checkpoint(links, tmp_path)

    assert checkpoint_path == gemini_discovery_checkpoint_path(tmp_path)
    assert load_gemini_discovery_checkpoint(tmp_path) == links
    with patch(
        "app.core.gemini_downloader.time.time",
        return_value=checkpoint_path.stat().st_mtime + 86_401,
    ):
        assert load_gemini_discovery_checkpoint(tmp_path) == []


def test_gemini_discovery_checkpoint_ignores_unreadable_data(tmp_path: Path) -> None:
    checkpoint_path = gemini_discovery_checkpoint_path(tmp_path)
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text("not json", encoding="utf-8")

    assert load_gemini_discovery_checkpoint(tmp_path) == []



def test_gemini_conversation_urls_are_canonical_and_host_scoped() -> None:
    source = "https://gemini.google.com/app/abc_123?utm_source=test#fragment"

    assert normalize_gemini_conversation_url(source) == "https://gemini.google.com/app/abc_123"
    assert gemini_conversation_id(source) == "abc_123"
    assert is_gemini_conversation_url(source)
    assert not is_gemini_conversation_url("https://example.com/app/abc_123")
    assert not is_gemini_conversation_url("https://gemini.google.com/app")


def test_gemini_history_store_atomically_merges_and_replaces_conversations(tmp_path: Path) -> None:
    path = gemini_history_path(tmp_path)
    first = GeminiConversationLink("conversation-a", "https://gemini.google.com/app/conversation-a", "Demo chat")
    second = GeminiConversationLink("conversation-b", "https://gemini.google.com/app/conversation-b", "Second chat")
    store = GeminiHistoryStore(path)

    added, unchanged = store.replace_conversation(first, _messages(), "2026-08-12T05:00:00Z")
    assert added == 2
    assert not unchanged
    store.save()

    table = pq.read_table(path)
    assert table.schema.names == GEMINI_HISTORY_SCHEMA.names
    assert table.num_rows == 2
    assert table.to_pylist()[1]["source_links"] == ["https://example.com/source"]

    reloaded = GeminiHistoryStore(path)
    added, unchanged = reloaded.replace_conversation(first, _messages(), "2026-08-12T06:00:00Z")
    assert added == 0
    assert unchanged
    reloaded.replace_conversation(second, _messages("Question", "Answer"), "2026-08-12T06:00:00Z")
    reloaded.save()

    final_store = GeminiHistoryStore(path)
    assert final_store.cached_conversations == 2
    assert final_store.cached_messages == 4
    assert final_store.cached_conversation_ids == {"conversation-a", "conversation-b"}
    first_rows = [row for row in final_store.rows if row["conversation_id"] == "conversation-a"]
    assert {row["first_seen_at"] for row in first_rows} == {"2026-08-12T05:00:00Z"}
    assert {row["last_seen_at"] for row in first_rows} == {"2026-08-12T05:00:00Z"}

    added, unchanged = final_store.replace_conversation(
        first,
        [_messages("Edited question", "unused")[0]],
        "2026-08-12T07:00:00Z",
    )
    final_store.save()
    assert added == 1
    assert not unchanged
    assert final_store.cached_conversations == 2
    assert final_store.cached_messages == 3


def test_gemini_history_store_prefers_source_timestamp_over_capture_time(tmp_path: Path) -> None:
    store = GeminiHistoryStore(gemini_history_path(tmp_path))
    conversation = GeminiConversationLink(
        "conversation-source-time",
        "https://gemini.google.com/app/conversation-source-time",
        "Source time",
    )
    messages = _messages()
    messages[0]["message_timestamp"] = "2026-06-04T06:40:37.050958Z"
    messages[1]["message_timestamp"] = "2026-06-04T06:40:37.050958Z"

    store.replace_conversation(conversation, messages, "2026-08-15T17:38:42Z")

    assert {row["last_seen_at"] for row in store.rows} == {"2026-06-04T06:40:37.050958Z"}


def test_gemini_history_store_preserves_source_timestamp_when_refresh_omits_it(
    tmp_path: Path,
) -> None:
    history_path = gemini_history_path(tmp_path)
    conversation = GeminiConversationLink(
        "conversation-source-time",
        "https://gemini.google.com/app/conversation-source-time",
        "Source time",
    )
    timestamped_messages = _messages()
    timestamped_messages[0]["message_timestamp"] = "2026-06-04T06:40:37.050958Z"
    timestamped_messages[1]["message_timestamp"] = "2026-06-04T06:40:37.050958Z"
    store = GeminiHistoryStore(history_path)
    store.replace_conversation(conversation, timestamped_messages, "2026-08-15T17:38:42Z")
    store.save()

    refreshed_store = GeminiHistoryStore(history_path)
    refreshed_store.replace_conversation(
        conversation,
        _messages(),
        "2026-08-28T03:20:00Z",
    )

    assert {row["last_seen_at"] for row in refreshed_store.rows} == {
        "2026-06-04T06:40:37.050958Z"
    }


def test_gemini_history_store_rejects_sessions_without_text_rows(tmp_path: Path) -> None:
    store = GeminiHistoryStore(gemini_history_path(tmp_path))
    conversation = GeminiConversationLink(
        "conversation-empty",
        "https://gemini.google.com/app/conversation-empty",
        "Media only",
    )

    with pytest.raises(GeminiNoCacheableMessagesError, match="no cacheable text"):
        store.replace_conversation(conversation, [], "2026-08-14T08:00:00Z")

    assert store.cached_conversations == 0


def test_gemini_initial_snapshot_counts_cached_rows(tmp_path: Path) -> None:
    path = gemini_history_path(tmp_path)
    store = GeminiHistoryStore(path)
    store.replace_conversation(
        GeminiConversationLink("conversation-a", "https://gemini.google.com/app/conversation-a", "Demo"),
        _messages(),
        "2026-08-12T05:00:00Z",
    )
    store.save()

    snapshot = build_gemini_initial_snapshot("v-test", tmp_path)

    assert snapshot.downloaded_posts == 1
    assert snapshot.discovered_tweets == 1
    assert snapshot.downloaded_tweets == 2
    assert snapshot.output_dir == str(path.parent)
    assert "1 session, 2 messages" in snapshot.message


def test_gemini_history_store_rejects_an_unreadable_existing_parquet(tmp_path: Path) -> None:
    path = gemini_history_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not parquet", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Parquet is unreadable"):
        GeminiHistoryStore(path)


def test_rendered_gemini_links_are_deduplicated_and_normalized() -> None:
    class Page:
        def evaluate(self, _script):
            return [
                {
                    "conversationId": "abc",
                    "url": "https://gemini.google.com/app/abc?query=1",
                    "title": "First",
                },
                {
                    "conversationId": "abc",
                    "url": "https://gemini.google.com/app/abc",
                    "title": "Duplicate",
                },
                {
                    "conversationId": "foreign",
                    "url": "https://example.com/app/foreign",
                    "title": "Foreign",
                },
            ]

    assert _read_gemini_conversation_links(Page()) == [
        GeminiConversationLink("abc", "https://gemini.google.com/app/abc", "First")
    ]


def test_gemini_scroll_dispatches_for_a_virtualized_conversation_list() -> None:
    class Page:
        def evaluate(self, script):
            self.script = script
            return {
                "moved": False,
                "eventDispatched": True,
                "top": 0,
                "height": 0,
                "viewport": 0,
            }

    page = Page()
    state = _scroll_gemini_conversation_navigation(page)

    assert state["eventDispatched"] is True
    assert "closest('infinite-scroller')" in page.script
    assert "dispatchEvent(new Event('scroll'" in page.script


def test_gemini_keeps_safari_render_active_in_background() -> None:
    class Page:
        def __init__(self) -> None:
            self.background_render_calls = 0
            self.background_calls = 0
            self.waits: list[int] = []

        def keep_rendering_in_background(self) -> None:
            self.background_render_calls += 1

        def keep_background(self) -> None:
            self.background_calls += 1

        def wait_for_timeout(self, milliseconds: int) -> None:
            self.waits.append(milliseconds)

    page = Page()
    _prepare_gemini_page_for_rendering(page)

    assert page.background_render_calls == 1
    assert page.background_calls == 0
    assert page.waits == [1_000]


def test_gemini_bot_check_pauses_until_user_clears_google_verification() -> None:
    class Page:
        def __init__(self) -> None:
            self.checks = [
                {"detected": True, "reason": "captcha"},
                {"detected": False},
            ]
            self.front_calls = 0
            self.waits = 0

        def evaluate(self, _script, _markers):
            return self.checks.pop(0)

        def bring_to_front(self):
            self.front_calls += 1

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 1_000
            self.waits += 1

    page = Page()
    state = TaskState("v-test", snapshot_factory=lambda version: TaskSnapshot(version=version))

    assert wait_for_gemini_bot_check_clear(page, state, lambda: False, "collecting")

    snapshot = state.snapshot()
    assert page.front_calls == 1
    assert page.waits == 1
    assert snapshot["phase"] == "collecting"
    assert any("human verification" in event for event in snapshot["recent_events"])
    assert any("verification cleared" in event for event in snapshot["recent_events"])


def test_gemini_bot_check_inspection_uses_page_markers_and_challenge_selectors() -> None:
    class Page:
        def evaluate(self, script, markers):
            assert "challengeElement" in script
            assert "unusual traffic" in markers
            assert "[data-sitekey], [id*=" in script
            assert " + '[class*=\"captcha\"]" in script
            return {"detected": True, "reason": "challenge element"}

    assert inspect_gemini_bot_check(Page())["detected"]


def test_gemini_collection_keeps_virtualized_loading_after_a_scroll_without_pixel_motion() -> None:
    class Page:
        def __init__(self) -> None:
            self.scroll_round = 0

        def goto(self, *_args, **_kwargs):
            return None

        def evaluate(self, script, *_args):
            if "dispatchEvent(new Event('scroll'" in script:
                self.scroll_round += 1
                return {"moved": False, "eventDispatched": True}
            if "document.querySelectorAll('a[href]')" in script:
                count = min(2 + max(0, self.scroll_round - 1) * 2, 6)
                return [
                    {
                        "conversationId": f"conversation-{index}",
                        "url": f"https://gemini.google.com/app/conversation-{index}",
                        "title": f"Conversation {index}",
                    }
                    for index in range(count)
                ]
            raise AssertionError(f"Unexpected page script: {script[:80]}")

        def wait_for_timeout(self, _milliseconds):
            return None

    page = Page()
    config = CrawlConfig(
        gemini_max_conversations=6,
        max_scroll_rounds=5,
        gemini_stale_round_limit=1,
        gemini_scroll_pause_seconds=0.1,
    )
    with patch("app.core.gemini_downloader.inspect_gemini_bot_check", return_value={"detected": False}), patch(
        "app.core.gemini_downloader._wait_for_gemini_ready"
    ), patch("app.core.gemini_downloader._open_gemini_sidebar"), patch(
        "app.core.gemini_downloader._prepare_gemini_page_for_rendering"
    ):
        links = collect_gemini_conversation_links(page, config, lambda: False)

    assert len(links) == 6


def test_gemini_ready_gate_rejects_an_authenticated_region_unavailable_page() -> None:
    class Page:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            raise AssertionError("Region unavailability must fail without another readiness wait.")

    with patch(
        "app.core.gemini_downloader.inspect_gemini_session",
        return_value={
            "href": "https://gemini.google.com/",
            "accountLabel": "Google Account: Demo",
            "conversationLinks": 0,
            "hasComposer": False,
            "signedOut": False,
            "unsupportedRegion": True,
        },
    ):
        with pytest.raises(RuntimeError, match="not available.*current region"):
            _wait_for_gemini_ready(Page(), timeout_seconds=1)


def test_gemini_ready_gate_tolerates_one_signed_out_hydration_frame() -> None:
    waits: list[int] = []

    class Page:
        def wait_for_timeout(self, milliseconds: int) -> None:
            waits.append(milliseconds)

    with patch(
        "app.core.gemini_downloader.inspect_gemini_session",
        side_effect=(
            {
                "href": "https://gemini.google.com/app",
                "accountLabel": "",
                "conversationLinks": 0,
                "hasComposer": True,
                "signedOut": True,
                "unsupportedRegion": False,
            },
            {
                "href": "https://gemini.google.com/app",
                "accountLabel": "Google Account: Demo",
                "conversationLinks": 1,
                "hasComposer": True,
                "signedOut": False,
                "unsupportedRegion": False,
            },
        ),
    ):
        snapshot = _wait_for_gemini_ready(Page(), timeout_seconds=1)

    assert snapshot["accountLabel"] == "Google Account: Demo"
    assert waits == [500]


def test_gemini_ready_gate_requires_two_signed_out_frames() -> None:
    waits: list[int] = []

    class Page:
        def wait_for_timeout(self, milliseconds: int) -> None:
            waits.append(milliseconds)

    signed_out_snapshot = {
        "href": "https://gemini.google.com/app",
        "accountLabel": "",
        "conversationLinks": 1,
        "hasComposer": True,
        "signedOut": True,
        "unsupportedRegion": False,
    }
    with patch(
        "app.core.gemini_downloader.inspect_gemini_session",
        side_effect=(signed_out_snapshot, signed_out_snapshot),
    ):
        with pytest.raises(RuntimeError, match="not signed in to Gemini"):
            _wait_for_gemini_ready(Page(), timeout_seconds=1)

    assert waits == [500]
