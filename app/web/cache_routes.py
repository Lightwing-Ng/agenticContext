"""Cache routes: one registered source per page, runtime control, and status.

Every cache source shares one page contract, one start/stop contract, and one
reconciled-snapshot rule, so they are registered from one registry rather than one
route per provider. The snapshot builders are module-level because the Settings page
renders two of them; everything else is request handling for this surface.

The Safari mutual-exclusion check belongs to the Agent surface, so it arrives as a
capability instead of being reimplemented here.
"""

# Code version: v1.2.0-codex.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from app.core.agent import is_loopback_address
from app.core.browser import build_browser_options, browser_descriptors
from app.core.claude_media import build_claude_media_initial_snapshot
from app.core.foundation import (
    APP_VERSION,
    DEFAULT_HOST,
    DEFAULT_PORT,
    PRODUCT_NAME,
    TaskState,
    get_log_file_path,
    utc_now,
)
from app.core.providers import (
    build_chatgpt_initial_snapshot,
    chatgpt_conversation_id,
    build_chatgpt_text_snapshot,
    build_grok_history_snapshot,
    build_grok_initial_snapshot,
    chatgpt_history_counts,
    is_chatgpt_conversation_url,
    reset_chatgpt_state,
    reset_grok_state,
)
from app.core.storage import (
    format_datetime_label,
    local_file_manager_label,
    open_directory_path,
)
from app.core.state import build_x_text_snapshot
from app.web.cache_sources import (
    LLM_CACHE_SOURCE_VIEWS,
    LLM_SWITCHER_SOURCE_VIEWS,
    MEDIA_CACHE_SOURCE_VIEWS,
    cache_source_views_for_page,
    get_cache_source_view,
)
from app.web.config_store import SavedConfigStore
from app.web.form_config import parse_form_config
from app.web.navigation import is_supported_cache_browser, normalize_cache_content_mode
from app.web.presentation import reconcile_cached_snapshot


CACHE_BLUEPRINT_NAME = "cache"


@dataclass(frozen=True, slots=True)
class CacheRuntimeAdapter:
    """Connect one registered cache page to its task runtime."""

    state: TaskState
    service: Any
    hydrate_snapshot: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class CacheRouteContext:
    """The registered cache runtimes plus the stores and policy these routes need."""

    cache_runtimes: dict[str, CacheRuntimeAdapter]
    config_store: SavedConfigStore
    media_catalog: Any
    grok_history_state: TaskState
    grok_history_service: Any
    chatgpt_service: Any
    reject_active_safari_agent_for_cache: Callable[[str], Any]
    external_agent_operations_enabled: Callable[[], bool]
    reject_external_agent_operation: Callable[[], Any]


def build_reconciled_cache_snapshot(
    context: CacheRouteContext,
    source_key: str,
    content_mode: str | None = None,
) -> dict[str, Any]:
    """Refresh one registered source without discarding live task status."""
    runtime = context.cache_runtimes.get(source_key)
    if runtime is None:
        raise KeyError(source_key)
    mode = content_mode or request.args.get("content_mode")
    if source_key == "grok" and mode == "text":
        return build_reconciled_grok_history_snapshot(context)
    if source_key == "x":
        selected_mode = "text" if mode == "text" else "media"
        hydrated = asdict(
            build_x_text_snapshot(APP_VERSION, context.media_catalog.local_store_root)
            if selected_mode == "text" else runtime.hydrate_snapshot()
        )
        live = runtime.state.snapshot()
        live_mode = str((live.get("performance_metrics") or {}).get("content_mode") or "media")
        if live_mode != selected_mode:
            if live.get("running"):
                hydrated.update(
                    running=True,
                    phase=live["phase"],
                    started_at=live["started_at"],
                    message=f"X {live_mode} cache is running.",
                )
            return hydrated
        return reconcile_cached_snapshot(live, hydrated)
    if source_key == "chatgpt" and mode == "text":
        hydrated = asdict(build_chatgpt_text_snapshot(APP_VERSION, context.media_catalog.local_store_root))
        snapshot = reconcile_cached_snapshot(runtime.state.snapshot(), hydrated)
        snapshot["cached_sessions"] = hydrated["downloaded_posts"]
        snapshot["cached_messages"] = hydrated["downloaded_tweets"]
        return snapshot
    if source_key == "claude":
        selected_mode = "media" if mode == "media" else "text"
        hydrated = asdict(
            build_claude_media_initial_snapshot(APP_VERSION, context.media_catalog.local_store_root)
            if selected_mode == "media" else runtime.hydrate_snapshot()
        )
        live = runtime.state.snapshot()
        live_mode = str((live.get("performance_metrics") or {}).get("content_mode") or "text")
        if live_mode != selected_mode:
            if live.get("running"):
                hydrated.update(
                    running=True,
                    phase=live["phase"],
                    started_at=live["started_at"],
                    message=f"Claude {live_mode} cache is running.",
                )
            return hydrated
        return reconcile_cached_snapshot(live, hydrated)
    snapshot = reconcile_cached_snapshot(runtime.state.snapshot(), asdict(runtime.hydrate_snapshot()))
    if source_key == "chatgpt":
        snapshot.update(chatgpt_history_counts(context.media_catalog.local_store_root))
    return snapshot

