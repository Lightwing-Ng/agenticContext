"""Isolated local-page to authenticated MCP workflow acceptance.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from flask.testing import FlaskClient

from app.web.app import create_app


AUTHORIZATION = {"Authorization": "Bearer isolated-tunnel-token"}


def git(root: Path, *arguments: str) -> None:
    """Run Git with an isolated test identity."""
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=tunnel@example.invalid",
            "-c",
            "user.name=Tunnel Test",
            *arguments,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def mcp_call(
    client: FlaskClient,
    name: str,
    arguments: dict,
    *,
    request_id: int,
) -> dict:
    """Call one tool through the real authenticated loopback MCP route."""
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=AUTHORIZATION,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["result"]


def observation(result: dict) -> dict:
    """Return a tool's model-facing structured result."""
    return result["structuredContent"]


def test_selected_project_completes_authenticated_crud_check_and_review(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    reference = tmp_path / "reference"
    reference.mkdir()
    (project / "AGENTS.md").write_text("# Test instructions\n", encoding="utf-8")
    (project / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n",
        encoding="utf-8",
    )
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "baseline")
    (reference / "README.md").write_text("reference\n", encoding="utf-8")

    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    registry_path = settings_dir / "tunnel-projects.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": [
                    {"id": "acceptance", "root": str(project), "writable": True},
                    {"id": "reference", "root": str(reference), "writable": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    application = create_app(
        tmp_path / "store",
        computer_use_settings_path=settings_dir / "computer-use.json",
        computer_use_runtime_root=tmp_path / "computer-runtime",
        tunnel_credentials_path=settings_dir / "tunnel-credentials.json",
        tunnel_projects_path=registry_path,
        tunnel_runtime_root=tmp_path / "tunnel-runtime",
        agent_external_operations_enabled=False,
    )
    application.config.update(TESTING=True)
    application.extensions["tunnel_runtime"]._authorization = AUTHORIZATION[
        "Authorization"
    ]

    with application.test_client() as client:
        initial = client.get("/api/agent/tunnel/project")
        assert initial.status_code == 200
        context = initial.get_json()["project_context"]
        assert context["current"] is None
        assert context["projects"][0]["id"] == "acceptance"
        assert context["projects"][0]["registered"] is True
        assert context["projects"][0]["available"] is True

        selected = client.post(
            "/api/agent/tunnel/project",
            json={"project_id": "acceptance", "expected_revision": 0},
        )
        assert selected.status_code == 200
        selected_context = selected.get_json()["project_context"]
        assert selected_context["current"]["id"] == "acceptance"
        assert selected_context["revision"] == 1

        current = mcp_call(client, "current_project", {}, request_id=1)
        assert current["isError"] is False
        discovered = observation(current)
        identity = discovered["current_project"]["identity"]
        assert discovered["current_project"]["id"] == "acceptance"
        assert discovered["selection"]["revision"] == 1
        assert str(project) not in json.dumps(discovered)

        pinned = {"project": "acceptance", "project_identity": identity}
        overview = mcp_call(client, "project_overview", pinned, request_id=2)
        assert overview["isError"] is False
        assert observation(overview)["instruction_files"] == ["AGENTS.md"]

        created = mcp_call(
            client,
            "write_file",
            {
                **pinned,
                "request_id": "acceptance-create-0001",
                "path": "notes/workflow.txt",
                "content": "first\n",
            },
            request_id=3,
        )
        assert created["isError"] is False
        assert observation(created)["created"] is True
        assert (project / "notes" / "workflow.txt").read_text(encoding="utf-8") == "first\n"

        first_read = mcp_call(
            client,
            "read_files",
            {**pinned, "files": [{"path": "notes/workflow.txt"}]},
            request_id=4,
        )
        first_file = observation(first_read)["files"][0]
        assert first_read["isError"] is False
        assert "first" in first_file["content"]

        edited = mcp_call(
            client,
            "apply_edits",
            {
                **pinned,
                "request_id": "acceptance-edit-0001",
                "edits": [
                    {
                        "path": "notes/workflow.txt",
                        "old_text": "first",
                        "new_text": "second",
                        "expected_sha256": first_file["sha256"],
                    }
                ],
            },
            request_id=5,
        )
        assert edited["isError"] is False
        assert observation(edited)["outcome"] == "committed"

        second_read = mcp_call(
            client,
            "read_files",
            {**pinned, "files": [{"path": "notes/workflow.txt"}]},
            request_id=6,
        )
        second_file = observation(second_read)["files"][0]
        assert second_read["isError"] is False
        assert "second" in second_file["content"]

        deleted = mcp_call(
            client,
            "delete_file",
            {
                **pinned,
                "request_id": "acceptance-delete-0001",
                "path": "notes/workflow.txt",
                "expected_sha256": second_file["sha256"],
            },
            request_id=7,
        )
        assert deleted["isError"] is False
        assert observation(deleted)["exists_after"] is False
        assert not (project / "notes" / "workflow.txt").exists()

        missing = mcp_call(
            client,
            "read_files",
            {**pinned, "files": [{"path": "notes/workflow.txt"}]},
            request_id=8,
        )
        assert missing["isError"] is True
        assert observation(missing)["outcome"] == "all_failed"

        checked = mcp_call(
            client,
            "run_check",
            {**pinned, "command": "python -m pytest -q test_smoke.py"},
            request_id=9,
        )
        assert checked["isError"] is False
        assert observation(checked)["exit_code"] == 0

        reviewed = mcp_call(client, "review_changes", pinned, request_id=10)
        assert reviewed["isError"] is False
        assert observation(reviewed)["ok"] is True

        reference_identity = next(
            item["identity"]
            for item in discovered["projects"]
            if item["id"] == "reference"
        )
        refused = mcp_call(
            client,
            "write_file",
            {
                "project": "reference",
                "project_identity": reference_identity,
                "request_id": "acceptance-readonly-0001",
                "path": "blocked.txt",
                "content": "must not exist\n",
            },
            request_id=11,
        )
        assert refused["isError"] is True
        assert observation(refused)["code"] == "read_only_project"
        assert not (reference / "blocked.txt").exists()
