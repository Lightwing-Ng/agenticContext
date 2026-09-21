"""Approved-command policy for workspace verification runs.

This is the single allow-list that decides which commands a task may run inside its
selected project. The Browser Agent controller and the Tunnel MCP server both reach
it through the controller's ``run`` action, so neither connection can widen it.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import sys

from ..config import is_windows_host
from .executables import _portable_executable_names, _trusted_system_executable
from .paths import (
    _path_has_controller_internal_file,
    _path_has_ignored_part,
    _path_has_sensitive_part,
    _path_is_link_like,
)


_COMMAND_WRITE_PATTERN = re.compile(
    r"(?:^|[;&|]\s*)(?:rm|rmdir|mv|cp|install|curl|wget|scp|rsync|sudo|chmod|chown|"
    r"git\s+(?:add|commit|push|pull|reset|clean|checkout|switch|restore|merge|rebase|tag)|"
    r"npm\s+(?:install|uninstall|publish)|pip(?:3)?\s+install|brew\s+(?:install|uninstall)|"
    r"python(?:3(?:\.\d+)?)?\s+-m\s+pip\s+install)\b",
    re.IGNORECASE,
)


_COMMAND_REDIRECTION_PATTERN = re.compile(r"(?:^|\s)(?:>>?|2>|&>)\s*\S|\btee\b", re.IGNORECASE)


_COMMAND_SHELL_OPERATOR_PATTERN = re.compile(r"(?:&&|\|\||[;|`]|\$\(|\n|\r)")


_SAFE_GIT_SUBCOMMANDS = frozenset({"status"})


_SAFE_GIT_STATUS_FLAGS = frozenset(
    {
        "--branch",
        "--porcelain",
        "--porcelain=v1",
        "--short",
        "--untracked-files=all",
        "--untracked-files=no",
        "--untracked-files=normal",
        "-b",
        "-s",
    }
)


_SAFE_PYTHON_MODULES = frozenset(
    {"compileall", "mypy", "py_compile", "pytest", "ruff", "unittest"}
)


_SAFE_UNITTEST_FLAGS = frozenset(
    {"--buffer", "--catch", "--failfast", "--locals", "--quiet", "--verbose", "-b", "-c", "-f", "-q", "-v"}
)


_SAFE_PYTHON_RUNNER = "\n".join(
    (
        "import importlib, os, runpy, sys",
        "mode = sys.argv.pop(1)",
        "target = sys.argv.pop(1)",
        "workspace = os.path.realpath(os.getcwd())",
        "if mode == 'module':",
        "    module = importlib.import_module(target)",
        "    spec = getattr(module, '__spec__', None)",
        "    origin = str(getattr(spec, 'origin', '') or '')",
        "    if origin not in {'', 'built-in', 'frozen'}:",
        "        origin = os.path.realpath(origin)",
        "        try:",
        "            shadowed = os.path.commonpath((workspace, origin)) == workspace",
        "        except ValueError:",
        "            shadowed = False",
        "        if shadowed:",
        "            raise RuntimeError('Approved Python module resolved inside the workspace.')",
        "    sys.path.insert(0, workspace)",
        "    sys.argv[0] = target",
        "    runpy.run_module(target, run_name='__main__', alter_sys=True)",
        "elif mode == 'script':",
        "    sys.path.insert(0, workspace)",
        "    sys.argv[0] = target",
        "    runpy.run_path(target, run_name='__main__')",
        "else:",
        "    raise RuntimeError('Unknown safe Python runner mode.')",
    )
)


_SAFE_PACKAGE_SCRIPTS = re.compile(
    r"^(?:build|check|ci|lint|test|test:[\w:-]+|typecheck|verify)$",
    re.IGNORECASE,
)


_SAFE_SCRIPT_NAME = re.compile(
    r"^(?:check|lint|test|verify)(?:[._-][\w.-]+)?\.(?:sh|zsh|bash|py|ps1)$",
    re.IGNORECASE,
)


_UNSAFE_WRAPPER_EXECUTABLES = frozenset(
    {"bash", "cmd", "dash", "fish", "powershell", "pwsh", "sh", "zsh"}
)


_MUTATING_OR_UNBOUNDED_RUN_FLAGS = frozenset(
    {
        "--apply",
        "--add-noqa",
        "--createstub",
        "--exec",
        "--fix",
        "--force",
        "--in-place",
        "--install-types",
        "--output",
        "--output-file",
        "--pastebin",
        "--pre",
        "--pre-glob",
        "--replace",
        "--update-snapshots",
        "--watch",
        "--write",
        "-exec",
        "-i",
        "-o",
        "-w",
    }
)


def _inspection_argument_path_value(argument: str) -> str:
    value = str(argument or "").strip()
    if value.startswith("-") and "=" in value:
        value = value.split("=", 1)[1].strip()
    return value.split("::", 1)[0]


def _ruff_subcommand(arguments: list[str]) -> str:
    """Return Ruff's subcommand after the small approved global-option subset."""
    index = 0
    value_options = {"--config", "--cache-dir"}
    standalone_options = {
        "--isolated",
        "--no-cache",
        "--quiet",
        "--silent",
        "--verbose",
    }
    while index < len(arguments):
        normalized = arguments[index].casefold()
        if normalized in standalone_options:
            index += 1
            continue
        if normalized in value_options:
            index += 2
            continue
        if any(normalized.startswith(f"{option}=") for option in value_options):
            index += 1
            continue
        return normalized
    return ""


