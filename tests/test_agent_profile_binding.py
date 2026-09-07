"""Regression coverage for profile-bound source evidence and task handoff.

Code version: v1.1.0-codex.1
"""
from __future__ import annotations

import importlib
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock

import pytest

def _isolated_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    web = importlib.import_module("app.web.app")
    from app.core.config import CrawlConfig

    original = CrawlConfig(
        chrome_user_data_dir=tmp_path / "Data A",
        chrome_profile_directory="Profile A",
    )
    monkeypatch.setattr(web, "load_saved_config", lambda: original)
    monkeypatch.setattr(web, "save_config", lambda _config: None)
    application = web.create_app(
        tmp_path / "store",
        computer_use_settings_path=tmp_path / "agent-settings.json",
        computer_use_runtime_root=tmp_path / "runtime",
    )
    application.config.update(TESTING=True)
    return web, application, original

def test_profile_change_collects_new_account_bootstrap(monkeypatch, tmp_path):
    web, application, original = _isolated_app(monkeypatch, tmp_path)
    collected_profiles = []

    def collect(browser, config, *, silent):
        collected_profiles.append(config.chrome_profile_directory)
        return (
            {
                "platform": "chatgpt",
                "browser": browser,
                "browser_label": "Chrome",
                "logged_in": True,
                "can_download": True,
                "account_name": config.chrome_profile_directory,
                "message": "Ready",
            },
            {
                "platform": "chatgpt",
                "recent_sessions": [],
                "projects": [],
                "limit": 20,
            },
        )

    monkeypatch.setattr(web, "probe_and_collect_chatgpt_sources", collect)
    endpoint = "/api/browser-session?platform=chatgpt&browser=chrome&scope=agent"
    with application.test_client() as client:
        first = client.get(endpoint)
        assert first.status_code == 200
        assert first.get_json()["browser_session_freshness"]["kind"] == "live_browser"
        saved = client.post(
            "/settings",
            data={
                "chrome_user_data_dir": str(tmp_path / "Data B"),
                "chrome_profile_directory": "Profile B",
            },
        )
        assert saved.status_code == 302
        second = client.get(endpoint)
        assert second.status_code == 200
        payload = second.get_json()
        assert payload["account_name"] == "Profile B"
        assert payload["browser_session_freshness"]["kind"] == "live_browser"
        assert payload["profile_identity"] != first.get_json()["profile_identity"]
        assert collected_profiles == [original.chrome_profile_directory, "Profile B"]

def test_profile_cache_preserves_inflight_request_identity(monkeypatch, tmp_path):
    web, application, _original = _isolated_app(monkeypatch, tmp_path)
    entered = Event()
    release = Event()
    responses = []

    def collect(browser, config, *, silent):
        if config.chrome_profile_directory == "Profile A":
            entered.set()
            assert release.wait(5)
        return ({"platform": "chatgpt", "browser": browser, "can_download": True,
                 "logged_in": True, "account_name": config.chrome_profile_directory}, None)

    monkeypatch.setattr(web, "probe_and_collect_chatgpt_sources", collect)
    endpoint = "/api/browser-session?platform=chatgpt&browser=chrome&scope=agent"

    def request_a():
        with application.test_client() as client:
            responses.append(client.get(endpoint).get_json())

    pending = Thread(target=request_a)
    pending.start()
    try:
        assert entered.wait(5)
        with application.test_client() as client:
            assert client.post("/settings", data={"chrome_profile_directory": "Profile B"}).status_code == 302
            current = client.get(endpoint).get_json()
            assert current["account_name"] == "Profile B"
        release.set()
        pending.join(5)
        assert not pending.is_alive()
        assert responses[0]["account_name"] == "Profile A"
        assert responses[0]["profile_identity"] != current["profile_identity"]
        with application.test_client() as client:
            assert client.get(endpoint).get_json()["account_name"] == "Profile B"
    finally:
        release.set()
        pending.join(5)


