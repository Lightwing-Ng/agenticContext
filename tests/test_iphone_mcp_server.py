"""Ported MCP server tests: stdio protocol, tool catalog, preview tools and READY recovery.

Ported from iPhone Use 0.3.10 tests/test_protocol.py, tests/test_screen_protocol.py,
tests/test_tool_loading.py, tests/test_recovery.py and tests/test_readiness_cli.py
(MIT, Copyright (c) 2026 ZHONG XIN); see app/core/iphone/LICENSE-iphone-use.txt.
The MCP-Apps widget, analytics and Codex packaging cases were dropped or adapted:
agenticContext serves the live preview at /iphone and sends no telemetry.

Code version: v1.0.1-codex.0
"""
# ruff: noqa: E701
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pytest

fcntl = pytest.importorskip("fcntl")

from app.core.iphone import mcp_server  # noqa: E402
from app.core.iphone.mcp_server import (  # noqa: E402
    BATCH_OPS,
    INSTRUCTIONS,
    LISTED_TOOLS,
    SCHEMAS,
    SCREEN_PAGE_URL,
    TOOLS,
    Runtime,
    result_content,
    serve,
    validate,
    validate_semantics,
)
from app.core.iphone.wda_client import WDAError  # noqa: E402
from app.core.iphone.wda_controller import PhoneController  # noqa: E402
from tests.iphone_wda_fakes import FakeWDA  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "app" / "core" / "iphone"