def build_reconciled_grok_snapshot(context: CacheRouteContext) -> dict[str, Any]:
    """Refresh Grok cache counters from disk without discarding live task status."""
    return build_reconciled_cache_snapshot(context, "grok")

def build_reconciled_grok_history_snapshot(context: CacheRouteContext) -> dict[str, Any]:
    """Refresh Grok text-history counters from disk without discarding live task status."""
    return reconcile_cached_snapshot(
        context.grok_history_state.snapshot(),
        asdict(
            build_grok_history_snapshot(
                version=APP_VERSION,
                local_store_root=context.media_catalog.local_store_root,
            )
        ),
    )

def build_reconciled_chatgpt_snapshot(context: CacheRouteContext) -> dict[str, Any]:
    """Refresh ChatGPT image counters from disk without discarding live task status."""
    return build_reconciled_cache_snapshot(context, "chatgpt")


def cache_source_switcher_path(
    context: CacheRouteContext,
    current_source_key: str,
    target_source_key: str,
) -> str:
    """Return the canonical Cache destination for one source option."""
    target_source = get_cache_source_view(target_source_key)
    if (
        get_cache_source_view(current_source_key) is None
        or target_source is None
        or target_source.key not in context.cache_runtimes
    ):
        return ""
    view_args = request.view_args or {}
    content_mode = normalize_cache_content_mode(view_args.get("content_mode"))
    browser = str(
        view_args.get("browser")
        or getattr(context.config_store.config, target_source.browser_config_field, "")
        or ""
    ).strip().lower()
    if target_source.supported_browsers and browser not in target_source.supported_browsers:
        browser = str(
            getattr(context.config_store.config, target_source.browser_config_field, "") or ""
        ).strip().lower()
        if browser not in target_source.supported_browsers:
            browser = target_source.supported_browsers[0]
    if not target_source.show_content_mode or not is_supported_cache_browser(browser):
        return url_for(f"{CACHE_BLUEPRINT_NAME}.cache_source", source_key=target_source.key)
    return url_for(
        f"{CACHE_BLUEPRINT_NAME}.cache_source_selected",
        source_key=target_source.key,
        content_mode=content_mode,
        browser=browser,
    )

