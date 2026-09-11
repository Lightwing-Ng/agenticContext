"""Authenticated Zhihu answer normalization and bounded collection.

Code version: v1.0.0-codex.1
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

ZHIHU_HOME_URL = "https://www.zhihu.com"
ZHIHU_ME_URL = f"{ZHIHU_HOME_URL}/api/v4/me"
ZHIHU_PROFILE_HOSTS = frozenset({"zhihu.com", "www.zhihu.com"})
ZHIHU_ANSWER_PAGE_SIZE = 20
ZHIHU_ANSWER_PAGE_LIMIT = 1_000
ZHIHU_ANSWER_LIMIT = 20_000
ZHIHU_API_RESPONSE_LIMIT = 12_000_000
ZHIHU_ANSWER_CONTENT_LIMIT = 4_000_000
ZHIHU_ARCHIVE_CONTENT_LIMIT = 256_000_000
ZHIHU_FETCH_RETRY_LIMIT = 3
ZHIHU_FETCH_RETRY_DELAY_MS = 1_500
ZHIHU_FETCH_TIMEOUT_MS = 45_000
ZHIHU_PAGE_SETTLE_MS = 650
_ZHIHU_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)


class ZhihuArchiveError(RuntimeError):
    """Base error for a run that must not replace the last complete archive."""


class ZhihuVerificationRequiredError(ZhihuArchiveError):
    """The selected browser must be verified manually before another run."""


@dataclass(frozen=True, slots=True)
class ZhihuProfile:
    """A validated public Zhihu profile identity."""

    author_token: str
    profile_url: str
    answers_url: str


@dataclass(frozen=True, slots=True)
class ZhihuAnswer:
    """One normalized public answer returned by Zhihu."""

    answer_id: str
    author_id: str
    author_token: str
    author_name: str
    profile_url: str
    question_id: str
    question_title: str
    question_url: str
    answer_url: str
    excerpt: str
    content_text: str
    content_html: str
    content_available: bool
    media_urls: tuple[str, ...]
    content_need_truncated: bool
    force_login_when_click_read_more: bool
    is_collapsed: bool
    is_normal: bool | None
    collapse_reason: str | None
    created_at: str
    updated_at: str
    voteup_count: int | None
    comment_count: int | None
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ZhihuCollectionResult:
    """A complete or cooperatively stopped pagination result."""

    answers: tuple[ZhihuAnswer, ...]
    expected_total: int | None
    pages_processed: int
    raw_answers: int
    duplicates: int
    stopped: bool


@dataclass(frozen=True, slots=True)
class ZhihuEnumerationFingerprint:
    """Bounded identity proof for one provider pagination pass."""

    answer_ids: tuple[str, ...]
    expected_total: int | None
    pages_processed: int
    raw_answers: int
    duplicates: int
    stopped: bool


class _ZhihuHTMLTextExtractor(HTMLParser):
    """Convert stored answer HTML to reviewable text without executing markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._media_urls: list[str] = []
        self._resource_urls: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized in {"script", "style", "template"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if normalized in _BLOCK_TAGS:
            self._parts.append("\n")
        if normalized == "a":
            href = next((value for key, value in attrs if key.casefold() == "href"), None)
            self._append_resource_url(href)
        if normalized == "img":
            alt = next((value for key, value in attrs if key.casefold() == "alt"), None)
            if alt:
                self._parts.append(f"[{alt}]")
            for key, value in attrs:
                if key.casefold() not in {"src", "data-original", "data-actualsrc"} or not value:
                    continue
                candidate = self._resolve_resource_url(value)
                if candidate is None:
                    continue
                host = (candidate.hostname or "").casefold()
                if candidate.scheme == "https" and (
                    host in {"zhihu.com", "zhimg.com"}
                    or host.endswith(".zhihu.com")
                    or host.endswith(".zhimg.com")
                ):
                    normalized_url = candidate.geturl()
                    if normalized_url not in self._media_urls:
                        self._media_urls.append(normalized_url)
                    if normalized_url not in self._resource_urls:
                        self._resource_urls.append(normalized_url)

    def _append_resource_url(self, value: str | None) -> None:
        """Retain one absolute HTTP(S) hyperlink without loading its payload."""

        if not value:
            return
        candidate = self._resolve_resource_url(value)
        if candidate is None:
            return
        normalized_url = candidate.geturl()
        if normalized_url not in self._resource_urls:
            self._resource_urls.append(normalized_url)

    @staticmethod
    def _resolve_resource_url(value: str) -> Any | None:
        """Resolve relative provider links without fetching their targets."""

        try:
            candidate = urlsplit(urljoin(f"{ZHIHU_HOME_URL}/", value.strip()))
        except ValueError:
            return None
        if candidate.scheme.casefold() not in {"http", "https"} or not candidate.netloc:
            return None
        return candidate

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in {"script", "style", "template"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if not self._ignored_depth and normalized in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    def text(self) -> str:
        normalized_lines = [
            re.sub(r"[\t\f\v ]+", " ", line).strip()
            for line in "".join(self._parts).replace("\x00", "").splitlines()
        ]
        output: list[str] = []
        for line in normalized_lines:
            if line:
                output.append(line)
            elif output and output[-1]:
                output.append("")
        return "\n".join(output).strip()

    @property
    def media_urls(self) -> tuple[str, ...]:
        """Return deduplicated HTTPS Zhihu media references in source order."""

        return tuple(self._media_urls)

    @property
    def resource_urls(self) -> tuple[str, ...]:
        """Return deduplicated rich-text hyperlinks and media references."""

        return tuple(self._resource_urls)


def normalize_zhihu_profile_url(value: object) -> ZhihuProfile:
    """Accept only one canonical Zhihu people profile or answers URL."""

    raw_url = str(value or "").strip()
    if not raw_url:
        raise ValueError("Enter a Zhihu profile answers URL.")
    try:
        parsed = urlsplit(raw_url)
    except ValueError as exc:
        raise ValueError("Enter a valid Zhihu profile answers URL.") from exc
    host = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid Zhihu profile answers URL.") from exc
    if (
        parsed.scheme.casefold() != "https"
        or host not in ZHIHU_PROFILE_HOSTS
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Use an https://www.zhihu.com/people/<name>/answers URL.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) not in {2, 3} or parts[0] != "people" or (len(parts) == 3 and parts[2] != "answers"):
        raise ValueError("Use a Zhihu people profile or its Answers tab URL.")
    author_token = parts[1]
    if not _ZHIHU_TOKEN_RE.fullmatch(author_token):
        raise ValueError("The Zhihu profile name is not valid.")
    encoded_token = quote(author_token, safe="-_")
    profile_url = f"{ZHIHU_HOME_URL}/people/{encoded_token}"
    return ZhihuProfile(
        author_token=author_token,
        profile_url=profile_url,
        answers_url=f"{profile_url}/answers",
    )


def _answer_api_url(profile: ZhihuProfile, offset: int) -> str:
    include = (
        "data[*].id,url,excerpt,content,created_time,updated_time,voteup_count,"
        "comment_count,author.id,author.name,author.url_token,"
        "content_need_truncated,force_login_when_click_read_more,is_collapsed,"
        "is_normal,collapse_reason,question.id,question.title,question.url"
    )
    query = urlencode(
        {
            "include": include,
            "limit": ZHIHU_ANSWER_PAGE_SIZE,
            "offset": offset,
            "sort_by": "created",
        }
    )
    return f"{ZHIHU_HOME_URL}/api/v4/members/{quote(profile.author_token, safe='-_')}/answers?{query}"


def _parse_answer_html(value: str) -> tuple[str, tuple[str, ...]]:
    parser = _ZhihuHTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except (AssertionError, ValueError):
        return re.sub(r"\s+", " ", value.replace("\x00", " ")).strip(), ()
    return parser.text(), parser.media_urls


def extract_zhihu_resource_links(value: str) -> tuple[str, ...]:
    """Extract links from answer rich text without retaining executable markup."""

    parser = _ZhihuHTMLTextExtractor()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except (AssertionError, ValueError):
        return ()
    return parser.resource_urls


def _clean_text(value: object, *, maximum: int | None = None) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if maximum is not None and len(text) > maximum:
        raise ZhihuArchiveError(
            f"Zhihu returned one answer field larger than the {maximum:,}-character safety limit."
        )
    return text


def _optional_text(value: object, *, maximum: int | None = None) -> str | None:
    if value is None:
        return None
    return _clean_text(value, maximum=maximum)


def _nonnegative_int(value: object, field: str) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ZhihuArchiveError(f"Zhihu returned an invalid {field} value.") from exc
    if parsed < 0:
        raise ZhihuArchiveError(f"Zhihu returned an invalid {field} value.")
    return parsed


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value in {None, ""}:
        return None
    return _nonnegative_int(value, field)


def _boolean(value: object, field: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ZhihuArchiveError(f"Zhihu returned an invalid {field} value.")
    return value


def _optional_boolean(value: object, field: str) -> bool | None:
    if value is None:
        return None
    return _boolean(value, field)


def _timestamp(value: object) -> str:
    if value in {None, ""}:
        return ""
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        text = _clean_text(value)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        return datetime.fromtimestamp(numeric, tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError) as exc:
        raise ZhihuArchiveError("Zhihu returned an invalid answer timestamp.") from exc


def normalize_zhihu_answer_payload(
    payload: Mapping[str, Any],
    profile: ZhihuProfile,
    *,
    require_profile_author: bool = True,
    allow_unavailable_content: bool = False,
) -> ZhihuAnswer:
    """Normalize one provider answer for either an author or activity collection."""

    answer_id = _clean_text(payload.get("id"))
    if not answer_id.isdigit():
        raise ZhihuArchiveError("Zhihu returned an answer without a stable numeric ID.")
    author = payload.get("author")
    question = payload.get("question")
    if not isinstance(author, Mapping) or not isinstance(question, Mapping):
        raise ZhihuArchiveError(f"Zhihu answer {answer_id} is missing author or question metadata.")
    author_token = _clean_text(author.get("url_token")) or (
        profile.author_token if require_profile_author else "anonymous"
    )
    if require_profile_author and author_token != profile.author_token:
        raise ZhihuArchiveError(
            f"Zhihu answer {answer_id} belongs to a different profile; the archive was not replaced."
        )
    question_id = _clean_text(question.get("id"))
    if not question_id.isdigit():
        raise ZhihuArchiveError(f"Zhihu answer {answer_id} is missing a stable question ID.")
    content_html = _clean_text(payload.get("content"), maximum=ZHIHU_ANSWER_CONTENT_LIMIT)
    excerpt = _clean_text(payload.get("excerpt"), maximum=ZHIHU_ANSWER_CONTENT_LIMIT)
    content_text, media_urls = _parse_answer_html(content_html)
    is_collapsed = _boolean(payload.get("is_collapsed"), "collapsed flag")
    is_normal = _optional_boolean(payload.get("is_normal"), "normal-answer flag")
    content_available = bool(content_html and content_text)
    if (
        not content_available
        and not allow_unavailable_content
        and not (is_collapsed or is_normal is False)
    ):
        raise ZhihuArchiveError(
            f"Zhihu answer {answer_id} does not expose complete cacheable content; "
            "the previous archive was preserved."
        )
    question_title = _clean_text(question.get("title"), maximum=20_000)
    if not question_title:
        raise ZhihuArchiveError(f"Zhihu answer {answer_id} has no question title.")
    content_sha256 = hashlib.sha256(
        json.dumps(
            {
                "content_html": content_html,
                "content_text": content_text,
                "content_available": content_available,
                "question_title": question_title,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    question_url = f"{ZHIHU_HOME_URL}/question/{question_id}"
    return ZhihuAnswer(
        answer_id=answer_id,
        author_id=_clean_text(author.get("id")),
        author_token=author_token,
        author_name=_clean_text(author.get("name")) or author_token,
        profile_url=(
            f"{ZHIHU_HOME_URL}/people/{quote(author_token, safe='-_')}"
            if _ZHIHU_TOKEN_RE.fullmatch(author_token)
            else ""
        ),
        question_id=question_id,
        question_title=question_title,
        question_url=question_url,
        answer_url=f"{question_url}/answer/{answer_id}",
        excerpt=excerpt or content_text[:280],
        content_text=content_text,
        content_html=content_html,
        content_available=content_available,
        media_urls=media_urls,
        content_need_truncated=_boolean(
            payload.get("content_need_truncated"),
            "content truncation flag",
        ),
        force_login_when_click_read_more=_boolean(
            payload.get("force_login_when_click_read_more"),
            "read-more login flag",
        ),
        is_collapsed=is_collapsed,
        is_normal=is_normal,
        collapse_reason=_optional_text(payload.get("collapse_reason"), maximum=20_000),
        created_at=_timestamp(payload.get("created_time")),
        updated_at=_timestamp(payload.get("updated_time")),
        voteup_count=_optional_nonnegative_int(payload.get("voteup_count"), "vote count"),
        comment_count=_optional_nonnegative_int(payload.get("comment_count"), "comment count"),
        content_sha256=content_sha256,
    )


def _parse_api_page(
    payload: object,
    profile: ZhihuProfile,
    current_offset: int,
) -> tuple[list[ZhihuAnswer], int | None, bool, int | None]:
    if not isinstance(payload, Mapping):
        raise ZhihuArchiveError("Zhihu returned an invalid answer-page response.")
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = _clean_text(error.get("message")) or "Zhihu rejected the answer request."
        try:
            error_code = int(error.get("code") or 0)
        except (TypeError, ValueError):
            error_code = 0
        if bool(error.get("need_login")) or error_code == 40352:
            raise ZhihuVerificationRequiredError(
                f"{message} Open the profile in the selected browser, complete verification manually, then retry."
            )
        raise ZhihuArchiveError(message[:500])
    data = payload.get("data")
    paging = payload.get("paging")
    if not isinstance(data, list) or not isinstance(paging, Mapping):
        raise ZhihuArchiveError("Zhihu returned an answer page without data or paging metadata.")
    answers = []
    for item in data:
        if not isinstance(item, Mapping):
            raise ZhihuArchiveError("Zhihu returned an invalid answer entry.")
        answers.append(
            normalize_zhihu_answer_payload(
                item,
                profile,
                allow_unavailable_content=True,
            )
        )
    totals_value = paging.get("totals")
    if totals_value is None or totals_value == "":
        expected_total = None
    elif isinstance(totals_value, bool) or not isinstance(totals_value, int):
        raise ZhihuArchiveError("Zhihu returned an invalid answer total value.")
    else:
        expected_total = _nonnegative_int(totals_value, "answer total")
    is_end_value = paging.get("is_end")
    if not isinstance(is_end_value, bool):
        raise ZhihuArchiveError("Zhihu returned an invalid terminal-page flag.")
    is_end = is_end_value
    if is_end:
        return answers, expected_total, True, None
    next_value = paging.get("next")
    if next_value is None or next_value == "":
        return answers, expected_total, False, None
    try:
        parsed_next = urlsplit(str(next_value))
        next_port = parsed_next.port
    except ValueError as exc:
        raise ZhihuArchiveError("Zhihu returned an invalid next-page cursor.") from exc
    query = parse_qs(parsed_next.query)
    expected_path = f"/api/v4/members/{profile.author_token}/answers"
    offset_values = query.get("offset", [])
    try:
        next_offset = int(offset_values[0]) if len(offset_values) == 1 else -1
    except (TypeError, ValueError):
        next_offset = -1
    if (
        parsed_next.scheme.casefold() not in {"http", "https"}
        or (parsed_next.hostname or "").casefold() not in ZHIHU_PROFILE_HOSTS
        or next_port is not None
        or parsed_next.username is not None
        or parsed_next.password is not None
        or parsed_next.path != expected_path
        or query.get("limit") != [str(ZHIHU_ANSWER_PAGE_SIZE)]
        or query.get("sort_by", ["created"]) != ["created"]
        or next_offset != current_offset + ZHIHU_ANSWER_PAGE_SIZE
        or next_offset > ZHIHU_ANSWER_LIMIT
    ):
        raise ZhihuArchiveError("Zhihu returned an invalid next-page cursor.")
    return answers, expected_total, False, next_offset


def _collect_enumeration_fingerprint(
    profile: ZhihuProfile,
    fetch_page: Callable[[str], object],
    should_stop: Callable[[], bool],
) -> ZhihuEnumerationFingerprint:
    """Repeat pagination while retaining only stable record digests."""

    answer_ids: dict[str, None] = {}
    expected_total: int | None = None
    page_signatures: set[tuple[str, ...]] = set()
    pages_processed = 0
    raw_answers = 0
    duplicates = 0
    total_content = 0
    offset = 0
    while pages_processed < ZHIHU_ANSWER_PAGE_LIMIT:
        if should_stop():
            return ZhihuEnumerationFingerprint(
                tuple(answer_ids),
                expected_total,
                pages_processed,
                raw_answers,
                duplicates,
                True,
            )
        page_answers, page_total, is_end, next_offset = _parse_api_page(
            fetch_page(_answer_api_url(profile, offset)),
            profile,
            offset,
        )
        signature = tuple(answer.answer_id for answer in page_answers)
        if signature and signature in page_signatures:
            raise ZhihuArchiveError("Zhihu repeated an answer page; the archive was not replaced.")
        page_signatures.add(signature)
        pages_processed += 1
        if page_total is None:
            raise ZhihuArchiveError(
                "Zhihu did not report an answer total on every page; completeness could not be verified."
            )
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            raise ZhihuArchiveError(
                "The Zhihu answer total changed during verification; retry to create one stable snapshot."
            )
        raw_answers += len(page_answers)
        for answer in page_answers:
            if answer.answer_id in answer_ids:
                duplicates += 1
                continue
            total_content += len(answer.content_html) + len(answer.content_text)
            if total_content > ZHIHU_ARCHIVE_CONTENT_LIMIT:
                raise ZhihuArchiveError(
                    "The Zhihu archive exceeded the 256,000,000-character in-memory safety limit."
                )
            answer_ids[answer.answer_id] = None
        if len(answer_ids) > ZHIHU_ANSWER_LIMIT:
            raise ZhihuArchiveError(
                f"The Zhihu profile exceeds the {ZHIHU_ANSWER_LIMIT:,}-answer safety limit."
            )
        if is_end:
            break
        if not page_answers:
            raise ZhihuArchiveError("Zhihu returned an empty page before the end of the answer list.")
        if next_offset is None:
            raise ZhihuArchiveError("Zhihu omitted the next-page cursor before the terminal page.")
        offset = next_offset
    else:
        raise ZhihuArchiveError(
            f"Zhihu pagination exceeded the {ZHIHU_ANSWER_PAGE_LIMIT:,}-page safety limit."
        )
    return ZhihuEnumerationFingerprint(
        tuple(answer_ids),
        expected_total,
        pages_processed,
        raw_answers,
        duplicates,
        False,
    )


def collect_zhihu_answers(
    profile: ZhihuProfile,
    fetch_page: Callable[[str], object],
    should_stop: Callable[[], bool],
    *,
    on_progress: Callable[[int, int, int | None, int], None] | None = None,
    on_gap_verification: Callable[[int], None] | None = None,
) -> ZhihuCollectionResult:
    """Collect a stable, complete answer snapshot through bounded API pagination."""

    unique_answers: dict[str, ZhihuAnswer] = {}
    expected_total: int | None = None
    first_page_ids: tuple[str, ...] = ()
    page_signatures: set[tuple[str, ...]] = set()
    raw_answers = 0
    duplicates = 0
    pages_processed = 0
    offset = 0
    total_content = 0

    while pages_processed < ZHIHU_ANSWER_PAGE_LIMIT:
        if should_stop():
            return ZhihuCollectionResult(
                tuple(unique_answers.values()),
                expected_total,
                pages_processed,
                raw_answers,
                duplicates,
                True,
            )
        request_url = _answer_api_url(profile, offset)
        page_answers, page_total, is_end, next_offset = _parse_api_page(
            fetch_page(request_url),
            profile,
            offset,
        )
        signature = tuple(answer.answer_id for answer in page_answers)
        if signature and signature in page_signatures:
            raise ZhihuArchiveError("Zhihu repeated an answer page; the archive was not replaced.")
        page_signatures.add(signature)
        pages_processed += 1
        if page_total is None:
            raise ZhihuArchiveError(
                "Zhihu did not report an answer total on every page; completeness could not be verified."
            )
        if pages_processed == 1:
            first_page_ids = signature
            expected_total = page_total
        elif expected_total is not None and page_total != expected_total:
            raise ZhihuArchiveError(
                "The Zhihu answer total changed during collection; retry to create one stable snapshot."
            )
        elif expected_total is None:
            expected_total = page_total

        raw_answers += len(page_answers)
        for answer in page_answers:
            prior = unique_answers.get(answer.answer_id)
            if prior is not None:
                duplicates += 1
                continue
            total_content += len(answer.content_html) + len(answer.content_text)
            if total_content > ZHIHU_ARCHIVE_CONTENT_LIMIT:
                raise ZhihuArchiveError(
                    "The Zhihu archive exceeded the 256,000,000-character in-memory safety limit."
                )
            unique_answers[answer.answer_id] = answer
        if len(unique_answers) > ZHIHU_ANSWER_LIMIT:
            raise ZhihuArchiveError(
                f"The Zhihu profile exceeds the {ZHIHU_ANSWER_LIMIT:,}-answer safety limit."
            )
        if on_progress is not None:
            on_progress(pages_processed, len(unique_answers), expected_total, duplicates)

        if is_end:
            break
        if not page_answers:
            raise ZhihuArchiveError("Zhihu returned an empty page before the end of the answer list.")
        if next_offset is None:
            raise ZhihuArchiveError("Zhihu omitted the next-page cursor before the terminal page.")
        offset = next_offset
    else:
        raise ZhihuArchiveError(
            f"Zhihu pagination exceeded the {ZHIHU_ANSWER_PAGE_LIMIT:,}-page safety limit."
        )

    if should_stop():
        return ZhihuCollectionResult(
            tuple(unique_answers.values()),
            expected_total,
            pages_processed,
            raw_answers,
            duplicates,
            True,
        )
    if expected_total is not None and len(unique_answers) > expected_total:
        raise ZhihuArchiveError(
            f"Zhihu reported {expected_total:,} answers but returned more unique answers. "
            "The previous complete archive was preserved."
        )
    if expected_total is None:
        raise ZhihuArchiveError(
            "Zhihu did not report an answer total; completeness could not be verified. "
            "The previous archive was preserved."
        )

    verification_answers, verification_total, _, _ = _parse_api_page(
        fetch_page(_answer_api_url(profile, 0)),
        profile,
        0,
    )
    if should_stop():
        return ZhihuCollectionResult(
            tuple(unique_answers.values()),
            expected_total,
            pages_processed,
            raw_answers,
            duplicates,
            True,
        )
    verification_ids = tuple(answer.answer_id for answer in verification_answers)
    if verification_ids != first_page_ids or verification_total != expected_total:
        raise ZhihuArchiveError(
            "The newest Zhihu answer page changed during collection; retry to create one stable snapshot."
        )

    provider_gap = expected_total - len(unique_answers)
    if provider_gap:
        if raw_answers < expected_total:
            raise ZhihuArchiveError(
                f"Zhihu reported {expected_total:,} answers but returned only "
                f"{len(unique_answers):,} unique records. The previous complete archive was preserved."
            )
        if on_gap_verification is not None:
            on_gap_verification(provider_gap)
        gap_verification = _collect_enumeration_fingerprint(
            profile,
            fetch_page,
            should_stop,
        )
        if gap_verification.stopped:
            return ZhihuCollectionResult(
                tuple(unique_answers.values()),
                expected_total,
                pages_processed,
                raw_answers,
                duplicates,
                True,
            )
        if gap_verification != ZhihuEnumerationFingerprint(
            tuple(unique_answers),
            expected_total,
            pages_processed,
            raw_answers,
            duplicates,
            False,
        ):
            raise ZhihuArchiveError(
                "Zhihu pagination did not reproduce the same provider gap; retry to create one stable snapshot."
            )

    return ZhihuCollectionResult(
        answers=tuple(unique_answers.values()),
        expected_total=expected_total,
        pages_processed=pages_processed,
        raw_answers=raw_answers,
        duplicates=duplicates,
        stopped=False,
    )


def _is_allowed_zhihu_api_url(parsed) -> bool:
    """Keep authenticated reads inside the three Zhihu endpoints used here."""

    query = parse_qs(parsed.query)
    if parsed.path == "/api/v4/me":
        return not query
    answers_match = re.fullmatch(
        r"/api/v4/members/([A-Za-z0-9_-]{1,128})/answers",
        parsed.path,
    )
    if answers_match is not None:
        try:
            offset = int(query.get("offset", [""])[0])
        except (TypeError, ValueError):
            offset = -1
        return (
            query.get("sort_by") == ["created"]
            and query.get("limit") == [str(ZHIHU_ANSWER_PAGE_SIZE)]
            and offset >= 0
        )
    activity_match = re.fullmatch(
        r"/api/v3/moments/([A-Za-z0-9_-]{1,128})/activities",
        parsed.path,
    )
    if activity_match is None:
        return False
    initial_page = (
        query.get("limit") == ["5"]
        and query.get("desktop") == ["true"]
        and query.get("ws_qiangzhisafe") == ["0"]
        and set(query) == {"limit", "desktop", "ws_qiangzhisafe"}
    )
    if initial_page:
        return True
    try:
        offset = int(query.get("offset", [""])[0])
        page_number = int(query.get("page_num", [""])[0])
    except (TypeError, ValueError):
        return False
    return offset > 0 and page_number >= 1 and set(query) == {"offset", "page_num"}


def _browser_fetch_json(page: Any, url: str) -> object:
    """Fetch one same-origin JSON page through the authenticated browser context."""

    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Zhihu collection refused a non-Zhihu API URL.") from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != "www.zhihu.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Zhihu collection refused a non-Zhihu API URL.")
    if not _is_allowed_zhihu_api_url(parsed):
        raise ValueError("Zhihu collection refused an unexpected API request.")

    last_message = ""
    for attempt in range(1, ZHIHU_FETCH_RETRY_LIMIT + 1):
        result = page.evaluate(
            """
            async ({url, timeoutMs, responseLimit}) => {
              const controller = new AbortController();
              const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
              try {
                const response = await fetch(url, {
                  method: "GET",
                  credentials: "include",
                  headers: {Accept: "application/json"},
                  signal: controller.signal,
                });
                const text = await response.text();
                return {
                  status: response.status,
                  contentType: response.headers.get("content-type") || "",
                  text: text.slice(0, responseLimit + 1),
                  truncated: text.length > responseLimit,
                };
              } catch (error) {
                return {status: 0, contentType: "", text: "", error: String(error), truncated: false};
              } finally {
                clearTimeout(timeoutId);
              }
            }
            """,
            {
                "url": url,
                "timeoutMs": ZHIHU_FETCH_TIMEOUT_MS,
                "responseLimit": ZHIHU_API_RESPONSE_LIMIT,
            },
        )
        if not isinstance(result, Mapping):
            raise ZhihuArchiveError("Zhihu returned an unreadable browser response.")
        if bool(result.get("truncated")):
            raise ZhihuArchiveError(
                f"One Zhihu API page exceeded the {ZHIHU_API_RESPONSE_LIMIT:,}-character safety limit."
            )
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        text = str(result.get("text") or "")
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            if status in {401, 403}:
                raise ZhihuVerificationRequiredError(
                    "Zhihu requires sign-in or human verification. Open the profile in the selected "
                    "browser, complete verification manually, then retry."
                ) from exc
            if attempt < ZHIHU_FETCH_RETRY_LIMIT and (
                status in {0, 408, 425, 429} or status >= 500
            ):
                page.wait_for_timeout(ZHIHU_FETCH_RETRY_DELAY_MS * attempt)
                continue
            raise ZhihuArchiveError(
                "Zhihu returned non-JSON content. Open the profile in the selected browser and check for verification."
            ) from exc
        if 200 <= status < 300:
            return payload
        error = payload.get("error") if isinstance(payload, Mapping) else None
        error_message = _clean_text(error.get("message")) if isinstance(error, Mapping) else ""
        try:
            error_code = int(error.get("code") or 0) if isinstance(error, Mapping) else 0
        except (TypeError, ValueError):
            error_code = 0
        if status in {401, 403} or error_code == 40352 or bool(
            isinstance(error, Mapping) and error.get("need_login")
        ):
            raise ZhihuVerificationRequiredError(
                f"{error_message or 'Zhihu requires sign-in or human verification.'} "
                "Open the profile in the selected browser, complete verification manually, then retry."
            )
        last_message = error_message or str(result.get("error") or "") or f"HTTP {status}"
        retryable = status in {0, 408, 425, 429} or status >= 500
        if attempt >= ZHIHU_FETCH_RETRY_LIMIT or not retryable:
            raise ZhihuArchiveError(f"Zhihu answer collection failed: {last_message[:400]}")
        page.wait_for_timeout(ZHIHU_FETCH_RETRY_DELAY_MS * attempt)
    raise ZhihuArchiveError(f"Zhihu answer collection failed: {last_message[:400]}")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
