"""Compact sender runs preserve source identity, boundaries, and raw message data.

Code version: v1.2.0-codex.0
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.wechat_history_browser import _mark_sender_runs


START = datetime(2026, 10, 30, 12, tzinfo=timezone(timedelta(hours=8)))


def message(index, *, sender="wxid_peer", seconds=None, **context):
    time = START + timedelta(seconds=index if seconds is None else seconds)
    return SimpleNamespace(
        stable_id=str(index),
        conversation_id="same-conversation",
        author_label="Same display name",
        content_text=f"Original message {index}",
        last_seen_at=time.isoformat(),
        messaging={
            "account_id": "wxid_owner",
            "sender_id": sender,
            "sender_identifier_namespace": "native_user_name",
            "direction": "received",
            "provenance": {"unix_timestamp": int(time.timestamp())},
            **context,
        },
    )


def visible(items):
    return [item.messaging["show_sender_name"] for item in items]


def test_only_first_sender_is_visible_without_merging_or_rewriting_messages():
    items = [message(index) for index in range(4)] + [
        message(4, sender="wxid_owner", direction="sent")
    ]
    original = deepcopy(items)
    _mark_sender_runs(items)
    assert visible(items) == [True, False, False, False, True]
    assert len(items) == len(original)
    for actual, before in zip(items, original, strict=True):
        assert {
            key: value for key, value in vars(actual).items() if key != "messaging"
        } == {key: value for key, value in vars(before).items() if key != "messaging"}
        assert {
            key: value
            for key, value in actual.messaging.items()
            if key != "show_sender_name"
        } == before.messaging


@pytest.mark.parametrize(
    "changes",
    [
        {"sender_id": "wxid_different_peer"},
        {"sender_id": ""},
        {"account_id": "wxid_other_owner"},
        {"direction": "sent"},
        {"direction": "unknown"},
        {"sender_identifier_namespace": "unverified_export_field"},
        {"is_system_notice": True},
        {"display_text": ""},
        {"provenance": {"unix_timestamp": None}},
        {"provenance": None},
        {"provenance": {"unix_timestamp": True}},
    ],
)
def test_matching_labels_do_not_override_identity_or_source_boundaries(changes):
    items = [message(0), message(1, **changes)]
    _mark_sender_runs(items)
    assert visible(items) == [True, True]


@pytest.mark.parametrize(
    "gap,expected",
    [(0, False), (300, False), (301, False), (3 * 86400 + 429, False), (-1, True)],
)
def test_chronological_adjacency_is_independent_of_elapsed_time(gap, expected):
    items = [message(0), message(1, seconds=gap)]
    _mark_sender_runs(items)
    assert visible(items) == [True, expected]


def test_system_notifications_and_pdf_suffixes_interrupt_runs():
    items = [message(0), message(1, is_system_notice=True), message(2)]
    _mark_sender_runs(items)
    assert visible(items) == [True, True, True]
    items = [
        message(0),
        message(1, system_notices=[{"text": "A system notice"}]),
        message(2),
    ]
    _mark_sender_runs(items)
    assert visible(items) == [True, False, True]


def test_search_and_page_edges_retain_an_identifying_sender():
    items = [message(index) for index in range(4)]
    _mark_sender_runs(items[:3])
    assert visible(items[:3]) == [True, False, False]
    _mark_sender_runs(items, filtered=True)
    assert visible(items) == [True] * 4


@pytest.mark.parametrize("display_time", ["", "invalid", "2026-10-30T12:00:00"])
def test_unknown_or_unqualified_display_clocks_are_not_grouped(display_time):
    items = [message(0), message(1)]
    items[1].last_seen_at = display_time
    _mark_sender_runs(items)
    assert visible(items) == [True, True]


def test_conversation_changes_break_runs():
    items = [message(0), message(1)]
    items[1].conversation_id = "another-conversation"
    _mark_sender_runs(items)
    assert visible(items) == [True, True]


def test_calendar_day_changes_do_not_break_adjacent_sender_runs():
    items = [message(0), message(1)]
    first = START.replace(hour=23, minute=59, second=59)
    for item, time in zip(items, (first, first + timedelta(seconds=1)), strict=True):
        item.conversation_id = "same-conversation"
        item.last_seen_at = time.isoformat()
        item.messaging["provenance"]["unix_timestamp"] = int(time.timestamp())
    _mark_sender_runs(items)
    assert visible(items) == [True, False]
