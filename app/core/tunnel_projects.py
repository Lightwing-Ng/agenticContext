"""Explicit project registry for the Secure MCP Tunnel coding backend.

Code version: v1.0.0-claude.0

A Tunnel project is an authority-bearing identity mapped to exactly one canonical
root. Every model-facing filesystem, Git, mutation, and verification tool names
one registered project by its identifier; identifiers are never interpreted as
paths, and write authority comes only from the registry entry, never from where a
directory happens to live.

The registry is the user-owned ``tunnel-projects.json`` file beside
``settings.json``. When that file is absent, the Agent's selected workspace is
registered as one writable project only if it is itself a Git work-tree root, so a
parent folder such as the Desktop never becomes an implicit project.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import default_settings_path

TUNNEL_PROJECTS_FILENAME = "tunnel-projects.json"
TUNNEL_PROJECTS_SCHEMA_VERSION = 1
MAX_TUNNEL_PROJECTS = 32
MAX_REGISTRY_BYTES = 64 * 1024
MAX_DESCRIPTION_CHARACTERS = 200
PROJECT_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9._-]{0,63}$"
_PROJECT_ID_RE = re.compile(PROJECT_ID_PATTERN)
_PROJECT_KEYS = frozenset({"id", "root", "writable", "description"})
_REGISTRY_KEYS = frozenset({"schema_version", "projects"})


class ProjectRegistryError(ValueError):
    """Raised when the registry is invalid or a project cannot be resolved."""


@dataclass(frozen=True, slots=True)
class TunnelProject:
    """One registered project and the authority granted to it."""

    id: str
    root: Path
    writable: bool
    description: str = ""

    def public_record(self) -> dict[str, Any]:
        """Return the model-facing record; absolute host paths are never included."""
        record: dict[str, Any] = {"id": self.id, "writable": self.writable}
        if self.description:
            record["description"] = self.description
        return record


def default_tunnel_projects_path() -> Path:
    """Return the registry file beside the current settings file."""
    return default_settings_path().parent / TUNNEL_PROJECTS_FILENAME


def _canonical_root(raw_root: Any, label: str) -> Path:
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise ProjectRegistryError(f"{label} needs a root path.")
    candidate = Path(raw_root.strip()).expanduser()
    if not candidate.is_absolute():
        raise ProjectRegistryError(f"{label} root must be an absolute path.")
    try:
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectRegistryError(f"{label} root does not exist.") from exc
    if not root.is_dir():
        raise ProjectRegistryError(f"{label} root must be a directory.")
    if root == Path(root.anchor) or root == Path.home().resolve():
        # A filesystem root or the home folder would grant every nested project at once.
        raise ProjectRegistryError(f"{label} root is too broad to be one project.")
    return root


def _validate_project_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _PROJECT_ID_RE.fullmatch(value):
        raise ProjectRegistryError(
            f"{label} id must start with a letter and use at most 64 letters, digits, "
            "'.', '_', or '-'."
        )
    return value


def parse_project_registry(payload: Any) -> tuple[TunnelProject, ...]:
    """Validate one registry document and return canonical projects."""
    if not isinstance(payload, dict):
        raise ProjectRegistryError("The project registry must be a JSON object.")
    unknown = sorted(set(payload) - _REGISTRY_KEYS)
    if unknown:
        raise ProjectRegistryError(f"The project registry has unknown fields: {', '.join(unknown)}.")
    if payload.get("schema_version") != TUNNEL_PROJECTS_SCHEMA_VERSION:
        raise ProjectRegistryError("The project registry must use schema_version 1.")
    entries = payload.get("projects")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_TUNNEL_PROJECTS:
        raise ProjectRegistryError(
            f"The project registry must list 1 to {MAX_TUNNEL_PROJECTS} projects."
        )
    projects: list[TunnelProject] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        label = f"Project {index}"
        if not isinstance(entry, dict):
            raise ProjectRegistryError(f"{label} must be an object.")
        unknown = sorted(set(entry) - _PROJECT_KEYS)
        if unknown:
            raise ProjectRegistryError(f"{label} has unknown fields: {', '.join(unknown)}.")
        project_id = _validate_project_id(entry.get("id"), label)
        if project_id.casefold() in seen_ids:
            raise ProjectRegistryError(f"Project id {project_id} is registered twice.")
        seen_ids.add(project_id.casefold())
        writable = entry.get("writable", False)
        if not isinstance(writable, bool):
            raise ProjectRegistryError(f"Project {project_id} writable must be true or false.")
        description = entry.get("description", "")
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_CHARACTERS:
            raise ProjectRegistryError(
                f"Project {project_id} description must be text of at most "
                f"{MAX_DESCRIPTION_CHARACTERS} characters."
            )
        root = _canonical_root(entry.get("root"), f"Project {project_id}")
        projects.append(TunnelProject(project_id, root, writable, description.strip()))
    for first in projects:
        for second in projects:
            if first is second:
                continue
            if first.root == second.root or first.root in second.root.parents:
                # Nested roots would let one project's authority reach into another.
                raise ProjectRegistryError(
                    f"Project roots must not overlap: {first.id} contains {second.id}."
                )
    return tuple(projects)


def _fallback_project(workspace_path: str) -> tuple[TunnelProject, ...]:
    """Register the selected Agent workspace only when it is one Git repository root."""
    if not workspace_path:
        return ()
    try:
        root = Path(workspace_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return ()
    if not root.is_dir() or not (root / ".git").exists():
        return ()
    if root == Path(root.anchor) or root == Path.home().resolve():
        return ()
    project_id = root.name if _PROJECT_ID_RE.fullmatch(root.name) else "project"
    return (TunnelProject(project_id, root, True, "The Agent's selected project."),)


class ProjectRegistry:
    """Load the registry on demand and resolve exact project identities."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._cache_key: tuple[str, int, int] | None = None
        self._cache: tuple[TunnelProject, ...] = ()

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_tunnel_projects_path()

    def projects(self, fallback_workspace: str = "") -> tuple[TunnelProject, ...]:
        """Return the registered projects; an invalid registry fails closed."""
        path = self.path
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return _fallback_project(fallback_workspace)
        except OSError as exc:
            raise ProjectRegistryError("The project registry cannot be read.") from exc
        cache_key = (str(path), int(metadata.st_mtime_ns), int(metadata.st_size))
        with self._lock:
            if self._cache_key == cache_key:
                return self._cache
        if path.is_symlink() or not path.is_file():
            raise ProjectRegistryError("The project registry must be a regular file.")
        if metadata.st_size > MAX_REGISTRY_BYTES:
            raise ProjectRegistryError("The project registry is too large.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ProjectRegistryError("The project registry is not valid UTF-8 JSON.") from exc
        projects = parse_project_registry(payload)
        with self._lock:
            self._cache_key = cache_key
            self._cache = projects
        return projects

    def resolve(self, project_id: Any, fallback_workspace: str = "") -> TunnelProject:
        """Return the project with exactly this identifier."""
        if not isinstance(project_id, str) or not project_id:
            raise ProjectRegistryError("Name a registered project; call list_projects to see them.")
        projects = self.projects(fallback_workspace)
        for project in projects:
            if project.id == project_id:
                # The root was canonical when loaded; re-check that it still is.
                try:
                    current = project.root.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise ProjectRegistryError(
                        f"Project {project.id} is unavailable on this computer."
                    ) from exc
                if current != project.root or not current.is_dir():
                    raise ProjectRegistryError(
                        f"Project {project.id} root changed; review the project registry."
                    )
                return project
        known = ", ".join(project.id for project in projects) or "none"
        raise ProjectRegistryError(
            f"Unknown project: {project_id[:64]}. Registered projects: {known}."
        )
