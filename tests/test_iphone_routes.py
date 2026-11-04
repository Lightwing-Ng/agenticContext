"""iPhone HTTP gates, request validation, error mapping, and application wiring.

The application is isolated (external Agent operations disabled) and every service
method that would reach the runtime is stubbed before a test enables operations, so
nothing here creates the vendored runtime or contacts a device.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import logging
import re
from unittest.mock import Mock

import pytest

from app.core.agent import (
    AGENT_ACCESS_SESSION_KEY,
    IPhoneOperationError,
    IPhoneUnavailableError,
)
from app.web import iphone_routes
from app.web.app import create_app


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00\x00\x00\x01" * 2 + b"\x00" * 8
LAN_ENVIRON = {"REMOTE_ADDR": "10.0.0.5"}
LAN_ORIGIN = {"Origin": "http://localhost"}
SERVICE_METHODS = (
    "status", "doctor", "setup", "ready", "frame", "screen",
    "screen_action", "screenshot_png", "act", "apps",
)
# Each runtime-touching route, with a request that is valid once operations are enabled.
OPERATION_ROUTES = (
    ("post", "/api/iphone/doctor", {}, "doctor"),
    ("post", "/api/iphone/setup", {"action": "discover"}, "setup"),
    ("post", "/api/iphone/ready", {}, "ready"),
    ("get", "/api/iphone/screen/frame", None, "frame"),
    ("post", "/api/iphone/screen", {"action": "pause"}, "screen"),
    ("post", "/api/iphone/screen/action", {"action": "refresh"}, "screen_action"),
    ("get", "/api/iphone/screenshot", None, "screenshot_png"),
    ("post", "/api/iphone/action", {"op": "observe", "args": {}}, "act"),
    ("get", "/api/iphone/apps?query=Maps", None, "apps"),
)


@pytest.fixture
def iphone_app(tmp_path):
    """Keep state private and stub every runtime-facing service method."""
    application = create_app(
        tmp_path / "local-store",
        computer_use_settings_path=tmp_path / "settings" / "agent.json",
        computer_use_runtime_root=tmp_path / "agent-runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    service = application.extensions["iphone_service"]
    for name in SERVICE_METHODS:
        setattr(service, name, Mock(name=name))
    service.status.return_value = {
        "supported": True, "started": True, "state_dir": "/state",
        "setup": {"ok": True}, "preview": {"paused": False, "pause_reason": None}, "busy": False,
    }
    for name in ("doctor", "setup", "ready", "frame", "screen", "screen_action", "act", "apps"):
        getattr(service, name).return_value = {"ok": True, "method": name}
    service.screenshot_png.return_value = PNG_BYTES
    yield application, service
    application.extensions["runtime_shutdown"]()


def enable_fake_operations(application):
    """Enable route dispatch only after the runtime-facing methods have been stubbed."""
    application.config["AGENT_EXTERNAL_OPERATIONS_ENABLED"] = True


def send(client, method, path, body, **kwargs):
    if method == "get":
        return client.get(path, **kwargs)
    return client.post(path, json=body, **kwargs)


def unlocked_lan_client(application):
    """A private-network browser that has passed the Agent LAN password gate."""
    client = application.test_client()
    with client.session_transaction(environ_base=LAN_ENVIRON) as session:
        session[AGENT_ACCESS_SESSION_KEY] = True
    return client


def assert_no_runtime_calls(service):
    for name in SERVICE_METHODS:
        if name != "status":
            getattr(service, name).assert_not_called()


# Application wiring
def test_factory_builds_an_idle_service_and_registers_every_route(tmp_path):
    application = create_app(
        tmp_path / "store",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    service = application.extensions["iphone_service"]
    assert service.started() is False
    assert not (tmp_path / "runtime" / "iphone").exists()
    rules = {
        (rule.rule, method)
        for rule in application.url_map.iter_rules()
        for method in rule.methods - {"HEAD", "OPTIONS"}
        if rule.endpoint.startswith(f"{iphone_routes.IPHONE_BLUEPRINT_NAME}.")
    }
    assert rules == {
        ("/iphone", "GET"),
        ("/api/iphone/status", "GET"),
        ("/api/iphone/tunnel", "GET"),
        ("/api/iphone/tunnel", "POST"),
        ("/api/iphone/doctor", "POST"),
        ("/api/iphone/setup", "POST"),
        ("/api/iphone/ready", "POST"),
        ("/api/iphone/screen/frame", "GET"),
        ("/api/iphone/screen", "POST"),
        ("/api/iphone/screen/action", "POST"),
        ("/api/iphone/screenshot", "GET"),
        ("/api/iphone/action", "POST"),
        ("/api/iphone/apps", "GET"),
    }
    application.extensions["runtime_shutdown"]()
    assert service.started() is False


def test_runtime_shutdown_stops_the_iphone_service_once(iphone_app):
    application, service = iphone_app
    service.stop_at_exit = Mock()
    application.extensions["runtime_shutdown"]()
    application.extensions["runtime_shutdown"]()
    service.stop_at_exit.assert_called_once_with()


def test_main_fallback_shutdown_includes_the_iphone_service():
    import main

    stopped = []

    class Service:
        def __init__(self, label):
            self.label = label

        def stop_at_exit(self):
            stopped.append(self.label)

    application = type("App", (), {"extensions": {
        key: Service(key) for key in ("jury_service", "agent_session_pool", "iphone_service")
    }})()
    main._stop_runtime_services(application)
    assert stopped == ["jury_service", "agent_session_pool", "iphone_service"]


def test_frame_access_log_filter_is_installed_once_and_drops_only_successful_polls(tmp_path):
    create_app(tmp_path / "second", agent_external_operations_enabled=False)
    werkzeug_filters = [
        item for item in logging.getLogger("werkzeug").filters
        if isinstance(item, iphone_routes.FramePollAccessLogFilter)
    ]
    assert len(werkzeug_filters) == 1
    log_filter = werkzeug_filters[0]

    def record(request_line, status):
        return logging.LogRecord(
            "werkzeug", logging.INFO, __file__, 1,
            '127.0.0.1 - - [13/Oct/2026 10:00:00] "%s" %s %s',
            (request_line, status, "-"), None,
        )

    poll = "GET /api/iphone/screen/frame?after_seq=4&last_event_id=0 HTTP/1.1"
    assert log_filter.filter(record(poll, "200")) is False
    assert log_filter.filter(record(poll, "503")) is True
    assert log_filter.filter(record("GET /api/iphone/status HTTP/1.1", "200")) is True
    assert log_filter.filter(logging.LogRecord(
        "werkzeug", logging.INFO, __file__, 1, "Running on http://127.0.0.1:8666", None, None,
    )) is True


# Page
def test_page_render_context_needs_no_runtime(iphone_app, monkeypatch):
    application, service = iphone_app
    rendered = {}

    def fake_render(template, **context):
        rendered.update(template=template, **context)
        return "rendered"

    monkeypatch.setattr(iphone_routes, "render_template", fake_render)
    response = application.test_client().get("/iphone")

    assert response.status_code == 200
    assert rendered["template"] == "iphone.html"
    assert rendered["version"]
    assert rendered["settings"] is application.extensions["computer_use_settings"].settings
    assert rendered["iphone_supported"] is service.supported
    assert rendered["iphone_operations_enabled"] is False
    assert rendered["iphone_loopback"] is True
    assert "no-store" in response.headers["Cache-Control"]
    assert_no_runtime_calls(service)
    service.status.assert_not_called()


def test_page_from_an_unlocked_lan_session_renders_the_non_loopback_state(iphone_app, monkeypatch):
    application, _service = iphone_app
    rendered = {}
    monkeypatch.setattr(
        iphone_routes, "render_template",
        lambda template, **context: rendered.update(context) or "rendered",
    )

    response = unlocked_lan_client(application).get("/iphone", environ_base=LAN_ENVIRON)

    assert response.status_code == 200
    assert rendered["iphone_loopback"] is False


def test_locked_lan_page_request_redirects_to_the_agent_gate(iphone_app):
    application, _service = iphone_app
    response = application.test_client().get("/iphone", environ_base=LAN_ENVIRON)
    assert response.status_code == 302
    assert response.headers["Location"] == "/agent"


def test_page_renders_three_agent_modes_with_iphone_active(iphone_app):
    application, service = iphone_app
    response = application.test_client().get("/iphone")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'data-option-count="3"' in body
    assert 'href="/iphone"' in body
    assert "<span>iPhone</span>" in body
    nav = body[body.index('aria-label="Agent modes"'):]
    nav = nav[:nav.index("</nav>")]
    options = [item for item in re.split(r"<a\s", nav)[1:] if "segmented-control-option" in item]
    assert len(options) == 3
    active = [item for item in options if 'aria-current="page"' in item]
    assert len(active) == 1
    assert 'href="/iphone"' in active[0]
    assert_no_runtime_calls(service)


# Gates
def test_status_while_operations_are_disabled_never_creates_the_runtime(iphone_app):
    application, service = iphone_app
    response = application.test_client().get("/api/iphone/status")

    assert response.status_code == 200
    assert response.get_json() == {
        "supported": service.supported, "started": False, "operations_enabled": False,
    }
    service.status.assert_not_called()
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["X-Frame-Options"] == "DENY"


def test_status_with_operations_enabled_reports_the_service_state(iphone_app):
    application, service = iphone_app
    enable_fake_operations(application)
    response = application.test_client().get("/api/iphone/status")

    assert response.status_code == 200
    assert response.get_json() == {**service.status.return_value, "operations_enabled": True}
    service.status.assert_called_once_with()


@pytest.mark.parametrize(("method", "path", "body", "service_method"), OPERATION_ROUTES)
def test_external_operations_disabled_rejects_every_runtime_route(
    iphone_app, method, path, body, service_method,
):
    application, service = iphone_app
    response = send(application.test_client(), method, path, body)

    # The shared Agent helper answers 409 with its own string-valued error.
    assert response.status_code == 409
    assert "External Agent operations are disabled" in response.get_json()["error"]
    getattr(service, service_method).assert_not_called()


@pytest.mark.parametrize(("method", "path", "body", "service_method"), OPERATION_ROUTES)
def test_locked_lan_api_requests_stop_at_the_global_gate(
    iphone_app, method, path, body, service_method,
):
    application, service = iphone_app
    enable_fake_operations(application)
    response = send(
        application.test_client(), method, path, body,
        environ_base=LAN_ENVIRON, headers=LAN_ORIGIN,
    )

    # The application-wide before_request answers a locked LAN API call first.
    assert response.status_code == 401
    getattr(service, service_method).assert_not_called()


@pytest.mark.parametrize(
    ("method", "path", "body", "service_method"),
    (*OPERATION_ROUTES, ("get", "/api/iphone/status", None, "status")),
)
def test_unlocked_lan_sessions_cannot_reach_the_phone(
    iphone_app, method, path, body, service_method,
):
    application, service = iphone_app
    enable_fake_operations(application)
    response = send(
        unlocked_lan_client(application), method, path, body,
        environ_base=LAN_ENVIRON, headers=LAN_ORIGIN,
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "loopback_required"
    getattr(service, service_method).assert_not_called()


def test_unsupported_platform_returns_501_and_status_explains_it(iphone_app):
    application, service = iphone_app
    service.supported = False
    service.platform = "win32"
    del service.doctor  # Use the real method, which refuses before creating a runtime.
    enable_fake_operations(application)
    client = application.test_client()

    refused = client.post("/api/iphone/doctor", json={})
    assert refused.status_code == 501
    assert refused.get_json()["error"]["code"] == "iphone_unavailable"
    assert service.started() is False

    service.status.return_value = {"supported": False, "started": False, "reason": "macOS only"}
    status = client.get("/api/iphone/status")
    assert status.status_code == 200
    assert status.get_json()["supported"] is False


# Error mapping
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("invalid_argument", 400),
        ("newline_requires_intent", 400),
        ("unknown_tool", 400),
        ("device_busy", 409),
        ("preview_paused", 409),
        ("phone_locked", 409),
        ("action_uncertain", 409),
        ("input_continuation_expired", 409),
        ("pua_unreachable", 503),
        ("not_ready", 503),
        ("mirroring_conflict", 503),
        ("pua_recovery_required", 503),
        ("invalid_response", 502),
        ("internal_error", 502),
        ("ambiguous_target", 502),
    ],
)
def test_operation_error_codes_map_to_http_statuses(iphone_app, code, status):
    application, service = iphone_app
    enable_fake_operations(application)
    service.act.side_effect = IPhoneOperationError(code, "Refused.", {"action_executed": False})

    response = application.test_client().post(
        "/api/iphone/action", json={"op": "tap", "args": {"x": 1, "y": 2}},
    )

    assert response.status_code == status
    assert response.get_json() == {
        "error": {"code": code, "message": "Refused.", "action_executed": False},
    }


def test_unavailable_runtime_maps_to_501(iphone_app):
    application, service = iphone_app
    enable_fake_operations(application)
    service.ready.side_effect = IPhoneUnavailableError("Runtime state must be outside all Git checkouts.")

    response = application.test_client().post("/api/iphone/ready", json={})

    assert response.status_code == 501
    assert response.get_json()["error"] == {
        "code": "iphone_unavailable",
        "message": "Runtime state must be outside all Git checkouts.",
    }


def test_responses_from_a_real_service_lose_host_paths(iphone_app):
    application, service = iphone_app
    enable_fake_operations(application)
    del service.act  # Exercise the real sanitizing service over an injected fake runtime.

    class Runtime:
        def call(self, name, args):
            return {"image": {"path": "/private/tmp/shot.jpg", "mimeType": "image/jpeg"}, "tool": name}

        def close(self):
            pass

    if not service.supported:
        pytest.skip("The real service refuses before dispatch on non-macOS hosts.")
    service._runtime = Runtime()
    response = application.test_client().post(
        "/api/iphone/action", json={"op": "observe", "args": {"mode": "screenshot"}},
    )

    assert response.status_code == 200
    assert response.get_json() == {"image": {"mimeType": "image/jpeg"}, "tool": "pua_observe"}


# Request validation
@pytest.mark.parametrize(
    "query",
    [
        "after_seq=-1",
        "after_seq=abc",
        "after_seq=1.5",
        "after_seq=9007199254740992",
        "after_seq=99999999999999999",
        "last_event_id=%EF%BC%91",
        "last_event_id=+1",
    ],
)
def test_frame_query_validation(iphone_app, query):
    application, service = iphone_app
    enable_fake_operations(application)
    response = application.test_client().get(f"/api/iphone/screen/frame?{query}")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"
    service.frame.assert_not_called()


@pytest.mark.parametrize(
    ("query", "expected"),
    [("", (0, 0)), ("after_seq=5&last_event_id=7", (5, 7)), ("after_seq=9007199254740991", (9007199254740991, 0))],
)
def test_frame_passes_validated_cursors(iphone_app, query, expected):
    application, service = iphone_app
    enable_fake_operations(application)
    response = application.test_client().get(f"/api/iphone/screen/frame?{query}")

    assert response.status_code == 200
    service.frame.assert_called_once_with(*expected)


def test_screenshot_is_an_uncached_inline_png(iphone_app):
    application, service = iphone_app
    enable_fake_operations(application)
    response = application.test_client().get("/api/iphone/screenshot")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == PNG_BYTES
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Content-Disposition"] == 'inline; filename="iphone-screenshot.png"'
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_screenshot_refusal_is_json(iphone_app):
    application, service = iphone_app
    enable_fake_operations(application)
    service.screenshot_png.side_effect = IPhoneOperationError("preview_paused", "Paused.")

    response = application.test_client().get("/api/iphone/screenshot")

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "preview_paused"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/iphone/doctor", ["not", "an", "object"]),
        ("/api/iphone/ready", "ready"),
        ("/api/iphone/setup", {}),
        ("/api/iphone/setup", {"action": 3}),
        ("/api/iphone/screen", {}),
        ("/api/iphone/screen/action", {"action": ""}),
        ("/api/iphone/action", {"args": {}}),
        ("/api/iphone/action", {"op": "tap", "args": []}),
        ("/api/iphone/action", {"op": "tap", "args": {}, "extra": True}),
    ],
)
def test_bad_request_shapes_are_rejected_before_the_service(iphone_app, path, body):
    application, service = iphone_app
    enable_fake_operations(application)
    response = application.test_client().post(path, json=body)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"
    assert_no_runtime_calls(service)


def test_missing_json_body_and_oversized_bodies_are_rejected(iphone_app):
    application, service = iphone_app
    enable_fake_operations(application)
    client = application.test_client()

    assert client.post("/api/iphone/ready").status_code == 400
    oversized = client.post(
        "/api/iphone/action",
        json={"op": "type_text", "args": {"text": "x" * (iphone_routes.MAX_IPHONE_BODY_BYTES + 1)}},
    )
    assert oversized.status_code == 400
    assert_no_runtime_calls(service)


def test_valid_requests_reach_the_matching_service_method(iphone_app):
    application, service = iphone_app
    enable_fake_operations(application)
    client = application.test_client()

    assert client.post("/api/iphone/doctor", json={}).status_code == 200
    assert client.post("/api/iphone/ready", json={}).status_code == 200
    assert client.post(
        "/api/iphone/setup", json={"action": "configure", "udid": "u", "team_id": "t"},
    ).status_code == 200
    assert client.post("/api/iphone/screen", json={"action": "resume"}).status_code == 200
    assert client.post("/api/iphone/screen/action", json={"action": "home"}).status_code == 200
    assert client.post("/api/iphone/action", json={"op": "observe"}).status_code == 200
    assert client.post(
        "/api/iphone/action", json={"op": "press_button", "args": {"name": "home"}},
    ).get_json() == {"ok": True, "method": "act"}

    service.doctor.assert_called_once_with()
    service.ready.assert_called_once_with()
    service.setup.assert_called_once_with("configure", {"udid": "u", "team_id": "t"})
    service.screen.assert_called_once_with("resume")
    service.screen_action.assert_called_once_with("home")
    assert service.act.call_args_list[0].args == ("observe", {})
    assert service.act.call_args_list[1].args == ("press_button", {"name": "home"})


@pytest.mark.parametrize("query", ["", "query=", "query=%20%20", "query=" + "x" * 101])
def test_app_lookup_requires_a_bounded_query(iphone_app, query):
    application, service = iphone_app
    enable_fake_operations(application)
    response = application.test_client().get(f"/api/iphone/apps?{query}")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"
    service.apps.assert_not_called()


def test_app_lookup_passes_the_trimmed_query(iphone_app):
    application, service = iphone_app
    enable_fake_operations(application)
    response = application.test_client().get("/api/iphone/apps?query=%20Maps%20")

    assert response.status_code == 200
    service.apps.assert_called_once_with("Maps")
