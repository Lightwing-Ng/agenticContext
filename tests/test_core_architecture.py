"""Regression tests for the dependency direction between Web, Core, and workspace.

These checks protect boundaries, not file layout: which package may import which, and
which private implementations must stay behind their owner's public surface. They do
not assert how many modules exist, how long a module is, or which symbols one module
happens to re-export today.
"""

# Code version: v2.0.1-codex.0

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPOSITORY_ROOT / "app" / "web"
CORE_ROOT = REPOSITORY_ROOT / "app" / "core"
WORKSPACE_ROOT = CORE_ROOT / "workspace"

# The Web layer reaches Core only through these domain facades.
CORE_FACADES = frozenset(
    {
        "app.core.agent",
        "app.core.browser",
        "app.core.foundation",
        "app.core.providers",
        "app.core.storage",
    }
)

# The workspace package is the shared safety boundary for a selected project root.
# It may use these Core neighbours; it must never import the Browser Agent run loop,
# because that would make the Tunnel depend on Agent orchestration again.
WORKSPACE_ALLOWED_CORE_MODULES = frozenset(
    {
        "app.core.config",
        "app.core.browser_sessions",
        "app.core.agent.browser_acceptance",
        "app.core.agent.capability_registry",
        "app.core.agent.compute_jobs",
    }
)
AGENT_ORCHESTRATION_MODULES = frozenset(
    {
        "app.core.computer_use_agent",
        "app.core.agent.session_pool",
        "app.core.agent.event_chain",
        "app.core.jury",
        "app.core.jury_browser",
    }
)

# Modules outside the workspace package that may still reach a private workspace name,
# and the names each one is allowed to reach. ``computer_use_agent`` owns the Agent run
# loop that grew these helpers, so it keeps compatibility re-exports; nothing else does.
WORKSPACE_PRIVATE_ACCESS_ALLOWANCES = {"app.core.computer_use_agent"}

TUNNEL_PATHS = tuple(
    sorted(
        {
            *CORE_ROOT.glob("tunnel*.py"),
            CORE_ROOT / "gemini_tunnel.py",
        }
    )
)

TUNNEL_FORBIDDEN_WORKSPACE_MEMBERS = frozenset(
    {
        "ActionState",
        "WorkspaceController",
        "_current_file_snapshot",
        "_mark_edit",
        "_replace_text_file",
        "_resolve_path",
        "_write_new_file",
        "read_receipts",
    }
)


def module_name_for(path: Path) -> str:
    """Return the dotted module name for one repository file."""
    relative = path.relative_to(REPOSITORY_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


PYTHON_MODULES = frozenset(
    module_name_for(path)
    for path in (REPOSITORY_ROOT / "app").rglob("*.py")
    if "__pycache__" not in path.parts and not path.name.startswith(".")
)


def _resolved_import_base(
    *,
    importing_module: str,
    importing_package: str,
    level: int,
    module: str | None,
) -> str:
    """Resolve one ImportFrom base without importing application code."""
    if not level:
        return module or ""
    parts = importing_package.split(".") if importing_package else []
    parent_hops = level - 1
    if parent_hops > len(parts):
        return ""
    if parent_hops:
        parts = parts[:-parent_hops]
    if module:
        parts.extend(module.split("."))
    return ".".join(parts) or importing_module


def imported_modules(path: Path) -> list[tuple[int, str]]:
    """Return every absolute module name this file imports, with its line number.

    Relative imports are resolved against the importing module, so a package-internal
    ``from ..config import x`` is compared on the same footing as an absolute import.
    """
    module_name = module_name_for(path)
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
    results: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            results.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_import_base(
                importing_module=module_name,
                importing_package=package,
                level=node.level,
                module=node.module,
            )
            if not resolved:
                continue
            for alias in node.names:
                candidate = f"{resolved}.{alias.name}"
                results.append(
                    (
                        node.lineno,
                        candidate if candidate in PYTHON_MODULES else resolved,
                    )
                )
    return results


def python_files(root: Path) -> list[Path]:
    """Return every Python module under one package, recursively."""
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and not path.name.startswith(".")
    )


def relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


def test_web_package_reaches_core_only_through_domain_facades() -> None:
    """Every Web module, at any depth and any import form, uses the five facades."""
    violations: list[str] = []
    for path in python_files(WEB_ROOT):
        for lineno, module in imported_modules(path):
            if not module.startswith("app.core"):
                continue
            if module in CORE_FACADES:
                continue
            violations.append(f"{relative(path)}:{lineno} imports {module}")
    assert violations == []


