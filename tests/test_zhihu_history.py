"""Fixture-only coverage for the formal Zhihu text cache.

Code version: v1.1.0-codex.1
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.chat_history_browser import query_chat_history
from app.core.config import CrawlConfig
from app.core.zhihu_answers import (
    ZhihuArchiveError,
    normalize_zhihu_profile_url,
)
from app.core.zhihu_history import (
    ZhihuHistoryStore,
    build_zhihu_history_initial_snapshot,
    collect_zhihu_liked_answers,
    parse_zhihu_account,
    sync_zhihu_history,
    zhihu_history_path,
)


class RecordingState:
    """Capture task state without opening the application cache root."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.events: list[str] = []

    def update(self, **values: Any) -> None:
        self.values.update(values)

    def append_event(self, message: str) -> None:
        self.events.append(message)


@pytest.fixture(autouse=True)
def forbid_live_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail before a fixture test can launch a browser."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Zhihu history fixture tests must not launch a browser")

    monkeypatch.setattr("app.core.zhihu_history.sync_playwright_or_error", forbidden)
    monkeypatch.setattr("app.core.zhihu_history.launch_chromium_context", forbidden)
    monkeypatch.setattr("app.core.zhihu_history.goto_with_retry", forbidden)


def _account_payload() -> dict[str, object]:
    return {
        "id": "verified-account-id",
        "url_token": "MayukoSF",
        "name": "Fixture Account",
    }


def _answer_payload(
    answer_id: int,
    *,
    author_token: str = "fixture-author",
    content: str | None = None,
) -> dict[str, object]:
    return {
        "id": answer_id,
        "type": "answer",
        "excerpt": f"Answer {answer_id}",
        "content": content or f"<p>Answer {answer_id}</p>",
        "created_time": 1_700_000_000 + answer_id,
        "updated_time": 1_710_000_000 + answer_id,
        "voteup_count": answer_id,
        "comment_count": answer_id % 5,
        "author": {
            "id": f"author-{author_token}",
            "name": "Fixture Author",
            "url_token": author_token,
        },
        "question": {
            "id": 100_000 + answer_id,
            "title": f"Question {answer_id}",
        },
    }


def _activity_page(
    activities: list[dict[str, object]],
    *,
    is_end: bool,
    page_number: int = 0,
) -> dict[str, object]:
    paging: dict[str, object] = {"is_end": is_end}
    if not is_end:
        paging["next"] = (
            "https://www.zhihu.com/api/v3/moments/MayukoSF/activities"
            f"?offset={1_788_364_960_964 - page_number}&page_num={page_number + 1}"
        )
    return {"data": activities, "paging": paging}


def _upvote(activity_id: int, answer_id: int) -> dict[str, object]:
    return {
        "id": activity_id,
        "verb": "MEMBER_VOTEUP_ANSWER",
        "target": _answer_payload(answer_id),
    }


def test_account_identity_comes_from_the_authenticated_browser() -> None:
    account = parse_zhihu_account(_account_payload())

    assert account.profile.author_token == "MayukoSF"
    assert account.profile.profile_url == "https://www.zhihu.com/people/MayukoSF"
    assert account.display_name == "Fixture Account"


def test_liked_answer_collection_filters_other_activity_and_deduplicates() -> None:
    profile = normalize_zhihu_profile_url("https://www.zhihu.com/people/MayukoSF")
    first_page = _activity_page(
        [
            _upvote(10, 1),
            {"id": 11, "verb": "MEMBER_CREATE_ARTICLE", "target": {"type": "article"}},
        ],
        is_end=False,
    )
    second_page = _activity_page(
        [_upvote(12, 1), _upvote(13, 2)],
        is_end=True,
        page_number=1,
    )
    requests: list[str] = []

    def fetch_page(url: str) -> object:
        requests.append(url)
        query = parse_qs(urlsplit(url).query)
        return second_page if query.get("page_num") == ["1"] else first_page

    result = collect_zhihu_liked_answers(profile, fetch_page, lambda: False)

    assert [answer.answer_id for answer in result.answers] == ["1", "2"]
    assert result.pages_processed == 2
    assert result.raw_answers == 3
    assert result.duplicates == 1
    assert len(requests) == 3


def test_liked_answer_collection_retains_one_provider_unavailable_body() -> None:
    profile = normalize_zhihu_profile_url("https://www.zhihu.com/people/MayukoSF")
    unavailable = _answer_payload(2_686_710_792)
    unavailable.update(
        {
            "content": "",
            "excerpt": "This older upvoted answer is available from its source link.",
        }
    )
    payload = _activity_page(
        [
            {
                "id": 10,
                "verb": "MEMBER_VOTEUP_ANSWER",
                "target": unavailable,
            },
            _upvote(11, 2_686_710_793),
        ],
        is_end=True,
    )

    result = collect_zhihu_liked_answers(
        profile,
        lambda _url: payload,
        lambda: False,
    )

    assert len(result.answers) == 2
    assert result.answers[0].content_available is False
    assert result.answers[0].excerpt.startswith("This older upvoted answer")
    assert result.answers[1].content_available is True


