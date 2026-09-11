"""Authenticated Zhihu answer history for the formal text cache.

Code version: v1.1.0-codex.1
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .browser_sessions import (
    browser_descriptors,
    goto_with_retry,
    launch_chromium_context,
    sync_playwright_or_error,
)
from .config import LOCAL_STORE_ROOT, CrawlConfig
from .resource_persistence import (
    ZHIHU_HISTORY_FILENAME,
    ZHIHU_HISTORY_SCHEMA,
    ZHIHU_HISTORY_SCHEMA_VERSION,
    read_parquet_rows,
    write_parquet_rows_atomic,
)
from .state import TaskSnapshot, TaskState
from .zhihu_answers import (
    ZHIHU_ANSWER_LIMIT,
    ZHIHU_ARCHIVE_CONTENT_LIMIT,
    ZHIHU_HOME_URL,
    ZHIHU_ME_URL,
    ZHIHU_PAGE_SETTLE_MS,
    ZHIHU_PROFILE_HOSTS,
    ZhihuAnswer,
    ZhihuArchiveError,
    ZhihuCollectionResult,
    ZhihuProfile,
    ZhihuVerificationRequiredError,
    _browser_fetch_json,
    extract_zhihu_resource_links,
    normalize_zhihu_answer_payload,
    normalize_zhihu_profile_url,
    utc_now_iso,
)


ZHIHU_ACTIVITY_PAGE_LIMIT = 20_000
ZHIHU_ACTIVITY_INITIAL_PAGE_SIZE = 5
ZHIHU_ACTIVITY_VERB = "MEMBER_VOTEUP_ANSWER"
ZHIHU_ACTIVITY_PATH_TEMPLATE = "/api/v3/moments/{author_token}/activities"
_ZHIHU_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


@dataclass(frozen=True, slots=True)
class ZhihuAccount:
    """A verified account returned by Zhihu's authenticated identity endpoint."""

    profile: ZhihuProfile
    display_name: str


@dataclass(frozen=True, slots=True)
class ZhihuHistoryWriteResult:
    """Counts from one atomically persisted text-history merge."""

    cached_answers: int
    added: int
    changed: int
    unchanged: int


def zhihu_history_path(local_store_root: Path | str = LOCAL_STORE_ROOT) -> Path:
    """Return the formal Local resources text-history path for Zhihu."""

    return Path(local_store_root).expanduser() / "llm" / "zhihu" / ZHIHU_HISTORY_FILENAME


def parse_zhihu_account(payload: object) -> ZhihuAccount:
    """Validate the minimal current-account identity required for activity reads."""

    if not isinstance(payload, Mapping):
        raise ZhihuVerificationRequiredError(
            "Zhihu did not expose an authenticated account in the selected browser."
        )
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = str(error.get("message") or "Zhihu requires sign-in.").strip()
        raise ZhihuVerificationRequiredError(message[:500])
    author_token = str(payload.get("url_token") or "").strip()
    account_id = str(payload.get("id") or "").strip()
    if not account_id or not _ZHIHU_TOKEN_RE.fullmatch(author_token):
        raise ZhihuVerificationRequiredError(
            "Zhihu did not expose an authenticated account in the selected browser."
        )
    profile = normalize_zhihu_profile_url(
        f"{ZHIHU_HOME_URL}/people/{quote(author_token, safe='-_')}"
    )
    return ZhihuAccount(
        profile=profile,
        display_name=str(payload.get("name") or author_token).replace("\x00", "").strip()
        or author_token,
    )


def _activity_api_url(profile: ZhihuProfile) -> str:
    encoded_token = quote(profile.author_token, safe="-_")
    return (
        f"{ZHIHU_HOME_URL}/api/v3/moments/{encoded_token}/activities"
        f"?limit={ZHIHU_ACTIVITY_INITIAL_PAGE_SIZE}&desktop=true&ws_qiangzhisafe=0"
    )


def _activity_error(payload: Mapping[str, Any]) -> None:
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return
    message = str(error.get("message") or "Zhihu rejected the activity request.").strip()
    try:
        error_code = int(error.get("code") or 0)
    except (TypeError, ValueError):
        error_code = 0
    if bool(error.get("need_login")) or error_code == 40352:
        raise ZhihuVerificationRequiredError(
            f"{message[:400]} Open Zhihu in the selected browser, complete verification manually, then retry."
        )
    raise ZhihuArchiveError(message[:500])


