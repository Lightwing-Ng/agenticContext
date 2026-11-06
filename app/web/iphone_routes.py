"""iPhone routes: the USB iPhone page, its setup steps, live preview, and phone actions.

Every API requires the shared Agent request gate and, beyond it, a request from this Mac
itself: an unlocked LAN session may read Agent pages, but it may not see or drive the
phone. Anything that creates or touches the runtime also requires external Agent
operations, so isolated and test applications never reach a device.
"""

# Code version: v1.1.1-codex.0

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import re
from threading import Lock
from typing import Any

from flask import Blueprint, Flask, Response, jsonify, render_template, request

from app.core.agent import (
    ComputerUseSettingsStore,
    IPhoneOperationError,
    IPhoneService,
    IPhoneUnavailableError,
    is_loopback_address,
)
from app.core.foundation import APP_VERSION


IPHONE_BLUEPRINT_NAME = "iphone"
IPHONE_FRAME_PATH = "/api/iphone/screen/frame"
MAX_IPHONE_BODY_BYTES = 64 * 1024
MAX_FRAME_CURSOR = 2**53 - 1
FRAME_CURSOR_PATTERN = re.compile(r"[0-9]{1,16}")
IPHONE_ERROR_STATUS: dict[str, int] = {
    "invalid_argument": 400,
    "newline_requires_intent": 400,
    "unknown_tool": 400,
    "iphone_access_disabled": 403,
    "operations_disabled": 409,
    "device_busy": 409,
    "preview_paused": 409,
    "phone_locked": 409,
    "action_uncertain": 409,
    "input_continuation_expired": 409,
    "pua_unreachable": 503,
    "not_ready": 503,
    "mirroring_conflict": 503,
    "pua_recovery_required": 503,
}
SCREENSHOT_FILENAME = "iphone-screenshot.png"

_frame_log_filter_lock = Lock()
_frame_log_filter_installed = False


@dataclass(frozen=True, slots=True)
class IphoneRouteContext:
    """The Agent-surface capabilities the iPhone routes borrow from the factory."""

    settings_store: ComputerUseSettingsStore
    iphone_service: IPhoneService
    require_local_agent_request: Callable[..., None]
    external_agent_operations_enabled: Callable[[], bool]
    reject_external_agent_operation: Callable[[], Any]


