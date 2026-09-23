"""Tunnel call accounting and compact badge acceptance. Code version: v1.5.0-codex.0."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from playwright.sync_api import expect

from app.core import tunnel_mcp
from app.core.computer_use_agent import ComputerUseSettings
from app.web.tunnel_routes import tunnel_status_payload
from tests import test_sidebar_e2e, test_tunnel_mcp

workspace = test_tunnel_mcp.workspace
reference = test_tunnel_mcp.reference
registry = test_tunnel_mcp.registry
service = test_tunnel_mcp.service
disposable_browser = test_sidebar_e2e.disposable_browser
sidebar_server_url = test_sidebar_e2e.sidebar_server_url


def test_running_call_keeps_identity_and_counts_visible_text_once(service, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    result = {}
    observed_text = []

    def estimate(text):
        observed_text.append(text)
        return len(text)

    def run(*_args):
        entered.set()
        assert release.wait(5)
        return {"ok": True, "text": "Unicode: 中文 <|endoftext|>"}

    monkeypatch.setattr(tunnel_mcp, "_estimated_tool_tokens", estimate)
    monkeypatch.setattr(service, "_run_tool", run)
    worker = threading.Thread(
        target=lambda: result.update(test_tunnel_mcp.call(service, "list_files"))
    )
    worker.start()
    try:
        assert entered.wait(5)
        active = service.activity_snapshot()
        assert active["call_count"] == 0
        assert active["recent_calls"] == []
        assert active["task_usage"] is None
        record = active["active_calls"][0]
        identity = record["call_id"]
        assert record["state"] == "running"
        assert record["usage_partial"] is True
        assert record["estimated_tokens"] == len(observed_text[0])
        record["estimated_tokens"] = -1
        assert service.activity_snapshot()["active_calls"][0]["estimated_tokens"] >= 0
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    snapshot = service.activity_snapshot()
    recent = snapshot["recent_calls"][0]
    assert snapshot["active_calls"] == []
    assert recent["call_id"] == identity
    assert recent["state"] == "completed"
    assert recent["usage_partial"] is False
    assert len(observed_text) == 2
    assert observed_text[1] == result["content"][0]["text"]
    assert recent["estimated_tokens"] == sum(map(len, observed_text))
    assert recent["response_tokens"] == len(observed_text[1])


@pytest.mark.parametrize("failure", [ValueError("failed"), KeyError("unexpected")])
def test_failure_always_finishes_the_same_call(service, monkeypatch, failure):
    monkeypatch.setattr(tunnel_mcp, "_estimated_tool_tokens", lambda text: 1)
    with patch.object(service, "_run_tool", side_effect=failure):
        if isinstance(failure, KeyError):
            with pytest.raises(KeyError):
                test_tunnel_mcp.call(service, "list_files")
        else:
            assert test_tunnel_mcp.call(service, "list_files")["isError"]
    snapshot = service.activity_snapshot()
    assert snapshot["active_calls"] == []
    recent = snapshot["recent_calls"][0]
    assert recent["state"] == "failed"
    assert recent["estimated_tokens"] == (None if isinstance(failure, KeyError) else 2)


def test_serialization_failure_is_failed_and_unavailable(service, monkeypatch):
    monkeypatch.setattr(tunnel_mcp, "_estimated_tool_tokens", lambda text: 1)
    with patch.object(service, "_run_tool", return_value={"ok": True, "bad": object()}):
        with pytest.raises(TypeError):
            test_tunnel_mcp.call(service, "list_files")
    snapshot = service.activity_snapshot()
    assert snapshot["active_calls"] == []
    assert snapshot["recent_calls"][0]["state"] == "failed"
    assert snapshot["recent_calls"][0]["estimated_tokens"] is None


def test_parallel_calls_and_bounded_history_are_not_task_totals(service, monkeypatch):
    monkeypatch.setattr(tunnel_mcp, "_estimated_tool_tokens", lambda text: 1)
    tool = tunnel_mcp.TUNNEL_TOOLS_BY_NAME["list_files"]
    first = service._start_activity(tool, {"project": "main"})
    second = service._start_activity(tool, {"project": "ref"})
    assert first != second
    assert len(service.activity_snapshot()["active_calls"]) == 2
    service._finish_activity(second, True, 1, "{}")
    snapshot = service.activity_snapshot()
    assert snapshot["active_calls"][0]["call_id"] == first
    assert snapshot["recent_calls"][0]["call_id"] == second
    service._finish_activity(first, False, 2, "{}")
    for _ in range(tunnel_mcp.TUNNEL_ACTIVITY_LIMIT + 2):
        call_id = service._start_activity(tool, {"project": "main"})
        service._finish_activity(call_id, True, 1, "{}")
    snapshot = service.activity_snapshot()
    assert len(snapshot["recent_calls"]) == tunnel_mcp.TUNNEL_ACTIVITY_LIMIT
    assert snapshot["call_count"] > len(snapshot["recent_calls"])
    assert all(record["estimated_tokens"] == 2 for record in snapshot["recent_calls"])
    assert snapshot["task_usage"] is None


def test_unknown_estimate_never_becomes_zero_or_a_partial_total(service, monkeypatch):
    monkeypatch.setattr(tunnel_mcp, "_estimated_tool_tokens", lambda text: None)
    test_tunnel_mcp.call(service, "list_files")
    assert service.activity_snapshot()["recent_calls"][0]["estimated_tokens"] is None
    estimates = iter([None, 99])
    monkeypatch.setattr(tunnel_mcp, "_estimated_tool_tokens", lambda text: next(estimates))
    test_tunnel_mcp.call(service, "list_files")
    record = service.activity_snapshot()["recent_calls"][0]
    assert record["request_tokens"] is None
    assert record["response_tokens"] == 99
    assert record["estimated_tokens"] is None


def test_active_status_requires_current_project_success_after_current_ready(
    service,
    workspace,
    tmp_path,
) -> None:
    service.selection_store.save("main")
    ready_since = time.time() - 1

    class Runtime:
        def snapshot(self):
            return {
                "runtime_instance_id": "a" * 32,
                "state": "ready",
                "ready": True,
                "enabled": True,
                "generation": 1,
                "ready_since": ready_since,
            }

    settings = SimpleNamespace(
        settings=ComputerUseSettings(workspace_path=str(workspace))
    )
    tool = tunnel_mcp.TUNNEL_TOOLS_BY_NAME["list_files"]

    def status() -> dict:
        with patch("app.web.tunnel_routes.url_for", return_value="/settings"):
            return tunnel_status_payload(
                settings,
                Runtime(),
                tunnel_mcp_service=service,
                credentials_path=tmp_path / "missing-credentials.json",
            )

    reference_call = service._start_activity(tool, {"project": "ref"})
    service._finish_activity(reference_call, True, 0.01, "{}")
    assert status()["activity_observed"] is False
    assert status()["presentation"]["label"] == "Ready"

    failed_main_call = service._start_activity(tool, {"project": "main"})
    service._finish_activity(failed_main_call, False, 0.01, "{}")
    assert status()["activity_observed"] is False

    successful_main_call = service._start_activity(tool, {"project": "main"})
    service._finish_activity(successful_main_call, True, 0.01, "{}")
    assert status()["activity_observed"] is True
    assert status()["presentation"]["label"] == "Active"

    ready_since = time.time() + 1
    assert status()["activity_observed"] is False
    assert status()["presentation"]["label"] == "Ready"


def test_estimator_unavailable_errors_and_special_text(monkeypatch):
    monkeypatch.setattr(tunnel_mcp, "_TOOL_ENCODING_STARTED", True)
    monkeypatch.setattr(tunnel_mcp, "_TOOL_ENCODING", None)
    assert tunnel_mcp._estimated_tool_tokens("hello") is None

    class Encoding:
        def encode(self, text, *, disallowed_special):
            assert disallowed_special == ()
            return [1, 2, 3]

    monkeypatch.setattr(tunnel_mcp, "_TOOL_ENCODING", Encoding())
    assert tunnel_mcp._estimated_tool_tokens("中文 <|endoftext|>") == 3
    assert tunnel_mcp._estimated_tool_tokens("a" * (tunnel_mcp.MAX_WRITE_CHARACTERS + 1)) is None
    with patch.object(Encoding, "encode", side_effect=RuntimeError("unavailable")):
        assert tunnel_mcp._estimated_tool_tokens("hello") is None


def test_status_endpoint_and_initial_html_share_the_call_record(tmp_path, monkeypatch):
    from app.web.app import create_app

    monkeypatch.setattr(tunnel_mcp, "_estimated_tool_tokens", lambda text: 1)
    app = create_app(
        tmp_path / "store",
        computer_use_settings_path=tmp_path / "settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    mcp = app.extensions["tunnel_mcp_service"]
    call_id = mcp._start_activity(tunnel_mcp.TUNNEL_TOOLS_BY_NAME["list_files"], {"project": "main"})
    with app.test_client() as client:
        payload = client.get("/api/agent/tunnel/status").get_json()
        assert payload["active_calls"][0]["call_id"] == call_id
        html = client.get("/agent/tunnel/chatgpt").get_data(as_text=True)
        assert html.count('class="agent-tunnel-usage-label"') == 1
        assert '<dt class="agent-tunnel-usage-label">Tokens:</dt>' in html
        assert 'aria-label="Estimated recent tool-text tokens"' in html
        for removed_label in (
            "Total calls:",
            "Active calls:",
            "Recent completed calls:",
            "Recent tool text (est.):",
            "Task/model billing:",
        ):
            assert removed_label not in html
        assert "data-agent-tunnel-hint" not in html


def usage_record(identity, count, state="running"):
    return {
        "call_id": identity, "project": "main", "tool": "run_check",
        "state": state, "estimated_tokens": count,
        "usage_partial": state == "running",
    }


@pytest.mark.parametrize("width", [1280, 875, 390, 320])
def test_summary_polling_states_and_long_integer_geometry(
    disposable_browser, sidebar_server_url, width
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900}, reduced_motion="reduce"
    )
    page = context.new_page()
    page.clock.install()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    payload = {
        "presentation": {"tone": "ready", "label": "Ready", "hint": ""},
        "call_count": 0,
        "active_calls": [], "recent_calls": [],
        "recent_usage": {"estimated_tokens": 0, "complete": True},
    }
    page.route(
        "**/api/agent/tunnel/status?platform=chatgpt",
        lambda route: route.fulfill(json=payload),
    )
    try:
        page.goto(f"{sidebar_server_url}/agent/tunnel/chatgpt")
        if width < 900:
            page.get_by_role("button", name="Toggle sidebar", exact=True).click()
        recent_tokens = page.locator(".agent-tunnel-recent-tokens")
        panel = page.locator("[data-agent-tunnel-usage]")
        badge = recent_tokens.locator("[data-tunnel-recent-token-badge]")
        expect(recent_tokens.locator("[data-tunnel-recent-token-digits]")).to_have_text("0")
        expect(page.locator("[data-tunnel-usage-row]")).to_have_count(0)
        expect(panel.locator(".agent-tunnel-usage-label")).to_have_text(
            ["Tokens:"]
        )
        expect(recent_tokens).to_have_attribute(
            "aria-label", "Estimated recent tool-text tokens"
        )
        assert all(
            height <= 20
            for height in panel.locator(".agent-tunnel-usage-label").evaluate_all(
                "labels => labels.map(label => label.getBoundingClientRect().height)"
            )
        )
        # Hanging indent: the usage row starts where the "Tunnel:" literal starts.
        literal_x = page.locator(
            "[data-agent-tunnel-status] .browser-session-status-literal"
        ).bounding_box()["x"]
        label_x = panel.locator(".agent-tunnel-usage-label").first.bounding_box()["x"]
        assert abs(label_x - literal_x) <= 1
        page.evaluate("document.fonts.ready")
        initial_height = panel.bounding_box()["height"]

        def refresh():
            # Advance the existing production poll; no extra refresh endpoint or timer.
            with page.expect_response("**/api/agent/tunnel/status?platform=chatgpt"):
                page.clock.run_for(10_100)

        payload["call_count"] = 1
        payload["active_calls"] = [usage_record(1, 1)]
        refresh()
        expect(recent_tokens.locator("[data-tunnel-recent-token-digits]")).to_have_text("0")
        payload["call_count"] = 1_293
        payload["active_calls"] = [
            usage_record(1, 12_345),
            usage_record(2, 70_475),
            usage_record(3, 0),
        ]
        refresh()
        expect(recent_tokens.locator("[data-tunnel-recent-token-digits]")).to_have_text("0")
        expect(badge).to_have_css("border-radius", "2px")
        expect(badge).to_have_css("padding", "2px 6px")
        expect(badge).to_have_css("font-variant-numeric", "tabular-nums")
        assert badge.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
        payload["active_calls"] = []
        payload["recent_calls"] = [usage_record(1, 9_007_199_254_740_991, "completed")]
        payload["recent_usage"] = {
            "estimated_tokens": 9_007_199_254_740_991,
            "complete": True,
        }
        refresh()
        expect(recent_tokens.locator("[data-tunnel-recent-token-digits]")).to_have_text("9,007,199,254,740,991")
        assert badge.evaluate("e => e.scrollWidth <= e.clientWidth + 1")

        payload["active_calls"] = []
        payload["recent_calls"] = [usage_record(1, 12_345_678, "completed")]
        payload["recent_usage"] = {"estimated_tokens": 12_345_678, "complete": True}
        refresh()
        expect(recent_tokens.locator("[data-tunnel-recent-token-digits]")).to_have_text("12,345,678")
        for unknown in [None, -1, 9_007_199_254_740_992, "12", True]:
            payload["recent_usage"] = {"estimated_tokens": unknown, "complete": False}
            refresh()
            expect(recent_tokens.locator("[data-tunnel-recent-token-unavailable]")).to_be_visible()
        payload["active_calls"] = []
        payload["recent_calls"] = []
        payload["recent_usage"] = {"estimated_tokens": 0, "complete": True}
        refresh()
        assert abs(panel.bounding_box()["height"] - initial_height) <= 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize(
    ("width", "height"),
    [(875, 1222), (390, 900), (875, 420)],
)
def test_agent_status_cards_reuse_cache_status_surface(
    disposable_browser, sidebar_server_url, width, height
):
    context = disposable_browser.new_context(
        viewport={"width": width, "height": height}, reduced_motion="reduce"
    )
    browser_payload = {
        "browser": "edge",
        "browser_label": "Edge",
        "logged_in": True,
        "can_download": True,
        "account_name": "Signed in",
        "message": "The selected browser session is ready.",
        "agent_sources": {"projects": [], "recent_sessions": []},
    }
    tunnel_payload = {
        "presentation": {"tone": "ready", "label": "Ready", "hint": ""},
        "call_count": 1_293,
        "active_calls": [
            usage_record(1, 12_345),
            usage_record(2, 70_475),
            usage_record(3, 0),
        ],
        "recent_calls": [usage_record(4, 82_820, "completed")],
        "recent_usage": {"estimated_tokens": 82_820, "complete": True},
    }
    surface_script = """element => {
        const style = getComputedStyle(element);
        return {
            padding: style.padding,
            background: style.background,
            borderWidth: style.borderWidth,
            borderRadius: style.borderRadius,
            boxShadow: style.boxShadow,
            backdropFilter: style.backdropFilter,
        };
    }"""
    cache_page = context.new_page()
    browser_page = context.new_page()
    tunnel_page = context.new_page()
    for page in (cache_page, browser_page):
        page.route("**/api/browser-session**", lambda route: route.fulfill(json=browser_payload))
    tunnel_page.route(
        "**/api/agent/tunnel/status?platform=chatgpt",
        lambda route: route.fulfill(json=tunnel_payload),
    )
    try:
        cache_page.goto(f"{sidebar_server_url}/cache/chatgpt/text/edge")
        browser_page.goto(f"{sidebar_server_url}/agent/edge/chatgpt")
        tunnel_page.goto(f"{sidebar_server_url}/agent/tunnel/chatgpt")
        if width <= 900:
            for page in (cache_page, browser_page, tunnel_page):
                page.evaluate(
                    """() => window.setSidebarOpen(true, {
                        animate: false,
                        persist: false,
                    })"""
                )
                expect(
                    page.get_by_role("button", name="Toggle sidebar", exact=True)
                ).to_have_attribute("aria-expanded", "true")

        cache_card = cache_page.locator("aside .browser-session-status-card")
        browser_card = browser_page.locator(
            '#agent_runtime_form .browser-session-status-card[data-role="browser-session-status"]'
        )
        tunnel_card = tunnel_page.locator("[data-agent-tunnel-status]")
        for card in (cache_card, browser_card, tunnel_card):
            expect(card).to_be_visible()
        reference_surface = cache_card.evaluate(surface_script)
        assert reference_surface["padding"] == "12px"
        assert reference_surface["borderWidth"] == "0px"
        assert browser_card.evaluate(surface_script) == reference_surface
        assert tunnel_card.evaluate(surface_script) == reference_surface
        expect(tunnel_card.locator("[data-tunnel-recent-token-digits]")).to_have_text("82,820")
        expect(tunnel_card.locator(".agent-tunnel-usage-label")).to_have_text("Tokens:")
        expect(tunnel_card.locator(".agent-tunnel-usage-row")).to_have_count(1)
        for page in (cache_page, browser_page, tunnel_page):
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if height == 420:
            scroll_state = tunnel_page.evaluate(
                """() => {
                    const sidebar = document.querySelector('#agent_sidebar');
                    return {
                        documentScrolls: document.documentElement.scrollHeight > innerHeight + 1,
                        sidebarScrolls: sidebar.scrollHeight > sidebar.clientHeight + 1,
                        sidebarOverflowY: getComputedStyle(sidebar).overflowY,
                    };
                }"""
            )
            assert not scroll_state["documentScrolls"]
            assert scroll_state["sidebarScrolls"]
            assert scroll_state["sidebarOverflowY"] == "auto"
    finally:
        context.close()


@pytest.mark.live
@pytest.mark.parametrize("width", [1280, 390])
def test_live_reference_badge_contract(disposable_browser, sidebar_server_url, width):
    """Read the real sibling specimen without changing either production store."""
    context = disposable_browser.new_context(viewport={"width": width, "height": 900})
    reference_page = context.new_page()
    local_page = context.new_page()
    styles = """e => {
        const s = getComputedStyle(e);
        const value = getComputedStyle(e.querySelector('.workspace-metric-value-major'));
        const glyph = getComputedStyle(e.querySelector('.investment-holdings-allocation-badge-glyph'));
        return {
            radius: s.borderRadius, padding: s.padding, background: s.backgroundColor,
            color: s.color, numeric: s.fontVariantNumeric, fontSize: value.fontSize,
            weight: value.fontWeight, spacing: value.letterSpacing,
            glyphWidth: glyph.width
        };
    }"""
    try:
        reference_page.goto("http://127.0.0.1:8688/settings/style-tokens")
        reference_badge = reference_page.locator(
            ".style-token-holdings-allocation-badge-demo .investment-holdings-allocation-badge"
        ).first
        expect(reference_badge).to_be_attached()
        reference_style = reference_badge.evaluate(styles)
        local_page.route("**/api/agent/tunnel/status?platform=chatgpt", lambda route: route.fulfill(json={
            "presentation": {"tone": "ready", "label": "Ready", "hint": ""},
            "active_calls": [usage_record(1, 12_345_678)], "recent_calls": [],
        }))
        local_page.goto(f"{sidebar_server_url}/agent/tunnel/chatgpt")
        local_badge = local_page.locator(
            '[data-agent-tunnel-usage] [data-tunnel-token-badge]'
        )
        expect(local_badge).to_have_js_property("hidden", False)
        local_style = local_badge.evaluate(styles)
        assert local_style == reference_style
        live_status = context.request.get("http://127.0.0.1:8666/api/agent/tunnel/status")
        assert live_status.ok
        print({
            "width": width, "reference_style": reference_style,
            "live_runtime_has_call_usage": "active_calls" in live_status.json(),
        })
    finally:
        context.close()
