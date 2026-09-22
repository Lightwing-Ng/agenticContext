"""The composition root for the local web console.

This module builds one application: it reads configuration, constructs the services and
stores, installs the request checks and security headers that guard every route,
coordinates one idempotent shutdown, and registers each surface's blueprint with the
small typed context that surface actually needs.

Domain request handling lives in the route modules, not here. The Agent surface is
registered first because it owns the access gate and the exclusive-browser rule that the
Tunnel, Jury, and Cache blueprints borrow through :class:`AgentSurface`.
"""

# Code version: v2.1.3-codex.0

from __future__ import annotations

import atexit
import os
import secrets
from collections.abc import Iterable
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, abort, redirect, request, session, url_for

from app.core.agent import (
    AGENT_ACCESS_SESSION_KEY,
    AgentSessionPool,
    AgentSourceCache,
    ComputerUseAgentService,
    ComputerUseSettingsStore,
    JuryService,
    is_allowed_agent_network_request,
    is_loopback_address,
    TunnelMcpService,
    TunnelRuntime,
    default_tunnel_credentials_path,
    load_tunnel_credentials,
)
from app.core.foundation import (
    APP_VERSION,
    LOCAL_STORE_ROOT,
    TaskState,
    build_initial_snapshot,
    configure_logging,
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
    build_claude_initial_snapshot,
    build_gemini_initial_snapshot,
    build_grok_history_snapshot,
    build_grok_initial_snapshot,
    build_zhihu_history_initial_snapshot,
)
from app.core.storage import (
    LocalMediaCatalog,
    ShadowBackupService,
    PromptStore,
)
from app.web.agent_routes import (
    AGENT_BLUEPRINT_NAME,
    AgentRouteContext,
    register_agent_routes,
)
from app.web.beta import register_beta
from app.web.cache_routes import (
    CacheRouteContext,
    CacheRuntimeAdapter,
    build_reconciled_chatgpt_snapshot,
    build_reconciled_grok_snapshot,
    register_cache_routes,
)
from app.web.config_store import SavedConfigStore
from app.web.jury_routes import JuryRouteContext, register_jury_routes
from app.web.local_resource_routes import (
    LocalResourceRouteContext,
    register_local_resource_routes,
)
from app.web.settings_routes import (
    SettingsRouteContext,
    register_settings_routes,
)
from app.web.tunnel_routes import (
    GeminiTunnelGateway,
    TunnelRouteContext,
    register_tunnel_routes,
)




AGENT_UNLOCK_FAILURE_LIMIT = 5
AGENT_UNLOCK_FAILURE_WINDOW_SECONDS = 300.0
UNSAFE_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


