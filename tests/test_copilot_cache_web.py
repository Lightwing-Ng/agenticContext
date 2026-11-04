"""Microsoft Copilot participates in the existing text-only Cache contracts.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from flask import template_rendered

from app.core import browser_sessions
from app.core.config import (
    DEFAULT_COPILOT_ENTRY_URL,
    CrawlConfig,
    load_saved_config,
    normalize_copilot_entry_url,
    save_config,
)
from app.core.copilot_history_store import CopilotHistoryStore, build_copilot_initial_snapshot
from app.core.copilot_sync_state import copilot_sync_state_path, read_copilot_sync_state, write_copilot_sync_state
from app.core.resource_persistence import COPILOT_HISTORY_SCHEMA, write_parquet_rows_atomic
from app.web.cache_sources import get_cache_source_view
from app.web.form_config import parse_form_config


def test_copilot_browser_default_and_persisted_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.is_macos_host", lambda: True)
    assert CrawlConfig().copilot_browser == "safari"
    monkeypatch.setattr("app.core.config.is_macos_host", lambda: False)
    assert CrawlConfig().copilot_browser == "chrome"
    destination = tmp_path / "settings.json"
    save_config(CrawlConfig(copilot_browser="edge", chatgpt_browser="chrome"), destination)
    loaded = load_saved_config(destination)
    assert loaded.copilot_browser == "edge"
    assert loaded.chatgpt_browser == "chrome"


def test_copilot_entry_query_survives_construction_and_settings_round_trip(tmp_path):
    entry = "https://WWW.Copilot.com/chat?es=Click&fromcode=fixture-route&fromCode=KeepCase&redirectId=fixture%2Fhistory"
    expected = entry.replace("WWW.Copilot.com", "www.copilot.com")
    config = CrawlConfig(copilot_entry_url=entry)
    assert config.copilot_entry_url == expected
    destination = tmp_path / "settings.json"
    save_config(config, destination)
    assert load_saved_config(destination).copilot_entry_url == expected
    assert CrawlConfig().copilot_entry_url == DEFAULT_COPILOT_ENTRY_URL


@pytest.mark.parametrize("entry", [
    "http://copilot.com/chat?fromcode=fixture",
    "https://copilot.com.evil.example/chat?fromcode=fixture",
    "https://copilot.com@evil.example/chat",
    "https://user@copilot.com/chat",
    "https://copilot.com:443/chat",
    "https://copilot.com:/chat",
    "https://copilot.com/chat#history",
    "https://copilot.com/chat#",
    "https://copilot.com/chat/all?fromcode=fixture",
    "https://copilot.com/chats/private-session",
    "https://copilot.com/chat?fromcode=bad\nvalue",
    "https://copilot.com/chat?fromcode=bad value",
    "https://[invalid/chat",
])
def test_copilot_entry_url_rejects_untrusted_destinations(entry):
    assert normalize_copilot_entry_url(entry) == DEFAULT_COPILOT_ENTRY_URL
    assert CrawlConfig(copilot_entry_url=entry).copilot_entry_url == DEFAULT_COPILOT_ENTRY_URL


def test_copilot_entry_form_preserves_missing_and_accepts_trusted_query(app):
    entry = "https://copilot.com/chat?fromcode=fixture-route&redirectId=history%2Fall"
    existing = CrawlConfig(copilot_entry_url=entry)
    with app.test_request_context("/cache/chatgpt/start", method="POST", data={"chatgpt_browser": "chrome"}):
        assert parse_form_config(existing).copilot_entry_url == entry
    changed = "https://www.copilot.com/chat?fromCode=another-route&es=Click"
    with app.test_request_context("/cache/copilot/start", method="POST", data={"copilot_entry_url": changed}):
        assert parse_form_config(existing).copilot_entry_url == changed
    with app.test_request_context("/cache/copilot/start", method="POST", data={"copilot_entry_url": "https://evil.example/chat"}):
        assert parse_form_config(existing).copilot_entry_url == DEFAULT_COPILOT_ENTRY_URL


def test_copilot_page_has_browser_and_text_history_without_media(client, macos_host):
    response = client.get("/cache/copilot/text/safari")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'aria-label="Cache source: Microsoft Copilot"' in html
    assert 'data-cache-page-content-mode="text"' in html
    assert 'data-cache-content-modes="text"' in html
    assert 'name="copilot_browser"' in html
    assert 'data-platform="copilot"' in html
    assert 'data-cache-content-mode-option="media"' not in html
    assert 'data-cache-content-mode-option="text"' not in html
    assert "Sessions cached" in html and "Messages cached" in html
    assert "source=copilot" in html
    assert client.get("/cache/copilot/media/safari").status_code == 404
    assert get_cache_source_view("copilot").content_modes == ("text",)


@pytest.mark.parametrize("source", ["chatgpt", "gemini", "claude", "grok", "x", "zhihu"])
def test_other_sources_offer_copilot_with_a_text_route(client, macos_host, source):
    html = client.get(f"/cache/{source}/media/safari").get_data(as_text=True)
    if source == "zhihu":
        html = client.get("/cache/zhihu/media/edge").get_data(as_text=True)
    assert 'data-cache-source-switcher-option="copilot"' in html
    assert 'data-cache-source-content-modes="text"' in html
    assert 'data-cache-source-switcher-path="/cache/copilot/text/' in html
    assert "/cache/copilot/media/" not in html


def test_copilot_start_uses_its_browser_and_only_text(client, app, macos_host):
    service = app.extensions["cache_runtimes"]["copilot"]["text"].service
    service.start = Mock()
    response = client.post("/cache/copilot/start", data={
        "copilot_browser": "safari", "cache_content_mode": "media",
    })
    assert response.status_code == 302
    assert response.location == "/cache/copilot/text/safari"
    config = service.start.call_args.args[0]
    assert config.copilot_browser == "safari"
    assert service.start.call_args.kwargs == {"content_mode": "text"}
    assert set(app.extensions["cache_runtimes"]["copilot"]) == {"text"}


def test_copilot_probe_dispatch_preserves_existing_browser_descriptor(macos_host):
    config = CrawlConfig(copilot_browser="safari")
    expected = {"logged_in": True, "can_download": True, "account_name": "Signed-in fixture", "message": "Ready"}
    with patch("app.core.copilot_history.probe_copilot_session", return_value=expected) as probe:
        result = browser_sessions.probe_browser_session("copilot", "safari", config, silent=True)
    assert result["platform"] == "copilot"
    assert result["browser"] == "safari"
    assert result["can_download"] is True
    assert probe.call_args.args[0].browser_id == "safari"
    assert probe.call_args.args[1] is config
    assert probe.call_args.kwargs == {"silent": True, "prefer_initialized_debug_profile": False}


def test_copilot_manual_open_uses_fixed_home_without_replacing_profile(macos_host):
    with patch("app.core.browser_sessions.subprocess.Popen") as launch:
        result = browser_sessions.open_copilot_browser_for_login("safari", CrawlConfig())
    assert launch.call_args.args[0] == ["/usr/bin/open", "-g", "-a", "Safari", "https://copilot.com/chat"]
    assert result["platform"] == "copilot"
    assert result["url"] == "https://copilot.com/chat"


def test_copilot_manual_open_preserves_configured_entry_query(macos_host):
    entry = "https://copilot.com/chat?fromcode=fixture-route&es=Click&redirectId=history%2Fall"
    with patch("app.core.browser_sessions.subprocess.Popen") as launch:
        result = browser_sessions.open_copilot_browser_for_login("safari", CrawlConfig(copilot_entry_url=entry))
    assert launch.call_args.args[0][-1] == entry
    assert result["url"] == entry


def test_copilot_start_persists_the_trusted_entry_and_preserves_it_when_missing(client, app, macos_host):
    service = app.extensions["cache_runtimes"]["copilot"]["text"].service
    service.start = Mock()
    entry = "https://copilot.com/chat?es=Click&fromcode=fixture-route"
    response = client.post("/cache/copilot/start", data={
        "copilot_browser": "safari", "copilot_entry_url": entry,
    })
    assert response.status_code == 302
    assert service.start.call_args.args[0].copilot_entry_url == entry
    client.post("/cache/copilot/start", data={"copilot_browser": "safari"})
    assert service.start.call_args.args[0].copilot_entry_url == entry


def test_copilot_manual_open_route_stays_outside_agent_execution(client, macos_host):
    with patch("app.web.agent_routes.open_copilot_browser_for_login", return_value={"opened": True}) as launch:
        response = client.post("/api/browser-session/open-login", json={"platform": "copilot", "browser": "safari"})
        rejected = client.post("/api/browser-session/open-login", json={"platform": "copilot", "browser": "safari", "scope": "agent"})
    assert response.status_code == 200
    assert rejected.status_code == 400
    launch.assert_called_once()


def _complete_copilot_archive(application):
    root = application.extensions["local_media_catalog"].local_store_root
    store = CopilotHistoryStore(root)
    for index in range(2):
        store.replace_conversation(
            {"conversation_id": f"fixture-session-{index}"},
            [{"role": "user", "content_text": f"Fixture message {index}."}],
        )
    write_copilot_sync_state(root, {
        "status": "complete",
        "started_at": "2026-10-21T01:00:00Z",
        "finished_at": "2026-10-21T02:00:00Z",
        "discovery_complete": True,
        "sync_complete": True,
        "discovered_sessions": 2,
        "processed_sessions": 2,
        "failed_sessions": 0,
        "completion_basis": "Fixture provider catalog exhausted its explicit continuation.",
    })
    runtime = application.extensions["cache_runtimes"]["copilot"]["text"]
    runtime.state.replace_snapshot(build_copilot_initial_snapshot("test", root))
    runtime.state.update(
        recent_events=["Fixture task finished after a previous browser failure."],
        task_failures=2,
        started_at="2026-10-21T03:00:00Z",
        finished_at="2026-10-21T04:00:00Z",
    )
    return root, store, runtime


@pytest.mark.parametrize("remaining_sessions", [0, 1])
def test_copilot_finished_status_and_page_recheck_archive_without_changing_manifest(
    client, app, macos_host, remaining_sessions
):
    root, store, runtime = _complete_copilot_archive(app)
    runtime.service.start = Mock(side_effect=AssertionError("Status reconciliation must not launch Safari."))
    before_live = runtime.state.snapshot()
    assert before_live["phase"] == "finished"
    assert client.get("/api/cache/copilot/status").get_json()["performance_metrics"]["sync_complete"] is True
    manifest = copilot_sync_state_path(root)
    before_manifest = (manifest.read_bytes(), manifest.stat().st_mtime_ns)
    if remaining_sessions:
        write_parquet_rows_atomic(store.path, store.rows[:remaining_sessions], COPILOT_HISTORY_SCHEMA)
        before_history = (store.path.read_bytes(), store.path.stat().st_mtime_ns)
    else:
        store.path.unlink()
        before_history = None

    payload = client.get("/api/cache/copilot/status").get_json()
    assert payload["phase"] == "failed"
    assert payload["performance_metrics"]["status"] == "incomplete"
    assert payload["performance_metrics"]["sync_complete"] is False
    assert payload["downloaded_posts"] == payload["downloaded_tweets"] == remaining_sessions
    assert payload["discovered_images"] == remaining_sessions
    assert payload["discovered_tweets"] == payload["processed_tweets"] == 2
    assert "no longer verified" in payload["message"]
    assert "no longer verified" in payload["last_error"]
    for name in ("recent_events", "task_failures", "started_at", "finished_at"):
        assert payload[name] == before_live[name]

    page_snapshots = []

    def record_template(_sender, template, context, **_extra):
        if template.name == "copilot.html":
            page_snapshots.append(context["snapshot"])

    with template_rendered.connected_to(record_template, app):
        response = client.get("/cache/copilot/text/safari")
    assert response.status_code == 200
    assert page_snapshots == [payload]
    html = response.get_data(as_text=True)
    assert 'data-phase="failed"' in html
    assert "no longer verified" in html
    assert "Full history coverage was verified for the last sync." not in html
    assert runtime.state.snapshot() == before_live
    assert (manifest.read_bytes(), manifest.stat().st_mtime_ns) == before_manifest
    assert read_copilot_sync_state(root)["sync_complete"] is True
    if before_history is None:
        assert not store.path.exists()
    else:
        assert (store.path.read_bytes(), store.path.stat().st_mtime_ns) == before_history
    runtime.service.start.assert_not_called()


def test_copilot_running_status_retains_active_progress_instead_of_idle_hydration(client, app):
    _root, store, runtime = _complete_copilot_archive(app)
    store.path.unlink()
    runtime.state.update(
        running=True,
        phase="collecting",
        message="Following fixture history continuation.",
        discovered_tweets=20,
        processed_tweets=5,
        performance_metrics={"status": "running", "sync_complete": False},
    )
    payload = client.get("/api/cache/copilot/status").get_json()
    assert payload["running"] is True
    assert payload["phase"] == "collecting"
    assert payload["message"] == "Following fixture history continuation."
    assert payload["discovered_tweets"] == 20
    assert payload["processed_tweets"] == 5
    assert payload["performance_metrics"]["status"] == "running"
