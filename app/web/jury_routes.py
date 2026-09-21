"""Browser Jury routes: page rendering, deliberation control, and session archives.

The Jury owns its own request handling and its own presentation of a deliberation
snapshot. The application factory builds the service and hands this module the Agent
request gate it shares with the rest of the Agent surface.
"""

# Code version: v1.1.0-codex.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from flask import (
    Blueprint,
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from app.core.agent import (
    AGENT_PLATFORM_OPTIONS,
    JURY_MODEL_OPTIONS_BY_PROVIDER,
    ComputerUseSettingsStore,
    JuryService,
    browser_options_for_host,
)
from app.core.foundation import APP_VERSION
from app.web.presentation import render_prompt_markdown


JURY_BLUEPRINT_NAME = "jury"


@dataclass(frozen=True, slots=True)
class JuryRouteContext:
    """The Agent-surface capabilities the Jury routes borrow from the factory."""

    settings_store: ComputerUseSettingsStore
    jury_service: JuryService
    require_local_agent_request: Callable[..., None]
    available_agent_browser_keys: Callable[[], set[str]]
    external_agent_operations_enabled: Callable[[], bool]
    reject_external_agent_operation: Callable[[], Any]


def jury_snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Render one deliberation snapshot's Markdown into the shape the page consumes."""
    result = dict(snapshot)
    result["response_html"] = str(render_prompt_markdown(str(result.get("response") or "")))
    result["rounds"] = [
        {**item, "opinions": [
            {**opinion, "response_html": str(render_prompt_markdown(
                str(opinion.get("conclusion") or opinion.get("response") or "")
            ))}
            for opinion in item.get("opinions", [])
        ]}
        for item in result.get("rounds", [])
    ]
    return result


def register_jury_routes(app: Flask, context: JuryRouteContext) -> None:
    """Register the Jury page and deliberation API on one blueprint."""
    blueprint = Blueprint(JURY_BLUEPRINT_NAME, __name__)
    jury_service = context.jury_service

    def jury_payload() -> dict[str, Any]:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            abort(make_response(jsonify({"error": "Send a JSON object."}), 400))
        return payload

    def jury_snapshot_response(snapshot: dict[str, Any]):
        return jsonify(jury_snapshot_payload(snapshot))

    @blueprint.get("/jury")
    def jury():
        context.require_local_agent_request()
        return redirect(url_for(f"{JURY_BLUEPRINT_NAME}.jury_selected", browser="edge"))

    @blueprint.get("/jury/<browser>")
    def jury_selected(browser: str):
        context.require_local_agent_request()
        if browser not in context.available_agent_browser_keys():
            abort(404)
        return render_template(
            "jury.html", version=APP_VERSION,
            settings=replace(context.settings_store.settings, browser=browser),
            browser_options=browser_options_for_host(),
            platform_options=AGENT_PLATFORM_OPTIONS,
            jury_model_options_by_provider=JURY_MODEL_OPTIONS_BY_PROVIDER,
        )

    @blueprint.post("/api/jury/check")
    def jury_check():
        context.require_local_agent_request()
        if not context.external_agent_operations_enabled():
            return context.reject_external_agent_operation()
        payload = jury_payload()
        try:
            return jsonify(jury_service.check(
                payload.get("browser", "edge"), payload.get("providers"),
                payload.get("models"),
            ))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409

    @blueprint.post("/api/jury/start")
    def jury_start():
        context.require_local_agent_request()
        if not context.external_agent_operations_enabled():
            return context.reject_external_agent_operation()
        payload = jury_payload()
        try:
            snapshot = jury_service.start(
                payload.get("browser", "edge"), payload.get("providers"),
                payload.get("question"), payload.get("max_rounds"),
                payload.get("models"),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except (RuntimeError, OSError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jury_snapshot_response(snapshot), 202

    @blueprint.get("/api/jury/status")
    def jury_status():
        context.require_local_agent_request()
        try:
            return jury_snapshot_response(jury_service.status(
                request.args.get("session_id", ""),
            ))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @blueprint.get("/api/jury/sessions")
    def jury_sessions():
        context.require_local_agent_request()
        browser = request.args.get("browser", "edge")
        if browser not in context.available_agent_browser_keys():
            return jsonify({"error": "Choose a supported browser."}), 400
        return jsonify(jury_service.sessions(browser))

    @blueprint.post("/api/jury/stop")
    def jury_stop():
        context.require_local_agent_request()
        payload = jury_payload()
        session_id = payload.get("session_id")
        if not isinstance(session_id, str):
            return jsonify({"error": "Choose a jury session."}), 400
        try:
            return jury_snapshot_response(jury_service.stop(session_id))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @blueprint.delete("/api/jury/session")
    def delete_failed_jury_session():
        """Dismiss one fully cleaned failed Jury session from the local archive."""
        context.require_local_agent_request()
        if not context.external_agent_operations_enabled():
            return context.reject_external_agent_operation()
        payload = jury_payload()
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return jsonify({"error": "Choose a jury session."}), 400
        try:
            dismissed = jury_service.dismiss_failed(session_id.strip())
        except ValueError as exc:
            return jsonify({"error": str(exc), "code": "unknown_jury_session"}), 404
        except (RuntimeError, OSError) as exc:
            return jsonify({"error": str(exc), "code": "jury_session_not_deletable"}), 409
        return jsonify({"deleted": True, **dismissed})

    app.register_blueprint(blueprint)


__all__ = [
    "JURY_BLUEPRINT_NAME",
    "JuryRouteContext",
    "jury_snapshot_payload",
    "register_jury_routes",
]
