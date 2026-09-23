"""Settings routes: the configuration page, persistence, and local-directory pickers.

The page shows configuration owned by several domains, so the factory hands this
module the two cache snapshots it renders and the stores it writes. Form parsing lives
in ``app.web.form_config``; this module owns validation of what a submitted form means
for Agent settings, shadow backup, and local directories.
"""

# Code version: v1.3.0-codex.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from flask import (
    Blueprint,
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from app.core.agent import (
    ComputerUseSettingsStore,
    default_tunnel_browse_root,
    is_loopback_address,
    validate_computer_use_settings,
)
from app.core.foundation import (
    APP_VERSION,
    DEFAULT_HOST,
    DEFAULT_PORT,
    get_log_file_path,
)
from app.core.native_directory_picker import (
    NativeDirectoryPickerError,
    choose_native_directory,
)
from app.core.storage import (
    SettingsDirectoryBrowserError,
    ShadowBackupError,
    browse_settings_directory,
)
from app.web.config_store import SavedConfigStore
from app.web.form_config import parse_form_config
from app.web.presentation import validate_local_directory_path
from app.web.token_registry import build_style_token_component_rows


SETTINGS_BLUEPRINT_NAME = "settings"
_native_picker_lock = Lock()


@dataclass(frozen=True, slots=True)
class SettingsRouteContext:
    """What the Settings surface writes, plus the cache snapshots its page renders."""

    config_store: SavedConfigStore
    computer_use_settings: ComputerUseSettingsStore
    shadow_backup_service: Any
    media_catalog: Any
    build_reconciled_grok_snapshot: Callable[[], dict[str, Any]]
    build_reconciled_chatgpt_snapshot: Callable[[], dict[str, Any]]