def _canonical_inspection_executable(
    token: str,
    executable_name: str,
    workspace: Path | None,
    *,
    trusted_override: Path | None = None,
) -> str:
    """Return a trusted absolute executable and reject basename path aliases."""
    trusted = trusted_override or _trusted_system_executable(
        executable_name, forbidden_root=workspace
    )
    if trusted is None:
        raise ValueError(
            f"The approved verification executable {executable_name} is unavailable."
        )
    portable_token = token.replace("\\", "/")
    if "/" not in portable_token:
        return str(trusted)
    candidate = Path(portable_token)
    if not candidate.is_absolute():
        candidate = (workspace or Path.cwd()) / candidate
    try:
        if not candidate.resolve(strict=True).samefile(trusted):
            raise ValueError(
                "Run executable paths must resolve to the trusted PATH executable."
            )
    except OSError as exc:
        raise ValueError(
            "Run executable paths must resolve to the trusted PATH executable."
        ) from exc
    return str(trusted)


def _safe_workspace_script(
    token: str,
    workspace: Path | None,
) -> tuple[Path, str] | None:
    """Resolve one real non-linked verification script under workspace/scripts."""
    if workspace is None:
        return None
    portable_token = token.replace("\\", "/")
    relative = Path(portable_token)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0].casefold() != "scripts"
        or not _SAFE_SCRIPT_NAME.fullmatch(relative.name)
    ):
        return None
    try:
        resolved_workspace = workspace.resolve(strict=True)
        scripts_root = (workspace / "scripts").resolve(strict=True)
        scripts_root.relative_to(resolved_workspace)
        candidate = workspace / relative
        current = workspace
        for part in relative.parts:
            current /= part
            if _path_is_link_like(current):
                return None
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(scripts_root)
        if not candidate.is_file():
            return None
    except (OSError, ValueError):
        return None
    suffix = resolved_candidate.suffix.casefold()
    if is_windows_host():
        return (
            (resolved_candidate, "powershell")
            if suffix == ".ps1"
            else None
        )
    if suffix not in {".bash", ".py", ".sh", ".zsh"}:
        return None
    if not os.access(resolved_candidate, os.X_OK):
        return None
    return resolved_candidate, "direct"


def _safe_python_workspace_script(
    token: str,
    workspace: Path | None,
) -> Path | None:
    """Resolve one real, non-linked Python verification script in the workspace."""
    if workspace is None:
        return None
    portable_token = token.replace("\\", "/")
    relative = Path(portable_token)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.suffix.casefold() != ".py"
        or not _SAFE_SCRIPT_NAME.fullmatch(relative.name)
    ):
        return None
    try:
        resolved_workspace = workspace.resolve(strict=True)
        candidate = workspace / relative
        current = workspace
        for part in relative.parts:
            current /= part
            if _path_is_link_like(current):
                return None
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(resolved_workspace)
        if not candidate.is_file():
            return None
    except (OSError, ValueError):
        return None
    return resolved_candidate


