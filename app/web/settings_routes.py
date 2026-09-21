"""Settings routes: the configuration page, persistence, and local-directory pickers.

The page shows configuration owned by several domains, so the factory hands this
module the two cache snapshots it renders and the stores it writes. Form parsing lives
in ``app.web.form_config``; this module owns validation of what a submitted form means
for Agent settings, shadow backup, and local directories.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
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
    is_loopback_address,
    validate_computer_use_settings,
)
from app.core.foundation import (
    APP_VERSION,
    DEFAULT_HOST,
    DEFAULT_PORT,
    get_log_file_path,
)
from app.core.storage import (
    ShadowBackupError,
    choose_settings_directory,
    choose_shadow_backup_destination,
)
from app.web.config_store import SavedConfigStore
from app.web.form_config import parse_form_config
from app.web.presentation import validate_local_directory_path
from app.web.token_registry import build_style_token_component_rows


SETTINGS_BLUEPRINT_NAME = "settings"


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
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "The folder picker is only available on the local host."}), 403

        payload = request.get_json(silent=True) or {}
        requested_initial_path = payload.get("initial_path")
        initial_value = (
            requested_initial_path.strip()
            if isinstance(requested_initial_path, str)
            else str(context.config_store.config.shadow_backup_destination)
        )
        try:
            selected_path = choose_shadow_backup_destination(
                Path(initial_value or str(context.config_store.config.shadow_backup_destination))
            )
        except ShadowBackupError as exc:
            return jsonify({"error": str(exc)}), 500

        if selected_path is None:
            return jsonify({"cancelled": True})
        return jsonify({"destination": str(selected_path)})

    @blueprint.post("/api/settings/directory")
    def choose_settings_directory_route():
        if not is_loopback_address(request.remote_addr):
            return jsonify({"error": "The folder picker is only available on the local host."}), 403

        directory_options = {
            "chrome_user_data_dir": (
                context.config_store.config.chrome_user_data_dir,
                "Select Chrome user data directory",
            ),
            "shadow_backup_destination": (
                context.config_store.config.shadow_backup_destination,
                "Select shadow cloud backup destination",
            ),
            "agent_allowed_root": (
                Path(context.computer_use_settings.settings.workspace_path),
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

    @blueprint.post("/api/settings/directory/validate")
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


    app.register_blueprint(blueprint)


__all__ = [
    "SETTINGS_BLUEPRINT_NAME",
    "SettingsRouteContext",
    "register_settings_routes",
]
