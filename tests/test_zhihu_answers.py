"""Pure-fixture coverage for the isolated Zhihu answer archive.

Code version: v0.7.0-codex.1
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pyarrow.parquet as pq
import pytest

from app.core.config import CrawlConfig
from app.core.job_lock import CacheTaskLock
from app.core.state import TaskSnapshot, TaskState
from app.core.zhihu_answers import (
    ZHIHU_ANSWER_PAGE_SIZE,
    ZHIHU_ANSWER_SCHEMA,
    ZHIHU_ANSWER_SUMMARY_COLUMNS,
    ZHIHU_EXAMPLE_PROFILE_URL,
    ZhihuAnswerStore,
    ZhihuArchiveError,
    ZhihuProfile,
    ZhihuVerificationRequiredError,
    _browser_fetch_json,
    collect_zhihu_answers,
    normalize_zhihu_profile_url,
    sync_zhihu_answers,
    zhihu_archive_path,
)
from app.core.zhihu_answers_service import ZhihuAnswersService


class RecordingState:
    """Record worker state without reading the application cache root."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.events: list[str] = []

    def update(self, **values: Any) -> None:
        self.values.update(values)

    def append_event(self, message: str) -> None:
        self.events.append(message)


@pytest.fixture(autouse=True)
def forbid_live_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make an accidental browser or provider call fail before leaving the test."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Zhihu fixture tests must not launch a browser or contact the provider")

    monkeypatch.setattr("app.core.zhihu_answers.sync_playwright_or_error", forbidden)
    monkeypatch.setattr("app.core.zhihu_answers.launch_chromium_context", forbidden)
    monkeypatch.setattr("app.core.zhihu_answers.goto_with_retry", forbidden)


def _answer_payload(
    answer_id: int,
    *,
    author_token: str = "feifeimao",
    content: str | None = None,
) -> dict[str, Any]:
    rendered_content = content or f"<p>Answer {answer_id}</p>"
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
            "private_badge": "must-not-persist",
        },
        "question": {
            "id": 100_000 + answer_id,
            "title": f"Question {answer_id}",
            "url": f"https://www.zhihu.com/api/v4/questions/{100_000 + answer_id}",
            "private_notes": "must-not-persist",
        },
        "provider_cookie": "must-not-persist",
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


def _complete_answers(*payloads: dict[str, Any]):
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    page = {"data": list(payloads), "paging": {"totals": len(payloads), "is_end": True}}
    result = collect_zhihu_answers(profile, lambda _url: page, lambda: False)
    return profile, result.answers


def test_profile_url_normalization_accepts_only_the_zhihu_people_boundary() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)

    assert profile == ZhihuProfile(
        author_token="feifeimao",
        profile_url="https://www.zhihu.com/people/feifeimao",
        answers_url="https://www.zhihu.com/people/feifeimao/answers",
    )
    assert normalize_zhihu_profile_url(
        "https://zhihu.com/people/feifeimao"
    ) == profile


def test_archive_path_is_confined_to_the_injected_beta_store(tmp_path: Path) -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)

    assert zhihu_archive_path(tmp_path, profile) == (
        tmp_path / "zhihu" / "feifeimao" / "answers.parquet"
    )


@pytest.mark.parametrize("author_token", ("", ".", "..", "author/name", "author%2Fname"))
def test_archive_path_rejects_untrusted_author_segments(
    tmp_path: Path,
    author_token: str,
) -> None:
    with pytest.raises(ValueError, match="profile name"):
        zhihu_archive_path(tmp_path, author_token)


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
        "https://www.zhihu.com/people/feifeimao/answers/extra",
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


def test_collects_all_333_answers_through_the_short_terminal_page() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    requested_offsets: list[int] = []
    progress: list[tuple[int, int, int | None, int]] = []

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

    result = collect_zhihu_answers(
        profile,
        fetch_page,
        lambda: False,
        on_progress=lambda *values: progress.append(values),
    )

    assert len(result.answers) == 333
    assert {answer.answer_id for answer in result.answers} == {str(value) for value in range(1, 334)}
    assert result.expected_total == 333
    assert result.pages_processed == 17
    assert result.raw_answers == 333
    assert result.duplicates == 0
    assert result.stopped is False
    assert requested_offsets == [*range(0, 333, ZHIHU_ANSWER_PAGE_SIZE), 0]
    assert progress[-1] == (17, 333, 333, 0)


