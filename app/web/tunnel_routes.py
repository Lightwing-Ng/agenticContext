"""Tunnel connection routes: MCP transport, credentials, and the Tunnel Agent page.

The application factory stays a composition root: it builds the Tunnel services and
hands this module the few Agent-page capabilities these routes share with the rest of
the Agent surface. Request handling, credential validation, and status presentation
live here.
"""

# Code version: v1.10.3-codex.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
from threading import RLock
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import (
    Blueprint,
    Flask,
    Response,
    abort,
    jsonify,
    make_response,
    redirect,
    request,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge

from app.core.agent import (
    MCP_SCOPE,
    ProjectRegistryError,
    ProjectSelectionConflict,
    SUPPORTED_AGENT_PLATFORMS,
    ComputerUseSettingsStore,
    GeminiOAuthAuthority,
    GeminiOAuthError,
    GeminiTunnelConfig,
    GeminiTunnelConfigError,
    TunnelRuntime,
    authorization_server_metadata,
    clear_gemini_tunnel_config,
    configure_gemini_tunnel,
    default_tunnel_browse_root,
    describe_tunnel_status,
    is_loopback_address,
    load_gemini_tunnel_config,
    load_tunnel_credentials,
    merge_tunnel_credentials,
    protected_resource_metadata,
    project_availability,
    resolve_current_project,
    save_tunnel_credentials,
    selected_projects,
    valid_tunnel_id,
    www_authenticate_challenge,
)


TUNNEL_BLUEPRINT_NAME = "tunnel"
GEMINI_PUBLIC_PATHS = frozenset(
    {
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp/gemini",
        "/mcp/gemini",
        "/oauth/authorize",
        "/oauth/token",
    }
)
MAX_OAUTH_FORM_BYTES = 16 * 1024
MAX_MCP_BODY_BYTES = 2 * 1024 * 1024
MAX_GEMINI_MCP_BODY_BYTES = MAX_MCP_BODY_BYTES
GEMINI_AUTHORIZATION_REVIEW_SECONDS = 10 * 60
GEMINI_AUTHORIZATION_REVIEW_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}", re.ASCII)


