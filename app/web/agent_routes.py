"""Agent routes: the Computer Use surface, its access gate, and its session APIs.

This module owns everything the Agent page and its APIs need: the LAN password gate
built on the application-wide request checks, source-catalog loading, snapshot and
history presentation, and the exclusive-browser arbitration that keeps a running Agent
task and a cache worker off the same Safari.

``register_agent_routes`` returns an :class:`AgentSurface`, which is how the Tunnel,
Jury, and Cache route modules borrow those few Agent capabilities without importing
this module's internals or duplicating the gate.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
import re
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import quote, urlsplit

from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.core.agent import (
    AGENT_ACCESS_SESSION_KEY,
    AGENT_MODEL_OPTIONS_BY_PLATFORM,
    AGENT_PLATFORM_OPTIONS,
    CAPABILITY_REGISTRY_VERSION,
    GROK_AGENT_HISTORY_RENDER_CONTRACT,
    OPERATING_SYSTEM_OPTIONS as AGENT_OPERATING_SYSTEM_OPTIONS,
    SUPPORTED_AGENT_PLATFORMS,
    SUPPORTED_SAFARI_AGENT_EXECUTION_PLATFORMS,
    TUNNEL_SUPPORTED_PLATFORMS,
    AgentSourceCache,
    ComputerUseSettingsStore,
    agent_access_password_is_configured,
    agent_execution_blocked_message,
    browser_options_for_host,
    build_agent_optimization_manifest,
    capability_registry_snapshot,
    default_model_for_platform,
    fetch_grok_conversation_history,
    is_agent_execution_supported,
    launch_terminal_authorization,
    list_agent_project_sessions,
    list_agent_sources,
    normalize_agent_conversation_url,
    normalize_agent_project_url,
    normalize_agent_source_catalog_payload,
    normalize_grok_display_markdown,
    open_agent_in_browser,
    open_browser_for_login,
    probe_and_collect_claude_sources,
    probe_and_collect_gemini_sources,
    probe_and_collect_grok_sources,
    validate_agent_access_password,
)
from app.core.browser import open_zhihu_browser_for_login, probe_browser_session
from app.core.foundation import APP_VERSION, is_macos_host, is_windows_host
from app.core.providers import (
    fetch_chatgpt_conversation_history,
    humanize_agent_history_prompts,
    list_chatgpt_agent_sources,
    list_chatgpt_project_sessions,
    normalize_chatgpt_conversation_url,
    probe_and_collect_chatgpt_sources,
)
from app.web.cache_routes import CacheRuntimeAdapter
from app.web.cache_sources import get_cache_source_view
from app.web.config_store import SavedConfigStore
from app.web.navigation import is_supported_agent_selection
from app.web.presentation import (
    format_agent_activity_time,
    render_agent_response,
    render_agent_response_copy_text,
    render_prompt_markdown,
)
from app.web.tunnel_routes import TUNNEL_BLUEPRINT_NAME, tunnel_status_payload


AGENT_BLUEPRINT_NAME = "agent"
AGENT_UNLOCK_FAILURE_LIMIT = 5
AGENT_UNLOCK_FAILURE_WINDOW_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class AgentRouteContext:
    """The Agent surface's collaborators, including the app-wide request checks.

    The three request checks come from the factory because they guard every route,
    not only this surface; the Agent gate composes them with its password rule.
    """

    config_store: SavedConfigStore
    computer_use_settings: ComputerUseSettingsStore
    computer_use_agent_service: Any
    agent_session_pool: Any
    agent_source_cache: AgentSourceCache
    grok_history_service: Any
    cache_runtimes: dict[str, CacheRuntimeAdapter]
    tunnel_runtime: Any
    tunnel_mcp_service: Any
    require_trusted_request_network: Callable[[], None]
    require_safe_request_origin: Callable[[], None]
    is_agent_access_unlocked: Callable[[], bool]


@dataclass(frozen=True, slots=True)
class AgentSurface:
    """The Agent capabilities the other route modules share.

    Keeping these in one returned value is what lets the Tunnel, Jury, and Cache
    blueprints reuse one access gate and one exclusive-browser rule instead of
    each growing its own copy.
    """

    require_local_agent_request: Callable[..., None]
    is_agent_access_unlocked: Callable[[], bool]
    render_locked_agent_access: Callable[[], Any]
    render_agent_page: Callable[..., Any]
    available_agent_browser_keys: Callable[[], set[str]]
    external_agent_operations_enabled: Callable[[], bool]
    reject_external_agent_operation: Callable[[], Any]
    reject_active_safari_agent_for_cache: Callable[[str], Any]


def register_agent_routes(app: Flask, context: AgentRouteContext) -> AgentSurface:
    """Register the Agent pages and APIs and return the surface other modules share."""
    blueprint = Blueprint(AGENT_BLUEPRINT_NAME, __name__)
    agent_unlock_failure_lock = Lock()
    agent_unlock_failures: dict[str, list[float]] = {}

    def available_agent_browser_keys() -> set[str]:
        """Return Agent browsers supported by the current host."""
        return {str(option["key"]) for option in browser_options_for_host()}

    def agent_settings_for_route(browser: str, platform: str):
        """Render one canonical Agent route without mutating persisted preferences."""
        current = context.computer_use_settings.settings
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

    def agent_entry_url() -> str:
        """Return the current Agent URL, defaulting new entries to Tunnel."""
        route_args = request.view_args or {}
        platform = str(route_args.get("platform") or context.computer_use_settings.settings.platform).strip().lower()
        if platform not in SUPPORTED_AGENT_PLATFORMS:
            platform = "chatgpt"
        if request.endpoint == f"{AGENT_BLUEPRINT_NAME}.agent_selected":
            browser = str(route_args.get("browser") or context.computer_use_settings.settings.browser).strip().lower()
            if not is_supported_agent_selection(browser, platform) or browser not in available_agent_browser_keys():
                browser = context.computer_use_settings.settings.browser
                if browser not in available_agent_browser_keys():
                    browser = "edge"
            return url_for(f"{AGENT_BLUEPRINT_NAME}.agent_selected", browser=browser, platform=platform)
        return url_for(f"{TUNNEL_BLUEPRINT_NAME}.agent_tunnel_selected", platform=platform)

    def build_agent_optimization_manifest_for_template() -> dict[str, Any]:
        """Expose the registry-derived Site manifest to the shared sidebar adapter."""
        return build_agent_optimization_manifest()

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

    def render_locked_agent_access():
        """Fail closed on LAN when no valid password was explicitly configured."""
        if not agent_access_password_is_configured():
            return render_agent_access_unlock(
                (
                    "LAN access is disabled. Set "
                    "AGENTIC_CONTEXT_AGENT_PASSWORD to exactly six ASCII digits, then restart."
                ),
                status_code=503,
            )
        return render_agent_access_unlock()

    def agent_unlock_retry_after_seconds() -> int:
        """Return the remaining per-client cooldown after repeated failed unlocks."""
        now = monotonic()
        threshold = now - AGENT_UNLOCK_FAILURE_WINDOW_SECONDS
        client_key = str(request.remote_addr or "")
        with agent_unlock_failure_lock:
            recent_failures = [
                attempted_at
                for attempted_at in agent_unlock_failures.get(client_key, [])
                if attempted_at > threshold
            ]
            if recent_failures:
                agent_unlock_failures[client_key] = recent_failures
            else:
                agent_unlock_failures.pop(client_key, None)
            if len(recent_failures) < AGENT_UNLOCK_FAILURE_LIMIT:
                return 0
            remaining = AGENT_UNLOCK_FAILURE_WINDOW_SECONDS - (
                now - recent_failures[0]
            )
            return max(1, int(remaining) + 1)

    def record_agent_unlock_failure() -> None:
        """Record one failed password attempt against the network peer."""
        client_key = str(request.remote_addr or "")
        now = monotonic()
        threshold = now - AGENT_UNLOCK_FAILURE_WINDOW_SECONDS
        with agent_unlock_failure_lock:
            recent_failures = [
                attempted_at
                for attempted_at in agent_unlock_failures.get(client_key, [])
                if attempted_at > threshold
            ]
            recent_failures.append(now)
            agent_unlock_failures[client_key] = recent_failures

    def clear_agent_unlock_failures() -> None:
        """Forget the peer's failed attempts after a successful unlock."""
        client_key = str(request.remote_addr or "")
        with agent_unlock_failure_lock:
            agent_unlock_failures.pop(client_key, None)

    def require_local_agent_request(*, allow_locked: bool = False) -> None:
        """Keep the Agent control plane on loopback or a private network with a password gate."""
        context.require_trusted_request_network()
        context.require_safe_request_origin()
        if not allow_locked and not context.is_agent_access_unlocked():
            abort(401)

    def external_agent_operations_enabled() -> bool:
        """Return whether this app instance may contact a browser or start an Agent worker."""
        return bool(current_app.config["AGENT_EXTERNAL_OPERATIONS_ENABLED"])

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
            and uses_exclusive_agent_browser(browser)
            and context.agent_session_pool.has_active_worker(browser)
        ):
            allow_live_collection = False
        requested_refresh = request.args.get("refresh", "").strip().lower() in {"1", "true", "yes"}
        is_browser_session = source_kind == "browser-session"
        if is_browser_session and platform == "chatgpt":
            # Re-probe legacy bootstrap rows once after the capability upgrade.
            project_url = "capabilities-v3"
        is_passive_source_catalog = source_kind == "sources"
        force_refresh = requested_refresh and allow_live_collection
        payload = context.agent_source_cache.get_or_collect(
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
            "gemini": "Gemini",
            "grok": "Grok",
            "claude": "Claude",
        }[platform]

        def collect_bootstrap() -> dict[str, Any]:
            status_payload, source_payload = collector()
            payload = dict(status_payload)
            if source_payload is not None:
                sources = normalize_agent_source_catalog_payload(platform, source_payload)
                context.agent_source_cache.store(
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
        selected = str(browser or "").strip().lower()
        if selected not in {"edge", "chrome"}:
            return False
        if is_windows_host():
            return True
        return is_macos_host() and selected == "edge"

    def uses_exclusive_agent_browser(browser: str) -> bool:
        """Return whether live probes would contend with an active Agent browser."""
        return uses_windows_debug_browser(browser) or (
            is_macos_host() and browser == "safari"
        )

    def active_safari_cache_consumer() -> str:
        """Return the active Cache source that currently owns Safari."""
        for source_key, runtime in context.cache_runtimes.items():
            cache_source = get_cache_source_view(source_key)
            if (
                cache_source is not None
                and runtime.service.is_running()
                and str(
                    getattr(context.config_store.config, cache_source.browser_config_field, "")
                    or ""
                ).strip().lower()
                == "safari"
            ):
                return cache_source.label
        if (
            context.grok_history_service.is_running()
            and str(context.config_store.config.grok_browser).strip().lower() == "safari"
        ):
            return "Grok history"
        return ""

    def reject_active_safari_agent_for_cache(browser: str):
        """Reject a Cache start that would wait behind the Safari Agent lock."""
        if (
            str(browser or "").strip().lower() == "safari"
            and context.agent_session_pool.has_active_worker("safari")
        ):
            return (
                jsonify(
                    {
                        "error": (
                            "Safari is busy with an active Agent task. Stop it or "
                            "wait for it to finish before starting this Cache task."
                        ),
                        "code": "safari_agent_busy",
                    }
                ),
                409,
            )
        return None

    def selected_agent_session_id() -> str:
        return str(request.headers.get("X-CacheLikes-Agent-Session") or "primary").strip()

    def selected_agent_service():
        try:
            return context.agent_session_pool.get(selected_agent_session_id())
        except ValueError as exc:
            abort(404, description=str(exc))

    def requested_agent_route(
        runtime_snapshot: dict[str, Any],
    ) -> tuple[str, str, str]:
        """Return the validated browser, provider, and workspace for this request."""
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
            selected_browser = str(
                runtime_snapshot.get("browser") or "edge"
            ).strip().lower()
            selected_platform = str(
                runtime_snapshot.get("platform") or "chatgpt"
            ).strip().lower()
            selected_workspace = str(
                runtime_snapshot.get("workspace_path") or ""
            ).strip()
        return selected_browser, selected_platform, selected_workspace

    def render_agent_history_item(
        raw_item: dict[str, Any],
        *,
        provider: str,
    ) -> dict[str, Any]:
        """Add a safe display/copy projection while retaining provider provenance."""
        item = dict(raw_item)
        response = str(item.get("response", ""))
        citations = item.get("citations")
        citation_rows = citations if isinstance(citations, list) else []
        display_response = (
            str(item.get("display_response") or normalize_grok_display_markdown(response))
            if provider == "grok"
            else response
        )
        item["response_html"] = str(
            render_agent_response(
                display_response,
                provider=provider,
                citations=citation_rows,
            )
        )
        if provider == "grok":
            item["display_response"] = display_response
            item["response_copy_text"] = render_agent_response_copy_text(
                display_response,
                provider=provider,
                citations=citation_rows,
            )
        return item

    def build_agent_snapshot(session_id=None) -> dict[str, Any]:
        """Add safe rendered Markdown to the Agent status payload."""
        selected_id = session_id or selected_agent_session_id()
        if selected_id == "new":
            snapshot = {}
        else:
            service = context.agent_session_pool.get(session_id) if session_id else selected_agent_service()
            snapshot = service.snapshot()
        snapshot["session_id"] = selected_id
        provider = str(snapshot.get("platform") or "").strip().lower()
        rendered_snapshot = render_agent_history_item(snapshot, provider=provider)
        snapshot["response_html"] = rendered_snapshot["response_html"]
        if provider == "grok":
            snapshot["display_response"] = rendered_snapshot["display_response"]
            snapshot["response_copy_text"] = rendered_snapshot["response_copy_text"]
        rendered_history: list[dict[str, Any]] = []
        for raw_item in snapshot.get("history", []):
            if not isinstance(raw_item, dict):
                continue
            rendered_history.append(
                render_agent_history_item(raw_item, provider=provider)
            )
        snapshot["history"] = rendered_history
        return snapshot

    def build_agent_doctor() -> dict[str, Any]:
        """Combine run diagnostics with host readiness without exposing private content."""
        doctor = selected_agent_service().doctor()
        doctor["runtime"] = context.computer_use_settings.snapshot()
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
                "agentic_token_count": 0,
                "agentic_transcript_tokens": 0,
                "workspace_path": "",
                "running": foreign_run_active,
            }
        )
        return isolated

    def render_agent_page(browser: str, platform: str, connection_mode: str = "browser"):
        """Render one Agent page using the browser/provider encoded by its URL."""
        agent_settings = agent_settings_for_route(browser, platform)
        runtime_snapshot = context.computer_use_settings.snapshot()
        agent_snapshot = agent_snapshot_for_route(
            build_agent_snapshot(),
            browser,
            platform,
            agent_settings.workspace_path,
        )
        compute_job_snapshot = context.computer_use_agent_service.compute_job_status(
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
            safari_agent_execution_platforms=",".join(
                sorted(SUPPORTED_SAFARI_AGENT_EXECUTION_PLATFORMS)
            ),
            model_options_by_platform=AGENT_MODEL_OPTIONS_BY_PLATFORM,
            render_prompt_markdown=render_prompt_markdown,
            format_agent_activity_time=format_agent_activity_time,
            connection_mode=connection_mode,
            tunnel_status=tunnel_status_payload(
                context.computer_use_settings,
                context.tunnel_runtime,
                platform=platform,
                tunnel_mcp_service=context.tunnel_mcp_service,
                gemini_gateway=current_app.extensions.get("gemini_tunnel_gateway"),
            ),
            tunnel_supported_platforms=",".join(sorted(TUNNEL_SUPPORTED_PLATFORMS)),
        )

    @blueprint.get("/agent")
    def agent():
        """Redirect the legacy Agent entrypoint to the canonical selection URL."""
        require_local_agent_request(allow_locked=True)
        if not context.is_agent_access_unlocked():
            return render_locked_agent_access()
        settings = context.computer_use_settings.settings
        platform = settings.platform if settings.platform in SUPPORTED_AGENT_PLATFORMS else "chatgpt"
        return redirect(url_for(f"{TUNNEL_BLUEPRINT_NAME}.agent_tunnel_selected", platform=platform))

    @blueprint.get("/agent/<browser>/")
    def agent_browser(browser: str):
        """Keep the browser-scoped Agent URL useful while exposing provider selection."""
        require_local_agent_request(allow_locked=True)
        selected_browser = browser.strip().lower()
        if selected_browser not in available_agent_browser_keys():
            abort(404)
        platform = context.computer_use_settings.settings.platform
        if not is_supported_agent_selection(selected_browser, platform):
            platform = "chatgpt"
        if not is_supported_agent_selection(selected_browser, platform):
            abort(404)
        return redirect(
            url_for(f"{AGENT_BLUEPRINT_NAME}.agent_selected", browser=selected_browser, platform=platform),
            code=302,
        )

    @blueprint.get("/agent/<browser>/<platform>")
    def agent_selected(browser: str, platform: str):
        """Render the Agent page for one explicit browser/provider selection."""
        require_local_agent_request(allow_locked=True)
        if (
            not is_supported_agent_selection(browser, platform)
            or browser.strip().lower() not in available_agent_browser_keys()
        ):
            abort(404)
        if not context.is_agent_access_unlocked():
            return render_locked_agent_access()
        return render_agent_page(browser.strip().lower(), platform.strip().lower())

    @blueprint.post("/agent/unlock")
    def unlock_agent():
        """Unlock the Agent control plane for the current private-network session."""
        require_local_agent_request(allow_locked=True)
        if not agent_access_password_is_configured():
            return render_locked_agent_access()
        retry_after_seconds = agent_unlock_retry_after_seconds()
        if retry_after_seconds:
            response = render_agent_access_unlock(
                "Too many failed attempts. Try again later.",
                status_code=429,
            )
            response.headers["Retry-After"] = str(retry_after_seconds)
            return response
        if not validate_agent_access_password(request.form.get("password")):
            record_agent_unlock_failure()
            return render_agent_access_unlock("The password is incorrect.", status_code=401)
        clear_agent_unlock_failures()
        session[AGENT_ACCESS_SESSION_KEY] = True
        settings = context.computer_use_settings.settings
        platform = settings.platform if settings.platform in SUPPORTED_AGENT_PLATFORMS else "chatgpt"
        return redirect(
            url_for(f"{TUNNEL_BLUEPRINT_NAME}.agent_tunnel_selected", platform=platform),
            code=303,
        )

    @blueprint.get("/api/agent/status")
    def agent_status():
        require_local_agent_request()
        runtime_snapshot = context.computer_use_settings.snapshot()
        selected_browser, selected_platform, selected_workspace = (
            requested_agent_route(runtime_snapshot)
        )
        if selected_agent_session_id() != "new":
            try:
                context.agent_session_pool.get(selected_agent_session_id())
            except ValueError as exc:
                return jsonify({"error": str(exc), "code": "unknown_agent_session"}), 404
        compute_service = (
            context.computer_use_agent_service
            if selected_agent_session_id() == "new"
            else selected_agent_service()
        )
        snapshot_session_id = selected_agent_session_id()
        if snapshot_session_id == "new":
            snapshot_session_id = context.agent_session_pool.unique_active_session_id() or "new"
        agent_snapshot = build_agent_snapshot(snapshot_session_id)
        if snapshot_session_id != "new" and agent_snapshot.get("running"):
            agent_snapshot["stop_target_session_id"] = snapshot_session_id
            agent_snapshot["stop_target_run_id"] = str(
                agent_snapshot.get("run_id") or ""
            )
        return jsonify(
            {
                "runtime": runtime_snapshot,
                **context.agent_session_pool.catalog(selected_browser, selected_platform, selected_workspace),
                "agent": agent_snapshot_for_route(
                    agent_snapshot,
                    selected_browser,
                    selected_platform,
                    selected_workspace,
                ),
                "compute_job": compute_service.compute_job_status(
                    selected_workspace
                ),
            }
        )

    @blueprint.post("/api/agent/compute-job/stop")
    def stop_agent_compute_job():
        """Stop one durable job without stopping or resuming the Web Agent turn."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        payload = request.get_json(silent=True) or {}
        workspace_path = str(
            payload.get("workspace_path")
            or context.computer_use_settings.settings.workspace_path
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

    @blueprint.get("/api/agent/capabilities")
    def agent_capabilities():
        """Return the local capability registry for human diagnostics and tests."""
        require_local_agent_request()
        return jsonify(capability_registry_snapshot())

    @blueprint.get("/api/agent/doctor")
    def agent_doctor():
        """Return bounded Agent diagnostics and explicit recovery affordances."""
        require_local_agent_request()
        return jsonify(build_agent_doctor())

    @blueprint.post("/api/agent/doctor/recover")
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
                "runtime": context.computer_use_settings.snapshot(),
                "agent": build_agent_snapshot(),
            }
        )

    @blueprint.post("/api/agent/preferences")
    def save_agent_preferences():
        require_local_agent_request()
        payload = request.get_json(silent=True) or {}
        try:
            settings = context.computer_use_settings.update_preferences(
                workspace_path=str(payload.get("workspace_path", "")),
                operating_system=str(payload.get("operating_system", "")),
                browser=str(payload.get("browser", "")),
                platform=str(payload.get("platform", context.computer_use_settings.settings.platform)),
                model=str(payload.get("model", context.computer_use_settings.settings.model)),
                chatgpt_effort=str(
                    payload.get(
                        "chatgpt_effort",
                        context.computer_use_settings.settings.chatgpt_effort,
                    )
                ),
                client_id=str(payload.get("preference_client_id", "")),
                client_revision=payload.get("preference_revision", 0),
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(
            {
                "settings": asdict(settings),
                "runtime": context.computer_use_settings.snapshot(),
                "agent": build_agent_snapshot(),
            }
        )

    @blueprint.post("/api/agent/terminal-authorization")
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

    @blueprint.post("/api/agent/open-conversation")
    def open_agent_conversation():
        """Open the current Agent Web target in the browser selected for the task."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        snapshot = selected_agent_service().snapshot()
        try:
            platform = str(snapshot.get("platform", context.computer_use_settings.settings.platform))
            browser = str(snapshot.get("browser", context.computer_use_settings.settings.browser))
            target_url = str(snapshot.get("conversation_url", ""))
            payload = request.get_json(silent=True) or {}
            background = bool(payload.get("background", True))
            result = open_agent_in_browser(
                platform,
                browser,
                target_url,
                background=background,
                config=context.config_store.config,
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(result)

    @blueprint.post("/api/agent/ask")
    def ask_agent():
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        payload = request.get_json(silent=True) or {}
        requested_browser = str(payload.get("browser", "")).strip().lower()
        if requested_browser == "safari":
            active_cache_source = active_safari_cache_consumer()
            if active_cache_source:
                return jsonify(
                    {
                        "error": (
                            f"Safari is busy with the active {active_cache_source} "
                            "Cache task. Stop it or wait for it to finish before "
                            "starting an Agent task."
                        ),
                        "code": "safari_cache_busy",
                    }
                ), 409
        try:
            started_session_id = context.agent_session_pool.start(
                selected_agent_session_id(),
                str(payload.get("prompt", "")),
                str(payload.get("workspace_path", "")),
                context.config_store.config,
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
                "runtime": context.computer_use_settings.snapshot(),
                "agent": build_agent_snapshot(started_session_id),
            }
        ), 202

    @blueprint.get("/api/agent/chatgpt-sources")
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
                    **list_chatgpt_agent_sources(browser_name, context.config_store.config, silent=True),
                    "platform": "chatgpt",
                },
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(payload)

    @blueprint.get("/api/agent/sources")
    def agent_sources():
        """Load recent sessions for any selected Web Agent provider."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        platform = request.args.get("platform", context.computer_use_settings.settings.platform).strip().lower()
        browser_name = request.args.get("browser", "").strip().lower()
        try:
            payload = load_agent_source_catalog(
                platform=platform,
                browser=browser_name,
                source_kind="sources",
                collector=lambda: list_agent_sources(
                    platform,
                    browser_name,
                    context.config_store.config,
                    silent=True,
                ),
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(payload)

    @blueprint.get("/api/agent/chatgpt-project-sessions")
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
                        context.config_store.config,
                        silent=True,
                    ),
                    "platform": "chatgpt",
                },
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(payload)

    @blueprint.get("/api/agent/project-sessions")
    def agent_project_sessions():
        """Load recent sessions inside one provider-neutral Agent Project."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        platform = request.args.get("platform", context.computer_use_settings.settings.platform).strip().lower()
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
                    context.config_store.config,
                    silent=True,
                ),
            )
        except (RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(payload)

    @blueprint.get("/api/agent/chatgpt-session-history")
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
        conversation_id = urlsplit(conversation_url).path.rstrip("/").rsplit("/", 1)[-1]
        if conversation_id.casefold().startswith("web:"):
            return jsonify(
                {
                    "error": (
                        "ChatGPT has not assigned this task a server conversation ID yet. "
                        "History remains unavailable until the provider finishes that transition."
                    )
                }
            ), 409
        ask_running = uses_exclusive_agent_browser(
            browser_name
        ) and context.agent_session_pool.has_active_worker(browser_name)
        try:
            payload = load_agent_source_catalog(
                platform="chatgpt",
                browser=browser_name,
                source_kind="session-history",
                project_url=conversation_url,
                collector=lambda: fetch_chatgpt_conversation_history(
                    browser_name,
                    conversation_url,
                    context.config_store.config,
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
        title = str(payload.get("title") or "Untitled session")
        rendered_history: list[dict[str, Any]] = []
        for raw_item in humanize_agent_history_prompts(
            payload.get("history", []),
            session_title=title,
        ):
            if not isinstance(raw_item, dict):
                continue
            rendered_history.append(
                render_agent_history_item(raw_item, provider="chatgpt")
            )
        return jsonify(
            {
                "conversation_url": conversation_url,
                "title": title,
                "history": rendered_history,
                "limit": int(payload.get("limit") or len(rendered_history)),
                "cache": payload.get("cache", {}),
            }
        )

    @blueprint.get("/api/agent/grok-session-history")
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
        ask_running = uses_exclusive_agent_browser(
            browser_name
        ) and context.agent_session_pool.has_active_worker(browser_name)
        cache_separator = "&" if "?" in conversation_url else "?"
        history_cache_url = (
            f"{conversation_url}{cache_separator}agent_render_contract="
            f"{quote(GROK_AGENT_HISTORY_RENDER_CONTRACT, safe='')}"
        )
        try:
            payload = load_agent_source_catalog(
                platform="grok",
                browser=browser_name,
                source_kind="session-history",
                project_url=history_cache_url,
                collector=lambda: fetch_grok_conversation_history(
                    browser_name,
                    conversation_url,
                    context.config_store.config,
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
            rendered_history.append(
                render_agent_history_item(raw_item, provider="grok")
            )
        return jsonify(
            {
                "conversation_url": conversation_url,
                "title": str(payload.get("title") or ""),
                "history": rendered_history,
                "limit": int(payload.get("limit") or len(rendered_history)),
                "cache": payload.get("cache", {}),
            }
        )

    @blueprint.post("/api/agent/stop")
    def stop_agent():
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        runtime_snapshot = context.computer_use_settings.snapshot()
        requested_session_id = selected_agent_session_id()
        payload = request.get_json(silent=True) or {}
        target_session_id = str(
            payload.get("stop_target_session_id") or ""
        ).strip()
        target_run_id = str(payload.get("stop_target_run_id") or "").strip()
        if requested_session_id != "new":
            try:
                context.agent_session_pool.get(requested_session_id)
            except ValueError as exc:
                return jsonify(
                    {"error": str(exc), "code": "unknown_agent_session"}
                ), 404
        invalid_session_id = (
            target_session_id != "primary"
            and not re.fullmatch(r"[0-9a-f]{32}", target_session_id)
        )
        if (
            invalid_session_id
            or not re.fullmatch(r"run-[0-9a-f]{16,64}", target_run_id)
            or (
                requested_session_id != "new"
                and target_session_id != requested_session_id
            )
        ):
            return jsonify(
                {
                    "error": (
                        "The Agent stop target is stale. Refresh status before "
                        "trying again."
                    ),
                    "code": "stale_agent_stop_target",
                }
            ), 409
        try:
            target_service = context.agent_session_pool.get(target_session_id)
        except ValueError as exc:
            if requested_session_id == "new":
                return jsonify(
                    {
                        "error": (
                            "The Agent stop target is stale. Refresh status before "
                            "trying again."
                        ),
                        "code": "stale_agent_stop_target",
                    }
                ), 409
            return jsonify({"error": str(exc), "code": "unknown_agent_session"}), 404
        selected_browser, selected_platform, selected_workspace = (
            requested_agent_route(runtime_snapshot)
        )
        stop_requested = target_service.request_stop(
            expected_run_id=target_run_id,
        )
        if not stop_requested:
            return jsonify(
                {
                    "error": (
                        "The Agent stop target is stale. Refresh status before "
                        "trying again."
                    ),
                    "code": "stale_agent_stop_target",
                }
            ), 409
        return jsonify(
            {
                "stop_requested": stop_requested,
                "runtime": runtime_snapshot,
                "agent": agent_snapshot_for_route(
                    build_agent_snapshot(target_session_id),
                    selected_browser,
                    selected_platform,
                    selected_workspace,
                ),
            }
        )

    @blueprint.delete("/api/agent/session")
    def delete_failed_agent_session():
        """Dismiss one failed local task only after the UI confirms no remote record."""
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        payload = request.get_json(silent=True) or {}
        session_id = str(payload.get("session_id") or "").strip()
        try:
            dismissed = context.agent_session_pool.dismiss_failed(
                session_id,
                expected_conversation_url=str(payload.get("conversation_url") or "").strip(),
                remote_record_absent=payload.get("remote_record_absent") is True,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": "unknown_agent_session"}), 404
        except RuntimeError as exc:
            return jsonify({"error": str(exc), "code": "agent_session_not_deletable"}), 409
        return jsonify({"deleted": True, **dismissed})

    @blueprint.post("/api/agent/resume")
    def resume_agent():
        require_local_agent_request()
        if not external_agent_operations_enabled():
            return reject_external_agent_operation()
        return jsonify(
            {
                "resume_requested": selected_agent_service().request_resume(),
                "runtime": context.computer_use_settings.snapshot(),
                "agent": build_agent_snapshot(),
            }
        )


    @blueprint.get("/api/browser-session")
    def api_browser_session():
        platform_name = request.args.get("platform", "").strip().lower()
        browser_name = request.args.get("browser", "").strip().lower()
        scope = request.args.get("scope", "").strip().lower()
        if scope == "agent":
            require_local_agent_request()

        def browser_session_response(payload: dict[str, Any], status_code: int = 200):
            if scope == "agent":
                payload = dict(payload)
                payload["agent_execution_supported"] = is_agent_execution_supported(
                    browser_name,
                    platform_name,
                )
                payload["agent_execution_message"] = agent_execution_blocked_message(
                    browser_name,
                    platform_name,
                )
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
            "gemini": probe_and_collect_gemini_sources,
            "grok": probe_and_collect_grok_sources,
            "claude": probe_and_collect_claude_sources,
        }
        safari_agent_busy = (
            browser_name == "safari"
            and not (
                scope == "agent" and platform_name in agent_bootstrap_collectors
            )
            and context.agent_session_pool.has_active_worker("safari")
        )
        if safari_agent_busy:
            return browser_session_response(
                {
                    "platform": platform_name,
                    "browser": browser_name,
                    "browser_label": "Safari",
                    "logged_in": False,
                    "can_download": False,
                    "account_name": "",
                    "message": (
                        "Safari is busy with an active Agent task. "
                        "Account checks resume after that task finishes."
                    ),
                    "busy": True,
                },
                409,
            )
        if scope == "agent" and platform_name in agent_bootstrap_collectors:
            try:
                payload = load_agent_browser_session_bootstrap(
                    platform=platform_name,
                    browser=browser_name,
                    collector=lambda: agent_bootstrap_collectors[platform_name](
                        browser_name,
                        context.config_store.config,
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
                context.config_store.config,
                silent=scope == "agent",
                # macOS Edge Agent tasks run in the project debug profile, so
                # Recheck must read that profile rather than a daily clone.
                prefer_initialized_debug_profile=(
                    scope == "agent"
                    and (
                        platform_name == "gemini"
                        or (browser_name == "edge" and is_macos_host())
                    )
                ),
            )
        except ValueError as exc:
            return browser_session_response({"error": str(exc)}, 400)
        except RuntimeError as exc:
            return browser_session_response({"error": str(exc)}, 409)
        return browser_session_response(payload)

    @blueprint.post("/api/browser-session/open-login")
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
                open_zhihu_browser_for_login(browser_name, context.config_store.config)
                if is_zhihu_selection
                else open_browser_for_login(platform_name, browser_name, config=context.config_store.config)
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(result)

    app.template_global("agent_entry_url")(agent_entry_url)
    app.template_global("build_agent_optimization_manifest")(
        build_agent_optimization_manifest_for_template
    )
    app.register_blueprint(blueprint)
    return AgentSurface(
        require_local_agent_request=require_local_agent_request,
        is_agent_access_unlocked=context.is_agent_access_unlocked,
        render_locked_agent_access=render_locked_agent_access,
        render_agent_page=render_agent_page,
        available_agent_browser_keys=available_agent_browser_keys,
        external_agent_operations_enabled=external_agent_operations_enabled,
        reject_external_agent_operation=reject_external_agent_operation,
        reject_active_safari_agent_for_cache=reject_active_safari_agent_for_cache,
    )


__all__ = [
    "AGENT_BLUEPRINT_NAME",
    "AgentRouteContext",
    "AgentSurface",
    "register_agent_routes",
]
