"""Local grants, privacy, and native image results for ChatGPT phone tools.

Code version: v1.0.1-codex.0
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.iphone.service import IPhoneOperationError, IPhoneService
from tests import test_iphone_routes as route_fixtures


iphone_app = route_fixtures.iphone_app


def test_catalog_and_grant_do_not_create_a_runtime(tmp_path):
    factory = Mock(side_effect=AssertionError("Catalog must not touch the phone"))
    service = IPhoneService(tmp_path / "state", platform="darwin", runtime_factory=factory)
    assert service.tunnel_tools() == []
    assert service.tunnel_instructions() == ""
    assert service.set_tunnel_enabled(True) == {"available": True, "enabled": True}
    pytest.importorskip("fcntl")
    tools = service.tunnel_tools()
    assert len(tools) == 15
    names = {tool["name"] for tool in tools}
    assert {"pua_setup", "pua_doctor", "pua_screen_frame", "pua_screen_action"}.isdisjoint(names)
    ready = next(tool for tool in tools if tool["name"] == "pua_ready")
    assert ready["inputSchema"]["properties"]["recover"]["enum"] == [False]
    assert "recover=false" in service.tunnel_instructions()
    assert service.started() is False
    factory.assert_not_called()
    assert not (tmp_path / "state").exists()
    service.set_tunnel_enabled(False)
    assert service.tunnel_tools() == []


def test_catalog_is_disabled_on_windows_and_when_external_operations_are_disabled(tmp_path):
    service = IPhoneService(tmp_path, platform="win32")
    assert service.tunnel_status() == {"available": False, "enabled": False}
    assert service.tunnel_tools() == []
    enabled = True
    service = IPhoneService(tmp_path, platform="darwin", external_operations_enabled=lambda: enabled)
    service.set_tunnel_enabled(True)
    enabled = False
    assert service.tunnel_status() == {"available": False, "enabled": False}
    assert service.tunnel_tools() == []
    with pytest.raises(IPhoneOperationError, match="disabled"):
        service.set_tunnel_enabled(True)


def test_tunnel_capture_keeps_image_block_but_removes_local_path(tmp_path):
    pytest.importorskip("fcntl")
    image = tmp_path / "screen.png"
    image.write_bytes(b"fake-image-bytes")
    runtime = SimpleNamespace(call=Mock(return_value={
        "image": {"path": str(image), "mimeType": "image/png"}, "nodes": [],
    }))
    service = IPhoneService(tmp_path, platform="darwin", runtime_factory=lambda _: runtime)
    service.set_tunnel_enabled(True)
    result = service.call_tunnel_tool("pua_observe", {"mode": "screenshot"})
    assert result["isError"] is False
    text, pixels = result["content"]
    assert "path" not in json.loads(text["text"])["image"]
    assert pixels == {"type": "image", "mimeType": "image/png", "data": "ZmFrZS1pbWFnZS1ieXRlcw=="}
    runtime.call.assert_called_once_with("pua_observe", {"mode": "screenshot"})


def test_selector_recovery_uses_attached_image_without_local_file_instructions(tmp_path):
    pytest.importorskip("fcntl")
    from app.core.iphone.wda_client import WDAError
    from app.core.iphone.wda_controller import PhoneController

    image = tmp_path / "screen.png"
    image.write_bytes(b"fake-image-bytes")
    client = Mock()
    controller = PhoneController(client, tmp_path)
    error = WDAError(
        "no_such_element",
        "No matching element.",
        details={
            "observation": {
                "image": {
                    "path": str(image),
                    "mimeType": "image/png",
                    "pixel_to_point": 1 / 3,
                },
            },
        },
    )
    controller.switch_to_coordinates(error)
    assert "observation.image.path" in error.details["recovery"]["next_step"]
    runtime = SimpleNamespace(call=Mock(side_effect=error))
    service = IPhoneService(tmp_path, platform="darwin", runtime_factory=lambda _: runtime)
    service.set_tunnel_enabled(True)

    result = service.call_tunnel_tool("pua_tap", {"selector": {"label": "Continue"}})

    assert result["isError"] is True
    text, pixels = result["content"]
    details = json.loads(text["text"])["error"]
    assert details["observation"]["image"] == {
        "mimeType": "image/png",
        "pixel_to_point": 1 / 3,
    }
    recovery = details["recovery"]
    assert recovery["use"] == "coordinates"
    assert recovery["visual_check_required"] is True
    assert recovery["replay_action"] is False
    assert recovery["next_tool"] == "pua_tap"
    assert "image.path" not in recovery["next_step"]
    assert "attached MCP image content" in recovery["next_step"]
    assert "image.pixel_to_point" in recovery["next_step"]
    assert "pua_observe(mode=screenshot)" in recovery["next_step"]
    assert pixels == {"type": "image", "mimeType": "image/png", "data": "ZmFrZS1pbWFnZS1ieXRlcw=="}
    assert "observation.image.path" in error.details["recovery"]["next_step"]
    assert error.details["observation"]["image"]["path"] == str(image)
    assert client.mock_calls == []
    runtime.call.assert_called_once_with("pua_tap", {"selector": {"label": "Continue"}})


def test_ready_never_requests_runtime_recovery(tmp_path):
    pytest.importorskip("fcntl")
    runtime = SimpleNamespace(call=Mock(return_value={"ready": False}))
    service = IPhoneService(tmp_path, platform="darwin", runtime_factory=lambda _: runtime)
    service.set_tunnel_enabled(True)
    service.call_tunnel_tool("pua_ready", {})
    runtime.call.assert_called_once_with("pua_ready", {"recover": False})
    with pytest.raises(IPhoneOperationError, match="recovery"):
        service.call_tunnel_tool("pua_ready", {"recover": True})
    with pytest.raises(IPhoneOperationError):
        service.call_tunnel_tool("pua_setup", {"action": "start"})
    service.set_tunnel_enabled(False)
    with pytest.raises(IPhoneOperationError):
        service.call_tunnel_tool("pua_tap", {"x": 1, "y": 1})
    assert runtime.call.call_count == 1


def test_grant_is_rechecked_after_runtime_construction(tmp_path):
    pytest.importorskip("fcntl")
    runtime = SimpleNamespace(call=Mock())

    def factory(_):
        service.set_tunnel_enabled(False)
        return runtime

    service = IPhoneService(tmp_path, platform="darwin", runtime_factory=factory)
    service.set_tunnel_enabled(True)
    with pytest.raises(IPhoneOperationError, match="revoked"):
        service.call_tunnel_tool("pua_observe", {"mode": "tree"})
    runtime.call.assert_not_called()


def test_local_toggle_requires_loopback_and_enabled_external_operations(iphone_app):
    application, service = iphone_app
    service.supported = True
    client = application.test_client()
    assert client.get("/api/iphone/tunnel").get_json() == {"available": False, "enabled": False}
    assert client.post("/api/iphone/tunnel", json={"enabled": True}).status_code == 409
    application.config["AGENT_EXTERNAL_OPERATIONS_ENABLED"] = True
    response = client.post("/api/iphone/tunnel", json={"enabled": True})
    assert response.status_code == 200
    assert response.get_json() == {"available": True, "enabled": True}
    assert service.started() is False
    response = client.post(
        "/api/iphone/tunnel", json={"enabled": False},
        headers={"Origin": "https://untrusted.example"},
    )
    assert response.status_code == 403
    assert service.tunnel_status()["enabled"] is True
    for payload in ({"enabled": "true"}, {"enabled": True, "extra": 1}, {}):
        assert client.post("/api/iphone/tunnel", json=payload).status_code == 400
    assert client.post("/api/iphone/tunnel", json={"enabled": False}).get_json()["enabled"] is False


def test_unlocked_lan_client_cannot_grant_phone_access(iphone_app):
    from tests.test_iphone_routes import LAN_ENVIRON, LAN_ORIGIN, unlocked_lan_client

    application, service = iphone_app
    application.config["AGENT_EXTERNAL_OPERATIONS_ENABLED"] = True
    service.supported = True
    client = unlocked_lan_client(application)
    response = client.post(
        "/api/iphone/tunnel", json={"enabled": True},
        environ_base=LAN_ENVIRON, headers=LAN_ORIGIN,
    )
    assert response.status_code == 403
    assert service.tunnel_status()["enabled"] is False
