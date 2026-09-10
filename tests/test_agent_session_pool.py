"""Concurrent session admission and independent lifecycle checks. Code version: v1.6.0-codex.1."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from threading import Barrier, Event, Lock
import time

import pytest

from app.core.agent import compute_jobs
from app.core.agent.session_pool import AgentSessionPool
from app.core.computer_use_agent import ComputerUseAgentService, ComputerUseSettingsStore, default_model_for_platform
from app.core.foundation import CrawlConfig


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.computer_use_agent._start_macos_idle_sleep_assertion", lambda: None)
    monkeypatch.setattr("app.core.agent.session_pool.is_windows_host", lambda: False)
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


def sibling_workspace(workspace, name):
    candidate = workspace.parent / name
    candidate.mkdir(exist_ok=True)
    (candidate / "README.md").write_text("Test workspace", encoding="utf-8")
    return candidate


def write_compute_job_metadata(pool, workspace, *, raw_text=""):
    resolved = workspace.resolve()
    workspace_key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:24]
    job_id = "a" * 32
    job_root = pool.get()._runtime_root / "compute-jobs" / workspace_key / job_id
    job_root.mkdir(parents=True)
    metadata_path = job_root / "metadata.json"
    if raw_text:
        metadata_path.write_text(raw_text, encoding="utf-8")
        return metadata_path
    workspace_metadata = resolved.stat()
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job_id,
                "workspace": str(resolved),
                "workspace_device": int(workspace_metadata.st_dev),
                "workspace_inode": int(workspace_metadata.st_ino),
                "state": "running",
                "pid": 123,
                "process_identity": "test-worker-birth",
                "containment_kind": compute_jobs._platform_compute_containment_kind(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return metadata_path


def test_atomic_two_worker_limit_and_independent_stop(sessions):
    pool, workspace, entered = sessions
    barrier = Barrier(3)
    workspaces = [
        workspace,
        sibling_workspace(workspace, "workspace-two"),
        sibling_workspace(workspace, "workspace-three"),
    ]

    def submit(index):
        barrier.wait()
        try:
            return start(pool, workspaces[index], f"task-{index}")
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
    third_id = start(pool, Path(first_snapshot["workspace_path"]), "replacement")
    assert pool.get(third_id).snapshot()["running"]
    entered[second_snapshot["prompt"]][0].set()
    wait_until(lambda: not second.snapshot()["running"])
    assert second.snapshot()["response"] == second_snapshot["prompt"]
    assert first.snapshot()["history"] != second.snapshot()["history"]


def test_same_conversation_rejected_and_paused_slot_counted(sessions):
    pool, workspace, entered = sessions
    second_workspace = sibling_workspace(workspace, "workspace-two")
    url = "https://chatgpt.com/c/existing"
    first_id = start(pool, workspace, "first", session_mode="recent", conversation_url=url)
    wait_until(lambda: "first" in entered)
    with pytest.raises(RuntimeError, match="conversation already"):
        start(pool, workspace, "duplicate", session_mode="recent", conversation_url=url + "?view=1")
    entered["first"][1]["update"](paused=True, phase="paused")
    start(pool, second_workspace, "second")
    with pytest.raises(RuntimeError, match="2 of 2"):
        start(pool, workspace, "third")
    assert pool.get(first_id).snapshot()["paused"]
    catalog = pool.catalog("edge", "chatgpt", str(workspace))
    assert catalog["active_count"] == 2
    assert catalog["can_start"] is False
    assert len(catalog["sessions"]) == 1
    assert pool.catalog("edge", "chatgpt", "/unrelated")["sessions"] == []
    assert {item["workspace_path"] for item in catalog["sessions"]} == {str(workspace)}
    assert pool.catalog("edge", "gemini", str(workspace))["sessions"] == []


def test_guard_also_covers_direct_start_and_other_providers(sessions):
    pool, workspace, _ = sessions
    second_workspace = sibling_workspace(workspace, "workspace-two")
    start(pool, workspace, "first")
    with pytest.raises(RuntimeError, match="only for ChatGPT in Edge"):
        pool.get().start("other", str(workspace), CrawlConfig(), browser="edge", platform="gemini", model=default_model_for_platform("gemini"))
    start(pool, second_workspace, "second")
    with pytest.raises(RuntimeError, match="2 of 2"):
        pool.get().start("bypass", str(workspace), CrawlConfig(), browser="edge", platform="chatgpt")


def test_has_active_worker_reflects_running_state(sessions):
    pool, workspace, entered = sessions
    assert pool.has_active_worker("edge", "chatgpt") is False
    first_id = start(pool, workspace, "first")
    wait_until(lambda: "first" in entered)
    assert pool.has_active_worker("edge") is True
    assert pool.has_active_worker("edge", "chatgpt") is True
    assert pool.has_active_worker("chrome", "chatgpt") is False
    assert pool.has_active_worker("edge", "gemini") is False
    pool.get(first_id).request_stop()
    wait_until(lambda: not pool.get(first_id).snapshot()["running"])
    assert pool.has_active_worker("edge", "chatgpt") is False


def test_windows_debug_browser_admits_only_one_worker(sessions, monkeypatch):
    pool, workspace, entered = sessions
    monkeypatch.setattr("app.core.agent.session_pool.is_windows_host", lambda: True)
    start(pool, workspace, "first")
    wait_until(lambda: "first" in entered)

    with pytest.raises(RuntimeError, match="one active Agent task"):
        start(pool, workspace, "second")

    catalog = pool.catalog("edge", "chatgpt", str(workspace))
    assert catalog["concurrency_limit"] == 1
    assert catalog["active_count"] == 1
    assert catalog["can_start"] is False


def test_failure_frees_capacity_and_shutdown_rejects_new_work(sessions, monkeypatch):
    pool, workspace, _ = sessions
    second_workspace = sibling_workspace(workspace, "workspace-two")
    with monkeypatch.context() as patch:
        patch.setattr("app.core.computer_use_agent.Thread.start", lambda _self: (_ for _ in ()).throw(RuntimeError("startup failed")))
        with pytest.raises(RuntimeError, match="startup failed"):
            start(pool, workspace, "failed")
    assert pool.catalog("edge", "chatgpt", str(workspace))["active_count"] == 0
    start(pool, workspace, "first")
    start(pool, second_workspace, "second")
    pool.stop_at_exit()
    with pytest.raises(RuntimeError, match="shutting down"):
        start(pool, workspace, "late")


def test_failed_session_can_be_dismissed_only_after_remote_absence_confirmation(sessions):
    pool, workspace, _ = sessions

    def failing_runner(**_kwargs):
        raise RuntimeError("provider rejected the task before creating a record")

    pool.get()._runner = failing_runner
    session_id = start(pool, workspace, "failed-before-bind")
    wait_until(lambda: pool.get(session_id).snapshot()["phase"] == "failed")
    conversation_url = pool.get(session_id).snapshot()["conversation_url"]
    record_path = pool.get(session_id)._runtime_root / "last-run.json"
    assert record_path.is_file()

    with pytest.raises(RuntimeError, match="Confirm that the remote conversation record is absent"):
        pool.dismiss_failed(
            session_id,
            expected_conversation_url=conversation_url,
            remote_record_absent=False,
        )

    dismissed = pool.dismiss_failed(
        session_id,
        expected_conversation_url=conversation_url,
        remote_record_absent=True,
    )
    assert dismissed["session_id"] == session_id
    assert dismissed["conversation_url"] == conversation_url
    assert not record_path.exists()
    with pytest.raises(ValueError, match="unavailable"):
        pool.get(session_id)
    assert pool.catalog("edge", "chatgpt", str(workspace))["sessions"] == []


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
    workspaces = [tmp_path / "workspace-one", tmp_path / "workspace-two", tmp_path / "workspace-three"]
    for workspace in workspaces:
        workspace.mkdir()
    try:
        ids = []
        for prompt, workspace in zip(("first", "second"), workspaces[:2]):
            response = client.post("/api/agent/ask", headers={**headers, "X-CacheLikes-Agent-Workspace": str(workspace)}, json={"prompt": prompt, "workspace_path": str(workspace), "browser": "edge", "platform": "chatgpt"})
            assert response.status_code == 202
            ids.append(response.json["agent"]["session_id"])
        assert client.post("/api/agent/ask", headers={**headers, "X-CacheLikes-Agent-Workspace": str(workspaces[2])}, json={"prompt": "third", "workspace_path": str(workspaces[2])}).status_code == 409
        for key, prompt, workspace in zip(ids, ("first", "second"), workspaces[:2]):
            payload = client.get("/api/agent/status", headers={
                **headers,
                "X-CacheLikes-Agent-Session": key,
                "X-CacheLikes-Agent-Workspace": str(workspace),
            }).json
            assert payload["agent"]["prompt"] == prompt
            assert payload["active_count"] == 2
            assert len(payload["sessions"]) == 1
            assert payload["sessions"][0]["session_id"] == key
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
    candidate_workspace = sibling_workspace(workspace, "candidate-workspace")
    first = pool.start("new", "first", str(workspace), CrawlConfig(), browser=browser,
                       platform=platform, model=default_model_for_platform(platform))
    for candidate_browser, candidate_platform in (("edge", "chatgpt"), ("edge", "gemini"), ("chrome", "chatgpt")):
        catalog = pool.catalog(candidate_browser, candidate_platform, str(candidate_workspace))
        allowed = (browser, platform) == (candidate_browser, candidate_platform) == ("edge", "chatgpt")
        assert catalog["can_start"] is allowed
        if not allowed:
            with pytest.raises(RuntimeError, match="only for ChatGPT in Edge"):
                pool.start("new", "blocked", str(candidate_workspace), CrawlConfig(), browser=candidate_browser,
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


def test_same_workspace_allows_readers_but_rejects_a_second_writer(sessions):
    pool, workspace, entered = sessions
    first = start(pool, workspace, "writer")
    wait_until(lambda: "writer" in entered)

    with pytest.raises(RuntimeError, match="active write-capable Agent task"):
        start(pool, workspace, "second-writer")

    reader = start(pool, workspace, "reader", read_only=True)
    wait_until(lambda: "reader" in entered)
    assert pool.get(first).snapshot()["read_only"] is False
    assert pool.get(reader).snapshot()["read_only"] is True


def test_two_read_only_tasks_may_share_one_workspace(sessions):
    pool, workspace, entered = sessions
    first = start(pool, workspace, "reader-one", read_only=True)
    second = start(pool, workspace, "reader-two", read_only=True)
    wait_until(lambda: len(entered) == 2)
    assert pool.get(first).snapshot()["running"]
    assert pool.get(second).snapshot()["running"]


def test_workspace_lease_uses_resolved_directory_identity(sessions):
    pool, workspace, entered = sessions
    alias = workspace.parent / "workspace-alias"
    try:
        alias.symlink_to(workspace, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this host.")
    start(pool, workspace, "writer")
    wait_until(lambda: "writer" in entered)

    with pytest.raises(RuntimeError, match="active write-capable Agent task"):
        start(pool, alias, "aliased-writer")

    catalog = pool.catalog("edge", "chatgpt", str(alias))
    assert catalog["can_start"] is False
    assert "write-capable" in catalog["start_blocked_reason"]
    assert pool.catalog("edge", "chatgpt", str(alias), read_only=True)["can_start"]


def test_workspace_lease_blocks_overlapping_parent_and_child_writers(sessions):
    pool, workspace, entered = sessions
    nested = workspace / "nested-project"
    nested.mkdir()
    start(pool, workspace, "parent-writer")
    wait_until(lambda: "parent-writer" in entered)

    with pytest.raises(RuntimeError, match="active write-capable Agent task"):
        start(pool, nested, "child-writer")

    reader = start(pool, nested, "child-reader", read_only=True)
    wait_until(lambda: "child-reader" in entered)
    assert pool.get(reader).snapshot()["read_only"] is True


def test_workspace_lease_fails_closed_when_active_path_cannot_be_verified(sessions):
    pool, workspace, entered = sessions
    start(pool, workspace, "original-writer")
    wait_until(lambda: "original-writer" in entered)
    original_identity = pool._workspace_identity(workspace)
    moved = workspace.with_name("workspace-moved")
    workspace.rename(moved)
    assert pool._workspace_identity(moved) == original_identity

    with pytest.raises(RuntimeError, match="could not be verified safely"):
        start(pool, moved, "renamed-writer")


def test_workspace_lease_refreshes_ancestors_after_parent_replacement(sessions):
    pool, workspace, entered = sessions
    parent = workspace.parent / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "README.md").write_text("Nested workspace", encoding="utf-8")
    start(pool, child, "child-writer")
    wait_until(lambda: "child-writer" in entered)

    old_parent = workspace.parent / "parent-old"
    parent.rename(old_parent)
    parent.mkdir()
    (old_parent / child.name).rename(child)

    catalog = pool.catalog("edge", "chatgpt", str(parent))
    assert catalog["can_start"] is False
    assert "write-capable" in catalog["start_blocked_reason"]
    with pytest.raises(RuntimeError, match="active write-capable Agent task"):
        start(pool, parent, "replacement-parent-writer")


def test_workspace_lease_blocks_lexical_overlap_across_chain_sampling_race(
    sessions,
    monkeypatch,
):
    pool, workspace, entered = sessions
    parent = workspace.parent / "race-parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "README.md").write_text("Nested workspace", encoding="utf-8")
    start(pool, child, "racing-child-writer")
    wait_until(lambda: "racing-child-writer" in entered)

    original_identity_chain = pool._workspace_identity_chain
    old_parent = workspace.parent / "race-parent-old"
    swapped = False

    def identity_chain_with_parent_swap(candidate):
        nonlocal swapped
        identities = original_identity_chain(candidate)
        if not swapped and Path(candidate) == child:
            swapped = True
            parent.rename(old_parent)
            parent.mkdir()
            (old_parent / child.name).rename(child)
        return identities

    monkeypatch.setattr(
        pool,
        "_workspace_identity_chain",
        identity_chain_with_parent_swap,
    )

    with pytest.raises(RuntimeError, match="active write-capable Agent task"):
        start(pool, parent, "racing-parent-writer")
    assert swapped is True
    assert "racing-parent-writer" not in entered


def test_worker_rejects_workspace_rebound_after_admission(sessions, monkeypatch):
    pool, workspace, entered = sessions
    deferred = []

    class DeferredThread:
        def __init__(self, *, target, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}
            self._alive = False
            deferred.append(self)

        def start(self):
            self._alive = True

        def run(self):
            try:
                self._target(*self._args, **self._kwargs)
            finally:
                self._alive = False

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            del timeout

    monkeypatch.setattr("app.core.computer_use_agent.Thread", DeferredThread)
    session_id = start(pool, workspace, "delayed-writer")
    assert len(deferred) == 1
    assert pool.get(session_id).snapshot()["running"] is True

    moved = workspace.with_name("workspace-before-rebind")
    workspace.rename(moved)
    workspace.mkdir()
    (workspace / "README.md").write_text("Replacement workspace", encoding="utf-8")
    catalog = pool.catalog("edge", "chatgpt", str(workspace))
    assert catalog["can_start"] is False
    assert "could not be verified safely" in catalog["start_blocked_reason"]
    deferred[0].run()

    snapshot = pool.get(session_id).snapshot()
    assert snapshot["running"] is False
    assert snapshot["phase"] == "failed"
    assert "workspace identity changed after admission" in snapshot["last_error"]
    assert "delayed-writer" not in entered


def test_workspace_lease_uses_ancestor_identity_for_case_variant_path(sessions):
    pool, workspace, entered = sessions
    nested = workspace / "nested-project"
    nested.mkdir()
    parts = list(workspace.parts)
    variant = None
    for index, part in enumerate(parts):
        if index == 0 or not any(character.isalpha() for character in part):
            continue
        candidate_parts = [*parts]
        candidate_parts[index] = part.swapcase()
        candidate = Path(*candidate_parts)
        try:
            if candidate.samefile(workspace) and str(candidate.resolve()) != str(workspace.resolve()):
                variant = candidate
                break
        except OSError:
            continue
    if variant is None:
        pytest.skip("The test filesystem does not preserve a case-variant directory alias.")
    start(pool, workspace, "canonical-writer")
    wait_until(lambda: "canonical-writer" in entered)

    with pytest.raises(RuntimeError, match="active write-capable Agent task"):
        start(pool, variant / nested.name, "case-variant-child-writer")


def test_active_compute_job_blocks_only_new_workspace_writers(
    sessions,
    monkeypatch,
):
    pool, workspace, entered = sessions
    monkeypatch.setattr(
        "app.core.agent.compute_jobs._process_identity",
        lambda _pid: "test-worker-birth",
    )
    write_compute_job_metadata(pool, workspace)

    with pytest.raises(RuntimeError, match="active durable compute job"):
        start(pool, workspace, "writer")

    reader = start(pool, workspace, "reader", read_only=True)
    wait_until(lambda: "reader" in entered)
    assert pool.get(reader).snapshot()["read_only"] is True
    assert not pool.catalog("edge", "chatgpt", str(workspace))["can_start"]
    assert pool.catalog(
        "edge",
        "chatgpt",
        str(workspace),
        read_only=True,
    )["can_start"]


@pytest.mark.parametrize("job_is_parent", [True, False])
def test_active_compute_job_blocks_parent_child_workspace_writers(
    sessions,
    job_is_parent,
    monkeypatch,
):
    pool, workspace, _ = sessions
    monkeypatch.setattr(
        "app.core.agent.compute_jobs._process_identity",
        lambda _pid: "test-worker-birth",
    )
    nested = workspace / "nested-project"
    nested.mkdir()
    job_workspace, candidate = (
        (workspace, nested)
        if job_is_parent
        else (nested, workspace)
    )
    write_compute_job_metadata(pool, job_workspace)

    with pytest.raises(RuntimeError, match="active durable compute job"):
        start(pool, candidate, "overlapping-writer")


def test_active_compute_job_identity_survives_workspace_rename(
    sessions,
    monkeypatch,
):
    pool, workspace, _ = sessions
    monkeypatch.setattr(
        "app.core.agent.compute_jobs._process_identity",
        lambda _pid: "test-worker-birth",
    )
    write_compute_job_metadata(pool, workspace)
    moved = workspace.with_name("workspace-moved")
    workspace.rename(moved)

    with pytest.raises(RuntimeError, match="active durable compute job"):
        start(pool, moved, "renamed-workspace-writer")


def test_stale_compute_job_is_reconciled_before_writer_admission(
    sessions,
    monkeypatch,
):
    pool, workspace, entered = sessions
    metadata_path = write_compute_job_metadata(pool, workspace)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["updated_at"] = "2026-01-01T00:00:00+00:00"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(
        "app.core.agent.compute_jobs._process_identity",
        lambda _pid: "",
    )

    writer = start(pool, workspace, "writer-after-stale-job")
    wait_until(lambda: "writer-after-stale-job" in entered)
    assert pool.get(writer).snapshot()["running"] is True
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["state"] == "interrupted"


def test_stale_compute_job_without_containment_provenance_fails_closed(
    sessions,
    monkeypatch,
):
    pool, workspace, _ = sessions
    metadata_path = write_compute_job_metadata(pool, workspace)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("containment_kind")
    metadata["updated_at"] = "2026-01-01T00:00:00+00:00"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(
        "app.core.agent.compute_jobs._process_identity",
        lambda _pid: "",
    )

    with pytest.raises(RuntimeError, match="metadata could not be verified"):
        start(pool, workspace, "writer-with-legacy-active-job")

    assert json.loads(metadata_path.read_text(encoding="utf-8"))["state"] == "running"


def test_unverifiable_live_compute_process_fails_closed_for_writers(
    sessions,
    monkeypatch,
):
    pool, workspace, _ = sessions
    metadata_path = write_compute_job_metadata(pool, workspace)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["pid"] = os.getpid()
    metadata["updated_at"] = "2026-01-01T00:00:00+00:00"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(
        "app.core.agent.compute_jobs._process_identity",
        lambda _pid: "",
    )

    with pytest.raises(RuntimeError, match="metadata could not be verified"):
        start(pool, workspace, "unverifiable-process-writer")


def test_corrupt_compute_job_metadata_fails_closed_for_writers(sessions):
    pool, workspace, entered = sessions
    write_compute_job_metadata(pool, workspace, raw_text="{broken")

    with pytest.raises(RuntimeError, match="metadata could not be verified"):
        start(pool, sibling_workspace(workspace, "other-workspace"), "writer")

    reader = start(pool, workspace, "reader", read_only=True)
    wait_until(lambda: "reader" in entered)
    assert pool.get(reader).snapshot()["read_only"] is True


def test_session_catalog_preserves_project_filter_metadata(sessions):
    pool, workspace, entered = sessions
    project = 'https://chatgpt.com/g/g-p-demo/project'
    session_id = start(pool, workspace, 'project-scope', session_mode='project_new', project_url=project)
    wait_until(lambda: 'project-scope' in entered)
    catalog = pool.catalog('edge', 'chatgpt', str(workspace))
    item = next(item for item in catalog['sessions'] if item['session_id'] == session_id)
    assert item['project_url'] == project
