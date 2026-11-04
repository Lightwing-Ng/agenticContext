"""Resource presentation and Finder routes retain account and host boundaries.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core.config import CrawlConfig
from app.core.chat_history_browser import query_chat_history
from app.core.wechat_archive import import_wechat_archive
from app.core.wechat_resources import (
    present_resource,
    resolve_resource_for_reveal,
    resource_reference,
)
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests.test_wechat_archive import make_archive


OWNER = "wxid_fixture_owner"
PEER = "fixture_peer@chatroom"
REFERENCE = {"kind": "image", "account_id": OWNER, "peer_id": PEER}


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"resource_reference": {**REFERENCE, "account_id": "wxid_other_owner"}},
        {"resource_reference": {**REFERENCE, "peer_id": "another_room@chatroom"}},
        {"resource_reference": {**REFERENCE, "kind": "executable"}},
        {"resource_reference": {**REFERENCE, "kind": []}},
        {"resource_reference": {**REFERENCE, "kind": {}}},
    ],
)
def test_unverified_resource_scope_is_not_linked_to_a_mac_file(value):
    assert resource_reference(value, OWNER, PEER) is None


@pytest.mark.parametrize("status", ["file", "candidate", "folder", "missing"])
def test_public_presentation_retains_missing_items_without_host_path_leaks(status):
    private_path = Path("/private/synthetic-wechat-account/resource")
    resolver = Mock()
    resolver.resolve.return_value = {
        "status": status,
        "path": private_path if status != "missing" else None,
        "label": "Show the conversation image folder in Finder",
        "evidence": "private_backend_locator",
    }
    item = present_resource(REFERENCE, resolver)
    assert item["label"] == "Image"
    assert item["kind"] == "image"
    assert item["status"] == status
    assert item["can_reveal"] == (status != "missing")
    assert str(private_path) not in json.dumps(item)
    assert "path" not in item and "evidence" not in item
    assert item["status_label"]


def test_finder_resolution_requires_a_current_stored_reference_and_rechecks_the_mac(
    tmp_path,
):
    record = {
        "account_id": OWNER,
        "peer_id": PEER,
        "provenance": json.dumps({"resource_reference": REFERENCE}),
    }
    repository = SimpleNamespace(resource_message=Mock(return_value=record))
    destination = {"status": "folder", "path": tmp_path, "label": "Finder"}
    with (
        patch(
            "app.core.wechat_resources.WechatArchiveRepository", return_value=repository
        ),
        patch("app.core.wechat_resources.WechatMacResourceResolver") as resolver,
    ):
        resolver.return_value.resolve.return_value = destination
        assert resolve_resource_for_reveal(tmp_path, "fixture-resource") == destination
        repository.resource_message.assert_called_once_with("fixture-resource")
        resolver.return_value.resolve.assert_called_once_with(REFERENCE)
        resolver.return_value.resolve.return_value = {"status": "missing", "path": None}
        with pytest.raises(LookupError, match="no longer available"):
            resolve_resource_for_reveal(tmp_path, "fixture-resource")
        repository.resource_message.return_value = None
        with pytest.raises(LookupError, match="needs refreshing"):
            resolve_resource_for_reveal(tmp_path, "fixture-resource")


@pytest.fixture
def resource_app(tmp_path):
    store = tmp_path / "store"
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            store,
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True, TEST_STORE_ROOT=store)
    return app


def test_finder_route_uses_the_stored_destination_not_client_paths(
    resource_app, tmp_path
):
    destination = {"status": "folder", "path": tmp_path / "trusted-account"}
    with (
        patch(
            "app.web.local_resource_routes.resolve_resource_for_reveal",
            return_value=destination,
        ) as resolve,
        patch("app.web.local_resource_routes.reveal_media_path") as reveal,
        patch("app.web.local_resource_routes.open_directory_path") as open_folder,
    ):
        response = resource_app.test_client().post(
            "/api/browser/wechat/resources/fixture-reference/reveal",
            json={"path": "/etc/private-unrelated-file", "account_id": "unrelated"},
        )
        assert response.status_code == 200
        assert response.json["target_kind"] == "folder"
        resolve.assert_called_once_with(
            resource_app.config["TEST_STORE_ROOT"], "fixture-reference"
        )
        open_folder.assert_called_once_with(destination["path"])
        reveal.assert_not_called()
        assert str(tmp_path) not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "method,headers,environ",
    [
        ("GET", {}, {}),
        ("POST", {"Origin": "https://unrelated.example"}, {}),
        ("POST", {"Sec-Fetch-Site": "cross-site"}, {}),
        ("POST", {}, {"REMOTE_ADDR": "192.0.2.7"}),
    ],
)
def test_finder_route_cannot_be_prefetched_or_invoked_cross_site(
    resource_app, method, headers, environ
):
    with (
        patch("app.web.local_resource_routes.resolve_resource_for_reveal") as resolve,
        patch("app.web.local_resource_routes.reveal_media_path") as reveal,
    ):
        response = resource_app.test_client().open(
            "/api/browser/wechat/resources/fixture-reference/reveal",
            method=method,
            headers=headers,
            environ_overrides=environ,
        )
        assert response.status_code in {403, 405}
        resolve.assert_not_called()
        reveal.assert_not_called()


def test_missing_resource_returns_an_error_without_launching_finder(resource_app):
    with (
        patch(
            "app.web.local_resource_routes.resolve_resource_for_reveal",
            side_effect=LookupError("Not found on this Mac"),
        ),
        patch("app.web.local_resource_routes.reveal_media_path") as reveal,
    ):
        response = resource_app.test_client().post(
            "/api/browser/wechat/resources/fixture-reference/reveal", json={}
        )
        assert response.status_code == 404
        assert response.json["error"] == "Not found on this Mac"
        reveal.assert_not_called()


def test_native_finder_failure_does_not_expose_a_private_host_path(resource_app):
    private_path = Path("/private/synthetic-account/private-resource")
    with (
        patch(
            "app.web.local_resource_routes.resolve_resource_for_reveal",
            return_value={"status": "file", "path": private_path},
        ),
        patch(
            "app.web.local_resource_routes.reveal_media_path",
            side_effect=FileNotFoundError(2, "No such file", str(private_path)),
        ),
    ):
        response = resource_app.test_client().post(
            "/api/browser/wechat/resources/fixture-reference/reveal", json={}
        )
        assert response.status_code == 409
        assert str(private_path) not in response.get_data(as_text=True)
        assert "may have moved" in response.json["error"]


@pytest.mark.parametrize("status", ["file", "candidate"])
def test_exact_and_candidate_files_are_selected_without_opening_the_payload(
    resource_app, tmp_path, status
):
    destination = {"status": status, "path": tmp_path / "trusted-file"}
    with (
        patch(
            "app.web.local_resource_routes.resolve_resource_for_reveal",
            return_value=destination,
        ),
        patch("app.web.local_resource_routes.reveal_media_path") as reveal,
        patch("app.web.local_resource_routes.open_directory_path") as open_folder,
    ):
        response = resource_app.test_client().post(
            "/api/browser/wechat/resources/fixture-reference/reveal", json={}
        )
        assert response.status_code == 200
        reveal.assert_called_once_with(destination["path"])
        open_folder.assert_not_called()


@pytest.mark.parametrize("status", ["stale", "invalid"])
def test_unavailable_resource_index_is_explicit_on_the_conversation_index(
    resource_app, status
):
    page = query_chat_history(resource_app.config["TEST_STORE_ROOT"], source="wechat")
    page.messaging["resource_index_status"] = status
    with patch("app.web.local_resource_routes.query_chat_history", return_value=page):
        response = resource_app.test_client().get("/browser?view=text&source=wechat")
    assert response.status_code == 200
    assert "Media and file references need refreshing" in response.get_data(
        as_text=True
    )


def test_an_old_account_without_resource_metadata_is_not_presented_as_complete(
    resource_app, tmp_path
):
    store = resource_app.config["TEST_STORE_ROOT"]
    import_wechat_archive(make_archive(tmp_path / "archive"), store)
    session = query_chat_history(
        store, source="wechat", account="account:wxid_account_a"
    ).sessions[0]
    page = query_chat_history(store, source="wechat", session=session.stable_id)
    page.messaging.update(resource_index_status="ready", resource_index_accounts=())
    with patch("app.web.local_resource_routes.query_chat_history", return_value=page):
        response = resource_app.test_client().get(
            "/browser?view=text&source=wechat&session=" + session.stable_id
        )
    assert response.status_code == 200
    assert (
        "Media and file references are unavailable in this source archive."
        in response.get_data(as_text=True)
    )