def test_collect_rejects_a_repeated_page_instead_of_looping() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)

    def fetch_page(url: str) -> dict[str, Any]:
        return _page(
            [1, 2],
            total=4,
            is_end=False,
            next_offset=_offset(url) + ZHIHU_ANSWER_PAGE_SIZE,
        )

    with pytest.raises(ZhihuArchiveError, match="repeated an answer page"):
        collect_zhihu_answers(profile, fetch_page, lambda: False)


def test_collect_rejects_provider_total_drift_between_pages() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)

    def fetch_page(url: str) -> dict[str, Any]:
        offset = _offset(url)
        if offset == 0:
            return _page([1, 2], total=3, is_end=False, next_offset=20)
        return _page([3], total=4, is_end=True)

    with pytest.raises(ZhihuArchiveError, match="total changed"):
        collect_zhihu_answers(profile, fetch_page, lambda: False)


def test_collect_rejects_terminal_count_mismatch() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    payload = _page([1, 2], total=3, is_end=True)

    with pytest.raises(ZhihuArchiveError, match="reported 3 answers but returned only 2"):
        collect_zhihu_answers(profile, lambda _url: payload, lambda: False)


def test_collect_deduplicates_overlapping_pages_by_answer_id() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)

    def fetch_page(url: str) -> dict[str, Any]:
        offset = _offset(url)
        if offset == 0:
            return _page([1, 2], total=3, is_end=False, next_offset=20)
        return _page([2, 3], total=3, is_end=True)

    result = collect_zhihu_answers(profile, fetch_page, lambda: False)

    assert [answer.answer_id for answer in result.answers] == ["1", "2", "3"]
    assert result.raw_answers == 4
    assert result.duplicates == 1


def test_collect_uses_the_validated_next_offset_instead_of_response_length() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    requested_offsets: list[int] = []

    def fetch_page(url: str) -> dict[str, Any]:
        offset = _offset(url)
        requested_offsets.append(offset)
        if offset == 0:
            return _page(range(1, 40), total=40, is_end=False, next_offset=20)
        return _page(range(21, 41), total=40, is_end=True)

    result = collect_zhihu_answers(profile, fetch_page, lambda: False)

    assert requested_offsets == [0, 20, 0]
    assert len(result.answers) == 40
    assert result.raw_answers == 59
    assert result.duplicates == 19


def test_collect_accepts_only_a_reproducible_duplicate_backed_provider_gap() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
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


def test_collect_rejects_an_untrusted_next_cursor() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    payload = _page([1], total=2, is_end=False)
    payload["paging"]["next"] = "https://evil.test/api/v4/members/feifeimao/answers?offset=20&limit=20"

    with pytest.raises(ZhihuArchiveError, match="invalid next-page cursor"):
        collect_zhihu_answers(profile, lambda _url: payload, lambda: False)


@pytest.mark.parametrize(
    "cursor",
    (
        "https://www.zhihu.com/api/v4/members/feifeimao/answers?offset=40&limit=20&sort_by=created",
        "https://www.zhihu.com/api/v4/members/feifeimao/answers?offset=20&offset=40&limit=20&sort_by=created",
    ),
)
def test_collect_rejects_a_skipped_or_ambiguous_next_offset(cursor: str) -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    payload = _page([1], total=2, is_end=False)
    payload["paging"]["next"] = cursor

    with pytest.raises(ZhihuArchiveError, match="invalid next-page cursor"):
        collect_zhihu_answers(profile, lambda _url: payload, lambda: False)


def test_collect_rejects_a_missing_nonterminal_cursor() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    payload = _page([1], total=2, is_end=False)

    with pytest.raises(ZhihuArchiveError, match="omitted the next-page cursor"):
        collect_zhihu_answers(profile, lambda _url: payload, lambda: False)


def test_collect_requires_a_provider_total_on_every_page() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    payload = _page([1], total=None, is_end=True)

    with pytest.raises(ZhihuArchiveError, match="every page"):
        collect_zhihu_answers(profile, lambda _url: payload, lambda: False)


@pytest.mark.parametrize("invalid_total", (True, 1.9, "1"))
def test_collect_rejects_a_noninteger_provider_total(invalid_total: object) -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    payload = _page([1], total=1, is_end=True)
    payload["paging"]["totals"] = invalid_total

    with pytest.raises(ZhihuArchiveError, match="invalid answer total"):
        collect_zhihu_answers(profile, lambda _url: payload, lambda: False)


