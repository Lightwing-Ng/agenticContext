"""Presentation helpers shared by every Web route module.

These are pure transformations from stored or service data into what a template or a
JSON response shows: stored-HTML sanitization, Markdown and citation rendering, search
suggestions, snapshot reconciliation, and local-directory admission. Nothing here
touches a Flask request, a service, or an application instance, so route modules can
import it directly instead of receiving it through the application factory.
"""

# Code version: v1.1.0-codex.0

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from html import escape as escape_html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markupsafe import Markup

from app.core.agent import (
    GROK_INLINE_CITATION_PATTERN,
    normalize_grok_display_markdown,
    parse_agent_action,
    render_final_agent_action,
)
from app.core.providers import normalize_zhihu_rich_text
from app.core.storage import DISPLAY_TIMEZONE
from app.web.cache_sources import (
    LLM_SWITCHER_SOURCE_VIEWS,
    MEDIA_CACHE_SOURCE_VIEWS,
    get_cache_source_label,
)


PROMPT_MARKDOWN_RENDERER = MarkdownIt(
    "default",
    {"html": False, "linkify": False, "typographer": False},
)


CACHE_RECONCILE_PHASES = {"idle", "finished", "completed", "success", "stopped"}


_EXCLUDED_SYSTEM_DIRECTORY_PREFIXES = (
    "/System",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/usr/lib",
    "/usr/libexec",
    "/Library",
    "/private/etc",
    "/private/var/log",
    "/private/var/db",
    "/private/var/root",
    "/dev",
    "/cores",
    "/proc",
)


_STORED_HTML_ALLOWED_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "code",
        "del",
        "em",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "li",
        "mark",
        "ol",
        "p",
        "pre",
        "s",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)


_STORED_HTML_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


_STORED_HTML_SKIPPED_TAGS = frozenset({"iframe", "object", "script", "style", "svg"})


_STORED_HTML_SKIPPED_CLASSES = frozenset({"screen-reader-user-query-label"})


def format_agent_activity_time(value: str | None) -> str:
    """Format one Agent activity timestamp as a 24-hour UI time."""
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%H:%M:%S")


def format_media_size(content_bytes: int) -> str:
    """Render a byte count as a compact, readable English file size."""
    size = max(0, int(content_bytes))
    if size < 1_024:
        return f"{size:,} B"
    if size < 1_024**2:
        return f"{size / 1_024:.2f} KiB"
    if size < 1_024**3:
        return f"{size / 1_024**2:.2f} MiB"
    return f"{size / 1_024**3:.2f} GiB"


def _safe_stored_html_url(value: str) -> str:
    """Keep only absolute HTTP(S) URLs from cached rich-text attributes."""
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    return candidate if parsed.scheme in {"http", "https"} and parsed.netloc else ""