LEGACY_ENDPOINT_ALIASES: dict[str, str] = {
    "cache_source": "cache.cache_source",
    "cache_source_selected": "cache.cache_source_selected",
    "index": "cache.index",
    "grok": "cache.grok",
    "chatgpt": "cache.chatgpt",
    "gemini": "cache.gemini",
    "claude": "cache.claude",
    "zhihu": "cache.zhihu",
    "settings": "settings.settings",
    "settings_style_tokens": "settings.settings_style_tokens",
    "agent": "agent.agent",
    "tunnel_mcp_endpoint": "tunnel.mcp_endpoint",
    "tunnel_mcp_stream": "tunnel.mcp_stream",
    "api_tunnel_credentials": "tunnel.api_credentials",
    "api_tunnel_status": "tunnel.api_status",
    "api_tunnel_restart": "tunnel.api_restart",
    "api_tunnel_connect": "tunnel.api_connect",
    "api_tunnel_disconnect": "tunnel.api_disconnect",
    "agent_tunnel": "tunnel.agent_tunnel",
    "agent_tunnel_selected": "tunnel.agent_tunnel_selected",
    "agent_browser": "agent.agent_browser",
    "agent_selected": "agent.agent_selected",
    "jury": "jury.jury",
    "jury_selected": "jury.jury_selected",
    "jury_check": "jury.jury_check",
    "jury_start": "jury.jury_start",
    "jury_status": "jury.jury_status",
    "jury_sessions": "jury.jury_sessions",
    "jury_stop": "jury.jury_stop",
    "delete_failed_jury_session": "jury.delete_failed_jury_session",
    "unlock_agent": "agent.unlock_agent",
    "agent_status": "agent.agent_status",
    "stop_agent_compute_job": "agent.stop_agent_compute_job",
    "agent_capabilities": "agent.agent_capabilities",
    "agent_doctor": "agent.agent_doctor",
    "recover_agent_from_doctor": "agent.recover_agent_from_doctor",
    "save_agent_preferences": "agent.save_agent_preferences",
    "open_agent_terminal_authorization": "agent.open_agent_terminal_authorization",
    "open_agent_conversation": "agent.open_agent_conversation",
    "ask_agent": "agent.ask_agent",
    "agent_chatgpt_sources": "agent.agent_chatgpt_sources",
    "agent_sources": "agent.agent_sources",
    "agent_chatgpt_project_sessions": "agent.agent_chatgpt_project_sessions",
    "agent_project_sessions": "agent.agent_project_sessions",
    "agent_chatgpt_session_history": "agent.agent_chatgpt_session_history",
    "agent_grok_session_history": "agent.agent_grok_session_history",
    "stop_agent": "agent.stop_agent",
    "delete_failed_agent_session": "agent.delete_failed_agent_session",
    "browser": "local_resources.browser",
    "browser_session_export": "local_resources.browser_session_export",
    "browser_media": "local_resources.browser_media",
    "add_browser_prompt": "local_resources.add_browser_prompt",
    "add_browser_prompt_remark": "local_resources.add_browser_prompt_remark",
    "remove_browser_prompt_remark": "local_resources.remove_browser_prompt_remark",
    "browser_deleted_preview": "local_resources.browser_deleted_preview",
    "delete_browser_media": "local_resources.delete_browser_media",
    "restore_browser_media": "local_resources.restore_browser_media",
    "reveal_browser_media": "local_resources.reveal_browser_media",
    "open_cache_output_directory": "cache.open_cache_output_directory",
    "refresh_browser_chatgpt_session": "cache.refresh_browser_chatgpt_session",
    "start_cache_source": "cache.start_cache_source",
    "stop_cache_source": "cache.stop_cache_source",
    "start": "cache.start",
    "stop": "cache.stop",
    "start_grok": "cache.start_grok",
    "stop_grok": "cache.stop_grok",
    "start_grok_text_history": "cache.start_grok_text_history",
    "stop_grok_text_history": "cache.stop_grok_text_history",
    "start_chatgpt": "cache.start_chatgpt",
    "stop_chatgpt": "cache.stop_chatgpt",
    "start_gemini": "cache.start_gemini",
    "stop_gemini": "cache.stop_gemini",
    "start_claude": "cache.start_claude",
    "stop_claude": "cache.stop_claude",
    "reset_chatgpt": "cache.reset_chatgpt",
    "reset_grok": "cache.reset_grok",
    "save_settings": "settings.save_settings",
    "start_shadow_backup_sync": "settings.start_shadow_backup_sync",
    "api_shadow_backup_status": "settings.api_shadow_backup_status",
    "choose_shadow_backup_destination_route": (
        "settings.choose_shadow_backup_destination_route"
    ),
    "choose_settings_directory_route": "settings.choose_settings_directory_route",
    "validate_settings_directory_route": (
        "settings.validate_settings_directory_route"
    ),
    "resume_agent": "agent.resume_agent",
    "api_status": "cache.api_status",
    "api_grok_status": "cache.api_grok_status",
    "api_grok_text_status": "cache.api_grok_text_status",
    "api_chatgpt_status": "cache.api_chatgpt_status",
    "api_gemini_status": "cache.api_gemini_status",
    "api_claude_status": "cache.api_claude_status",
    "api_zhihu_status": "cache.api_zhihu_status",
    "api_cache_status": "cache.api_cache_status",
    "api_browser_session": "agent.api_browser_session",
    "open_browser_session_login": "agent.open_browser_session_login",
}


def _register_legacy_endpoint_aliases(app: Flask) -> None:
    """Keep pre-Blueprint endpoint names available only for URL building."""
    rules_by_endpoint: dict[str, list[Any]] = {}
    for rule in tuple(app.url_map.iter_rules()):
        rules_by_endpoint.setdefault(rule.endpoint, []).append(rule)

    missing_targets = sorted(
        target
        for target in LEGACY_ENDPOINT_ALIASES.values()
        if target not in rules_by_endpoint
    )
    if missing_targets:
        raise RuntimeError(
            "Legacy endpoint aliases target missing routes: "
            + ", ".join(missing_targets)
        )

    conflicting_aliases = sorted(
        endpoint
        for endpoint in LEGACY_ENDPOINT_ALIASES
        if endpoint in rules_by_endpoint
    )
    if conflicting_aliases:
        raise RuntimeError(
            "Legacy endpoint aliases conflict with registered routes: "
            + ", ".join(conflicting_aliases)
        )

    for legacy_endpoint, target_endpoint in LEGACY_ENDPOINT_ALIASES.items():
        for target_rule in rules_by_endpoint[target_endpoint]:
            alias_rule = target_rule.empty()
            alias_rule.endpoint = legacy_endpoint
            alias_rule.build_only = True
            app.url_map.add(alias_rule)



