def test_collect_rejects_a_nonboolean_terminal_flag() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    payload = _page([1], total=1, is_end=True)
    payload["paging"]["is_end"] = "true"

    with pytest.raises(ZhihuArchiveError, match="terminal-page flag"):
        collect_zhihu_answers(profile, lambda _url: payload, lambda: False)


def test_collect_fails_closed_on_40352_verification_payload() -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    payload = {
        "error": {
            "need_login": True,
            "code": 40352,
            "message": "Human verification required",
        }
    }

    with pytest.raises(ZhihuVerificationRequiredError, match="verification manually"):
        collect_zhihu_answers(profile, lambda _url: payload, lambda: False)


def test_stopped_sync_does_not_fetch_or_replace_the_complete_archive(tmp_path: Path) -> None:
    profile, answers = _complete_answers(_answer_payload(1, content="<p>Previous complete answer</p>"))
    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(answers, "2026-09-10T00:00:00Z")
    before = store.path.read_bytes()
    stop_checks = 0

    def should_stop() -> bool:
        nonlocal stop_checks
        stop_checks += 1
        return stop_checks >= 2

    def fetch_page(_url: str) -> dict[str, Any]:
        return _page([2], total=2, is_end=False, next_offset=20)

    result = sync_zhihu_answers(
        RecordingState(),
        CrawlConfig(),
        profile.answers_url,
        "edge",
        should_stop,
        tmp_path,
        fetch_page=fetch_page,
    )

    assert result["stopped"] is True
    assert result["processed_answers"] == 1
    assert store.path.read_bytes() == before


def test_stop_accepted_at_commit_boundary_preserves_the_previous_archive(tmp_path: Path) -> None:
    profile, previous = _complete_answers(_answer_payload(1))
    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(previous, "2026-09-10T00:00:00Z")
    before = store.path.read_bytes()
    stop_checks = 0

    def should_stop() -> bool:
        nonlocal stop_checks
        stop_checks += 1
        return stop_checks >= 5

    result = sync_zhihu_answers(
        RecordingState(),
        CrawlConfig(),
        profile.answers_url,
        "edge",
        should_stop,
        tmp_path,
        fetch_page=lambda _url: _page([2], total=1, is_end=True),
    )

    assert result["stopped"] is True
    assert stop_checks == 5
    assert store.path.read_bytes() == before


def test_service_uses_the_injected_shared_task_lock(tmp_path: Path) -> None:
    state = TaskState(
        "v-test",
        snapshot_factory=lambda version: TaskSnapshot(version=version),
    )
    task_lock = CacheTaskLock(tmp_path / "local_store" / ".cache_task.lock")
    service = ZhihuAnswersService(
        state,
        beta_store_root=tmp_path / "beta_store",
        cache_store_root=tmp_path / "local_store",
        task_lock=task_lock,
    )
    assert task_lock.acquire("fixture-other-cache") is True
    try:
        with pytest.raises(RuntimeError, match="cache task is already running"):
            service.start(CrawlConfig(), ZHIHU_EXAMPLE_PROFILE_URL, "edge")
    finally:
        task_lock.release()


def test_service_refuses_stop_after_atomic_commit_has_started(tmp_path: Path) -> None:
    state = TaskState(
        "v-test",
        snapshot_factory=lambda version: TaskSnapshot(version=version),
    )
    service = ZhihuAnswersService(
        state,
        beta_store_root=tmp_path / "beta_store",
        cache_store_root=tmp_path / "local_store",
    )
    state.reset_for_run()
    assert service._begin_commit() is True

    assert service.request_stop() is False
    snapshot = state.snapshot()
    assert snapshot["running"] is True
    assert snapshot["phase"] == "committing"
    assert "commit has already started" in snapshot["message"]


def test_service_commit_gate_honors_an_already_accepted_stop(tmp_path: Path) -> None:
    state = TaskState(
        "v-test",
        snapshot_factory=lambda version: TaskSnapshot(version=version),
    )
    service = ZhihuAnswersService(
        state,
        beta_store_root=tmp_path / "beta_store",
        cache_store_root=tmp_path / "local_store",
    )
    state.reset_for_run()

    assert service.request_stop() is True
    assert service._begin_commit() is False
    assert state.snapshot()["phase"] == "stopping"


