"""Flask application for the local web console."""

# Code version: v1.71.2-codex.1

from __future__ import annotations

import atexit
import json
import re
from datetime import UTC, datetime
import os
import secrets
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from html import escape as escape_html
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from flask import Flask, Response, abort, jsonify, make_response, redirect, render_template, request, send_file, session, url_for
from markdown_it import MarkdownIt
from markupsafe import Markup

from app.core.agent import (
    AGENT_ACCESS_SESSION_KEY,
    AGENT_MODEL_OPTIONS_BY_PLATFORM,
    AGENT_PLATFORM_OPTIONS,
    CAPABILITY_REGISTRY_VERSION,
    OPERATING_SYSTEM_OPTIONS as AGENT_OPERATING_SYSTEM_OPTIONS,
    AgentSourceCache,
    ComputerUseAgentService,
    ComputerUseSettingsStore,
    browser_options_for_host,
    build_agent_optimization_manifest,
    capability_registry_snapshot,
    default_model_for_platform,
    fetch_grok_conversation_history,
    is_allowed_agent_network_request,
    is_loopback_address,
    launch_terminal_authorization,
    list_agent_project_sessions,
    list_agent_sources,
    normalize_agent_conversation_url,
    normalize_agent_source_catalog_payload,
    normalize_agent_project_url,
    open_agent_in_browser,
    open_browser_for_login,
    probe_and_collect_claude_sources,
    probe_and_collect_grok_sources,
    validate_computer_use_settings,
    validate_agent_access_password,
)
from app.core.agent.session_pool import AgentSessionPool
from app.core.browser import (
    browser_descriptors,
    build_browser_options,
    open_zhihu_browser_for_login,
    probe_browser_session,
)
from app.core.foundation import (
    APP_VERSION,
    DEFAULT_HOST,
    DEFAULT_PORT,
    LOCAL_STORE_ROOT,
    MAX_CHATGPT_SCAN_WAIT_SECONDS,
    MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    MAX_DOWNLOAD_WORKERS,
    MAX_MAX_MEDIA_FILE_SIZE_MIB,
    MIN_CHATGPT_SCAN_WAIT_SECONDS,
    MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    MIN_MAX_MEDIA_FILE_SIZE_MIB,
    PRODUCT_NAME,
    SHARED_CACHE_TASK_LOCK,
    CrawlConfig,
    TaskState,
    build_initial_snapshot,
    configure_logging,
    get_log_file_path,
    is_windows_host,
    load_saved_config,
    save_config,
    utc_now,
)
from app.core.providers import (
    CacheLikesService,
    ChatGPTDownloadService,
    ClaudeHistoryService,
    GeminiHistoryService,
    GrokDownloadService,
    GrokHistoryService,
    ZhihuHistoryService,
    build_chatgpt_initial_snapshot,
    build_chatgpt_text_snapshot,
    chatgpt_history_counts,
    build_claude_initial_snapshot,
    build_gemini_initial_snapshot,
    build_grok_history_snapshot,
    build_grok_initial_snapshot,
    build_zhihu_history_initial_snapshot,
    chatgpt_conversation_id,
    fetch_chatgpt_conversation_history,
    is_chatgpt_conversation_url,
    list_chatgpt_agent_sources,
    list_chatgpt_project_sessions,
    normalize_chatgpt_conversation_url,
    probe_and_collect_chatgpt_sources,
    reset_chatgpt_state,
    reset_grok_state,
)
from app.core.storage import (
    DISPLAY_TIMEZONE,
    LocalMediaCatalog,
    ShadowBackupError,
    ShadowBackupService,
    attach_media_references,
    build_chat_history_markdown,
    choose_settings_directory,
    choose_shadow_backup_destination,
    format_captured_at_label,
    format_captured_at_timestamp_label,
    format_chat_message_timestamp_label,
    format_datetime_label,
    local_file_manager_label,
    media_route_relative_path,
    normalize_browser_filters,
    open_directory_path,
    PromptStore,
    prompt_pointer_key,
    query_chat_history,
    reveal_media_path,
    resolve_browser_media_path,
)
from app.web.beta import register_beta
from app.web.cache_sources import (
    LLM_CACHE_SOURCE_VIEWS,
    LLM_SWITCHER_SOURCE_VIEWS,
    MEDIA_CACHE_SOURCE_VIEWS,
    cache_source_views_for_page,
    get_cache_source_label,
    get_cache_source_view,
)
from app.web.navigation import build_agent_path, is_supported_agent_selection
from app.web.token_registry import (
    build_style_token_component_rows,
)


def format_agent_activity_time(value: str | None) -> str:
    """Format one Agent activity timestamp as a 24-hour UI time."""
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%H:%M:%S")


