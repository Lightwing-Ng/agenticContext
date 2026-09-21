"""Behavior tests for the shared workspace boundary and the split route modules.

These cover the risks the workspace extraction and the blueprint split could plausibly
break: path isolation, read-only refusal, read-receipt expiry, MCP response contracts,
route compatibility, application-instance isolation, and shutdown behavior.
"""

# Code version: v1.0.3-codex.0

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core.workspace import (
    WorkspaceAccess,
    is_withheld_workspace_path,
    open_workspace,
)
from app.core.workspace.capabilities import FileSnapshot, TextReplacement
from app.web.app import create_app


class _Settings:
    """The only setting a workspace controller reads."""

    command_timeout_seconds = 30


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "inner.txt").write_text("inner\n", encoding="utf-8")
    return root


@pytest.fixture
def access(project: Path) -> WorkspaceAccess:
    return open_workspace(project, _Settings(), lambda: False)


@pytest.fixture
def read_only_access(project: Path) -> WorkspaceAccess:
    return open_workspace(project, _Settings(), lambda: False, read_only=True)


# Boundary shape ------------------------------------------------------------


def test_open_workspace_returns_the_published_capability_surface(access: WorkspaceAccess) -> None:
    """A caller receives ``WorkspaceAccess``, so it cannot drift onto controller internals."""
    assert isinstance(access, WorkspaceAccess)
    assert access.read_only is False


# Path isolation ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_path",
    ["../outside.txt", "/etc/hosts", "nested/../../escape.txt"],
)
def test_paths_outside_the_project_root_are_refused(
    access: WorkspaceAccess, raw_path: str
) -> None:
    """Traversal and absolute paths never resolve outside the selected root."""
    with pytest.raises((ValueError, OSError)):
        access.project_relative_path(raw_path, allow_missing=True)


def test_admitted_paths_are_returned_relative_to_the_project_root(
    access: WorkspaceAccess, project: Path
) -> None:
    assert access.project_relative_path("nested/inner.txt") == "nested/inner.txt"
    assert access.project_relative_path("./app.py") == "app.py"
    assert not Path(access.project_relative_path("app.py")).is_absolute()
    assert (project / access.project_relative_path("app.py")).is_file()


