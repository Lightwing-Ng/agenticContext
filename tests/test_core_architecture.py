"""Regression tests for the application-to-core dependency boundary."""

# Code version: v1.1.0-codex.1

from __future__ import annotations

import ast
from pathlib import Path


CORE_FACADES = {
    "app.core.agent",
    "app.core.browser",
    "app.core.foundation",
    "app.core.providers",
    "app.core.storage",
}


def test_web_app_imports_core_through_domain_facades() -> None:
    """Keep the Flask layer independent from the flat core implementation modules."""
    source_path = Path(__file__).resolve().parents[1] / "app/web/app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    core_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("app.core")
    }

    assert core_imports == CORE_FACADES


def test_web_package_has_no_flat_or_nested_core_imports() -> None:
    """Apply the façade boundary to every Python module and nested import in Web."""

    web_root = Path(__file__).resolve().parents[1] / "app" / "web"
    violations: list[str] = []
    for source_path in sorted(web_root.glob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module_names = (node.module,) if node.module else ()
            elif isinstance(node, ast.Import):
                module_names = tuple(alias.name for alias in node.names)
            else:
                continue
            for module_name in module_names:
                if module_name.startswith("app.core") and module_name not in CORE_FACADES:
                    violations.append(
                        f"{source_path.relative_to(web_root.parent.parent)}:{node.lineno} "
                        f"imports {module_name}"
                    )

    assert violations == []