def test_web_modules_declare_their_core_dependencies_within_the_allowed_set() -> None:
    """A Web module may depend on any subset of the facades, but nothing beyond them.

    This replaces an older assertion that froze the application entry point's exact
    facade list, which turned an incidental import into a contract and broke whenever a
    route slice moved to its own module.
    """
    for path in python_files(WEB_ROOT):
        declared = {
            module for _lineno, module in imported_modules(path) if module.startswith("app.core")
        }
        assert declared <= CORE_FACADES, relative(path)


def test_core_never_imports_the_web_layer() -> None:
    """Core stays usable without Flask, templates, or any route module."""
    violations: list[str] = []
    for path in python_files(CORE_ROOT):
        for lineno, module in imported_modules(path):
            if module == "app.web" or module.startswith("app.web."):
                violations.append(f"{relative(path)}:{lineno} imports {module}")
    assert violations == []


def test_workspace_package_does_not_import_agent_orchestration() -> None:
    """The shared workspace boundary must not depend on the Browser Agent run loop.

    A reverse import would mean the Tunnel still loads Agent orchestration to reach the
    path rules, which is the coupling this package exists to remove.
    """
    violations: list[str] = []
    for path in python_files(WORKSPACE_ROOT):
        for lineno, module in imported_modules(path):
            if any(
                module == forbidden or module.startswith(f"{forbidden}.")
                for forbidden in AGENT_ORCHESTRATION_MODULES
            ):
                violations.append(f"{relative(path)}:{lineno} imports {module}")
    assert violations == []


def test_workspace_package_only_uses_its_declared_core_neighbours() -> None:
    """Keep the workspace package's outward Core dependencies explicit and small."""
    violations: list[str] = []
    for path in python_files(WORKSPACE_ROOT):
        for lineno, module in imported_modules(path):
            if not module.startswith("app.core"):
                continue
            if module.startswith("app.core.workspace"):
                continue
            if module in WORKSPACE_ALLOWED_CORE_MODULES:
                continue
            violations.append(f"{relative(path)}:{lineno} imports {module}")
    assert violations == []


@pytest.mark.parametrize("path", TUNNEL_PATHS, ids=relative)
def test_tunnel_modules_use_only_the_public_workspace_surface(path: Path) -> None:
    """The Tunnel reaches a project through ``WorkspaceAccess``, not controller internals.

    This is deliberately narrow: it forbids the Tunnel from importing a private
    workspace name or a workspace submodule, rather than banning private attribute
    access across the repository.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations: list[str] = []
    for lineno, module in imported_modules(path):
        if module == "app.core.computer_use_agent":
            violations.append(
                f"{relative(path)}:{lineno} imports Agent orchestration {module}"
            )
        if module.startswith("app.core.workspace."):
            violations.append(f"{relative(path)}:{lineno} imports submodule {module}")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            resolved = _resolved_import_base(
                importing_module=module_name_for(path),
                importing_package=module_name_for(path).rpartition(".")[0],
                level=node.level,
                module=node.module,
            )
            if resolved == "app.core.workspace":
                for alias in node.names:
                    if alias.name.startswith("_"):
                        violations.append(
                            f"{relative(path)}:{node.lineno} imports private {alias.name}"
                        )
                    if alias.name in TUNNEL_FORBIDDEN_WORKSPACE_MEMBERS:
                        violations.append(
                            f"{relative(path)}:{node.lineno} imports controller member "
                            f"{alias.name}"
                        )
        if (
            isinstance(node, ast.Attribute)
            and node.attr in TUNNEL_FORBIDDEN_WORKSPACE_MEMBERS
        ):
            violations.append(
                f"{relative(path)}:{node.lineno} accesses workspace internals {node.attr}"
            )
    assert violations == []


def test_only_the_agent_run_loop_keeps_workspace_compatibility_reexports() -> None:
    """One module may still re-export moved workspace helpers; others must not grow copies.

    ``computer_use_agent`` owned these helpers before the split, so its compatibility
    imports are expected. A second module importing a private workspace name would mean
    the boundary is leaking again.
    """
    violations: list[str] = []
    for path in python_files(CORE_ROOT):
        module_name = module_name_for(path)
        if module_name.startswith("app.core.workspace"):
            continue
        if module_name in WORKSPACE_PRIVATE_ACCESS_ALLOWANCES:
            continue
        for lineno, imported in imported_modules(path):
            if imported.startswith("app.core.workspace."):
                violations.append(
                    f"{relative(path)}:{lineno} imports workspace submodule {imported}"
                )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            resolved = _resolved_import_base(
                importing_module=module_name,
                importing_package=(
                    module_name
                    if path.name == "__init__.py"
                    else module_name.rpartition(".")[0]
                ),
                level=node.level,
                module=node.module,
            )
            if resolved != "app.core.workspace":
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    violations.append(
                        f"{relative(path)}:{node.lineno} imports private {alias.name}"
                    )
    assert violations == []
