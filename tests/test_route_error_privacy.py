"""Public Agent, Jury, and Tunnel errors retain codes without private diagnostics.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from app.core.agent import GeminiOAuthError, ProjectRegistryError, ProjectSelectionConflict
from app.web import agent_routes, tunnel_routes
from app.web.app import create_app


SECRET = "Traceback: /private/synthetic/credentials.json sk-private-fixture-token"
USER_TEXT = 'A literal user example: {"error": "keep this content"}.'


@pytest.fixture
def privacy_app(tmp_path):
    application = create_app(
        tmp_path / "store",
        computer_use_settings_path=tmp_path / "agent.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    yield application
    application.extensions["runtime_shutdown"]()


def assert_private_diagnostic_absent(response, status):
    assert response.status_code == status
    assert SECRET not in response.get_data(as_text=True)
    assert SECRET not in str(dict(response.headers))
    assert "sk-private-fixture-token" not in response.get_data(as_text=True)
    assert "error" in response.get_json()


@pytest.mark.parametrize(("path", "target", "status"), [
    ("/api/agent/ask", "pool.start", 409),
    ("/api/agent/preferences", "settings.update_preferences", 409),
    ("/api/agent/terminal-authorization", "terminal", 409),
    ("/api/agent/status", "pool.get", 404),
])
def test_agent_refusals_never_echo_private_exception_messages(privacy_app, path, target, status):
    application = privacy_app
    if target == "terminal":
        patched = patch.object(agent_routes, "launch_terminal_authorization", side_effect=RuntimeError(SECRET))
    else:
        owner, attribute = target.split(".")
        component = application.extensions[
            "agent_session_pool" if owner == "pool" else "computer_use_settings"
        ]
        patched = patch.object(component, attribute, side_effect=ValueError(SECRET))
    with patched as operation:
        application.config["AGENT_EXTERNAL_OPERATIONS_ENABLED"] = True
        client = application.test_client()
        response = client.get(path) if path.endswith("status") else client.post(path, json={})
    assert_private_diagnostic_absent(response, status)
    operation.assert_called()


def test_agent_status_sanitizes_only_known_diagnostic_nodes(privacy_app):
    service = privacy_app.extensions["computer_use_agent_service"]
    with patch.object(service, "snapshot", return_value={
        "platform": privacy_app.extensions["computer_use_settings"].settings.platform,
        "browser": privacy_app.extensions["computer_use_settings"].settings.browser,
        "workspace_path": privacy_app.extensions["computer_use_settings"].settings.workspace_path,
        "phase": "failed", "running": False,
        "message": SECRET, "last_error": SECRET, "error_traceback": SECRET,
        "catalog_error": SECRET, "response": USER_TEXT,
        "history": [{"response": USER_TEXT, "prompt": USER_TEXT}],
    }), patch.object(service, "compute_job_status", return_value={"error": SECRET}):
        settings = privacy_app.extensions["computer_use_settings"].settings
        response = privacy_app.test_client().get("/api/agent/status", headers={
            "X-CacheLikes-Agent-Browser": settings.browser,
            "X-CacheLikes-Agent-Platform": settings.platform,
            "X-CacheLikes-Agent-Workspace": settings.workspace_path,
        })
    assert response.status_code == 200
    payload = response.get_json()
    assert SECRET not in response.get_data(as_text=True)
    assert payload["agent"]["error_traceback"] == ""
    assert payload["agent"]["response"] == USER_TEXT
    assert payload["agent"]["history"][0]["response"] == USER_TEXT
    assert payload["agent"]["history"][0]["prompt"] == USER_TEXT


@pytest.mark.parametrize(("method", "path", "operation", "status"), [
    ("post", "/api/jury/check", "check", 400),
    ("post", "/api/jury/start", "start", 409),
    ("get", "/api/jury/status?session_id=synthetic", "status", 404),
    ("post", "/api/jury/stop", "stop", 404),
    ("delete", "/api/jury/session", "dismiss_failed", 409),
])
def test_jury_refusals_keep_status_without_echoing_private_diagnostics(
    privacy_app, method, path, operation, status,
):
    service = privacy_app.extensions["jury_service"]
    error = ValueError(SECRET) if status in {400, 404} else RuntimeError(SECRET)
    setattr(service, operation, Mock(side_effect=error))
    privacy_app.config["AGENT_EXTERNAL_OPERATIONS_ENABLED"] = True
    response = getattr(privacy_app.test_client(), method)(
        path, json={"session_id": "synthetic"},
    )
    assert_private_diagnostic_absent(response, status)
    getattr(service, operation).assert_called_once()


def test_jury_failed_status_preserves_response_content(privacy_app):
    service = privacy_app.extensions["jury_service"]
    service.status = Mock(return_value={
        "phase": "failed", "message": SECRET, "resource_cleanup_warning": SECRET,
        "response": USER_TEXT, "rounds": [],
    })
    response = privacy_app.test_client().get("/api/jury/status?session_id=synthetic")
    assert response.status_code == 200
    assert SECRET not in response.get_data(as_text=True)
    assert response.get_json()["response"] == USER_TEXT


def test_tunnel_registry_problem_hides_private_diagnostics(privacy_app):
    registry = privacy_app.extensions["tunnel_mcp_service"].registry
    with patch.object(registry, "projects", side_effect=ProjectRegistryError(SECRET)):
        response = privacy_app.test_client().get("/api/agent/tunnel/project")
    assert response.status_code == 200
    assert SECRET not in response.get_data(as_text=True)
    assert response.get_json()["project_context"]["problem"]


def test_tunnel_selection_conflict_preserves_conflict_status_without_diagnostics(privacy_app):
    registry = privacy_app.extensions["tunnel_mcp_service"].registry
    with patch.object(registry, "uses_fallback", return_value=False), patch.object(
        registry, "projects", side_effect=ProjectSelectionConflict(SECRET),
    ):
        response = privacy_app.test_client().post(
            "/api/agent/tunnel/project", json={"project_id": "synthetic", "expected_revision": 0},
        )
    assert_private_diagnostic_absent(response, 409)
    assert "project_context" in response.get_json()


def test_oauth_response_retains_protocol_code_and_challenge_without_private_description(privacy_app):
    with privacy_app.test_request_context():
        response = tunnel_routes._oauth_error_response(
            GeminiOAuthError("invalid_client", SECRET, status_code=401),
        )
    assert response.status_code == 401
    assert response.get_json()["error"] == "invalid_client"
    assert SECRET not in response.get_data(as_text=True)
    assert SECRET not in str(dict(response.headers))
    assert response.headers["WWW-Authenticate"].startswith("Basic ")
    assert "no-store" in response.headers["Cache-Control"]


@pytest.mark.parametrize("state", ["error", "degraded", "retrying"])
def test_tunnel_connection_status_and_presentation_hide_private_diagnostics(privacy_app, state):
    runtime = privacy_app.extensions["tunnel_runtime"]
    with patch.object(runtime, "snapshot", return_value={
        "state": state, "message": SECRET, "ready": False, "configured": True,
    }):
        response = privacy_app.test_client().get("/api/agent/tunnel/status?platform=chatgpt")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["state"] == state
    assert payload["message"] == "The Tunnel connection failed. Check local diagnostics and reconnect."
    assert payload["presentation"]["message"] == payload["message"]
    assert SECRET not in response.get_data(as_text=True)


def test_jury_provider_diagnostics_preserve_identity_and_response_content(privacy_app):
    service = privacy_app.extensions["jury_service"]
    service.status = Mock(return_value={
        "phase": "failed", "message": SECRET, "response": USER_TEXT, "rounds": [],
        "providers": [{"key": "chatgpt", "status": "unavailable", "message": SECRET}],
    })
    response = privacy_app.test_client().get("/api/jury/status?session_id=synthetic")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["providers"][0]["key"] == "chatgpt"
    assert payload["providers"][0]["status"] == "unavailable"
    assert payload["response"] == USER_TEXT
    assert SECRET not in response.get_data(as_text=True)


def test_jury_account_check_diagnostics_do_not_echo_provider_exceptions(privacy_app):
    service = privacy_app.extensions["jury_service"]
    service.check = Mock(return_value={
        "ready": False, "browser": "edge", "message": SECRET,
        "providers": [{"key": "chatgpt", "ready": False, "message": SECRET}],
    })
    privacy_app.config["AGENT_EXTERNAL_OPERATIONS_ENABLED"] = True
    response = privacy_app.test_client().post("/api/jury/check", json={})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ready"] is False
    assert payload["providers"][0]["key"] == "chatgpt"
    assert payload["providers"][0]["ready"] is False
    assert SECRET not in response.get_data(as_text=True)


def test_jury_sessions_preserves_the_actual_empty_and_persisted_service_envelope(privacy_app):
    service = privacy_app.extensions["jury_service"]
    client = privacy_app.test_client()
    empty = client.get("/api/jury/sessions?browser=edge")
    assert empty.status_code == 200
    assert empty.get_json() == {"sessions": []}
    record = {
        "session_id": "synthetic-jury", "browser": "edge", "question": USER_TEXT,
        "title": "Synthetic saved jury", "phase": "finished", "running": False,
        "started_at": "2026-11-05T00:00:00Z", "response": USER_TEXT, "rounds": [],
    }
    service._save(record)
    service._archive_loaded = False
    saved = client.get("/api/jury/sessions?browser=edge")
    assert saved.status_code == 200
    payload = saved.get_json()
    assert set(payload) == {"sessions"}
    assert len(payload["sessions"]) == 1
    assert payload["sessions"][0] == {
        **{key: record[key] for key in (
            "session_id", "browser", "question", "title", "phase", "running", "started_at",
        )},
        "deletable": False,
    }
    assert service.records[record["session_id"]]["response"] == USER_TEXT
    assert client.get("/api/jury/sessions?browser=chrome").get_json() == {"sessions": []}


@pytest.mark.parametrize("phase", ["failed", "interrupted"])
def test_actual_agent_snapshot_and_doctor_failure_schemas_keep_diagnostics_private(privacy_app, phase):
    service = privacy_app.extensions["computer_use_agent_service"]
    settings = privacy_app.extensions["computer_use_settings"].settings
    service._snapshot.phase = phase
    service._snapshot.message = SECRET
    service._snapshot.last_error = SECRET
    service._snapshot.error_traceback = SECRET
    service._snapshot.platform = settings.platform
    service._snapshot.browser = settings.browser
    service._snapshot.workspace_path = settings.workspace_path
    service._snapshot.response = USER_TEXT
    service._snapshot.prompt = USER_TEXT
    raw_doctor = service.doctor()
    assert next(item for item in raw_doctor["checks"] if item["id"] == "run_lifecycle")["detail"] == SECRET
    client = privacy_app.test_client()
    status = client.get("/api/agent/status", headers={
        "X-CacheLikes-Agent-Browser": settings.browser,
        "X-CacheLikes-Agent-Platform": settings.platform,
        "X-CacheLikes-Agent-Workspace": settings.workspace_path,
    })
    assert status.status_code == 200
    assert SECRET not in status.get_data(as_text=True)
    assert status.get_json()["agent"]["phase"] == phase
    assert status.get_json()["agent"]["response"] == USER_TEXT
    assert status.get_json()["agent"]["prompt"] == USER_TEXT
    doctor = client.get("/api/agent/doctor")
    assert doctor.status_code == 200
    assert SECRET not in doctor.get_data(as_text=True)
    projected = {item["id"]: item for item in doctor.get_json()["checks"]}
    for check in raw_doctor["checks"]:
        if check["id"] == "run_lifecycle":
            assert projected[check["id"]]["status"] == check["status"]
            assert projected[check["id"]]["detail"] != SECRET
        else:
            assert projected[check["id"]] == check
    assert service._snapshot.message == SECRET
    assert service._snapshot.response == USER_TEXT


def test_actual_unknown_compute_job_status_does_not_expose_metadata_exception(privacy_app, tmp_path):
    workspace = tmp_path / "compute-workspace"
    workspace.mkdir()
    service = privacy_app.extensions["computer_use_agent_service"]
    with patch("app.core.agent.compute_jobs.ComputeJobManager.status", side_effect=ValueError(SECRET)):
        raw = service.compute_job_status(str(workspace))
        assert raw["state"] == "unknown" and SECRET in raw["message"]
        client = privacy_app.test_client()
        response = client.get("/api/agent/status", headers={
            "X-CacheLikes-Agent-Workspace": str(workspace),
            "X-CacheLikes-Agent-Browser": "edge",
            "X-CacheLikes-Agent-Platform": "chatgpt",
        })
        page = client.get("/agent/edge/chatgpt")
    assert page.status_code == 200
    assert SECRET not in page.get_data(as_text=True)
    assert response.status_code == 200
    payload = response.get_json()["compute_job"]
    assert payload["state"] == "unknown"
    assert payload["active"] is True
    assert SECRET not in response.get_data(as_text=True)
    assert "metadata could not be reconciled safely" in payload["message"]