class GeminiTunnelGateway:
    """Keep one app instance's Gemini configuration and OAuth grants isolated."""

    def __init__(self, tunnel_mcp_service: Any) -> None:
        self.tunnel_mcp_service = tunnel_mcp_service
        self._lock = RLock()
        self._config = GeminiTunnelConfig()
        self._authority: GeminiOAuthAuthority | None = None
        self._activity_cursor = 0
        self._redirect_uri = ""
        self._pending_authorization_digest = ""
        self._pending_authorization_review_id = ""
        self._pending_redirect_uri = ""
        self._pending_authorization_expires_at = 0.0
        self._approved_authorization_digest = ""
        self._approved_authorization_expires_at = 0.0
        self._denied_authorization_digest = ""
        self._denied_authorization_expires_at = 0.0

    def _latest_activity_call_id_locked(self) -> int:
        snapshot = self.tunnel_mcp_service.activity_snapshot("gemini")
        records = [
            *list(snapshot.get("active_calls") or []),
            *list(snapshot.get("recent_calls") or []),
        ]
        return max(
            (
                int(record.get("call_id") or 0)
                for record in records
                if isinstance(record, dict)
            ),
            default=0,
        )

    def _adopt_config_locked(self, config: GeminiTunnelConfig) -> None:
        if self._authority is not None:
            self._authority.revoke_all()
        self._config = config
        self._authority = GeminiOAuthAuthority(config) if config.configured else None
        self._activity_cursor = self._latest_activity_call_id_locked()
        self._redirect_uri = ""
        self._clear_authorization_review_locked()

    def _clear_authorization_review_locked(self) -> None:
        self._pending_authorization_digest = ""
        self._pending_authorization_review_id = ""
        self._pending_redirect_uri = ""
        self._pending_authorization_expires_at = 0.0
        self._approved_authorization_digest = ""
        self._approved_authorization_expires_at = 0.0
        self._denied_authorization_digest = ""
        self._denied_authorization_expires_at = 0.0

    def _prune_authorization_review_locked(self, now: float) -> None:
        if now >= self._pending_authorization_expires_at:
            self._pending_authorization_digest = ""
            self._pending_authorization_review_id = ""
            self._pending_redirect_uri = ""
            self._pending_authorization_expires_at = 0.0
        if now >= self._approved_authorization_expires_at:
            self._approved_authorization_digest = ""
            self._approved_authorization_expires_at = 0.0
        if now >= self._denied_authorization_expires_at:
            self._denied_authorization_digest = ""
            self._denied_authorization_expires_at = 0.0

    @staticmethod
    def _authorization_digest(values: dict[str, str], state: str) -> str:
        encoded = json.dumps(
            {**values, "state": state},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def current(self) -> tuple[GeminiTunnelConfig, GeminiOAuthAuthority | None]:
        """Return the current configuration and its matching in-memory authority."""
        with self._lock:
            config = load_gemini_tunnel_config()
            if config != self._config:
                self._adopt_config_locked(config)
            return self._config, self._authority

    def replace(self, public_origin: str) -> GeminiTunnelConfig:
        """Save or clear the origin and revoke grants whenever credentials rotate."""
        with self._lock:
            config = (
                configure_gemini_tunnel(public_origin)
                if public_origin
                else clear_gemini_tunnel_config()
            )
            if config != self._config:
                self._adopt_config_locked(config)
        return config

    def activity_snapshot(self) -> dict[str, Any]:
        """Return only Gemini calls observed after the current credential generation."""
        with self._lock:
            self.current()
            cursor = self._activity_cursor
            snapshot = self.tunnel_mcp_service.activity_snapshot("gemini")
            active_calls = [
                record
                for record in snapshot.get("active_calls") or []
                if int(record.get("call_id") or 0) > cursor
            ]
            recent_calls = [
                record
                for record in snapshot.get("recent_calls") or []
                if int(record.get("call_id") or 0) > cursor
            ]
            return {
                **snapshot,
                "call_count": len(recent_calls),
                "active_calls": active_calls,
                "recent_calls": recent_calls,
            }

    def authorization_snapshot(self) -> dict[str, object]:
        """Return only the exact callback currently awaiting local review."""
        with self._lock:
            _config, authority = self.current()
            if authority is None:
                return {
                    "pending": False,
                    "review_id": "",
                    "redirect_uri": "",
                    "redirect_host": "",
                    "expires_in": 0,
                }
            now = time.monotonic()
            self._prune_authorization_review_locked(now)
            redirect_uri = self._pending_redirect_uri
            return {
                "pending": bool(self._pending_authorization_digest),
                "review_id": self._pending_authorization_review_id,
                "redirect_uri": redirect_uri,
                "redirect_host": str(urlsplit(redirect_uri).hostname or ""),
                "expires_in": max(
                    0,
                    int(self._pending_authorization_expires_at - now),
                ),
            }

    def resolve_pending_authorization(self, action: str, review_id: str) -> None:
        """Approve or deny the exact callback shown in the authenticated local UI."""
        with self._lock:
            _config, authority = self.current()
            if authority is None:
                raise GeminiTunnelConfigError("Gemini Tunnel is not configured.")
            now = time.monotonic()
            self._prune_authorization_review_locked(now)
            if not self._pending_authorization_digest:
                raise GeminiOAuthError(
                    "invalid_request",
                    "No Gemini OAuth callback is awaiting local review.",
                )
            if (
                GEMINI_AUTHORIZATION_REVIEW_ID_RE.fullmatch(review_id) is None
                or not hmac.compare_digest(
                    review_id,
                    self._pending_authorization_review_id,
                )
            ):
                raise GeminiOAuthError(
                    "invalid_request",
                    "The pending Gemini OAuth review has changed. Review it again.",
                )
            if action == "approve":
                self._approved_authorization_digest = (
                    self._pending_authorization_digest
                )
                self._approved_authorization_expires_at = (
                    self._pending_authorization_expires_at
                )
                self._denied_authorization_digest = ""
                self._denied_authorization_expires_at = 0.0
            elif action == "deny":
                self._denied_authorization_digest = self._pending_authorization_digest
                self._denied_authorization_expires_at = (
                    self._pending_authorization_expires_at
                )
                self._approved_authorization_digest = ""
                self._approved_authorization_expires_at = 0.0
            else:
                raise GeminiOAuthError(
                    "invalid_request",
                    "Authorization action must be approve or deny.",
                )
            self._pending_authorization_digest = ""
            self._pending_authorization_review_id = ""
            self._pending_redirect_uri = ""
            self._pending_authorization_expires_at = 0.0

    def stage_or_issue_authorization(
        self,
        *,
        state: str,
        **values: str,
    ) -> tuple[GeminiTunnelConfig, str | None, str]:
        """Stage an exact callback for review or issue its one approved code."""
        with self._lock:
            config, authority = self.current()
            if authority is None:
                raise GeminiTunnelConfigError("Gemini Tunnel is not configured.")
            redirect_uri = authority.validate_authorization_request(**values)
            canonical_values = {**values, "redirect_uri": redirect_uri}
            digest = self._authorization_digest(canonical_values, state)
            now = time.monotonic()
            self._prune_authorization_review_locked(now)
            if self._redirect_uri and redirect_uri != self._redirect_uri:
                raise GeminiOAuthError(
                    "invalid_request",
                    "OAuth redirect_uri does not match the callback pinned for this process.",
                )

            if self._approved_authorization_digest:
                if not hmac.compare_digest(
                    digest,
                    self._approved_authorization_digest,
                ):
                    raise GeminiOAuthError(
                        "temporarily_unavailable",
                        "A different Gemini OAuth request is already approved.",
                        status_code=409,
                    )
                code = authority.issue_authorization_code(**canonical_values)
                if not self._redirect_uri:
                    self._redirect_uri = redirect_uri
                self._approved_authorization_digest = ""
                self._approved_authorization_expires_at = 0.0
                return config, code, redirect_uri

            if self._denied_authorization_digest and hmac.compare_digest(
                digest,
                self._denied_authorization_digest,
            ):
                self._denied_authorization_digest = ""
                self._denied_authorization_expires_at = 0.0
                raise GeminiOAuthError(
                    "access_denied",
                    "The local Agent operator denied this OAuth callback.",
                )

            if self._pending_authorization_digest:
                if hmac.compare_digest(digest, self._pending_authorization_digest):
                    return config, None, redirect_uri
                raise GeminiOAuthError(
                    "temporarily_unavailable",
                    "A different Gemini OAuth callback is awaiting local review.",
                    status_code=409,
                )

            self._pending_authorization_digest = digest
            self._pending_authorization_review_id = secrets.token_urlsafe(24)
            self._pending_redirect_uri = redirect_uri
            self._pending_authorization_expires_at = (
                now + GEMINI_AUTHORIZATION_REVIEW_SECONDS
            )
            return config, None, redirect_uri

    def public_host_matches(self) -> bool:
        """Match only the configured public authority, never forwarded-host headers."""
        try:
            config, _authority = self.current()
            expected = urlsplit(config.public_origin)
            supplied = urlsplit(f"//{request.host}")
            expected_port = expected.port or 443
            supplied_port = supplied.port or 443
        except (GeminiTunnelConfigError, ValueError):
            return False
        return bool(
            config.configured
            and supplied.username is None
            and supplied.password is None
            and str(supplied.hostname or "").casefold()
            == str(expected.hostname or "").casefold()
            and supplied_port == expected_port
        )

    def require_public_request(self) -> None:
        """Admit only the exact public Host through a loopback reverse proxy."""
        if not self.public_host_matches():
            abort(404)
        if not is_loopback_address(request.remote_addr):
            abort(403)
        if request.path not in GEMINI_PUBLIC_PATHS:
            abort(404)

    def authority(self) -> tuple[GeminiTunnelConfig, GeminiOAuthAuthority]:
        """Return a configured authority or fail closed without leaking credentials."""
        config, authority = self.current()
        if authority is None:
            raise GeminiTunnelConfigError("Gemini Tunnel is not configured.")
        return config, authority


@dataclass(frozen=True, slots=True)
class TunnelRouteContext:
    """The Agent-page capabilities the Tunnel routes share with the browser routes.

    The factory injects each service once. The Tunnel routes also borrow only the
    request gate and page rendering that the Agent surface defines once.
    """

    settings_store: ComputerUseSettingsStore
    tunnel_runtime: TunnelRuntime
    tunnel_mcp_service: Any
    gemini_gateway: GeminiTunnelGateway
    require_local_agent_request: Callable[..., None]
    is_agent_access_unlocked: Callable[[], bool]
    render_locked_agent_access: Callable[[], Any]
    render_agent_page: Callable[..., Any]
    available_agent_browser_keys: Callable[[], set[str]]
    credentials_path: Path | None = None


def default_tunnel_platform(settings_store: ComputerUseSettingsStore) -> str:
    """Return the saved Agent provider, falling back to the default Tunnel provider."""
    platform = settings_store.settings.platform
    return platform if platform in SUPPORTED_AGENT_PLATFORMS else "chatgpt"


def _local_project_context(
    settings_store: ComputerUseSettingsStore,
    tunnel_mcp_service: Any | None,
) -> dict[str, Any]:
    """Return the local-only project catalog, including absolute host roots.

    This payload is available only behind the local Agent request gate or while
    rendering the unlocked local page. Model-facing MCP records continue to omit
    absolute host paths.
    """
    empty = {
        "registry_configured": False,
        "revision": 0,
        "selected_at": 0.0,
        "source": "none",
        "current": None,
        "selected_project_ids": [],
        "projects": [],
        "browse_root": str(default_tunnel_browse_root()),
        "problem": "The Tunnel project registry service is unavailable.",
    }
    if tunnel_mcp_service is None:
        return empty

    workspace_path = str(settings_store.settings.workspace_path or "")
    registry = tunnel_mcp_service.registry
    selection_store = tunnel_mcp_service.selection_store
    selection = selection_store.load()
    registry_configured = not registry.uses_fallback()
    try:
        projects = registry.projects(workspace_path)
        resolved = resolve_current_project(projects, selection, workspace_path)
    except ProjectRegistryError as exc:
        return {
            **empty,
            "registry_configured": registry_configured,
            "revision": selection.revision,
            "selected_at": selection.selected_at,
            "problem": str(exc),
        }

    preferred_projects = selected_projects(projects, selection)
    preferred_ids = {project.id for project in preferred_projects}
    records: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for project in projects:
        problem = project_availability(project)
        record = {
            "id": project.id,
            "identity": project.identity,
            "root": str(project.root),
            "writable": project.writable,
            "access": "Read and write" if project.writable else "Read only",
            "registered": registry_configured,
            "selected": project.id in preferred_ids,
            "available": not problem,
            "availability": "Available" if not problem else "Unavailable",
            "problem": problem,
            "description": project.description,
        }
        records.append(record)
        by_id[project.id] = record

    current_record = None
    current_problem = resolved.problem if projects else ""
    if resolved.project is not None:
        current_record = {
            **by_id[resolved.project.id],
            "source": resolved.source,
        }
        current_problem = current_problem or str(current_record.get("problem") or "")

    return {
        "registry_configured": registry_configured,
        "revision": selection.revision,
        "selected_at": selection.selected_at,
        "source": resolved.source,
        "current": current_record,
        "selected_project_ids": [project.id for project in preferred_projects],
        "projects": records,
        "browse_root": str(default_tunnel_browse_root()),
        "problem": current_problem,
    }


def tunnel_status_payload(
    settings_store: ComputerUseSettingsStore,
    tunnel_runtime: TunnelRuntime,
    *,
    platform: str = "chatgpt",
    tunnel_mcp_service: Any | None = None,
    gemini_gateway: GeminiTunnelGateway | None = None,
    credentials_path: Path | None = None,
) -> dict[str, Any]:
    """Return one provider's Tunnel status and UI-safe configuration.

    The Agent page renders this for every connection mode, so it takes only the two
    services it reads instead of the whole route context.
    """
    workspace = Path(settings_store.settings.workspace_path)
    project_context = _local_project_context(settings_store, tunnel_mcp_service)
    current_project = project_context.get("current")
    project_name = (
        str(current_project.get("id") or "")
        if isinstance(current_project, dict)
        else ""
    ) or workspace.name or str(workspace)
    observed_at = time.time()
    selected_platform = str(platform or "").strip().lower()
    if selected_platform == "gemini":
        service = tunnel_mcp_service or (
            gemini_gateway.tunnel_mcp_service if gemini_gateway is not None else None
        )
        if gemini_gateway is not None:
            activity = gemini_gateway.activity_snapshot()
        elif service is not None:
            activity = service.activity_snapshot("gemini")
        else:
            activity = {
                "call_count": 0,
                "active_calls": [],
                "recent_calls": [],
                "task_usage": None,
                "usage_scope": "tool_call",
                "usage_encoding": "o200k_base",
            }
        try:
            config, _authority = (
                gemini_gateway.current()
                if gemini_gateway is not None
                else (load_gemini_tunnel_config(), None)
            )
            config_snapshot = config.snapshot()
            authorization_snapshot = (
                gemini_gateway.authorization_snapshot()
                if gemini_gateway is not None
                else {
                    "pending": False,
                    "review_id": "",
                    "redirect_uri": "",
                    "redirect_host": "",
                    "expires_in": 0,
                }
            )
            config_error = ""
        except GeminiTunnelConfigError:
            config = GeminiTunnelConfig()
            config_snapshot = config.snapshot()
            authorization_snapshot = {
                "pending": False,
                "review_id": "",
                "redirect_uri": "",
                "redirect_host": "",
                "expires_in": 0,
            }
            config_error = "Gemini Tunnel configuration is unreadable. Clear and save it again."
        activity_observed = bool(activity.get("call_count"))
        if config_error:
            state = "error"
            presentation = {
                "tone": "error",
                "label": "Unavailable",
                "message": config_error,
                "hint": config_error,
                "action": None,
            }
        elif not config.configured:
            state = "not_configured"
            presentation = {
                "tone": "error",
                "label": "Not configured",
                "message": "Save a dedicated public HTTPS origin in Step 1.",
                "hint": "Save a dedicated public HTTPS origin in Step 1.",
                "action": None,
            }
        elif activity_observed:
            state = "active"
            presentation = {
                "tone": "ready",
                "label": "Active",
                "message": f"Authenticated Gemini tool call received for {project_name}.",
                "hint": "",
                "action": None,
            }
        else:
            state = "configured"
            presentation = {
                "tone": "configured",
                "label": "Configured",
                "message": "Awaiting the first authenticated Gemini tool call.",
                "hint": "Awaiting the first authenticated Gemini tool call.",
                "action": None,
            }
        return {
            **activity,
            "platform": "gemini",
            "state": state,
            "ready": activity_observed,
            "enabled": config.configured,
            "configured": config.configured,
            "activity_observed": activity_observed,
            "project_name": project_name,
            "project_context": project_context,
            "status_observed_at": observed_at,
            "credentials": {
                "configured": False,
                "tunnel_id": "",
                "tunnel_id_valid": False,
                "api_key_saved": False,
                "api_key_hint": "",
                "qualified": False,
            },
            "config": config_snapshot,
            "authorization": authorization_snapshot,
            "presentation": presentation,
        }

    if selected_platform != "chatgpt":
        return {
            "platform": selected_platform,
            "state": "unsupported",
            "ready": False,
            "enabled": False,
            "configured": False,
            "activity_observed": False,
            "active_calls": [],
            "recent_calls": [],
            "call_count": 0,
            "task_usage": None,
            "usage_scope": "tool_call",
            "usage_encoding": "o200k_base",
            "project_name": project_name,
            "project_context": project_context,
            "status_observed_at": observed_at,
            "credentials": {
                "configured": False,
                "tunnel_id": "",
                "tunnel_id_valid": False,
                "api_key_saved": False,
                "api_key_hint": "",
                "qualified": False,
            },
            "presentation": {
                "tone": "error",
                "label": "Unsupported",
                "message": (
                    "Tunnel is available for ChatGPT and Gemini. Choose one of "
                    "those services or switch to Browser."
                ),
                "hint": "",
                "action": None,
            },
        }
    snapshot = tunnel_runtime.snapshot()
    if tunnel_mcp_service is not None:
        activity = tunnel_mcp_service.activity_snapshot("chatgpt")
        snapshot.update(activity)
    credentials = load_tunnel_credentials(credentials_path).snapshot()
    tunnel_id = str(credentials.get("tunnel_id") or "")
    credentials["tunnel_id_valid"] = valid_tunnel_id(tunnel_id) if tunnel_id else False
    credentials["qualified"] = bool(
        credentials["tunnel_id_valid"] and credentials.get("api_key_saved")
    )
    ready_since = float(snapshot.get("ready_since") or 0)
    last_success_by_project = snapshot.get("last_success_by_project") or {}
    current_project_id = (
        str(current_project.get("id") or "")
        if isinstance(current_project, dict)
        else ""
    )
    last_success = (
        last_success_by_project.get(current_project_id)
        if isinstance(last_success_by_project, dict) and current_project_id
        else None
    )
    activity_observed = bool(
        snapshot.get("ready")
        and ready_since
        and isinstance(last_success, dict)
        and float(last_success.get("at") or 0) >= ready_since
    )
    snapshot["activity_observed"] = activity_observed
    return {
        **snapshot,
        "platform": "chatgpt",
        "project_name": project_name,
        "project_context": project_context,
        "status_observed_at": observed_at,
        "credentials": credentials,
        "presentation": describe_tunnel_status(
            snapshot,
            project_name=project_name,
            settings_url=url_for("settings.settings", _anchor="settings-llm"),
        ),
    }


def _no_store(response: Response) -> Response:
    """Prevent OAuth, MCP, and copied connection values from entering caches."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _oauth_error_response(error: GeminiOAuthError) -> Response:
    response = make_response(jsonify(error.as_dict()), error.status_code)
    if error.error == "invalid_client":
        response.headers["WWW-Authenticate"] = (
            'Basic realm="AgenticContext Gemini OAuth", charset="UTF-8"'
        )
    return _no_store(response)


def _oauth_authorization_waiting_response() -> Response:
    """Keep the provider browser waiting while the exact callback is reviewed."""
    response = make_response(
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="2">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Approve Gemini callback</title>
</head>
<body>
  <main>
    <h1>Review the Gemini callback in AgenticContext</h1>
    <p>This page will continue after you approve the exact callback in the local Agent UI.</p>
  </main>
</body>
</html>
""",
        202,
    )
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Refresh"] = "2"
    return _no_store(response)