def test_liked_answer_collection_rejects_untrusted_activity_cursor() -> None:
    profile = normalize_zhihu_profile_url("https://www.zhihu.com/people/MayukoSF")
    payload = _activity_page([_upvote(10, 1)], is_end=False)
    payload["paging"]["next"] = "https://evil.test/api/v3/moments/MayukoSF/activities?offset=1&page_num=1"

    with pytest.raises(ZhihuArchiveError, match="invalid activity cursor"):
        collect_zhihu_liked_answers(profile, lambda _url: payload, lambda: False)


def test_formal_store_keeps_text_and_links_without_provider_html(tmp_path: Path) -> None:
    profile = normalize_zhihu_profile_url("https://www.zhihu.com/people/fixture-author")
    payload = _answer_payload(
        7,
        content=(
            '<p>Readable <a href="/question/100007">rich text</a></p>'
            '<img alt="diagram" data-original="//pic1.zhimg.com/example.png">'
        ),
    )
    page = {"data": [payload], "paging": {"totals": 1, "is_end": True}}

    from app.core.zhihu_answers import collect_zhihu_answers

    answer = collect_zhihu_answers(profile, lambda _url: page, lambda: False).answers[0]
    store = ZhihuHistoryStore(zhihu_history_path(tmp_path))
    result = store.merge_answers((answer,), "2026-09-11T00:00:00Z")
    store.save()

    row = ZhihuHistoryStore(zhihu_history_path(tmp_path)).rows[0]
    assert result.cached_answers == 1
    assert row["content_text"] == "Readable rich text\n[diagram]"
    assert row["content_html"] == ""
    assert "https://www.zhihu.com/question/100007" in row["source_links"]
    assert "https://pic1.zhimg.com/example.png" in row["source_links"]
    assert row["conversation_url"].endswith("/answer/7")


def test_formal_store_keeps_collapsed_answers_visible(tmp_path: Path) -> None:
    profile = normalize_zhihu_profile_url("https://www.zhihu.com/people/fixture-author")
    payload = _answer_payload(8, content="")
    payload.update(
        {
            "content": "",
            "excerpt": "Provider excerpt for a collapsed answer.",
            "is_collapsed": True,
        }
    )

    from app.core.zhihu_answers import normalize_zhihu_answer_payload

    answer = normalize_zhihu_answer_payload(payload, profile)
    store = ZhihuHistoryStore(zhihu_history_path(tmp_path))
    store.merge_answers((answer,), "2026-09-11T00:00:00Z")
    store.save()

    page = query_chat_history(tmp_path, source="zhihu", session_view=True)
    assert page.conversation_count == 1
    assert page.items[0].content_text == "Provider excerpt for a collapsed answer."
    assert page.items[0].source_links[0].endswith("/answer/8")


def test_liked_mode_sync_is_visible_in_local_resources(tmp_path: Path) -> None:
    payload = _activity_page([_upvote(10, 2197549311)], is_end=True)

    result = sync_zhihu_history(
        RecordingState(),
        CrawlConfig(zhihu_browser="edge"),
        lambda: False,
        tmp_path,
        fetch_page=lambda _url: payload,
        account_payload=_account_payload(),
    )
    page = query_chat_history(tmp_path, source="zhihu", session_view=True)

    assert result["collection_mode"] == "liked"
    assert result["cached_answers"] == 1
    assert page.conversation_count == 1
    assert page.sessions[0].conversation_id == "2197549311"
    assert page.items[0].source == "zhihu"
    assert page.items[0].content_html == ""


def test_optional_author_mode_uses_the_answerer_url_and_merges_history(tmp_path: Path) -> None:
    payload = _answer_payload(33, author_token="wang-xian-nan-da-tian-cai")
    answer_page = {"data": [payload], "paging": {"totals": 1, "is_end": True}}

    result = sync_zhihu_history(
        RecordingState(),
        CrawlConfig(zhihu_browser="edge"),
        lambda: False,
        tmp_path,
        author_url="https://www.zhihu.com/people/wang-xian-nan-da-tian-cai",
        fetch_page=lambda _url: answer_page,
        account_payload=_account_payload(),
    )

    assert result["collection_mode"] == "author"
    assert result["processed_answers"] == 1
    assert ZhihuHistoryStore(zhihu_history_path(tmp_path)).rows[0]["author_label"] == "Fixture Author"


def test_initial_snapshot_reads_the_formal_history_count(tmp_path: Path) -> None:
    sync_zhihu_history(
        RecordingState(),
        CrawlConfig(),
        lambda: False,
        tmp_path,
        fetch_page=lambda _url: _activity_page([_upvote(10, 1)], is_end=True),
        account_payload=_account_payload(),
    )

    snapshot = build_zhihu_history_initial_snapshot("v-test", tmp_path)

    assert snapshot.downloaded_posts == 1
    assert snapshot.processed_tweets == 1
    assert snapshot.output_dir.endswith("/llm/zhihu")
