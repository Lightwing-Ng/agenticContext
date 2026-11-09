"""Exercise the review capability and temporary-file HTTP boundary.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import CrawlConfig
from app.web.app import create_app
from app.web.config_store import SavedConfigStore


@pytest.fixture
def exchange_client(tmp_path, monkeypatch):
    calls = []
    files = []
    state = {"value": "initial"}
    verified = SimpleNamespace(
        package_id="f" * 64,
        manifest={
            "format_version": 1,
            "counts": dict(
                accounts=2, sources=3, conversations=2, messages=5, resources=1
            ),
            "accounts": [{"label": "External account", "external_id": "claimed-id"}],
        },
    )

    def read_package(path):
        files.append(Path(path))
        if Path(path).read_bytes() != b"valid exchange":
            raise ValueError("invalid")
        return verified

    def export_conversation(root, session, path, **scope):
        calls.append(("export", session, scope))
        Path(path).write_bytes(b"valid exchange")
        files.append(Path(path))
        return verified.manifest

    def preview_package(root, package):
        calls.append(("preview", package.package_id))
        return {"inserted_messages": 5}

    def import_package(root, package, *, expected_state):
        calls.append(("import", package.package_id))
        if expected_state != state["value"]:
            raise ValueError("stale target")
        return {"inserted_messages": 5, "published": True}

    monkeypatch.setitem(
        sys.modules,
        "app.core.wechat_exchange",
        SimpleNamespace(
            read_package=read_package, export_conversation=export_conversation
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.core.wechat_exchange_import",
        SimpleNamespace(
            preview_package=preview_package,
            import_package=import_package,
            archive_state=lambda root: state["value"],
        ),
    )
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            tmp_path / "store",
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    return app.test_client(), calls, files, state


def _preview(client, value=b"valid exchange", filename="friend.wechatx"):
    return client.post(
        "/api/browser/wechat/exchange/preview",
        data={"file": (io.BytesIO(value), filename)},
    )


def test_preview_does_not_import_and_one_use_token_commits(exchange_client):
    client, calls, files, _ = exchange_client
    preview = _preview(client, filename="../../untrusted.wechatx")
    assert preview.status_code == 200
    payload = preview.get_json()
    assert payload["counts"]["accounts"] == 2 and payload["added_messages"] == 5
    assert [call[0] for call in calls] == ["preview"]
    assert files[0].name == "package.wechatx" and files[0].is_file()
    result = client.post(
        "/api/browser/wechat/exchange/import", json={"token": payload["token"]}
    )
    assert result.status_code == 200 and result.get_json()["status"] == "imported"
    assert [call[0] for call in calls] == ["preview", "import"]
    assert not files[0].exists()
    assert (
        client.post(
            "/api/browser/wechat/exchange/import", json={"token": payload["token"]}
        ).status_code
        == 409
    )


def test_cancel_removes_uploaded_package_without_import(exchange_client):
    client, calls, files, _ = exchange_client
    token = _preview(client).get_json()["token"]
    assert (
        client.post(
            "/api/browser/wechat/exchange/cancel", json={"token": token}
        ).status_code
        == 200
    )
    assert not files[0].exists()
    assert (
        client.post(
            "/api/browser/wechat/exchange/import", json={"token": token}
        ).status_code
        == 409
    )
    assert [call[0] for call in calls] == ["preview"]


def test_changed_target_requires_fresh_preview(exchange_client):
    client, _, files, state = exchange_client
    token = _preview(client).get_json()["token"]
    state["value"] = "changed"
    response = client.post("/api/browser/wechat/exchange/import", json={"token": token})
    assert response.status_code == 409 and not files[0].exists()


def test_invalid_package_cleans_upload_and_returns_json(exchange_client):
    client, calls, files, _ = exchange_client
    response = _preview(client, b"corrupt")
    assert response.status_code == 400 and response.is_json
    assert not files[0].exists() and not calls


def test_upload_limit_is_enforced_before_parsing(exchange_client, monkeypatch):
    from app.web import wechat_exchange_routes

    monkeypatch.setattr(wechat_exchange_routes, "MAX_UPLOAD_BYTES", 8)
    client, calls, _, _ = exchange_client
    response = _preview(client)
    assert response.status_code == 413 and not calls


def test_cross_site_import_never_reaches_handler(exchange_client):
    client, calls, _, _ = exchange_client
    response = client.post(
        "/api/browser/wechat/exchange/preview",
        data={"file": (io.BytesIO(b"valid exchange"), "data.wechatx")},
        headers={"Origin": "https://untrusted.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403 and not calls


def test_export_stream_is_private_and_cleans_on_close(exchange_client):
    client, calls, files, _ = exchange_client
    session = "wechat:" + "a" * 64
    response = client.get(
        f"/browser/wechat/session/{session}/exchange?account=source:sample&chat_kind=group"
    )
    assert response.status_code == 200 and response.data == b"valid exchange"
    assert response.cache_control.no_store
    assert response.mimetype == "application/vnd.agenticcontext.wechat-exchange"
    assert calls == [("export", session, {"account": "source:sample", "kind": "group"})]
    response.close()
    assert not files[0].exists()


def test_upload_reservation_bounds_inflight_previews(exchange_client, monkeypatch):
    from app.web import wechat_exchange_routes

    client, _, files, _ = exchange_client
    entered, release = threading.Event(), threading.Event()

    def slow_preview(root, package):
        entered.set()
        assert release.wait(5)
        return {"inserted_messages": 5}

    monkeypatch.setattr(wechat_exchange_routes, "MAX_PENDING_PREVIEWS", 1)
    monkeypatch.setattr(
        sys.modules["app.core.wechat_exchange_import"], "preview_package", slow_preview
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: _preview(client.application.test_client()))
        try:
            assert entered.wait(5)
            response = _preview(client)
            assert response.status_code == 409 and len(files) == 1
        finally:
            release.set()
        token = future.result(timeout=5).get_json()["token"]
    client.post("/api/browser/wechat/exchange/cancel", json={"token": token})
    assert not files[0].exists()


def _export_status(client, token):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/browser/wechat/exchange/export/{token}")
        if response.get_json().get("status") != "preparing":
            return response
        time.sleep(0.01)
    raise AssertionError("The bounded synthetic export did not finish.")


def test_async_export_reports_ready_then_downloads_exact_bytes(exchange_client):
    client, calls, files, _ = exchange_client
    session = "wechat:" + "a" * 64
    response = client.post(
        "/api/browser/wechat/exchange/export",
        json={"session_id": session, "account": "source:sample", "chat_kind": "group"},
    )
    assert response.status_code == 202 and response.get_json()["status"] == "preparing"
    token = response.get_json()["token"]
    state = _export_status(client, token)
    assert state.status_code == 200 and state.cache_control.no_store
    assert state.get_json()["status"] == "ready"
    download = client.get(state.get_json()["download_url"])
    assert download.status_code == 200 and download.data == b"valid exchange"
    assert (
        download.cache_control.no_store
        and ".wechatx" in download.headers["Content-Disposition"]
    )
    download.close()
    assert not files[0].exists()
    assert calls == [("export", session, {"account": "source:sample", "kind": "group"})]


def test_async_export_limits_active_work_and_hides_failed_paths(
    exchange_client, monkeypatch
):
    client, _, _, _ = exchange_client
    entered, release = threading.Event(), threading.Event()

    def fail_export(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        raise ValueError("/private/donor/secret-source")

    monkeypatch.setattr(
        sys.modules["app.core.wechat_exchange"], "export_conversation", fail_export
    )
    payload = {"session_id": "wechat:" + "a" * 64}
    response = client.post("/api/browser/wechat/exchange/export", json=payload)
    try:
        assert entered.wait(5)
        assert (
            client.post("/api/browser/wechat/exchange/export", json=payload).status_code
            == 409
        )
    finally:
        release.set()
    state = _export_status(client, response.get_json()["token"])
    assert state.get_json()["status"] == "failed"
    assert "secret-source" not in state.get_data(as_text=True)