@pytest.mark.parametrize(
    ("beta_suffix", "cache_suffix"),
    [
        ("shared", "shared"),
        ("shared/beta", "shared"),
        ("shared", "shared/local_store"),
    ],
)
def test_service_rejects_overlapping_beta_and_local_store_roots(
    tmp_path: Path,
    beta_suffix: str,
    cache_suffix: str,
) -> None:
    state = TaskState(
        "v-test",
        snapshot_factory=lambda version: TaskSnapshot(version=version),
    )

    with pytest.raises(ValueError, match="must not overlap"):
        ZhihuAnswersService(
            state,
            beta_store_root=tmp_path / beta_suffix,
            cache_store_root=tmp_path / cache_suffix,
        )


def test_service_rejects_symlink_aliases_between_store_roots(tmp_path: Path) -> None:
    cache_root = tmp_path / "local_store"
    cache_root.mkdir()
    beta_alias = tmp_path / "beta_store"
    beta_alias.symlink_to(cache_root, target_is_directory=True)
    state = TaskState(
        "v-test",
        snapshot_factory=lambda version: TaskSnapshot(version=version),
    )

    with pytest.raises(ValueError, match="must not overlap"):
        ZhihuAnswersService(
            state,
            beta_store_root=beta_alias,
            cache_store_root=cache_root,
        )


def test_complete_store_write_is_typed_atomic_and_field_whitelisted(tmp_path: Path) -> None:
    first_payload = _answer_payload(1, content="<p>First <strong>answer</strong></p>")
    second_payload = _answer_payload(2)
    profile, first_snapshot = _complete_answers(first_payload, second_payload)
    store = ZhihuAnswerStore(tmp_path, profile)

    first_result = store.replace_complete(first_snapshot, "2026-09-10T00:00:00Z")

    assert first_result.cached_answers == 2
    assert first_result.added == 2
    assert store.path.is_file()
    table = pq.read_table(store.path)
    assert table.schema.names == ZHIHU_ANSWER_SCHEMA.names
    assert all(set(row) == set(ZHIHU_ANSWER_SCHEMA.names) for row in table.to_pylist())
    serialized_rows = json.dumps(table.to_pylist(), ensure_ascii=False, sort_keys=True)
    assert "provider_cookie" not in serialized_rows
    assert "private_badge" not in serialized_rows
    assert "private_notes" not in serialized_rows
    assert "<script" not in serialized_rows
    assert not list(store.path.parent.glob(f".{store.path.name}.*.tmp"))

    changed_payload = _answer_payload(1, content="<p>Changed answer</p>")
    added_payload = _answer_payload(3)
    _, second_snapshot = _complete_answers(changed_payload, added_payload)
    second_result = store.replace_complete(second_snapshot, "2026-09-10T01:00:00Z")

    assert second_result.cached_answers == 2
    assert second_result.added == 1
    assert second_result.changed == 1
    assert second_result.removed == 1
    assert second_result.unchanged == 0
    assert {row["answer_id"] for row in pq.read_table(store.path).to_pylist()} == {"1", "3"}


def test_status_summary_projects_metadata_without_reading_answer_bodies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, answers = _complete_answers(_answer_payload(1), _answer_payload(2))
    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(answers, "2026-09-10T00:00:00Z")
    original_read_table = pq.read_table
    projected_columns: list[tuple[str, ...]] = []

    def track_read_table(*args: Any, **kwargs: Any):
        columns = kwargs.get("columns")
        if columns is not None:
            projected_columns.append(tuple(columns))
        return original_read_table(*args, **kwargs)

    monkeypatch.setattr("app.core.zhihu_answers.pq.read_table", track_read_table)

    summary = ZhihuAnswerStore.load_summary(tmp_path, profile)

    assert summary["cache_exists"] is True
    assert summary["cached_answers"] == 2
    assert projected_columns == [ZHIHU_ANSWER_SUMMARY_COLUMNS]
    assert "content_html" not in projected_columns[0]
    assert "content_text" not in projected_columns[0]


