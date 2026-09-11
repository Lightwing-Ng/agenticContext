"""Pure-fixture coverage for the shared Zhihu answer collector.

Code version: v1.0.0-codex.1
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.zhihu_answers import (
    ZHIHU_ANSWER_PAGE_SIZE,
    ZhihuArchiveError,
    ZhihuProfile,
    ZhihuVerificationRequiredError,
    _browser_fetch_json,
    collect_zhihu_answers,
    normalize_zhihu_answer_payload,
    normalize_zhihu_profile_url,
)


ZHIHU_FIXTURE_PROFILE_URL = "https://www.zhihu.com/people/feifeimao/answers?page=2"


def _answer_payload(
    answer_id: int,
    *,
    author_token: str = "feifeimao",
    content: str | None = None,
) -> dict[str, Any]:
    rendered_content = content if content is not None else f"<p>Answer {answer_id}</p>"
    return {
        "id": answer_id,
        "url": f"https://www.zhihu.com/api/v4/answers/{answer_id}",
        "excerpt": f"Answer {answer_id}",
        "content": rendered_content,
        "created_time": 1_700_000_000 + answer_id,
        "updated_time": 1_710_000_000 + answer_id,
        "voteup_count": answer_id,
        "comment_count": answer_id % 7,
        "author": {
            "id": f"author-{author_token}",
            "name": "Fixture Author",
            "url_token": author_token,
        },
        "question": {
            "id": 100_000 + answer_id,
            "title": f"Question {answer_id}",
            "url": f"https://www.zhihu.com/api/v4/questions/{100_000 + answer_id}",
        },
    }


def _page(
    answer_ids: range | list[int] | tuple[int, ...],
    *,
    total: int | None,
    is_end: bool,
    next_offset: int | None = None,
    content_for: Callable[[int], str | None] | None = None,
) -> dict[str, Any]:
    content_for = content_for or (lambda _answer_id: None)
    paging: dict[str, Any] = {"is_end": is_end}
    if total is not None:
        paging["totals"] = total
    if not is_end and next_offset is not None:
        paging["next"] = (
            "http://www.zhihu.com/api/v4/members/feifeimao/answers"
            f"?offset={next_offset}&limit={ZHIHU_ANSWER_PAGE_SIZE}&sort_by=created"
        )
    return {
        "data": [
            _answer_payload(answer_id, content=content_for(answer_id))
            for answer_id in answer_ids
        ],
        "paging": paging,
    }


def _offset(url: str) -> int:
    return int(parse_qs(urlsplit(url).query)["offset"][0])


def test_profile_url_normalization_accepts_only_the_zhihu_people_boundary() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_FIXTURE_PROFILE_URL)

    assert profile == ZhihuProfile(
        author_token="feifeimao",
        profile_url="https://www.zhihu.com/people/feifeimao",
        answers_url="https://www.zhihu.com/people/feifeimao/answers",
    )
    assert normalize_zhihu_profile_url("https://zhihu.com/people/feifeimao") == profile


@pytest.mark.parametrize(
    "candidate",
    (
        "",
        "http://www.zhihu.com/people/feifeimao/answers",
        "https://www.zhihu.com.evil.test/people/feifeimao/answers",
        "https://www.zhihu.com@evil.test/people/feifeimao/answers",
        "https://evil.test@www.zhihu.com/people/feifeimao/answers",
        "https://www.zhihu.com:443/people/feifeimao/answers",
        "https://www.zhihu.com/question/123",
        "https://www.zhihu.com/people/feifeimao/posts",
        "https://www.zhihu.com/people/%2e%2e/answers",
        "https://www.zhihu.com/people/fei%2Ffeimao/answers",
    ),
)
def test_profile_url_normalization_rejects_ssrf_and_path_confusion(candidate: str) -> None:
    with pytest.raises(ValueError, match="Zhihu|https"):
        normalize_zhihu_profile_url(candidate)


@pytest.mark.parametrize(
    "url",
    (
        "https://evil.test/api/v4/members/feifeimao/answers?sort_by=created",
        "https://www.zhihu.com.evil.test/api/v4/members/feifeimao/answers?sort_by=created",
        "https://www.zhihu.com/question/1?sort_by=created",
        "https://www.zhihu.com/api/v4/members/feifeimao/answers?sort_by=updated",
    ),
)
def test_browser_fetch_rejects_noncanonical_api_urls_before_evaluation(url: str) -> None:
    class NoEvaluationPage:
        def evaluate(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("Unsafe URL reached the browser")

    with pytest.raises(ValueError, match="refused"):
        _browser_fetch_json(NoEvaluationPage(), url)


def test_collects_all_answers_through_the_short_terminal_page() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_FIXTURE_PROFILE_URL)
    requested_offsets: list[int] = []

    def fetch_page(url: str) -> dict[str, Any]:
        offset = _offset(url)
        requested_offsets.append(offset)
        answer_ids = list(range(offset + 1, min(offset + ZHIHU_ANSWER_PAGE_SIZE, 333) + 1))
        is_end = offset + len(answer_ids) >= 333
        return _page(
            answer_ids,
            total=333,
            is_end=is_end,
            next_offset=None if is_end else offset + ZHIHU_ANSWER_PAGE_SIZE,
        )

    result = collect_zhihu_answers(profile, fetch_page, lambda: False)

    assert len(result.answers) == 333
    assert result.expected_total == 333
    assert result.pages_processed == 17
    assert result.raw_answers == 333
    assert result.duplicates == 0
    assert requested_offsets == [*range(0, 333, ZHIHU_ANSWER_PAGE_SIZE), 0]


def test_collect_deduplicates_overlapping_pages_by_answer_id() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_FIXTURE_PROFILE_URL)

    def fetch_page(url: str) -> dict[str, Any]:
        if _offset(url) == 0:
            return _page([1, 2], total=3, is_end=False, next_offset=20)
        return _page([2, 3], total=3, is_end=True)

    result = collect_zhihu_answers(profile, fetch_page, lambda: False)

    assert [answer.answer_id for answer in result.answers] == ["1", "2", "3"]
    assert result.raw_answers == 4
    assert result.duplicates == 1


def test_collect_accepts_only_a_reproducible_duplicate_backed_provider_gap() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_FIXTURE_PROFILE_URL)
    requested_offsets: list[int] = []

    def fetch_page(url: str) -> dict[str, Any]:
        offset = _offset(url)
        requested_offsets.append(offset)
        if offset == 0:
            return _page([1, 2], total=3, is_end=False, next_offset=20)
        return _page([2], total=3, is_end=True)

    result = collect_zhihu_answers(profile, fetch_page, lambda: False)

    assert [answer.answer_id for answer in result.answers] == ["1", "2"]
    assert result.expected_total == 3
    assert result.raw_answers == 3
    assert result.duplicates == 1
    assert requested_offsets == [0, 20, 0, 0, 20]


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (_page([1, 2], total=3, is_end=True), "reported 3 answers"),
        (_page([1], total=2, is_end=False), "omitted the next-page cursor"),
        (_page([1], total=None, is_end=True), "every page"),
    ),
)
def test_collect_rejects_incomplete_pagination(payload: dict[str, Any], message: str) -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_FIXTURE_PROFILE_URL)

    with pytest.raises(ZhihuArchiveError, match=message):
        collect_zhihu_answers(profile, lambda _url: payload, lambda: False)


def test_collect_fails_closed_on_verification_payload() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_FIXTURE_PROFILE_URL)
    payload = {
        "error": {
            "need_login": True,
            "code": 40352,
            "message": "Human verification required",
        }
    }

    with pytest.raises(ZhihuVerificationRequiredError, match="verification manually"):
        collect_zhihu_answers(profile, lambda _url: payload, lambda: False)


def test_normalizer_retains_provider_unavailable_body_as_a_source_record() -> None:
    payload = _answer_payload(2_225_904_820, content="")
    payload.update(
        {
            "excerpt": "This answer remains available from its source link.",
            "is_collapsed": False,
        }
    )
    profile = normalize_zhihu_profile_url(ZHIHU_FIXTURE_PROFILE_URL)
    answer = normalize_zhihu_answer_payload(
        payload,
        profile,
        allow_unavailable_content=True,
    )

    assert answer.content_available is False
    assert answer.content_text == ""
    assert answer.excerpt.startswith("This answer remains available")


def test_normalizer_rejects_an_unmarked_empty_answer() -> None:
    payload = _answer_payload(11, content="")
    payload.update({"excerpt": "", "is_collapsed": False})
    profile = normalize_zhihu_profile_url(ZHIHU_FIXTURE_PROFILE_URL)

    with pytest.raises(ZhihuArchiveError, match="does not expose complete cacheable content"):
        normalize_zhihu_answer_payload(payload, profile)