def create_app(
    local_store_root: Path | str | None = None,
    *,
    computer_use_settings_path: Path | None = None,
    computer_use_runtime_root: Path | None = None,
    agent_external_operations_enabled: bool = True,
    beta_enabled: bool | None = None,
    beta_experiments: Iterable[str] | None = None,
) -> Flask:
    """Build one configured application with its services, hooks, and route blueprints."""
    configure_logging(APP_VERSION)
    effective_local_store_root = (
        Path(local_store_root).expanduser()
        if local_store_root is not None
        else LOCAL_STORE_ROOT
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
    config_store = SavedConfigStore.load()
    computer_use_settings = ComputerUseSettingsStore(computer_use_settings_path)
    agent_service_kwargs: dict[str, Any] = {
        "config_provider": lambda: config_store.config,
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
    jury_service = JuryService(
        lambda: computer_use_settings.settings,
        lambda: config_store.config,
        Path(computer_use_runtime_root or effective_local_store_root / "agent") / "jury",
    )
    app.extensions["jury_service"] = jury_service

    tunnel_mcp_service = TunnelMcpService(lambda: computer_use_settings.settings)
    tunnel_runtime = TunnelRuntime(
        credentials_loader=load_tunnel_credentials,
        state_root=default_tunnel_credentials_path().parent / "tunnel",
        activity_provider=lambda: tunnel_mcp_service.activity_snapshot("chatgpt"),
    )
    app.extensions["tunnel_mcp_service"] = tunnel_mcp_service
    app.extensions["tunnel_runtime"] = tunnel_runtime
    gemini_tunnel_gateway = GeminiTunnelGateway(tunnel_mcp_service)
    app.extensions["gemini_tunnel_gateway"] = gemini_tunnel_gateway

    runtime_shutdown_lock = RLock()
    runtime_shutdown_started = False

    def stop_runtime_services() -> None:
        """Stop browser-owning services exactly once before process exit."""
        nonlocal runtime_shutdown_started
        with runtime_shutdown_lock:
            if runtime_shutdown_started:
                return
            runtime_shutdown_started = True
        tunnel_mcp_service.stop()
        try:
            tunnel_runtime.stop()
        except Exception as exc:
            app.logger.error("Could not stop the Tunnel during service shutdown: %s", exc)
        for label, service in (
            ("jury_service", jury_service),
            ("agent_session_pool", agent_session_pool),
        ):
            try:
                service.stop_at_exit()
            except Exception as exc:
                app.logger.error(
                    "Could not stop %s during service shutdown: %s",
                    label,
                    exc,
                )

    app.extensions["runtime_shutdown"] = stop_runtime_services
    atexit.register(stop_runtime_services)

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
                project_name=config_store.config.chatgpt_project_name,
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

    def is_agent_access_unlocked() -> bool:
        """Allow the host itself to bypass the LAN gate after validating the request network."""
        return is_loopback_address(request.remote_addr) or bool(
            session.get(AGENT_ACCESS_SESSION_KEY)
        )

    def require_trusted_request_network() -> None:
        """Reject public clients and Host-header rebinding for every application route."""
        try:
            host_parts = urlsplit(f"//{request.host}")
            host_name, _host_port = host_parts.hostname, host_parts.port
        except ValueError:
            abort(403)
        if not is_allowed_agent_network_request(request.remote_addr, host_name):
            abort(403)

    def request_origin_matches_host() -> bool:
        """Return whether the submitted Origin identifies this exact local service."""
        origin = request.headers.get("Origin", "").strip()
        if not origin:
            return False
        try:
            origin_parts = urlsplit(origin)
            expected_parts = urlsplit(request.host_url)
            origin_identity = (
                origin_parts.scheme,
                origin_parts.hostname,
                origin_parts.port,
            )
            expected_identity = (
                expected_parts.scheme,
                expected_parts.hostname,
                expected_parts.port,
            )
        except ValueError:
            return False
        return (
            origin_parts.scheme in {"http", "https"}
            and origin_parts.username is None
            and origin_parts.password is None
            and origin_parts.path in {"", "/"}
            and not origin_parts.query
            and not origin_parts.fragment
            and origin_identity == expected_identity
        )

    def require_safe_request_origin() -> None:
        """Block browser cross-site writes and require an Origin for LAN writes."""
        fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
        if fetch_site == "cross-site":
            abort(403)
        origin = request.headers.get("Origin", "").strip()
        if origin and not request_origin_matches_host():
            abort(403)
        if (
            request.method in UNSAFE_HTTP_METHODS
            and not is_loopback_address(request.remote_addr)
            and not origin
        ):
            abort(403)

    @app.before_request
    def protect_local_application_request():
        """Apply one network, CSRF, and LAN-session boundary to the whole application."""
        if gemini_tunnel_gateway.public_host_matches():
            gemini_tunnel_gateway.require_public_request()
            return None
        require_trusted_request_network()
        require_safe_request_origin()
        if is_agent_access_unlocked():
            return None
        if request.endpoint == "static" or request.path.startswith("/static/"):
            return None
        if request.endpoint in {
            f"{AGENT_BLUEPRINT_NAME}.agent",
            f"{AGENT_BLUEPRINT_NAME}.agent_browser",
            f"{AGENT_BLUEPRINT_NAME}.agent_selected",
            f"{AGENT_BLUEPRINT_NAME}.unlock_agent",
        }:
            return None
        if request.method in {"GET", "HEAD"} and not request.path.startswith("/api/"):
            return redirect(url_for(f"{AGENT_BLUEPRINT_NAME}.agent"))
        abort(401)

    @app.after_request
    def apply_local_security_headers(response: Response) -> Response:
        """Prevent framing, MIME sniffing, and storage of Agent credentials or state."""
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if gemini_tunnel_gateway.public_host_matches():
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            )
        if (
            request.path.startswith("/agent")
            or request.path.startswith("/api/agent")
            or request.path.startswith("/jury")
            or request.path.startswith("/api/jury")
            or request.path == "/api/browser-session"
        ):
            response.headers["Cache-Control"] = (
                "no-store, no-cache, max-age=0, must-revalidate"
            )
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    agent_surface = register_agent_routes(
        app,
        AgentRouteContext(
            config_store=config_store,
            computer_use_settings=computer_use_settings,
            computer_use_agent_service=computer_use_agent_service,
            agent_session_pool=agent_session_pool,
            agent_source_cache=agent_source_cache,
            grok_history_service=grok_history_service,
            cache_runtimes=cache_runtimes,
            tunnel_runtime=tunnel_runtime,
            tunnel_mcp_service=tunnel_mcp_service,
            require_trusted_request_network=require_trusted_request_network,
            require_safe_request_origin=require_safe_request_origin,
            is_agent_access_unlocked=is_agent_access_unlocked,
        ),
    )

    register_tunnel_routes(
        app,
        TunnelRouteContext(
            settings_store=computer_use_settings,
            tunnel_runtime=tunnel_runtime,
            tunnel_mcp_service=tunnel_mcp_service,
            gemini_gateway=gemini_tunnel_gateway,
            require_local_agent_request=agent_surface.require_local_agent_request,
            is_agent_access_unlocked=agent_surface.is_agent_access_unlocked,
            render_locked_agent_access=agent_surface.render_locked_agent_access,
            render_agent_page=agent_surface.render_agent_page,
            available_agent_browser_keys=agent_surface.available_agent_browser_keys,
        ),
    )

    register_jury_routes(
        app,
        JuryRouteContext(
            settings_store=computer_use_settings,
            jury_service=jury_service,
            require_local_agent_request=agent_surface.require_local_agent_request,
            available_agent_browser_keys=agent_surface.available_agent_browser_keys,
            external_agent_operations_enabled=agent_surface.external_agent_operations_enabled,
            reject_external_agent_operation=agent_surface.reject_external_agent_operation,
        ),
    )

    register_local_resource_routes(
        app,
        LocalResourceRouteContext(
            media_catalog=media_catalog,
            prompt_store=prompt_store,
        ),
    )

    cache_context = CacheRouteContext(
        cache_runtimes=cache_runtimes,
        config_store=config_store,
        media_catalog=media_catalog,
        grok_history_state=grok_history_state,
        grok_history_service=grok_history_service,
        chatgpt_service=chatgpt_service,
        reject_active_safari_agent_for_cache=agent_surface.reject_active_safari_agent_for_cache,
        external_agent_operations_enabled=agent_surface.external_agent_operations_enabled,
        reject_external_agent_operation=agent_surface.reject_external_agent_operation,
    )
    register_cache_routes(app, cache_context)
    register_settings_routes(
        app,
        SettingsRouteContext(
            config_store=config_store,
            computer_use_settings=computer_use_settings,
            shadow_backup_service=shadow_backup_service,
            media_catalog=media_catalog,
            build_reconciled_grok_snapshot=lambda: build_reconciled_grok_snapshot(cache_context),
            build_reconciled_chatgpt_snapshot=lambda: build_reconciled_chatgpt_snapshot(cache_context),
        ),
    )
    _register_legacy_endpoint_aliases(app)

    return app
