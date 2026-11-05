"""Public route failures preserve useful copy without exposing private diagnostics.

Code version: v1.0.0-codex.0
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.native_directory_picker import NativeDirectoryPickerError
from app.core.shadow_backup import SettingsDirectoryBrowserError, ShadowBackupError
from app.web.public_errors import public_diagnostic_payload, public_exception_message


PRIVATE = "Traceback: /private/synthetic-account/token-file sk-private-synthetic-token"


def test_unexpected_exception_is_logged_privately_and_cannot_supply_public_copy(caplog):
    expected = "This source is no longer available."
    assert public_exception_message(
        LookupError(expected), fallback="Try again.", allowed=(expected,), operation="read source"
    ) == expected
    assert public_exception_message(
        LookupError(PRIVATE), fallback="Try again.", allowed=(expected,), operation="read source"
    ) == "Try again."
    assert PRIVATE in caplog.text


def test_diagnostic_boundary_keeps_user_and_tool_content_unchanged():
    content = {"error": "Literal user-supplied JSON", "text": PRIVATE}
    source = {
        "phase": "failed", "message": PRIVATE, "last_error": PRIVATE,
        "error_traceback": PRIVATE, "resource_cleanup_warning": PRIVATE,
        "response": PRIVATE, "messages": [content],
    }
    result = public_diagnostic_payload(source, fallback="Try again.", operation="status")
    assert result["message"] == result["last_error"] == "Try again."
    assert result["error_traceback"] == ""
    assert result["resource_cleanup_warning"] == "Try again."
    assert result["response"] == PRIVATE and result["messages"] == [content]
    assert source["last_error"] == PRIVATE


@pytest.mark.parametrize(
    "method,url,target,error,status",
    [
        ("post", "/api/browser/prompts", "app.core.prompt_store.PromptStore.add_pointer", ValueError, 400),
        ("post", "/api/browser/prompts", "app.core.prompt_store.PromptStore.add_pointer", LookupError, 404),
        ("delete", "/api/browser/prompts/synthetic", "app.core.prompt_store.PromptStore.remove", LookupError, 404),
        ("post", "/api/browser/prompts/synthetic/restore", "app.core.prompt_store.PromptStore.restore", LookupError, 404),
        ("post", "/api/browser/prompts/synthetic/remarks", "app.core.prompt_store.PromptStore.add_remark", ValueError, 400),
        ("delete", "/api/browser/prompts/synthetic/remarks", "app.core.prompt_store.PromptStore.remove_remark", LookupError, 404),
        ("post", "/api/browser/media/synthetic/delete", "app.core.local_media_browser.LocalMediaCatalog.delete", OSError, 409),
        ("post", "/api/browser/media/synthetic/delete", "app.core.local_media_browser.LocalMediaCatalog.delete", RuntimeError, 409),
        ("post", "/api/browser/media/synthetic/restore", "app.core.local_media_browser.LocalMediaCatalog.restore", OSError, 409),
        ("post", "/api/browser/wechat/resources/synthetic/reveal", "app.web.local_resource_routes.resolve_resource_for_reveal", LookupError, 404),
    ],
)
def test_local_resource_failure_payloads_never_include_exception_details(
    client, method, url, target, error, status
):
    with patch(target, side_effect=error(PRIVATE)):
        response = getattr(client, method)(url, json={})
    assert response.status_code == status
    assert response.get_json()["error"]
    assert PRIVATE not in response.get_data(as_text=True)


def test_known_prompt_validation_copy_remains_actionable(client):
    expected = "This media item has no indexed prompt."
    with patch("app.core.prompt_store.PromptStore.add_pointer", side_effect=ValueError(expected)):
        response = client.post("/api/browser/prompts", json={})
    assert response.status_code == 400
    assert response.get_json() == {"error": expected}


def test_cached_media_reveal_failure_does_not_expose_host_paths(client):
    with (
        patch("app.core.local_media_browser.LocalMediaCatalog.resolved_media_path", return_value=Path("/private/synthetic.png")),
        patch("app.web.local_resource_routes.reveal_media_path", side_effect=OSError(PRIVATE)),
    ):
        response = client.post("/api/browser/media/synthetic/reveal", json={})
    assert response.status_code == 500
    assert "Unable to open" in response.get_json()["error"]
    assert PRIVATE not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "url,target,error,payload,status",
    [
        ("/api/settings/directory", "browse_settings_directory", SettingsDirectoryBrowserError, {"field": "chrome_user_data_dir"}, 400),
        ("/api/settings/directory/native", "choose_native_directory", NativeDirectoryPickerError, {"field": "tunnel_project_root"}, 503),
    ],
)
def test_settings_picker_diagnostics_stay_private(client, url, target, error, payload, status):
    with patch("app.web.settings_routes." + target, side_effect=error(PRIVATE)):
        response = client.post(url, json=payload)
    assert response.status_code == status
    assert response.get_json()["error"]
    assert PRIVATE not in response.get_data(as_text=True)


def test_directory_permission_copy_remains_useful(client):
    expected = "Permission denied for this directory."
    with patch("app.web.settings_routes.browse_settings_directory", side_effect=SettingsDirectoryBrowserError(expected)):
        response = client.post("/api/settings/directory", json={"field": "chrome_user_data_dir"})
    assert response.status_code == 400 and response.get_json() == {"error": expected}


def test_shadow_backup_start_and_status_do_not_echo_private_diagnostics(app, client):
    service = app.extensions["shadow_backup_service"]
    with patch.object(service, "start", side_effect=ShadowBackupError(PRIVATE)):
        response = client.post("/settings/shadow-backup/sync")
    assert response.status_code == 302
    status = client.get("/api/settings/shadow-backup/status")
    assert status.get_json()["phase"] == "failed"
    assert PRIVATE not in status.get_data(as_text=True)


@pytest.mark.parametrize(
    "source,url,payload",
    [
        ("chatgpt", "/api/browser/chatgpt/session/refresh", {"conversation_url": "https://chatgpt.com/c/synthetic-session"}),
        ("zhihu", "/api/browser/zhihu/answerer/refresh", {"profile_url": "https://www.zhihu.com/people/synthetic-author"}),
    ],
)
def test_targeted_cache_refresh_never_echoes_runtime_diagnostics(app, client, source, url, payload):
    mode = "media" if source == "chatgpt" else "text"
    service = app.extensions["cache_runtimes"][source][mode].service
    with patch.object(service, "start", side_effect=RuntimeError(PRIVATE)):
        response = client.post(url, json=payload)
    assert response.status_code == 409
    assert response.get_json()["error"]
    assert PRIVATE not in response.get_data(as_text=True)


def test_cache_start_and_subsequent_status_keep_runtime_diagnostics_private(app, client):
    runtime = app.extensions["cache_runtimes"]["chatgpt"]["text"]
    with patch.object(runtime.service, "start", side_effect=RuntimeError(PRIVATE)):
        response = client.post("/chatgpt/start", data={"cache_content_mode": "text"})
    assert response.status_code == 302
    status = client.get("/api/chatgpt/status?content_mode=text")
    assert status.get_json()["phase"] == "failed"
    assert PRIVATE not in status.get_data(as_text=True)
    runtime.state.update(last_error=PRIVATE, message=PRIVATE, recent_events=[PRIVATE])
    status = client.get("/api/chatgpt/status?content_mode=text")
    assert PRIVATE not in status.get_data(as_text=True)


def test_cache_output_directory_failure_does_not_echo_host_diagnostics(client):
    with (
        patch("app.web.cache_routes.build_reconciled_cache_snapshot", return_value={"output_dir": "/private/synthetic"}),
        patch("app.web.cache_routes.open_directory_path", side_effect=OSError(PRIVATE)),
    ):
        response = client.post("/api/cache/chatgpt/output-directory/open", json={})
    assert response.status_code == 409
    assert "Unable to open" in response.get_json()["error"]
    assert PRIVATE not in response.get_data(as_text=True)