def register_cache_routes(app: Flask, context: CacheRouteContext) -> None:
    """Register the cache pages, runtime control, and status endpoints."""
    blueprint = Blueprint(CACHE_BLUEPRINT_NAME, __name__)
    app.template_global("cache_source_switcher_path")(
        lambda current_source_key, target_source_key: cache_source_switcher_path(
            context, current_source_key, target_source_key
        )
    )

    @blueprint.app_context_processor
    def inject_cache_source_views() -> dict[str, Any]:
        """Expose the ordered cache registry to every dock instance."""
        return {
            "cache_sources": MEDIA_CACHE_SOURCE_VIEWS,
            "llm_cache_sources": LLM_CACHE_SOURCE_VIEWS,
            "chat_history_sources": LLM_SWITCHER_SOURCE_VIEWS,
            "product_name": PRODUCT_NAME,
            "beta_enabled": "beta" in current_app.blueprints,
        }

    def render_cache_source_page(
        source_key: str,
        *,
        content_mode: str | None = None,
        browser: str | None = None,
    ):
        """Render one source through the shared cache-page contract."""
        cache_source = get_cache_source_view(source_key)
        if cache_source is None or source_key not in context.cache_runtimes:
            abort(404)
        browser_options = build_browser_options(context.config_store.config)
        if cache_source.supported_browsers:
            browser_options = [
                option
                for option in browser_options
                if option["id"] in cache_source.supported_browsers
            ]
        available_browser_ids = {option["id"] for option in browser_options}
        requested_browser = str(browser or "").strip().lower()
        selected_browser_id = str(
            getattr(context.config_store.config, cache_source.browser_config_field, "") or ""
        )
        if requested_browser:
            if requested_browser not in available_browser_ids:
                abort(404)
            selected_browser_id = requested_browser
        cache_content_mode = normalize_cache_content_mode(
            content_mode or request.args.get("content_mode")
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
                if source.key in context.cache_runtimes
            ),
            snapshot=build_reconciled_cache_snapshot(context, source_key, cache_content_mode),
            history_snapshot=(
                build_reconciled_grok_history_snapshot(context) if source_key == "grok" else None
            ),
            saved_config=context.config_store.config,
            browser_options=browser_options,
            selected_browser_id=selected_browser_id,
            selected_browser_label=selected_browser_label,
            cache_content_mode=cache_content_mode,
            file_manager_label=local_file_manager_label(),
            version=APP_VERSION,
            default_host=DEFAULT_HOST,
            default_port=DEFAULT_PORT,
            log_file_path=str(get_log_file_path()),
            format_datetime_label=format_datetime_label,
        )

    def cache_source_url(source_key: str) -> str:
        """Build the canonical page URL for one registered cache source."""
        cache_source = get_cache_source_view(source_key)
        if cache_source is None:
            return url_for(f"{CACHE_BLUEPRINT_NAME}.cache_source", source_key=source_key)
        view_args = request.view_args or {}
        content_mode = normalize_cache_content_mode(
            request.form.get("cache_content_mode")
            or request.form.get("chatgpt_content_mode")
            or request.args.get("content_mode")
            or view_args.get("content_mode")
        )
        browser = str(
            request.form.get(cache_source.browser_config_field)
            or view_args.get("browser")
            or getattr(context.config_store.config, cache_source.browser_config_field, "")
            or ""
        ).strip().lower()
        if cache_source.supported_browsers and browser not in cache_source.supported_browsers:
            browser = str(
                getattr(context.config_store.config, cache_source.browser_config_field, "") or ""
            ).strip().lower()
        if cache_source.show_content_mode and is_supported_cache_browser(browser):
            return url_for(
                f"{CACHE_BLUEPRINT_NAME}.cache_source_selected",
                source_key=source_key,
                content_mode=content_mode,
                browser=browser,
            )
        return url_for(f"{CACHE_BLUEPRINT_NAME}.cache_source", source_key=source_key)

    def legacy_cache_source_redirect(source_key: str):
        """Redirect a legacy cache page path while preserving its query string."""
        location = cache_source_url(source_key)
        if request.query_string:
            location = f"{location}?{request.query_string.decode('latin-1')}"
        return redirect(location)

    @blueprint.get("/cache/<source_key>")
    def cache_source(source_key: str):
        if get_cache_source_view(source_key) is None or source_key not in context.cache_runtimes:
            abort(404)
        return render_cache_source_page(source_key)

    @blueprint.get("/cache/<source_key>/<content_mode>/<browser>")
    def cache_source_selected(source_key: str, content_mode: str, browser: str):
        cache_source = get_cache_source_view(source_key)
        if (
            cache_source is None
            or source_key not in context.cache_runtimes
            or not cache_source.show_content_mode
            or content_mode not in {"text", "media"}
            or not is_supported_cache_browser(browser)
        ):
            abort(404)
        return render_cache_source_page(
            source_key,
            content_mode=content_mode,
            browser=browser,
        )

    @blueprint.get("/")
    def index():
        return legacy_cache_source_redirect("x")

    @blueprint.get("/grok")
    def grok():
        return legacy_cache_source_redirect("grok")

    @blueprint.get("/chatgpt")
    def chatgpt():
        return legacy_cache_source_redirect("chatgpt")

    @blueprint.get("/gemini")
    def gemini():
        return legacy_cache_source_redirect("gemini")

    @blueprint.get("/claude")
    def claude():
        return legacy_cache_source_redirect("claude")

    @blueprint.get("/zhihu")
    def zhihu():
        return legacy_cache_source_redirect("zhihu")


    @blueprint.post("/api/cache/<source_key>/output-directory/open")
    def open_cache_output_directory(source_key: str):
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "Local folders can only be opened from this computer."}), 403
        if get_cache_source_view(source_key) is None or source_key not in context.cache_runtimes:
            abort(404)

        output_directory = str(build_reconciled_cache_snapshot(context, source_key).get("output_dir") or "").strip()
        if not output_directory or output_directory == "-":
            return jsonify({"error": "The output directory is not available yet."}), 409
        try:
            open_directory_path(output_directory)
        except (OSError, ValueError) as exc:
            return jsonify({"error": f"Unable to open {local_file_manager_label()}: {exc}"}), 409
        return jsonify({"opened": True, "file_manager": local_file_manager_label()})


    def start_cache_source_runtime(source_key: str):
        """Persist shared form values and start one registered runtime."""
        cache_source = get_cache_source_view(source_key)
        runtime = context.cache_runtimes.get(source_key)
        if cache_source is None or runtime is None:
            abort(404)
        if source_key == "grok" and request.form.get("cache_content_mode") == "text":
            return start_grok_history_runtime()
        config = parse_form_config(context.config_store.config, preserve_missing_booleans=True)
        browser_name = getattr(config, cache_source.browser_config_field)
        busy_response = context.reject_active_safari_agent_for_cache(browser_name)
        if busy_response is not None:
            return busy_response
        context.config_store.replace(config)
        runtime_config = config
        if cache_source.require_browser_ready:
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
            elif source_key == "claude":
                content_mode = "media" if request.form.get("cache_content_mode") == "media" else "text"
                runtime.service.start(runtime_config, content_mode=content_mode)
            elif source_key == "x":
                content_mode = "text" if request.form.get("cache_content_mode") == "text" else "media"
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
        runtime = context.cache_runtimes.get(source_key)
        if cache_source is None or runtime is None:
            abort(404)
        runtime.service.request_stop()
        return redirect(cache_source_url(source_key))

    def start_grok_history_runtime():
        """Persist shared form values and start the Grok text-history runtime."""
        config = parse_form_config(context.config_store.config, preserve_missing_booleans=True)
        busy_response = context.reject_active_safari_agent_for_cache(config.grok_browser)
        if busy_response is not None:
            return busy_response
        context.config_store.replace(config)
        browser_name = config.grok_browser
        descriptor = browser_descriptors(config).get(browser_name)
        if descriptor is None:
            context.grok_history_state.finish_error(f"Unsupported Grok browser: {browser_name}")
            return redirect(cache_source_url("grok"))
        try:
            context.grok_history_service.start(config)
        except RuntimeError as exc:
            if context.grok_history_service.is_running():
                context.grok_history_state.append_event(str(exc))
                context.grok_history_state.update(last_error=str(exc))
            else:
                context.grok_history_state.finish_error(str(exc))
        return redirect(cache_source_url("grok"))

    def stop_grok_history_runtime():
        """Request a safe stop for the Grok text-history runtime."""
        context.grok_history_service.request_stop()
        return redirect(cache_source_url("grok"))

    @blueprint.post("/cache/<source_key>/start")
    def start_cache_source(source_key: str):
        return start_cache_source_runtime(source_key)

    @blueprint.post("/cache/<source_key>/stop")
    def stop_cache_source(source_key: str):
        return stop_cache_source_runtime(source_key)

    @blueprint.post("/start")
    def start():
        return start_cache_source_runtime("x")

    @blueprint.post("/stop")
    def stop():
        return stop_cache_source_runtime("x")

    @blueprint.post("/grok/start")
    def start_grok():
        return start_cache_source_runtime("grok")

    @blueprint.post("/grok/stop")
    def stop_grok():
        return stop_cache_source_runtime("grok")

    @blueprint.post("/cache/grok/text/start")
    def start_grok_text_history():
        return start_grok_history_runtime()

    @blueprint.post("/cache/grok/text/stop")
    def stop_grok_text_history():
        return stop_grok_history_runtime()

    @blueprint.post("/chatgpt/start")
    def start_chatgpt():
        return start_cache_source_runtime("chatgpt")

    @blueprint.post("/chatgpt/stop")
    def stop_chatgpt():
        return stop_cache_source_runtime("chatgpt")

    @blueprint.post("/gemini/start")
    def start_gemini():
        return start_cache_source_runtime("gemini")

    @blueprint.post("/gemini/stop")
    def stop_gemini():
        return stop_cache_source_runtime("gemini")

    @blueprint.post("/claude/start")
    def start_claude():
        return start_cache_source_runtime("claude")

    @blueprint.post("/claude/stop")
    def stop_claude():
        return stop_cache_source_runtime("claude")

    @blueprint.post("/chatgpt/reset")
    def reset_chatgpt():
        runtime = context.cache_runtimes["chatgpt"]
        if runtime.service.is_running():
            runtime.state.append_event("Reset skipped because a ChatGPT sync is still running.")
            runtime.state.update(last_error="Cannot reset ChatGPT state while a sync is running.")
            return redirect(cache_source_url("chatgpt"))

        result = reset_chatgpt_state(project_name=context.config_store.config.chatgpt_project_name)
        snapshot = build_chatgpt_initial_snapshot(
            APP_VERSION,
            project_name=context.config_store.config.chatgpt_project_name,
        )
        message = (
            f"Reset ChatGPT state. Removed {result.removed_media_files} image files, "
            f"{result.removed_state_files} state files, "
            f"{result.removed_partial_files} partial files."
        )
        snapshot.message = message
        snapshot.recent_events = [f"[{utc_now()}] {message}"]
        runtime.state.replace_snapshot(snapshot)
        return redirect(cache_source_url("chatgpt"))

    @blueprint.post("/grok/reset")
    def reset_grok():
        runtime = context.cache_runtimes["grok"]
        if runtime.service.is_running():
            runtime.state.append_event("Reset skipped because a Grok sync is still running.")
            runtime.state.update(last_error="Cannot reset Grok state while a sync is running.")
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
        runtime.state.replace_snapshot(snapshot)
        return redirect(cache_source_url("grok"))

    @blueprint.get("/api/status")
    def api_status():
        return jsonify(build_reconciled_cache_snapshot(context, "x"))

    @blueprint.get("/api/grok/status")
    def api_grok_status():
        return jsonify(build_reconciled_cache_snapshot(context, "grok"))

    @blueprint.get("/api/cache/grok/text/status")
    def api_grok_text_status():
        return jsonify(build_reconciled_grok_history_snapshot(context))

    @blueprint.get("/api/chatgpt/status")
    def api_chatgpt_status():
        return jsonify(build_reconciled_cache_snapshot(context, "chatgpt"))

    @blueprint.get("/api/gemini/status")
    def api_gemini_status():
        return jsonify(build_reconciled_cache_snapshot(context, "gemini"))

    @blueprint.get("/api/claude/status")
    def api_claude_status():
        return jsonify(build_reconciled_cache_snapshot(context, "claude"))

    @blueprint.get("/api/zhihu/status")
    def api_zhihu_status():
        return jsonify(build_reconciled_cache_snapshot(context, "zhihu"))

    @blueprint.get("/api/cache/<source_key>/status")
    def api_cache_status(source_key: str):
        if get_cache_source_view(source_key) is None or source_key not in context.cache_runtimes:
            abort(404)
        return jsonify(build_reconciled_cache_snapshot(context, source_key))

    @blueprint.post("/api/browser/chatgpt/session/refresh")
    def refresh_browser_chatgpt_session():
        """Start a targeted ChatGPT refresh for one valid conversation URL."""
        if not context.external_agent_operations_enabled():
            return context.reject_external_agent_operation()
        payload = request.get_json(silent=True) or {}
        conversation_url = str(payload.get("conversation_url") or "").strip()
        if not is_chatgpt_conversation_url(conversation_url):
            return jsonify({"error": "A valid ChatGPT session URL is required."}), 400

        busy_response = context.reject_active_safari_agent_for_cache(
            context.config_store.config.chatgpt_browser
        )
        if busy_response is not None:
            return busy_response

        resource_count = sum(
            item.source == "chatgpt"
            for item in context.media_catalog.snapshot(force_refresh=True)
        )
        session_config = replace(context.config_store.config, chatgpt_project_url=conversation_url)
        try:
            context.chatgpt_service.start(session_config)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return (
            jsonify(
                {
                    "started": True,
                    "session_key": chatgpt_conversation_id(conversation_url),
                    "resource_count": resource_count,
                    "status_url": url_for(f"{CACHE_BLUEPRINT_NAME}.api_chatgpt_status"),
                }
            ),
            202,
        )

    app.register_blueprint(blueprint)


__all__ = [
    "CACHE_BLUEPRINT_NAME",
    "CacheRouteContext",
    "CacheRuntimeAdapter",
    "build_reconciled_cache_snapshot",
    "build_reconciled_chatgpt_snapshot",
    "build_reconciled_grok_history_snapshot",
    "build_reconciled_grok_snapshot",
    "register_cache_routes",
]