def test_a_symlink_that_leaves_the_project_is_refused(
    access: WorkspaceAccess, project: Path, tmp_path: Path
) -> None:
    """Following a link out of the root would defeat every other path rule."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = project / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("This platform cannot create symbolic links.")
    with pytest.raises((ValueError, OSError, RuntimeError)):
        access.file_snapshot("escape.txt")


def test_credential_and_controller_internal_paths_stay_out_of_model_output() -> None:
    """One rule decides what is withheld, and both connections ask the same question."""
    assert is_withheld_workspace_path(Path("config/id_rsa"))
    assert is_withheld_workspace_path(Path("secrets/server.pem"))
    assert is_withheld_workspace_path(Path(".app.agent-backup-0123456789abcdef.tmp"))
    assert not is_withheld_workspace_path(Path("app.py"))


# Read-only refusal ---------------------------------------------------------


MUTATING_ACTIONS = (
    {"action": "write", "path": "new.txt", "content": "x\n"},
    {"action": "replace", "path": "app.py", "old": "hi", "new": "bye"},
    {"action": "delete", "path": "app.py", "expected_sha256": "0" * 64},
    {"action": "run", "command": "python -m pytest -q"},
    {"action": "browser_acceptance", "root": "."},
)


@pytest.mark.parametrize("payload", MUTATING_ACTIONS, ids=lambda item: item["action"])
def test_a_read_only_workspace_refuses_every_mutating_action(
    read_only_access: WorkspaceAccess, payload: dict
) -> None:
    observation = read_only_access.execute(dict(payload))
    assert observation["ok"] is False
    assert "read-only" in observation["error"]


def test_a_read_only_workspace_still_reads(read_only_access: WorkspaceAccess) -> None:
    observation = read_only_access.execute({"action": "read", "path": "app.py"})
    assert observation["ok"] is True


def test_a_read_only_workspace_refuses_boundary_writes(
    read_only_access: WorkspaceAccess, project: Path
) -> None:
    """The public capability methods obey the same read-only rule as the action protocol."""
    with pytest.raises((RuntimeError, ValueError, OSError)):
        read_only_access.create_file("new.txt", b"data\n")
    assert not (project / "new.txt").exists()


# Read receipts -------------------------------------------------------------


def test_a_never_read_file_gains_no_synthesized_receipt(access: WorkspaceAccess) -> None:
    """Refreshing a receipt the caller never held must be a no-op, not a grant."""
    digest = hashlib.sha256((b"print('hi')\n")).hexdigest()
    assert access.refresh_stale_read_receipt("app.py", expected_sha256=digest) is False


def test_a_current_receipt_is_not_refreshed(access: WorkspaceAccess) -> None:
    observation = access.execute({"action": "read", "path": "app.py"})
    assert observation["ok"] is True
    assert access.refresh_stale_read_receipt("app.py", expected_sha256=observation["sha256"]) is False


def test_a_current_receipt_returns_the_same_file_snapshot(access: WorkspaceAccess) -> None:
    observation = access.execute({"action": "read", "path": "app.py"})
    snapshot = access.current_read_receipt_snapshot(
        "app.py",
        expected_sha256=observation["sha256"],
    )
    assert snapshot.as_text() == "print('hi')\n"
    assert snapshot.sha256 == observation["sha256"]


def test_a_correct_digest_without_a_read_receipt_is_rejected(
    access: WorkspaceAccess,
) -> None:
    digest = hashlib.sha256(b"print('hi')\n").hexdigest()
    with pytest.raises(ValueError, match="read the current file first"):
        access.current_read_receipt_snapshot("app.py", expected_sha256=digest)


def test_an_externally_changed_file_expires_its_read_receipt(
    access: WorkspaceAccess,
    project: Path,
) -> None:
    observation = access.execute({"action": "read", "path": "app.py"})
    (project / "app.py").write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no longer matches"):
        access.current_read_receipt_snapshot(
            "app.py",
            expected_sha256=observation["sha256"],
        )


def test_an_aged_receipt_with_the_same_digest_is_refreshed(
    access: WorkspaceAccess, project: Path
) -> None:
    """An unrelated local edit ages every receipt; the caller's own digest still stands."""
    observation = access.execute({"action": "read", "path": "app.py"})
    access.create_file("unrelated.txt", b"new\n")
    assert access.refresh_stale_read_receipt("app.py", expected_sha256=observation["sha256"]) is True


def test_an_aged_receipt_with_a_different_digest_is_not_refreshed(
    access: WorkspaceAccess, project: Path
) -> None:
    observation = access.execute({"action": "read", "path": "app.py"})
    assert observation["ok"] is True
    (project / "app.py").write_text("print('changed')\n", encoding="utf-8")
    access.create_file("unrelated.txt", b"new\n")
    assert access.refresh_stale_read_receipt("app.py", expected_sha256="0" * 64) is False