class IphoneRequestError(ValueError):
    """A request whose shape the iPhone API cannot accept."""

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class FramePollAccessLogFilter(logging.Filter):
    """Drop successful frame-poll access lines; the page polls about four times a second."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 3:
            return True
        request_line, status = str(args[0]), str(args[1])
        return not (f" {IPHONE_FRAME_PATH}" in request_line and status.startswith("2"))


def install_frame_access_log_filter() -> None:
    """Install the frame-poll filter on the Werkzeug logger once per process."""
    global _frame_log_filter_installed
    with _frame_log_filter_lock:
        if _frame_log_filter_installed:
            return
        logger = logging.getLogger("werkzeug")
        if not any(isinstance(item, FramePollAccessLogFilter) for item in logger.filters):
            logger.addFilter(FramePollAccessLogFilter())
        _frame_log_filter_installed = True


def iphone_error_status(code: str) -> int:
    """Map one runtime error code to its HTTP status."""
    return IPHONE_ERROR_STATUS.get(code, 502)


def iphone_error_response(status: int, code: str, message: str, **details: Any):
    """Return the one error shape every iPhone API uses."""
    return jsonify({"error": {**details, "code": code, "message": message}}), status


def register_iphone_routes(app: Flask, context: IphoneRouteContext) -> None:
    """Register the iPhone page and its API on one blueprint."""
    blueprint = Blueprint(IPHONE_BLUEPRINT_NAME, __name__)
    iphone_service = context.iphone_service
    install_frame_access_log_filter()

    def request_object() -> dict[str, Any]:
        if (request.content_length or 0) > MAX_IPHONE_BODY_BYTES:
            raise IphoneRequestError("The request body is too large.")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise IphoneRequestError("Send a JSON object.")
        return payload

    def required_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise IphoneRequestError(f"Send {key} as a string.")
        return value

    def frame_cursor(name: str) -> int:
        raw = request.args.get(name)
        if raw is None or raw == "":
            return 0
        if not FRAME_CURSOR_PATTERN.fullmatch(raw) or int(raw) > MAX_FRAME_CURSOR:
            raise IphoneRequestError(f"{name} must be an integer between 0 and {MAX_FRAME_CURSOR}.")
        return int(raw)

    def gate(*, operations: bool = True):
        """Apply the Agent gate, strict loopback, then the external-operations switch."""
        context.require_local_agent_request()
        if not is_loopback_address(request.remote_addr):
            return iphone_error_response(
                403, "loopback_required", "The iPhone can only be operated from this Mac."
            )
        if operations and not context.external_agent_operations_enabled():
            return context.reject_external_agent_operation()
        return None

    def respond(operation: Callable[[], Any]):
        """Run one service call and map its refusals onto HTTP statuses."""
        try:
            result = operation()
        except IphoneRequestError as exc:
            return iphone_error_response(400, "invalid_request", exc.public_message)
        except IPhoneUnavailableError:
            logging.getLogger(__name__).warning("iPhone runtime is unavailable.", exc_info=True)
            return iphone_error_response(
                501,
                "iphone_unavailable",
                "iPhone control is unavailable. Check this Mac's setup and try again.",
            )
        except IPhoneOperationError as exc:
            return jsonify({"error": exc.as_dict()}), iphone_error_status(exc.code)
        if isinstance(result, Response):
            return result
        return jsonify(result)

    @blueprint.get("/iphone")
    def iphone():
        context.require_local_agent_request()
        return render_template(
            "iphone.html",
            version=APP_VERSION,
            settings=context.settings_store.settings,
            iphone_supported=iphone_service.supported,
            iphone_operations_enabled=context.external_agent_operations_enabled(),
            iphone_loopback=is_loopback_address(request.remote_addr),
        )

    @blueprint.get("/api/iphone/status")
    def iphone_status():
        if (rejected := gate(operations=False)) is not None:
            return rejected
        if not context.external_agent_operations_enabled():
            return jsonify({
                "supported": iphone_service.supported,
                "started": False,
                "operations_enabled": False,
            })
        return respond(lambda: {**iphone_service.status(), "operations_enabled": True})

    @blueprint.post("/api/iphone/doctor")
    def iphone_doctor():
        if (rejected := gate()) is not None:
            return rejected

        def run():
            request_object()
            return iphone_service.doctor()

        return respond(run)

    @blueprint.get("/api/iphone/tunnel")
    def iphone_tunnel_status():
        if (rejected := gate(operations=False)) is not None:
            return rejected
        return jsonify(iphone_service.tunnel_status())

    @blueprint.post("/api/iphone/tunnel")
    def iphone_tunnel_access():
        if (rejected := gate()) is not None:
            return rejected

        def run():
            payload = request_object()
            if set(payload) != {"enabled"} or not isinstance(payload["enabled"], bool):
                raise IphoneRequestError("Send only enabled as a boolean.")
            return iphone_service.set_tunnel_enabled(payload["enabled"])

        return respond(run)

    @blueprint.post("/api/iphone/setup")
    def iphone_setup():
        if (rejected := gate()) is not None:
            return rejected

        def run():
            payload = request_object()
            action = required_string(payload, "action")
            arguments = {key: value for key, value in payload.items() if key != "action"}
            return iphone_service.setup(action, arguments)

        return respond(run)

    @blueprint.post("/api/iphone/ready")
    def iphone_ready():
        if (rejected := gate()) is not None:
            return rejected

        def run():
            request_object()
            return iphone_service.ready()

        return respond(run)

    @blueprint.get(IPHONE_FRAME_PATH)
    def iphone_screen_frame():
        if (rejected := gate()) is not None:
            return rejected
        return respond(lambda: iphone_service.frame(
            frame_cursor("after_seq"), frame_cursor("last_event_id"),
        ))

    @blueprint.post("/api/iphone/screen")
    def iphone_screen():
        if (rejected := gate()) is not None:
            return rejected
        return respond(lambda: iphone_service.screen(required_string(request_object(), "action")))

    @blueprint.post("/api/iphone/screen/action")
    def iphone_screen_action():
        if (rejected := gate()) is not None:
            return rejected
        return respond(
            lambda: iphone_service.screen_action(required_string(request_object(), "action"))
        )

    @blueprint.get("/api/iphone/screenshot")
    def iphone_screenshot():
        if (rejected := gate()) is not None:
            return rejected

        def run():
            response = Response(iphone_service.screenshot_png(), mimetype="image/png")
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Disposition"] = f'inline; filename="{SCREENSHOT_FILENAME}"'
            return response

        return respond(run)

    @blueprint.post("/api/iphone/action")
    def iphone_action():
        if (rejected := gate()) is not None:
            return rejected

        def run():
            payload = request_object()
            unknown = sorted(str(key) for key in set(payload) - {"op", "args"})
            if unknown:
                raise IphoneRequestError("Unknown fields: " + ", ".join(unknown) + ".")
            arguments = payload.get("args", {})
            if not isinstance(arguments, dict):
                raise IphoneRequestError("Send args as a JSON object.")
            return iphone_service.act(required_string(payload, "op"), arguments)

        return respond(run)

    @blueprint.get("/api/iphone/apps")
    def iphone_apps():
        if (rejected := gate()) is not None:
            return rejected

        def run():
            query = request.args.get("query", "").strip()
            if not 1 <= len(query) <= 100:
                raise IphoneRequestError("Enter an app name of 1 to 100 characters.")
            return iphone_service.apps(query)

        return respond(run)

    app.register_blueprint(blueprint)


__all__ = [
    "IPHONE_BLUEPRINT_NAME",
    "IPHONE_ERROR_STATUS",
    "FramePollAccessLogFilter",
    "IphoneRouteContext",
    "install_frame_access_log_filter",
    "iphone_error_status",
    "register_iphone_routes",
]