def test_archive_browser_searches_all_rows_and_reads_one_body_by_stable_id(
    tmp_path: Path,
) -> None:
    selected_id = 21
    profile, answers = _complete_answers(
        *(
            _answer_payload(
                answer_id,
                content=(
                    "<p>The consumer is the parent, not the child.</p>"
                    if answer_id == selected_id
                    else None
                ),
            )
            for answer_id in range(1, 26)
        )
    )
    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(answers, "2026-09-10T00:00:00Z")

    second_page = store.browse(page=2, page_size=20)
    content_match = store.browse(query="consumer is the parent")
    url_match = store.browse(
        query=f"https://www.zhihu.com/question/{100_000 + selected_id}/answer/{selected_id}"
    )
    selected = store.read_answer(str(selected_id))

    assert second_page["total"] == 25
    assert second_page["page"] == 2
    assert second_page["page_count"] == 2
    assert len(second_page["items"]) == 5
    assert content_match["total"] == 1
    assert content_match["items"][0]["answer_id"] == str(selected_id)
    assert "content_text" not in content_match["items"][0]
    assert url_match["total"] == 1
    assert url_match["items"][0]["answer_id"] == str(selected_id)
    assert selected is not None
    assert selected["content_text"] == "The consumer is the parent, not the child."
    assert len(selected["content_sha256"]) == 64
    assert selected["content_sha256"] == next(
        answer.content_sha256
        for answer in answers
        if answer.answer_id == str(selected_id)
    )
    assert "content_html" not in selected


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"query": "x" * 501}, "at most 500"),
        ({"page": 0}, "positive integer"),
        ({"page_size": 101}, "between 1 and 100"),
    ],
)
def test_archive_browser_rejects_unbounded_queries(
    tmp_path: Path,
    options: dict[str, object],
    message: str,
) -> None:
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)
    store = ZhihuAnswerStore(tmp_path, profile)

    with pytest.raises(ValueError, match=message):
        store.browse(**options)

    with pytest.raises(ValueError, match="decimal digits"):
        store.read_answer("1/../../private")


def test_archive_metadata_preserves_a_verified_provider_enumeration_gap(
    tmp_path: Path,
) -> None:
    profile, answers = _complete_answers(_answer_payload(1), _answer_payload(2))
    store = ZhihuAnswerStore(tmp_path, profile)

    store.replace_complete(
        answers,
        "2026-09-10T00:00:00Z",
        provider_reported_answers=3,
        pages_processed=2,
        raw_answers=3,
        duplicates=1,
        stability_passes=2,
    )
    summary = ZhihuAnswerStore.load_summary(tmp_path, profile)

    assert summary["expected_answers"] == 3
    assert summary["cached_answers"] == 2
    assert summary["unavailable_answers"] == 1
    assert summary["pages_processed"] == 2
    assert summary["raw_answers"] == 3
    assert summary["duplicates"] == 1
    assert summary["stability_passes"] == 2


def test_idle_service_recovers_enumerated_count_from_verified_archive(
    tmp_path: Path,
) -> None:
    profile, answers = _complete_answers(_answer_payload(1), _answer_payload(2))
    beta_store_root = tmp_path / "beta_store"
    ZhihuAnswerStore(beta_store_root, profile).replace_complete(
        answers,
        "2026-09-10T00:00:00Z",
        provider_reported_answers=3,
        pages_processed=2,
        raw_answers=3,
        duplicates=1,
        stability_passes=2,
    )
    state = TaskState(
        "v-test",
        snapshot_factory=lambda version: TaskSnapshot(version=version),
    )
    service = ZhihuAnswersService(
        state,
        beta_store_root=beta_store_root,
        cache_store_root=tmp_path / "local_store",
    )

    snapshot = service.snapshot(profile.answers_url)

    assert snapshot["phase"] == "idle"
    assert snapshot["expected_answers"] == 3
    assert snapshot["processed_answers"] == 2
    assert snapshot["cached_answers"] == 2
    assert snapshot["unavailable_answers"] == 1


def test_failed_service_status_does_not_mix_partial_run_and_archive_metrics(
    tmp_path: Path,
) -> None:
    profile, answers = _complete_answers(_answer_payload(1), _answer_payload(2))
    beta_store_root = tmp_path / "beta_store"
    ZhihuAnswerStore(beta_store_root, profile).replace_complete(
        answers,
        "2026-09-10T00:00:00Z",
        provider_reported_answers=3,
        pages_processed=2,
        raw_answers=3,
        duplicates=1,
        stability_passes=2,
    )
    state = TaskState(
        "v-test",
        snapshot_factory=lambda version: TaskSnapshot(version=version),
    )
    state.reset_for_run()
    state.update(
        processed_tweets=1,
        performance_metrics={
            "expected_answers": 10,
            "pages_processed": 1,
            "duplicates": 0,
        },
    )
    state.finish_error("fixture failure")
    service = ZhihuAnswersService(
        state,
        beta_store_root=beta_store_root,
        cache_store_root=tmp_path / "local_store",
    )

    snapshot = service.snapshot(profile.answers_url)

    assert snapshot["phase"] == "failed"
    assert snapshot["expected_answers"] == 10
    assert snapshot["processed_answers"] == 1
    assert snapshot["cached_answers"] == 2
    assert snapshot["pages_processed"] == 1
    assert snapshot["duplicates"] == 0
    assert snapshot["unavailable_answers"] == 0
    assert snapshot["raw_answers"] == 0
    assert snapshot["stability_passes"] == 0


