"""Jury HTTP validation, isolation, and control-plane security regressions.

Code version: v1.5.0-codex.0
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.web.app import create_app


@pytest.fixture
def jury_app(tmp_path):
    """Keep state private and stub provider operations on the injected service."""
    application = create_app(
        tmp_path / "local-store",
        computer_use_settings_path=tmp_path / "settings" / "agent.json",
        computer_use_runtime_root=tmp_path / "agent-runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    service = application.extensions["jury_service"]
    service.check = Mock()
    service.dismiss_failed = Mock()
    service.start = Mock()
    service.status = Mock()
    service.sessions = Mock()
    service.stop = Mock()
    service.status.return_value = {
        "session_id": "jury-example",
        "question": "Check the original claim against primary evidence.",
        "phase": "reviewing",
        "running": True,
        "message": "Reviewing the other juror's evidence.",
        "round": 2,
        "convergence_mode": "automatic",
        "termination_reason": "",
        "providers": ["chatgpt", "grok"],
        "rounds": [],
        "response": "",
        "consensus": False,
    }
    service.start.return_value = service.status.return_value
    service.check.return_value = {
        "browser": "edge",
        "providers": [
            {"provider": "chatgpt", "logged_in": True},
            {"provider": "grok", "logged_in": True},
        ],
        "ready": True,
    }
    service.sessions.return_value = [service.status.return_value]
    service.stop.return_value = {
        **service.status.return_value,
        "running": False,
        "phase": "stopped",
    }
    service.dismiss_failed.return_value = {"session_id": "jury-example"}
    yield application, service
    service.stop_at_exit()
    application.extensions["agent_session_pool"].stop_at_exit()


def enable_fake_operations(application):
    """Enable route dispatch only after the browser-facing methods have been stubbed."""
    application.config["AGENT_EXTERNAL_OPERATIONS_ENABLED"] = True


def test_macos_jury_route_exposes_safari_as_a_persistable_browser(jury_app):
    application, service = jury_app
    response = application.test_client().get("/jury/safari")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'data-jury-browser="safari"' in body
    assert 'name="browser" value="safari"' in body
    assert 'data-jury-browser-option="safari"' in body
    assert 'agent-sessions.css?v=1.9.0' in body
    assert 'jury.css?v=jury-v1.2.0-codex.0' in body
    assert 'jury.js?v=jury-v1.3.5-codex.1' in body
    assert 'data-jury-provider-row="claude" aria-disabled="true"' in body
    service.check.assert_not_called()
    service.start.assert_not_called()


def test_jury_status_and_sessions_are_read_only_and_not_cached(jury_app):
    application, service = jury_app
    client = application.test_client()
    response = client.get("/api/jury/status?session_id=jury-example")
    assert response.status_code == 200
    assert response.get_json()["session_id"] == "jury-example"
    assert response.get_json()["round"] == 2
    service.status.assert_called_once_with("jury-example")
    catalog = client.get("/api/jury/sessions?browser=edge")
    assert catalog.status_code == 200
    service.sessions.assert_called_once_with("edge")
    for item in (response, catalog):
        assert "no-store" in item.headers["Cache-Control"]
        assert item.headers["X-Frame-Options"] == "DENY"
        assert item.headers["X-Content-Type-Options"] == "nosniff"
    service.check.assert_not_called()
    service.start.assert_not_called()


def test_safari_jury_sessions_catalog_uses_the_selected_browser(jury_app):
    application, service = jury_app
    response = application.test_client().get("/api/jury/sessions?browser=safari")

    assert response.status_code == 200
    service.sessions.assert_called_once_with("safari")


@pytest.mark.parametrize("route", ["check", "start"])
def test_safari_api_rejects_claude_before_service_browser_activity(tmp_path, monkeypatch, route):
    from app.core import jury as jury_module

    monkeypatch.setattr(
        jury_module,
        "browser_options_for_host",
        lambda: ({"key": "edge"}, {"key": "safari"}),
    )
    application = create_app(
        tmp_path / "local-store",
        computer_use_settings_path=tmp_path / "settings" / "agent.json",
        computer_use_runtime_root=tmp_path / "agent-runtime",
        agent_external_operations_enabled=True,
    )
    application.config.update(TESTING=True)
    probed = []
    real_service = application.extensions["jury_service"]
    real_service.login_check = lambda *_args, **_kwargs: probed.append("probed") or {
        "logged_in": True,
    }
    try:
        response = application.test_client().post(
            f"/api/jury/{route}",
            json={
                "browser": "safari",
                "providers": ["chatgpt", "claude"],
                "question": "Do not send this fixture to a provider.",
                "models": {
                    "chatgpt": "chatgpt-latest-extra-high",
                    "claude": "claude-auto",
                },
            },
        )
        assert response.status_code == 400
        assert "ChatGPT, Grok, and Gemini" in response.get_json()["error"]
        assert probed == []
        assert real_service.records == {}
    finally:
        real_service.stop_at_exit()
        application.extensions["agent_session_pool"].stop_at_exit()


@pytest.mark.parametrize("route", ["check", "start"])
def test_isolated_jury_app_blocks_browser_operations(jury_app, route):
    application, service = jury_app
    response = application.test_client().post(
        f"/api/jury/{route}",
        json={
            "browser": "edge",
            "providers": ["chatgpt", "grok"],
            "question": "Do not send this fixture to a provider.",
            "session_id": "jury-example",
        },
    )
    assert response.status_code == 409
    assert "disabled" in response.get_json()["error"].lower()
    getattr(service, route).assert_not_called()


def test_isolated_jury_app_blocks_failed_session_deletion(jury_app):
    application, service = jury_app
    response = application.test_client().delete(
        "/api/jury/session", json={"session_id": "jury-example"},
    )
    assert response.status_code == 409
    assert "disabled" in response.get_json()["error"].lower()
    service.dismiss_failed.assert_not_called()


@pytest.mark.parametrize("route", ["check", "start"])
@pytest.mark.parametrize("raw", ["{", "[]", '"question"', "null"])
def test_jury_rejects_malformed_or_non_object_json_before_service_dispatch(jury_app, route, raw):
    application, service = jury_app
    enable_fake_operations(application)
    response = application.test_client().post(
        f"/api/jury/{route}", data=raw, content_type="application/json"
    )
    assert response.status_code == 400
    assert "error" in response.get_json()
    getattr(service, route).assert_not_called()


@pytest.mark.parametrize("route", ["check", "start"])
def test_jury_validation_errors_return_bad_request(jury_app, route):
    application, service = jury_app
    enable_fake_operations(application)
    getattr(service, route).side_effect = ValueError("Choose a supported browser and at least two distinct jurors.")
    response = application.test_client().post(
        f"/api/jury/{route}",
        json={"browser": "invalid", "providers": ["unknown"], "question": "Claim"},
    )
    assert response.status_code == 400
    assert "error" in response.get_json()


@pytest.mark.parametrize("message", ["A Jury task is already running.", "The selected browser is in use."])
def test_jury_start_capacity_failures_return_conflict(jury_app, message):
    application, service = jury_app
    enable_fake_operations(application)
    service.start.side_effect = RuntimeError(message)
    response = application.test_client().post(
        "/api/jury/start",
        json={"browser": "edge", "providers": ["chatgpt", "grok"], "question": "Claim"},
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == message


def test_jury_check_and_start_dispatch_web_only_inputs(jury_app):
    application, service = jury_app
    enable_fake_operations(application)
    client = application.test_client()
    selection = {
        "browser": "edge",
        "providers": ["chatgpt", "grok"],
        "models": {
            "chatgpt": "chatgpt-latest-extra-high",
            "grok": "grok-auto",
        },
    }
    checked = client.post("/api/jury/check", json=selection)
    assert checked.status_code == 200
    assert checked.get_json()["ready"] is True
    service.check.assert_called_once_with(
        "edge", ["chatgpt", "grok"], selection["models"],
    )
    started = client.post(
        "/api/jury/start",
        json={
            **selection,
            "question": "Check the original claim against primary evidence.",
            "max_rounds": 2,
        },
    )
    assert started.status_code == 202
    assert started.get_json()["session_id"] == "jury-example"
    service.start.assert_called_once_with(
        "edge", ["chatgpt", "grok"],
        "Check the original claim against primary evidence.", 2, selection["models"],
    )
    assert "no-store" in started.headers["Cache-Control"]


def test_current_jury_start_uses_automatic_convergence_without_a_round_budget(jury_app):
    application, service = jury_app
    enable_fake_operations(application)
    response = application.test_client().post(
        "/api/jury/start",
        json={
            "browser": "edge",
            "providers": ["chatgpt", "grok"],
            "question": "Check the claim without a client-specified round budget.",
            "models": {
                "chatgpt": "chatgpt-latest-extra-high",
                "grok": "grok-auto",
            },
        },
    )
    assert response.status_code == 202
    service.start.assert_called_once_with(
        "edge",
        ["chatgpt", "grok"],
        "Check the claim without a client-specified round budget.",
        None,
        {"chatgpt": "chatgpt-latest-extra-high", "grok": "grok-auto"},
    )


@pytest.mark.parametrize("route", ["check", "start", "stop"])
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://attacker@localhost"},
    ],
)
def test_jury_rejects_cross_site_writes_before_provider_dispatch(jury_app, route, headers):
    application, service = jury_app
    enable_fake_operations(application)
    response = application.test_client().post(f"/api/jury/{route}", headers=headers, json={})
    assert response.status_code == 403
    assert "no-store" in response.headers["Cache-Control"]
    getattr(service, route).assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "http://attacker@localhost"},
    ],
)
def test_jury_rejects_cross_site_failed_session_deletion(jury_app, headers):
    application, service = jury_app
    enable_fake_operations(application)
    response = application.test_client().delete(
        "/api/jury/session",
        headers=headers,
        json={"session_id": "jury-example"},
    )
    assert response.status_code == 403
    assert "no-store" in response.headers["Cache-Control"]
    service.dismiss_failed.assert_not_called()


@pytest.mark.parametrize("session_id", [None, "", "   ", 17])
def test_jury_failed_session_deletion_requires_a_session_id(jury_app, session_id):
    application, service = jury_app
    enable_fake_operations(application)
    response = application.test_client().delete(
        "/api/jury/session", json={"session_id": session_id},
    )
    assert response.status_code == 400
    service.dismiss_failed.assert_not_called()


@pytest.mark.parametrize("path", ["/api/jury/status", "/api/jury/sessions"])
def test_jury_lan_reads_require_the_existing_unlocked_session(jury_app, monkeypatch, path):
    application, service = jury_app
    monkeypatch.setenv("AGENTIC_CONTEXT_AGENT_PASSWORD", "246810")
    response = application.test_client().get(
        path,
        headers={"Host": "192.168.124.10:8666"},
        environ_overrides={"REMOTE_ADDR": "192.168.124.20"},
    )
    assert response.status_code == 401
    assert "no-store" in response.headers["Cache-Control"]
    service.status.assert_not_called()
    service.sessions.assert_not_called()


def test_jury_lan_writes_require_origin_even_after_unlock(jury_app, monkeypatch):
    application, service = jury_app
    enable_fake_operations(application)
    monkeypatch.setenv("AGENTIC_CONTEXT_AGENT_PASSWORD", "246810")
    client = application.test_client()
    headers = {"Host": "192.168.124.10:8666", "Origin": "http://192.168.124.10:8666"}
    environ = {"REMOTE_ADDR": "192.168.124.20"}
    unlocked = client.post(
        "/agent/unlock", data={"password": "246810"}, headers=headers, environ_overrides=environ
    )
    assert unlocked.status_code == 303
    response = client.post(
        "/api/jury/start",
        json={"browser": "edge", "providers": ["chatgpt", "grok"], "question": "Claim"},
        headers={"Host": headers["Host"]},
        environ_overrides=environ,
    )
    assert response.status_code == 403
    service.start.assert_not_called()


def test_jury_stop_targets_the_existing_session(jury_app):
    application, service = jury_app
    enable_fake_operations(application)
    response = application.test_client().post("/api/jury/stop", json={"session_id": "jury-example"})
    assert response.status_code == 200
    service.stop.assert_called_once_with("jury-example")
    service.start.assert_not_called()


def test_jury_failed_session_delete_targets_one_local_archive(jury_app):
    application, service = jury_app
    enable_fake_operations(application)
    response = application.test_client().delete(
        "/api/jury/session", json={"session_id": "jury-example"},
    )
    assert response.status_code == 200
    assert response.get_json() == {"deleted": True, "session_id": "jury-example"}
    service.dismiss_failed.assert_called_once_with("jury-example")
    service.start.assert_not_called()


@pytest.mark.parametrize(
    ("side_effect", "status", "code"),
    [
        (ValueError("Unknown jury session."), 404, "unknown_jury_session"),
        (RuntimeError("Only failed Jury sessions can be deleted."), 409,
         "jury_session_not_deletable"),
    ],
)
def test_jury_failed_session_delete_returns_typed_errors(
    jury_app, side_effect, status, code,
):
    application, service = jury_app
    enable_fake_operations(application)
    service.dismiss_failed.side_effect = side_effect
    response = application.test_client().delete(
        "/api/jury/session", json={"session_id": "jury-example"},
    )
    assert response.status_code == status
    assert response.get_json()["code"] == code


def test_jury_status_sanitizes_provider_markdown_before_browser_rendering(jury_app):
    application, service = jury_app
    source = '**Evidence** <script>alert(1)</script> [unsafe](javascript:alert(1))'
    service.status.return_value.update(
        response=source,
        rounds=[{"round": 1, "opinions": [{"provider": "grok", "conclusion": source}]}],
    )
    response = application.test_client().get("/api/jury/status?session_id=jury-example")
    assert response.status_code == 200
    payload = response.get_json()
    for rendered in (payload["response_html"], payload["rounds"][0]["opinions"][0]["response_html"]):
        assert "<strong>Evidence</strong>" in rendered
        assert "<script>" not in rendered
        assert 'href="javascript:' not in rendered


def test_unknown_jury_session_returns_not_found_without_starting_a_replacement(jury_app):
    application, service = jury_app
    service.status.side_effect = ValueError("Unknown jury session.")
    response = application.test_client().get("/api/jury/status?session_id=missing")
    assert response.status_code == 404
    assert response.get_json()["error"] == "Unknown jury session."
    service.start.assert_not_called()
