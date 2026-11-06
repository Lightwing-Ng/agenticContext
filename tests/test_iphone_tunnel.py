"""Offline contracts for the locally enabled ChatGPT iPhone Tunnel extension.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import tunnel_mcp
from app.core.agent.capability_registry import validate_closed_schema
from app.core.computer_use_agent import ComputerUseSettings
from app.core.iphone.service import IPhoneOperationError, IPhoneService, sanitize_tunnel_result
from app.core.tunnel_mcp import (
    MCP_STATELESS_PROTOCOL_VERSION,
    SERVER_INSTRUCTIONS,
    WECHAT_ARCHIVE_INSTRUCTIONS,
    McpAdmissionRevoked,
    TunnelMcpService,
    tunnel_tool_definitions,
)
from app.core.tunnel_projects import ProjectRegistry


def definition(name, properties, required=(), *, read_only=False):
    return {
        "name": name,
        "title": "iPhone " + name[4:],
        "description": "Operate the locally enabled iPhone.",
        "inputSchema": {
            "type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only},
    }


class FakePhoneService:
    """A closed-schema service boundary with no runtime, files, or phone transport."""

    def __init__(self):
        self.enabled = False
        self.operations_enabled = True
        self.supported = True
        self.calls = []
        self.catalog_reads = 0
        self.failure = None
        self.result = {"content": [{"type": "text", "text": '{"ok":true}'}], "isError": False}
        self.definitions = [
            definition("pua_observe", {"mode": {"type": "string", "enum": ["tree", "screenshot"]}}, read_only=True),
            definition("pua_screen", {"action": {"type": "string", "enum": ["pause", "resume"]}}, ("action",)),
            definition("pua_apps", {"query": {"type": "string"}}, ("query",), read_only=True),
        ]

    def tunnel_tools(self):
        self.catalog_reads += 1
        return deepcopy(self.definitions) if self.enabled and self.operations_enabled and self.supported else []

    def tunnel_instructions(self):
        return "For iPhone tasks first call pua_ready; setup remains on the local iPhone page." if self.tunnel_tools() else ""

    def call_tunnel_tool(self, name, arguments):
        catalog = {item["name"]: item for item in self.tunnel_tools()}
        if name not in catalog:
            return {"content": [{"type": "text", "text": '{"error":{"code":"iphone_tunnel_disabled"}}'}], "isError": True}
        try:
            validate_closed_schema(arguments, catalog[name]["inputSchema"], subject="iPhone arguments")
        except ValueError:
            return {"content": [{"type": "text", "text": '{"error":{"code":"invalid_argument"}}'}], "isError": True}
        self.calls.append((name, deepcopy(arguments)))
        if self.failure is not None:
            raise self.failure
        return deepcopy(self.result)


@pytest.fixture
def phone():
    return FakePhoneService()


@pytest.fixture
def service(tmp_path, monkeypatch, phone):
    # Token accounting stays deterministic and cannot fetch an encoding asset.
    monkeypatch.setattr(tunnel_mcp, "_estimated_tool_tokens", lambda text: len(text))
    return TunnelMcpService(
        lambda: ComputerUseSettings(workspace_path=str(tmp_path)),
        registry=ProjectRegistry(tmp_path / "projects.json"),
        iphone_service=phone,
    )


def rpc(service, method, params=None, *, provider="chatgpt", admission=None, headers=None):
    status, response = service.handle(
        {"jsonrpc": "2.0", "id": 17, "method": method, "params": params or {}},
        headers or {}, provider=provider, admission=admission,
    )
    assert status == 200
    return response


def call(service, name, arguments=None, **kwargs):
    return rpc(service, "tools/call", {"name": name, "arguments": arguments or {}}, **kwargs)


@pytest.mark.parametrize("gate", ["enabled", "operations_enabled", "supported"])
def test_closed_gate_hides_and_refuses_phone_tools_without_dispatch(service, phone, gate):
    phone.enabled = True
    setattr(phone, gate, False)
    assert rpc(service, "tools/list")["result"]["tools"] == tunnel_tool_definitions()
    assert rpc(service, "initialize")["result"]["instructions"] == (
        SERVER_INSTRUCTIONS + " " + WECHAT_ARCHIVE_INSTRUCTIONS
    )
    assert call(service, "pua_observe")["error"]["code"] == -32602
    assert phone.calls == []
    assert service.activity_snapshot()["call_count"] == 0


def test_catalog_is_additive_and_metadata_does_not_run_phone_commands(service, phone):
    phone.enabled = True
    base = tunnel_tool_definitions()
    catalog = rpc(service, "tools/list")["result"]["tools"]
    assert catalog[:len(base)] == base
    assert catalog[len(base):] == phone.definitions
    instructions = rpc(service, "initialize")["result"]["instructions"]
    assert instructions.startswith(SERVER_INSTRUCTIONS)
    assert "first call pua_ready" in instructions
    discovered = rpc(service, "server/discover")["result"]
    assert discovered["instructions"] == instructions
    assert phone.calls == []
    assert not any(item["name"] in {"pua_setup", "pua_doctor"} for item in catalog)


def test_gemini_never_reads_or_executes_the_chatgpt_phone_capability(service, phone):
    phone.enabled = True
    assert rpc(service, "tools/list", provider="gemini")["result"]["tools"] == [
        tool for tool in tunnel_tool_definitions() if not tool["name"].startswith("wechat_")
    ]
    assert rpc(service, "initialize", provider="gemini")["result"]["instructions"] == SERVER_INSTRUCTIONS
    assert call(service, "pua_observe", provider="gemini")["error"]["code"] == -32602
    assert phone.catalog_reads == 0
    assert phone.calls == []


def test_native_image_content_and_stateless_metadata_survive_without_duplication(service, phone):
    phone.enabled = True
    phone.result = {
        "content": [
            {"type": "text", "text": '{"observation_id":"fresh-1","image":{"mimeType":"image/png"}}'},
            {"type": "image", "mimeType": "image/png", "data": "aW1hZ2U="},
        ],
        "isError": False,
    }
    response = call(
        service, "pua_observe", {"mode": "screenshot"},
        headers={"MCP-Protocol-Version": MCP_STATELESS_PROTOCOL_VERSION},
    )["result"]
    assert response["content"] == phone.result["content"]
    assert "structuredContent" not in response
    assert response["resultType"] == "complete"
    assert phone.calls == [("pua_observe", {"mode": "screenshot"})]
    activity = service.activity_snapshot("chatgpt")
    assert activity["call_count"] == 1
    assert "aW1hZ2U=" not in json.dumps(activity)


def test_phone_action_argument_is_valid_but_project_authority_cannot_be_injected(service, phone):
    phone.enabled = True
    assert call(service, "pua_screen", {"action": "pause"})["result"]["isError"] is False
    for arguments in ({"action": "pause", "project": "main"}, {"action": "pause", "runtime": "any"}):
        response = call(service, "pua_screen", arguments)["result"]
        assert response["isError"] is True
    assert phone.calls == [("pua_screen", {"action": "pause"})]


@pytest.mark.parametrize("arguments", [None, [], False, "", 2])
def test_phone_argument_shape_is_not_coerced_to_an_empty_object(service, phone, arguments):
    phone.enabled = True
    response = rpc(service, "tools/call", {"name": "pua_observe", "arguments": arguments})
    assert response["error"]["code"] == -32602
    assert phone.calls == []


def test_authorization_is_rechecked_at_phone_admission(service, phone):
    phone.enabled = True
    admissions = 0

    @contextmanager
    def admission():
        nonlocal admissions
        admissions += 1
        if admissions == 2:
            raise McpAdmissionRevoked()
        yield

    with pytest.raises(McpAdmissionRevoked):
        call(service, "pua_observe", admission=admission)
    assert admissions == 2
    assert phone.calls == []
    assert service.activity_snapshot()["call_count"] == 0


def test_local_revocation_between_catalog_and_dispatch_cannot_start_a_phone_action(service, phone):
    phone.enabled = True
    admissions = 0

    @contextmanager
    def admission():
        nonlocal admissions
        admissions += 1
        if admissions == 2:
            phone.enabled = False
        yield

    result = call(service, "pua_observe", admission=admission)["result"]
    assert result["isError"] is True
    assert "iphone_tunnel_disabled" in result["content"][0]["text"]
    assert phone.calls == []


def test_native_uncertainty_is_returned_once_and_not_replayed(service, phone):
    phone.enabled = True
    phone.result = {"content": [{"type": "text", "text": '{"error":{"code":"action_uncertain","uncertain":true}}'}], "isError": True}
    assert call(service, "pua_screen", {"action": "pause"})["result"] == phone.result
    assert len(phone.calls) == 1
    assert service.activity_snapshot()["call_count"] == 1


def test_phone_content_is_not_recorded_in_activity_or_application_logs(service, phone, caplog):
    phone.enabled = True
    phone.result["content"][0]["text"] = '{"result":"private response marker"}'
    with caplog.at_level("INFO"):
        call(service, "pua_apps", {"query": "private query marker"})
    serialized = json.dumps(service.activity_snapshot())
    assert "private query marker" not in serialized + caplog.text
    assert "private response marker" not in serialized + caplog.text
    assert '"project": ""' in serialized
    assert '"target": ""' in serialized


def test_boundary_exception_returns_a_safe_failure_without_losing_activity(service, phone, caplog):
    phone.enabled = True
    phone.failure = OSError("/private/phone-state/private-secret")
    response = call(service, "pua_observe")["result"]
    assert response["isError"] is True
    assert "iphone_unavailable" in response["content"][0]["text"]
    assert "private-secret" not in json.dumps(response) + caplog.text
    assert len(phone.calls) == 1
    assert service.activity_snapshot()["call_count"] == 1


def test_service_refusals_keep_their_actionable_error_code(service, phone):
    phone.enabled = True
    phone.failure = IPhoneOperationError("iphone_access_disabled", "iPhone access was revoked.")
    result = call(service, "pua_observe")["result"]
    assert json.loads(result["content"][0]["text"])["error"]["code"] == "iphone_access_disabled"
    assert result["isError"] is True


def test_unpublished_phone_tools_and_original_project_tool_validation_remain_closed(service, phone):
    phone.enabled = True
    for name in ("pua_setup", "pua_doctor", "pua_screen_frame", "pua_unknown"):
        assert call(service, name)["error"]["code"] == -32602
    # A phone opt-in grants no path or project selection to the coding tools.
    result = call(service, "read_files", {"paths": ["/private/file"]})["result"]
    assert result["isError"] is True
    assert phone.calls == []


def test_real_service_catalog_is_lazy_and_ready_cannot_request_recovery(tmp_path, service):
    pytest.importorskip("fcntl")
    constructed = []

    def forbidden_runtime(path):
        constructed.append(path)
        raise AssertionError("Metadata and invalid recovery must not construct a runtime")

    phone = IPhoneService(tmp_path / "phone", platform="darwin", runtime_factory=forbidden_runtime)
    service._iphone_service = phone
    phone.set_tunnel_enabled(True)
    tools = rpc(service, "tools/list")["result"]["tools"]
    phone_tools = [tool for tool in tools if tool["name"].startswith("pua_")]
    assert len(phone_tools) == 15
    ready = next(tool for tool in phone_tools if tool["name"] == "pua_ready")
    assert ready["inputSchema"]["properties"]["recover"]["enum"] == [False]
    assert "recover=false" in rpc(service, "initialize")["result"]["instructions"]
    result = call(service, "pua_ready", {"recover": True})["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["error"]["code"] == "invalid_argument"
    assert constructed == []
    assert not (tmp_path / "phone").exists()


def test_real_service_and_runtime_share_the_lock_and_return_sanitized_images(tmp_path, service):
    fcntl = pytest.importorskip("fcntl")
    from app.core.iphone.mcp_server import Runtime
    from app.core.iphone.wda_controller import PhoneController
    from tests.iphone_wda_fakes import FakeWDA

    state_dir = tmp_path / "phone"
    runtime = Runtime(state_dir)
    transport = FakeWDA()
    transport.close = lambda: None
    runtime.client = transport
    runtime.phone = PhoneController(transport, state_dir)
    runtime.phone.screen = runtime.screen
    phone = IPhoneService(state_dir, platform="darwin", runtime_factory=lambda path: runtime)
    service._iphone_service = phone
    phone.set_tunnel_enabled(True)
    try:
        with (state_dir / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            refused = call(service, "pua_observe", {"mode": "screenshot"})["result"]
            assert refused["isError"] is True
            assert json.loads(refused["content"][0]["text"])["error"]["code"] == "device_busy"
            assert transport.calls == []
        result = call(service, "pua_observe", {"mode": "screenshot"})["result"]
        assert result["isError"] is False
        assert [block["type"] for block in result["content"]] == ["text", "image"]
        data = json.loads(result["content"][0]["text"])
        assert "path" not in data["image"]
        assert str(state_dir) not in json.dumps(result)
        assert "structuredContent" not in result
        assert result["content"][1]["mimeType"] == "image/png"
        before = list(transport.calls)
        invalid = call(service, "pua_observe", {"mode": "tree", "project": "main"})["result"]
        assert invalid["isError"] is True
        assert json.loads(invalid["content"][0]["text"])["error"]["code"] == "invalid_argument"
        assert transport.calls == before
        phone.set_tunnel_enabled(False)
        assert call(service, "pua_observe")["error"]["code"] == -32602
        assert transport.calls == before
    finally:
        phone.stop_at_exit()


def test_phone_tools_retain_the_real_http_bearer_and_external_operations_boundary(tmp_path, phone):
    from app.web.app import create_app

    application = create_app(
        tmp_path / "store",
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
    )
    application.config.update(TESTING=True)
    service = application.extensions["tunnel_mcp_service"]
    wired_phone = application.extensions["iphone_service"]
    assert service._iphone_service is wired_phone
    # Replace only the phone transport boundary; real routes and bearer admission remain.
    service._iphone_service = phone
    phone.enabled = True
    application.extensions["tunnel_runtime"]._authorization = "Bearer isolated-test-token"
    request = {"jsonrpc": "2.0", "id": 18, "method": "tools/call", "params": {"name": "pua_observe", "arguments": {}}}
    with application.test_client() as client:
        assert client.post("/mcp", json=request).status_code == 401
        assert client.post("/mcp", json=request, headers={"Authorization": "Bearer incorrect"}).status_code == 401
        assert phone.calls == []
        result = client.post("/mcp", json=request, headers={"Authorization": "Bearer isolated-test-token"})
        assert result.status_code == 200
        assert result.get_json()["result"]["isError"] is False
        assert len(phone.calls) == 1
        # Restore the actual factory-wired permission boundary with a simulated
        # supported host and explicit grant; the disabled switch must still win.
        service._iphone_service = wired_phone
        wired_phone.supported = True
        wired_phone.set_tunnel_enabled(True)
        application.config["AGENT_EXTERNAL_OPERATIONS_ENABLED"] = False
        refused = client.post("/mcp", json=request, headers={"Authorization": "Bearer isolated-test-token"})
        assert refused.status_code == 200
        assert refused.get_json()["error"]["code"] == -32602
        assert wired_phone.started() is False
        assert len(phone.calls) == 1


@pytest.mark.parametrize("condition", ["cold", "not_ready", "xctest", "recovering"])
def test_real_ready_failures_direct_tunnel_recovery_to_the_local_page(service, condition):
    pytest.importorskip("fcntl")
    from app.core.iphone.mcp_server import Runtime
    from app.core.iphone.wda_client import WDAError

    errors = {
        "cold": WDAError("pua_unreachable", "No local WDA service."),
        "not_ready": WDAError("not_ready", "PUA is not accepting commands. Run pua_doctor and inspect setup status."),
        "xctest": WDAError("unknown error", "Not authorized for performing UI testing actions"),
    }
    # Exercise the real READY decisions without Runtime construction, files, or I/O.
    runtime = Runtime.__new__(Runtime)
    runtime.screen = SimpleNamespace(locked_pause_id=lambda: None)
    runtime.client = SimpleNamespace(request=Mock(side_effect=errors.get(condition)), close=Mock())
    runtime.phone = SimpleNamespace(reset=Mock())
    runtime.setup_manager = SimpleNamespace(
        pending_recovery=lambda: {"job_id": "local-recovery-job"} if condition == "recovering" else None,
    )
    bridge = SimpleNamespace(call=lambda name, arguments: runtime.ready(**arguments))
    phone = IPhoneService(platform="darwin", runtime_factory=lambda path: bridge)
    phone.set_tunnel_enabled(True)
    service._iphone_service = phone
    result = call(service, "pua_ready", {"screenshot": False})["result"]
    data = json.loads(result["content"][0]["text"])
    evidence = data.get("error", data)
    recovery = evidence["recovery"]
    assert recovery["requires_local_setup"] is True
    assert "local iPhone page" in recovery["next_step"]
    assert "recover=false" in recovery["next_step"]
    assert not {"next_tool", "next_arguments", "status_tool", "status_arguments"} & recovery.keys()
    assert "recover=true" not in json.dumps(data)
    assert "pua_setup" not in json.dumps(data)
    assert "pua_doctor" not in json.dumps(data)
    if condition in {"cold", "not_ready"}:
        assert result["isError"] is True
        assert evidence["code"] == errors[condition].code
        assert evidence["uncertain"] is False
    else:
        assert result["isError"] is False
        assert data["ready"] is False
        assert data["state"] == ("recovering" if condition == "recovering" else "recovery_required")
    if condition == "recovering":
        assert recovery["job_id"] == "local-recovery-job"


def test_tunnel_recovery_projection_keeps_semantic_recovery_and_nested_evidence():
    semantic = {"next_tool": "pua_tap", "next_arguments": {"x": 40, "y": 80}, "visual_check_required": True}
    raw = {
        "results": [
            {"error": {"code": "selector_failed", "recovery": semantic}},
            {"error": {
                "code": "not_ready", "uncertain": True, "action_executed": False,
                "recovery": {"next_tool": "pua_setup", "next_arguments": {"action": "status"}},
                "observation": {"image": {"path": "/private/capture.png", "mimeType": "image/png", "width": 1}},
            }},
        ],
    }
    result = sanitize_tunnel_result(raw)
    assert result["results"][0]["error"]["recovery"] == semantic
    error = result["results"][1]["error"]
    assert error["code"] == "not_ready"
    assert error["uncertain"] is True
    assert error["action_executed"] is False
    assert error["recovery"]["requires_local_setup"] is True
    assert error["observation"]["image"] == {"mimeType": "image/png", "width": 1}
    assert raw["results"][1]["error"]["recovery"]["next_tool"] == "pua_setup"