def test_service_status_reuses_summary_until_the_archive_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = TaskState(
        "v-test",
        snapshot_factory=lambda version: TaskSnapshot(version=version),
    )
    service = ZhihuAnswersService(
        state,
        beta_store_root=tmp_path / "beta_store",
        cache_store_root=tmp_path / "local_store",
    )
    calls: list[str] = []

    def fake_summary(
        cls: type[ZhihuAnswerStore],
        beta_store_root: Path | str,
        profile: ZhihuProfile,
        *,
        recent_limit: int = 6,
        expected_beta_store_root: Path | str | None = None,
    ) -> dict[str, Any]:
        calls.append(profile.author_token)
        return {
            "cache_exists": False,
            "cached_answers": 0,
            "author_name": "",
            "output_dir": str(Path(beta_store_root) / "zhihu" / profile.author_token),
            "recent_answers": [],
        }

    monkeypatch.setattr(ZhihuAnswerStore, "load_summary", classmethod(fake_summary))

    service.snapshot()
    service.snapshot()
    archive_path = zhihu_archive_path(
        tmp_path / "beta_store",
        normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL),
    )
    archive_path.parent.mkdir(parents=True)
    archive_path.write_bytes(b"new archive identity")
    service.snapshot()
    service.snapshot()

    assert calls == ["feifeimao", "feifeimao"]


def test_store_rejects_a_profile_directory_symlink_escape(tmp_path: Path) -> None:
    beta_root = tmp_path / "beta_store"
    local_root = tmp_path / "local_store"
    escaped_directory = local_root / "escaped"
    escaped_directory.mkdir(parents=True)
    zhihu_directory = beta_root / "zhihu"
    zhihu_directory.mkdir(parents=True)
    (zhihu_directory / "feifeimao").symlink_to(
        escaped_directory,
        target_is_directory=True,
    )
    profile = normalize_zhihu_profile_url(ZHIHU_EXAMPLE_PROFILE_URL)

    with pytest.raises(ZhihuArchiveError, match="symlinked archive path"):
        ZhihuAnswerStore(beta_root, profile)

    assert not (escaped_directory / "answers.parquet").exists()


def test_service_start_rejects_a_symlink_escape_without_leaking_the_task_lock(
    tmp_path: Path,
) -> None:
    beta_root = tmp_path / "beta_store"
    local_root = tmp_path / "local_store"
    escaped_directory = local_root / "escaped"
    escaped_directory.mkdir(parents=True)
    zhihu_directory = beta_root / "zhihu"
    zhihu_directory.mkdir(parents=True)
    (zhihu_directory / "feifeimao").symlink_to(
        escaped_directory,
        target_is_directory=True,
    )
    task_lock = CacheTaskLock(local_root / ".cache_task.lock")
    state = TaskState(
        "v-test",
        snapshot_factory=lambda version: TaskSnapshot(version=version),
    )
    service = ZhihuAnswersService(
        state,
        beta_store_root=beta_root,
        cache_store_root=local_root,
        task_lock=task_lock,
    )

    with pytest.raises(ZhihuArchiveError, match="symlinked archive path"):
        service.start(CrawlConfig(), ZHIHU_EXAMPLE_PROFILE_URL, "edge")

    assert task_lock.acquire("fixture-after-rejection") is True
    task_lock.release()


