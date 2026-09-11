"""Authenticated Zhihu answer collection and isolated local persistence.

Code version: v0.9.1-codex.1
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

import pyarrow as pa
import pyarrow.parquet as pq

from .browser_sessions import (
    browser_descriptors,
    goto_with_retry,
    launch_chromium_context,
    sync_playwright_or_error,
)
from .config import BETA_STORE_ROOT, CrawlConfig
from .resource_persistence import read_parquet_rows, write_parquet_rows_atomic
from .state import TaskState


ZHIHU_HOME_URL = "https://www.zhihu.com"
ZHIHU_ME_URL = f"{ZHIHU_HOME_URL}/api/v4/me"
ZHIHU_PROFILE_HOSTS = frozenset({"zhihu.com", "www.zhihu.com"})
ZHIHU_ANSWER_SCHEMA_VERSION = 2
ZHIHU_ANSWER_PAGE_SIZE = 20
ZHIHU_ANSWER_PAGE_LIMIT = 1_000
ZHIHU_ANSWER_LIMIT = 20_000
ZHIHU_ARCHIVE_BROWSER_PAGE_SIZE = 20
ZHIHU_ARCHIVE_BROWSER_PAGE_SIZE_LIMIT = 100
ZHIHU_ARCHIVE_SEARCH_LIMIT = 500
ZHIHU_API_RESPONSE_LIMIT = 12_000_000
ZHIHU_ANSWER_CONTENT_LIMIT = 4_000_000
ZHIHU_ARCHIVE_CONTENT_LIMIT = 256_000_000
ZHIHU_FETCH_RETRY_LIMIT = 3
ZHIHU_FETCH_RETRY_DELAY_MS = 1_500
ZHIHU_FETCH_TIMEOUT_MS = 45_000
ZHIHU_PAGE_SETTLE_MS = 650
ZHIHU_EXAMPLE_PROFILE_URL = "https://www.zhihu.com/people/feifeimao/answers?page=2"
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


ZHIHU_ANSWER_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("answer_id", pa.string(), nullable=False),
        pa.field("author_id", pa.string(), nullable=False),
        pa.field("author_token", pa.string(), nullable=False),
        pa.field("author_name", pa.string(), nullable=False),
        pa.field("profile_url", pa.string(), nullable=False),
        pa.field("question_id", pa.string(), nullable=False),
        pa.field("question_title", pa.string(), nullable=False),
        pa.field("question_url", pa.string(), nullable=False),
        pa.field("answer_url", pa.string(), nullable=False),
        pa.field("excerpt", pa.string(), nullable=False),
        pa.field("content_text", pa.string(), nullable=False),
        pa.field("content_html", pa.string(), nullable=False),
        pa.field("content_available", pa.bool_(), nullable=False),
        pa.field("media_urls", pa.list_(pa.string()), nullable=False),
        pa.field("content_need_truncated", pa.bool_(), nullable=False),
        pa.field("force_login_when_click_read_more", pa.bool_(), nullable=False),
        pa.field("is_collapsed", pa.bool_(), nullable=False),
        pa.field("is_normal", pa.bool_(), nullable=True),
        pa.field("collapse_reason", pa.string(), nullable=True),
        pa.field("created_at", pa.string(), nullable=False),
        pa.field("updated_at", pa.string(), nullable=False),
        pa.field("voteup_count", pa.int64(), nullable=True),
        pa.field("comment_count", pa.int64(), nullable=True),
        pa.field("content_sha256", pa.string(), nullable=False),
        pa.field("first_seen_at", pa.string(), nullable=False),
        pa.field("last_seen_at", pa.string(), nullable=False),
    ]
)

ZHIHU_ANSWER_SUMMARY_COLUMNS = (
    "schema_version",
    "answer_id",
    "author_token",
    "author_name",
    "question_title",
    "answer_url",
    "created_at",
    "updated_at",
    "voteup_count",
    "comment_count",
)
_ZHIHU_METADATA_PREFIX = "agenticcontext.zhihu."


def _archive_metadata_key(name: str) -> bytes:
    return f"{_ZHIHU_METADATA_PREFIX}{name}".encode("ascii")


def _archive_metadata_int(schema: pa.Schema, name: str, default: int) -> int:
    raw_value = (schema.metadata or {}).get(_archive_metadata_key(name))
    if raw_value is None:
        return default
    try:
        value = int(raw_value.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"Zhihu answer archive has invalid {name} metadata.") from exc
    if value < 0:
        raise RuntimeError(f"Zhihu answer archive has invalid {name} metadata.")
    return value


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
class ZhihuArchiveWriteResult:
    """Counts produced by one verified complete archive replacement."""

    cached_answers: int
    added: int
    changed: int
    removed: int
    unchanged: int


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


def zhihu_archive_path(
    beta_store_root: Path | str,
    profile: ZhihuProfile | str,
    *,
    expected_beta_store_root: Path | str | None = None,
) -> Path:
    """Return the isolated, traversal-safe Parquet path for one answerer."""

    author_token = profile.author_token if isinstance(profile, ZhihuProfile) else str(profile)
    if not _ZHIHU_TOKEN_RE.fullmatch(author_token):
        raise ValueError("The Zhihu profile name is not valid.")
    configured_root = Path(beta_store_root).expanduser().absolute()
    if configured_root.is_symlink():
        raise ZhihuArchiveError("Zhihu answer storage refused a symlinked Beta store root.")
    resolved_root = configured_root.resolve(strict=False)
    if expected_beta_store_root is not None:
        expected_root = Path(expected_beta_store_root).expanduser().absolute()
        if resolved_root != expected_root:
            raise ZhihuArchiveError(
                "Zhihu answer storage root changed after task admission."
            )
    zhihu_directory = resolved_root / "zhihu"
    author_directory = zhihu_directory / author_token
    archive_path = author_directory / "answers.parquet"
    if any(path.is_symlink() for path in (zhihu_directory, author_directory, archive_path)):
        raise ZhihuArchiveError("Zhihu answer storage refused a symlinked archive path.")
    resolved_author_directory = author_directory.resolve(strict=False)
    if (
        resolved_author_directory != resolved_root
        and resolved_root not in resolved_author_directory.parents
    ):
        raise ZhihuArchiveError("Zhihu answer storage escaped the configured Beta store.")
    return archive_path


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


class ZhihuAnswerStore:
    """Own one answerer's complete, atomically replaced Parquet archive."""

    def __init__(
        self,
        beta_store_root: Path | str,
        profile: ZhihuProfile,
        *,
        expected_beta_store_root: Path | str | None = None,
    ) -> None:
        self._configured_beta_store_root = Path(beta_store_root).expanduser().absolute()
        self._beta_store_root = (
            self._configured_beta_store_root.resolve(strict=False)
            if expected_beta_store_root is None
            else Path(expected_beta_store_root).expanduser().absolute()
        )
        self.profile = profile
        self.path = zhihu_archive_path(
            self._configured_beta_store_root,
            profile,
            expected_beta_store_root=self._beta_store_root,
        )
        rows = read_parquet_rows(self.path)
        if self.path.exists() and rows is None:
            raise RuntimeError(f"Zhihu answer archive is unreadable: {self.path}")
        self._rows = self._validate_rows(rows or [])

    def _validate_rows(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        validated: dict[str, dict[str, Any]] = {}
        for row in rows:
            answer_id = str(row.get("answer_id") or "")
            if (
                not set(ZHIHU_ANSWER_SCHEMA.names).issubset(row)
                or int(row.get("schema_version") or 0) != ZHIHU_ANSWER_SCHEMA_VERSION
                or not answer_id.isdigit()
                or str(row.get("author_token") or "") != self.profile.author_token
                or answer_id in validated
            ):
                raise RuntimeError(f"Zhihu answer archive has an incompatible row: {self.path}")
            validated[answer_id] = dict(row)
        return validated

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Return deterministic newest-first archive rows."""

        return sorted(
            self._rows.values(),
            key=lambda row: (
                str(row.get("created_at") or ""),
                int(row.get("answer_id") or 0),
            ),
            reverse=True,
        )

    @property
    def cached_answers(self) -> int:
        return len(self._rows)

    @staticmethod
    def _browser_summary(row: Mapping[str, Any]) -> dict[str, Any]:
        """Return fields needed to locate one locally cached answer."""

        return {
            "answer_id": str(row.get("answer_id") or ""),
            "question_title": str(row.get("question_title") or "")[:500],
            "answer_url": str(row.get("answer_url") or ""),
            "excerpt": str(row.get("excerpt") or "")[:1_000],
            "content_available": bool(row.get("content_available")),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "voteup_count": (
                None if row.get("voteup_count") is None else int(row["voteup_count"])
            ),
            "comment_count": (
                None if row.get("comment_count") is None else int(row["comment_count"])
            ),
        }

    def browse(
        self,
        *,
        query: str = "",
        page: int = 1,
        page_size: int = ZHIHU_ARCHIVE_BROWSER_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Search and paginate the complete local archive without returning bodies."""

        normalized_query = str(query or "").strip()
        if len(normalized_query) > ZHIHU_ARCHIVE_SEARCH_LIMIT:
            raise ValueError(
                f"Search text must be at most {ZHIHU_ARCHIVE_SEARCH_LIMIT:,} characters."
            )
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError("Archive page must be a positive integer.")
        if (
            not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or page_size < 1
            or page_size > ZHIHU_ARCHIVE_BROWSER_PAGE_SIZE_LIMIT
        ):
            raise ValueError(
                "Archive page size must be between 1 and "
                f"{ZHIHU_ARCHIVE_BROWSER_PAGE_SIZE_LIMIT:,}."
            )

        needle = normalized_query.casefold()
        answer_id_match = re.search(r"(?:^|/)answer/(\d+)(?:$|[/?#])", normalized_query)
        if answer_id_match:
            needle = answer_id_match.group(1)

        def matches(row: Mapping[str, Any]) -> bool:
            if not needle:
                return True
            searchable = "\n".join(
                str(row.get(field) or "")
                for field in (
                    "answer_id",
                    "question_id",
                    "question_title",
                    "answer_url",
                    "excerpt",
                    "content_text",
                )
            ).casefold()
            return needle in searchable

        matched = [row for row in self.rows if matches(row)]
        total = len(matched)
        page_count = max(1, (total + page_size - 1) // page_size)
        bounded_page = min(page, page_count)
        start = (bounded_page - 1) * page_size
        items = matched[start : start + page_size]
        return {
            "profile_url": self.profile.answers_url,
            "author_token": self.profile.author_token,
            "query": normalized_query,
            "page": bounded_page,
            "page_size": page_size,
            "page_count": page_count,
            "total": total,
            "items": [self._browser_summary(row) for row in items],
        }

    def read_answer(self, answer_id: str) -> dict[str, Any] | None:
        """Return one complete local text record by stable Zhihu answer ID."""

        normalized_id = str(answer_id or "").strip()
        if not normalized_id.isdigit():
            raise ValueError("Answer ID must contain only decimal digits.")
        row = self._rows.get(normalized_id)
        if row is None:
            return None
        return {
            **self._browser_summary(row),
            "author_name": str(row.get("author_name") or ""),
            "author_token": str(row.get("author_token") or ""),
            "question_id": str(row.get("question_id") or ""),
            "question_url": str(row.get("question_url") or ""),
            "content_text": str(row.get("content_text") or ""),
            "content_sha256": str(row.get("content_sha256") or ""),
            "content_need_truncated": bool(row.get("content_need_truncated")),
            "is_collapsed": bool(row.get("is_collapsed")),
            "collapse_reason": str(row.get("collapse_reason") or ""),
            "first_seen_at": str(row.get("first_seen_at") or ""),
            "last_seen_at": str(row.get("last_seen_at") or ""),
        }

    @classmethod
    def load_summary(
        cls,
        beta_store_root: Path | str,
        profile: ZhihuProfile,
        *,
        recent_limit: int = 6,
        expected_beta_store_root: Path | str | None = None,
    ) -> dict[str, Any]:
        """Read only the columns required by status instead of answer bodies."""

        path = zhihu_archive_path(
            beta_store_root,
            profile,
            expected_beta_store_root=expected_beta_store_root,
        )
        empty = {
            "cache_exists": False,
            "cached_answers": 0,
            "expected_answers": None,
            "unavailable_answers": 0,
            "pages_processed": 0,
            "raw_answers": 0,
            "duplicates": 0,
            "stability_passes": 0,
            "author_name": "",
            "output_dir": str(path.parent),
            "recent_answers": [],
        }
        if not path.is_file():
            return empty
        try:
            schema = pq.read_schema(path)
            if not schema.remove_metadata().equals(ZHIHU_ANSWER_SCHEMA):
                raise RuntimeError(f"Zhihu answer archive has an incompatible schema: {path}")
            rows = pq.read_table(path, columns=list(ZHIHU_ANSWER_SUMMARY_COLUMNS)).to_pylist()
        except RuntimeError:
            raise
        except (OSError, ValueError, pa.ArrowException) as exc:
            raise RuntimeError(f"Zhihu answer archive is unreadable: {path}") from exc
        if len(rows) > ZHIHU_ANSWER_LIMIT:
            raise RuntimeError(f"Zhihu answer archive exceeds the supported record limit: {path}")

        answer_ids: set[str] = set()
        for row in rows:
            answer_id = str(row.get("answer_id") or "")
            if (
                int(row.get("schema_version") or 0) != ZHIHU_ANSWER_SCHEMA_VERSION
                or not answer_id.isdigit()
                or str(row.get("author_token") or "") != profile.author_token
                or answer_id in answer_ids
            ):
                raise RuntimeError(f"Zhihu answer archive has an incompatible row: {path}")
            answer_ids.add(answer_id)
        rows.sort(
            key=lambda row: (
                str(row.get("created_at") or ""),
                int(row.get("answer_id") or 0),
            ),
            reverse=True,
        )
        bounded_limit = max(0, min(int(recent_limit), 20))
        expected_answers = _archive_metadata_int(schema, "reported_answers", len(rows))
        unavailable_answers = _archive_metadata_int(
            schema,
            "unavailable_answers",
            max(expected_answers - len(rows), 0),
        )
        raw_answers = _archive_metadata_int(schema, "raw_answers", len(rows))
        duplicates = _archive_metadata_int(
            schema,
            "duplicate_answers",
            max(raw_answers - len(rows), 0),
        )
        if (
            expected_answers < len(rows)
            or unavailable_answers != expected_answers - len(rows)
            or raw_answers < len(rows)
            or duplicates != raw_answers - len(rows)
        ):
            raise RuntimeError(f"Zhihu answer archive has inconsistent capture metadata: {path}")
        return {
            "cache_exists": True,
            "cached_answers": len(rows),
            "expected_answers": expected_answers,
            "unavailable_answers": unavailable_answers,
            "pages_processed": _archive_metadata_int(schema, "pages_processed", 0),
            "raw_answers": raw_answers,
            "duplicates": duplicates,
            "stability_passes": _archive_metadata_int(schema, "stability_passes", 1),
            "author_name": str(rows[0].get("author_name") or profile.author_token) if rows else "",
            "output_dir": str(path.parent),
            "recent_answers": [cls._browser_summary(row) for row in rows[:bounded_limit]],
        }

    def replace_complete(
        self,
        answers: tuple[ZhihuAnswer, ...],
        captured_at: str,
        *,
        provider_reported_answers: int | None = None,
        pages_processed: int = 0,
        raw_answers: int | None = None,
        duplicates: int = 0,
        stability_passes: int = 1,
    ) -> ZhihuArchiveWriteResult:
        """Replace the complete snapshot only after collection stability checks pass."""

        previous = self._rows
        next_rows: dict[str, dict[str, Any]] = {}
        added = 0
        changed = 0
        unchanged = 0
        comparison_fields = tuple(
            name
            for name in ZHIHU_ANSWER_SCHEMA.names
            if name not in {"first_seen_at", "last_seen_at"}
        )
        for answer in answers:
            prior = previous.get(answer.answer_id)
            row = {
                "schema_version": ZHIHU_ANSWER_SCHEMA_VERSION,
                "answer_id": answer.answer_id,
                "author_id": answer.author_id,
                "author_token": answer.author_token,
                "author_name": answer.author_name,
                "profile_url": answer.profile_url,
                "question_id": answer.question_id,
                "question_title": answer.question_title,
                "question_url": answer.question_url,
                "answer_url": answer.answer_url,
                "excerpt": answer.excerpt,
                "content_text": answer.content_text,
                "content_html": answer.content_html,
                "content_available": answer.content_available,
                "media_urls": list(answer.media_urls),
                "content_need_truncated": answer.content_need_truncated,
                "force_login_when_click_read_more": answer.force_login_when_click_read_more,
                "is_collapsed": answer.is_collapsed,
                "is_normal": answer.is_normal,
                "collapse_reason": answer.collapse_reason,
                "created_at": answer.created_at,
                "updated_at": answer.updated_at,
                "voteup_count": answer.voteup_count,
                "comment_count": answer.comment_count,
                "content_sha256": answer.content_sha256,
                "first_seen_at": str(prior.get("first_seen_at") or captured_at) if prior else captured_at,
                "last_seen_at": captured_at,
            }
            if prior is None:
                added += 1
            elif all(prior.get(field) == row.get(field) for field in comparison_fields):
                unchanged += 1
            else:
                changed += 1
            next_rows[answer.answer_id] = row
        removed = len(set(previous).difference(next_rows))
        reported_answers = (
            len(next_rows)
            if provider_reported_answers is None
            else int(provider_reported_answers)
        )
        captured_raw_answers = len(next_rows) if raw_answers is None else int(raw_answers)
        unavailable_answers = reported_answers - len(next_rows)
        if (
            reported_answers < len(next_rows)
            or captured_raw_answers < len(next_rows)
            or int(duplicates) != captured_raw_answers - len(next_rows)
            or int(pages_processed) < 0
            or int(stability_passes) not in {1, 2}
            or (unavailable_answers > 0 and int(stability_passes) != 2)
        ):
            raise ValueError("Zhihu capture metadata is inconsistent with the answer snapshot.")
        archive_schema = ZHIHU_ANSWER_SCHEMA.with_metadata(
            {
                _archive_metadata_key("reported_answers"): str(reported_answers).encode("ascii"),
                _archive_metadata_key("unavailable_answers"): str(unavailable_answers).encode("ascii"),
                _archive_metadata_key("pages_processed"): str(int(pages_processed)).encode("ascii"),
                _archive_metadata_key("raw_answers"): str(captured_raw_answers).encode("ascii"),
                _archive_metadata_key("duplicate_answers"): str(int(duplicates)).encode("ascii"),
                _archive_metadata_key("stability_passes"): str(int(stability_passes)).encode("ascii"),
                _archive_metadata_key("captured_at"): captured_at.encode("utf-8"),
            }
        )
        write_path = zhihu_archive_path(
            self._configured_beta_store_root,
            self.profile,
            expected_beta_store_root=self._beta_store_root,
        )
        if write_path != self.path:
            raise ZhihuArchiveError("Zhihu answer storage path changed before publication.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if zhihu_archive_path(
            self._configured_beta_store_root,
            self.profile,
            expected_beta_store_root=self._beta_store_root,
        ) != self.path:
            raise ZhihuArchiveError("Zhihu answer storage path changed during publication.")
        descriptor, candidate_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".candidate",
            dir=self.path.parent,
        )
        os.close(descriptor)
        candidate_path = Path(candidate_name)
        candidate_path.unlink()
        try:
            write_parquet_rows_atomic(candidate_path, next_rows.values(), archive_schema)
            verified = read_parquet_rows(candidate_path)
            if verified is None or len(verified) != len(next_rows):
                raise RuntimeError(f"Zhihu answer archive readback failed: {self.path}")
            verified_schema = pq.read_schema(candidate_path)
            if (
                not verified_schema.remove_metadata().equals(ZHIHU_ANSWER_SCHEMA)
                or (verified_schema.metadata or {}) != (archive_schema.metadata or {})
            ):
                raise RuntimeError(
                    f"Zhihu answer archive capture metadata readback failed: {self.path}"
                )
            validated = self._validate_rows(verified)
            if zhihu_archive_path(
                self._configured_beta_store_root,
                self.profile,
                expected_beta_store_root=self._beta_store_root,
            ) != self.path:
                raise ZhihuArchiveError("Zhihu answer storage path changed during publication.")
            os.replace(candidate_path, self.path)
        finally:
            if candidate_path.exists():
                candidate_path.unlink()
        self._rows = validated
        return ZhihuArchiveWriteResult(
            cached_answers=len(self._rows),
            added=added,
            changed=changed,
            removed=removed,
            unchanged=unchanged,
        )

    def summary(self, *, recent_limit: int = 6) -> dict[str, Any]:
        """Return bounded metadata for the Beta status page."""

        return self.load_summary(
            self._configured_beta_store_root,
            self.profile,
            recent_limit=recent_limit,
            expected_beta_store_root=self._beta_store_root,
        )


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sync_zhihu_answers(
    state: TaskState,
    config: CrawlConfig,
    profile_url: str,
    browser_id: str,
    should_stop: Callable[[], bool],
    beta_store_root: Path | str = BETA_STORE_ROOT,
    *,
    fetch_page: Callable[[str], object] | None = None,
    begin_commit: Callable[[], bool] | None = None,
    expected_beta_store_root: Path | str | None = None,
) -> dict[str, Any]:
    """Cache one answerer's complete public answer corpus into isolated Parquet."""

    profile = normalize_zhihu_profile_url(profile_url)
    descriptor = browser_descriptors(config).get(browser_id)
    if descriptor is None or descriptor.engine != "chromium":
        raise ValueError("Zhihu answer caching requires Chrome or Edge.")
    store = ZhihuAnswerStore(
        beta_store_root,
        profile,
        expected_beta_store_root=expected_beta_store_root,
    )
    state.update(
        phase="collecting",
        account_name=profile.author_token,
        output_dir=str(store.path.parent),
        progress_unit="answers",
        downloaded_posts=store.cached_answers,
        message=f"Opening {profile.author_token}'s public Answers page in {descriptor.label}...",
    )
    state.append_event(f"Opening the public Zhihu Answers page in {descriptor.label}.")

    def update_progress(pages: int, unique: int, total: int | None, duplicates: int) -> None:
        state.update(
            phase="downloading",
            discovered_tweets=total or unique,
            queued_tweets=total or unique,
            processed_tweets=unique,
            performance_metrics={
                "pages_processed": pages,
                "duplicates": duplicates,
                "expected_answers": total,
            },
            message=(
                f"Read {unique:,} unique answers across {pages:,} pages"
                + (f" of {total:,} reported answers." if total is not None else ".")
            ),
        )

    def collect(active_fetch: Callable[[str], object]) -> ZhihuCollectionResult:
        def verify_provider_gap(gap: int) -> None:
            state.update(
                phase="verifying",
                message=(
                    f"Zhihu reports {gap:,} more answers than its pagination exposes. "
                    "Repeating the complete enumeration before publication..."
                ),
            )
            state.append_event(
                f"Repeating the complete Zhihu enumeration to verify a stable {gap:,}-record provider gap."
            )

        return collect_zhihu_answers(
            profile,
            active_fetch,
            should_stop,
            on_progress=update_progress,
            on_gap_verification=verify_provider_gap,
        )

    if fetch_page is None:
        with sync_playwright_or_error() as playwright:
            with launch_chromium_context(
                playwright,
                descriptor,
                headless=False,
                clone_profile_first=True,
                background_window=True,
            ) as context:
                page = context.pages[0] if context.pages else context.new_page()
                goto_with_retry(
                    page,
                    profile.answers_url,
                    attempts=3,
                    timeout_ms=90_000,
                    should_stop=should_stop,
                )
                if should_stop():
                    collection = ZhihuCollectionResult((), None, 0, 0, 0, True)
                else:
                    current_url = str(getattr(page, "url", "") or "")
                    body_text = page.locator("body").inner_text(timeout=10_000)
                    if "/account/unhuman" in current_url or "系统监测到您的网络环境存在异常" in body_text:
                        raise ZhihuVerificationRequiredError(
                            "Zhihu requires human verification. Open the profile in the selected browser, "
                            "complete verification manually, then retry."
                        )

                    def browser_fetch(url: str) -> object:
                        payload = _browser_fetch_json(page, url)
                        if not should_stop():
                            page.wait_for_timeout(ZHIHU_PAGE_SETTLE_MS)
                        return payload

                    collection = collect(browser_fetch)
    else:
        collection = collect(fetch_page)

    if collection.stopped or should_stop():
        return {
            "stopped": True,
            "profile": profile,
            "cached_answers": store.cached_answers,
            "expected_answers": collection.expected_total,
            "processed_answers": len(collection.answers),
            "pages_processed": collection.pages_processed,
            "duplicates": collection.duplicates,
            "unavailable_answers": 0,
            "raw_answers": collection.raw_answers,
            "stability_passes": 0,
            "added": 0,
            "changed": 0,
            "removed": 0,
            "unchanged": 0,
            "output_dir": str(store.path.parent),
        }

    state.update(
        phase="commit_pending",
        message=f"Preparing to commit {len(collection.answers):,} verified answer records...",
    )
    if should_stop():
        return {
            "stopped": True,
            "profile": profile,
            "cached_answers": store.cached_answers,
            "expected_answers": collection.expected_total,
            "processed_answers": len(collection.answers),
            "pages_processed": collection.pages_processed,
            "duplicates": collection.duplicates,
            "unavailable_answers": 0,
            "raw_answers": collection.raw_answers,
            "stability_passes": 0,
            "added": 0,
            "changed": 0,
            "removed": 0,
            "unchanged": 0,
            "output_dir": str(store.path.parent),
        }
    if begin_commit is not None:
        if not begin_commit():
            return {
                "stopped": True,
                "profile": profile,
                "cached_answers": store.cached_answers,
                "expected_answers": collection.expected_total,
                "processed_answers": len(collection.answers),
                "pages_processed": collection.pages_processed,
                "duplicates": collection.duplicates,
                "unavailable_answers": 0,
                "raw_answers": collection.raw_answers,
                "stability_passes": 0,
                "added": 0,
                "changed": 0,
                "removed": 0,
                "unchanged": 0,
                "output_dir": str(store.path.parent),
            }
    else:
        state.update(
            phase="committing",
            message=f"Writing and verifying {len(collection.answers):,} unique answer records...",
        )
    unavailable_answers = max(
        int(collection.expected_total or len(collection.answers)) - len(collection.answers),
        0,
    )
    stability_passes = 2 if unavailable_answers else 1
    write_result = store.replace_complete(
        collection.answers,
        utc_now_iso(),
        provider_reported_answers=collection.expected_total,
        pages_processed=collection.pages_processed,
        raw_answers=collection.raw_answers,
        duplicates=collection.duplicates,
        stability_passes=stability_passes,
    )
    if write_result.cached_answers != len(collection.answers):
        raise RuntimeError(
            "Zhihu archive readback did not match the verified unique records; completion was not recorded."
        )
    state.update(
        discovered_tweets=collection.expected_total or write_result.cached_answers,
        queued_tweets=collection.expected_total or write_result.cached_answers,
        processed_tweets=len(collection.answers),
        downloaded_posts=write_result.cached_answers,
        downloaded_tweets=write_result.cached_answers,
        performance_metrics={
            "pages_processed": collection.pages_processed,
            "duplicates": collection.duplicates,
            "unavailable_answers": unavailable_answers,
            "raw_answers": collection.raw_answers,
            "stability_passes": stability_passes,
            "expected_answers": collection.expected_total,
            "added": write_result.added,
            "changed": write_result.changed,
            "removed": write_result.removed,
            "unchanged": write_result.unchanged,
        },
    )
    return {
        "stopped": False,
        "profile": profile,
        "cached_answers": write_result.cached_answers,
        "expected_answers": collection.expected_total,
        "processed_answers": len(collection.answers),
        "pages_processed": collection.pages_processed,
        "duplicates": collection.duplicates,
        "unavailable_answers": unavailable_answers,
        "raw_answers": collection.raw_answers,
        "stability_passes": stability_passes,
        "added": write_result.added,
        "changed": write_result.changed,
        "removed": write_result.removed,
        "unchanged": write_result.unchanged,
        "output_dir": str(store.path.parent),
    }
