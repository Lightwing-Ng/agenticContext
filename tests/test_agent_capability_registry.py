"""Contract tests for the unified Agent capability registry.

Code version: v1.6.0-codex.1
"""

from __future__ import annotations

import pytest

from app.core.agent.capability_registry import (
    AGENT_ACTIONS,
    CAPABILITY_REGISTRY,
    PAGE_OBSERVATIONS,
    WEBMCP_TOOLS,
    build_agent_optimization_manifest,
    capability_registry_snapshot,
    validate_controller_action_payload,
)


def test_registry_covers_agent_actions_page_observations_and_webmcp_tools() -> None:
    assert len({capability.key for capability in CAPABILITY_REGISTRY}) == len(CAPABILITY_REGISTRY)
    assert set(AGENT_ACTIONS) == {
        "list",
        "read",
        "search",
        "replace",
        "replace_base64",
        "write",
        "write_base64",
        "delete",
        "run",
        "browser_acceptance",
        "job_start",
        "job_status",
        "job_stop",
        "bodycheck",
        "final",
    }
    assert set(PAGE_OBSERVATIONS) == {
        "provider_turn",
        "browser_interruption",
        "agent_status",
        "browser_session",
        "agent_response",
    }
    assert set(WEBMCP_TOOLS) == {
        "get_site_capabilities",
        "get_page_context",
        "navigate_to_site_target",
    }


def test_public_manifest_is_derived_from_the_registry_and_stays_bounded() -> None:
    manifest = build_agent_optimization_manifest()
    assert [item["id"] for item in manifest["capabilities"]] == [
        "cache_review",
        "local_resources",
        "agent_workspace",
        "configuration",
        "agent_actions",
        "page_observation",
        "webmcp_tools",
    ]
    assert "15 registered actions" in manifest["capabilities"][4]["description"]
    assert "5 registered observations" in manifest["capabilities"][5]["description"]
    assert "3 registered tools" in manifest["capabilities"][6]["description"]
    assert {target["path"] for target in manifest["navigation"]} == {
        "/cache/x",
        "/cache/grok",
        "/cache/chatgpt",
        "/cache/gemini",
        "/cache/claude",
        "/cache/zhihu",
        "/browser",
        "/agent",
        "/settings",
    }
    assert all(not target["path"].startswith("/api/") for target in manifest["navigation"])


def test_internal_snapshot_contains_transport_schemas_but_no_runtime_content() -> None:
    snapshot = capability_registry_snapshot()
    assert snapshot["version"] == "1.5.0"
    records = {record["key"]: record for record in snapshot["capabilities"]}
    assert records["agent.action.replace"]["read_only"] is False
    assert records["agent.action.delete"]["handler_name"] == "_delete"
    assert records["agent.action.delete"]["input_schema"]["required"] == [
        "action",
        "path",
        "expected_sha256",
    ]
    assert records["agent.action.run"]["read_only"] is True
    assert records["agent.action.run"]["handler_name"] == "_run"
    assert records["agent.action.run"]["input_schema"]["required"] == ["action", "command"]
    assert records["agent.action.browser_acceptance"]["read_only"] is True
    assert records["agent.action.browser_acceptance"]["handler_name"] == "_browser_acceptance"
    assert records["agent.action.job_start"]["input_schema"]["required"] == [
        "action",
        "entrypoint",
        "config_path",
        "idempotency_key",
    ]
    assert records["agent.action.job_status"]["read_only"] is True
    assert records["agent.action.job_stop"]["handler_name"] == "_job_stop"
    assert records["agent.action.final"]["handler_name"] == ""
    assert records["agent.action.final"]["input_schema"]["required"] == [
        "action",
        "summary",
    ]
    assert records["agent.action.final"]["input_schema"]["additionalProperties"] is False
    assert records["agent.action.final"]["input_schema"]["properties"]["action"]["const"] == "final"
    assert records["agent.action.final"]["input_schema"]["properties"]["verification"] == {
        "type": "array",
        "items": {
            "type": "string",
            "description": "One verification result.",
            "maxLength": 1_000,
            "minLength": 1,
        },
        "maxItems": 20,
    }
    assert records["webmcp.get_page_context"]["read_only"] is True
    assert records["webmcp.get_page_context"]["input_schema"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    assert records["webmcp.navigate_to_site_target"]["input_schema"]["required"] == [
        "target",
    ]
    assert "prompt" not in snapshot
    assert "response" not in snapshot
    assert "page_content" not in snapshot


def test_controller_action_payload_validation_uses_the_registry_owned_schema() -> None:
    validate_controller_action_payload(
        AGENT_ACTIONS["list"],
        {"action": "list", "path": ".", "depth": 2},
    )
    validate_controller_action_payload(
        AGENT_ACTIONS["read"],
        {"action": "read", "path": "README.md", "start_line": 1},
    )
    validate_controller_action_payload(
        AGENT_ACTIONS["browser_acceptance"],
        {
            "action": "browser_acceptance",
            "root": ".",
            "target": "/",
            "expected_text": ["Ready"],
            "expected_selectors": ["main"],
        },
    )
    with pytest.raises(ValueError, match="unsupported field"):
        validate_controller_action_payload(
            AGENT_ACTIONS["browser_acceptance"],
            {
                "action": "browser_acceptance",
                "root": ".",
                "target": "/",
                "user_data_dir": "browser-profile",
            },
        )
    validate_controller_action_payload(
        AGENT_ACTIONS["final"],
        {
            "action": "final",
            "summary": "Completed.",
            "verification": ["Focused test passed."],
            "limitations": ["No remote task was sent."],
        },
    )

    for capability, payload, field in (
        (
            "read",
            {"action": "read", "path": "README.md", "start_line": True},
            "start_line",
        ),
        (
            "read",
            {"action": "read"},
            "path",
        ),
        (
            "read",
            {"action": "read", "path": "README.md", "unexpected": True},
            "unsupported field",
        ),
        (
            "read",
            {"action": "list", "path": "README.md"},
            "registered action value",
        ),
        (
            "final",
            {"action": "final", "summary": "Completed.", "verification": "not-an-array"},
            "verification",
        ),
    ):
        with pytest.raises(ValueError, match=field):
            validate_controller_action_payload(AGENT_ACTIONS[capability], payload)


def test_manifest_webmcp_definitions_are_registry_owned() -> None:
    manifest = build_agent_optimization_manifest()
    assert [tool["name"] for tool in manifest["webmcpTools"]] == [
        "get_site_capabilities",
        "get_page_context",
        "navigate_to_site_target",
    ]
    assert all(
        tool["inputSchema"]["additionalProperties"] is False
        for tool in manifest["webmcpTools"]
    )