def test_service_rejects_beta_root_rebound_into_local_store_without_leaking_lock(
    tmp_path: Path,
) -> None:
    beta_root = tmp_path / "beta_store"
    local_root = tmp_path / "local_store"
    local_root.mkdir()
    task_lock = CacheTaskLock(local_root / ".cache_task.lock")
    state = TaskState(
        "v-test",
        snapshot_factory=lambda version: TaskSnapshot(version=version),
    )
    service = ZhihuAnswersService(
        state,
        beta_store_root=beta_root,
        cache_store_root=local_root,
        task_lock=task_lock,
    )
    beta_root.symlink_to(local_root, target_is_directory=True)

    with pytest.raises(ZhihuArchiveError, match="roots changed"):
        service.start(CrawlConfig(), ZHIHU_EXAMPLE_PROFILE_URL, "edge")

    assert not (local_root / "zhihu").exists()
    assert task_lock.acquire("fixture-after-rebound-rejection") is True
    task_lock.release()


def test_store_rejects_beta_root_rebound_before_archive_publication(tmp_path: Path) -> None:
    beta_root = tmp_path / "beta_store"
    local_root = tmp_path / "local_store"
    beta_root.mkdir()
    local_root.mkdir()
    profile, answers = _complete_answers(_answer_payload(1))
    store = ZhihuAnswerStore(beta_root, profile)
    beta_root.rmdir()
    beta_root.symlink_to(local_root, target_is_directory=True)

    with pytest.raises(ZhihuArchiveError, match="symlinked Beta store root"):
        store.replace_complete(answers, "2026-09-10T00:00:00Z")

    assert not (local_root / "zhihu").exists()


def test_provider_exposed_long_content_flags_and_media_metadata_are_preserved(
    tmp_path: Path,
) -> None:
    visible_text = "x" * 1_139
    payload = _answer_payload(
        7,
        content=(
            f"<p>{visible_text}</p>"
            '<img src="https://pic1.zhimg.com/v2-fixture.jpg">'
            '<img src="http://pic2.zhimg.com/not-https.jpg">'
            '<img src="https://evil.test/not-provider.jpg">'
        ),
    )
    payload.update(
        {
            "content_need_truncated": True,
            "force_login_when_click_read_more": False,
            "is_collapsed": False,
            "is_normal": True,
            "collapse_reason": "",
        }
    )
    profile, answers = _complete_answers(payload)

    assert answers[0].content_text == visible_text
    assert answers[0].content_available is True
    assert answers[0].content_need_truncated is True
    assert answers[0].force_login_when_click_read_more is False
    assert answers[0].is_collapsed is False
    assert answers[0].is_normal is True
    assert answers[0].collapse_reason == ""
    assert answers[0].media_urls == ("https://pic1.zhimg.com/v2-fixture.jpg",)

    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(answers, "2026-09-10T00:00:00Z")
    row = pq.read_table(store.path).to_pylist()[0]

    assert row["content_need_truncated"] is True
    assert row["content_available"] is True
    assert row["force_login_when_click_read_more"] is False
    assert row["is_collapsed"] is False
    assert row["is_normal"] is True
    assert row["collapse_reason"] == ""
    assert row["media_urls"] == ["https://pic1.zhimg.com/v2-fixture.jpg"]
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == [store.path]


def test_atomic_parquet_failure_preserves_the_previous_complete_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, previous = _complete_answers(_answer_payload(1))
    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(previous, "2026-09-10T00:00:00Z")
    before = store.path.read_bytes()
    _, replacement = _complete_answers(_answer_payload(2))
    original_write_table = pq.write_table

    def write_then_fail(*args: Any, **kwargs: Any) -> None:
        original_write_table(*args, **kwargs)
        raise OSError("fixture write failure after temporary output")

    monkeypatch.setattr("app.core.resource_persistence.pq.write_table", write_then_fail)
    with pytest.raises(OSError, match="fixture write failure"):
        store.replace_complete(replacement, "2026-09-10T01:00:00Z")

    assert store.path.read_bytes() == before
    assert not list(store.path.parent.glob(f".{store.path.name}.*.tmp"))


def test_semantic_candidate_readback_failure_preserves_the_previous_complete_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, previous = _complete_answers(_answer_payload(1))
    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(previous, "2026-09-10T00:00:00Z")
    before = store.path.read_bytes()
    _, replacement = _complete_answers(_answer_payload(2))
    from app.core import zhihu_answers as module

    original_read = module.read_parquet_rows

    def fail_candidate_readback(path: Path):
        if path.suffix == ".candidate":
            return None
        return original_read(path)

    monkeypatch.setattr(module, "read_parquet_rows", fail_candidate_readback)
    with pytest.raises(RuntimeError, match="readback failed"):
        store.replace_complete(replacement, "2026-09-10T01:00:00Z")

    assert store.path.read_bytes() == before
    assert not list(store.path.parent.glob(f".{store.path.name}.*.candidate"))


