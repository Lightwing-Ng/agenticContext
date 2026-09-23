"""Explicit project registry for the Secure MCP Tunnel coding backend.

Code version: v1.4.0-codex.0

A Tunnel project is an authority-bearing identity mapped to exactly one canonical
root. Every model-facing filesystem, Git, mutation, and verification tool names
one registered project by its identifier; identifiers are never interpreted as
paths, and write authority comes only from the registry entry, never from where a
directory happens to live.

The registry is the user-owned ``tunnel-projects.json`` file beside
``settings.json``. When that file is absent, the Agent's selected workspace is
offered as one read-only project only if it is itself a Git work-tree root, so a
parent folder such as the Desktop never becomes an implicit project and choosing a
folder never grants write access; only a registry entry does.

The local Tunnel page keeps a preferred subset of registered projects and selects
exactly one available member as the *current* project. That preference is stored
in ``tunnel-selection.json`` beside the registry and carries a revision that
increases on every change. It never grants or revokes authority: the registry
alone decides which explicit project ids may be used and whether they are writable.

A registered root may be temporarily absent on one computer. The registry still
loads that authority as unavailable so the local page can diagnose it without
hiding unrelated usable projects; no tool can open the root until it exists and
passes the normal native-identity and access checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import default_settings_path

LOGGER = logging.getLogger(__name__)

TUNNEL_PROJECTS_FILENAME = "tunnel-projects.json"
TUNNEL_SELECTION_FILENAME = "tunnel-selection.json"
TUNNEL_SELECTION_SCHEMA_VERSION = 2
TUNNEL_SELECTION_LEGACY_SCHEMA_VERSION = 1
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
    filesystem_identity: tuple[int, int, int] | None = None

    def __post_init__(self) -> None:
        """Pin the native directory identity that this authority admitted."""
        if self.filesystem_identity is None:
            try:
                filesystem_identity = _root_filesystem_identity(self.root)
            except (OSError, RuntimeError):
                # Keep a missing or inaccessible registered root visible but
                # unavailable. A later registry read binds it if it appears.
                return
            object.__setattr__(
                self,
                "filesystem_identity",
                filesystem_identity,
            )

    @property
    def identity(self) -> str:
        """Return a short fingerprint of this exact id, root, and authority.

        A task pins it when it starts; if the same id is later remapped to another
        root or its write authority changes, the fingerprint no longer matches.
        """
        material = (
            f"{self.id}\0{self.root}\0{int(self.writable)}\0{self.filesystem_identity}"
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:16]

    def filesystem_identity_matches(self) -> bool:
        """Return whether the registered path still names the admitted directory."""
        try:
            return _root_filesystem_identity(self.root) == self.filesystem_identity
        except (OSError, RuntimeError):
            return False

    def public_record(self) -> dict[str, Any]:
        """Return the model-facing record; absolute host paths are never included."""
        record: dict[str, Any] = {
            "id": self.id,
            "writable": self.writable,
            "identity": self.identity,
        }
        if self.description:
            record["description"] = self.description
        return record


def _root_filesystem_identity(root: Path) -> tuple[int, int, int]:
    """Return a stable native identity for one directory on POSIX and Windows.

    Python exposes the volume/device and native file id as ``st_dev`` and
    ``st_ino`` on both platforms. A birth timestamp, or Windows creation time on
    older Python builds, further protects against a quickly reused native file id
    without changing when files inside the directory are edited.
    """
    metadata = root.stat()
    if not os.path.isdir(root):
        raise NotADirectoryError(str(root))
    birth_ns = int(getattr(metadata, "st_birthtime_ns", 0) or 0)
    if not birth_ns:
        birth_ns = int(float(getattr(metadata, "st_birthtime", 0.0) or 0.0) * 1_000_000_000)
    if os.name == "nt" and not birth_ns:
        birth_ns = int(metadata.st_ctime_ns)
    return int(metadata.st_dev), int(metadata.st_ino), birth_ns


def _refresh_project_filesystem_identity(project: TunnelProject) -> TunnelProject:
    """Return the same registration bound to the directory currently at its path."""
    try:
        current = project.root.resolve(strict=True)
        if current != project.root or not current.is_dir():
            return project
        filesystem_identity = _root_filesystem_identity(current)
    except (OSError, RuntimeError):
        return project
    if filesystem_identity == project.filesystem_identity:
        return project
    return TunnelProject(
        project.id,
        project.root,
        project.writable,
        project.description,
        filesystem_identity,
    )


def project_availability(project: TunnelProject) -> str:
    """Return an empty string when the project root is usable, else the problem."""
    try:
        current = project.root.resolve(strict=True)
    except (OSError, RuntimeError):
        return "The project folder is missing on this computer."
    if current != project.root or not current.is_dir():
        return "The project folder moved or was replaced; review the project registry."
    if project.filesystem_identity is None:
        return "The project folder identity cannot be read by this service."
    if not project.filesystem_identity_matches():
        return (
            "The project folder was replaced after this project identity was resolved; "
            "discover the project again before starting a new task."
        )
    if not os.access(current, os.R_OK | os.X_OK):
        return "The project folder is not readable by this service."
    if project.writable and not os.access(current, os.W_OK):
        return (
            "The project is registered for read and write access, but its folder is "
            "not writable by this service."
        )
    return ""


def default_tunnel_projects_path() -> Path:
    """Return the registry file beside the current settings file."""
    return default_settings_path().parent / TUNNEL_PROJECTS_FILENAME


def default_tunnel_selection_path() -> Path:
    """Return the current-project selection file beside the registry."""
    return default_settings_path().parent / TUNNEL_SELECTION_FILENAME


def default_tunnel_browse_root() -> Path:
    """Return the per-user Desktop used only as the local folder-browser start."""
    desktop = Path.home() / "Desktop"
    try:
        if desktop.is_dir():
            return desktop.resolve(strict=True)
    except (OSError, RuntimeError):
        pass
    return Path.home().resolve(strict=False)


def _canonical_root(raw_root: Any, label: str) -> Path:
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise ProjectRegistryError(f"{label} needs a root path.")
    candidate = Path(raw_root.strip()).expanduser()
    if not candidate.is_absolute():
        raise ProjectRegistryError(f"{label} root must be an absolute path.")
    try:
        root = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProjectRegistryError(f"{label} root cannot be resolved.") from exc
    if root.exists() and not root.is_dir():
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
    """Offer the selected Agent workspace read-only when it is one Git repository root.

    Choosing a folder never grants write access; a writable project must be listed
    in the registry.
    """
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
    return (
        TunnelProject(
            project_id,
            root,
            False,
            "The Agent's selected folder; read-only until it is registered.",
        ),
    )


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

    def uses_fallback(self) -> bool:
        """Return whether no registry file exists, so only the fallback applies."""
        return not self.path.exists()

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
                return tuple(_refresh_project_filesystem_identity(project) for project in self._cache)
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
        """Return the registered authority with exactly this identifier.

        Availability is deliberately separate: callers must run
        ``project_availability`` before opening the root. This lets discovery show
        one missing registered project without suppressing every usable entry.
        """
        if not isinstance(project_id, str) or not project_id:
            raise ProjectRegistryError("Name a registered project by its configured id.")
        projects = self.projects(fallback_workspace)
        for project in projects:
            if project.id == project_id:
                return project
        known = ", ".join(project.id for project in projects) or "none"
        raise ProjectRegistryError(
            f"Unknown project: {project_id[:64]}. Registered projects: {known}."
        )


class ProjectSelectionConflict(ProjectRegistryError):
    """Raised when a selection was saved against an older selection revision."""


@dataclass(frozen=True, slots=True)
class ProjectSelection:
    """The preferred project set and unique current-project choice."""

    project_id: str = ""
    selected_project_ids: tuple[str, ...] | None = None
    revision: int = 0
    selected_at: float = 0.0


class ProjectSelectionStore:
    """Persist the current-project choice with a monotonically increasing revision."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_tunnel_selection_path()

    def load(self) -> ProjectSelection:
        """Return the saved selection; a missing or unreadable file means none."""
        path = self.path
        try:
            if path.is_symlink() or path.stat().st_size > 4_096:
                return ProjectSelection()
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ProjectSelection()
        except (OSError, UnicodeError, ValueError):
            LOGGER.warning("The Tunnel project selection file is unreadable; ignoring it.")
            return ProjectSelection()
        if not isinstance(payload, dict) or payload.get("schema_version") not in {
            TUNNEL_SELECTION_LEGACY_SCHEMA_VERSION,
            TUNNEL_SELECTION_SCHEMA_VERSION,
        }:
            return ProjectSelection()
        project_id = payload.get("project_id")
        raw_selected = payload.get("selected_project_ids")
        if raw_selected is None:
            # Accept the brief development spelling without making it part of the
            # persisted public contract.
            raw_selected = payload.get("enabled_project_ids")
        revision = payload.get("revision")
        selected_at = payload.get("selected_at")
        if (
            not isinstance(project_id, str)
            or (project_id and not _PROJECT_ID_RE.fullmatch(project_id))
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
            or not isinstance(selected_at, (int, float))
        ):
            return ProjectSelection()
        selected_project_ids: tuple[str, ...] | None
        if raw_selected is None and payload.get("schema_version") == TUNNEL_SELECTION_LEGACY_SCHEMA_VERSION:
            selected_project_ids = None
        elif not isinstance(raw_selected, list) or not raw_selected:
            return ProjectSelection()
        else:
            normalized: list[str] = []
            seen: set[str] = set()
            for index, value in enumerate(raw_selected, start=1):
                try:
                    selected_id = _validate_project_id(value, f"Selected project {index}")
                except ProjectRegistryError:
                    return ProjectSelection()
                folded = selected_id.casefold()
                if folded in seen:
                    return ProjectSelection()
                seen.add(folded)
                normalized.append(selected_id)
            if len(normalized) > MAX_TUNNEL_PROJECTS or (
                project_id and project_id not in normalized
            ):
                return ProjectSelection()
            selected_project_ids = tuple(normalized)
        return ProjectSelection(
            project_id,
            selected_project_ids,
            revision,
            float(selected_at),
        )

    def save(
        self,
        project_id: str,
        *,
        selected_project_ids: tuple[str, ...] | list[str] | None = None,
        expected_revision: int | None = None,
    ) -> ProjectSelection:
        """Atomically save the preferred set and current id with revision CAS."""
        _validate_project_id(project_id, "Selected project")
        with self._lock:
            current = self.load()
            if expected_revision is not None and expected_revision != current.revision:
                raise ProjectSelectionConflict(
                    "The current project changed in another window. Review the selection "
                    "and choose again."
                )
            if selected_project_ids is None:
                selected = current.selected_project_ids
                if selected is not None and project_id not in selected:
                    # Preserve the legacy single-project switch contract after a
                    # multi-select page has narrowed its preferred set.
                    selected = (*selected, project_id)
            else:
                normalized: list[str] = []
                seen: set[str] = set()
                for index, value in enumerate(selected_project_ids, start=1):
                    selected_id = _validate_project_id(value, f"Selected project {index}")
                    folded = selected_id.casefold()
                    if folded in seen:
                        raise ProjectRegistryError(
                            f"Selected project {selected_id} is selected twice."
                        )
                    seen.add(folded)
                    normalized.append(selected_id)
                if not normalized:
                    raise ProjectRegistryError("Select at least one Tunnel project.")
                if len(normalized) > MAX_TUNNEL_PROJECTS:
                    raise ProjectRegistryError(
                        f"Select at most {MAX_TUNNEL_PROJECTS} Tunnel projects."
                    )
                selected = tuple(normalized)
            if selected is not None and project_id not in selected:
                raise ProjectRegistryError(
                    "The current Tunnel project must also be selected."
                )
            selection = ProjectSelection(
                project_id,
                selected,
                current.revision + 1,
                time.time(),
            )
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                payload: dict[str, Any] = {
                    "schema_version": (
                        TUNNEL_SELECTION_SCHEMA_VERSION
                        if selection.selected_project_ids is not None
                        else TUNNEL_SELECTION_LEGACY_SCHEMA_VERSION
                    ),
                    "project_id": selection.project_id,
                    "revision": selection.revision,
                    "selected_at": selection.selected_at,
                }
                if selection.selected_project_ids is not None:
                    payload["selected_project_ids"] = list(
                        selection.selected_project_ids
                    )
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            return selection