def test_an_expired_write_is_rejected(access: WorkspaceAccess, project: Path) -> None:
    """A replace against text that no longer exists on disk must fail closed."""
    with pytest.raises((ValueError, RuntimeError, OSError)):
        access.overwrite_text_file("app.py", source="never written\n", content="x\n")
    assert (project / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


# Edit batches --------------------------------------------------------------


def test_one_edit_batch_advances_the_edit_generation_once(
    access: WorkspaceAccess, project: Path
) -> None:
    """A multi-file batch is one edit, which is what the Tunnel's apply_edits promises."""
    before = access.execute({"action": "bodycheck"})
    access.apply_text_replacements(
        [
            TextReplacement("app.py", "print('hi')\n", "print('one')\n"),
            TextReplacement("nested/inner.txt", "inner\n", "changed\n"),
        ]
    )
    assert (project / "app.py").read_text(encoding="utf-8") == "print('one')\n"
    assert (project / "nested" / "inner.txt").read_text(encoding="utf-8") == "changed\n"
    after = access.execute({"action": "bodycheck"})
    assert after["edit_generation"] == before["edit_generation"] + 1


def test_file_snapshot_reports_text_and_digest_from_one_pass(access: WorkspaceAccess) -> None:
    snapshot = access.file_snapshot("app.py")
    assert isinstance(snapshot, FileSnapshot)
    assert snapshot.as_text() == "print('hi')\n"
    assert snapshot.sha256 == hashlib.sha256(snapshot.data).hexdigest()
    assert snapshot.size == len(snapshot.data)


def test_existing_file_snapshot_returns_none_for_a_missing_file(
    access: WorkspaceAccess,
) -> None:
    assert access.existing_file_snapshot("absent.txt") is None


def test_existing_file_snapshot_refuses_a_directory(access: WorkspaceAccess) -> None:
    with pytest.raises(ValueError, match="not a regular file"):
        access.existing_file_snapshot("nested")


# Stop signal ---------------------------------------------------------------


def test_a_stopped_workspace_refuses_every_action(project: Path) -> None:
    """The stop signal is checked before dispatch, for reads as well as writes."""
    access = open_workspace(project, _Settings(), lambda: True)
    observation = access.execute({"action": "read", "path": "app.py"})
    assert observation["ok"] is False
    assert observation["stopped"] is True


def test_a_stopped_workspace_refuses_direct_capability_reads_and_writes(
    project: Path,
) -> None:
    """Direct capability methods must honor the same stop signal as execute()."""
    access = open_workspace(project, _Settings(), lambda: True)
    original_app = (project / "app.py").read_bytes()
    original_inner = (project / "nested" / "inner.txt").read_bytes()

    direct_calls = (
        lambda: access.project_relative_path("app.py"),
        lambda: access.refresh_stale_read_receipt(
            "app.py",
            expected_sha256=hashlib.sha256(original_app).hexdigest(),
        ),
        lambda: access.file_snapshot("app.py"),
        lambda: access.existing_file_snapshot("app.py"),
        lambda: access.current_read_receipt_snapshot(
            "app.py",
            expected_sha256=hashlib.sha256(original_app).hexdigest(),
        ),
        lambda: access.apply_text_replacements(
            [TextReplacement("app.py", original_app.decode(), "changed\n")]
        ),
        lambda: access.overwrite_text_file(
            "app.py",
            source=original_app.decode(),
            content="changed\n",
        ),
        lambda: access.create_file("created.txt", b"created\n"),
    )
    for direct_call in direct_calls:
        with pytest.raises(RuntimeError, match="Stop requested"):
            direct_call()

    assert (project / "app.py").read_bytes() == original_app
    assert (project / "nested" / "inner.txt").read_bytes() == original_inner
    assert not (project / "created.txt").exists()


@pytest.mark.parametrize("operation", ("apply", "overwrite", "create"))
def test_direct_mutations_recheck_stop_immediately_before_commit(
    project: Path,
    operation: str,
) -> None:
    """A stop arriving after path admission must still prevent the disk mutation."""
    stop_checks = iter((False, False, True))
    access = open_workspace(project, _Settings(), lambda: next(stop_checks, True))
    original = (project / "app.py").read_bytes()
    mutations = {
        "apply": lambda: access.apply_text_replacements(
            [TextReplacement("app.py", original.decode(), "changed\n")]
        ),
        "overwrite": lambda: access.overwrite_text_file(
            "app.py",
            source=original.decode(),
            content="changed\n",
        ),
        "create": lambda: access.create_file("created.txt", b"created\n"),
    }

    with pytest.raises(RuntimeError, match="Stop requested"):
        mutations[operation]()

    assert (project / "app.py").read_bytes() == original
    assert not (project / "created.txt").exists()


# Route compatibility -------------------------------------------------------


EXPECTED_ROUTES = (
    ("/", {"GET"}),
    ("/agent", {"GET"}),
    ("/agent/<browser>/<platform>", {"GET"}),
    ("/agent/tunnel/<platform>", {"GET"}),
    ("/agent/unlock", {"POST"}),
    ("/api/agent/status", {"GET"}),
    ("/api/agent/tunnel/status", {"GET"}),
    ("/api/browser-session", {"GET"}),
    ("/api/cache/<source_key>/status", {"GET"}),
    ("/api/jury/status", {"GET"}),
    ("/api/settings/shadow-backup/status", {"GET"}),
    ("/api/status", {"GET"}),
    ("/browser", {"GET"}),
    ("/cache/<source_key>", {"GET"}),
    ("/jury", {"GET"}),
    ("/mcp", {"GET", "POST"}),
    ("/settings", {"GET", "POST"}),
)


@pytest.fixture(scope="module")
def application():
    return create_app()


@pytest.mark.parametrize(("rule", "methods"), EXPECTED_ROUTES)
def test_blueprint_split_preserved_every_public_route(application, rule, methods) -> None:
    """Splitting routes into blueprints must not move, rename, or drop a URL."""
    observed = {
        frozenset(item.methods - {"HEAD", "OPTIONS"})
        for item in application.url_map.iter_rules()
        if item.rule == rule
    }
    assert observed, f"{rule} is missing"
    assert methods <= set().union(*observed)


def test_every_registered_endpoint_can_be_reversed(application) -> None:
    """A blueprint-qualified endpoint name must still resolve from its own module."""
    with application.test_request_context("/"):
        from flask import url_for

        for rule in application.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            values = {name: "sample" for name in rule.arguments}
            assert url_for(rule.endpoint, **values)


def test_agent_page_redirects_to_the_tunnel_selection(application) -> None:
    """The cross-blueprint redirect from Agent to Tunnel keeps its URL and status."""
    with application.test_client() as client:
        response = client.get("/agent")
    assert response.status_code == 302
    assert response.headers["Location"] == "/agent/tunnel/chatgpt"


# Application isolation and shutdown ----------------------------------------


def test_two_applications_do_not_share_runtime_services(tmp_path: Path) -> None:
    """Each instance builds its own services, so one test app cannot touch another's."""
    first = create_app(tmp_path / "first")
    second = create_app(tmp_path / "second")
    for name in (
        "computer_use_settings",
        "computer_use_agent_service",
        "agent_session_pool",
        "jury_service",
        "tunnel_mcp_service",
        "tunnel_runtime",
        "gemini_tunnel_gateway",
        "local_media_catalog",
    ):
        assert first.extensions[name] is not second.extensions[name], name
    first.extensions["runtime_shutdown"]()
    second.extensions["runtime_shutdown"]()


def test_runtime_shutdown_is_ordered_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated shutdown stops each captured service once, in dependency order."""
    application = create_app(tmp_path / "store")
    stopped: list[str] = []
    monkeypatch.setattr(
        application.extensions["tunnel_mcp_service"],
        "stop",
        lambda: stopped.append("mcp"),
    )
    monkeypatch.setattr(
        application.extensions["tunnel_runtime"],
        "stop",
        lambda: stopped.append("tunnel"),
    )
    monkeypatch.setattr(
        application.extensions["jury_service"],
        "stop_at_exit",
        lambda: stopped.append("jury"),
    )
    monkeypatch.setattr(
        application.extensions["agent_session_pool"],
        "stop_at_exit",
        lambda: stopped.append("agent"),
    )
    shutdown = application.extensions["runtime_shutdown"]
    shutdown()
    shutdown()
    assert stopped == ["mcp", "tunnel", "jury", "agent"]


def test_runtime_shutdown_continues_after_a_tunnel_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One managed-runtime failure must not strand later browser-owning services."""
    application = create_app(tmp_path / "store")
    stopped: list[str] = []
    monkeypatch.setattr(
        application.extensions["tunnel_mcp_service"],
        "stop",
        lambda: stopped.append("mcp"),
    )

    def fail_tunnel_stop() -> None:
        stopped.append("tunnel")
        raise RuntimeError("expected shutdown probe")

    monkeypatch.setattr(
        application.extensions["tunnel_runtime"],
        "stop",
        fail_tunnel_stop,
    )
    monkeypatch.setattr(
        application.extensions["jury_service"],
        "stop_at_exit",
        lambda: stopped.append("jury"),
    )
    monkeypatch.setattr(
        application.extensions["agent_session_pool"],
        "stop_at_exit",
        lambda: stopped.append("agent"),
    )

    application.extensions["runtime_shutdown"]()

    assert stopped == ["mcp", "tunnel", "jury", "agent"]


# Tunnel MCP contracts over the shared boundary -----------------------------


@pytest.fixture
def tunnel_service(tmp_path: Path, project: Path):
    """Build a Tunnel service over one writable and one read-only project."""
    import json

    from app.core.computer_use_agent import ComputerUseSettings
    from app.core.tunnel_mcp import TunnelMcpService
    from app.core.tunnel_projects import ProjectRegistry

    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "app.py").write_text("reference = True\n", encoding="utf-8")
    registry_path = tmp_path / "tunnel-projects.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": [
                    {"id": "main", "root": str(project), "writable": True},
                    {"id": "ref", "root": str(reference), "writable": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = ComputerUseSettings(workspace_path=str(tmp_path))
    return TunnelMcpService(
        lambda: settings,
        registry=ProjectRegistry(registry_path),
        runtime_root=tmp_path / "runtime",
    )


def tunnel_call(service, name: str, arguments: dict, project: str = "main") -> dict:
    payload = {"name": name, "arguments": {**arguments, "project": project}}
    status, body = service.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": payload}, {}
    )
    assert status == 200
    return body["result"]


def test_tunnel_read_write_round_trip_keeps_its_response_contract(tunnel_service) -> None:
    """read_files then write_file must still report path, created, bytes, and sha256."""
    read_result = tunnel_call(tunnel_service, "read_files", {"files": [{"path": "app.py"}]})
    assert read_result["structuredContent"]["ok"] is True

    written = tunnel_call(
        tunnel_service, "write_file", {"path": "fresh.txt", "content": "hello\n"}
    )["structuredContent"]
    assert written["ok"] is True
    assert written["path"] == "fresh.txt"
    assert written["created"] is True
    assert written["bytes"] == len(b"hello\n")
    assert written["sha256"] == hashlib.sha256(b"hello\n").hexdigest()


def test_tunnel_write_to_an_existing_file_requires_the_read_digest(tunnel_service) -> None:
    """The expired-write rule reaches the Tunnel through the shared boundary."""
    result = tunnel_call(
        tunnel_service, "write_file", {"path": "app.py", "content": "x\n"}
    )
    assert result["isError"] is True
    assert "expected_sha256" in result["content"][0]["text"]

    stale = tunnel_call(
        tunnel_service,
        "write_file",
        {"path": "app.py", "content": "x\n", "expected_sha256": "0" * 64},
    )
    assert stale["isError"] is True
    assert "read the current file first" in stale["content"][0]["text"]


def test_tunnel_apply_edits_reports_each_changed_file(tunnel_service) -> None:
    result = tunnel_call(
        tunnel_service,
        "apply_edits",
        {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "hey"}]},
    )["structuredContent"]
    assert result["ok"] is True
    assert result["files"][0]["path"] == "app.py"
    assert result["files"][0]["replacements"] == 1