def _validate_unittest_arguments(
    arguments: list[str],
    workspace: Path | None,
) -> None:
    """Allow focused unittest files while blocking installed-module imports."""
    targets: list[str] = []
    for argument in arguments:
        if argument.startswith("-"):
            if argument.casefold() not in _SAFE_UNITTEST_FLAGS:
                raise ValueError("Run does not allow this unittest option.")
            continue
        targets.append(argument)
    if not targets:
        raise ValueError("Unittest run actions require a focused project test file.")
    for target in targets:
        relative = Path(target.replace("\\", "/"))
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or len(relative.parts) != 1
            or relative.suffix.casefold() != ".py"
            or not re.fullmatch(
                r"(?:test[A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*_test)\.py",
                relative.name,
            )
        ):
            raise ValueError(
                "Unittest run actions are limited to top-level project test Python files."
            )
        if workspace is None:
            continue
        try:
            resolved_workspace = workspace.resolve(strict=True)
            candidate = workspace / relative
            current = workspace
            for part in relative.parts:
                current /= part
                if _path_is_link_like(current):
                    raise ValueError(
                        "Unittest targets cannot traverse symlinks or junctions."
                    )
            resolved_candidate = candidate.resolve(strict=True)
            resolved_candidate.relative_to(resolved_workspace)
            if not candidate.is_file():
                raise ValueError("Unittest targets must be existing regular files.")
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("Unittest"):
                raise
            raise ValueError(
                "Unittest targets must stay inside the selected project."
            ) from exc


def _validate_inspection_arguments(
    parts: list[str],
    workspace: Path | None = None,
) -> None:
    """Reject mutating flags, network targets, and paths outside the workspace."""
    _executable_name, executable = _portable_executable_names(parts[0])
    effective_tool = executable
    effective_arguments = parts[1:]
    if (
        re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable)
        or executable == "py"
    ) and len(parts) >= 3 and parts[1] == "-m":
        effective_tool = parts[2].casefold()
        effective_arguments = parts[3:]
    if effective_tool in {"eslint", "pytest"} and any(
        argument.casefold() == "-c"
        or (
            argument.casefold().startswith("-c")
            and not argument.casefold().startswith("--")
        )
        for argument in parts[1:]
    ):
        raise ValueError(
            f"Run does not allow custom {effective_tool} configuration files."
        )
    if effective_tool == "pytest" and any(
        argument.casefold() == "-o"
        or (
            argument.casefold().startswith("-o")
            and not argument.casefold().startswith("--")
        )
        or argument.casefold() == "--override-ini"
        or argument.casefold().startswith("--override-ini=")
        for argument in effective_arguments
    ):
        raise ValueError("Run does not allow pytest configuration overrides.")
    if effective_tool == "pytest" and any(
        argument.casefold() == "--pyargs"
        or argument.casefold().startswith("--pyargs=")
        or argument.casefold() == "-p"
        or (
            argument.casefold().startswith("-p")
            and not argument.casefold().startswith("--")
        )
        for argument in effective_arguments
    ):
        raise ValueError("Run does not allow pytest package or plugin loading.")
    if effective_tool == "mypy" and any(
        argument.casefold() in {"-m", "-p", "--module", "--package"}
        or argument.casefold().startswith(("--module=", "--package="))
        or (
            argument.casefold().startswith(("-m", "-p"))
            and not argument.casefold().startswith("--")
        )
        for argument in effective_arguments
    ):
        raise ValueError("Run does not allow mypy targets outside project paths.")
    if effective_tool == "compileall" and any(
        (
            argument.casefold().startswith("-i")
            and not argument.casefold().startswith("--")
        )
        or argument.casefold() == "-b"
        for argument in effective_arguments
    ):
        raise ValueError("Run does not allow unsafe compileall output or file lists.")
    if effective_tool == "unittest":
        _validate_unittest_arguments(effective_arguments, workspace)
    if effective_tool == "cargo" and any(
        argument.casefold() == "--config"
        or argument.casefold().startswith("--config=")
        for argument in effective_arguments
    ):
        raise ValueError("Run does not allow Cargo command-line configuration overrides.")
    if effective_tool == "ruff" and any(
        argument.casefold() in {"--cache-dir", "--config"}
        or argument.casefold().startswith(("--cache-dir=", "--config="))
        for argument in effective_arguments
    ):
        raise ValueError("Run does not allow Ruff path or inline configuration overrides.")
    if effective_tool == "ruff" and _ruff_subcommand(effective_arguments) != "check":
        raise ValueError("Run allows only the non-mutating ruff check command.")
    for argument in parts[1:]:
        normalized = argument.casefold()
        if argument.startswith("@"):
            raise ValueError("Run does not allow external response files.")
        flag = normalized.split("=", 1)[0]
        if flag in _MUTATING_OR_UNBOUNDED_RUN_FLAGS:
            raise ValueError(
                f"Run does not allow the mutating or unbounded flag {flag}."
            )

        path_value = _inspection_argument_path_value(argument)
        if not path_value or path_value.startswith("-"):
            continue
        portable_value = path_value.replace("\\", "/")
        if "://" in portable_value:
            raise ValueError("Run cannot access network targets.")
        portable_path = Path(portable_value)
        if (
            portable_value.startswith(("/", "//"))
            or re.match(r"^[a-zA-Z]:/", portable_value)
            or ".." in portable_path.parts
        ):
            raise ValueError("Run arguments must stay inside the selected project.")
        if (
            any(
                part.casefold() in {".git", ".computer-use-agent"}
                for part in portable_path.parts
            )
            or _path_has_controller_internal_file(portable_path)
            or _path_has_sensitive_part(portable_path)
        ):
            raise ValueError(
                "Run arguments cannot target credentials or internal Agent metadata."
            )
        if workspace is None:
            continue
        candidate = workspace / portable_value
        try:
            current = workspace
            for part in portable_path.parts:
                current /= part
                if _path_is_link_like(current):
                    raise ValueError(
                        "Run arguments cannot traverse symlinks or junctions."
                    )
                if not current.exists():
                    break
            resolved_workspace = workspace.resolve(strict=True)
            resolved_candidate = candidate.resolve(strict=False)
            resolved_relative = resolved_candidate.relative_to(resolved_workspace)
            if (
                _path_has_ignored_part(resolved_relative)
                or _path_has_controller_internal_file(resolved_relative)
                or _path_has_sensitive_part(resolved_relative)
            ):
                raise ValueError(
                    "Run arguments cannot target credentials or internal Agent metadata."
                )
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("Run arguments"):
                raise
            raise ValueError(
                "Run arguments must stay inside the selected project."
            ) from exc


