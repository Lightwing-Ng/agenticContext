"""Keep local conversation refresh jobs scoped and credentials out of HTTP.

Code version: v1.0.0-codex.0
"""

from types import SimpleNamespace
from unittest.mock import Mock

from flask import Blueprint, Flask
import pytest

from app.web.wechat_refresh_routes import (
    WECHAT_REFRESH_EXTENSION,
    conversation_refresh_available,
    register_wechat_refresh_routes,
)
from tests import test_wechat_session_csv
from tests.test_wechat_resource_timeline import _session


SESSION = "wechat:" + "a" * 64
TOKEN = "job_" + "b" * 40
session_app = test_wechat_session_csv.session_app


@pytest.fixture
def refresh_client():
    calls = []
    service = SimpleNamespace(
        start=lambda session: calls.append(session) or {"task_id": TOKEN},
        status=lambda token: {
            "state": "succeeded",
            "added": 3,
            "total": 42,
            "partial": True,
            "message": "The available local records were refreshed.",
            "database_path": "/private/test-source",
            "key": "must-not-escape",
        },
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.extensions[WECHAT_REFRESH_EXTENSION] = service
    blueprint = Blueprint("local_resources", __name__)
    register_wechat_refresh_routes(blueprint)
    app.register_blueprint(blueprint)
    return app.test_client(), service, calls


def test_refresh_accepts_only_the_canonical_session_and_returns_an_opaque_job(
    refresh_client,
):
    client, _, calls = refresh_client
    response = client.post("/api/browser/wechat/refresh", json={"session_id": SESSION})
    assert response.status_code == 202
    assert response.json == {
        "state": "running",
        "status_url": "/api/browser/wechat/refresh/" + TOKEN,
    }
    assert calls == [SESSION]
    assert response.cache_control.no_store
    status = client.get(response.json["status_url"])
    assert status.json == {
        "state": "succeeded",
        "added": 3,
        "total": 42,
        "partial": True,
        "message": "The available local records were refreshed.",
    }
    assert status.cache_control.no_store
    assert (
        b"private/test-source" not in status.data
        and b"must-not-escape" not in status.data
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"session_id": 123},
        {"session_id": "../history.sqlite3"},
        {"session_id": "wechat:" + "g" * 64},
        {"session_id": SESSION, "account": "another-account"},
        {"session_id": SESSION, "key": "not-accepted"},
        {"session_id": SESSION, "database_path": "/other/database"},
    ],
)
def test_refresh_rejects_browser_supplied_scope_or_credentials(refresh_client, payload):
    client, _, calls = refresh_client
    response = client.post("/api/browser/wechat/refresh", json=payload)
    assert response.status_code == 400
    assert calls == []


def test_refresh_rejects_oversized_payload_before_starting(refresh_client):
    client, _, calls = refresh_client
    response = client.post(
        "/api/browser/wechat/refresh",
        json={"session_id": SESSION, "extra": "x" * 5_000},
    )
    assert response.status_code == 413 and calls == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/browser/wechat/refresh"),
        ("get", "/api/browser/wechat/refresh/" + TOKEN),
    ],
)
def test_refresh_jobs_remain_loopback_only(refresh_client, method, path):
    client, _, calls = refresh_client
    response = getattr(client, method)(
        path,
        json={"session_id": SESSION},
        environ_overrides={"REMOTE_ADDR": "192.168.1.9"},
    )
    assert response.status_code == 403 and calls == []


@pytest.mark.parametrize(
    "error,status",
    [
        (LookupError("private path"), 404),
        (ValueError("private key"), 409),
        (PermissionError("private key"), 409),
        (RuntimeError("private key"), 409),
        (OSError("private path"), 503),
    ],
)
def test_refresh_errors_do_not_disclose_internal_details(refresh_client, error, status):
    client, service, _ = refresh_client

    def fail(session):
        raise error

    service.start = fail
    response = client.post("/api/browser/wechat/refresh", json={"session_id": SESSION})
    assert response.status_code == status
    assert b"private path" not in response.data and b"private key" not in response.data


def test_refresh_unknown_or_expired_jobs_return_no_details(refresh_client):
    client, service, _ = refresh_client
    assert client.get("/api/browser/wechat/refresh/invalid").status_code == 404
    service.status = lambda token: None
    assert client.get("/api/browser/wechat/refresh/" + TOKEN).status_code == 404


@pytest.mark.parametrize(
    "kind,peer,expected",
    [
        ("direct", "wxid_private", True),
        ("group", "123@chatroom", True),
        ("direct", "gh_official", False),
        ("unclassified", "unknown", False),
    ],
)
def test_refresh_eligibility_uses_the_existing_conversation_classification(
    kind, peer, expected
):
    calls = []
    service = SimpleNamespace(available=lambda *scope: calls.append(scope) or True)
    messaging = {"account_id": "wxid_owner", "peer_id": peer, "kind": kind}
    assert conversation_refresh_available(service, messaging) is expected
    assert calls == ([("wxid_owner", peer, kind)] if expected else [])


def test_refresh_eligibility_is_false_without_matching_private_host_configuration():
    service = SimpleNamespace(available=lambda *scope: False)
    assert not conversation_refresh_available(service, None)
    assert not conversation_refresh_available(service, {"kind": "direct"})
    assert not conversation_refresh_available(
        service, {"account_id": "wxid_owner", "peer_id": "wxid_peer", "kind": "direct"}
    )


def test_refresh_inherits_application_origin_guard_and_idempotent_shutdown(session_app):
    app, root = session_app
    calls = []
    service = SimpleNamespace(
        available=lambda *scope: True,
        start=lambda session: calls.append(session) or {"task_id": TOKEN},
        status=lambda token: {"state": "running"},
        shutdown=Mock(),
    )
    app.extensions[WECHAT_REFRESH_EXTENSION] = service
    client = app.test_client()
    session = _session(root, kind="group")
    endpoint = "/api/browser/wechat/refresh"
    rejected = client.post(
        endpoint,
        json={"session_id": session["id"]},
        headers={"Origin": "https://untrusted.invalid"},
    )
    assert rejected.status_code == 403 and calls == []
    response = client.post(endpoint, json={"session_id": session["id"]})
    assert response.status_code == 202 and calls == [session["id"]]
    app.extensions["runtime_shutdown"]()
    app.extensions["runtime_shutdown"]()
    service.shutdown.assert_called_once_with(wait=True, timeout=5.0)


def test_refresh_button_eligibility_is_rendered_from_host_state(session_app):
    app, root = session_app
    service = SimpleNamespace(
        available=lambda *scope: True, shutdown=lambda **options: None
    )
    app.extensions[WECHAT_REFRESH_EXTENSION] = service
    client = app.test_client()
    session = _session(root, kind="group")
    address = "/browser?view=text&source=wechat&session=" + session["id"]
    visible = client.get(address)
    assert visible.status_code == 200
    assert b"data-wechat-conversation-refresh" in visible.data
    service.available = lambda *scope: False
    hidden = client.get(address)
    assert b"data-wechat-conversation-refresh" not in hidden.data
    service.available = lambda *scope: True
    remote = client.get(address, environ_overrides={"REMOTE_ADDR": "192.168.1.9"})
    assert remote.status_code == 302
    assert "/agent" in remote.headers["Location"]
    assert b"data-wechat-conversation-refresh" not in remote.data
