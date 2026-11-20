"""Preserve the controller virtual environment for isolated verification commands.

Code version: v1.0.0-codex.0
"""

import os
from pathlib import Path
import subprocess
import sys
import venv

import pytest

from app.core.workspace import command_policy


def test_verification_uses_controller_environment_without_loading_workspace_shadow_modules(
    tmp_path, monkeypatch,
):
    environment_root = tmp_path / "controller environment"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment_root)
    executable = environment_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = subprocess.run(
        [str(executable), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    Path(probe.stdout.strip(), "controller_environment_probe.py").write_text(
        "VALUE = 'trusted controller environment'\n", encoding="utf-8",
    )
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "test_environment.py").write_text(
        "import unittest\nfrom controller_environment_probe import VALUE\n"
        "class EnvironmentTest(unittest.TestCase):\n"
        "    def test_environment(self):\n"
        "        self.assertEqual(VALUE, 'trusted controller environment')\n",
        encoding="utf-8",
    )
    for name in ("unittest.py", "sitecustomize.py"):
        (workspace / name).write_text(
            "raise RuntimeError('Workspace shadow module must never run')\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(sys, "executable", str(executable))
    command = command_policy.inspection_command_parts(
        "python -m unittest -q test_environment.py", workspace=workspace,
    )
    completed = subprocess.run(
        command, cwd=workspace, capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Ran 1 test" in completed.stderr
    assert command[1:3] == ["-I", "-c"]
    assert Path(command[0]).samefile(executable.resolve())


def test_verification_rejects_a_controller_environment_inside_the_selected_project(
    tmp_path, monkeypatch,
):
    environment_root = tmp_path / "controller environment"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment_root)
    executable = environment_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    monkeypatch.setattr(sys, "executable", str(executable))
    (tmp_path / "test_environment.py").write_text("import unittest\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot be inside the selected project"):
        command_policy.inspection_command_parts(
            "python -m unittest -q test_environment.py", workspace=tmp_path,
        )