def _split_inspection_command(command: str) -> list[str]:
    """Split one direct command and remove Windows-only outer token quotes."""
    windows_host = is_windows_host()
    try:
        parts = shlex.split(command, posix=not windows_host)
    except ValueError as exc:
        raise ValueError("Run contains invalid shell quoting.") from exc
    if not windows_host:
        return parts
    normalized: list[str] = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in {"'", '"'}:
            part = part[1:-1]
        if '"' in part:
            raise ValueError("Run contains unsupported Windows command quoting.")
        normalized.append(part)
    return normalized


def _normalize_inspection_launcher(parts: list[str]) -> list[str]:
    """Map the Windows Python 3 launcher to the pinned controller runtime."""
    if is_windows_host() and len(parts) > 1:
        _name, executable = _portable_executable_names(parts[0])
        if executable == "py" and parts[1] == "-3":
            return [parts[0], *parts[2:]]
    return parts


def validate_inspection_command(command: str) -> None:
    """Reject shell commands that mutate outside the explicit file actions."""
    if not command or len(command) > 4_000 or "\x00" in command or "\n" in command:
        raise ValueError("Run requires one bounded shell command line.")
    if re.match(
        r"^\s*(?:bash|cmd|dash|fish|powershell|pwsh|sh|zsh)(?:\s|$)",
        command,
        flags=re.IGNORECASE,
    ):
        raise ValueError("Run cannot invoke a nested shell or command interpreter.")
    if _COMMAND_WRITE_PATTERN.search(command) or _COMMAND_REDIRECTION_PATTERN.search(command):
        raise ValueError("Run is limited to inspection, build, lint, and test commands.")
    if _COMMAND_SHELL_OPERATOR_PATTERN.search(command):
        raise ValueError("Run accepts one direct command without shell operators.")
    if re.search(r"\b(?:env|printenv|set)\b", command, flags=re.IGNORECASE):
        raise ValueError("Commands that enumerate the environment are not allowed.")
    parts = _normalize_inspection_launcher(_split_inspection_command(command))
    _validate_inspection_arguments(parts)


