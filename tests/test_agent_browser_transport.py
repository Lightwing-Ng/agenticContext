"""Browser transport boundary and compatibility tests.

Code version: v1.2.0-codex.0
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import ModuleType

from app.core import agent
from app.core import agent_debug_browser
from app.core import browser_executables
from app.core import browser_host
from app.core import browser_sessions
from app.core.agent import browser_transport


def _module_imports(module: ModuleType) -> set[str]:
    path = Path(str(module.__file__))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = f"{'.' * node.level}{node.module or ''}"
            resolved = (
                importlib.util.resolve_name(name, module.__package__)
                if node.level
                else name
            )
            imports.add(resolved)
            if node.level and not node.module:
                imports.update(f"{resolved}.{alias.name}" for alias in node.names)
    return imports


def test_agent_facade_exports_browser_transport_directly() -> None:
    assert agent.open_agent_in_browser is browser_transport.open_agent_in_browser
    assert agent.open_browser_for_login is browser_transport.open_browser_for_login


def test_browser_transport_import_does_not_load_agent_run_loop() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.core.agent.browser_transport; "
                "raise SystemExit('app.core.computer_use_agent' in sys.modules)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_browser_host_leaves_do_not_initialize_agent_package() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.core.browser_executables; import app.core.browser_host; "
                "raise SystemExit(any(name in sys.modules for name in "
                "('app.core.agent', 'app.core.computer_use_agent')))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_browser_host_leaves_keep_transport_dependencies_acyclic() -> None:
    executable_imports = _module_imports(browser_executables)
    host_imports = _module_imports(browser_host)
    transport_imports = _module_imports(browser_transport)
    session_imports = _module_imports(browser_sessions)
    debug_browser_imports = _module_imports(agent_debug_browser)

    assert not any(name.startswith("app.core.agent") for name in executable_imports)
    assert "app.core.browser_sessions" not in executable_imports
    assert not any(name.startswith("app.core.agent") for name in host_imports)
    assert "app.core.browser_sessions" not in host_imports
    assert "app.core.browser_executables" in transport_imports
    assert "app.core.browser_executables" in session_imports
    assert "app.core.browser_host" in session_imports
    assert "app.core.browser_executables" in debug_browser_imports
    assert "app.core.agent.browser_transport" not in session_imports
    assert "app.core.agent.browser_transport" not in debug_browser_imports
    assert "app.core.computer_use_agent" not in session_imports


def test_legacy_browser_wrapper_preserves_patchable_dependencies(monkeypatch) -> None:
    import app.core.computer_use_agent as legacy

    def host_check() -> bool:
        return True

    def descriptor_loader(_config: object) -> dict[str, object]:
        return {}

    def executable_resolver(_browser: str) -> str:
        return "/test/msedge.exe"
    captured: dict[str, object] = {}

    def open_browser(*args: object, **kwargs: object) -> dict[str, object]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"opened": True}

    monkeypatch.setattr(legacy, "is_windows_host", host_check)
    monkeypatch.setattr(legacy, "browser_descriptors", descriptor_loader)
    monkeypatch.setattr(
        legacy,
        "resolve_windows_browser_executable",
        executable_resolver,
    )
    monkeypatch.setattr(browser_transport, "open_agent_in_browser", open_browser)

    assert legacy.open_agent_in_browser("chatgpt", "edge") == {"opened": True}
    assert captured["args"] == ("chatgpt", "edge", "")
    assert captured["kwargs"] == {
        "background": True,
        "config": None,
        "_windows_host_check": host_check,
        "_browser_descriptor_loader": descriptor_loader,
        "_windows_executable_resolver": executable_resolver,
    }


def test_debug_browser_resolves_windows_executable_through_shared_leaf(
    monkeypatch,
) -> None:
    monkeypatch.setattr(agent_debug_browser, "is_macos_host", lambda: False)
    monkeypatch.setattr(
        browser_executables,
        "resolve_windows_browser_executable",
        lambda browser: f"/test/{browser}.exe",
    )

    assert agent_debug_browser._resolve_browser_executable("edge") == "/test/edge.exe"