def _validate_activity_cursor(
    value: object,
    profile: ZhihuProfile,
    expected_page_number: int,
) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        port = parsed.port
    except ValueError as exc:
        raise ZhihuArchiveError("Zhihu returned an invalid activity cursor.") from exc
    query = parse_qs(parsed.query)
    try:
        offset = int(query.get("offset", [""])[0])
        page_number = int(query.get("page_num", [""])[0])
    except (TypeError, ValueError):
        offset = -1
        page_number = -1
    expected_path = ZHIHU_ACTIVITY_PATH_TEMPLATE.format(
        author_token=profile.author_token
    )
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() not in ZHIHU_PROFILE_HOSTS
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
        or set(query) != {"offset", "page_num"}
        or offset <= 0
        or page_number != expected_page_number
    ):
        raise ZhihuArchiveError("Zhihu returned an invalid activity cursor.")
    return parsed.geturl()


def _parse_activity_page(
    payload: object,
    profile: ZhihuProfile,
    page_number: int,
) -> tuple[list[Mapping[str, Any]], bool, str | None]:
    if not isinstance(payload, Mapping):
        raise ZhihuArchiveError("Zhihu returned an invalid activity-page response.")
    _activity_error(payload)
    data = payload.get("data")
    paging = payload.get("paging")
    if not isinstance(data, list) or not isinstance(paging, Mapping):
        raise ZhihuArchiveError("Zhihu returned an activity page without data or paging metadata.")
    activities: list[Mapping[str, Any]] = []
    for item in data:
        if not isinstance(item, Mapping):
            raise ZhihuArchiveError("Zhihu returned an invalid activity entry.")
        activities.append(item)
    if bool(paging.get("need_force_login")):
        raise ZhihuVerificationRequiredError(
            "Zhihu requires sign-in or human verification before its activity history can be read."
        )
    is_end = paging.get("is_end")
    if not isinstance(is_end, bool):
        raise ZhihuArchiveError("Zhihu returned an invalid activity terminal-page flag.")
    if is_end:
        return activities, True, None
    next_url = _validate_activity_cursor(
        paging.get("next"),
        profile,
        page_number + 1,
    )
    return activities, False, next_url


