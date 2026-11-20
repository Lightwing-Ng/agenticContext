"""Keep local conversation refresh jobs scoped and credentials out of HTTP.

Code version: v1.2.2-codex.0
"""

from types import SimpleNamespace
from unittest.mock import Mock

from flask import Blueprint, Flask
import pytest

from app.core.wechat_mac_refresh_source import RefreshIssue, RefreshNotice
from app.web.wechat_read_routes import WECHAT_SUMMARY_EXTENSION
from app.web.wechat_refresh_routes import (
    WECHAT_REFRESH_EXTENSION,
    conversation_refresh_available,
    register_wechat_refresh_routes,
)
from tests import test_wechat_session_csv
from tests.test_wechat_resource_timeline import _session
from tests.wechat_browser_markup import get_verified_browser_page


SESSION = "wechat:" + "a" * 64
TOKEN = "job_" + "b" * 40
ACCOUNT = "wxid_account_a"
ACCOUNT_ENDPOINT = "/api/browser/wechat/refresh/account"
session_app = test_wechat_session_csv.session_app


@pytest.fixture
def refresh_client():
    calls = []
    service = SimpleNamespace(
        start=lambda session: calls.append(session) or {"task_id": TOKEN},
        start_account=lambda account: calls.append(account) or {"task_id": TOKEN},
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
    summary = Mock()
    client.application.extensions[WECHAT_SUMMARY_EXTENSION] = summary
    status = client.get(response.json["status_url"])
    assert status.json == {
        "state": "succeeded",
        "added": 3,
        "total": 42,
        "partial": True,
        "message": "The available local records were refreshed.",
    }
    assert status.cache_control.no_store
    summary.wake.assert_called_once_with()
    assert (
        b"private/test-source" not in status.data
        and b"must-not-escape" not in status.data
    )


def test_account_refresh_accepts_one_account_and_returns_the_existing_opaque_status(refresh_client):
    client, service, calls = refresh_client
    response = client.post(ACCOUNT_ENDPOINT, json={"account_id": ACCOUNT})
    assert response.status_code == 202 and response.cache_control.no_store
    assert calls == [ACCOUNT]
    assert response.json == {
        "state": "running",
        "status_url": "/api/browser/wechat/refresh/" + TOKEN,
    }
    assert client.get(response.json["status_url"]).json["added"] == 3

    service.start_account = Mock(side_effect=LookupError("private path"))
    response = client.post(ACCOUNT_ENDPOINT, json={"account_id": "wxid_unknown"})
    assert response.status_code == 404 and b"private path" not in response.data


@pytest.mark.parametrize("payload", [
    {"account_id": ""},
    {"account_id": "../keyfile"},
    {"account_id": "account:wxid_account_a"},
    {"account_id": ["wxid_account_a", "wxid_account_b"]},
    {"account_id": ACCOUNT, "key": "not-accepted"},
    {"account_id": ACCOUNT, "chat_kind": "group"},
])
def test_account_refresh_rejects_untrusted_scope_and_key_material(refresh_client, payload):
    client, _, calls = refresh_client
    assert client.post(ACCOUNT_ENDPOINT, json=payload).status_code == 400
    assert calls == []


def test_status_exposes_actual_bounded_progress_without_internal_values(refresh_client):
    client, service, _calls = refresh_client
    summary = Mock()
    client.application.extensions[WECHAT_SUMMARY_EXTENSION] = summary
    progress = {
        "phase": "comparing",
        "parts_total": 7,
        "parts_completed": 2,
        "part_current": 3,
        "checked": 2500,
        "candidates": 12,
        "skipped_parts": 1,
        "unavailable_parts": 3,
        "excluded": 2,
        "upgraded": 0,
        "reparsed": 0,
        "conflicts": 0,
    }
    service.status = lambda _token: {
        "state": "running",
        "partial": True,
        "added": 0,
        "total": 0,
        **progress,
        "database_path": "/private/secret",
        "raw_key": "private-key",
    }
    response = client.get("/api/browser/wechat/refresh/" + TOKEN)
    summary.wake.assert_not_called()
    assert response.status_code == 200 and response.cache_control.no_store
    assert response.json == {
        "state": "running",
        "partial": True,
        "added": 0,
        "total": 0,
        **progress,
    }
    assert b"private" not in response.data


@pytest.mark.parametrize(
    "progress",
    [
        {"phase": "/private/source"},
        {"checked": True},
        {"candidates": -1},
        {"checked": 2**53},
        {"parts_total": 2, "part_current": 3},
        {"member_snapshot": "/private/contact-source"},
    ],
)
def test_unsafe_progress_returns_a_generic_status_error(refresh_client, progress):
    client, service, _calls = refresh_client
    service.status = lambda _token: {"state": "running", "partial": False, **progress}
    response = client.get("/api/browser/wechat/refresh/" + TOKEN)
    assert response.status_code == 503 and b"private" not in response.data


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
        ("post", ACCOUNT_ENDPOINT),
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


@pytest.mark.parametrize("headers", [
    {"Origin": "https://untrusted.invalid"},
    {"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"},
])
def test_conversation_refresh_rejects_cross_origin_requests(refresh_client, headers):
    client, _, calls = refresh_client
    response = client.post(
        "/api/browser/wechat/refresh", json={"session_id": SESSION}, headers=headers,
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
    response = client.get("/api/browser/wechat/refresh/" + TOKEN)
    assert response.status_code == 404
    assert "The local service may have restarted." in response.json["error"]
    assert "Reload this conversation" in response.json["error"]


def test_refresh_explains_known_start_failure(refresh_client):
    client, service, _ = refresh_client
    service.start = Mock(side_effect=RefreshNotice(RefreshIssue.BUSY))
    response = client.post("/api/browser/wechat/refresh", json={"session_id": SESSION})
    assert response.status_code == 409
    assert response.json == {"error": RefreshIssue.BUSY.value}


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

    try:
        visible = get_verified_browser_page(client, address)
        assert b"data-wechat-conversation-refresh" in visible.data
        service.available = lambda *scope: False
        hidden = get_verified_browser_page(client, address)
        assert b"data-wechat-conversation-refresh" not in hidden.data
        service.available = lambda *scope: True
        remote = client.get(address, environ_overrides={"REMOTE_ADDR": "192.168.1.9"})
        assert remote.status_code == 302
        assert "/agent" in remote.headers["Location"]
        assert b"data-wechat-conversation-refresh" not in remote.data
    finally:
        app.extensions["runtime_shutdown"]()


def test_account_refresh_action_uses_only_the_selected_native_account(session_app):
    app, _ = session_app
    available = Mock(return_value=True)
    start_account = Mock(return_value={"task_id": TOKEN})
    app.extensions[WECHAT_REFRESH_EXTENSION] = SimpleNamespace(
        account_available=available, start_account=start_account, shutdown=Mock(),
    )
    client = app.test_client()
    view = "/browser?view=text&source=wechat&chat_kind=group"
    assert b"data-wechat-account-refresh" not in client.get(view).data
    assert b"data-wechat-account-refresh" not in client.get(view + "&account=source:xlsx-2").data
    assert b"data-wechat-account-refresh" not in client.get(view + "&account=account:wxid_unknown").data
    selected = client.get(view + "&account=account:" + ACCOUNT)
    assert b"data-wechat-account-refresh" in selected.data
    assert f'data-wechat-account-id="{ACCOUNT}"'.encode() in selected.data
    available.assert_called_once_with(ACCOUNT)
    start_account.assert_not_called()

    remote_origin = client.post(ACCOUNT_ENDPOINT, json={"account_id": ACCOUNT},
                                headers={"Origin": "https://untrusted.invalid"})
    assert remote_origin.status_code == 403
    start_account.assert_not_called()