CACHE_RECONCILE_PHASES = {"idle", "finished", "completed", "success", "stopped"}
PROMPT_MARKDOWN_RENDERER = MarkdownIt(
    "default",
    {"html": False, "linkify": False, "typographer": False},
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
_STORED_HTML_VOID_TAGS = frozenset({"br", "hr"})
_STORED_HTML_SKIPPED_TAGS = frozenset({"iframe", "object", "script", "style", "svg"})
_STORED_HTML_SKIPPED_CLASSES = frozenset({"screen-reader-user-query-label"})


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

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []
        self.skipped_tags: list[str] = []

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


def sanitize_stored_html(value: str) -> str:
    """Sanitize cached rich text before marking it safe for a Jinja template."""
    source = str(value or "").replace("\x00", "").strip()
    if not source:
        return ""
    parser = _StoredHtmlSanitizer()
    parser.feed(source)
    parser.close()
    return parser.render()


def render_prompt_markdown(value: str) -> Markup:
    """Render stored ChatGPT prompt Markdown while escaping embedded HTML."""
    prompt = str(value or "").replace("\x00", "").strip()
    return Markup(PROMPT_MARKDOWN_RENDERER.render(prompt)) if prompt else Markup("")


def _render_agent_markdown(value: str) -> Markup:
    """Preserve TeX delimiters that Markdown would otherwise consume as escapes."""
    source = str(value or "").replace("\x00", "").strip()
    if not source:
        return Markup("")

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
    return Markup(rendered)


def render_agent_response(value: str) -> Markup:
    """Render live and restored final actions through the same Markdown boundary.

    The source remains untouched for copying and provenance. Only a complete
    controller final envelope is unwrapped; ordinary JSON and malformed data
    retain their original presentation.
    """
    from app.core.computer_use_agent import _render_final_action, parse_agent_action

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
            source = _render_final_action(payload)
    except (ValueError, TypeError):
        pass
    return _render_agent_markdown(source)


def render_cached_message(content_text: str, content_html: str = "") -> Markup:
    """Render one cached message from sanitized rich text or Markdown fallback."""
    rich_text = sanitize_stored_html(content_html)
    return Markup(rich_text) if rich_text else render_prompt_markdown(content_text)


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


@dataclass(frozen=True, slots=True)
class CacheRuntimeAdapter:
    """Connect one registered cache page to its task runtime."""

    state: TaskState
    service: Any
    hydrate_snapshot: Callable[[], Any]


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


def create_app(
    local_store_root: Path | str | None = None,
    *,
    beta_store_root: Path | str | None = None,
    computer_use_settings_path: Path | None = None,
    computer_use_runtime_root: Path | None = None,
    agent_external_operations_enabled: bool = True,
    beta_enabled: bool | None = None,
    beta_experiments: Iterable[str] | None = None,
) -> Flask:
    """Build and configure the Flask app."""
    configure_logging(APP_VERSION)
    effective_local_store_root = (
        Path(local_store_root).expanduser()
        if local_store_root is not None
        else LOCAL_STORE_ROOT
    )
    effective_beta_store_root = (
        Path(beta_store_root).expanduser()
        if beta_store_root is not None
        else effective_local_store_root / "beta"
    )
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent / "static"),
    )
    app.config.update(
        SECRET_KEY=(
            os.environ.get("AGENTIC_CONTEXT_SESSION_SECRET", "").strip()
            or os.environ.get("CACHELIKES_SESSION_SECRET", "").strip()
        )
        or secrets.token_urlsafe(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        AGENT_EXTERNAL_OPERATIONS_ENABLED=bool(agent_external_operations_enabled),
    )
    register_beta(
        app,
        version=APP_VERSION,
        enabled=beta_enabled,
        experiment_ids=beta_experiments,
        beta_store_root=effective_beta_store_root,
        cache_store_root=effective_local_store_root,
        task_lock=SHARED_CACHE_TASK_LOCK,
        config_provider=lambda: saved_config,
    )

    media_catalog = LocalMediaCatalog(effective_local_store_root)
    app.extensions["local_media_catalog"] = media_catalog
    prompt_store = PromptStore(media_catalog.local_store_root)
    app.extensions["prompt_store"] = prompt_store
    agent_source_cache = AgentSourceCache(media_catalog.local_store_root)
    app.extensions["agent_source_cache"] = agent_source_cache
    shadow_backup_service = ShadowBackupService(media_catalog.local_store_root)
    app.extensions["shadow_backup_service"] = shadow_backup_service
    state = TaskState(version=APP_VERSION)
    service = CacheLikesService(state, shadow_backup_service=shadow_backup_service)
    grok_state = TaskState(version=APP_VERSION, snapshot_factory=build_grok_initial_snapshot)
    grok_service = GrokDownloadService(grok_state, shadow_backup_service=shadow_backup_service)
    grok_history_state = TaskState(
        version=APP_VERSION,
        snapshot_factory=lambda version: build_grok_history_snapshot(
            version=version,
            local_store_root=media_catalog.local_store_root,
        ),
    )
    grok_history_service = GrokHistoryService(
        grok_history_state,
        media_catalog.local_store_root,
        shadow_backup_service=shadow_backup_service,
    )
    app.extensions["grok_history_service"] = grok_history_service
    chatgpt_state = TaskState(version=APP_VERSION, snapshot_factory=build_chatgpt_initial_snapshot)
    chatgpt_service = ChatGPTDownloadService(chatgpt_state, shadow_backup_service=shadow_backup_service)
    app.extensions["chatgpt_service"] = chatgpt_service
    gemini_state = TaskState(
        version=APP_VERSION,
        snapshot_factory=lambda version: build_gemini_initial_snapshot(version, media_catalog.local_store_root),
    )
    gemini_service = GeminiHistoryService(
        gemini_state,
        media_catalog.local_store_root,
        shadow_backup_service=shadow_backup_service,
    )
    app.extensions["gemini_service"] = gemini_service
    claude_state = TaskState(
        version=APP_VERSION,
        snapshot_factory=lambda version: build_claude_initial_snapshot(
            version,
            media_catalog.local_store_root,
        ),
    )
    claude_service = ClaudeHistoryService(
        claude_state,
        media_catalog.local_store_root,
        shadow_backup_service=shadow_backup_service,
    )
    app.extensions["claude_history_service"] = claude_service
    zhihu_state = TaskState(
        version=APP_VERSION,
        snapshot_factory=lambda version: build_zhihu_history_initial_snapshot(
            version,
            media_catalog.local_store_root,
        ),
    )
    zhihu_service = ZhihuHistoryService(
        zhihu_state,
        media_catalog.local_store_root,
        shadow_backup_service=shadow_backup_service,
    )
    app.extensions["zhihu_history_service"] = zhihu_service
    saved_config = load_saved_config()
    computer_use_settings = ComputerUseSettingsStore(computer_use_settings_path)
    agent_service_kwargs: dict[str, Any] = {
        "config_provider": lambda: saved_config,
    }
    if computer_use_runtime_root is not None:
        agent_service_kwargs["runtime_root"] = computer_use_runtime_root
    computer_use_agent_service = ComputerUseAgentService(
        computer_use_settings,
        **agent_service_kwargs,
    )
    app.extensions["computer_use_settings"] = computer_use_settings
    app.extensions["computer_use_agent_service"] = computer_use_agent_service
    agent_session_pool = AgentSessionPool(computer_use_agent_service)
    app.extensions["agent_session_pool"] = agent_session_pool
    atexit.register(agent_session_pool.stop_at_exit)

    def available_agent_browser_keys() -> set[str]:
        """Return Agent browsers supported by the current host."""
        return {str(option["key"]) for option in browser_options_for_host()}

    def agent_settings_for_route(browser: str, platform: str):
        """Render one canonical Agent route without mutating persisted preferences."""
        current = computer_use_settings.settings
        selected_platform = next(
            option for option in AGENT_PLATFORM_OPTIONS if option["key"] == platform
        )
        selected_models = AGENT_MODEL_OPTIONS_BY_PLATFORM[platform]
        selected_model = next(
            (option["key"] for option in selected_models if option["key"] == current.model),
            default_model_for_platform(platform),
        )
        target_url = (
            current.target_url
            if current.platform == platform
            else str(selected_platform["home_url"])
        )
        return replace(
            current,
            browser=browser,
            platform=platform,
            model=selected_model,
            target_url=target_url,
        )

    @app.template_global("agent_entry_url")
    def agent_entry_url() -> str:
        """Return the canonical Agent URL for the current route or saved selection."""
        route_args = request.view_args or {}
        browser = str(route_args.get("browser") or computer_use_settings.settings.browser).strip().lower()
        platform = str(route_args.get("platform") or computer_use_settings.settings.platform).strip().lower()
        if not is_supported_agent_selection(browser, platform) or browser not in available_agent_browser_keys():
            browser = computer_use_settings.settings.browser
            if browser not in available_agent_browser_keys():
                browser = "edge"
            platform = computer_use_settings.settings.platform
        return url_for("agent_selected", browser=browser, platform=platform)

    @app.template_global("build_agent_optimization_manifest")
    def build_agent_optimization_manifest_for_template() -> dict[str, Any]:
        """Expose the registry-derived Site manifest to the shared sidebar adapter."""
        return build_agent_optimization_manifest()

    @app.template_global("browser_media_url")
    def browser_media_url(relative_path: str) -> str:
        """Return the stable public URL for one stored media path."""
        return url_for(
            "browser_media",
            relative_path=media_route_relative_path(relative_path),
        )
    cache_runtimes = {
        "x": CacheRuntimeAdapter(
            state=state,
            service=service,
            hydrate_snapshot=lambda: build_initial_snapshot(APP_VERSION),
        ),
        "grok": CacheRuntimeAdapter(
            state=grok_state,
            service=grok_service,
            hydrate_snapshot=lambda: build_grok_initial_snapshot(APP_VERSION),
        ),
        "chatgpt": CacheRuntimeAdapter(
            state=chatgpt_state,
            service=chatgpt_service,
            hydrate_snapshot=lambda: build_chatgpt_initial_snapshot(
                APP_VERSION,
                project_name=saved_config.chatgpt_project_name,
            ),
        ),
        "gemini": CacheRuntimeAdapter(
            state=gemini_state,
            service=gemini_service,
            hydrate_snapshot=lambda: build_gemini_initial_snapshot(
                APP_VERSION,
                media_catalog.local_store_root,
            ),
        ),
        "claude": CacheRuntimeAdapter(
            state=claude_state,
            service=claude_service,
            hydrate_snapshot=lambda: build_claude_initial_snapshot(
                APP_VERSION,
                media_catalog.local_store_root,
            ),
        ),
        "zhihu": CacheRuntimeAdapter(
            state=zhihu_state,
            service=zhihu_service,
            hydrate_snapshot=lambda: build_zhihu_history_initial_snapshot(
                APP_VERSION,
                media_catalog.local_store_root,
            ),
        ),
    }

    @app.template_global("cache_source_switcher_path")
    def cache_source_switcher_path(current_source_key: str, target_source_key: str) -> str:
        """Return the canonical Cache destination for one source option."""
        target_source = get_cache_source_view(target_source_key)
        if (
            get_cache_source_view(current_source_key) is None
            or target_source is None
            or target_source.key not in cache_runtimes
        ):
            return ""
        return url_for("cache_source", source_key=target_source.key)

    @app.context_processor
    def inject_cache_source_views() -> dict[str, Any]:
        """Expose the ordered cache registry to every dock instance."""
        return {
            "cache_sources": MEDIA_CACHE_SOURCE_VIEWS,
            "llm_cache_sources": LLM_CACHE_SOURCE_VIEWS,
            "chat_history_sources": LLM_SWITCHER_SOURCE_VIEWS,
            "product_name": PRODUCT_NAME,
            "beta_enabled": "beta" in app.blueprints,
        }

    def serialize_media_item(item) -> dict[str, Any]:
        """Serialize one browser item without exposing local absolute paths."""
        media_url = (
            url_for("browser_deleted_preview", stable_id=item.stable_id)
            if item.is_deleted
            else url_for(
                "browser_media",
                relative_path=media_route_relative_path(item.relative_path),
            )
        )
        return {
            "id": item.stable_id,
            "source": item.source,
            "source_label": get_cache_source_label(item.source),
            "media_kind": item.media_kind,
            "media_kind_label": item.media_kind.title(),
            "relative_path": item.relative_path,
            "filename": item.filename,
            "title": item.title,
            "description": item.description,
            "prompt_markdown": item.prompt_markdown,
            "creator": item.creator,
            "project_name": item.project_name,
            "source_url": item.source_url,
            "resource_key": item.resource_key,
            "captured_at_label": format_captured_at_label(item.captured_at),
            "content_bytes": item.content_bytes,
            "size_label": format_media_size(item.content_bytes),
            "media_url": media_url,
            "preview_url": media_url,
            "alt_text": item.alt_text,
            "width": item.width,
            "height": item.height,
            "is_deleted": item.is_deleted,
        }

    def serialize_prompt_item(item) -> dict[str, Any]:
        """Serialize one resolved prompt without exposing duplicated storage."""
        return {
            "id": item.stable_id,
            "source": item.source,
            "source_label": get_cache_source_label(item.source),
            "conversation_id": item.conversation_id,
            "message_key": item.message_key,
            "conversation_title": item.conversation_title,
            "conversation_url": item.conversation_url,
            "author_label": item.author_label,
            "content_text": item.content_text,
            "captured_at": item.captured_at,
            "added_at": item.added_at,
            "remarks": list(item.remarks),
        }

    def build_reconciled_cache_snapshot(source_key: str, content_mode: str | None = None) -> dict[str, Any]:
        """Refresh one registered source without discarding live task status."""
        runtime = cache_runtimes.get(source_key)
        if runtime is None:
            raise KeyError(source_key)
        mode = content_mode or request.args.get("content_mode")
        if source_key == "grok" and mode == "text":
            return build_reconciled_grok_history_snapshot()
        if source_key == "chatgpt" and mode == "text":
            hydrated = asdict(build_chatgpt_text_snapshot(APP_VERSION, media_catalog.local_store_root))
            snapshot = reconcile_cached_snapshot(runtime.state.snapshot(), hydrated)
            snapshot["cached_sessions"] = hydrated["downloaded_posts"]
            snapshot["cached_messages"] = hydrated["downloaded_tweets"]
            return snapshot
        snapshot = reconcile_cached_snapshot(runtime.state.snapshot(), asdict(runtime.hydrate_snapshot()))
        if source_key == "chatgpt":
            snapshot.update(chatgpt_history_counts(media_catalog.local_store_root))
        return snapshot

    def build_reconciled_grok_snapshot() -> dict[str, Any]:
        """Refresh Grok cache counters from disk without discarding live task status."""
        return build_reconciled_cache_snapshot("grok")

    def build_reconciled_grok_history_snapshot() -> dict[str, Any]:
        """Refresh Grok text-history counters from disk without discarding live task status."""
        return reconcile_cached_snapshot(
            grok_history_state.snapshot(),
            asdict(
                build_grok_history_snapshot(
                    version=APP_VERSION,
                    local_store_root=media_catalog.local_store_root,
                )
            ),
        )

    def build_reconciled_chatgpt_snapshot() -> dict[str, Any]:
        """Refresh ChatGPT image counters from disk without discarding live task status."""
        return build_reconciled_cache_snapshot("chatgpt")

    def parse_int_field(
        field_name: str,
        fallback: int,
        minimum: int = 1,
        maximum: int | None = None,
    ) -> int:
        """Parse one integer form field while tolerating display separators."""
        raw_value = (request.form.get(field_name, str(fallback)) or str(fallback)).replace(",", "").strip()
        try:
            parsed = max(minimum, int(raw_value or fallback))
        except (TypeError, ValueError):
            parsed = max(minimum, int(fallback))
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed

    def parse_float_field(
        field_name: str,
        fallback: float,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        """Parse one float form field while tolerating display separators."""
        raw_value = (request.form.get(field_name, str(fallback)) or str(fallback)).replace(",", "").strip()
        parsed = float(raw_value or fallback)
        if minimum is not None:
            parsed = max(minimum, parsed)
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed

    def parse_checkbox_field(field_name: str, fallback: bool, preserve_missing: bool) -> bool:
        """Parse one checkbox while partial cache forms preserve unrelated settings."""
        raw_value = request.form.get(field_name)
        if raw_value is None:
            return fallback if preserve_missing else False
        return raw_value == "on"

    def parse_form_config(
        base: CrawlConfig | None = None,
        *,
        preserve_missing_booleans: bool = False,
    ) -> CrawlConfig:
        source = base or CrawlConfig()
        return CrawlConfig(
            headless=parse_checkbox_field("headless", source.headless, preserve_missing_booleans),
            download_workers=parse_int_field(
                "download_workers",
                source.download_workers,
                maximum=MAX_DOWNLOAD_WORKERS,
            ),
            max_media_file_size_mib=parse_int_field(
                "max_media_file_size_mib",
                source.max_media_file_size_mib,
                minimum=MIN_MAX_MEDIA_FILE_SIZE_MIB,
                maximum=MAX_MAX_MEDIA_FILE_SIZE_MIB,
            ),
            max_media_items=parse_int_field("max_media_items", source.max_media_items),
            max_scroll_rounds=parse_int_field("max_scroll_rounds", source.max_scroll_rounds),
            scroll_pause_seconds=parse_float_field("scroll_pause_seconds", source.scroll_pause_seconds),
            stale_round_limit=parse_int_field("stale_round_limit", source.stale_round_limit),
            x_browser=(request.form.get("x_browser", source.x_browser) or source.x_browser).strip().lower(),
            grok_browser=(request.form.get("grok_browser", source.grok_browser) or source.grok_browser).strip().lower(),
            chatgpt_browser=(request.form.get("chatgpt_browser", source.chatgpt_browser) or source.chatgpt_browser)
            .strip()
            .lower(),
            gemini_browser=(request.form.get("gemini_browser", source.gemini_browser) or source.gemini_browser)
            .strip()
            .lower(),
            claude_browser=(request.form.get("claude_browser", source.claude_browser) or source.claude_browser)
            .strip()
            .lower(),
            zhihu_browser=(request.form.get("zhihu_browser", source.zhihu_browser) or source.zhihu_browser)
            .strip()
            .lower(),
            gemini_max_conversations=parse_int_field(
                "gemini_max_conversations",
                source.gemini_max_conversations,
            ),
            gemini_scroll_pause_seconds=parse_float_field(
                "gemini_scroll_pause_seconds",
                source.gemini_scroll_pause_seconds,
                minimum=0.1,
            ),
            gemini_stale_round_limit=parse_int_field(
                "gemini_stale_round_limit",
                source.gemini_stale_round_limit,
            ),
            chatgpt_project_url=(
                request.form["chatgpt_project_url"].strip()
                if "chatgpt_project_url" in request.form
                else source.chatgpt_project_url
            ),
            chatgpt_project_name=(
                request.form.get("chatgpt_project_name", source.chatgpt_project_name) or source.chatgpt_project_name
            ).strip()
            or source.chatgpt_project_name,
            chatgpt_startup_timeout_seconds=parse_float_field(
                "chatgpt_startup_timeout_seconds",
                source.chatgpt_startup_timeout_seconds,
                minimum=MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
                maximum=MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
            ),
            chatgpt_scan_wait_seconds=parse_float_field(
                "chatgpt_scan_wait_seconds",
                source.chatgpt_scan_wait_seconds,
                minimum=MIN_CHATGPT_SCAN_WAIT_SECONDS,
                maximum=MAX_CHATGPT_SCAN_WAIT_SECONDS,
            ),
            cache_scan_waits={
                key: parse_float_field(
                    f"cache_scan_wait_{key}", source.cache_scan_wait(*key.split("_")),
                    minimum=0.0, maximum=60.0,
                )
                for key in ("chatgpt_text", "claude_text", "grok_text", "grok_media")
            },
            chatgpt_text_startup_timeout_seconds=parse_float_field(
                "chatgpt_text_startup_timeout_seconds", source.chatgpt_text_startup_timeout_seconds,
                minimum=MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
                maximum=MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
            ),
            chrome_user_data_dir=Path(
                request.form.get("chrome_user_data_dir", str(source.chrome_user_data_dir)).strip()
            ).expanduser(),
            chrome_profile_directory=request.form.get(
                "chrome_profile_directory", source.chrome_profile_directory
            ).strip()
            or source.chrome_profile_directory,
            account_name_override=request.form.get("account_name_override", source.account_name_override).strip(),
            shadow_backup_enabled=parse_checkbox_field(
                "shadow_backup_enabled",
                source.shadow_backup_enabled,
                preserve_missing_booleans,
            ),
            shadow_backup_auto_sync=parse_checkbox_field(
                "shadow_backup_auto_sync",
                source.shadow_backup_auto_sync,
                preserve_missing_booleans,
            ),
            shadow_backup_mirror_deletions=parse_checkbox_field(
                "shadow_backup_mirror_deletions",
                source.shadow_backup_mirror_deletions,
                preserve_missing_booleans,
            ),
            shadow_backup_destination=Path(
                (
                    request.form.get("shadow_backup_destination", str(source.shadow_backup_destination))
                    or str(source.shadow_backup_destination)
                ).strip()
            ).expanduser(),
        )

    def render_cache_source_page(source_key: str):
        """Render one source through the shared cache-page contract."""
        cache_source = get_cache_source_view(source_key)
        if cache_source is None or source_key not in cache_runtimes:
            abort(404)
        browser_options = build_browser_options(saved_config)
        if cache_source.supported_browsers:
            browser_options = [
                option
                for option in browser_options
                if option["id"] in cache_source.supported_browsers
            ]
        selected_browser_id = str(
            getattr(saved_config, cache_source.browser_config_field, "") or ""
        )
        selected_browser_label = next(
            (
                option["label"]
                for option in browser_options
                if option["id"] == selected_browser_id
            ),
            "Safari" if selected_browser_id == "safari" else "background browser",
        )
        return render_template(
            cache_source.template_name,
            cache_source=cache_source,
            cache_source_options=tuple(
                source
                for source in cache_source_views_for_page(source_key)
                if source.key in cache_runtimes
            ),
            snapshot=build_reconciled_cache_snapshot(source_key, request.args.get("content_mode", "text")),
            history_snapshot=(
                build_reconciled_grok_history_snapshot() if source_key == "grok" else None
            ),
            saved_config=saved_config,
            browser_options=browser_options,
            selected_browser_label=selected_browser_label,
            file_manager_label=local_file_manager_label(),
            version=APP_VERSION,
            default_host=DEFAULT_HOST,
            default_port=DEFAULT_PORT,
            log_file_path=str(get_log_file_path()),
            format_datetime_label=format_datetime_label,
        )

    def cache_source_url(source_key: str) -> str:
        """Build the canonical page URL for one registered cache source."""
        return url_for("cache_source", source_key=source_key)

    def legacy_cache_source_redirect(source_key: str):
        """Redirect a legacy cache page path while preserving its query string."""
        location = cache_source_url(source_key)
        if request.query_string:
            location = f"{location}?{request.query_string.decode('latin-1')}"
        return redirect(location)

    @app.get("/cache/<source_key>")
    def cache_source(source_key: str):
        if get_cache_source_view(source_key) is None or source_key not in cache_runtimes:
            abort(404)
        return render_cache_source_page(source_key)

    @app.get("/")
    def index():
        return legacy_cache_source_redirect("x")

    @app.get("/grok")
    def grok():
        return legacy_cache_source_redirect("grok")

    @app.get("/chatgpt")
    def chatgpt():
        return legacy_cache_source_redirect("chatgpt")

    @app.get("/gemini")
    def gemini():
        return legacy_cache_source_redirect("gemini")

    @app.get("/claude")
    def claude():
        return legacy_cache_source_redirect("claude")

    @app.get("/zhihu")
    def zhihu():
        return legacy_cache_source_redirect("zhihu")

    @app.get("/settings")
    def settings():
        grok_snapshot = build_reconciled_grok_snapshot()
        chatgpt_snapshot = build_reconciled_chatgpt_snapshot()
        return render_template(
            "settings.html",
            grok_snapshot=grok_snapshot,
            chatgpt_snapshot=chatgpt_snapshot,
            version=APP_VERSION,
            default_host=DEFAULT_HOST,
            default_port=DEFAULT_PORT,
            saved_config=saved_config,
            log_file_path=str(get_log_file_path()),
            local_store_root=str(media_catalog.local_store_root),
            shadow_backup_snapshot=shadow_backup_service.snapshot(),
            agent_settings=computer_use_settings.settings,
            agent_runtime_snapshot=computer_use_settings.snapshot(),
        )

    @app.get("/settings/style-tokens")
    def settings_style_tokens():
        return render_template(
            "settings_style_tokens.html",
            version=APP_VERSION,
            style_token_rows=build_style_token_component_rows(),
        )

    def is_agent_access_unlocked() -> bool:
        """Allow the host itself to bypass the LAN gate after validating the request network."""
        return is_loopback_address(request.remote_addr) or bool(
            session.get(AGENT_ACCESS_SESSION_KEY)
        )

    def render_agent_access_unlock(error_message: str = "", status_code: int = 200):
        """Render the no-store Agent password gate."""
        response = make_response(
            render_template(
                "agent_access_unlock.html",
                error_message=error_message,
                version=APP_VERSION,
            ),
            status_code,
        )
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    def require_local_agent_request(*, allow_locked: bool = False) -> None:
        """Keep the Agent control plane on loopback or a private network with a password gate."""
        host_name = urlsplit(f"//{request.host}").hostname
        if not is_allowed_agent_network_request(request.remote_addr, host_name):
            abort(403)
        origin = request.headers.get("Origin", "").strip()
        if origin:
            origin_parts = urlsplit(origin)
            expected_parts = urlsplit(request.host_url)
            if (
                origin_parts.scheme,
                origin_parts.hostname,
                origin_parts.port,
            ) != (
                expected_parts.scheme,
                expected_parts.hostname,
                expected_parts.port,
            ):
                abort(403)
        if not allow_locked and not is_agent_access_unlocked():
            abort(401)

    def external_agent_operations_enabled() -> bool:
        """Return whether this app instance may contact a browser or start an Agent worker."""
        return bool(app.config["AGENT_EXTERNAL_OPERATIONS_ENABLED"])

    def reject_external_agent_operation():
        """Fail closed when an isolated app instance must not touch host browser state."""
        return jsonify(
            {
                "error": (
                    "External Agent operations are disabled for this isolated application."
                )
            }
        ), 409

    def disabled_browser_session_payload(
        platform_name: str,
        browser_name: str,
    ) -> dict[str, Any]:
        """Provide a deterministic no-browser status for an explicit isolated app."""
        return {
            "platform": platform_name,
            "browser": browser_name,
            "browser_label": browser_name.title(),
            "logged_in": False,
            "can_download": False,
            "account_name": "",
            "message": "Browser session probing is disabled for this isolated application.",
            "browser_session_freshness": {
                "kind": "disabled",
                "cache_status": "disabled",
                "cached_at": "",
                "age_seconds": 0,
            },
        }

    def load_agent_source_catalog(
        *,
        platform: str,
        browser: str,
        source_kind: str,
        project_url: str = "",
        collector: Callable[[], dict[str, Any]],
        allow_live_collection: bool = True,
    ) -> dict[str, Any]:
        """Route an Agent catalog through cache without optional browser I/O."""
        if (
            allow_live_collection
            and uses_windows_debug_browser(browser)
            and agent_session_pool.has_active_worker(browser)
        ):
            allow_live_collection = False
        requested_refresh = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes"}
        is_browser_session = source_kind == "browser-session"
        if is_browser_session and platform == "chatgpt":
            # Re-probe legacy bootstrap rows once after the capability upgrade.
            project_url = "capabilities-v3"
        is_passive_source_catalog = source_kind == "sources"
        force_refresh = requested_refresh and allow_live_collection
        payload = agent_source_cache.get_or_collect(
            platform=platform,
            browser=browser,
            source_kind=source_kind,
            project_url=project_url,
            collector=collector,
            force_refresh=force_refresh,
            stale_while_revalidate=allow_live_collection and not (
                is_browser_session
                or is_passive_source_catalog
                or source_kind == "project-sessions"
                or source_kind == "session-history"
                or requested_refresh
            ),
            collect_on_miss=allow_live_collection
            and (not is_passive_source_catalog or requested_refresh),
        )
        if source_kind == "sources":
            normalized = normalize_agent_source_catalog_payload(platform, payload)
            normalized.setdefault("recent_sessions", [])
            normalized.setdefault("limit", 0)
            return normalized
        if source_kind == "project-sessions":
            normalized = normalize_agent_source_catalog_payload(platform, payload)
            normalized.setdefault("sessions", [])
            normalized.setdefault("limit", 0)
            return normalized
        return payload

    def load_agent_browser_session_bootstrap(
        *,
        platform: str,
        browser: str,
        collector: Callable[[], tuple[dict[str, Any], dict[str, Any] | None]],
        allow_live_collection: bool = True,
    ) -> dict[str, Any]:
        """Reuse one provider readiness-and-sources browser flight across Agent polls."""
        platform_label = {
            "chatgpt": "ChatGPT",
            "grok": "Grok",
            "claude": "Claude",
        }[platform]

        def collect_bootstrap() -> dict[str, Any]:
            status_payload, source_payload = collector()
            payload = dict(status_payload)
            if source_payload is not None:
                sources = normalize_agent_source_catalog_payload(platform, source_payload)
                agent_source_cache.store(
                    platform=platform,
                    browser=browser,
                    source_kind="sources",
                    payload=sources,
                )
                payload["agent_sources"] = sources
            elif payload.get("can_download"):
                payload["agent_sources_error"] = (
                    f"{platform_label} is signed in, but Recent sessions could not be loaded from this browser."
                )
            return payload

        return load_agent_source_catalog(
            platform=platform,
            browser=browser,
            source_kind="browser-session",
            collector=collect_bootstrap,
            allow_live_collection=allow_live_collection,
        )

    def uses_windows_debug_browser(browser: str) -> bool:
        """Return whether this host/browser can share one project CDP context."""
        return is_windows_host() and browser in {"edge", "chrome"}

    def selected_agent_session_id() -> str:
        return str(request.headers.get("X-CacheLikes-Agent-Session") or "primary").strip()

    def selected_agent_service():
        try:
            return agent_session_pool.get(selected_agent_session_id())
        except ValueError as exc:
            abort(404, description=str(exc))

    def build_agent_snapshot(session_id=None) -> dict[str, Any]:
        """Add safe rendered Markdown to the Agent status payload."""
        selected_id = session_id or selected_agent_session_id()
        if selected_id == "new":
            snapshot = {}
        else:
            service = agent_session_pool.get(session_id) if session_id else selected_agent_service()
            snapshot = service.snapshot()
        snapshot["session_id"] = selected_id
        snapshot["response_html"] = str(
            render_agent_response(str(snapshot.get("response", "")))
        )
        rendered_history: list[dict[str, Any]] = []
        for raw_item in snapshot.get("history", []):
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            item["response_html"] = str(
                render_agent_response(str(item.get("response", "")))
            )
            rendered_history.append(item)
        snapshot["history"] = rendered_history
        return snapshot

    def build_agent_doctor() -> dict[str, Any]:
        """Combine run diagnostics with host readiness without exposing private content."""
        doctor = selected_agent_service().doctor()
        doctor["runtime"] = computer_use_settings.snapshot()
        doctor["capability_registry_version"] = CAPABILITY_REGISTRY_VERSION
        return doctor

    def agent_snapshot_for_route(
        snapshot: dict[str, Any],
        browser: str,
        platform: str,
        workspace_path: str,
    ) -> dict[str, Any]:
        """Hide a foreign snapshot while retaining only the global stop state."""
        snapshot_browser = str(snapshot.get("browser", "")).strip().lower()
        snapshot_platform = str(snapshot.get("platform", "")).strip().lower()
        snapshot_workspace = str(snapshot.get("workspace_path", "")).strip()
        route_workspace = str(workspace_path or "").strip()
        if (
            snapshot_browser == browser
            and snapshot_platform == platform
            and snapshot_workspace == route_workspace
        ):
            return snapshot
        foreign_run_active = bool(snapshot.get("running"))
        isolated = dict(snapshot)
        isolated.update(
            {
                "activity": [],
                "actual_model": "",
                "bodycheck_passed": False,
                "browser": "",
                "catalog_error": "",
                "catalog_state": "idle",
                "chatgpt_effort": "",
                "verification_passed": False,
                "conversation_url": "",
                "conversation_bound": False,
                "context_attached": False,
                "context_bytes": 0,
                "context_file": "",
                "engine": "",
                "event_chain": {
                    "version": "1.0.0",
                    "run_id": "",
                    "count": 0,
                    "state": "idle",
                    "error": "",
                    "last_event": None,
                },
                "event_chain_state": "idle",
                "event_count": 0,
                "error_traceback": "",
                "finished_at": "",
                "history": [],
                "last_error": "",
                "model_verified": False,
                "model": "",
                "last_action_id": "",
                "last_event_kind": "",
                "paused": False,
                "pause_reason": "",
                "phase": "running" if foreign_run_active else "idle",
                "message": (
                    "An Agent task is running in another project. Stop remains available here."
                    if foreign_run_active
                    else ""
                ),
                "operating_system": "",
                "platform": "",
                "project_url": "",
                "prompt": "",
                "read_only": False,
                "response": "",
                "response_html": "",
                "run_id": "",
                "run_revision": 0,
                "started_at": "",
                "session_mode": "new",
                "session_title": "",
                "session_type": "",
                "thinking_effort": "",
                "available_efforts": [],
                "effort_catalog_complete": False,
                "traditional_handoff_available": False,
                "traditional_handoff_message": "",
                "traditional_handoff_opened": False,
                "turn_count": 0,
                "workspace_path": "",
                "running": foreign_run_active,
            }
        )
        return isolated

    def render_agent_page(browser: str, platform: str):
        """Render one Agent page using the browser/provider encoded by its URL."""
        agent_settings = agent_settings_for_route(browser, platform)
        runtime_snapshot = computer_use_settings.snapshot()
        agent_snapshot = agent_snapshot_for_route(
            build_agent_snapshot(),
            browser,
            platform,
            agent_settings.workspace_path,
        )
        compute_job_snapshot = computer_use_agent_service.compute_job_status(
            agent_settings.workspace_path
        )
        return render_template(
            "agent.html",
            version=APP_VERSION,
            runtime_snapshot=runtime_snapshot,
            agent_snapshot=agent_snapshot,
            compute_job_snapshot=compute_job_snapshot,
            settings=agent_settings,
            agent_project_name=(
                Path(agent_settings.workspace_path).name or agent_settings.workspace_path
            ),
            operating_system_options=AGENT_OPERATING_SYSTEM_OPTIONS,
            browser_options=browser_options_for_host(),
            platform_options=AGENT_PLATFORM_OPTIONS,
            model_options_by_platform=AGENT_MODEL_OPTIONS_BY_PLATFORM,
            render_prompt_markdown=render_prompt_markdown,
            format_agent_activity_time=format_agent_activity_time,
        )

    @app.get("/agent")
    def agent():
        """Redirect the legacy Agent entrypoint to the canonical selection URL."""
        require_local_agent_request(allow_locked=True)
        if not is_agent_access_unlocked():
            return render_agent_access_unlock()
        settings = computer_use_settings.settings
        browser = settings.browser if settings.browser in available_agent_browser_keys() else "edge"
        return redirect(build_agent_path(browser, settings.platform))

    @app.get("/agent/<browser>/")
    def agent_browser(browser: str):
        """Keep the browser-scoped Agent URL useful while exposing provider selection."""
        require_local_agent_request(allow_locked=True)
        selected_browser = browser.strip().lower()
        if selected_browser not in available_agent_browser_keys():
            abort(404)
        platform = computer_use_settings.settings.platform
        if not is_supported_agent_selection(selected_browser, platform):
            platform = "chatgpt"
        if not is_supported_agent_selection(selected_browser, platform):
            abort(404)
        return redirect(
            url_for("agent_selected", browser=selected_browser, platform=platform),
            code=302,
        )

    @app.get("/agent/<browser>/<platform>")
    def agent_selected(browser: str, platform: str):
        """Render the Agent page for one explicit browser/provider selection."""
        require_local_agent_request(allow_locked=True)
        if (
            not is_supported_agent_selection(browser, platform)
            or browser.strip().lower() not in available_agent_browser_keys()
        ):
            abort(404)
        if not is_agent_access_unlocked():
            return render_agent_access_unlock()
        return render_agent_page(browser.strip().lower(), platform.strip().lower())

    @app.post("/agent/unlock")
    def unlock_agent():
        """Unlock the Agent control plane for the current private-network session."""
        require_local_agent_request(allow_locked=True)
        if not validate_agent_access_password(request.form.get("password")):
            return render_agent_access_unlock("The password is incorrect.", status_code=401)
        session[AGENT_ACCESS_SESSION_KEY] = True
        settings = computer_use_settings.settings
        browser = settings.browser if settings.browser in available_agent_browser_keys() else "edge"
        return redirect(
            url_for(
                "agent_selected",
                browser=browser,
                platform=settings.platform,
            ),
            code=303,
        )

    @app.get("/api/agent/status")
    def agent_status():
        require_local_agent_request()
        runtime_snapshot = computer_use_settings.snapshot()
        selected_browser = str(
            request.headers.get("X-CacheLikes-Agent-Browser")
            or runtime_snapshot.get("browser")
            or ""
        ).strip().lower()
        selected_platform = str(
            request.headers.get("X-CacheLikes-Agent-Platform")
            or runtime_snapshot.get("platform")
            or ""
        ).strip().lower()
        selected_workspace = str(
            request.headers.get("X-CacheLikes-Agent-Workspace")
            or runtime_snapshot.get("workspace_path")
            or ""
        ).strip()
        if not is_supported_agent_selection(selected_browser, selected_platform):
            selected_browser = str(runtime_snapshot.get("browser") or "edge").strip().lower()
            selected_platform = str(runtime_snapshot.get("platform") or "chatgpt").strip().lower()
            selected_workspace = str(runtime_snapshot.get("workspace_path") or "").strip()
        if selected_agent_session_id() != "new":
            try:
                agent_session_pool.get(selected_agent_session_id())
            except ValueError as exc:
                return jsonify({"error": str(exc), "code": "unknown_agent_session"}), 404
        compute_service = (
            computer_use_agent_service
            if selected_agent_session_id() == "new"
            else selected_agent_service()
        )
        return jsonify(
            {
                "runtime": runtime_snapshot,
                **agent_session_pool.catalog(selected_browser, selected_platform, selected_workspace),
                "agent": agent_snapshot_for_route(
                    build_agent_snapshot(),
                    selected_browser,
                    selected_platform,
                    selected_workspace,
                ),
                "compute_job": compute_service.compute_job_status(
                    selected_workspace
                ),
            }
        )

    @app.post("/api/agent/compute-job/stop")
    def stop_agent_compute_job():
        """Stop one durable job without stopping or resuming the Web Agent turn."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        payload = request.get_json(silent=True) or {}
        workspace_path = str(
            payload.get("workspace_path")
            or computer_use_settings.settings.workspace_path
            or ""
        ).strip()
        try:
            job = selected_agent_service().stop_compute_job(
                workspace_path,
                str(payload.get("job_id") or ""),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"compute_job": job})

    @app.get("/api/agent/capabilities")
    def agent_capabilities():
        """Return the local capability registry for human diagnostics and tests."""
        require_local_agent_request()
        return jsonify(capability_registry_snapshot())

    @app.get("/api/agent/doctor")
    def agent_doctor():
        """Return bounded Agent diagnostics and explicit recovery affordances."""
        require_local_agent_request()
        return jsonify(build_agent_doctor())

    @app.post("/api/agent/doctor/recover")
    def recover_agent_from_doctor():
        """Run one explicit local recovery action selected by the doctor UI."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        payload = request.get_json(silent=True) or {}
        try:
            recovery = selected_agent_service().recover(str(payload.get("action", "")))
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(
            {
                "recovery": recovery,
                "doctor": build_agent_doctor(),
                "runtime": computer_use_settings.snapshot(),
                "agent": build_agent_snapshot(),
            }
        )

    @app.post("/api/agent/preferences")
    def save_agent_preferences():
        require_local_agent_request()
        payload = request.get_json(silent=True) or {}
        try:
            settings = computer_use_settings.update_preferences(
                workspace_path=str(payload.get("workspace_path", "")),
                operating_system=str(payload.get("operating_system", "")),
                browser=str(payload.get("browser", "")),
                platform=str(payload.get("platform", computer_use_settings.settings.platform)),
                model=str(payload.get("model", computer_use_settings.settings.model)),
                chatgpt_effort=str(
                    payload.get(
                        "chatgpt_effort",
                        computer_use_settings.settings.chatgpt_effort,
                    )
                ),
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(
            {
                "settings": asdict(settings),
                "runtime": computer_use_settings.snapshot(),
                "agent": build_agent_snapshot(),
            }
        )

    @app.post("/api/agent/terminal-authorization")
    def open_agent_terminal_authorization():
        """Open the host-native authorization surface for Terminal or PowerShell."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        payload = request.get_json(silent=True) or {}
        try:
            result = launch_terminal_authorization(
                str(payload.get("operating_system", ""))
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(result)

    @app.post("/api/agent/open-conversation")
    def open_agent_conversation():
        """Open the current Agent Web target in the browser selected for the task."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        snapshot = selected_agent_service().snapshot()
        try:
            platform = str(snapshot.get("platform", computer_use_settings.settings.platform))
            browser = str(snapshot.get("browser", computer_use_settings.settings.browser))
            target_url = str(snapshot.get("conversation_url", ""))
            payload = request.get_json(silent=True) or {}
            background = bool(payload.get("background", True))
            result = open_agent_in_browser(
                platform,
                browser,
                target_url,
                background=background,
                config=saved_config,
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(result)

    @app.post("/api/agent/ask")
    def ask_agent():
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        payload = request.get_json(silent=True) or {}
        try:
            started_session_id = agent_session_pool.start(
                selected_agent_session_id(),
                str(payload.get("prompt", "")),
                str(payload.get("workspace_path", "")),
                saved_config,
                operating_system=str(payload.get("operating_system", "")),
                platform=str(payload.get("platform", "")),
                browser=str(payload.get("browser", "")),
                model=str(payload.get("model", "")),
                chatgpt_effort=(
                    str(payload["chatgpt_effort"])
                    if "chatgpt_effort" in payload
                    else None
                ),
                session_mode=str(payload.get("session_mode", "new")),
                conversation_url=str(payload.get("conversation_url", "")),
                project_url=str(payload.get("project_url", "")),
                session_title=str(payload.get("session_title", "")),
                read_only=bool(payload.get("read_only", False)),
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(
            {
                "runtime": computer_use_settings.snapshot(),
                "agent": build_agent_snapshot(started_session_id),
            }
        ), 202

    @app.get("/api/agent/chatgpt-sources")
    def agent_chatgpt_sources():
        """Load recent ChatGPT sessions and projects for the selected browser."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        browser_name = request.args.get("browser", "").strip().lower()
        try:
            payload = load_agent_source_catalog(
                platform="chatgpt",
                browser=browser_name,
                source_kind="sources",
                collector=lambda: {
                    **list_chatgpt_agent_sources(browser_name, saved_config, silent=True),
                    "platform": "chatgpt",
                },
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(payload)

    @app.get("/api/agent/sources")
    def agent_sources():
        """Load recent sessions for any selected Web Agent provider."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        platform = request.args.get("platform", computer_use_settings.settings.platform).strip().lower()
        browser_name = request.args.get("browser", "").strip().lower()
        try:
            payload = load_agent_source_catalog(
                platform=platform,
                browser=browser_name,
                source_kind="sources",
                collector=lambda: list_agent_sources(
                    platform,
                    browser_name,
                    saved_config,
                    silent=True,
                ),
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(payload)

    @app.get("/api/agent/chatgpt-project-sessions")
    def agent_chatgpt_project_sessions():
        """Load recent sessions for one selected ChatGPT project."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        browser_name = request.args.get("browser", "").strip().lower()
        project_url = request.args.get("project_url", "").strip()
        try:
            normalized_project_url = normalize_agent_project_url("chatgpt", project_url)
            if not normalized_project_url:
                raise ValueError(
                    "Choose a valid ChatGPT Project before loading its sessions."
                )
            payload = load_agent_source_catalog(
                platform="chatgpt",
                browser=browser_name,
                source_kind="project-sessions",
                project_url=normalized_project_url,
                collector=lambda: {
                    **list_chatgpt_project_sessions(
                        browser_name,
                        normalized_project_url,
                        saved_config,
                        silent=True,
                    ),
                    "platform": "chatgpt",
                },
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(payload)

    @app.get("/api/agent/project-sessions")
    def agent_project_sessions():
        """Load recent sessions inside one provider-neutral Agent Project."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        platform = request.args.get("platform", computer_use_settings.settings.platform).strip().lower()
        browser_name = request.args.get("browser", "").strip().lower()
        project_url = request.args.get("project_url", "").strip()
        try:
            normalized_project_url = normalize_agent_project_url(platform, project_url)
            if not normalized_project_url:
                raise ValueError(
                    "Choose a valid Agent Project before loading its sessions."
                )
            payload = load_agent_source_catalog(
                platform=platform,
                browser=browser_name,
                source_kind="project-sessions",
                project_url=normalized_project_url,
                collector=lambda: list_agent_project_sessions(
                    platform,
                    browser_name,
                    normalized_project_url,
                    saved_config,
                    silent=True,
                ),
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(payload)

    @app.get("/api/agent/chatgpt-session-history")
    def agent_chatgpt_session_history():
        """Reuse one selected ChatGPT conversation from the shared memory/Parquet cache."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        browser_name = request.args.get("browser", "").strip().lower()
        conversation_url = normalize_chatgpt_conversation_url(
            request.args.get("conversation_url", "").strip()
        )
        if not conversation_url:
            return jsonify({"error": "Choose a valid ChatGPT conversation before loading its history."}), 400
        ask_running = uses_windows_debug_browser(
            browser_name
        ) and agent_session_pool.has_active_worker(browser_name)
        try:
            payload = load_agent_source_catalog(
                platform="chatgpt",
                browser=browser_name,
                source_kind="session-history",
                project_url=conversation_url,
                collector=lambda: fetch_chatgpt_conversation_history(
                    browser_name,
                    conversation_url,
                    saved_config,
                    silent=True,
                ),
                allow_live_collection=not ask_running,
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        cache_metadata = payload.get("cache")
        if (
            ask_running
            and isinstance(cache_metadata, dict)
            and cache_metadata.get("status") == "unprobed"
        ):
            return jsonify(
                {
                    "error": (
                        "An Agent task is running in this browser. "
                        "History is unavailable until it finishes."
                    )
                }
            ), 409
        rendered_history: list[dict[str, Any]] = []
        for raw_item in payload.get("history", []):
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            item["response_html"] = str(render_agent_response(str(item.get("response", ""))))
            rendered_history.append(item)
        return jsonify(
            {
                "conversation_url": conversation_url,
                "title": str(payload.get("title") or "Untitled session"),
                "history": rendered_history,
                "limit": int(payload.get("limit") or len(rendered_history)),
                "cache": payload.get("cache", {}),
            }
        )

    @app.get("/api/agent/grok-session-history")
    def agent_grok_session_history():
        """Reuse one selected Grok conversation from the shared memory/Parquet cache."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        browser_name = request.args.get("browser", "").strip().lower()
        conversation_url = normalize_agent_conversation_url(
            "grok",
            request.args.get("conversation_url", "").strip(),
        )
        if not conversation_url:
            return jsonify({"error": "Choose a valid Grok conversation before loading its history."}), 400
        ask_running = uses_windows_debug_browser(
            browser_name
        ) and agent_session_pool.has_active_worker(browser_name)
        try:
            payload = load_agent_source_catalog(
                platform="grok",
                browser=browser_name,
                source_kind="session-history",
                project_url=conversation_url,
                collector=lambda: fetch_grok_conversation_history(
                    browser_name,
                    conversation_url,
                    saved_config,
                    silent=True,
                ),
                allow_live_collection=not ask_running,
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        cache_metadata = payload.get("cache")
        if (
            ask_running
            and isinstance(cache_metadata, dict)
            and cache_metadata.get("status") == "unprobed"
        ):
            return jsonify(
                {
                    "error": (
                        "An Agent task is running in this browser. "
                        "History is unavailable until it finishes."
                    )
                }
            ), 409
        rendered_history: list[dict[str, Any]] = []
        for raw_item in payload.get("history", []):
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            item["response_html"] = str(render_agent_response(str(item.get("response", ""))))
            rendered_history.append(item)
        return jsonify(
            {
                "conversation_url": conversation_url,
                "title": str(payload.get("title") or ""),
                "history": rendered_history,
                "limit": int(payload.get("limit") or len(rendered_history)),
                "cache": payload.get("cache", {}),
            }
        )

    @app.post("/api/agent/stop")
    def stop_agent():
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        return jsonify(
            {
                "stop_requested": selected_agent_service().request_stop(),
                "runtime": computer_use_settings.snapshot(),
                "agent": build_agent_snapshot(),
            }
        )

    @app.delete("/api/agent/session")
    def delete_failed_agent_session():
        """Dismiss one failed local task only after the UI confirms no remote record."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        payload = request.get_json(silent=True) or {}
        session_id = str(payload.get("session_id") or "").strip()
        try:
            dismissed = agent_session_pool.dismiss_failed(
                session_id,
                expected_conversation_url=str(payload.get("conversation_url") or "").strip(),
                remote_record_absent=payload.get("remote_record_absent") is True,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": "unknown_agent_session"}), 404
        except RuntimeError as exc:
            return jsonify({"error": str(exc), "code": "agent_session_not_deletable"}), 409
        return jsonify({"deleted": True, **dismissed})

    @app.get("/browser")
    def browser():
        filters = normalize_browser_filters(
            source=request.args.get("source"),
            media_kind=request.args.get("kind"),
            query=request.args.get("q"),
            sort=request.args.get("sort"),
            page=request.args.get("page"),
            session=request.args.get("session"),
            session_view=request.args.get("session_view"),
            view=request.args.get("view"),
            media_id=request.args.get("media_id"),
            session_page=request.args.get("session_page"),
            answerer=request.args.get("answerer"),
        )
        force_refresh = request.args.get("refresh") == "1"
        prompt_page = None
        saved_prompt_keys = prompt_store.saved_pointer_keys()
        if filters["view"] == "text":
            media_items = media_catalog.snapshot(force_refresh=force_refresh)
            text_page = query_chat_history(
                media_catalog.local_store_root,
                source=filters["source"],
                query=filters["q"],
                sort=filters["sort"],
                page=filters["page"],
                session_view=filters["session_view"],
                session=filters["session"],
                answerer=filters["answerer"],
            )
            filters["answerer"] = text_page.selected_answerer
            text_page = attach_media_references(
                text_page,
                media_items,
                lambda stable_id: url_for(
                    "browser",
                    view="media",
                    media_id=stable_id,
                    source="all",
                    kind="all",
                    q="",
                    sort="newest",
                ),
                lambda item: (
                    url_for("browser_deleted_preview", stable_id=item.stable_id)
                    if item.is_deleted
                    else url_for(
                        "browser_media",
                        relative_path=media_route_relative_path(item.relative_path),
                    )
                ),
            )
            all_items = ()
            media_page = None
            media_payload = []
        elif filters["view"] == "prompts":
            prompt_page = prompt_store.query(
                source=filters["source"],
                query=filters["q"],
                sort=filters["sort"],
                page=filters["page"],
            )
            all_items = ()
            media_page = None
            text_page = None
            media_payload = []
        else:
            all_items = media_catalog.snapshot(force_refresh=force_refresh)
            media_page = media_catalog.query(
                source=filters["source"],
                media_kind=filters["kind"],
                query=filters["q"],
                sort=filters["sort"],
                page=filters["page"],
                chatgpt_session_key=filters["session"],
                chatgpt_session_view=filters["session_view"],
                media_id=filters["media_id"],
            )
            text_page = None
            media_payload = [serialize_media_item(item) for item in media_page.items]
        browser_search_suggestions = build_browser_search_suggestions(
            view=filters["view"],
            media_items=media_page.items if media_page is not None else all_items,
            text_page=text_page,
            prompt_page=prompt_page,
        )
        return render_template(
            "browser.html",
            media_page=media_page,
            text_page=text_page,
            prompt_page=prompt_page,
            media_payload=media_payload,
            filters=filters,
            browser_search_suggestions=browser_search_suggestions,
            has_any_media=bool(all_items),
            has_any_text=bool(text_page and (text_page.total_count or filters["q"])),
            has_any_prompts=prompt_store.has_any(),
            saved_prompt_keys=saved_prompt_keys,
            prompt_remark_options=prompt_store.remark_options(),
            prompt_pointer_key=prompt_pointer_key,
            format_captured_at_timestamp_label=format_captured_at_timestamp_label,
            format_chat_message_timestamp_label=format_chat_message_timestamp_label,
            format_media_size=format_media_size,
            render_prompt_markdown=render_prompt_markdown,
            render_cached_message=render_cached_message,
            file_manager_label=local_file_manager_label(),
            version=APP_VERSION,
        )

    @app.get("/browser/session/<session_id>/export")
    def browser_session_export(session_id: str):
        """Download one complete session or the currently displayed session page."""
        source = request.args.get("source", "all")
        sort = request.args.get("sort", "newest")
        page_only = request.args.get("scope") == "page"
        text_page = query_chat_history(
            media_catalog.local_store_root,
            source=source,
            sort=sort,
            page=request.args.get("page", "1") if page_only else 1,
            page_size=100 if page_only else 1_000_000,
            session_view=True,
            session=session_id,
        )
        markdown = build_chat_history_markdown(
            text_page,
            message_count=len(text_page.items) if page_only else None,
        )
        if not markdown:
            abort(404)
        title = text_page.current_session.conversation_title if text_page.current_session else "session"
        filename = "".join(
            character if character.isalnum() or character in {"-", "_", " "} else "_"
            for character in str(title)
        ).strip()
        filename = "_".join(filename.split()) or "session"
        if page_only:
            filename = f"{filename}_page_{text_page.current_page}"
        ascii_filename = "".join(
            character
            if character.isascii() and (character.isalnum() or character in {"-", "_", " "})
            else "_"
            for character in filename
        ).strip()
        ascii_filename = "_".join(ascii_filename.split()) or "session"
        download_filename = f"{filename}.md"
        return Response(
            markdown,
            mimetype="text/markdown",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{ascii_filename}.md"; '
                    f"filename*=UTF-8''{quote(download_filename, safe='')}"
                )
            },
        )

    @app.get("/browser/media/<path:relative_path>")
    def browser_media(relative_path: str):
        resolved_path = resolve_browser_media_path(media_catalog.local_store_root, relative_path)
        if resolved_path is None:
            abort(404)
        return send_file(resolved_path, conditional=True, etag=True, max_age=0)

    @app.post("/api/browser/prompts")
    def add_browser_prompt():
        payload = request.get_json(silent=True) or {}
        try:
            item, created = prompt_store.add_pointer(
                source=str(payload.get("source") or ""),
                conversation_id=str(payload.get("conversation_id") or ""),
                message_key=str(payload.get("message_key") or ""),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"created": created, "item": serialize_prompt_item(item)})

    @app.post("/api/browser/prompts/<stable_id>/remarks")
    def add_browser_prompt_remark(stable_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            item, created = prompt_store.add_remark(stable_id, payload.get("remark"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(
            {
                "created": created,
                "item": serialize_prompt_item(item),
                "remark_options": prompt_store.remark_options(),
            }
        )

    @app.delete("/api/browser/prompts/<stable_id>/remarks")
    def remove_browser_prompt_remark(stable_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            item = prompt_store.remove_remark(stable_id, payload.get("remark"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(
            {
                "item": serialize_prompt_item(item),
                "remark_options": prompt_store.remark_options(),
            }
        )

    @app.get("/browser/deleted-preview/<stable_id>")
    def browser_deleted_preview(stable_id: str):
        resolved_path = media_catalog.deleted_preview_path(stable_id)
        if resolved_path is None:
            abort(404)
        return send_file(resolved_path, conditional=True, etag=True, max_age=0)

    @app.post("/api/browser/media/<stable_id>/delete")
    def delete_browser_media(stable_id: str):
        try:
            item = media_catalog.delete(stable_id)
        except KeyError:
            return jsonify({"error": "Cached media was not found."}), 404
        except FileNotFoundError:
            return jsonify({"error": "Cached media is no longer available."}), 404
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"item": serialize_media_item(item)})

    @app.post("/api/browser/media/<stable_id>/restore")
    def restore_browser_media(stable_id: str):
        try:
            item = media_catalog.restore(stable_id)
        except KeyError:
            return jsonify({"error": "Removed media was not found."}), 404
        except FileNotFoundError:
            return jsonify({"error": "The retained preview is no longer available."}), 404
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"item": serialize_media_item(item)})

    @app.post("/api/browser/media/<stable_id>/reveal")
    def reveal_browser_media(stable_id: str):
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "Local files can only be revealed from this computer."}), 403

        resolved_path = media_catalog.resolved_media_path(stable_id)
        if resolved_path is None:
            return jsonify({"error": "Cached media is no longer available."}), 404
        try:
            reveal_media_path(resolved_path)
        except OSError as exc:
            return jsonify({"error": f"Unable to open {local_file_manager_label()}: {exc}"}), 500
        return jsonify({"revealed": True, "file_manager": local_file_manager_label()})

    @app.post("/api/cache/<source_key>/output-directory/open")
    def open_cache_output_directory(source_key: str):
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "Local folders can only be opened from this computer."}), 403
        if get_cache_source_view(source_key) is None or source_key not in cache_runtimes:
            abort(404)

        output_directory = str(build_reconciled_cache_snapshot(source_key).get("output_dir") or "").strip()
        if not output_directory or output_directory == "-":
            return jsonify({"error": "The output directory is not available yet."}), 409
        try:
            open_directory_path(output_directory)
        except (OSError, ValueError) as exc:
            return jsonify({"error": f"Unable to open {local_file_manager_label()}: {exc}"}), 409
        return jsonify({"opened": True, "file_manager": local_file_manager_label()})

    @app.post("/api/browser/chatgpt/session/refresh")
    def refresh_browser_chatgpt_session():
        """Start a targeted ChatGPT refresh for one valid conversation URL."""
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        payload = request.get_json(silent=True) or {}
        conversation_url = str(payload.get("conversation_url") or "").strip()
        if not is_chatgpt_conversation_url(conversation_url):
            return jsonify({"error": "A valid ChatGPT session URL is required."}), 400

        resource_count = sum(
            item.source == "chatgpt"
            for item in media_catalog.snapshot(force_refresh=True)
        )
        session_config = replace(saved_config, chatgpt_project_url=conversation_url)
        try:
            chatgpt_service.start(session_config)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return (
            jsonify(
                {
                    "started": True,
                    "session_key": chatgpt_conversation_id(conversation_url),
                    "resource_count": resource_count,
                    "status_url": url_for("api_chatgpt_status"),
                }
            ),
            202,
        )

    def start_cache_source_runtime(source_key: str):
        """Persist shared form values and start one registered runtime."""
        nonlocal saved_config
        cache_source = get_cache_source_view(source_key)
        runtime = cache_runtimes.get(source_key)
        if cache_source is None or runtime is None:
            abort(404)
        if source_key == "grok" and request.form.get("cache_content_mode") == "text":
            return start_grok_history_runtime()
        config = parse_form_config(saved_config, preserve_missing_booleans=True)
        saved_config = config
        save_config(saved_config)
        runtime_config = config
        if cache_source.require_browser_ready:
            browser_name = getattr(runtime_config, cache_source.browser_config_field)
            descriptor = browser_descriptors(runtime_config).get(browser_name)
            if descriptor is None:
                runtime.state.finish_error(f"Unsupported {cache_source.label} browser: {browser_name}")
                return redirect(cache_source_url(source_key))
        try:
            if source_key == "chatgpt":
                content_mode = (
                    "media"
                    if request.form.get("cache_content_mode", request.form.get("chatgpt_content_mode")) == "media"
                    else "text"
                )
                runtime.service.start(runtime_config, content_mode=content_mode)
            elif source_key == "zhihu":
                runtime.service.start(
                    runtime_config,
                    author_url=request.form.get("zhihu_author_url", "").strip(),
                )
            else:
                runtime.service.start(runtime_config)
        except RuntimeError as exc:
            if runtime.service.is_running():
                runtime.state.append_event(str(exc))
                runtime.state.update(last_error=str(exc))
            else:
                runtime.state.finish_error(str(exc))
        return redirect(cache_source_url(source_key))

    def stop_cache_source_runtime(source_key: str):
        """Request a safe stop for one registered runtime."""
        if source_key == "grok" and request.form.get("cache_content_mode") == "text":
            return stop_grok_history_runtime()
        cache_source = get_cache_source_view(source_key)
        runtime = cache_runtimes.get(source_key)
        if cache_source is None or runtime is None:
            abort(404)
        runtime.service.request_stop()
        return redirect(cache_source_url(source_key))

    def start_grok_history_runtime():
        """Persist shared form values and start the Grok text-history runtime."""
        nonlocal saved_config
        config = parse_form_config(saved_config, preserve_missing_booleans=True)
        saved_config = config
        save_config(saved_config)
        browser_name = config.grok_browser
        descriptor = browser_descriptors(config).get(browser_name)
        if descriptor is None:
            grok_history_state.finish_error(f"Unsupported Grok browser: {browser_name}")
            return redirect(cache_source_url("grok"))
        try:
            grok_history_service.start(config)
        except RuntimeError as exc:
            if grok_history_service.is_running():
                grok_history_state.append_event(str(exc))
                grok_history_state.update(last_error=str(exc))
            else:
                grok_history_state.finish_error(str(exc))
        return redirect(cache_source_url("grok"))

    def stop_grok_history_runtime():
        """Request a safe stop for the Grok text-history runtime."""
        grok_history_service.request_stop()
        return redirect(cache_source_url("grok"))

    @app.post("/cache/<source_key>/start")
    def start_cache_source(source_key: str):
        return start_cache_source_runtime(source_key)

    @app.post("/cache/<source_key>/stop")
    def stop_cache_source(source_key: str):
        return stop_cache_source_runtime(source_key)

    @app.post("/start")
    def start():
        return start_cache_source_runtime("x")

    @app.post("/stop")
    def stop():
        return stop_cache_source_runtime("x")

    @app.post("/grok/start")
    def start_grok():
        return start_cache_source_runtime("grok")

    @app.post("/grok/stop")
    def stop_grok():
        return stop_cache_source_runtime("grok")

    @app.post("/cache/grok/text/start")
    def start_grok_text_history():
        return start_grok_history_runtime()

    @app.post("/cache/grok/text/stop")
    def stop_grok_text_history():
        return stop_grok_history_runtime()

    @app.post("/chatgpt/start")
    def start_chatgpt():
        return start_cache_source_runtime("chatgpt")

    @app.post("/chatgpt/stop")
    def stop_chatgpt():
        return stop_cache_source_runtime("chatgpt")

    @app.post("/gemini/start")
    def start_gemini():
        return start_cache_source_runtime("gemini")

    @app.post("/gemini/stop")
    def stop_gemini():
        return stop_cache_source_runtime("gemini")

    @app.post("/claude/start")
    def start_claude():
        return start_cache_source_runtime("claude")

    @app.post("/claude/stop")
    def stop_claude():
        return stop_cache_source_runtime("claude")

    @app.post("/chatgpt/reset")
    def reset_chatgpt():
        if chatgpt_service.is_running():
            chatgpt_state.append_event("Reset skipped because a ChatGPT sync is still running.")
            chatgpt_state.update(last_error="Cannot reset ChatGPT state while a sync is running.")
            return redirect(cache_source_url("chatgpt"))

        result = reset_chatgpt_state(project_name=saved_config.chatgpt_project_name)
        snapshot = build_chatgpt_initial_snapshot(
            APP_VERSION,
            project_name=saved_config.chatgpt_project_name,
        )
        message = (
            f"Reset ChatGPT state. Removed {result.removed_media_files} image files, "
            f"{result.removed_state_files} state files, "
            f"{result.removed_partial_files} partial files."
        )
        snapshot.message = message
        snapshot.recent_events = [f"[{utc_now()}] {message}"]
        chatgpt_state.replace_snapshot(snapshot)
        return redirect(cache_source_url("chatgpt"))

    @app.post("/grok/reset")
    def reset_grok():
        if grok_service.is_running():
            grok_state.append_event("Reset skipped because a Grok sync is still running.")
            grok_state.update(last_error="Cannot reset Grok state while a sync is running.")
            return redirect(cache_source_url("grok"))

        result = reset_grok_state()
        snapshot = build_grok_initial_snapshot(APP_VERSION)
        message = (
            f"Reset Grok state. Removed {result.removed_media_files} media files, "
            f"{result.removed_state_files} state files, "
            f"{result.removed_partial_files} partial files."
        )
        snapshot.message = message
        snapshot.recent_events = [f"[{utc_now()}] {message}"]
        grok_state.replace_snapshot(snapshot)
        return redirect(cache_source_url("grok"))

    @app.post("/settings")
    def save_settings():
        nonlocal saved_config
        saved_config = parse_form_config(saved_config)
        save_config(saved_config)
        agent_field_names = {
            "agent_operating_system",
            "agent_context_limit_mib",
            "agent_max_turns",
            "agent_command_timeout_seconds",
            "agent_macos_system_prompt",
            "agent_windows_system_prompt",
        }
        if any(request.form.get(field_name) is not None for field_name in agent_field_names):
            candidate_payload = asdict(computer_use_settings.settings)
            candidate_payload.update(
                {
                    "operating_system": request.form.get(
                        "agent_operating_system",
                        computer_use_settings.settings.operating_system,
                    ),
                    "context_limit_mib": request.form.get(
                        "agent_context_limit_mib",
                        computer_use_settings.settings.context_limit_mib,
                    ),
                    "max_turns": request.form.get(
                        "agent_max_turns",
                        computer_use_settings.settings.max_turns,
                    ),
                    "command_timeout_seconds": request.form.get(
                        "agent_command_timeout_seconds",
                        computer_use_settings.settings.command_timeout_seconds,
                    ),
                    "macos_system_prompt": request.form.get(
                        "agent_macos_system_prompt",
                        computer_use_settings.settings.macos_system_prompt,
                    ),
                    "windows_system_prompt": request.form.get(
                        "agent_windows_system_prompt",
                        computer_use_settings.settings.windows_system_prompt,
                    ),
                }
            )
            try:
                computer_use_settings.update(validate_computer_use_settings(candidate_payload))
            except (RuntimeError, ValueError):
                pass
        return redirect(url_for("settings"))

    @app.post("/settings/shadow-backup/sync")
    def start_shadow_backup_sync():
        nonlocal saved_config
        saved_config = parse_form_config(saved_config)
        save_config(saved_config)
        try:
            shadow_backup_service.start(saved_config)
        except ShadowBackupError as exc:
            shadow_backup_service.record_start_error(exc)
        return redirect(url_for("settings", _anchor="settings-cloud"))

    @app.get("/api/settings/shadow-backup/status")
    def api_shadow_backup_status():
        return jsonify(shadow_backup_service.snapshot())

    @app.post("/api/settings/shadow-backup/destination")
    def choose_shadow_backup_destination_route():
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "The folder picker is only available on the local host."}), 403

        payload = request.get_json(silent=True) or {}
        requested_initial_path = payload.get("initial_path")
        initial_value = (
            requested_initial_path.strip()
            if isinstance(requested_initial_path, str)
            else str(saved_config.shadow_backup_destination)
        )
        try:
            selected_path = choose_shadow_backup_destination(
                Path(initial_value or str(saved_config.shadow_backup_destination))
            )
        except ShadowBackupError as exc:
            return jsonify({"error": str(exc)}), 500

        if selected_path is None:
            return jsonify({"cancelled": True})
        return jsonify({"destination": str(selected_path)})

    @app.post("/api/settings/directory")
    def choose_settings_directory_route():
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "The folder picker is only available on the local host."}), 403

        directory_options = {
            "chrome_user_data_dir": (
                saved_config.chrome_user_data_dir,
                "Select Chrome user data directory",
            ),
            "shadow_backup_destination": (
                saved_config.shadow_backup_destination,
                "Select shadow cloud backup destination",
            ),
            "agent_allowed_root": (
                Path(computer_use_settings.settings.workspace_path),
                "Select local Agent project folder",
            ),
        }
        payload = request.get_json(silent=True) or {}
        field_name = payload.get("field")
        if field_name not in directory_options:
            return jsonify({"error": "Unknown Settings directory field."}), 400

        default_path, picker_prompt = directory_options[field_name]
        requested_initial_path = payload.get("initial_path")
        initial_value = (
            requested_initial_path.strip()
            if isinstance(requested_initial_path, str)
            else str(default_path)
        )
        try:
            selected_path = choose_settings_directory(
                Path(initial_value or str(default_path)),
                picker_prompt,
            )
        except ShadowBackupError as exc:
            return jsonify({"error": str(exc)}), 500

        if selected_path is None:
            return jsonify({"cancelled": True})
        return jsonify({"directory": str(selected_path)})

    @app.post("/api/settings/directory/validate")
    def validate_settings_directory_route():
        """Validate a manually-entered directory path without opening a native picker."""
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "Path validation is only available on the local host."}), 403
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path") or "").strip()
        valid, reason, resolved = validate_local_directory_path(raw_path)
        body: dict[str, Any] = {"valid": valid, "reason": reason}
        if resolved:
            body["path"] = resolved
        return jsonify(body)

    @app.post("/api/agent/resume")
    def resume_agent():
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        return jsonify(
            {
                "resume_requested": selected_agent_service().request_resume(),
                "runtime": computer_use_settings.snapshot(),
                "agent": build_agent_snapshot(),
            }
        )


    @app.get("/api/status")
    def api_status():
        return jsonify(build_reconciled_cache_snapshot("x"))

    @app.get("/api/grok/status")
    def api_grok_status():
        return jsonify(build_reconciled_cache_snapshot("grok"))

    @app.get("/api/cache/grok/text/status")
    def api_grok_text_status():
        return jsonify(build_reconciled_grok_history_snapshot())

    @app.get("/api/chatgpt/status")
    def api_chatgpt_status():
        return jsonify(build_reconciled_cache_snapshot("chatgpt"))

    @app.get("/api/gemini/status")
    def api_gemini_status():
        return jsonify(build_reconciled_cache_snapshot("gemini"))

    @app.get("/api/claude/status")
    def api_claude_status():
        return jsonify(build_reconciled_cache_snapshot("claude"))

    @app.get("/api/zhihu/status")
    def api_zhihu_status():
        return jsonify(build_reconciled_cache_snapshot("zhihu"))

    @app.get("/api/cache/<source_key>/status")
    def api_cache_status(source_key: str):
        if get_cache_source_view(source_key) is None or source_key not in cache_runtimes:
            abort(404)
        return jsonify(build_reconciled_cache_snapshot(source_key))

    @app.get("/api/browser-session")
    def api_browser_session():
        platform_name = request.args.get("platform", "").strip().lower()
        browser_name = request.args.get("browser", "").strip().lower()
        scope = request.args.get("scope", "").strip().lower()
        if scope == "agent":
            require_local_agent_request()

        def browser_session_response(payload: dict[str, Any], status_code: int = 200):
            response = jsonify(payload)
            response.status_code = status_code
            if scope == "agent":
                response.headers["Cache-Control"] = (
                    "no-store, no-cache, max-age=0, must-revalidate"
                )
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

        if not external_agent_operations_enabled():
            return browser_session_response(
                disabled_browser_session_payload(platform_name, browser_name)
            )

        agent_bootstrap_collectors = {
            "chatgpt": probe_and_collect_chatgpt_sources,
            "grok": probe_and_collect_grok_sources,
            "claude": probe_and_collect_claude_sources,
        }
        if scope == "agent" and platform_name in agent_bootstrap_collectors:
            try:
                payload = load_agent_browser_session_bootstrap(
                    platform=platform_name,
                    browser=browser_name,
                    collector=lambda: agent_bootstrap_collectors[platform_name](
                        browser_name,
                        saved_config,
                        silent=True,
                    ),
                )
            except ValueError as exc:
                return browser_session_response({"error": str(exc)}, 400)
            except RuntimeError as exc:
                return browser_session_response({"error": str(exc)}, 409)
            cache = payload.pop("cache", {})
            cache_status = str(cache.get("status", "")).strip().lower() if isinstance(cache, dict) else ""
            freshness_kind = {
                "miss": "live_browser",
                "refreshed": "live_browser",
                "hit": "server_cache",
                "stale": "stale_cache",
            }.get(cache_status, "unknown")
            payload["browser_session_freshness"] = {
                "kind": freshness_kind,
                "cache_status": cache_status,
                "cached_at": str(cache.get("cached_at", "")) if isinstance(cache, dict) else "",
                "age_seconds": (
                    max(0, int(cache.get("age_seconds", 0)))
                    if isinstance(cache, dict)
                    and str(cache.get("age_seconds", "")).strip().lstrip("-").isdigit()
                    else 0
                ),
            }
            return browser_session_response(payload)
        try:
            payload = probe_browser_session(
                platform_name,
                browser_name,
                saved_config,
                silent=scope == "agent",
                prefer_initialized_debug_profile=(
                    scope == "agent" and platform_name == "gemini"
                ),
            )
        except ValueError as exc:
            return browser_session_response({"error": str(exc)}, 400)
        except RuntimeError as exc:
            return browser_session_response({"error": str(exc)}, 409)
        return browser_session_response(payload)

    @app.post("/api/browser-session/open-login")
    def open_browser_session_login():
        """Open the selected visible browser at its platform home for sign-in."""
        require_local_agent_request()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        platform_name = str(payload.get("platform", "")).strip().lower()
        browser_name = str(payload.get("browser", "")).strip().lower()
        is_zhihu_selection = (
            platform_name == "zhihu"
            and browser_name in {"edge", "chrome"}
            and browser_name in available_agent_browser_keys()
        )
        if not is_zhihu_selection and (
            not is_supported_agent_selection(browser_name, platform_name)
            or browser_name not in available_agent_browser_keys()
        ):
            return jsonify({"error": "Unsupported browser or platform selection."}), 400
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        try:
            result = (
                open_zhihu_browser_for_login(browser_name, saved_config)
                if is_zhihu_selection
                else open_browser_for_login(platform_name, browser_name, config=saved_config)
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(result)

    return app


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