def test_tunnel_refuses_a_write_to_a_read_only_project(tunnel_service) -> None:
    result = tunnel_call(
        tunnel_service, "write_file", {"path": "new.txt", "content": "x\n"}, project="ref"
    )
    assert result["isError"] is True


def test_stopped_tunnel_refuses_direct_write_capabilities(
    tunnel_service, project: Path
) -> None:
    """Stopping the Tunnel must close write_file and apply_edits as well as execute()."""
    original = (project / "app.py").read_bytes()
    tunnel_service.stop()

    created = tunnel_call(
        tunnel_service,
        "write_file",
        {"path": "stopped.txt", "content": "must not exist\n"},
    )
    edited = tunnel_call(
        tunnel_service,
        "apply_edits",
        {"edits": [{"path": "app.py", "old_text": "hi", "new_text": "changed"}]},
    )

    assert created["isError"] is True
    assert edited["isError"] is True
    assert "Stop requested" in created["content"][0]["text"]
    assert "Stop requested" in edited["content"][0]["text"]
    assert not (project / "stopped.txt").exists()
    assert (project / "app.py").read_bytes() == original


def test_tunnel_refuses_a_path_outside_its_project(tunnel_service) -> None:
    """Project isolation is the shared path rule, not a second Tunnel-only check."""
    result = tunnel_call(tunnel_service, "read_files", {"files": [{"path": "../reference/app.py"}]})
    payload = result["structuredContent"]
    assert payload["ok"] is False
    assert payload["files"][0]["ok"] is False


def test_tunnel_projects_cannot_reach_each_other(tunnel_service, project: Path) -> None:
    """Two registered projects resolve against their own root only."""
    main_listing = tunnel_call(tunnel_service, "list_files", {"path": "."})["structuredContent"]
    ref_listing = tunnel_call(
        tunnel_service, "list_files", {"path": "."}, project="ref"
    )["structuredContent"]
    main_names = set(main_listing["entries"])
    ref_names = set(ref_listing["entries"])
    assert "AGENTS.md" in main_names
    assert "AGENTS.md" not in ref_names