def _read_bounded_request_body(max_bytes: int) -> bytes:
    """Cache at most one byte beyond a route limit so overflow is detectable."""
    request.max_content_length = max_bytes + 1
    if request.content_length is not None and request.content_length > max_bytes:
        raise RequestEntityTooLarge()
    body = request.get_data(cache=True)
    if len(body) > max_bytes:
        raise RequestEntityTooLarge()
    return body


def _redirect_with_oauth_values(
    redirect_uri: str,
    values: list[tuple[str, str]],
) -> str:
    """Append encoded OAuth values while preserving an exact registered query."""
    parsed = urlsplit(redirect_uri)
    query = urlencode([*parse_qsl(parsed.query, keep_blank_values=True), *values])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _one_form_value(name: str, *, required: bool = True) -> str:
    values = request.form.getlist(name)
    if len(values) > 1:
        raise GeminiOAuthError("invalid_request", f"OAuth parameter {name} is repeated.")
    value = values[0] if values else ""
    if required and not value:
        raise GeminiOAuthError("invalid_request", f"OAuth parameter {name} is required.")
    return value


def _oauth_client_credentials() -> tuple[str, str]:
    authorization = request.authorization
    form_client_id = _one_form_value("client_id", required=False)
    form_client_secret = _one_form_value("client_secret", required=False)
    if authorization is not None:
        if str(authorization.type or "").casefold() != "basic":
            raise GeminiOAuthError(
                "invalid_client",
                "OAuth client authentication must use HTTP Basic or form credentials.",
                status_code=401,
            )
        if form_client_id or form_client_secret:
            raise GeminiOAuthError(
                "invalid_client",
                "Use exactly one OAuth client authentication method.",
                status_code=401,
            )
        return str(authorization.username or ""), str(authorization.password or "")
    if not form_client_id or not form_client_secret:
        raise GeminiOAuthError(
            "invalid_client",
            "OAuth client credentials are required.",
            status_code=401,
        )
    return form_client_id, form_client_secret