def test_task_handoff_retains_profile_after_settings_change_and_restart(monkeypatch, tmp_path):
    from app.core import computer_use_agent as agent
    from app.core.computer_use_agent import ComputerUseAgentService, ComputerUseSettingsStore

    web, application, original = _isolated_app(monkeypatch, tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = application.extensions["computer_use_agent_service"]
    monkeypatch.setattr(agent, "_start_macos_idle_sleep_assertion", lambda: None)
    captured = []

    def runner(**kwargs):
        captured.append(kwargs["config"])
        return "Synthetic completed task", "https://chatgpt.com/c/profile-bound", 1, True

    service._runner = runner
    opener = Mock(return_value={"opened": True})
    monkeypatch.setattr(web, "open_agent_in_browser", opener)
    with application.test_client() as client:
        started = client.post("/api/agent/ask", json={"prompt": "Synthetic task", "workspace_path": str(workspace),
                                                   "browser": "chrome", "platform": "chatgpt",
                                                   "profile_identity": web.browser_profile_cache_identity("chrome", original)})
        assert started.status_code == 202
        assert service._worker is not None
        service._worker.join(5)
        assert not service._worker.is_alive()
        binding = service.snapshot()["browser_profile_binding"]
        assert binding["profile_directory"] == "Profile A"
        assert client.post("/settings", data={"chrome_user_data_dir": str(tmp_path / "Data B"),
                                               "chrome_profile_directory": "Profile B"}).status_code == 302
        assert client.post("/api/agent/open-conversation", json={}).status_code == 200
    assert captured[0] is not original
    assert opener.call_args.kwargs["config"].chrome_profile_directory == "Profile B"
    assert opener.call_args.kwargs["profile_binding"] == binding
    target = agent._bound_browser_descriptor("chrome", opener.call_args.kwargs["config"], binding)
    assert target.profile_directory == "Profile A"
    assert target.user_data_dir == original.chrome_user_data_dir.resolve()
    restored = ComputerUseAgentService(ComputerUseSettingsStore(tmp_path / "restore-settings.json"),
                                      runner=runner, runtime_root=service._runtime_root)
    assert restored.snapshot()["browser_profile_binding"] == binding


def test_legacy_task_handoff_refuses_missing_profile_proof(monkeypatch, tmp_path):
    web, application, _original = _isolated_app(monkeypatch, tmp_path)
    service = application.extensions["computer_use_agent_service"]
    snapshot = service.snapshot()
    snapshot.update(run_id="legacy-run", browser="chrome", platform="chatgpt",
                    conversation_url="https://chatgpt.com/c/legacy-profile")
    monkeypatch.setattr(service, "snapshot", lambda: snapshot)
    opener = Mock()
    monkeypatch.setattr(web, "open_agent_in_browser", opener)
    with application.test_client() as client:
        response = client.post("/api/agent/open-conversation", json={})
    assert response.status_code == 409
    assert "profile binding" in response.get_json()["error"]
    opener.assert_not_called()


@pytest.mark.parametrize("profile", ["../../outside", r"..\outside", r"C:\outside"])
def test_invalid_profile_settings_are_rejected_before_save(monkeypatch, tmp_path, profile):
    web, application, _original = _isolated_app(monkeypatch, tmp_path)
    save = Mock()
    monkeypatch.setattr(web, "save_config", save)
    with application.test_client() as client:
        response = client.post("/settings", data={"chrome_profile_directory": profile})
    assert response.status_code == 400
    save.assert_not_called()


def test_profile_partition_survives_catalog_reload(tmp_path):
    from app.core.agent_source_cache import AgentSourceCache

    store = AgentSourceCache(tmp_path / "catalog")
    store.store(platform="chatgpt", browser="chrome", source_kind="sources", profile_identity="profile-a",
                payload={"account_name": "A"})
    reloaded = AgentSourceCache(tmp_path / "catalog")
    collect_b = Mock(return_value={"account_name": "B"})
    assert reloaded.get_or_collect(platform="chatgpt", browser="chrome", source_kind="sources",
                                   profile_identity="profile-b", collector=collect_b)["account_name"] == "B"
    collect_b.assert_called_once()
    final = AgentSourceCache(tmp_path / "catalog")
    unused = Mock(side_effect=AssertionError("Profile A's own cache should remain available."))
    assert final.get_or_collect(platform="chatgpt", browser="chrome", source_kind="sources",
                                profile_identity="profile-a", collector=unused)["account_name"] == "A"
    unused.assert_not_called()


@pytest.mark.parametrize("proof", ("missing", "stale", "current"))
def test_ask_requires_the_profile_identity_that_was_verified_before_submission(monkeypatch, tmp_path, proof):
    web, application, original = _isolated_app(monkeypatch, tmp_path)
    pool = application.extensions["agent_session_pool"]
    start = Mock(return_value="primary")
    monkeypatch.setattr(pool, "start", start)
    verified_a = web.browser_profile_cache_identity("chrome", original)
    with application.test_client() as client:
        assert client.post("/settings", data={"chrome_profile_directory": "Profile B"}).status_code == 302
        current = client.get("/api/agent/status").get_json()["runtime"]["browser_profile_identities"]["chrome"]
        request = {"prompt": "A synthetic task", "workspace_path": str(tmp_path), "browser": "chrome"}
        if proof != "missing":
            request["profile_identity"] = verified_a if proof == "stale" else current
        result = client.post("/api/agent/ask", json=request)
    if proof == "current":
        assert result.status_code == 202
        start.assert_called_once()
        captured = start.call_args.args[3]
        assert captured.chrome_profile_directory == "Profile B"
        assert web.browser_profile_cache_identity("chrome", captured) == current
    else:
        assert result.status_code == 409
        assert result.get_json()["code"] == "browser_profile_changed"
        start.assert_not_called()
        assert application.extensions["computer_use_agent_service"]._worker is None


def test_ask_compares_and_starts_with_one_configuration_snapshot(monkeypatch, tmp_path):
    web, application, original = _isolated_app(monkeypatch, tmp_path)
    pool = application.extensions["agent_session_pool"]
    observed = []

    def start(*args, **kwargs):
        # A concurrent settings change after admission cannot replace its config.
        with application.test_client() as other_client:
            assert other_client.post("/settings", data={"chrome_profile_directory": "Profile B"}).status_code == 302
        observed.append(args[3])
        return "primary"

    monkeypatch.setattr(pool, "start", start)
    with application.test_client() as client:
        result = client.post("/api/agent/ask", json={
            "prompt": "A synthetic task", "workspace_path": str(tmp_path), "browser": "chrome",
            "profile_identity": web.browser_profile_cache_identity("chrome", original),
        })
    assert result.status_code == 202
    assert observed[0] is not original
    assert observed[0].chrome_profile_directory == "Profile A"


@pytest.mark.parametrize("next_selection", ("old-profile-change", "legacy-binding", "same-profile", "new-session"))
def test_managed_session_followup_preserves_its_original_profile(monkeypatch, tmp_path, next_selection):
    from app.core import computer_use_agent as agent

    web, application, original = _isolated_app(monkeypatch, tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    service = application.extensions["computer_use_agent_service"]
    pool = application.extensions["agent_session_pool"]
    observed = []
    monkeypatch.setattr(agent, "_start_macos_idle_sleep_assertion", lambda: None)

    def runner(**kwargs):
        observed.append(kwargs["config"].chrome_profile_directory)
        kwargs["update"](conversation_bound=True)
        return "Synthetic completed task", "https://chatgpt.com/c/profile-bound", 1, True

    service._runner = runner
    original_identity = web.browser_profile_cache_identity("chrome", original)
    base_request = {
        "prompt": "Synthetic task", "workspace_path": str(workspace),
        "browser": "chrome", "platform": "chatgpt", "profile_identity": original_identity,
    }
    with application.test_client() as client:
        assert client.post("/api/agent/ask", json=base_request).status_code == 202
        service._worker.join(5)
        assert not service._worker.is_alive()
        prior_run = service.snapshot()["run_id"]
        if next_selection == "legacy-binding":
            service._snapshot.browser_profile_binding = {}
        if next_selection in {"old-profile-change", "new-session"}:
            assert client.post("/settings", data={"chrome_profile_directory": "Profile B"}).status_code == 302
        current = client.get("/api/agent/status").get_json()["runtime"]["browser_profile_identities"]["chrome"]
        next_request = {
            **base_request, "profile_identity": current, "session_mode": "recent",
            "conversation_url": "https://chatgpt.com/c/profile-bound",
        }
        headers = {}
        if next_selection == "new-session":
            headers["X-CacheLikes-Agent-Session"] = "new"
            next_request.update(session_mode="new", conversation_url="")
        response = client.post("/api/agent/ask", headers=headers, json=next_request)
        if next_selection in {"old-profile-change", "legacy-binding"}:
            assert response.status_code == 409
            assert response.get_json()["code"] == "browser_profile_changed"
            assert service.snapshot()["run_id"] == prior_run
            assert observed == ["Profile A"]
        else:
            assert response.status_code == 202
            selected = pool.get(response.get_json()["agent"]["session_id"])
            selected._worker.join(5)
            assert not selected._worker.is_alive()
            assert observed == ["Profile A", "Profile B" if next_selection == "new-session" else "Profile A"]
    pool.stop_at_exit()