def inspection_command_parts(
    command: str,
    *,
    workspace: Path | None = None,
) -> list[str]:
    """Parse one direct command and enforce the controller executable allowlist."""
    validate_inspection_command(command)
    parts = _normalize_inspection_launcher(_split_inspection_command(command))
    if not parts:
        raise ValueError("Run requires a command.")
    _validate_inspection_arguments(parts, workspace)

    executable_name, executable = _portable_executable_names(parts[0])
    arguments = parts[1:]
    python_executable = bool(
        re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable)
        or executable == "py"
    )
    if executable in _UNSAFE_WRAPPER_EXECUTABLES:
        raise ValueError("Run cannot invoke a nested shell or command interpreter.")
    safe_script = _safe_workspace_script(parts[0], workspace)
    canonical_executable = ""
    if safe_script is None:
        trusted_override: Path | None = None
        if python_executable:
            allowed_python_names = {
                "py",
                "python",
                "python3",
                f"python{sys.version_info.major}.{sys.version_info.minor}",
            }
            if executable not in allowed_python_names:
                raise ValueError(
                    "Python run actions must use the controller runtime version."
                )
            try:
                trusted_override = Path(sys.executable).resolve(strict=True)
                if workspace is not None:
                    trusted_override.relative_to(workspace.resolve(strict=True))
            except ValueError:
                pass
            except OSError as exc:
                raise ValueError(
                    "The controller Python runtime is unavailable."
                ) from exc
            else:
                if workspace is not None:
                    raise ValueError(
                        "The controller Python runtime cannot be inside the selected project."
                    )
        canonical_executable = _canonical_inspection_executable(
            parts[0],
            executable_name,
            workspace,
            trusted_override=trusted_override,
        )
    if executable == "git":
        if not arguments or arguments[0].casefold() not in _SAFE_GIT_SUBCOMMANDS:
            raise ValueError("Git run actions are limited to filtered status inspection.")
        if any(
            argument.casefold() not in _SAFE_GIT_STATUS_FLAGS
            for argument in arguments[1:]
        ):
            raise ValueError("Git status run actions contain an unsupported argument.")
        return ["git", "status", "--short"]
    if executable == "rg":
        raise ValueError(
            "Use the project-confined search action instead of running ripgrep directly."
        )
    if executable == "tsc":
        if arguments != ["--noEmit"]:
            raise ValueError(
                "TypeScript verification must use exactly one standalone --noEmit "
                "and no other arguments."
            )
    if executable in {"pytest", "ruff", "mypy", "pyright", "eslint", "tsc"}:
        return [canonical_executable, *arguments]
    if python_executable:
        if len(arguments) >= 2 and arguments[0] == "-m":
            if arguments[1] not in _SAFE_PYTHON_MODULES:
                raise ValueError("Python run actions must use an approved verification module.")
            return [
                canonical_executable,
                "-I",
                "-c",
                _SAFE_PYTHON_RUNNER,
                "module",
                arguments[1],
                *arguments[2:],
            ]
        safe_python_script = (
            _safe_python_workspace_script(arguments[0], workspace)
            if arguments
            else None
        )
        if safe_python_script is not None:
            return [
                canonical_executable,
                "-I",
                "-c",
                _SAFE_PYTHON_RUNNER,
                "script",
                str(safe_python_script),
                *arguments[1:],
            ]
        raise ValueError(
            "Python run actions must use an approved verification module or project verification script."
        )
    if executable == "node":
        if (
            len(arguments) != 2
            or arguments[0] != "--check"
            or arguments[1].startswith("-")
        ):
            raise ValueError("Node run actions are limited to syntax checks.")
        return [canonical_executable, *arguments]
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        normalized = [argument.casefold() for argument in arguments]
        if normalized == ["test"] or (
            len(arguments) >= 2
            and normalized[0] == "run"
            and _SAFE_PACKAGE_SCRIPTS.fullmatch(arguments[1])
        ):
            return [canonical_executable, *arguments]
        raise ValueError("Package-manager run actions are limited to existing check scripts.")
    if executable == "go" and arguments and arguments[0] in {"test", "vet"}:
        return [canonical_executable, *arguments]
    if executable == "cargo" and arguments and arguments[0] in {"check", "clippy", "test"}:
        return [canonical_executable, *arguments]
    if executable == "make" and arguments and all(
        _SAFE_PACKAGE_SCRIPTS.fullmatch(argument) for argument in arguments
    ):
        return [canonical_executable, *arguments]
    if safe_script is not None:
        script_path, launch_kind = safe_script
        if launch_kind == "powershell":
            powershell = _trusted_system_executable(
                "pwsh",
                forbidden_root=workspace,
            ) or _trusted_system_executable(
                "powershell",
                forbidden_root=workspace,
            )
            if powershell is None:
                raise ValueError("Windows PowerShell is required to run a .ps1 verification script.")
            return [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(script_path),
                *parts[1:],
            ]
        return [str(script_path), *parts[1:]]
    raise ValueError("Run executable is outside the inspection and verification allowlist.")
