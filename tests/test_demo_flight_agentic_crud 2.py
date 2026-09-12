"""Controller integration coverage against a copied Global Flight Atlas workspace.

Code version: v1.1.1-codex.1
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory

import pytest

from app.core.computer_use_agent import ComputerUseSettings, WorkspaceController


DEMO_FLIGHT_ROOT_CANDIDATES = (
    Path.home() / "Desktop" / "demo" / "_flight",
    Path.home() / "Desktop" / "demo_flight",
)
DEMO_FLIGHT_FILES = (
    "README.md",
    "app.js",
    "geometry-worker.js",
    "favicon.svg",
    "index.html",
    "serve.py",
    "styles.css",
    "test_verify.py",
    "verify.py",
    "assets/blender/Season2026.blend",
    "assets/blender/Season2026.manifest.json",
)


def _demo_flight_hashes(root: Path) -> dict[str, str]:
    """Return deterministic digests for every immutable demo source copied into a test."""
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in DEMO_FLIGHT_FILES
    }


def _demo_flight_root() -> Path | None:
    """Return the first host demo with the complete immutable manifest."""
    return next(
        (
            root
            for root in DEMO_FLIGHT_ROOT_CANDIDATES
            if root.is_dir() and all(
                (root / name).is_file() for name in DEMO_FLIGHT_FILES
            )
        ),
        None,
    )


@pytest.mark.integration
def test_copied_demo_flight_supports_safe_agentic_crud_and_cold_verification() -> None:
    """Use the real demo as immutable input and exercise the controller end-to-end."""
    demo_flight_root = _demo_flight_root()
    if demo_flight_root is None:
        pytest.skip("A complete local demo flight acceptance project is unavailable.")

    source_hashes = _demo_flight_hashes(demo_flight_root)

    with TemporaryDirectory(prefix="demo-flight-agentic-") as raw_workspace:
        workspace = Path(raw_workspace) / "demo_flight"
        workspace.mkdir()
        for name in DEMO_FLIGHT_FILES:
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(demo_flight_root / name, target)
        assert _demo_flight_hashes(workspace) == source_hashes

        controller = WorkspaceController(
            workspace,
            ComputerUseSettings(
                workspace_path=str(workspace),
                command_timeout_seconds=30,
            ),
            lambda: False,
        )

        listed = controller.execute({"action": "list", "path": ".", "depth": 3})
        assert listed["ok"]
        assert set(DEMO_FLIGHT_FILES).issubset(set(listed["entries"]))

        readme = controller.execute({"action": "read", "path": "README.md"})
        assert readme["ok"]
        assert readme["sha256"] == source_hashes["README.md"]
        assert "Global Flight Atlas" in readme["content"]

        replaced = controller.execute(
            {
                "action": "replace",
                "path": "README.md",
                "old": "production-oriented",
                "new": "agent-verified",
            }
        )
        assert replaced == {
            "ok": True,
            "action": "replace",
            "path": "README.md",
            "changed_characters": -5,
        }

        styles = controller.execute({"action": "read", "path": "styles.css"})
        assert styles["ok"]
        assert "--blue: #0055cc;" in styles["content"]
        changed_styles = controller.execute(
            {
                "action": "replace",
                "path": "styles.css",
                "old": "--blue: #0055cc;",
                "new": "--blue: #0055cd;",
            }
        )
        assert changed_styles["ok"]
        restored_styles = controller.execute(
            {
                "action": "replace",
                "path": "styles.css",
                "old": "--blue: #0055cd;",
                "new": "--blue: #0055cc;",
            }
        )
        assert restored_styles["ok"]
        assert _demo_flight_hashes(workspace)["styles.css"] == source_hashes["styles.css"]

        written = controller.execute(
            {
                "action": "write",
                "path": "agentic-evidence.txt",
                "content": "Temporary controller evidence only.\n",
            }
        )
        assert written["ok"]
        evidence = controller.execute({"action": "read", "path": "agentic-evidence.txt"})
        assert evidence["ok"]
        assert re.fullmatch(r"[0-9a-f]{64}", str(evidence["sha256"]))

        rejected_delete = controller.execute(
            {
                "action": "delete",
                "path": "agentic-evidence.txt",
                "expected_sha256": "0" * 64,
            }
        )
        assert not rejected_delete["ok"]
        assert "SHA-256" in rejected_delete["error"]

        deleted = controller.execute(
            {
                "action": "delete",
                "path": "agentic-evidence.txt",
                "expected_sha256": evidence["sha256"],
            }
        )
        assert deleted == {
            "ok": True,
            "action": "delete",
            "path": "agentic-evidence.txt",
            "deleted_bytes": len("Temporary controller evidence only.\n".encode()),
        }
        assert not (workspace / "agentic-evidence.txt").exists()

        verification_command = (
            "py -3 -m unittest -v test_verify.py"
            if sys.platform == "win32"
            else "python3 -m unittest -v test_verify.py"
        )
        verification = controller.execute(
            {
                "action": "run",
                "command": verification_command,
            }
        )
        assert verification["ok"], verification["output"]
        assert verification["exit_code"] == 0
        assert not verification["mutated_workspace"]
        assert "OK" in verification["output"]

        bodycheck = controller.execute({"action": "bodycheck"})
        assert bodycheck["ok"]
        assert bodycheck["verification_current"]
        assert bodycheck["bodycheck_current"]

    assert _demo_flight_hashes(demo_flight_root) == source_hashes