def _bearer_token() -> str:
    values = request.headers.getlist("Authorization")
    if len(values) != 1:
        return ""
    scheme, separator, token = values[0].partition(" ")
    if separator != " " or scheme.casefold() != "bearer":
        return ""
    candidate = token.strip()
    if not candidate or len(candidate) > 8_192 or "," in candidate:
        return ""
    return candidate


def register_tunnel_routes(app: Flask, context: TunnelRouteContext) -> None:
    """Register the Tunnel transport, credential, and page routes on one blueprint."""
    blueprint = Blueprint(TUNNEL_BLUEPRINT_NAME, __name__)
    gemini_gateway = context.gemini_gateway

    def status_payload(platform: str = "chatgpt") -> dict[str, Any]:
        return tunnel_status_payload(
            context.settings_store,
            context.tunnel_runtime,
            platform=platform,
            tunnel_mcp_service=context.tunnel_mcp_service,
            gemini_gateway=gemini_gateway,
            credentials_path=context.credentials_path,
        )

    def reconnect_after_project_change(platform: str, saved_revision: int) -> None:
        """Refresh a qualified ChatGPT Tunnel after the project save commits."""
        if platform != "chatgpt":
            return
        credentials = load_tunnel_credentials(context.credentials_path)
        if (
            credentials.configured
            and valid_tunnel_id(credentials.tunnel_id)
            and credentials.api_key.startswith("sk-")
            and len(credentials.api_key) >= 12
        ):
            try:
                context.tunnel_runtime.connect_or_fail_closed(
                    expected_revision=saved_revision,
                    current_revision=lambda: (
                        context.tunnel_mcp_service.selection_store.load().revision
                    ),
                )
            except Exception:
                app.logger.error("Tunnel reconnect startup failed after a project change.")

    def route_browser() -> str:
        """Keep the saved Browser choice so switching back from Tunnel restores it."""
        browser = context.settings_store.settings.browser
        return browser if browser in context.available_agent_browser_keys() else "edge"

    @blueprint.post("/mcp")
    def mcp_endpoint():
        """Serve MCP tool calls forwarded by the local tunnel-client only."""
        if not is_loopback_address(request.remote_addr):
            abort(403)
        if not context.tunnel_runtime.authorization_matches(request.headers.get("Authorization")):
            return jsonify(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "Unauthorized."}}
            ), 401
        try:
            _read_bounded_request_body(MAX_MCP_BODY_BYTES)
        except RequestEntityTooLarge:
            return jsonify(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Request is too large."},
                }
            ), 413
        body = request.get_json(silent=True, force=True)
        if body is None:
            return jsonify(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error."}}
            ), 400
        status, payload = context.tunnel_mcp_service.handle(
            body,
            request.headers,
            provider="chatgpt",
        )
        if payload is None:
            return Response(status=status)
        return jsonify(payload), status

    @blueprint.get("/mcp")
    def mcp_stream():
        """This server answers JSON-RPC over POST only and offers no SSE stream."""
        return Response(status=405, headers={"Allow": "POST"})

    @blueprint.post("/api/agent/tunnel/credentials")
    def api_credentials():
        """Save a qualified Tunnel credential pair without echoing the API key."""
        context.require_local_agent_request()
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("tunnel_id"), str)
            or not isinstance(payload.get("api_key", ""), str)
        ):
            return jsonify({"error": "Send a JSON object with a Tunnel ID and optional API key."}), 400
        tunnel_id = payload["tunnel_id"].strip()
        submitted_key = payload.get("api_key", "").strip()
        current = load_tunnel_credentials(context.credentials_path)
        if not tunnel_id:
            next_credentials = merge_tunnel_credentials(current, "", "")
        else:
            if not valid_tunnel_id(tunnel_id):
                return jsonify(
                    {
                        "error": (
                            "Tunnel ID must look like tunnel_ followed by "
                            "32 lowercase hexadecimal characters."
                        )
                    }
                ), 400
            if submitted_key and (
                not submitted_key.startswith("sk-") or len(submitted_key) < 12
            ):
                return jsonify({"error": "OpenAI API key must start with sk-."}), 400
            next_credentials = merge_tunnel_credentials(
                current,
                tunnel_id,
                submitted_key,
            )
            if not next_credentials.api_key:
                return jsonify({"error": "Enter the API key for this Tunnel."}), 400
            if (
                not next_credentials.api_key.startswith("sk-")
                or len(next_credentials.api_key) < 12
            ):
                return jsonify({"error": "OpenAI API key must start with sk-."}), 400
        credentials_changed = next_credentials != current
        if credentials_changed:
            save_tunnel_credentials(next_credentials, context.credentials_path)
        if next_credentials.configured:
            # A successful re-save is also an explicit recovery request. This keeps
            # the existing credential pair while replacing an unhealthy client.
            context.tunnel_runtime.request_restart(delay=0.05)
        elif credentials_changed:
            # Revoke the live bearer and client immediately when credentials are cleared.
            context.tunnel_runtime.restart()
        return jsonify(status_payload())

    @blueprint.get("/api/agent/tunnel/status")
    def api_status():
        context.require_local_agent_request()
        platform = str(request.args.get("platform") or "chatgpt").strip().lower()
        if platform not in {"chatgpt", "gemini"}:
            return jsonify({"error": "Tunnel status supports ChatGPT and Gemini."}), 400
        return jsonify(status_payload(platform))

    @blueprint.route("/api/agent/tunnel/project", methods=["GET", "POST"])
    def api_project():
        """Read or select one registered local Tunnel project by exact identity."""
        context.require_local_agent_request()
        if request.method == "GET":
            return jsonify(
                {
                    "project_context": _local_project_context(
                        context.settings_store,
                        context.tunnel_mcp_service,
                    )
                }
            )

        payload = request.get_json(silent=True)
        legacy_fields = {"project_id", "expected_revision"}
        multi_fields = {
            "project_id",
            "selected_project_ids",
            "expected_revision",
        }
        payload_fields = frozenset(payload) if isinstance(payload, dict) else frozenset()
        selected_ids = payload.get("selected_project_ids") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload_fields not in {frozenset(legacy_fields), frozenset(multi_fields)}
            or not isinstance(payload.get("project_id"), str)
            or not isinstance(payload.get("expected_revision"), int)
            or isinstance(payload.get("expected_revision"), bool)
            or payload["expected_revision"] < 0
            or (
                payload_fields == multi_fields
                and (
                    not isinstance(selected_ids, list)
                    or not selected_ids
                    or any(not isinstance(project_id, str) for project_id in selected_ids)
                )
            )
        ):
            return jsonify(
                {
                    "error": (
                        "Send project_id, a non-negative expected_revision, and optionally "
                        "a non-empty selected_project_ids string list."
                    )
                }
            ), 400

        registry = context.tunnel_mcp_service.registry
        if registry.uses_fallback():
            return jsonify(
                {
                    "error": (
                        "Register this project in tunnel-projects.json before selecting "
                        "it for Tunnel tasks."
                    )
                }
            ), 409
        workspace_path = str(context.settings_store.settings.workspace_path or "")
        try:
            projects = registry.projects(workspace_path)
            previous_selection = context.tunnel_mcp_service.selection_store.load()
            previous_ids = {
                item.id for item in selected_projects(projects, previous_selection)
            }
            project = registry.resolve(payload["project_id"], workspace_path)
            problem = project_availability(project)
            if problem:
                return jsonify({"error": problem}), 409
            normalized_selected_ids: tuple[str, ...] | None = None
            if selected_ids is not None:
                normalized_selected_ids = tuple(selected_ids)
                for selected_id in normalized_selected_ids:
                    registry.resolve(selected_id, workspace_path)
                if project.id not in normalized_selected_ids:
                    return jsonify(
                        {"error": "The current Tunnel project must also be selected."}
                    ), 400
            saved_selection = context.tunnel_mcp_service.selection_store.save(
                project.id,
                selected_project_ids=normalized_selected_ids,
                expected_revision=payload["expected_revision"],
            )
            should_reconnect = (
                saved_selection.project_id != previous_selection.project_id
                or bool(
                    {
                        item.id for item in selected_projects(projects, saved_selection)
                    } - previous_ids
                )
            )
        except ProjectSelectionConflict as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "project_context": _local_project_context(
                        context.settings_store,
                        context.tunnel_mcp_service,
                    ),
                }
            ), 409
        except ProjectRegistryError as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError:
            return jsonify(
                {
                    "error": (
                        "The project selection could not be saved. Check the local "
                        "Tunnel configuration folder and try again."
                    )
                }
            ), 500

        platform = str(request.args.get("platform") or "chatgpt").strip().lower()
        if platform not in {"chatgpt", "gemini"}:
            platform = "chatgpt"
        if should_reconnect:
            reconnect_after_project_change(platform, saved_selection.revision)
        return jsonify(status_payload(platform))

    @blueprint.post("/api/agent/tunnel/project/register")
    def api_project_register():
        """Register one chosen folder as a writable project and make it current."""
        context.require_local_agent_request()
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"path", "expected_revision"}
            or not isinstance(payload.get("path"), str)
            or len(payload["path"]) > 4_096
            or not isinstance(payload.get("expected_revision"), int)
            or isinstance(payload.get("expected_revision"), bool)
            or payload["expected_revision"] < 0
        ):
            return jsonify(
                {"error": "Send one absolute path and a non-negative expected_revision."}
            ), 400
        registry = context.tunnel_mcp_service.registry
        selection_store = context.tunnel_mcp_service.selection_store
        workspace_path = str(context.settings_store.settings.workspace_path or "")
        try:
            previous_selection = selection_store.load()
            if previous_selection.revision != payload["expected_revision"]:
                raise ProjectSelectionConflict(
                    "The current project changed in another window. Review the selection "
                    "and choose again."
                )
            previous_ids = {
                item.id for item in selected_projects(
                    registry.projects(workspace_path), previous_selection
                )
            }
            project = registry.register(payload["path"], workspace_path)
            problem = project_availability(project)
            if problem:
                return jsonify({"error": problem}), 409
            projects = registry.projects(workspace_path)
            selection = selection_store.load()
            selected_ids = [item.id for item in selected_projects(projects, selection)]
            if project.id not in selected_ids:
                selected_ids.append(project.id)
            saved_selection = selection_store.save(
                project.id,
                selected_project_ids=tuple(selected_ids),
                expected_revision=payload["expected_revision"],
            )
            should_reconnect = (
                saved_selection.project_id != previous_selection.project_id
                or project.id not in previous_ids
            )
        except ProjectSelectionConflict as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "project_context": _local_project_context(
                        context.settings_store,
                        context.tunnel_mcp_service,
                    ),
                }
            ), 409
        except ProjectRegistryError as exc:
            return jsonify({"error": str(exc)}), 400
        except (OSError, ValueError, KeyError, TypeError):
            return jsonify(
                {
                    "error": (
                        "The project could not be registered. Check the local Tunnel "
                        "configuration folder and try again."
                    )
                }
            ), 500

        platform = str(request.args.get("platform") or "chatgpt").strip().lower()
        if platform not in {"chatgpt", "gemini"}:
            platform = "chatgpt"
        if should_reconnect:
            reconnect_after_project_change(platform, saved_selection.revision)
        return jsonify(status_payload(platform))

    @blueprint.post("/api/agent/tunnel/project/unregister")
    def api_project_unregister():
        """Revoke one local Tunnel mapping while retaining its folder on disk."""
        context.require_local_agent_request()
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"project_id", "expected_revision"}
            or not isinstance(payload.get("project_id"), str)
            or not isinstance(payload.get("expected_revision"), int)
            or isinstance(payload.get("expected_revision"), bool)
            or payload["expected_revision"] < 0
        ):
            return jsonify(
                {"error": "Send a project_id and a non-negative expected_revision."}
            ), 400

        registry = context.tunnel_mcp_service.registry
        selection_store = context.tunnel_mcp_service.selection_store
        try:
            selection = selection_store.load()
            if selection.revision != payload["expected_revision"]:
                raise ProjectSelectionConflict(
                    "The current project changed in another window. Review the selection "
                    "and choose again."
                )
            projects = registry.projects()
            preferred_ids = [item.id for item in selected_projects(projects, selection)]
            remaining = registry.unregister(payload["project_id"])
            remaining_ids = {item.id for item in remaining}
            selected_ids = [item for item in preferred_ids if item in remaining_ids]
            if remaining and not selected_ids:
                selected_ids = [remaining[0].id]
            if selection.project_id in selected_ids:
                current_id = selection.project_id
            else:
                current_id = selected_ids[0] if selected_ids else ""
            selection_store.save(
                current_id,
                selected_project_ids=selected_ids,
                expected_revision=payload["expected_revision"],
            )
        except ProjectSelectionConflict as exc:
            return jsonify(
                {
                    "error": str(exc),
                    "project_context": _local_project_context(
                        context.settings_store,
                        context.tunnel_mcp_service,
                    ),
                }
            ), 409
        except ProjectRegistryError as exc:
            return jsonify({"error": str(exc)}), 400
        except (OSError, ValueError, KeyError, TypeError):
            return jsonify(
                {"error": "The Tunnel project mapping could not be removed. Try again."}
            ), 500

        platform = str(request.args.get("platform") or "chatgpt").strip().lower()
        if platform not in {"chatgpt", "gemini"}:
            platform = "chatgpt"
        return jsonify(status_payload(platform))

    @blueprint.post("/api/agent/tunnel/gemini/config")
    def api_gemini_config():
        """Save or revoke the Gemini public origin without returning secrets."""
        context.require_local_agent_request()
        payload = request.get_json(silent=True)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"public_origin"}
            or not isinstance(payload.get("public_origin"), str)
        ):
            return jsonify({"error": "Send one public_origin string."}), 400
        try:
            gemini_gateway.replace(payload["public_origin"].strip())
        except GeminiTunnelConfigError as exc:
            return jsonify({"error": str(exc)}), 400
        return _no_store(make_response(jsonify(status_payload("gemini"))))

    @blueprint.post("/api/agent/tunnel/gemini/copy-value")
    def api_gemini_copy_value():
        """Release one connection value only after an explicit local UI action."""
        context.require_local_agent_request()
        payload = request.get_json(silent=True)
        kind = str(payload.get("value") or "") if isinstance(payload, dict) else ""
        if not isinstance(payload, dict) or set(payload) != {"value"} or kind not in {
            "mcp_url",
            "client_id",
            "client_secret",
        }:
            return jsonify({"error": "Choose mcp_url, client_id, or client_secret."}), 400
        try:
            config, _authority = gemini_gateway.current()
        except GeminiTunnelConfigError:
            return jsonify({"error": "Gemini Tunnel configuration is unreadable."}), 409
        if not config.configured:
            return jsonify({"error": "Save the Gemini public origin first."}), 409
        value = {
            "mcp_url": config.mcp_url,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        }[kind]
        return _no_store(make_response(jsonify({"value": value})))

    @blueprint.post("/api/agent/tunnel/gemini/authorization")
    def api_gemini_authorization():
        """Approve or deny the exact pending callback from the local Agent UI."""
        context.require_local_agent_request()
        payload = request.get_json(silent=True)
        action = str(payload.get("action") or "") if isinstance(payload, dict) else ""
        review_id = (
            str(payload.get("review_id") or "") if isinstance(payload, dict) else ""
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"action", "review_id"}
            or action not in {"approve", "deny"}
            or GEMINI_AUTHORIZATION_REVIEW_ID_RE.fullmatch(review_id) is None
        ):
            return jsonify({"error": "Choose approve or deny for one pending review."}), 400
        try:
            gemini_gateway.resolve_pending_authorization(action, review_id)
        except GeminiOAuthError as exc:
            return _no_store(make_response(jsonify({"error": exc.description}), 409))
        except GeminiTunnelConfigError:
            return _no_store(
                make_response(
                    jsonify({"error": "Gemini Tunnel is not configured."}),
                    409,
                )
            )
        return _no_store(make_response(jsonify(status_payload("gemini"))))

    @blueprint.get("/.well-known/oauth-protected-resource")
    @blueprint.get("/.well-known/oauth-protected-resource/mcp/gemini")
    def gemini_protected_resource_metadata():
        gemini_gateway.require_public_request()
        try:
            config, _authority = gemini_gateway.authority()
        except GeminiTunnelConfigError:
            abort(404)
        return _no_store(make_response(jsonify(protected_resource_metadata(config))))

    @blueprint.get("/.well-known/oauth-authorization-server")
    def gemini_authorization_server_metadata():
        gemini_gateway.require_public_request()
        try:
            config, _authority = gemini_gateway.authority()
        except GeminiTunnelConfigError:
            abort(404)
        return _no_store(make_response(jsonify(authorization_server_metadata(config))))

    @blueprint.get("/oauth/authorize")
    def gemini_oauth_authorize():
        """Authorize the locally provisioned confidential Gemini client."""
        gemini_gateway.require_public_request()
        if request.method != "GET":
            response = make_response("", 405)
            response.headers["Allow"] = "GET"
            return _no_store(response)
        try:
            if any(
                len(request.args.getlist(name)) != 1
                for name in (
                    "response_type",
                    "client_id",
                    "redirect_uri",
                    "resource",
                    "scope",
                    "code_challenge",
                    "code_challenge_method",
                )
            ):
                raise GeminiOAuthError(
                    "invalid_request",
                    "Each required authorization parameter must appear exactly once.",
                )
            states = request.args.getlist("state")
            if len(states) > 1 or (
                states and (not states[0] or len(states[0]) > 2_048)
            ):
                raise GeminiOAuthError("invalid_request", "OAuth state is invalid.")
            if request.args.get("response_type") != "code":
                raise GeminiOAuthError(
                    "unsupported_response_type",
                    "Only the authorization code response type is supported.",
                )
            config, code, redirect_uri = gemini_gateway.stage_or_issue_authorization(
                state=states[0] if states else "",
                client_id=request.args["client_id"],
                redirect_uri=request.args["redirect_uri"],
                resource=request.args["resource"],
                scope=request.args["scope"],
                code_challenge=request.args["code_challenge"],
                code_challenge_method=request.args["code_challenge_method"],
            )
            if code is None:
                return _oauth_authorization_waiting_response()
            values = [("code", code), ("iss", config.public_origin)]
            if states:
                values.append(("state", states[0]))
            response = redirect(_redirect_with_oauth_values(redirect_uri, values), code=302)
            return _no_store(response)
        except GeminiOAuthError as exc:
            return _oauth_error_response(exc)
        except GeminiTunnelConfigError:
            abort(404)

    @blueprint.post("/oauth/token")
    def gemini_oauth_token():
        gemini_gateway.require_public_request()
        try:
            _read_bounded_request_body(MAX_OAUTH_FORM_BYTES)
            if request.mimetype != "application/x-www-form-urlencoded":
                raise GeminiOAuthError(
                    "invalid_request",
                    "OAuth token requests must use application/x-www-form-urlencoded.",
                )
            _config, authority = gemini_gateway.authority()
            client_id, client_secret = _oauth_client_credentials()
            grant_type = _one_form_value("grant_type")
            resource = _one_form_value("resource")
            scope = _one_form_value("scope", required=False) or MCP_SCOPE
            if grant_type == "authorization_code":
                token_payload = authority.exchange_authorization_code(
                    code=_one_form_value("code"),
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=_one_form_value("redirect_uri"),
                    resource=resource,
                    scope=scope,
                    code_verifier=_one_form_value("code_verifier"),
                )
            elif grant_type == "refresh_token":
                token_payload = authority.refresh_access_token(
                    refresh_token=_one_form_value("refresh_token"),
                    client_id=client_id,
                    client_secret=client_secret,
                    resource=resource,
                    scope=scope,
                )
            else:
                raise GeminiOAuthError(
                    "unsupported_grant_type",
                    "Only authorization_code and refresh_token are supported.",
                )
            return _no_store(make_response(jsonify(token_payload)))
        except RequestEntityTooLarge:
            return _oauth_error_response(
                GeminiOAuthError(
                    "invalid_request",
                    "OAuth form is too large.",
                    status_code=413,
                )
            )
        except GeminiOAuthError as exc:
            return _oauth_error_response(exc)
        except GeminiTunnelConfigError:
            abort(404)

    def gemini_mcp_unauthorized(
        config: GeminiTunnelConfig,
        *,
        error: str = "invalid_token",
        description: str = "A valid Gemini OAuth bearer token is required.",
    ) -> Response:
        response = make_response(
            jsonify(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": "Unauthorized."},
                }
            ),
            401,
        )
        response.headers["WWW-Authenticate"] = www_authenticate_challenge(
            config,
            error=error,
            error_description=description,
        )
        return _no_store(response)

    def authenticate_gemini_mcp() -> tuple[GeminiTunnelConfig, GeminiOAuthAuthority] | Response:
        try:
            config, authority = gemini_gateway.authority()
        except GeminiTunnelConfigError:
            abort(404)
        token = _bearer_token()
        if not token:
            return gemini_mcp_unauthorized(config)
        try:
            authority.verify_access_token(token, resource=config.mcp_url)
        except GeminiOAuthError as exc:
            return gemini_mcp_unauthorized(
                config,
                error="invalid_token",
                description=exc.description,
            )
        return config, authority

    @blueprint.post("/mcp/gemini")
    def gemini_mcp_endpoint():
        gemini_gateway.require_public_request()
        authenticated = authenticate_gemini_mcp()
        if isinstance(authenticated, Response):
            return authenticated
        try:
            _read_bounded_request_body(MAX_GEMINI_MCP_BODY_BYTES)
            body = request.get_json(silent=True)
        except RequestEntityTooLarge:
            return _no_store(
                make_response(
                    jsonify(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32600, "message": "Request is too large."},
                        }
                    ),
                    413,
                )
            )
        if body is None:
            return _no_store(
                make_response(
                    jsonify(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Parse error."},
                        }
                    ),
                    400,
                )
            )
        status, payload = context.tunnel_mcp_service.handle(
            body,
            request.headers,
            provider="gemini",
        )
        if payload is None:
            return _no_store(Response(status=status))
        return _no_store(make_response(jsonify(payload), status))

    @blueprint.get("/mcp/gemini")
    def gemini_mcp_stream():
        """Advertise OAuth before declining the optional SSE stream."""
        gemini_gateway.require_public_request()
        authenticated = authenticate_gemini_mcp()
        if isinstance(authenticated, Response):
            return authenticated
        return _no_store(Response(status=405, headers={"Allow": "POST"}))

    @blueprint.post("/api/agent/tunnel/restart")
    def api_restart():
        context.require_local_agent_request()
        context.tunnel_runtime.restart()
        return jsonify(status_payload())

    @blueprint.post("/api/agent/tunnel/connect")
    def api_connect():
        """Start or reconnect the Tunnel using the saved credential pair."""
        context.require_local_agent_request()
        credentials = load_tunnel_credentials(context.credentials_path)
        if (
            not credentials.configured
            or not valid_tunnel_id(credentials.tunnel_id)
            or not credentials.api_key.startswith("sk-")
            or len(credentials.api_key) < 12
        ):
            return jsonify({"error": "Save a qualified Tunnel ID and API key first."}), 409
        context.tunnel_runtime.connect()
        return jsonify(status_payload())

    @blueprint.post("/api/agent/tunnel/disconnect")
    def api_disconnect():
        """Stop Tunnel forwarding without deleting the saved credentials."""
        context.require_local_agent_request()
        context.tunnel_runtime.disconnect()
        return jsonify(status_payload())

    @blueprint.get("/agent/tunnel/")
    def agent_tunnel():
        """Open the Tunnel connection for the saved Web service."""
        context.require_local_agent_request(allow_locked=True)
        return redirect(
            url_for(
                f"{TUNNEL_BLUEPRINT_NAME}.agent_tunnel_selected",
                platform=default_tunnel_platform(context.settings_store),
            ),
            code=302,
        )

    @blueprint.get("/agent/tunnel/<platform>")
    def agent_tunnel_selected(platform: str):
        """Render the Agent page with the Tunnel connection selected."""
        context.require_local_agent_request(allow_locked=True)
        selected_platform = platform.strip().lower()
        if selected_platform not in SUPPORTED_AGENT_PLATFORMS:
            abort(404)
        if not context.is_agent_access_unlocked():
            return context.render_locked_agent_access()
        return context.render_agent_page(route_browser(), selected_platform, "tunnel")

    app.register_blueprint(blueprint)


__all__ = [
    "GEMINI_PUBLIC_PATHS",
    "TUNNEL_BLUEPRINT_NAME",
    "GeminiTunnelGateway",
    "TunnelRouteContext",
    "default_tunnel_platform",
    "register_tunnel_routes",
    "tunnel_status_payload",
]
