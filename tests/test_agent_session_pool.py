"""Concurrent session admission and independent lifecycle checks. Code version: v1.0.3-codex.1."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
import time

import pytest

from app.core.agent.session_pool import AgentSessionPool
from app.core.computer_use_agent import ComputerUseAgentService, ComputerUseSettingsStore, default_model_for_platform
from app.core.foundation import CrawlConfig


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.computer_use_agent._start_macos_idle_sleep_assertion", lambda: None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("Test workspace", encoding="utf-8")
    entered = {}
    lock = Lock()

    def runner(**kwargs):
        release = Event()
        with lock:
            entered[kwargs["prompt"]] = (release, kwargs)
        kwargs["update"](phase="running", message=kwargs["prompt"])
        while not release.wait(0.01) and not kwargs["should_stop"]():
            pass
        return kwargs["prompt"], f'https://chatgpt.com/c/{kwargs["prompt"]}', 1, True

    primary = ComputerUseAgentService(
        ComputerUseSettingsStore(tmp_path / "settings.json"), runner=runner, runtime_root=tmp_path / "runtime"
    )
    pool = AgentSessionPool(primary)
    yield pool, workspace, entered
    pool.stop_at_exit()


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def start(pool, workspace, prompt, **kwargs):
    return pool.start("new", prompt, str(workspace), CrawlConfig(), browser="edge", platform="chatgpt", **kwargs)


def test_atomic_two_worker_limit_and_independent_stop(sessions):
    pool, workspace, entered = sessions
    barrier = Barrier(3)

    def submit(index):
        barrier.wait()
        try:
            return start(pool, workspace, f"task-{index}")
        except RuntimeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(submit, range(3)))
    ids = [item for item in results if len(item) == 32]
    assert len(ids) == 2
    assert sum("2 of 2" in item for item in results) == 1
    wait_until(lambda: len(entered) == 2)
    first, second = [pool.get(key) for key in ids]
    first_snapshot, second_snapshot = first.snapshot(), second.snapshot()
    assert first_snapshot["context_file"] != second_snapshot["context_file"]
    assert first_snapshot["run_id"] != second_snapshot["run_id"]
    assert first.request_stop()
    wait_until(lambda: not first.snapshot()["running"])
    assert second.snapshot()["running"]
    assert not entered[second_snapshot["prompt"]][1]["should_stop"]()
    third_id = start(pool, workspace, "replacement")
    assert pool.get(third_id).snapshot()["running"]
    entered[second_snapshot["prompt"]][0].set()
    wait_until(lambda: not second.snapshot()["running"])
    assert second.snapshot()["response"] == second_snapshot["prompt"]
    assert first.snapshot()["history"] != second.snapshot()["history"]


def test_same_conversation_rejected_and_paused_slot_counted(sessions):
    pool, workspace, entered = sessions
    url = "https://chatgpt.com/c/existing"
    first_id = start(pool, workspace, "first", session_mode="recent", conversation_url=url)
    wait_until(lambda: "first" in entered)
    with pytest.raises(RuntimeError, match="conversation already"):
        start(pool, workspace, "duplicate", session_mode="recent", conversation_url=url + "?view=1")
    entered["first"][1]["update"](paused=True, phase="paused")
    start(pool, workspace, "second")
    with pytest.raises(RuntimeError, match="2 of 2"):
        start(pool, workspace, "third")
    assert pool.get(first_id).snapshot()["paused"]
    catalog = pool.catalog("edge", "chatgpt", str(workspace))
    assert catalog["active_count"] == 2
    assert catalog["can_start"] is False
    assert len(catalog["sessions"]) == 2
    assert len(pool.catalog("edge", "chatgpt", "/unrelated")["sessions"]) == 2
    assert all(item["workspace_path"] == str(workspace) for item in catalog["sessions"])
    assert pool.catalog("edge", "gemini", str(workspace))["sessions"] == []


def test_guard_also_covers_direct_start_and_other_providers(sessions):
    pool, workspace, _ = sessions
    start(pool, workspace, "first")
    with pytest.raises(RuntimeError, match="only for ChatGPT in Edge"):
        pool.get().start("other", str(workspace), CrawlConfig(), browser="edge", platform="gemini", model=default_model_for_platform("gemini"))
    start(pool, workspace, "second")
    with pytest.raises(RuntimeError, match="2 of 2"):
        pool.get().start("bypass", str(workspace), CrawlConfig(), browser="edge", platform="chatgpt")


def test_failure_frees_capacity_and_shutdown_rejects_new_work(sessions, monkeypatch):
    pool, workspace, _ = sessions
    with monkeypatch.context() as patch:
        patch.setattr("app.core.computer_use_agent.Thread.start", lambda _self: (_ for _ in ()).throw(RuntimeError("startup failed")))
        with pytest.raises(RuntimeError, match="startup failed"):
            start(pool, workspace, "failed")
    assert pool.catalog("edge", "chatgpt", str(workspace))["active_count"] == 0
    start(pool, workspace, "first")
    start(pool, workspace, "second")
    pool.stop_at_exit()
    with pytest.raises(RuntimeError, match="shutting down"):
        start(pool, workspace, "late")


def test_completed_sessions_restore_as_independent_metadata(sessions):
    pool, workspace, entered = sessions
    key = start(pool, workspace, "persisted")
    wait_until(lambda: "persisted" in entered)
    entered["persisted"][0].set()
    wait_until(lambda: not pool.get(key).snapshot()["running"])
    pool.stop_at_exit()
    primary = pool.get()
    restored = AgentSessionPool(ComputerUseAgentService(primary._settings_store, runner=primary._runner, runtime_root=primary._runtime_root))
    try:
        assert restored.get(key).snapshot()["run_id"] == pool.get(key).snapshot()["run_id"]
        assert restored.get(key).snapshot()["running"] is False
        with pytest.raises(ValueError):
            restored.get("../escape")
    finally:
        restored.stop_at_exit()


def test_api_targets_only_selected_session_and_rejects_unknown(tmp_path, monkeypatch):
    from app.web.app import create_app

    monkeypatch.setattr("app.core.computer_use_agent._start_macos_idle_sleep_assertion", lambda: None)
    application = create_app(tmp_path / "store", computer_use_settings_path=tmp_path / "settings.json", computer_use_runtime_root=tmp_path / "runtime")
    application.config["TESTING"] = True
    pool = application.extensions["agent_session_pool"]
    release = Event()

    def runner(**kwargs):
        kwargs["update"](phase="running", message=kwargs["prompt"])
        while not release.wait(0.01) and not kwargs["should_stop"]():
            pass
        return kwargs["prompt"], "https://chatgpt.com/c/result", 1, True

    pool.get()._runner = runner
    client = application.test_client()
    headers = {"X-CacheLikes-Agent-Session": "new", "X-CacheLikes-Agent-Browser": "edge", "X-CacheLikes-Agent-Platform": "chatgpt", "X-CacheLikes-Agent-Workspace": str(tmp_path)}
    try:
        ids = []
        profile_identity = client.get("/api/agent/status", headers=headers).json["runtime"]["browser_profile_identities"]["edge"]
        for prompt in ("first", "second"):
            response = client.post("/api/agent/ask", headers=headers, json={"prompt": prompt, "workspace_path": str(tmp_path), "browser": "edge", "platform": "chatgpt", "profile_identity": profile_identity})
            assert response.status_code == 202
            ids.append(response.json["agent"]["session_id"])
        third = client.post("/api/agent/ask", headers=headers, json={"prompt": "third", "workspace_path": str(tmp_path), "profile_identity": profile_identity})
        assert third.status_code == 409
        assert "2 of 2" in third.json["error"]
        for key, prompt in zip(ids, ("first", "second")):
            payload = client.get("/api/agent/status", headers={**headers, "X-CacheLikes-Agent-Session": key}).json
            assert payload["agent"]["prompt"] == prompt
            assert payload["active_count"] == 2
            assert len(payload["sessions"]) == 2
        assert client.post("/api/agent/stop", headers={**headers, "X-CacheLikes-Agent-Session": ids[0]}).json["stop_requested"]
        wait_until(lambda: not pool.get(ids[0]).snapshot()["running"])
        assert pool.get(ids[1]).snapshot()["running"]
        assert client.post("/api/agent/stop", headers={**headers, "X-CacheLikes-Agent-Session": "unknown"}).status_code == 404
        assert pool.get(ids[1]).snapshot()["running"]
        unknown = client.get("/api/agent/status", headers={**headers, "X-CacheLikes-Agent-Session": "expired"})
        assert unknown.status_code == 404
        assert unknown.json["code"] == "unknown_agent_session"
        assert not client.get("/api/agent/status", headers=headers).json["agent"].get("running")
    finally:
        release.set()
        pool.stop_at_exit()


@pytest.mark.parametrize("browser,platform", [("edge", "chatgpt"), ("edge", "gemini"), ("chrome", "chatgpt")])
def test_catalog_capacity_matches_atomic_admission(sessions, browser, platform):
    pool, workspace, _ = sessions
    first = pool.start("new", "first", str(workspace), CrawlConfig(), browser=browser,
                       platform=platform, model=default_model_for_platform(platform))
    for candidate_browser, candidate_platform in (("edge", "chatgpt"), ("edge", "gemini"), ("chrome", "chatgpt")):
        catalog = pool.catalog(candidate_browser, candidate_platform, str(workspace))
        allowed = (browser, platform) == (candidate_browser, candidate_platform) == ("edge", "chatgpt")
        assert catalog["can_start"] is allowed
        if not allowed:
            with pytest.raises(RuntimeError, match="only for ChatGPT in Edge"):
                pool.start("new", "blocked", str(workspace), CrawlConfig(), browser=candidate_browser,
                           platform=candidate_platform, model=default_model_for_platform(candidate_platform))
    pool.get(first).request_stop()
    wait_until(lambda: not pool.get(first).snapshot()["running"])
    assert pool.catalog("edge", "gemini", str(workspace))["can_start"]
    before = pool.get(first).snapshot()
    other_platform = "gemini" if platform == "chatgpt" else "chatgpt"
    with pytest.raises(RuntimeError, match="another browser or provider"):
        pool.start(first, "wrong route", str(workspace), CrawlConfig(), browser="edge",
                   platform=other_platform, model=default_model_for_platform(other_platform))
    assert pool.get(first).snapshot() == before


def test_session_catalog_preserves_project_filter_metadata(sessions):
    pool, workspace, entered = sessions
    project = 'https://chatgpt.com/g/g-p-demo/project'
    session_id = start(pool, workspace, 'project-scope', session_mode='project_new', project_url=project)
    wait_until(lambda: 'project-scope' in entered)
    catalog = pool.catalog('edge', 'chatgpt', str(workspace))
    item = next(item for item in catalog['sessions'] if item['session_id'] == session_id)
    assert item['project_url'] == project
