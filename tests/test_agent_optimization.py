"""Rendered-page contracts for OpenAI Site tools and Agent Optimization.

Code version: v1.1.2-codex.1
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from flask.testing import FlaskClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PROJECT_ROOT / "app/web/templates"
MANIFEST_PATTERN = re.compile(
    r'<script id="agent_optimization_manifest" type="application/json">(.*?)</script>',
    re.DOTALL,
)
EXPECTED_PATHS = {
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


def _manifest_from_body(body: str) -> dict[str, object]:
    match = MANIFEST_PATTERN.search(body)
    assert match is not None
    return json.loads(match.group(1))


def test_primary_pages_publish_one_versioned_top_level_site_tools_adapter(
    client: FlaskClient,
) -> None:
    for route in ("/cache/x", "/browser", "/settings", "/agent"):
        response = client.get(route, follow_redirects=True)
        body = response.get_data(as_text=True)

        assert response.status_code == 200
        assert len(MANIFEST_PATTERN.findall(body)) == 1
        assert body.count("/static/agent-optimization.js?v=agent-optimization-v1.1.0") == 1
        manifest = _manifest_from_body(body)
        assert manifest["contractVersion"] == "1.1.0"
        assert manifest["status"] == "project-convention"
        assert manifest["site"]["id"] == "agentic-context"
        assert {item["id"] for item in manifest["capabilities"]} == {
            "cache_review",
            "local_resources",
            "agent_workspace",
            "configuration",
            "agent_actions",
            "page_observation",
            "webmcp_tools",
        }
        assert [tool["name"] for tool in manifest["webmcpTools"]] == [
            "get_site_capabilities",
            "get_page_context",
            "navigate_to_site_target",
        ]
        assert all(
            tool["inputSchema"]["additionalProperties"] is False
            for tool in manifest["webmcpTools"]
        )


def test_manifest_has_bounded_navigation_and_no_mutating_or_recursive_tools(
    client: FlaskClient,
) -> None:
    body = client.get("/cache/x").get_data(as_text=True)
    manifest = _manifest_from_body(body)
    navigation = manifest["navigation"]

    assert {target["path"] for target in navigation} == EXPECTED_PATHS
    assert len({target["id"] for target in navigation}) == len(navigation)
    assert all(target["path"].startswith("/") for target in navigation)
    assert all(not target["path"].startswith("/api/") for target in navigation)
    assert {target["id"] for target in navigation}.isdisjoint(
        {
            "start_cache",
            "stop_cache",
            "delete_resource",
            "restore_resource",
            "run_agent",
            "authorize_terminal",
        },
    )


def test_manifest_and_runtime_are_centralized_and_unlock_page_stays_excluded() -> None:
    adapter_path = TEMPLATE_ROOT / "_agent_optimization.html"
    adapter_source = adapter_path.read_text(encoding="utf-8")
    runtime_source = (
        PROJECT_ROOT / "app/web/static/agent-optimization.js"
    ).read_text(encoding="utf-8")

    assert adapter_source.count('id="agent_optimization_manifest"') == 1
    assert "build_agent_optimization_manifest" in adapter_source
    assert "modelContext" in runtime_source
    assert "registerTool" in runtime_source
    assert "window.top" not in runtime_source
    assert "iframe" in runtime_source
    for template_path in TEMPLATE_ROOT.glob("*.html"):
        if template_path == adapter_path:
            continue
        assert 'id="agent_optimization_manifest"' not in template_path.read_text(
            encoding="utf-8",
        )
    assert "_agent_optimization.html" not in (
        TEMPLATE_ROOT / "agent_access_unlock.html"
    ).read_text(encoding="utf-8")