def register_settings_routes(app: Flask, context: SettingsRouteContext) -> None:
    """Register the Settings page, persistence, and directory-picker routes."""
    blueprint = Blueprint(SETTINGS_BLUEPRINT_NAME, __name__)

    @blueprint.get("/settings")
    def settings():
        grok_snapshot = context.build_reconciled_grok_snapshot()
        chatgpt_snapshot = context.build_reconciled_chatgpt_snapshot()
        return render_template(
            "settings.html",
            grok_snapshot=grok_snapshot,
            chatgpt_snapshot=chatgpt_snapshot,
            version=APP_VERSION,
            default_host=DEFAULT_HOST,
            default_port=DEFAULT_PORT,
            saved_config=context.config_store.config,
            log_file_path=str(get_log_file_path()),
            local_store_root=str(context.media_catalog.local_store_root),
            shadow_backup_snapshot=context.shadow_backup_service.snapshot(),
            agent_settings=context.computer_use_settings.settings,
            agent_runtime_snapshot=context.computer_use_settings.snapshot(),
        )

    @blueprint.get("/settings/style-tokens")
    def settings_style_tokens():
        return render_template(
            "settings_style_tokens.html",
            version=APP_VERSION,
            style_token_rows=build_style_token_component_rows(),
        )

    @blueprint.post("/settings")
    def save_settings():
        context.config_store.replace(parse_form_config(context.config_store.config))
        agent_field_names = {
            "agent_operating_system",
            "agent_context_limit_mib",
            "agent_max_turns",
            "agent_command_timeout_seconds",
            "agent_macos_system_prompt",
            "agent_windows_system_prompt",
        }
        if any(request.form.get(field_name) is not None for field_name in agent_field_names):
            candidate_payload = asdict(context.computer_use_settings.settings)
            candidate_payload.update(
                {
                    "operating_system": request.form.get(
                        "agent_operating_system",
                        context.computer_use_settings.settings.operating_system,
                    ),
                    "context_limit_mib": request.form.get(
                        "agent_context_limit_mib",
                        context.computer_use_settings.settings.context_limit_mib,
                    ),
                    "max_turns": request.form.get(
                        "agent_max_turns",
                        context.computer_use_settings.settings.max_turns,
                    ),
                    "command_timeout_seconds": request.form.get(
                        "agent_command_timeout_seconds",
                        context.computer_use_settings.settings.command_timeout_seconds,
                    ),
                    "macos_system_prompt": request.form.get(
                        "agent_macos_system_prompt",
                        context.computer_use_settings.settings.macos_system_prompt,
                    ),
                    "windows_system_prompt": request.form.get(
                        "agent_windows_system_prompt",
                        context.computer_use_settings.settings.windows_system_prompt,
                    ),
                }
            )
            try:
                context.computer_use_settings.update(validate_computer_use_settings(candidate_payload))
            except (RuntimeError, ValueError):
                pass
        return redirect(url_for(f"{SETTINGS_BLUEPRINT_NAME}.settings"))

    @blueprint.post("/settings/shadow-backup/sync")
    def start_shadow_backup_sync():
        context.config_store.replace(parse_form_config(context.config_store.config))
        try:
            context.shadow_backup_service.start(context.config_store.config)
        except ShadowBackupError as exc:
            context.shadow_backup_service.record_start_error(exc)
        return redirect(url_for(f"{SETTINGS_BLUEPRINT_NAME}.settings", _anchor="settings-cloud"))

    @blueprint.get("/api/settings/shadow-backup/status")
    def api_shadow_backup_status():
        return jsonify(context.shadow_backup_service.snapshot())

    @blueprint.post("/api/settings/shadow-backup/destination")
    def choose_shadow_backup_destination_route():
        """Fail closed for cached clients that still call the retired native picker."""
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "Directory browsing is only available on the local host."}), 403
        return (
            jsonify(
                {
                    "error": (
                        "The native folder picker has been retired. "
                        "Reload this page to use the in-page directory browser."
                    )
                }
            ),
            410,
        )

    @blueprint.post("/api/settings/directory")
    def choose_settings_directory_route():
        """Return one level of local directories for the in-page folder browser."""
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "Directory browsing is only available on the local host."}), 403

        directory_options = {
            "chrome_user_data_dir": context.config_store.config.chrome_user_data_dir,
            "shadow_backup_destination": context.config_store.config.shadow_backup_destination,
            "agent_allowed_root": Path(context.computer_use_settings.settings.workspace_path),
            "tunnel_project_root": default_tunnel_browse_root(),
        }
        payload = request.get_json(silent=True) or {}
        field_name = payload.get("field")
        if field_name not in directory_options:
            return jsonify({"error": "Unknown Settings directory field."}), 400

        default_path = Path(directory_options[field_name])
        requested_path = payload.get("path")
        path_value = requested_path.strip() if isinstance(requested_path, str) else str(default_path)
        if len(path_value) > 4_096:
            return jsonify({"error": "The directory path is too long."}), 400
        try:
            listing = browse_settings_directory(
                Path(path_value or str(default_path)),
                fallback_path=default_path,
                recover_invalid=payload.get("recover_invalid") is True,
            )
        except SettingsDirectoryBrowserError as exc:
            return jsonify({"error": str(exc)}), 400

        body = {
            "current_path": str(listing.path),
            "parent_path": str(listing.parent) if listing.parent is not None else "",
            "breadcrumbs": [
                {"label": label, "path": str(path)}
                for label, path in listing.breadcrumbs
            ],
            "directories": [
                {
                    "name": entry.name,
                    "path": str(entry.path),
                    "is_symlink": entry.is_symlink,
                    "accessible": entry.accessible,
                    "reason": entry.reason,
                }
                for entry in listing.directories
            ],
            "recovered": bool(listing.recovered_from),
        }
        if listing.recovered_from:
            body["notice"] = "The starting path was unavailable. Opened the nearest readable directory."
        return jsonify(body)

    @blueprint.post("/api/settings/directory/native")
    def choose_native_tunnel_directory_route():
        """Open one system folder panel for the local Tunnel project control."""
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "Folder selection is only available on the local host."}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("field") != "tunnel_project_root":
            return jsonify({"error": "Unknown native directory field."}), 400
        requested_path = payload.get("path")
        if requested_path is not None and (
            not isinstance(requested_path, str) or len(requested_path) > 4_096
        ):
            return jsonify({"error": "Invalid starting directory."}), 400
        initial_path = (
            Path(requested_path.strip())
            if requested_path and requested_path.strip()
            else default_tunnel_browse_root()
        )
        if not initial_path.is_absolute():
            return jsonify({"error": "The starting directory must be absolute."}), 400
        if not _native_picker_lock.acquire(blocking=False):
            return jsonify({"error": "A system folder picker is already open."}), 409
        try:
            selected = choose_native_directory(
                initial_path,
                "Choose a registered Tunnel project folder",
            )
        except NativeDirectoryPickerError as exc:
            return jsonify({"error": str(exc)}), 503
        finally:
            _native_picker_lock.release()
        return jsonify({"cancelled": selected is None, "path": str(selected) if selected else ""})

    @blueprint.post("/api/settings/directory/validate")
    def validate_settings_directory_route():
        """Validate a manually-entered directory path without opening a native picker."""
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "Path validation is only available on the local host."}), 403
        payload = request.get_json(silent=True) or {}
        raw_value = payload.get("path")
        raw_path = raw_value.strip() if isinstance(raw_value, str) else ""
        valid, reason, resolved = validate_local_directory_path(raw_path)
        body: dict[str, Any] = {"valid": valid, "reason": reason}
        if resolved:
            body["path"] = resolved
        return jsonify(body)
    app.register_blueprint(blueprint)


__all__ = [
    "SETTINGS_BLUEPRINT_NAME",
    "SettingsRouteContext",
    "register_settings_routes",
]