class _StoredHtmlSanitizer(HTMLParser):
    """Keep harmless rich-text structure while dropping cached page chrome."""

    def __init__(self, *, replace_images: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self.skipped_tags: list[str] = []
        self.replace_images = replace_images

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.skipped_tags:
            if tag not in _STORED_HTML_VOID_TAGS:
                self.skipped_tags.append(tag)
            return
        attrs_map = dict(attrs)
        class_names = set(str(attrs_map.get("class") or "").split())
        if tag in _STORED_HTML_SKIPPED_TAGS or class_names & _STORED_HTML_SKIPPED_CLASSES:
            if tag not in _STORED_HTML_VOID_TAGS:
                self.skipped_tags.append(tag)
            return
        if tag == "img" and self.replace_images:
            self.parts.append(
                '<span class="browser-zhihu-image-placeholder" role="img" '
                'aria-label="Image omitted from cached Zhihu answer"></span>'
            )
            return
        if tag not in _STORED_HTML_ALLOWED_TAGS:
            return

        safe_attrs: list[tuple[str, str]] = []
        if tag == "a":
            href = _safe_stored_html_url(attrs_map.get("href", ""))
            if href:
                safe_attrs.append(("href", href))
            title = str(attrs_map.get("title") or "").strip()
            if title:
                safe_attrs.append(("title", title))
        elif tag == "ol":
            start = str(attrs_map.get("start") or "").strip()
            if start.isdigit():
                safe_attrs.append(("start", start))
        elif tag in {"td", "th"}:
            for name in ("colspan", "rowspan"):
                value = str(attrs_map.get(name) or "").strip()
                if value.isdigit():
                    safe_attrs.append((name, value))
        elif tag == "span":
            math_value = str(attrs_map.get("data-math") or "").strip()
            if math_value:
                safe_attrs.append(("data-math", math_value))

        serialized_attrs = "".join(
            f' {name}="{escape_html(value, quote=True)}"' for name, value in safe_attrs
        )
        self.parts.append(f"<{tag}{serialized_attrs}>")
        if tag not in _STORED_HTML_VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _STORED_HTML_VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skipped_tags:
            if tag == self.skipped_tags[-1]:
                self.skipped_tags.pop()
            return
        if tag not in self.open_tags:
            return
        while self.open_tags:
            open_tag = self.open_tags.pop()
            self.parts.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.skipped_tags:
            self.parts.append(escape_html(data))

    def handle_comment(self, _data: str) -> None:
        return

    def render(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts).strip()


def sanitize_stored_html(value: str, *, replace_images: bool = False) -> str:
    """Sanitize cached rich text before marking it safe for a Jinja template."""
    source = str(value or "").replace("\x00", "").strip()
    if not source:
        return ""
    parser = _StoredHtmlSanitizer(replace_images=replace_images)
    parser.feed(source)
    parser.close()
    return parser.render()


def render_prompt_markdown(value: str) -> Markup:
    """Render stored ChatGPT prompt Markdown while escaping embedded HTML."""
    prompt = str(value or "").replace("\x00", "").strip()
    return Markup(PROMPT_MARKDOWN_RENDERER.render(prompt)) if prompt else Markup("")


def _wrap_rendered_tables(rendered: str, *, aria_label: str) -> str:
    """Give sanitized or generated tables the shared scrollable table surface."""
    return rendered.replace(
        "<table>",
        (
            '<div class="agent-markdown-table-shell" role="region" tabindex="0" '
            f'aria-label="{escape_html(aria_label, quote=True)}"><table>'
        ),
    ).replace("</table>", "</table></div>")


def _safe_agent_citation_url(value: Any) -> str:
    """Keep only bounded absolute HTTP(S) URLs for rendered provider citations."""
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 2_048 or any(ord(char) < 32 for char in candidate):
        return ""
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return ""
    return candidate


def _agent_citation_index(citations: Iterable[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Normalize trusted fields from one provider citation mapping."""
    index: dict[str, dict[str, str]] = {}
    for raw_item in citations:
        if not isinstance(raw_item, dict):
            continue
        card_id = str(raw_item.get("card_id") or "").strip()
        citation_id = str(raw_item.get("citation_id") or "").strip()
        url = _safe_agent_citation_url(raw_item.get("url"))
        label = " ".join(str(raw_item.get("label") or "").split())[:80]
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", card_id)
            or not re.fullmatch(r"[0-9]{1,6}", citation_id)
            or not url
        ):
            continue
        index[card_id] = {
            "citation_id": citation_id,
            "url": url,
            "label": label or "Source",
        }
    return index


def _prepare_grok_response_markdown(
    value: str,
    citations: Iterable[dict[str, Any]],
) -> tuple[str, dict[str, str]]:
    """Replace Grok-only citation tags with inert tokens before Markdown parsing."""
    source = normalize_grok_display_markdown(value)
    citation_index = _agent_citation_index(citations)
    token_root = "\ue100"
    while token_root in source:
        token_root += "\ue101"
    replacements: dict[str, str] = {}

    def replace_citation(match: re.Match[str]) -> str:
        token = f"{token_root}{len(replacements)}{token_root}"
        citation_id = match.group("citation_id")
        citation = citation_index.get(match.group("card_id"))
        if citation is None or citation["citation_id"] != citation_id:
            safe_id = escape_html(citation_id, quote=True)
            replacements[token] = (
                '<span class="agent-inline-citation agent-inline-citation--unresolved" '
                f'aria-label="Source citation {safe_id} unavailable">Source {safe_id}</span>'
            )
            return token
        url = escape_html(citation["url"], quote=True)
        label = escape_html(citation["label"])
        replacements[token] = (
            f'<a class="agent-inline-citation" href="{url}" target="_blank" '
            f'rel="noopener noreferrer nofollow" aria-label="Open citation from {label}">'
            f"{label}</a>"
        )
        return token

    return GROK_INLINE_CITATION_PATTERN.sub(replace_citation, source), replacements


def _render_agent_markdown(
    value: str,
    *,
    provider: str = "",
    citations: Iterable[dict[str, Any]] = (),
) -> Markup:
    """Preserve TeX delimiters that Markdown would otherwise consume as escapes."""
    source = str(value or "").replace("\x00", "").strip()
    if not source:
        return Markup("")

    inline_replacements: dict[str, str] = {}
    if str(provider or "").strip().lower() == "grok":
        source, inline_replacements = _prepare_grok_response_markdown(source, citations)

    sentinel = "\ue000"
    while sentinel in source:
        sentinel += "\ue000"
    replacements = {
        rf"{sentinel}0{sentinel}": r"\[",
        rf"{sentinel}1{sentinel}": r"\]",
        rf"{sentinel}2{sentinel}": r"\(",
        rf"{sentinel}3{sentinel}": r"\)",
    }
    protected = source
    for replacement, delimiter in replacements.items():
        protected = protected.replace(delimiter, replacement)
    rendered = PROMPT_MARKDOWN_RENDERER.render(protected)
    for replacement, delimiter in replacements.items():
        rendered = rendered.replace(replacement, delimiter)
    for replacement, citation_html in inline_replacements.items():
        rendered = rendered.replace(replacement, citation_html)
    return Markup(_wrap_rendered_tables(rendered, aria_label="Scrollable answer table"))


def render_agent_response(
    value: str,
    *,
    provider: str = "",
    citations: Iterable[dict[str, Any]] = (),
) -> Markup:
    """Render live and restored final actions through the same Markdown boundary.

    The payload source remains untouched for provenance. Only a complete controller
    final envelope is unwrapped; ordinary JSON and malformed data retain their
    original presentation.
    """
    source = str(value or "").strip()
    candidate = source
    try:
        # History exports may wrap the entire fenced message in a JSON string.
        # Decode only complete wrappers; never repair partial controller output.
        for _ in range(2):
            if not candidate.startswith('"'):
                break
            decoded = json.loads(candidate)
            if not isinstance(decoded, str):
                break
            candidate = decoded.strip()
        fence = re.fullmatch(
            r"```(?:json(?:[ \t]+[^\r\n]*)?)?[ \t]*\r?\n(.*?)\r?\n```",
            candidate,
            re.IGNORECASE | re.DOTALL,
        )
        if fence:
            candidate = fence.group(1).strip()
        # Unwrap only a complete envelope, not an example inside ordinary prose.
        json.loads(candidate)
        payload = parse_agent_action(candidate)
        if (
            isinstance(payload, dict)
            and payload.get("action") == "final"
            and isinstance(payload.get("summary"), str)
            and payload["summary"].strip()
        ):
            source = render_final_agent_action(payload)
    except (ValueError, TypeError):
        pass
    return _render_agent_markdown(source, provider=provider, citations=citations)


def render_agent_response_copy_text(
    value: str,
    *,
    provider: str = "",
    citations: Iterable[dict[str, Any]] = (),
) -> str:
    """Return copyable Markdown without exposing provider-only rendering tags."""
    source = str(value or "").replace("\x00", "").strip()
    if str(provider or "").strip().lower() != "grok":
        return source
    source = normalize_grok_display_markdown(source)
    citation_index = _agent_citation_index(citations)

    def replace_citation(match: re.Match[str]) -> str:
        citation_id = match.group("citation_id")
        citation = citation_index.get(match.group("card_id"))
        if citation is None or citation["citation_id"] != citation_id:
            return f"[Source {citation_id}]"
        return f"[{citation['label']}]({citation['url']})"

    return GROK_INLINE_CITATION_PATTERN.sub(replace_citation, source)


def render_cached_message(
    content_text: str,
    content_html: str = "",
    *,
    replace_images: bool = False,
) -> Markup:
    """Render one cached message from sanitized rich text or Markdown fallback."""
    source_html = (
        normalize_zhihu_rich_text(content_html).content_html
        if replace_images and content_html
        else content_html
    )
    rich_text = sanitize_stored_html(source_html, replace_images=replace_images)
    rendered = rich_text if rich_text else str(render_prompt_markdown(content_text))
    return Markup(_wrap_rendered_tables(rendered, aria_label="Scrollable message table"))


def build_browser_search_suggestions(
    *,
    view: str,
    media_items: Iterable[Any] = (),
    text_page: Any = None,
    prompt_page: Any = None,
) -> tuple[dict[str, str], ...]:
    """Build bounded, local-only search recommendations for the browser heading."""
    normalized_view = str(view or "").strip().lower()
    is_text_view = normalized_view == "text"
    is_prompt_view = normalized_view == "prompts"
    source_views = (
        LLM_SWITCHER_SOURCE_VIEWS
        if is_text_view or is_prompt_view
        else MEDIA_CACHE_SOURCE_VIEWS
    )
    suggestions: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(value: Any, detail: str) -> None:
        normalized = " ".join(str(value or "").split()).strip()[:120]
        if not normalized:
            return
        key = normalized.casefold()
        if key in seen:
            return
        seen.add(key)
        suggestions.append({"value": normalized, "detail": detail})

    for source in source_views:
        add(
            source.label,
            "Prompt source" if is_prompt_view else ("Chat source" if is_text_view else "Media source"),
        )

    if is_text_view and text_page is not None:
        sessions = [
            getattr(text_page, attribute, None)
            for attribute in ("current_session", "previous_session", "next_session")
        ]
        sessions.extend(getattr(text_page, "sessions", ()) or ())
        for session in sessions:
            if session is None:
                continue
            add(
                getattr(session, "conversation_title", ""),
                f"{get_cache_source_label(getattr(session, 'source', ''))} session",
            )
        for message in getattr(text_page, "items", ()) or ():
            add(
                getattr(message, "conversation_title", ""),
                f"{get_cache_source_label(getattr(message, 'source', ''))} session",
            )
    elif is_prompt_view and prompt_page is not None:
        for prompt in getattr(prompt_page, "items", ()) or ():
            source_label = get_cache_source_label(getattr(prompt, "source", ""))
            add(getattr(prompt, "conversation_title", ""), f"{source_label} session")
            add(getattr(prompt, "content_text", ""), f"{source_label} prompt")
    else:
        for item in media_items:
            source_label = get_cache_source_label(getattr(item, "source", ""))
            media_kind = str(getattr(item, "media_kind", "") or "media").title()
            detail = f"{source_label} · {media_kind}"
            add(getattr(item, "title", ""), detail)
            add(getattr(item, "filename", ""), detail)
            add(getattr(item, "creator", ""), f"{source_label} creator")

    return tuple(suggestions[:96])


def reconcile_cached_snapshot(snapshot: dict[str, Any], hydrated_payload: dict[str, Any]) -> dict[str, Any]:
    """Refresh persisted cache counters only for stable non-error task states."""
    if snapshot.get("running") or snapshot.get("phase") not in CACHE_RECONCILE_PHASES:
        return snapshot

    is_idle = snapshot.get("phase") == "idle"
    snapshot["account_name"] = hydrated_payload["account_name"]
    snapshot["output_dir"] = hydrated_payload["output_dir"]
    snapshot["downloaded_posts"] = hydrated_payload["downloaded_posts"]
    snapshot["downloaded_tweets"] = hydrated_payload["downloaded_tweets"]
    if "discovered_images" in hydrated_payload and (
        is_idle or "discovered_images" not in snapshot
    ):
        snapshot["discovered_images"] = hydrated_payload["discovered_images"]
    snapshot["downloaded_images"] = hydrated_payload["downloaded_images"]
    snapshot["downloaded_videos"] = hydrated_payload["downloaded_videos"]
    if is_idle:
        snapshot["message"] = hydrated_payload["message"]
    return snapshot


def is_excluded_system_directory(path: Path) -> bool:
    """Return whether a resolved path is a protected system directory."""
    posix = path.as_posix()
    if posix in {"/", "/usr"}:
        return True
    if len(posix) >= 2 and posix[1] == ":":
        drive_path = posix[2:].lower()
        if drive_path == "/windows" or drive_path.startswith("/windows/"):
            return True
        if drive_path == "/program files" or drive_path.startswith("/program files/"):
            return True
        if drive_path == "/program files (x86)" or drive_path.startswith("/program files (x86)/"):
            return True
    return any(
        posix == prefix or posix.startswith(prefix + "/")
        for prefix in _EXCLUDED_SYSTEM_DIRECTORY_PREFIXES
    )


def validate_local_directory_path(raw_path: str) -> tuple[bool, str, str]:
    """Validate an absolute, readable, non-system directory after symlink resolution."""
    candidate_text = str(raw_path or "").strip()
    if not candidate_text:
        return False, "No path provided.", ""
    candidate = Path(candidate_text).expanduser()
    if not candidate.is_absolute():
        return False, "The path must be absolute.", ""
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        return False, str(exc)[:200], ""
    if is_excluded_system_directory(resolved):
        return False, "System directories cannot be selected.", ""
    if not resolved.exists():
        return False, "The path does not exist.", ""
    if not resolved.is_dir():
        return False, "The path is not a directory.", ""
    try:
        resolved.iterdir().__next__()
    except StopIteration:
        pass
    except PermissionError:
        return False, "Permission denied.", ""
    except OSError as exc:
        return False, str(exc)[:200], ""
    return True, "", str(resolved)


__all__ = [
    "CACHE_RECONCILE_PHASES",
    "PROMPT_MARKDOWN_RENDERER",
    "build_browser_search_suggestions",
    "format_agent_activity_time",
    "format_media_size",
    "is_excluded_system_directory",
    "reconcile_cached_snapshot",
    "render_agent_response",
    "render_agent_response_copy_text",
    "render_cached_message",
    "render_prompt_markdown",
    "sanitize_stored_html",
    "validate_local_directory_path",
]