def selected_projects(
    projects: tuple[TunnelProject, ...],
    selection: ProjectSelection,
) -> tuple[TunnelProject, ...]:
    """Return the registry subset preferred by the local page.

    A legacy selection with no preferred list keeps the pre-checkbox behavior and
    therefore treats the whole registry as selected until the first multi-select
    save. This list never changes registry authority.
    """
    if selection.selected_project_ids is None:
        return projects
    by_id = {project.id: project for project in projects}
    return tuple(
        by_id[project_id]
        for project_id in selection.selected_project_ids
        if project_id in by_id
    )


def project_is_selected(
    project_id: str,
    projects: tuple[TunnelProject, ...],
    selection: ProjectSelection,
) -> bool:
    """Return whether one registered project is in the preferred local subset."""
    return any(project.id == project_id for project in selected_projects(projects, selection))


@dataclass(frozen=True, slots=True)
class CurrentProject:
    """The resolved current project and how it was chosen."""

    project: TunnelProject | None
    selection: ProjectSelection
    source: str
    problem: str = ""


def resolve_current_project(
    projects: tuple[TunnelProject, ...],
    selection: ProjectSelection,
    fallback_workspace: str = "",
) -> CurrentProject:
    """Choose the current project from registered projects only.

    An explicit selection wins. Without one, a registry with exactly one project,
    or a saved Agent folder that lies inside exactly one registered root, is used.
    Otherwise nothing is current and the user must choose.
    """
    selected = selected_projects(projects, selection)
    by_id = {project.id: project for project in selected}
    if selection.project_id:
        project = by_id.get(selection.project_id)
        if project is not None and not project_availability(project):
            return CurrentProject(project, selection, "selection")
        if selection.selected_project_ids is None:
            return CurrentProject(
                None,
                selection,
                "selection",
                f"The selected project {selection.project_id} is unavailable or no "
                "longer registered. Choose an available registered project.",
            )
    available = tuple(project for project in selected if not project_availability(project))
    if len(available) == 1:
        return CurrentProject(available[0], selection, "only_selected_project")
    if fallback_workspace:
        try:
            folder = Path(fallback_workspace).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            folder = None
        if folder is not None:
            matches = [
                project
                for project in available
                if folder == project.root or project.root in folder.parents
            ]
            if len(matches) == 1:
                return CurrentProject(matches[0], selection, "agent_folder")
    if selection.selected_project_ids is not None and available:
        # Recover deterministically when the previous current project was removed
        # or became unavailable. This never changes registry authority.
        return CurrentProject(available[0], selection, "selected_fallback")
    if selection.project_id:
        return CurrentProject(
            None,
            selection,
            "selection",
            f"The selected project {selection.project_id} is unavailable or no longer "
            "selected. Choose an available selected project.",
        )
    return CurrentProject(
        None,
        selection,
        "none",
        "No current project is selected. Choose one of the selected projects.",
    )