def payload(result):
    """The model-facing data: one JSON text block, never a second structured copy."""
    assert "structuredContent" not in result, "structuredContent would hide content blocks from the model"
    assert result["content"][0]["type"] == "text"
    return json.loads(result["content"][0]["text"])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def exchange(self, requests, url=None):
        payload = "".join(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n" if not isinstance(item, str) else item + "\n" for item in requests)
        command = [sys.executable, "-m", "app.core.iphone.mcp_server", "--state-dir", self.directory.name]
        if url:
            command.extend(["--url", url])
        process = subprocess.run(
            command,
            input=payload, capture_output=True, text=True, encoding="utf-8", timeout=10, cwd=PROJECT_ROOT)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        return [json.loads(line) for line in process.stdout.splitlines()]

    def runtime(self):
        runtime = Runtime(self.directory.name)
        self.addCleanup(runtime.close)
        client = FakeWDA()
        runtime.client = client
        # Restore a close method for Runtime cleanup; fake transports have no sockets.
        client.close = lambda: None
        runtime.phone = PhoneController(client, self.directory.name)
        return runtime, client

    def serve_ready(self, runtime, arguments):
        request = {"jsonrpc": "2.0", "id": "ready-state", "method": "tools/call", "params": {
            "name": "pua_ready", "arguments": arguments}}
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), contextlib.redirect_stdout(output):
            serve(runtime)
        response = json.loads(output.getvalue())
        self.assertEqual(response["id"], "ready-state")
        self.assertNotIn("error", response)
        result = response["result"]
        # Compact separators: no padding bytes in the model context.
        self.assertNotIn('": ', result["content"][0]["text"])
        payload(result)
        return result

    def test_ready_diagnostic_and_pending_states_are_normal_mcp_results(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                runtime, client = self.runtime()
                manager = Mock()
                manager.mirroring_running.return_value = False
                manager.pending_recovery.return_value = {"job_id": "existing-recovery", "state": "restarting"} if pending else None
                runtime.setup_manager = manager
                client.app = "local.pid.0"
                result = self.serve_ready(runtime, {"screenshot": False, "recover": False})
                self.assertFalse(result["isError"])
                data = payload(result)
                self.assertFalse(data["ready"])
                self.assertEqual(data["state"], "recovering" if pending else "recovery_required")
                self.assertNotIn("error", data)
                self.assertNotIn("proof", data)
                self.assertNotIn("observation", data)
                self.assertEqual([item["type"] for item in result["content"]], ["text"])
                manager.recover.assert_not_called()
                self.assertEqual([path for _, path, _ in client.actions() if path != "/session"], [])

    def test_queued_ready_recovery_is_normal_mcp_progress(self):
        runtime, client = self.runtime()
        manager = Mock()
        manager.pending_recovery.return_value = None
        manager.recover.return_value = {"ok": True, "job_id": "queued-recovery", "recovery": {"state": "queued"}}
        runtime.setup_manager = manager
        client.app = "local.pid.0"
        result = self.serve_ready(runtime, {"screenshot": False, "recover": True})
        self.assertFalse(result["isError"])
        data = payload(result)
        self.assertFalse(data["ready"])
        self.assertEqual(data["state"], "recovering")
        self.assertEqual(data["recovery"]["status_arguments"], {"action": "status", "job_id": "queued-recovery"})
        self.assertEqual(data["recovery"]["next_arguments"], {"screenshot": False, "recover": True})
        self.assertNotIn("proof", data)
        self.assertEqual([item["type"] for item in result["content"]], ["text"])
        manager.recover.assert_called_once_with()

    def test_ready_refusal_lock_and_unknown_fault_remain_mcp_errors(self):
        for case, expected in (("owner", "pua_recovery_required"), ("locked", "phone_locked"), ("unknown", "device-fault")):
            with self.subTest(case=case):
                runtime, client = self.runtime()
                manager = Mock()
                manager.pending_recovery.return_value = None
                manager.recover.return_value = {"ok": False, "error": "Service ownership could not be proven"}
                runtime.setup_manager = manager
                if case == "owner":
                    client.app = "local.pid.0"
                elif case == "locked":
                    client.locked = True
                else:
                    client.source = Mock(side_effect=WDAError("device-fault", "Unexpected channel fault"))
                result = self.serve_ready(runtime, {"screenshot": False})
                self.assertTrue(result["isError"])
                self.assertEqual(payload(result)["error"]["code"], expected)
                self.assertEqual([path for _, path, _ in client.actions() if path != "/session"], [])

    def test_ready_success_keeps_mcp_observation_and_proof(self):
        runtime, client = self.runtime()
        with patch.object(runtime.setup_manager, "mirroring_running", return_value=False):
            result = self.serve_ready(runtime, {"screenshot": False, "recover": False})
        self.assertFalse(result["isError"])
        data = payload(result)
        self.assertTrue(data["ready"])
        self.assertEqual(data["state"], "ready")
        self.assertTrue(data["proof"]["foreground_resolved"])
        self.assertGreater(data["observation"]["total_nodes"], 0)

    def test_stdio_initialization_notifications_ping_and_tool_catalog(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "ping", "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        ])
        self.assertEqual([response["id"] for response in responses], [1, "ping", 3])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[0]["result"]["serverInfo"], {"name": "agenticcontext-iphone", "version": "0.3.10"})
        self.assertEqual(responses[1]["result"], {})
        tools = responses[2]["result"]["tools"]
        names = [tool["name"] for tool in tools]
        # The preview tools stay callable in-process but are never published to MCP clients.
        self.assertEqual(len(names), 17)
        self.assertNotIn("pua_screen_frame", names)
        self.assertNotIn("pua_screen_action", names)
        self.assertEqual(len(names), len(set(names)))
        for name in ("pua_ready", "pua_setup", "pua_observe", "pua_tap", "pua_type_text", "pua_batch", "pua_collect_list", "pua_apps"):
            self.assertIn(name, names)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        apps = next(tool for tool in tools if tool["name"] == "pua_apps")
        self.assertTrue(apps["annotations"]["readOnlyHint"])
        self.assertFalse(apps["annotations"]["destructiveHint"])
        self.assertTrue(apps["annotations"]["idempotentHint"])
        self.assertEqual(apps["inputSchema"]["required"], ["query"])

    def test_action_schema_exposes_optimistic_defaults_and_explicit_checkpoints(self):
        response = self.exchange([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])[0]
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        def resolved(schema, prop):
            value = schema["properties"][prop]
            return schema["$defs"][value["$ref"].split("/")[-1]] if "$ref" in value else value
        for name in ("pua_tap", "pua_swipe", "pua_type_text", "pua_press_button", "pua_launch_app"):
            with self.subTest(name=name):
                schema = tools[name]["inputSchema"]
                self.assertEqual(resolved(schema, "observe")["default"], "none")
                self.assertEqual(set(resolved(schema, "expect")["properties"]), set(resolved(tools["pua_find"]["inputSchema"], "selector")["properties"]))
                if name != "pua_tap":
                    self.assertFalse(schema["properties"]["verify"]["default"])
        for name in ("pua_tap", "pua_swipe"):
            self.assertNotIn("observation_id", tools[name]["inputSchema"].get("required", []))
        batch_steps = tools["pua_batch"]["inputSchema"]["properties"]["steps"]["items"]["oneOf"]
        def contract(value, definitions):
            if isinstance(value, dict):
                if "$ref" in value:return contract(definitions[value["$ref"].split("/")[-1]], definitions)
                return {k:contract(v, definitions) for k,v in value.items() if k not in ("description", "examples", "$defs")}
            if isinstance(value, list):return [contract(v, definitions) for v in value]
            return value
        for step in batch_steps:
            op = step["properties"]["op"]["const"]
            standalone = tools["pua_" + op]["inputSchema"]
            self.assertEqual(contract(step["properties"]["args"], tools["pua_batch"]["inputSchema"]["$defs"]),
                             contract(standalone, standalone.get("$defs", {})))

    def test_stdio_apps_catalog_returns_public_evidence_without_wda_access(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "pua_apps", "arguments": {"query": "招商银行", "source": "catalog"}}},
        ], url="http://127.0.0.1:1")
        result = responses[0]["result"]
        self.assertFalse(result["isError"])
        data = payload(result)
        self.assertTrue(data["ok"])
        self.assertEqual(data["searched_sources"], ["catalog"])
        self.assertEqual(data["candidates"][0]["bundle_id"], "com.cmbchina.MPBBank")
        self.assertFalse(data["candidates"][0]["installed_verified"])

    def test_apps_catalog_is_read_only_even_when_phone_is_locked(self):
        runtime, client = self.runtime()
        client.locked = True
        result = runtime.call("pua_apps", {"query": "招商银行", "source": "catalog"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidates"][0]["bundle_id"], "com.cmbchina.MPBBank")
        self.assertEqual(client.calls, [])

    def test_stdio_invalid_arguments_return_tool_error_without_wda_access(self):
        invalid_cases = [
            ("pua_tap", {"x": True, "y": 200, "observation_id": "unused"}),
            ("pua_type_text", {"selector": {"label": "Target"}, "text": "line 1\nline 2"}),
            ("pua_find", {"selector": {"label": "Target", "predicate": "label == 'Target'"}}),
            ("pua_batch", {"steps": [{"op": "tap", "args": {"selector": {"label": "Target"}}}, {"op": "unknown", "args": {}}]}),
            ("wda_missing", {}),
            ("pua_apps", {"query": "招商银行", "source": "guess"}),
            ("pua_apps", {"query": "招商银行", "country": "CHN"}),
            ("pua_apps", {"query": ""}),
        ]
        responses = self.exchange([{"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": name, "arguments": args}} for index, (name, args) in enumerate(invalid_cases)])
        self.assertEqual(len(responses), len(invalid_cases))
        for response in responses:
            result = response["result"]
            self.assertTrue(result["isError"])
            content = payload(result)
            self.assertNotIn(content["error"]["code"], ("pua_unreachable", "action_uncertain", "internal_error"))

    def test_stdio_parse_error_does_not_break_following_ping(self):
        responses = self.exchange(["{broken", {"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"], {})

    def test_default_swipe_executes_once_without_progress_or_output_reads(self):
        runtime, client = self.runtime()
        result = runtime.call("pua_swipe", {"direction": "up"})
        self.assertTrue(result["action_executed"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(result["attempts"], 1)
        self.assertNotIn("observation", result)
        self.assertEqual(client.swipe_count, 1)
        self.assertFalse(any(path.startswith("/source") or path == "/screenshot" for _, path, _ in client.calls))

    def test_swipe_schema_accepts_explicit_progress_verification_without_output(self):
        runtime, client = self.runtime()
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_swipe", {"direction": "up", "observe": "none", "verify": True})
        self.assertEqual(caught.exception.code, "no_scroll_progress")
        self.assertEqual(caught.exception.details["attempts"], 1)
        self.assertTrue(caught.exception.details["action_executed"])
        self.assertEqual(client.swipe_count, 1)
        self.assertIn("image", caught.exception.details["observation"])

    def test_unknown_argument_reports_allowed_fields_without_device_access(self):
        runtime, client = self.runtime()
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_observe", {"observe": "none"})
        error = caught.exception.as_dict()
        self.assertEqual(error["code"], "invalid_argument")
        self.assertFalse(error["action_executed"])
        self.assertEqual(error["unknown_fields"], ["observe"])
        self.assertIn("mode", error["allowed_fields"])
        self.assertNotIn("observe", error["allowed_fields"])
        self.assertEqual(error["argument_path"], "arguments")
        self.assertEqual(client.calls, [])

    def test_no_progress_error_image_is_mcp_image_content_and_retains_details(self):
        runtime, client = self.runtime()
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_swipe", {"observe": "screenshot", "max_attempts": 1, "verify": True})
        result = result_content({"error": caught.exception.as_dict()})
        self.assertTrue(result["isError"])
        self.assertEqual(payload(result)["error"]["code"], "no_scroll_progress")
        self.assertTrue(payload(result)["error"]["action_executed"])
        self.assertEqual([item["type"] for item in result["content"]], ["text", "image"])
        self.assertEqual(result["content"][1]["mimeType"], "image/png")
        import base64
        self.assertEqual(base64.b64decode(result["content"][1]["data"]), client.screenshot)

    def test_scroll_search_returns_model_image_before_a_second_gesture(self):
        runtime, client = self.runtime()
        client.elements.clear()
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_scroll_find", {"selector": {"label": "Missing"}, "max_swipes": 10})
        result = result_content({"error": caught.exception.as_dict()})
        self.assertTrue(result["isError"])
        self.assertEqual(payload(result)["error"]["swipes"], 1)
        self.assertTrue(payload(result)["error"]["recovery"]["visual_check_required"])
        self.assertEqual([block["type"] for block in result["content"]], ["text", "image"])
        self.assertEqual(client.swipe_count, 1)

    def test_batch_continues_optimistic_navigation_and_input_without_readback(self):
        runtime, client = self.runtime()
        text = "直接输入完整的长文本，不做试输入。" * 20
        result = runtime.call("pua_batch", {"steps": [
            {"op": "press_button", "args": {"name": "home"}},
            {"op": "launch_app", "args": {"bundle_id": "com.example.phone"}},
            {"op": "tap", "args": {"selector": {"label": "Target"}}},
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": text}},
            {"op": "swipe", "args": {}},
        ]})
        self.assertTrue(result["complete"])
        self.assertEqual(result["completed_steps"], 5)
        self.assertTrue(all(item["action_executed"] for item in result["results"]))
        self.assertTrue(all(item["verification_deferred"] for item in result["results"]))
        self.assertTrue(all(not item["verified"] for item in result["results"]))
        self.assertEqual(client.elements[0]["value"], text)
        self.assertEqual(client.swipe_count, 1)
        self.assertFalse(any(path.startswith("/source") or path in ("/screenshot", "/wda/activeAppInfo")
                             or path.endswith("/attribute/value") for _, path, _ in client.calls))

    def test_batch_explicit_progress_check_stops_after_no_progress(self):
        runtime, client = self.runtime()
        result = runtime.call("pua_batch", {"steps": [
            {"op": "swipe", "args": {"observe": "none", "verify": True}},
            {"op": "press_button", "args": {"name": "home", "observe": "none"}},
        ]})
        self.assertFalse(result["complete"])
        self.assertEqual(result["error"]["code"], "no_scroll_progress")
        self.assertTrue(result["error"]["action_executed"])
        self.assertEqual(client.swipe_count, 1)
        self.assertEqual([block["type"] for block in result_content(result)["content"]], ["text", "image"])
        self.assertFalse(any(path == "/wda/homescreen" for _, path, _ in client.actions()))

    def test_batch_stops_after_uncertain_action_and_never_replays_it(self):
        runtime, client = self.runtime()
        client.home_error = WDAError("action_uncertain", "Home response was lost", uncertain=True)
        result = runtime.call("pua_batch", {"steps": [
            {"op": "press_button", "args": {"name": "home"}},
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Do not enter"}},
        ]})
        self.assertFalse(result["complete"])
        self.assertEqual(result["completed_steps"], 0)
        self.assertEqual(result["error"]["code"], "action_uncertain")
        self.assertTrue(result["error"]["uncertain"])
        self.assertEqual([path for _, path, _ in client.actions()], ["/wda/homescreen"])

    def test_batch_stops_after_submission_until_a_deliberate_checkpoint(self):
        runtime, client = self.runtime()
        result = runtime.call("pua_batch", {"steps": [
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Submit once", "submit": True}},
            {"op": "press_button", "args": {"name": "home"}},
        ]})
        self.assertFalse(result["complete"])
        self.assertEqual(result["completed_steps"], 1)
        self.assertEqual(result["stop_reason"], "submission_requires_verification")
        self.assertTrue(result["results"][0]["submitted"])
        self.assertEqual(sum(path == "/wda/keys" for _, path, _ in client.actions()), 1)
        self.assertFalse(any(path == "/wda/homescreen" for _, path, _ in client.actions()))

    def test_home_and_text_accept_explicit_expected_next_state(self):
        for name, arguments in (
            ("pua_press_button", {"name": "home", "expect": {"label": "Target"}}),
            ("pua_type_text", {"selector": {"label": "Target"}, "text": "Complete text", "expect": {"label": "Target"}}),
        ):
            with self.subTest(name=name):
                runtime, _ = self.runtime()
                result = runtime.call(name, arguments)
                self.assertTrue(result["verified"])
                self.assertFalse(result["verification_deferred"])
                self.assertIn("postcondition", result)

    def test_exact_enabled_tree_string_survives_protocol_schema(self):
        runtime, client = self.runtime()
        result = runtime.call("pua_find", {"selector": {"label": "Target", "enabled": "true"}})
        self.assertEqual(result["matches"], 1)
        query = next(body["value"] for _, path, body in client.calls if path == "/elements")
        self.assertIn("enabled == true", query)

    def test_stdio_unknown_method_and_invalid_params_are_rpc_errors(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "not/a/method"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": []},
        ])
        self.assertEqual(responses[0]["error"]["code"], -32601)
        self.assertEqual(responses[1]["error"]["code"], -32602)

    def test_nested_batch_schema_is_checked_before_any_phone_request(self):
        runtime, client = self.runtime()
        steps = [
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Do not enter", "observe": "none"}},
            {"op": "tap", "args": {"selector": {"label": "Target"}, "extra": True}},
        ]
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_batch", {"steps": steps})
        self.assertEqual(caught.exception.code, "invalid_argument")
        details = caught.exception.as_dict()
        self.assertEqual(details["argument_path"], "arguments.steps[1].args")
        self.assertEqual(details["unknown_fields"], ["extra"])
        self.assertIn("observe", details["allowed_fields"])
        self.assertFalse(details["action_executed"])
        self.assertEqual(client.calls, [])

    def test_malformed_later_selector_is_checked_before_any_phone_request(self):
        runtime, client = self.runtime()
        steps = [
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Do not enter", "observe": "none"}},
            {"op": "tap", "args": {"selector": {"label": "Target", "predicate": "label == 'Target'"}, "observe": "none"}},
        ]
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_batch", {"steps": steps})
        self.assertEqual(caught.exception.code, "invalid_selector")
        self.assertEqual(client.calls, [])

    def test_later_invalid_bundle_id_is_checked_before_any_phone_request(self):
        runtime, client = self.runtime()
        steps = [
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Do not enter", "observe": "none"}},
            {"op": "launch_app", "args": {"bundle_id": "not-a-bundle", "observe": "none"}},
        ]
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_batch", {"steps": steps})
        self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual(client.calls, [])

    def test_coordinates_and_custom_region_accept_no_observation_id(self):
        for name, arguments, expected_path in (
            ("pua_tap", {"x": 10, "y": 200}, "/wda/tap"),
            ("pua_swipe", {"region": {"x": 50, "y": 200, "width": 200, "height": 200}}, "/wda/dragfromtoforduration"),
        ):
            with self.subTest(name=name):
                runtime, client = self.runtime()
                result = runtime.call(name, arguments)
                self.assertTrue(result["action_executed"])
                self.assertTrue(result["verification_deferred"])
                self.assertEqual(client.actions()[-1][1], expected_path)
                self.assertFalse(any(path.startswith("/source") or path == "/screenshot" for _, path, _ in client.calls))

    def test_incomplete_or_mixed_coordinates_fail_before_phone_access(self):
        runtime, client = self.runtime()
        invalid = [
            ("pua_tap", {"x": 10}),
            ("pua_tap", {"y": 200}),
            ("pua_tap", {"selector": {"label": "Target"}, "x": 10}),
        ]
        for name, arguments in invalid:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(WDAError) as caught:
                runtime.call(name, arguments)
            self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual(client.calls, [])

    def test_unbound_coordinates_and_region_still_check_viewport_before_mutation(self):
        for name, arguments in (
            ("pua_tap", {"x": 390, "y": 200}),
            ("pua_swipe", {"region": {"x": 300, "y": 200, "width": 200, "height": 200}}),
        ):
            with self.subTest(name=name):
                runtime, client = self.runtime()
                with self.assertRaises(WDAError) as caught:
                    runtime.call(name, arguments)
                self.assertEqual(caught.exception.code, "invalid_argument")
                self.assertFalse(caught.exception.as_dict().get("action_executed", False))
                self.assertEqual(client.actions(), [])

    def test_other_process_holding_operation_lock_refuses_all_phone_requests(self):
        runtime, client = self.runtime()
        script = "import fcntl,sys; lock=open(sys.argv[1], 'a'); fcntl.flock(lock, fcntl.LOCK_EX); print('locked', flush=True); sys.stdin.read(); lock.close()"
        worker = subprocess.Popen(
            [sys.executable, "-c", script, str(Path(self.directory.name) / "operation.lock")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(worker.stdout.readline().strip(), "locked")
            with self.assertRaises(WDAError) as caught:
                runtime.call("pua_tap", {"selector": {"label": "Target"}, "observe": "none"})
            self.assertEqual(caught.exception.code, "device_busy")
            self.assertEqual(client.calls, [])
        finally:
            try:
                worker.communicate(input="release", timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate()
        self.assertEqual(worker.returncode, 0)

    def test_independent_runtimes_share_usable_session_without_new_phone_request(self):
        first, first_client = self.runtime()
        second, second_client = self.runtime()
        first_client.session_id = "shared-session-123"
        with patch.object(first, "_call", side_effect=lambda name, args: {"session_id": first.client.session_id}):
            self.assertEqual(first.call("pua_metrics", {}), {"session_id": "shared-session-123"})
        self.assertIsNone(second_client.session_id)
        with patch.object(second, "_call", side_effect=lambda name, args: {"session_id": second.client.session_id}):
            self.assertEqual(second.call("pua_metrics", {}), {"session_id": "shared-session-123"})
        self.assertEqual(first_client.calls + second_client.calls, [])
        cache = Path(self.directory.name) / "session.json"
        self.assertEqual(json.loads(cache.read_text()), {"url": first.base_url, "session_id": "shared-session-123"})
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)

    def test_locked_phone_blocks_mutations_before_semantic_lookup(self):
        runtime, client = self.runtime()
        client.locked = True
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_tap", {"selector": {"label": "Target"}, "observe": "none"})
        self.assertEqual(caught.exception.code, "phone_locked")
        self.assertEqual(client.calls, [("GET", "/wda/locked", None)])
        self.assertEqual(client.actions(), [])

    def test_ready_requires_unlocked_phone_before_session_or_observation(self):
        runtime, client = self.runtime()
        client.locked = True
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_ready", {"screenshot": False})
        self.assertEqual(caught.exception.code, "phone_locked")
        self.assertEqual(client.calls, [("GET", "/status", None), ("GET", "/wda/locked", None)])
        self.assertIsNone(client.session_id)

    def test_ready_empty_tree_with_mirroring_running_reports_conflict(self):
        runtime, client = self.runtime()
        client.nodes = []
        with patch.object(runtime.setup_manager, "mirroring_running", return_value=True), self.assertRaises(WDAError) as caught:
            runtime.call("pua_ready", {"screenshot": False})
        self.assertEqual(caught.exception.code, "mirroring_conflict")
        self.assertFalse(any(path in ("/wda/tap", "/wda/keys") for _, path, _ in client.actions()))

    def test_ready_nonempty_tree_proves_unlocked_usable_phone(self):
        runtime, client = self.runtime()
        with patch.object(runtime.setup_manager, "mirroring_running", return_value=False):
            result = runtime.call("pua_ready", {"screenshot": False})
        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["proof"]["phone_unlocked"])
        self.assertTrue(result["proof"]["session_usable"])
        self.assertGreater(result["observation"]["total_nodes"], 0)


class MockScreen:
    """Memory-only preview double: never captures or connects to a phone."""
    def __init__(self):
        self.events = []
        self.is_paused = False
        self.pause_reason = None
        self.device = None
        self.closed = False
        self.frame_result = {"frame": {"seq": 7, "data": "preview-only",
                                      "mimeType": "image/jpeg", "width": 390, "height": 844},
                             "events": [], "busy": False, "paused": False}

    def frame(self, **args):
        self.events.append(("frame", args))
        return self.frame_result

    def start(self):
        self.events.append(("start", {}))
        return {"frame": None, "events": [], "busy": False, "paused": self.is_paused}

    def set_paused(self, paused, reason="authentication"):
        self.is_paused = paused
        self.pause_reason = reason if paused else None
        self.events.append(("pause", {"paused": paused}))

    def pause_status(self):
        return {"paused": self.is_paused, "pause_reason": self.pause_reason}

    def locked_pause_id(self):
        return "test-lock" if self.is_paused and self.pause_reason == "device_locked" else None

    def reconnect_pause_id(self):
        return "test-pause" if self.is_paused else None

    def resume_after_unlock(self, expected_id, explicit=False):
        if expected_id and (self.locked_pause_id() == expected_id or explicit):
            self.is_paused = False
            self.pause_reason = None
            return True
        return False

    def paused(self):
        return self.is_paused

    def restart(self):
        self.events.append(("restart", {}))
        return {"frame": None, "events": [], "busy": False, "paused": self.is_paused, "stream_id": "restarted"}

    def set_viewport(self, viewport):
        self.events.append(("viewport", dict(viewport)))

    def begin(self, op):
        self.events.append(("begin", {"op": op}))
        return "activity-token"

    def end(self, token):
        self.events.append(("end", {"token": token}))

    def gesture(self, kind, **args):
        self.events.append((kind, args))

    def close(self):
        self.closed = True


class ScreenProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def runtime(self):
        runtime = Runtime(self.directory.name)
        # Close the unused real preview before replacing it; no frame poll occurs.
        runtime.screen.close()
        client = FakeWDA()
        client.close = Mock()
        runtime.client = client
        runtime.phone = PhoneController(client, self.directory.name)
        screen = MockScreen()
        runtime.screen = screen
        runtime.phone.screen = screen
        self.addCleanup(runtime.close)
        return runtime, client, screen

    def exchange(self, requests):
        payload = "".join(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n"
                          for item in requests)
        result = subprocess.run([sys.executable, "-m", "app.core.iphone.mcp_server",
                                 "--state-dir", self.directory.name,
                                 "--url", "http://127.0.0.1:1"],
                                input=payload, capture_output=True, text=True,
                                encoding="utf-8", timeout=10, cwd=PROJECT_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return [json.loads(line) for line in result.stdout.splitlines()]

    def call_over_stdio(self, runtime, name, arguments):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), contextlib.redirect_stdout(output):
            serve(runtime)
        return json.loads(output.getvalue())["result"]

    def test_catalog_publishes_model_tools_without_widget_metadata(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertIn("resources", responses[0]["result"]["capabilities"])
        self.assertIn(SCREEN_PAGE_URL, responses[0]["result"]["instructions"])
        tools = {item["name"]: item for item in responses[1]["result"]["tools"]}
        self.assertEqual(len(tools), 17)
        self.assertEqual(set(tools), {item["name"] for item in TOOLS} - {"pua_screen_frame", "pua_screen_action"})
        for tool in tools.values():
            self.assertNotIn("_meta", tool)
        self.assertIn(SCREEN_PAGE_URL, tools["pua_screen"]["description"])
        self.assertEqual(tools["pua_screen"]["title"], "iPhone screen")
        self.assertFalse(tools["pua_screen"]["annotations"]["destructiveHint"])
        catalog = {item["name"]: item for item in TOOLS}
        self.assertEqual(catalog["pua_screen_action"]["inputSchema"]["properties"]["action"]["enum"], ["refresh", "home", "screenshot"])
        self.assertFalse(catalog["pua_screen_action"]["annotations"]["destructiveHint"])
        self.assertTrue(catalog["pua_screen_frame"]["annotations"]["readOnlyHint"])

    def test_stdio_lists_no_resources_and_refuses_the_removed_widget(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "ui://iphone-use/phone-0.3.10.html"}},
        ])
        self.assertEqual(responses[0]["result"], {"resources": []})
        self.assertEqual(responses[1]["error"]["code"], -32602)
        self.assertNotIn("result", responses[1])

    def test_invalid_resource_uri_cannot_read_files_and_following_ping_survives(self):
        invalid = ["file:///etc/passwd", "ui://iphone-use/../config.json", "ui://iphone-use/phone-0.3.10.html", "", None, [], {}]
        requests = [{"jsonrpc": "2.0", "id": index, "method": "resources/read", "params": {"uri": uri}}
                    for index, uri in enumerate(invalid)]
        requests.append({"jsonrpc": "2.0", "id": "still-alive", "method": "ping"})
        responses = self.exchange(requests)
        for response in responses[:-1]:
            self.assertEqual(response["error"]["code"], -32602)
            self.assertNotIn("result", response)
        self.assertEqual(responses[-1], {"jsonrpc": "2.0", "id": "still-alive", "result": {}})

    def test_frame_poll_bypasses_phone_operation_lock_session_and_http(self):
        runtime, client, screen = self.runtime()
        with (Path(self.directory.name) / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = runtime.call("pua_screen_frame", {"after_seq": 6, "last_event_id": 2})
        self.assertIs(result, screen.frame_result)
        self.assertEqual(screen.events, [("frame", {"after_seq": 6, "last_event_id": 2})])
        self.assertEqual(client.calls, [])
        self.assertIsNone(client.session_id)
        self.assertFalse((Path(self.directory.name) / "session.json").exists())

    def test_frame_payload_is_structured_only_without_image_or_text_tokens(self):
        runtime, client, screen = self.runtime()
        result = self.call_over_stdio(runtime, "pua_screen_frame", {"after_seq": 0})
        self.assertEqual(result["content"], [])
        self.assertIs(result["isError"], False)
        self.assertEqual(result["structuredContent"], screen.frame_result)
        self.assertEqual(client.calls, [])

    def test_open_pause_resume_bypass_operation_lock_and_never_control_phone(self):
        runtime, client, screen = self.runtime()
        with (Path(self.directory.name) / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runtime.call("pua_screen", {})
            paused = runtime.call("pua_screen", {"action": "pause"})
            resumed = runtime.call("pua_screen", {"action": "resume"})
        self.assertTrue(paused["paused"])
        self.assertFalse(resumed["paused"])
        self.assertEqual(client.calls, [])
        self.assertEqual([kind for kind, _ in screen.events], ["start", "pause", "start", "pause", "start"])

    def test_ready_open_and_authentication_calls_carry_no_widget_metadata(self):
        runtime, client, screen = self.runtime()
        results = []
        with patch.object(runtime.setup_manager, "mirroring_running", return_value=False):
            for name, arguments in (("pua_ready", {"screenshot": False, "recover": False}),
                                    ("pua_screen", {}), ("pua_screen", {"action": "pause"}),
                                    ("pua_ready", {"screenshot": False, "recover": False}),
                                    ("pua_screen", {"action": "resume"})):
                results.append(self.call_over_stdio(runtime, name, arguments))
        # The agenticContext page is the viewer, so no host widget session is announced.
        for result in results:
            self.assertNotIn("_meta", result)
        self.assertTrue(json.loads(results[0]["content"][0]["text"])["ready"])
        self.assertTrue(results[2]["structuredContent"]["paused"])
        self.assertTrue(json.loads(results[3]["content"][0]["text"])["ready"])
        self.assertFalse(results[4]["structuredContent"]["paused"])
        self.assertNotIn("structuredContent", results[0])
        self.assertFalse(any(path == "/screenshot" for _, path, _ in client.calls))
        self.assertEqual([path for _, path, _ in client.actions()], ["/session"])

    def test_screen_state_survives_repeated_opens_and_mcp_process_reconnect(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "pua_screen", "arguments": {"action": "pause"}}}
        first, repeated = self.exchange([request, {**request, "id": 2, "params": {"name": "pua_screen", "arguments": {}}}])
        reconnected, = self.exchange([{**request, "params": {"name": "pua_screen", "arguments": {}}}])
        results = [reply["result"] for reply in (first, repeated, reconnected)]
        self.assertTrue(all(result["isError"] is False for result in results))
        self.assertTrue(all("_meta" not in result for result in results))
        # Pause state is shared through the state directory, not process memory.
        self.assertTrue(all(result["structuredContent"]["paused"] for result in results))
        self.assertEqual(reconnected["result"]["structuredContent"]["pause_reason"], "authentication")

    def test_setup_status_and_failed_ready_do_not_create_a_widget_session(self):
        runtime, client, screen = self.runtime()
        with patch.object(runtime.setup_manager, "setup", return_value={"configured": True}):
            status = self.call_over_stdio(runtime, "pua_setup", {"action": "status"})
        client.locked = True
        failed = self.call_over_stdio(runtime, "pua_ready", {"screenshot": False, "recover": False})
        self.assertNotIn("_meta", status)
        self.assertNotIn("_meta", failed)
        self.assertTrue(failed["isError"])
        self.assertEqual(json.loads(failed["content"][0]["text"])["error"]["code"], "phone_locked")
        self.assertTrue(screen.is_paused)
        self.assertEqual(client.actions(), [])

    def test_invalid_preview_arguments_rejected_before_backend_or_phone(self):
        cases = [("pua_screen", {"action": "click"}), ("pua_screen", {"x": 100}),
                 ("pua_screen_frame", {"after_seq": True}), ("pua_screen_frame", {"after_seq": -1}),
                 ("pua_screen_frame", {"last_event_id": 1.5}), ("pua_screen_frame", {"observe": "tree"})]
        runtime, client, screen = self.runtime()
        for name, args in cases:
            with self.subTest(name=name, args=args), self.assertRaises(WDAError) as caught:
                runtime.call(name, args)
            self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual(screen.events, [])
        self.assertEqual(client.calls, [])

    def test_ready_opens_preview_by_default_without_a_frame_poll_or_added_screenshot(self):
        runtime, client, screen = self.runtime()
        with patch.object(runtime.setup_manager, "mirroring_running", return_value=False):
            result = runtime.call("pua_ready", {"screenshot": False, "recover": False})
        self.assertTrue(result["ready"])
        self.assertEqual(sum(kind == "start" for kind, _ in screen.events), 1)
        self.assertFalse(any(kind == "frame" for kind, _ in screen.events))
        self.assertFalse(any(path == "/screenshot" for _, path, _ in client.calls))
        self.assertEqual(screen.events[0], ("begin", {"op": "ready"}))
        self.assertEqual(screen.events[-1], ("end", {"token": "activity-token"}))

    def test_coordinate_tap_emits_actual_point_before_single_action_without_extra_reads(self):
        runtime, client, screen = self.runtime()
        original = client.session
        at_action = []
        def request(method, path, payload=None, timeout=None):
            if path == "/wda/tap":
                at_action.extend(screen.events)
            return original(method, path, payload, timeout)
        client.session = request
        result = runtime.call("pua_tap", {"x": 120, "y": 240})
        self.assertTrue(result["action_executed"])
        self.assertEqual([path for _, path, _ in client.calls], ["/wda/locked", "/window/size", "/wda/tap"])
        self.assertEqual([event for event in at_action if event[0] == "tap"],
                         [("tap", {"point": {"x": 120, "y": 240}, "viewport": client.size})])
        self.assertEqual(screen.events[0], ("begin", {"op": "tap"}))
        self.assertEqual(screen.events[-1], ("end", {"token": "activity-token"}))

    def test_semantic_tap_cursor_uses_validated_rect_center(self):
        runtime, client, screen = self.runtime()
        result = runtime.call("pua_tap", {"selector": {"label": "Target"}})
        self.assertTrue(result["action_executed"])
        self.assertEqual([event for event in screen.events if event[0] == "tap"],
                         [("tap", {"point": {"x": 195, "y": 222}, "viewport": client.size})])
        self.assertEqual([path for _, path, _ in client.actions()], ["/element/target/click"])
        self.assertFalse(any(path.startswith("/source") or path == "/screenshot" for _, path, _ in client.calls))

    def test_swipe_cursor_tracks_the_executed_drag_endpoints(self):
        runtime, client, screen = self.runtime()
        result = runtime.call("pua_swipe", {"direction": "up", "region": {"x": 40, "y": 100, "width": 300, "height": 600}})
        self.assertTrue(result["action_executed"])
        action = next(body for method, path, body in client.calls if method == "POST" and path == "/wda/dragfromtoforduration")
        events = [args for kind, args in screen.events if kind == "drag"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from_point"], {"x": action["fromX"], "y": action["fromY"]})
        self.assertEqual(events[0]["to_point"], {"x": action["toX"], "y": action["toY"]})
        self.assertEqual(events[0]["viewport"], client.size)
        self.assertEqual(client.swipe_count, 1)
        self.assertEqual([path for _, path, _ in client.calls], ["/wda/locked", "/window/size", "/wda/dragfromtoforduration"])

    def test_preview_telemetry_never_contains_input_text_or_selector(self):
        runtime, client, screen = self.runtime()
        secret = "Long private text 绝不传给预览 " * 15
        client.elements[0]["label"] = "Private editor name"
        result = runtime.call("pua_type_text", {"selector": {"label": "Private editor name"}, "text": secret})
        self.assertTrue(result["action_executed"])
        encoded = json.dumps(screen.events, ensure_ascii=False)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("Private editor name", encoded)
        self.assertNotIn("selector", encoded)
        self.assertEqual([kind for kind, _ in screen.events], ["begin", "viewport", "tap", "end"])
        self.assertEqual(client.elements[0]["value"], secret)

    def test_authentication_takeover_errors_pause_preview_without_input(self):
        for secure_field in (False, True):
            with self.subTest(secure_field=secure_field):
                runtime, client, screen = self.runtime()
                if secure_field:
                    client.elements[0]["kind"] = "XCUIElementTypeSecureTextField"
                    name, args, code = "pua_type_text", {"selector": {"label": "Target"}, "text": "never typed"}, "not_editable"
                else:
                    client.locked = True
                    name, args, code = "pua_tap", {"x": 100, "y": 220}, "phone_locked"
                with self.assertRaises(WDAError) as caught:
                    runtime.call(name, args)
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(screen.is_paused)
                self.assertEqual(client.actions(), [])
                self.assertEqual(screen.events[-1], ("end", {"token": "activity-token"}))

    def test_preview_telemetry_failure_cannot_change_or_repeat_a_phone_action(self):
        runtime, client, screen = self.runtime()
        for method in ("begin", "end", "gesture", "set_viewport"):
            setattr(screen, method, Mock(side_effect=OSError("display unavailable")))
        result = runtime.call("pua_tap", {"x": 120, "y": 240})
        self.assertTrue(result["action_executed"])
        self.assertEqual([path for _, path, _ in client.calls], ["/wda/locked", "/window/size", "/wda/tap"])
        self.assertEqual([path for _, path, _ in client.actions()], ["/wda/tap"])

    def test_ready_preview_failure_cannot_break_channel_readiness(self):
        runtime, client, screen = self.runtime()
        screen.start=Mock(side_effect=OSError("display unavailable"))
        with patch.object(runtime.setup_manager,"mirroring_running",return_value=False):
            self.assertTrue(runtime.call("pua_ready",{"screenshot":False,"recover":False})["ready"])

    def test_nonsecure_noneditable_target_does_not_pause_the_preview(self):
        runtime, client, screen = self.runtime()
        client.elements[0]["kind"]="XCUIElementTypeButton"
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_type_text",{"selector":{"label":"Target"},"text":"never typed"})
        self.assertEqual(caught.exception.code,"not_editable")
        self.assertFalse(screen.is_paused)
        self.assertEqual(client.actions(),[])

    def toolbar(self, runtime):
        """Route the toolbar's own short-lived connection to a synthetic phone."""
        phone = FakeWDA()
        phone.close = Mock()
        patcher = patch("app.core.iphone.mcp_server.WDAClient", return_value=phone)
        patcher.start()
        self.addCleanup(patcher.stop)
        return phone

    def test_toolbar_home_is_one_sessionless_request_under_the_operation_lock(self):
        runtime, client, screen = self.runtime()
        phone = self.toolbar(runtime)
        runtime.phone.pending_input = {"token": "t"}
        before = runtime.phone.accepted_actions
        result = runtime.call("pua_screen_action", {"action": "home"})
        self.assertEqual(result, {"ok": True, "action": "home"})
        self.assertEqual(phone.calls, [("POST", "/wda/homescreen", {})])
        phone.close.assert_called_once_with()
        # The model's own connection and session are untouched; its stale focus is forgotten.
        self.assertEqual(client.calls, [])
        self.assertIsNone(client.session_id)
        self.assertEqual(runtime.phone.accepted_actions, before + 1)
        self.assertIsNone(runtime.phone.pending_input)

    def test_toolbar_home_reports_busy_instead_of_interleaving_with_a_running_tool(self):
        runtime, client, screen = self.runtime()
        phone = self.toolbar(runtime)
        with (Path(self.directory.name) / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(WDAError) as caught:
                runtime.call("pua_screen_action", {"action": "home"})
            # Refreshing the preview never needs the phone lock.
            self.assertTrue(runtime.call("pua_screen_action", {"action": "refresh"})["ok"])
        self.assertEqual(caught.exception.code, "device_busy")
        self.assertFalse(caught.exception.details["action_executed"])
        self.assertEqual(phone.actions(), [])

    def test_toolbar_refresh_restarts_only_the_preview_and_reports_service_state(self):
        runtime, client, screen = self.runtime()
        phone = self.toolbar(runtime)
        result = runtime.call("pua_screen_action", {"action": "refresh"})
        self.assertEqual((result["ok"], result["service_ready"], result["stream_id"]), (True, True, "restarted"))
        self.assertEqual([kind for kind, _ in screen.events], ["restart"])
        self.assertEqual(phone.calls, [("GET", "/status", None), ("GET", "/wda/locked", None)])
        self.assertEqual(client.calls, [])
        phone.status_ready = False
        self.assertFalse(runtime.call("pua_screen_action", {"action": "refresh"})["service_ready"])

    def test_toolbar_screenshot_copies_the_native_capture_and_leaves_no_file(self):
        runtime, client, screen = self.runtime()
        phone = self.toolbar(runtime)
        copied = []

        def run(command, **options):
            copied.append((command, Path(command[-1]).read_bytes(), Path(command[-1]).stat().st_mode & 0o777))
            return Mock(returncode=0)

        with patch("app.core.iphone.mcp_server.subprocess.run", side_effect=run):
            result = runtime.call("pua_screen_action", {"action": "screenshot"})
        self.assertEqual(result, {"ok": True, "action": "screenshot", "copied": True, "width": 1, "height": 1})
        command, data, mode = copied[0]
        self.assertEqual((data, mode), (phone.screenshot, 0o600))
        self.assertEqual(command[0], "/usr/bin/osascript")
        # The file path is an argument of the script, not part of its text.
        self.assertFalse(any(self.directory.name in part for part in command[:-1]))
        self.assertFalse(Path(command[-1]).exists())
        self.assertEqual(phone.calls, [("GET", "/screenshot", None)])
        self.assertFalse(list((Path(self.directory.name)).glob("**/*.png")))
        with patch("app.core.iphone.mcp_server.subprocess.run", return_value=Mock(returncode=1)), self.assertRaises(WDAError) as caught:
            runtime.call("pua_screen_action", {"action": "screenshot"})
        self.assertEqual(caught.exception.code, "clipboard_unavailable")
        self.assertFalse(list((Path(self.directory.name)).glob(".clipboard-*")))

    def test_toolbar_blocks_actions_until_user_explicitly_reconnects(self):
        runtime, client, screen = self.runtime()
        phone = self.toolbar(runtime)
        screen.is_paused = True
        for action in ("home", "screenshot"):
            with self.subTest(action=action), self.assertRaises(WDAError) as caught:
                runtime.call("pua_screen_action", {"action": action})
            self.assertEqual(caught.exception.code, "preview_paused")
        self.assertEqual(phone.calls, [])
        self.assertFalse(runtime.call("pua_screen_action", {"action": "refresh"})["paused"])
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_screen_action", {"action": "tap"})
        self.assertEqual(caught.exception.code, "invalid_argument")

    def test_toolbar_results_are_structured_for_the_app_and_answered_off_the_tool_queue(self):
        runtime, client, screen = self.runtime()
        self.toolbar(runtime)
        result = self.call_over_stdio(runtime, "pua_screen_action", {"action": "home"})
        self.assertEqual(result["structuredContent"], {"ok": True, "action": "home"})
        screen.is_paused = True
        refused = self.call_over_stdio(runtime, "pua_screen_action", {"action": "home"})
        self.assertTrue(refused["isError"])
        self.assertEqual(refused["structuredContent"]["error"]["code"], "preview_paused")
        self.assertEqual(len(runtime.responses), 0)

    def test_header_gets_only_the_model_name_looked_up_once(self):
        runtime, client, screen = self.runtime()
        (Path(self.directory.name) / "config.json").write_text(json.dumps({"udid": "00008150-TESTTESTTEST", "team_id": "TEAM"}))
        found = {"ok": True, "devices": [{"udid": "other", "name": "Someone's iPad", "model": "iPad Air"},
                                         {"udid": "00008150-TESTTESTTEST", "name": "Private Phone Name", "model": "iPhone 17 Pro Max"}]}
        with patch.object(runtime.setup_manager, "discover", return_value=found) as discover:
            runtime.call("pua_screen", {})
            runtime._device_lookup.join(timeout=5)
            runtime.call("pua_screen_frame", {})
        self.assertEqual(screen.device, {"model": "iPhone 17 Pro Max"})
        discover.assert_called_once_with()
        cached = json.loads((Path(self.directory.name) / "device.json").read_text())
        self.assertEqual(cached, {"udid": "00008150-TESTTESTTEST", "model": "iPhone 17 Pro Max"})
        self.assertEqual((Path(self.directory.name) / "device.json").stat().st_mode & 0o777, 0o600)
        again, _, later = self.runtime()
        with patch.object(again.setup_manager, "discover") as discover:
            again.call("pua_screen", {})
            again._device_lookup.join(timeout=5)
        discover.assert_not_called()
        self.assertEqual(later.device, {"model": "iPhone 17 Pro Max"})

    def test_unconfigured_runtime_never_looks_for_a_device(self):
        runtime, client, screen = self.runtime()
        with patch.object(runtime.setup_manager, "discover") as discover:
            runtime.call("pua_screen_frame", {})
            runtime._device_lookup.join(timeout=5)
        discover.assert_not_called()
        self.assertIsNone(screen.device)

    def test_runtime_close_releases_preview_and_transport(self):
        runtime, client, screen = self.runtime()
        runtime.close()
        self.assertTrue(screen.closed)
        client.close.assert_called_once_with()


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def without_documentation(value):
    if isinstance(value, list):
        return [without_documentation(item) for item in value]
    if isinstance(value, dict):
        return {key: without_documentation(item) for key, item in value.items()
                if key not in ("description", "examples")}
    return value


def expand_references(schema):
    """Expand local references for contract comparison, rejecting broken/cyclic refs."""
    def expand(value, active=()):
        if isinstance(value, list):
            return [expand(item, active) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            reference = value["$ref"]
            if not reference.startswith("#/$defs/") or reference in active:
                raise AssertionError("Unsupported or cyclic schema reference: " + reference)
            target = schema
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            expanded = expand(target, (*active, reference))
            # This catalog uses pure references, so no sibling keyword can be lost.
            if set(value) != {"$ref"}:
                raise AssertionError("Reference has sibling constraints")
            return expanded
        return {key: expand(item, active) for key, item in value.items()
                if key != "$defs"}
    return expand(schema)


def host_supported_schema(value):
    """Project to the JsonSchema fields a compacting MCP host keeps, without expanding refs.

    The field set follows Codex 0.160.1, which keeps reachable $defs and $ref and only
    compacts once this serialized projection exceeds 5,000 bytes. This test protects
    the actual branch contract through that boundary, including const -> enum lowering.
    """
    supported = {"$ref", "type", "description", "encrypted", "enum", "items",
                 "minItems", "properties", "required", "additionalProperties",
                 "anyOf", "oneOf", "allOf", "$defs", "definitions"}
    result = {}
    for key, item in value.items():
        if key == "const":
            result["enum"] = [item]
        elif key in supported:
            if key in ("properties", "$defs", "definitions"):
                item = {name: host_supported_schema(child) for name, child in item.items()}
            elif key in ("anyOf", "oneOf", "allOf"):
                item = [host_supported_schema(child) for child in item]
            elif key == "items" or (key == "additionalProperties" and isinstance(item, dict)):
                item = host_supported_schema(item)
            result[key] = item
    return result


class ToolLoadingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {tool["name"]: tool for tool in TOOLS}
        self.batch = self.catalog["pua_batch"]["inputSchema"]

    def test_pua_names_are_consistent_across_the_published_contract_and_guidance(self):
        self.assertEqual(set(self.catalog), {"pua_" + name for name in SCHEMAS})
        self.assertNotIn("wda_", json.dumps(TOOLS) + INSTRUCTIONS)
        for name, tool in self.catalog.items():
            if name != "pua_screen":
                self.assertEqual(tool["title"], "Pua " + name[4:].replace("_", " "))
        public_names = set(self.catalog)
        for reference in re.findall(r"pua_[a-z_]+", INSTRUCTIONS):
            self.assertTrue(reference in public_names or reference in {
                "pua_unreachable", "pua_foreground_unavailable", "pua_recovery_required"
            }, reference)

    def test_every_published_schema_fits_before_host_compaction(self):
        for name, tool in self.catalog.items():
            with self.subTest(tool=name):
                self.assertLess(json_bytes(tool["inputSchema"]), 5000)
                self.assertLess(json_bytes(host_supported_schema(tool["inputSchema"])), 5000)

    def test_every_published_schema_is_the_runtime_contract_minus_documentation(self):
        for name, tool in self.catalog.items():
            with self.subTest(tool=name):
                published = tool["inputSchema"]
                self.assertEqual(without_documentation(expand_references(published)),
                                 without_documentation(SCHEMAS[name[len("pua_"):]]))
                self.assertFalse(published["additionalProperties"])

    def test_selector_fields_are_documented_once_and_named_everywhere(self):
        reference = self.catalog["pua_find"]["inputSchema"]["properties"]["selector"]
        self.assertTrue(all("description" in field for field in reference["properties"].values()))
        self.assertEqual(set(reference["properties"]),
                         {"label", "label_contains", "name", "value", "type", "enabled", "index", "predicate"})
        documented = 0
        for name, tool in self.catalog.items():
            schema = tool["inputSchema"]
            for holder in (schema.get("$defs", {}), schema["properties"]):
                for key, value in holder.items():
                    if key in ("selector", "expect", "end_selector") and "properties" in value:
                        self.assertEqual(set(value["properties"]), set(reference["properties"]), name)
                        documented += any("description" in field for field in value["properties"].values())
        self.assertEqual(documented, 1)

    def test_catalog_stays_within_the_model_context_budget(self):
        # 0.1.14 published 39,084 bytes, 29,568 of them visible to the model.
        self.assertLess(json_bytes({"tools": LISTED_TOOLS}), 31000)
        visible = sum(json_bytes({"name": tool["name"], "description": tool["description"],
                                  "parameters": host_supported_schema(tool["inputSchema"])})
                      for tool in LISTED_TOOLS)
        self.assertLess(visible, 22000)

    def test_batch_references_resolve_to_the_complete_runtime_contract(self):
        self.assertIn("$defs", self.batch)
        expanded = expand_references(self.batch)
        self.assertEqual(expanded, without_documentation(SCHEMAS["batch"]))
        branches = expanded["properties"]["steps"]["items"]["oneOf"]
        self.assertEqual(len(branches), len(BATCH_OPS))
        for branch in branches:
            op = branch["properties"]["op"]["const"]
            with self.subTest(op=op):
                self.assertEqual(branch["required"], ["op", "args"])
                self.assertFalse(branch["additionalProperties"])
                arguments = branch["properties"]["args"]
                self.assertFalse(arguments["additionalProperties"])
                self.assertEqual(arguments, without_documentation(SCHEMAS[op]))

    def test_host_projection_keeps_references_and_all_operation_fields(self):
        projected = host_supported_schema(self.batch)
        self.assertEqual(set(projected["$defs"]), set(self.batch["$defs"]))
        expanded = expand_references(projected)
        expected = host_supported_schema(without_documentation(SCHEMAS["batch"]))
        self.assertEqual(expanded, expected)
        actual_ops = {branch["properties"]["op"]["enum"][0]
                      for branch in expanded["properties"]["steps"]["items"]["oneOf"]}
        self.assertEqual(actual_ops, set(BATCH_OPS))
        self.assertIn("$ref", json.dumps(projected))

    def test_publication_does_not_replace_the_strict_runtime_schemas(self):
        self.assertIsNot(self.batch, SCHEMAS["batch"])
        self.assertNotIn("$ref", json.dumps(SCHEMAS["batch"]))
        self.assertNotIn("$defs", SCHEMAS["batch"])
        original = copy.deepcopy(SCHEMAS["batch"])
        expand_references(self.batch)
        self.assertEqual(SCHEMAS["batch"], original)

    def test_valid_examples_accept_the_same_fields_in_both_contracts(self):
        examples = {
            "tap": {"x": 40, "y": 80, "observe": "tree"},
            "swipe": {"region": {"x": 0, "y": 100, "width": 300, "height": 400},
                      "verify": False, "observe": "both"},
            "type_text": {"selector": {"label_contains": "message", "type": "TextField", "index": 1},
                          "text": "直接输入完整文本", "replace": True, "verify": True},
            "launch_app": {"bundle_id": "com.example.app", "expect": {"label": "首页"}},
            "press_button": {"name": "home", "verify": False},
            "wait": {"selector": {"label": "完成"}, "timeout_seconds": 2},
            "observe": {"mode": "tree", "max_nodes": 50},
            "scroll_find": {"selector": {"label": "列表末项"}, "max_swipes": 3},
        }
        self.assertEqual(set(examples), set(BATCH_OPS))
        arguments = {"steps": [{"op": op, "args": args} for op, args in examples.items()]}
        for schema in (SCHEMAS["batch"], expand_references(self.batch)):
            validate(arguments, schema)
        validate_semantics("batch", arguments)

    def test_unknown_fields_in_later_batch_step_fail_before_any_execution(self):
        first = {"op": "press_button", "args": {"name": "home"}}
        invalid_cases = [
            {"steps": [first], "unknown": True},
            {"steps": [first, {"op": "tap", "args": {"x": 40, "y": 80}, "unknown": True}]},
            {"steps": [first, {"op": "type_text", "args": {
                "selector": {"name": "message"}, "text": "test", "unknown": True}}]},
            {"steps": [first, {"op": "tap", "args": {"selector": {"label": "继续", "rect": {}}}}]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            self.addCleanup(runtime.close)
            with patch.object(runtime, "_call") as execute, patch.object(runtime.client, "request") as request:
                for arguments in invalid_cases:
                    with self.subTest(arguments=arguments), self.assertRaises(WDAError) as caught:
                        runtime.call("pua_batch", arguments)
                    self.assertEqual(caught.exception.code, "invalid_argument")
                    self.assertFalse(caught.exception.details["action_executed"])
                execute.assert_not_called()
                request.assert_not_called()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Runtime(self.directory.name)
        self.addCleanup(self.runtime.close)
        self.client = FakeWDA()
        self.client.close = Mock()
        self.runtime.client = self.client
        self.runtime.phone = PhoneController(self.client, self.directory.name)
        self.manager = Mock()
        self.manager.mirroring_running.return_value = False
        self.manager.pending_recovery.return_value = None
        self.manager.recover.return_value = {"ok": True, "job_id": "recovery-test", "recovery": {"state": "queued"}}
        self.runtime.setup_manager = self.manager

    def assert_error(self, code, tool="pua_ready", **args):
        with self.assertRaises(WDAError) as caught:
            self.runtime.call(tool, args)
        self.assertEqual(caught.exception.code, code)
        return caught.exception.as_dict()

    def assert_no_phone_mutation(self):
        self.assertEqual([path for _, path, _ in self.client.actions() if path != "/session"], [])

    def test_transient_foreground_failure_retries_only_fresh_read_once(self):
        original = self.client.request
        attempts = []

        def request(method, path, payload=None, timeout=None):
            if path == "/wda/activeAppInfo":
                attempts.append(path)
                if len(attempts) == 1:
                    self.client.calls.append((method, path, payload))
                    return {"value": {"bundleId": "local.pid.0"}}
            return original(method, path, payload, timeout)

        with patch.object(self.client, "request", side_effect=request):
            result = self.runtime.call("pua_ready", {"screenshot": False})
        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["proof"]["foreground_resolved"])
        self.assertTrue(result["recovery"]["session_recreated"])
        self.assertFalse(result["recovery"]["replayed_action"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sum(path == "/session" for _, path, _ in self.client.calls), 2)
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_persistent_foreground_error_queues_recovery_and_explicit_poll_guidance(self):
        self.client.app = "local.pid.0"
        result = self.runtime.call("pua_ready", {"screenshot": False})
        self.assert_pending_state(result)
        self.assertEqual(result["category"], "channel_runtime")
        self.assertTrue(result["session_read_retried"])
        self.assertEqual(result["cause"]["code"], "pua_foreground_unavailable")
        self.assertEqual(result["recovery"]["status_tool"], "pua_setup")
        self.assertEqual(result["recovery"]["status_arguments"], {"action": "status", "job_id": "recovery-test"})
        self.assertEqual(result["recovery"]["next_tool"], "pua_ready")
        self.assertEqual(result["recovery"]["next_arguments"], {"screenshot": False, "recover": True})
        self.manager.recover.assert_called_once_with()
        self.assertEqual(sum(path == "/session" for _, path, _ in self.client.calls), 2)
        self.assert_no_phone_mutation()

    def test_local_pid_stale_error_from_source_uses_same_bounded_recovery(self):
        fault = WDAError("stale element reference", "Application local.pid.0 is not running")
        with patch.object(self.client, "source", side_effect=fault) as source:
            result = self.runtime.call("pua_ready", {"screenshot": False})
        self.assertEqual(source.call_count, 2)
        self.assert_pending_state(result)
        self.assertTrue(result["session_read_retried"])
        self.manager.recover.assert_called_once_with()
        self.assert_no_phone_mutation()

    def test_diagnose_mode_explains_runtime_failure_without_service_restart(self):
        self.client.app = "local.pid.0"
        result = self.runtime.call("pua_ready", {"screenshot": False, "recover": False})
        self.assert_not_ready_state(result, "recovery_required")
        self.assertEqual(result["reason"], "recovery_disabled")
        self.assertTrue(result["session_read_retried"])
        self.assertEqual(result["category"], "channel_runtime")
        self.assertEqual(result["cause"]["code"], "pua_foreground_unavailable")
        self.assertEqual(result["recovery"]["state"], "disabled")
        self.assertEqual(result["recovery"]["next_tool"], "pua_ready")
        self.assertEqual(result["recovery"]["next_arguments"], {"screenshot": False, "recover": True})
        self.assertIn("user", result["recovery"]["permission_note"].lower())
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_authorization_code_41_skips_session_read_retry_and_queues_recovery(self):
        fault = WDAError("unknown error", "Error Domain=XCTDaemonErrorDomain Code=41 Not authorized for performing UI testing actions")
        with patch.object(self.client, "source", side_effect=fault) as source:
            result = self.runtime.call("pua_ready", {"screenshot": False})
        self.assertEqual(source.call_count, 1)
        self.assert_pending_state(result)
        self.assertFalse(result["session_read_retried"])
        self.manager.recover.assert_called_once_with()
        self.assert_no_phone_mutation()

    def test_unproven_service_owner_never_claims_recovery_started(self):
        self.client.app = "local.pid.0"
        self.manager.recover.return_value = {"ok": False, "error": "owned service not found", "next_steps": ["Use the original owner to restart WDA"]}
        error = self.assert_error("pua_recovery_required", screenshot=False)
        self.assertFalse(error["ready"])
        self.assertIn("owned service", error["message"])
        self.assertNotIn("job_id", error["recovery"])
        self.assertFalse(error["recovery"]["replay_action"])
        self.assert_no_phone_mutation()

    def test_pending_service_recovery_handles_temporarily_unreachable_status(self):
        self.manager.pending_recovery.return_value = {"job_id": "in-flight", "state": "restarting"}
        fault = WDAError("pua_unreachable", "Service is restarting")
        with patch.object(self.client, "request", side_effect=fault):
            result = self.runtime.call("pua_ready", {"screenshot": False})
        self.assert_pending_state(result)
        self.assertEqual(result["recovery"]["job_id"], "in-flight")
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_cold_start_failure_directs_setup_without_starting_or_claiming_ready(self):
        for code in ("pua_unreachable", "not_ready"):
            for recover in (True, False):
                with self.subTest(code=code, recover=recover):
                    fault = WDAError(code, "Service is not accepting connections")
                    with patch.object(self.client, "request", side_effect=fault):
                        error = self.assert_error(code, screenshot=False, recover=recover)
                    self.assertFalse(error["ready"])
                    self.assertFalse(error["action_executed"])
                    self.assertTrue(error["initialization_required"])
                    self.assertEqual(error["recovery"]["next_tool"], "pua_setup")
                    self.assertEqual(error["recovery"]["next_arguments"], {"action": "status"})
                    self.assertFalse(error["recovery"]["replay_action"])
                    self.assertNotIn("proof", error)
                    self.manager.recover.assert_not_called()
                    self.manager.dispatch.assert_not_called()
                    self.assert_no_phone_mutation()

    def assert_not_ready_state(self, result, state):
        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], state)
        self.assertFalse(result["action_executed"])
        self.assertNotIn("error", result)
        self.assertNotIn("proof", result)
        self.assertNotIn("observation", result)
        self.assertNotIn("image", result)
        self.assertFalse(result["recovery"]["replay_action"])

    def assert_pending_state(self, result):
        self.assert_not_ready_state(result, "recovering")
        self.assertIn("job_id", result["recovery"])
        self.assertIsNone(self.client.session_id)
        self.assertEqual(self.runtime.phone.snapshots, {})

    def test_diagnostic_recovery_guidance_preserves_requested_screenshot(self):
        self.client.app = "local.pid.0"
        result = self.runtime.call("pua_ready", {"screenshot": True, "recover": False})
        self.assert_not_ready_state(result, "recovery_required")
        self.assertEqual(result["recovery"]["next_arguments"], {"screenshot": True, "recover": True})
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_pending_recovery_keeps_diagnostic_mode_in_next_ready_arguments(self):
        self.manager.pending_recovery.return_value = {"job_id": "already-running", "state": "restarting"}
        fault = WDAError("pua_unreachable", "Service is restarting")
        with patch.object(self.client, "request", side_effect=fault):
            result = self.runtime.call("pua_ready", {"screenshot": False, "recover": False})
        self.assert_pending_state(result)
        self.assertEqual(result["recovery"]["next_arguments"], {"screenshot": False, "recover": False})
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_healthy_ready_contains_state_and_does_not_try_recovery(self):
        result = self.runtime.call("pua_ready", {"screenshot": False, "recover": False})
        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["proof"]["foreground_resolved"])
        self.assertGreater(result["observation"]["total_nodes"], 0)
        self.assertNotIn("error", result)
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_pending_recovery_never_proves_ready_from_old_healthy_listener(self):
        self.manager.pending_recovery.return_value = {"job_id": "waiting-to-stop-old-service", "state": "queued"}
        self.client.session_id = "old-session"
        self.runtime.phone.snapshots["old-observation"] = {"nodes": []}
        result = self.runtime.call("pua_ready", {"screenshot": False, "recover": False})
        self.assert_pending_state(result)
        self.assertEqual(result["recovery"]["job_id"], "waiting-to-stop-old-service")
        self.assertEqual(result["recovery"]["next_arguments"], {"screenshot": False, "recover": False})
        self.assertEqual(self.client.calls, [])
        self.assertFalse(result["session_read_retried"])
        self.manager.recover.assert_not_called()

    def test_recovery_cooldown_remains_actionable_error(self):
        self.client.app = "local.pid.0"
        self.manager.recover.return_value = {"ok": False, "error": "Recovery cooldown is active", "recovery": {"state": "cooldown", "retry_after_seconds": 120}}
        error = self.assert_error("pua_recovery_required", screenshot=False)
        self.assertEqual(error["recovery"]["state"], "cooldown")
        self.assertEqual(error["recovery"]["retry_after_seconds"], 120)
        self.assertNotIn("job_id", error["recovery"])
        self.assertFalse(error["action_executed"])
        self.assert_no_phone_mutation()

    def test_locked_phone_never_recreates_session_or_restarts_service(self):
        self.client.locked = True
        error = self.assert_error("phone_locked", screenshot=False)
        self.assertEqual(error["code"], "phone_locked")
        self.assertIsNone(self.client.session_id)
        self.manager.recover.assert_not_called()
        self.client.close.assert_not_called()
        self.assert_no_phone_mutation()

    def test_phone_lock_during_foreground_retry_stops_recovery(self):
        original = self.client.request

        def request(method, path, payload=None, timeout=None):
            result = original(method, path, payload, timeout)
            if path == "/wda/activeAppInfo":
                self.client.locked = True
                return {"value": {"bundleId": "local.pid.0"}}
            return result

        with patch.object(self.client, "request", side_effect=request):
            self.assert_error("phone_locked", screenshot=False)
        self.assertEqual(sum(path == "/session" for _, path, _ in self.client.calls), 1)
        self.assertIsNone(self.client.session_id)
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_generic_stale_element_is_not_misclassified_as_channel_failure(self):
        fault = WDAError("stale element reference", "Previously found element Button 'Continue' is not present")
        with patch.object(self.client, "source", side_effect=fault) as source:
            error = self.assert_error("stale element reference", screenshot=False)
        self.assertEqual(source.call_count, 1)
        self.assertNotIn("category", error)
        self.manager.recover.assert_not_called()
        self.client.close.assert_not_called()
        self.assert_no_phone_mutation()

    def test_observe_foreground_failure_points_to_ready_without_restart(self):
        self.client.app = "local.pid.0"
        error = self.assert_error("pua_foreground_unavailable", tool="pua_observe", mode="tree")
        self.assertFalse(error["action_executed"])
        self.assertEqual(error["category"], "channel_runtime")
        self.assertEqual(error["recovery"]["tool"], "pua_ready")
        self.assertFalse(error["recovery"]["replay_action"])
        self.manager.recover.assert_not_called()
        self.client.close.assert_not_called()
        self.assert_no_phone_mutation()

    def test_successful_click_followed_by_stale_source_does_not_replay_click_or_restart(self):
        fault = WDAError("stale element reference", "Application local.pid.0 is not running")
        with patch.object(self.client, "source", side_effect=fault) as source:
            error = self.assert_error("stale element reference", tool="pua_tap", selector={"label": "Target"}, observe="tree")
        self.assertEqual(source.call_count, 1)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/element/target/click"])
        self.assertEqual(self.runtime.phone.accepted_actions, 1)
        self.assertTrue(error["action_executed"])
        self.assertFalse(error["action_complete"])
        self.assertIn("verification_required", error)
        self.manager.recover.assert_not_called()


class ReadinessCLITests(unittest.TestCase):
    def server_main(self, data=None, error=None):
        runtime = Mock()
        if error is not None:
            runtime.call.side_effect = error
        else:
            runtime.call.return_value = data
        output = io.StringIO()
        with patch.object(mcp_server, "Runtime", return_value=runtime), \
                patch.object(sys, "argv", ["mcp_server", "--ready"]), contextlib.redirect_stdout(output):
            status = mcp_server.main()
        runtime.call.assert_called_once_with("pua_ready", {})
        runtime.close.assert_called_once_with()
        return status, json.loads(output.getvalue())

    def test_server_cli_waiting_states_return_one_with_structured_state(self):
        for state in ("recovery_required", "recovering"):
            with self.subTest(state=state):
                data = {"ready": False, "state": state, "recovery": {"state": "disabled" if state == "recovery_required" else "queued"}}
                status, result = self.server_main(data)
                self.assertEqual(status, 1)
                self.assertEqual(result, data)
                self.assertNotIn("error", result)

    def test_server_cli_requires_explicit_true_ready(self):
        for ready in (None, 1, "true"):
            with self.subTest(ready=ready):
                data = {} if ready is None else {"ready": ready}
                status, result = self.server_main(data)
                self.assertEqual(status, 1)
                self.assertEqual(result, data)

    def test_server_cli_healthy_ready_returns_zero(self):
        data = {"ready": True, "state": "ready", "proof": {"foreground_resolved": True}}
        status, result = self.server_main(data)
        self.assertEqual(status, 0)
        self.assertEqual(result, data)

    def test_server_cli_real_failure_still_returns_error_and_one(self):
        status, result = self.server_main(error=WDAError("phone_locked", "Unlock the phone"))
        self.assertEqual(status, 1)
        self.assertEqual(result["error"]["code"], "phone_locked")


class AgenticContextServerTests(unittest.TestCase):
    """agenticContext-specific guarantees: no telemetry, hidden preview tools, launcher."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_no_analytics_module_or_telemetry_reference_is_vendored(self):
        self.assertIsNone(importlib.util.find_spec("app.core.iphone.analytics"))
        self.assertFalse(list(PACKAGE_ROOT.rglob("*posthog*")))
        vendored = [*PACKAGE_ROOT.glob("wda_*.py"), PACKAGE_ROOT / "mcp_server.py", PACKAGE_ROOT / "apps.json",
                    *(PACKAGE_ROOT / "tooling").iterdir()]
        self.assertEqual(len([path for path in vendored if path.suffix == ".py"]), 8)
        for path in vendored:
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("posthog", text, path.name)
            self.assertNotIn("analytics", text, path.name)

    def test_runtime_records_no_installation_identity(self):
        runtime = Runtime(self.directory.name)
        self.addCleanup(runtime.close)
        self.assertFalse(hasattr(runtime, "analytics"))
        self.assertTrue(runtime.call("pua_apps", {"query": "招商银行", "source": "catalog"})["ok"])
        self.assertFalse((Path(self.directory.name) / "analytics-id").exists())

    def test_hidden_preview_tools_stay_callable_in_process(self):
        runtime = Runtime(self.directory.name)
        self.addCleanup(runtime.close)
        frame = runtime.call("pua_screen_frame", {"after_seq": 0, "last_event_id": 0})
        self.assertFalse(frame["frame_available"])
        self.assertIsNone(frame["frame"])
        self.assertFalse(runtime.screen.paused())
        self.assertEqual({tool["name"] for tool in TOOLS} - {tool["name"] for tool in LISTED_TOOLS},
                         {"pua_screen_frame", "pua_screen_action"})
        runtime.screen.close()

    def test_state_directory_inside_a_git_checkout_is_refused(self):
        checkout = Path(self.directory.name) / "checkout"
        (checkout / ".git").mkdir(parents=True)
        with self.assertRaises(ValueError):
            Runtime(checkout / "local_store" / "iphone")

    def test_module_help_runs_from_the_repository_root_without_creating_state(self):
        home = Path(self.directory.name) / "home"
        home.mkdir()
        result = subprocess.run([sys.executable, "-m", "app.core.iphone.mcp_server", "--help"],
                                capture_output=True, text=True, timeout=10, cwd=PROJECT_ROOT,
                                env={**os.environ, "HOME": str(home)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--state-dir", result.stdout)
        self.assertEqual(list(home.iterdir()), [])

    def test_launcher_writes_only_json_rpc_to_stdout(self):
        launcher = PROJECT_ROOT / "scripts" / "iphone_mcp.sh"
        self.assertTrue(os.access(launcher, os.X_OK))
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
        result = subprocess.run(["bash", str(launcher), "--state-dir", self.directory.name],
                                input="".join(json.dumps(item) + "\n" for item in requests),
                                capture_output=True, text=True, encoding="utf-8", timeout=20,
                                cwd=self.directory.name,
                                env={**os.environ, "AGENTIC_CONTEXT_PYTHON": sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2])
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "agenticcontext-iphone")
        self.assertEqual(responses[1]["result"], {})