def collect_zhihu_liked_answers(
    profile: ZhihuProfile,
    fetch_page: Callable[[str], object],
    should_stop: Callable[[], bool],
    *,
    on_progress: Callable[[int, int, int | None, int], None] | None = None,
) -> ZhihuCollectionResult:
    """Collect every answer present in one account's paginated vote-up activity."""

    initial_url = _activity_api_url(profile)
    next_url = initial_url
    unique_answers: dict[str, ZhihuAnswer] = {}
    seen_urls: set[str] = set()
    page_signatures: set[tuple[str, ...]] = set()
    first_signature: tuple[str, ...] = ()
    pages_processed = 0
    raw_answers = 0
    duplicates = 0
    total_content = 0

    while pages_processed < ZHIHU_ACTIVITY_PAGE_LIMIT:
        if should_stop():
            return ZhihuCollectionResult(
                tuple(unique_answers.values()),
                None,
                pages_processed,
                raw_answers,
                duplicates,
                True,
            )
        if next_url in seen_urls:
            raise ZhihuArchiveError("Zhihu repeated an activity cursor; the text cache was not changed.")
        seen_urls.add(next_url)
        activities, is_end, following_url = _parse_activity_page(
            fetch_page(next_url),
            profile,
            pages_processed,
        )
        signature = tuple(str(item.get("id") or "") for item in activities)
        if signature and signature in page_signatures:
            raise ZhihuArchiveError("Zhihu repeated an activity page; the text cache was not changed.")
        page_signatures.add(signature)
        if pages_processed == 0:
            first_signature = signature
        pages_processed += 1
        if not activities and not is_end:
            raise ZhihuArchiveError("Zhihu returned an empty activity page before the terminal page.")

        for activity in activities:
            if str(activity.get("verb") or "") != ZHIHU_ACTIVITY_VERB:
                continue
            target = activity.get("target")
            if not isinstance(target, Mapping) or str(target.get("type") or "") != "answer":
                continue
            answer = normalize_zhihu_answer_payload(
                target,
                profile,
                require_profile_author=False,
                allow_unavailable_content=True,
            )
            raw_answers += 1
            if answer.answer_id in unique_answers:
                duplicates += 1
                continue
            total_content += len(answer.content_html) + len(answer.content_text)
            if total_content > ZHIHU_ARCHIVE_CONTENT_LIMIT:
                raise ZhihuArchiveError(
                    "The Zhihu activity archive exceeded the 256,000,000-character safety limit."
                )
            unique_answers[answer.answer_id] = answer
            if len(unique_answers) > ZHIHU_ANSWER_LIMIT:
                raise ZhihuArchiveError(
                    f"The Zhihu activity archive exceeds the {ZHIHU_ANSWER_LIMIT:,}-answer safety limit."
                )
        if on_progress is not None:
            on_progress(pages_processed, len(unique_answers), None, duplicates)
        if is_end:
            break
        if following_url is None:
            raise ZhihuArchiveError("Zhihu omitted the next activity cursor.")
        next_url = following_url
    else:
        raise ZhihuArchiveError(
            f"Zhihu activity pagination exceeded the {ZHIHU_ACTIVITY_PAGE_LIMIT:,}-page safety limit."
        )

    if should_stop():
        return ZhihuCollectionResult(
            tuple(unique_answers.values()),
            None,
            pages_processed,
            raw_answers,
            duplicates,
            True,
        )
    verification_activities, _, _ = _parse_activity_page(
        fetch_page(initial_url),
        profile,
        0,
    )
    verification_signature = tuple(
        str(item.get("id") or "") for item in verification_activities
    )
    if verification_signature != first_signature:
        raise ZhihuArchiveError(
            "The newest Zhihu activity page changed during collection; retry to create one stable snapshot."
        )
    return ZhihuCollectionResult(
        answers=tuple(unique_answers.values()),
        expected_total=None,
        pages_processed=pages_processed,
        raw_answers=raw_answers,
        duplicates=duplicates,
        stopped=False,
    )


class ZhihuHistoryStore:
    """Merge normalized answers into the formal Local resources text cache."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        rows = read_parquet_rows(self.path)
        if self.path.exists() and rows is None:
            raise RuntimeError(f"Zhihu history Parquet is unreadable: {self.path}")
        self._rows: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            message_key = str(row.get("message_key") or "")
            if (
                not set(ZHIHU_HISTORY_SCHEMA.names).issubset(row)
                or int(row.get("schema_version") or 0) != ZHIHU_HISTORY_SCHEMA_VERSION
                or row.get("platform") != "zhihu"
                or not message_key.startswith("answer:")
                or message_key in self._rows
            ):
                raise RuntimeError(f"Zhihu history has an incompatible row: {self.path}")
            self._rows[message_key] = dict(row)

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Return deterministic newest-first answer rows."""

        return sorted(
            self._rows.values(),
            key=lambda row: (
                str(row.get("last_seen_at") or ""),
                str(row.get("conversation_id") or ""),
            ),
            reverse=True,
        )

    @property
    def cached_answers(self) -> int:
        """Return the number of unique answer IDs in the formal cache."""

        return len(self._rows)

    def merge_answers(
        self,
        answers: tuple[ZhihuAnswer, ...],
        captured_at: str,
    ) -> ZhihuHistoryWriteResult:
        """Upsert one complete collection without deleting earlier cached answers."""

        added = 0
        changed = 0
        unchanged = 0
        for answer in answers:
            message_key = f"answer:{answer.answer_id}"
            previous = self._rows.get(message_key)
            display_text = answer.content_text.strip() or answer.excerpt.strip()
            if not display_text:
                display_text = "Answer body unavailable on Zhihu. Open the source link."
            links = list(
                dict.fromkeys(
                    value
                    for value in (
                        answer.answer_url,
                        answer.question_url,
                        answer.profile_url,
                        *extract_zhihu_resource_links(answer.content_html),
                        *answer.media_urls,
                    )
                    if value
                )
            )
            first_seen_at = str(previous.get("first_seen_at") or captured_at) if previous else captured_at
            row = {
                "schema_version": ZHIHU_HISTORY_SCHEMA_VERSION,
                "platform": "zhihu",
                "conversation_id": answer.answer_id,
                "conversation_url": answer.answer_url,
                "conversation_title": answer.question_title,
                "message_key": message_key,
                "turn_index": 1,
                "message_index": 0,
                "role": "answer",
                "author_label": answer.author_name or answer.author_token or "Zhihu answerer",
                "content_text": display_text,
                # Local resources deliberately renders text plus safe source links;
                # provider HTML and remote images are never mounted into the page.
                "content_html": "",
                "content_sha256": answer.content_sha256,
                "source_links": links,
                "model_label": "",
                "first_seen_at": first_seen_at,
                "last_seen_at": answer.updated_at or answer.created_at or captured_at,
            }
            if previous is None:
                added += 1
            elif all(previous.get(name) == row.get(name) for name in ZHIHU_HISTORY_SCHEMA.names):
                unchanged += 1
            else:
                changed += 1
            self._rows[message_key] = row
        return ZhihuHistoryWriteResult(
            cached_answers=self.cached_answers,
            added=added,
            changed=changed,
            unchanged=unchanged,
        )

    def save(self) -> None:
        """Persist and verify the formal text cache atomically."""

        write_parquet_rows_atomic(self.path, self.rows, ZHIHU_HISTORY_SCHEMA)