def test_candidate_metadata_readback_failure_preserves_the_previous_complete_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, previous = _complete_answers(_answer_payload(1))
    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(
        previous,
        "2026-09-10T00:00:00Z",
        provider_reported_answers=2,
        pages_processed=1,
        raw_answers=2,
        duplicates=1,
        stability_passes=2,
    )
    before = store.path.read_bytes()
    _, replacement = _complete_answers(_answer_payload(2))
    from app.core import zhihu_answers as module

    original_write = module.write_parquet_rows_atomic

    def write_without_capture_metadata(
        path: Path,
        rows: Any,
        schema: Any,
    ) -> None:
        original_write(path, rows, schema.remove_metadata())

    monkeypatch.setattr(module, "write_parquet_rows_atomic", write_without_capture_metadata)
    with pytest.raises(RuntimeError, match="capture metadata readback failed"):
        store.replace_complete(replacement, "2026-09-10T01:00:00Z")

    assert store.path.read_bytes() == before
    summary = ZhihuAnswerStore.load_summary(tmp_path, profile)
    assert summary["expected_answers"] == 2
    assert summary["cached_answers"] == 1
    assert summary["unavailable_answers"] == 1
    assert summary["stability_passes"] == 2
    assert not list(store.path.parent.glob(f".{store.path.name}.*.candidate"))


def test_collection_failure_preserves_the_previous_complete_file(tmp_path: Path) -> None:
    profile, previous = _complete_answers(_answer_payload(1))
    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(previous, "2026-09-10T00:00:00Z")
    before = store.path.read_bytes()

    def fetch_page(url: str) -> dict[str, Any]:
        if _offset(url) == 0:
            return _page([2], total=2, is_end=False, next_offset=20)
        raise ZhihuArchiveError("fixture second-page failure")

    with pytest.raises(ZhihuArchiveError, match="second-page failure"):
        sync_zhihu_answers(
            RecordingState(),
            CrawlConfig(),
            profile.answers_url,
            "edge",
            lambda: False,
            tmp_path,
            fetch_page=fetch_page,
        )

    assert store.path.read_bytes() == before


def test_content_digest_is_derived_from_whitelisted_answer_fields(tmp_path: Path) -> None:
    payload = _answer_payload(9, content="<p>Visible answer</p>")
    profile, answers = _complete_answers(payload)
    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(answers, "2026-09-10T00:00:00Z")
    row = pq.read_table(store.path).to_pylist()[0]
    expected_digest = hashlib.sha256(
        json.dumps(
            {
                "content_html": "<p>Visible answer</p>",
                "content_text": "Visible answer",
                "content_available": True,
                "question_title": "Question 9",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert row["content_sha256"] == expected_digest


def test_explicitly_collapsed_answer_is_cached_without_fabricating_body_text(
    tmp_path: Path,
) -> None:
    payload = _answer_payload(73_547_499)
    payload.update(
        {
            "content": "",
            "excerpt": "Collapsed answer",
            "is_collapsed": True,
            "content_need_truncated": False,
            "force_login_when_click_read_more": False,
        }
    )
    payload.pop("is_normal", None)
    payload.pop("collapse_reason", None)
    payload.pop("voteup_count", None)
    profile, answers = _complete_answers(payload)

    assert answers[0].content_available is False
    assert answers[0].content_html == ""
    assert answers[0].content_text == ""
    assert answers[0].excerpt == "Collapsed answer"
    assert answers[0].is_collapsed is True
    assert answers[0].is_normal is None
    assert answers[0].collapse_reason is None
    assert answers[0].voteup_count is None

    store = ZhihuAnswerStore(tmp_path, profile)
    store.replace_complete(answers, "2026-09-10T00:00:00Z")
    row = pq.read_table(store.path).to_pylist()[0]

    assert row["content_available"] is False
    assert row["content_html"] == ""
    assert row["content_text"] == ""
    assert row["is_collapsed"] is True
    assert row["is_normal"] is None
    assert row["collapse_reason"] is None
    assert row["voteup_count"] is None


def test_unmarked_empty_answer_still_rejects_the_complete_snapshot() -> None:
    payload = _answer_payload(11)
    payload.update({"content": "", "excerpt": "", "is_collapsed": False})

    with pytest.raises(ZhihuArchiveError, match="does not expose complete cacheable content"):
        _complete_answers(payload)