def build_zhihu_history_initial_snapshot(
    version: str,
    local_store_root: Path | str = LOCAL_STORE_ROOT,
) -> TaskSnapshot:
    """Hydrate the formal Zhihu cache page from Local resources."""

    path = zhihu_history_path(local_store_root)
    store = ZhihuHistoryStore(path)
    return TaskSnapshot(
        version=version,
        account_name="Zhihu",
        output_dir=str(path.parent),
        progress_unit="answers",
        discovered_tweets=store.cached_answers,
        queued_tweets=store.cached_answers,
        processed_tweets=store.cached_answers,
        downloaded_posts=store.cached_answers,
        downloaded_tweets=store.cached_answers,
        message=f"Ready. Found {store.cached_answers:,} cached Zhihu answers.",
    )


def sync_zhihu_history(
    state: TaskState,
    config: CrawlConfig,
    should_stop: Callable[[], bool],
    local_store_root: Path | str = LOCAL_STORE_ROOT,
    *,
    author_url: str = "",
    fetch_page: Callable[[str], object] | None = None,
    account_payload: object | None = None,
) -> dict[str, Any]:
    """Cache signed-in vote-up activity or every answer from one optional author."""

    descriptor = browser_descriptors(config).get(config.zhihu_browser)
    if descriptor is None or descriptor.engine != "chromium":
        raise ValueError("Zhihu answer caching requires Edge or Chrome.")
    target_profile = normalize_zhihu_profile_url(author_url) if author_url.strip() else None
    path = zhihu_history_path(local_store_root)
    store = ZhihuHistoryStore(path)
    collection_mode = "author" if target_profile is not None else "liked"
    state.update(
        phase="collecting",
        account_name="Zhihu",
        output_dir=str(path.parent),
        progress_unit="answers",
        downloaded_posts=store.cached_answers,
        downloaded_tweets=store.cached_answers,
        message=f"Opening the authenticated Zhihu session in {descriptor.label}...",
    )
    state.append_event(f"Opening the authenticated Zhihu session in {descriptor.label}.")

    def update_progress(
        pages: int,
        unique: int,
        total: int | None,
        duplicates: int,
    ) -> None:
        state.update(
            phase="downloading",
            discovered_tweets=total or unique,
            queued_tweets=total or unique,
            processed_tweets=unique,
            performance_metrics={
                "collection_mode": collection_mode,
                "pages_processed": pages,
                "duplicates": duplicates,
                "expected_answers": total,
            },
            message=(
                f"Read {unique:,} unique answers across {pages:,} pages"
                + (f" of {total:,} reported answers." if total is not None else ".")
            ),
        )

    def collect(account: ZhihuAccount, active_fetch: Callable[[str], object]) -> ZhihuCollectionResult:
        state.update(account_name=account.display_name)
        if target_profile is None:
            state.append_event(
                f"Caching answers upvoted by @{account.profile.author_token}."
            )
            return collect_zhihu_liked_answers(
                account.profile,
                active_fetch,
                should_stop,
                on_progress=update_progress,
            )

        from .zhihu_answers import collect_zhihu_answers

        state.append_event(
            f"Caching every answer published by @{target_profile.author_token}."
        )
        return collect_zhihu_answers(
            target_profile,
            active_fetch,
            should_stop,
            on_progress=update_progress,
        )

    if fetch_page is not None:
        if account_payload is None:
            raise ValueError("A fixture account payload is required with an injected Zhihu fetcher.")
        account = parse_zhihu_account(account_payload)
        collection = collect(account, fetch_page)
    else:
        with sync_playwright_or_error() as playwright:
            with launch_chromium_context(
                playwright,
                descriptor,
                headless=False,
                clone_profile_first=True,
                background_window=True,
            ) as context:
                page = context.pages[0] if context.pages else context.new_page()
                destination = target_profile.answers_url if target_profile else ZHIHU_HOME_URL
                goto_with_retry(
                    page,
                    destination,
                    attempts=3,
                    timeout_ms=90_000,
                    should_stop=should_stop,
                )
                if should_stop():
                    collection = ZhihuCollectionResult((), None, 0, 0, 0, True)
                    account = ZhihuAccount(
                        profile=target_profile or normalize_zhihu_profile_url(
                            f"{ZHIHU_HOME_URL}/people/unknown"
                        ),
                        display_name="Zhihu",
                    )
                else:
                    current_url = str(getattr(page, "url", "") or "")
                    body_text = page.locator("body").inner_text(timeout=10_000)
                    if (
                        "/account/unhuman" in current_url
                        or "系统监测到您的网络环境存在异常" in body_text
                    ):
                        raise ZhihuVerificationRequiredError(
                            "Zhihu requires human verification. Open Zhihu in the selected browser, "
                            "complete verification manually, then retry."
                        )
                    account = parse_zhihu_account(_browser_fetch_json(page, ZHIHU_ME_URL))

                    def browser_fetch(url: str) -> object:
                        payload = _browser_fetch_json(page, url)
                        if not should_stop():
                            page.wait_for_timeout(ZHIHU_PAGE_SETTLE_MS)
                        return payload

                    collection = collect(account, browser_fetch)

    if collection.stopped or should_stop():
        return {
            "stopped": True,
            "collection_mode": collection_mode,
            "account": account,
            "processed_answers": len(collection.answers),
            "cached_answers": store.cached_answers,
            "pages_processed": collection.pages_processed,
            "duplicates": collection.duplicates,
            "added": 0,
            "changed": 0,
            "unchanged": 0,
            "output_dir": str(path.parent),
        }

    state.update(
        phase="committing",
        message=f"Writing and verifying {len(collection.answers):,} Zhihu answer records...",
    )
    write_result = store.merge_answers(collection.answers, utc_now_iso())
    store.save()
    verified = ZhihuHistoryStore(path)
    if verified.cached_answers != write_result.cached_answers:
        raise RuntimeError("Zhihu history readback did not match the committed answer count.")
    state.update(
        discovered_tweets=collection.expected_total or len(collection.answers),
        queued_tweets=collection.expected_total or len(collection.answers),
        processed_tweets=len(collection.answers),
        downloaded_posts=write_result.cached_answers,
        downloaded_tweets=write_result.cached_answers,
        performance_metrics={
            "collection_mode": collection_mode,
            "pages_processed": collection.pages_processed,
            "duplicates": collection.duplicates,
            "expected_answers": collection.expected_total,
            "raw_answers": collection.raw_answers,
            "added": write_result.added,
            "changed": write_result.changed,
            "unchanged": write_result.unchanged,
        },
    )
    return {
        "stopped": False,
        "collection_mode": collection_mode,
        "account": account,
        "processed_answers": len(collection.answers),
        "cached_answers": write_result.cached_answers,
        "pages_processed": collection.pages_processed,
        "duplicates": collection.duplicates,
        "added": write_result.added,
        "changed": write_result.changed,
        "unchanged": write_result.unchanged,
        "output_dir": str(path.parent),
    }
