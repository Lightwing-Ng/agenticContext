"""The public workspace capability boundary.

Two connections reach a user's project: the Browser Agent controller and the Tunnel
MCP server. Both must obey the same path admission, read-receipt, and approved-command
rules, so this module publishes the narrow, typed surface a non-Agent caller may use
and nothing else. Callers depend on ``WorkspaceAccess``; they never touch controller
internals, never resolve absolute paths themselves, and never see the mutable receipt
store.
"""

# Code version: v1.3.0-codex.0

from __future__ import annotations

import errno
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """One bounded file read and hashed in a single pass."""

    relative_path: str
    data: bytes
    sha256: str
    size: int

    def as_text(self) -> str:
        """Decode this snapshot as UTF-8 text, or reject it with a caller-safe message."""
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{self.relative_path} is not UTF-8 text.") from exc


@dataclass(frozen=True, slots=True)
class TextReplacement:
    """One planned whole-text replacement inside an edit batch."""

    relative_path: str
    source: str
    text: str


def describe_workspace_error(error: BaseException | None) -> str:
    """Return one actionable, path-free explanation for a failed workspace operation.

    Operating-system errors carry absolute host paths and errno jargon; callers
    that report to a model or a user get the cause and the next step instead.
    """
    if error is None:
        return "The operation failed."
    if isinstance(error, PermissionError):
        return (
            "Permission denied by the operating system. Check the file and folder "
            "permissions on this computer, then read the file again before retrying."
        )
    if isinstance(error, FileNotFoundError):
        return (
            "The file or one of its folders no longer exists. List or read it again "
            "before retrying."
        )
    if isinstance(error, IsADirectoryError):
        return "The path is a folder, not a regular file."
    if isinstance(error, OSError):
        if error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
            return (
                "The disk is full or over quota, so nothing was written. Free space, "
                "then retry."
            )
        if error.errno == errno.EROFS:
            return "The file system is read-only on this computer."
        if error.errno in {errno.EAGAIN, getattr(errno, "EWOULDBLOCK", -1)}:
            return (
                "Another local operation holds this folder; wait a moment and retry."
            )
        reason = error.strerror or type(error).__name__
        return f"The operating system refused the operation ({reason})."
    return str(error)[:2_000] or type(error).__name__


@runtime_checkable
class WorkspaceAccess(Protocol):
    """Everything a non-Agent connection may do inside one selected project root.

    Path admission, symlink and file-identity checks, read receipts, expired-write
    rejection, the approved-command allow-list, and the stop signal all stay behind
    this surface. Nothing here exposes a mutable controller structure.
    """

    @property
    def root(self) -> Path:
        """Return the resolved project root this access is bound to."""

    @property
    def read_only(self) -> bool:
        """Return whether this access refuses every mutating action."""

    @property
    def verification_current(self) -> bool:
        """Return whether verification evidence still matches the current workspace."""

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute one registered controller action and return its observation."""

    def project_relative_path(self, raw_path: Any, *, allow_missing: bool = False) -> str:
        """Admit one caller-supplied path and return it relative to the project root."""

    def refresh_stale_read_receipt(self, relative_path: str, *, expected_sha256: str) -> bool:
        """Re-read a file whose receipt aged out but still matches the caller's digest.

        A caller that never read the file gains nothing: without a recorded receipt of
        the same digest this is a no-op, so no receipt is ever synthesized.
        """

    def file_snapshot(self, relative_path: str) -> FileSnapshot:
        """Read and hash one existing regular file in a single consistent pass."""

    def existing_file_snapshot(self, relative_path: str) -> FileSnapshot | None:
        """Return the snapshot of an existing regular file, or ``None`` when absent."""

    def current_read_receipt_snapshot(
        self,
        relative_path: str,
        *,
        expected_sha256: str,
    ) -> FileSnapshot:
        """Return a snapshot only when it matches this access's current read receipt."""

    def apply_text_replacements(self, replacements: Sequence[TextReplacement]) -> None:
        """Apply one batch of whole-text replacements, or raise after recovering."""

    def apply_text_replacement_batch(
        self,
        replacements: Sequence[TextReplacement],
    ) -> dict[str, Any]:
        """Apply whole-text replacements and report each file's true outcome.

        See ``WorkspaceController.apply_text_replacement_batch`` for the recovery
        contract: ``committed``, ``rolled_back``, or ``partial``.
        """

    def overwrite_text_file(self, relative_path: str, *, source: str, content: str) -> None:
        """Replace one existing text file's contents as a single edit."""

    def create_file(self, relative_path: str, data: bytes) -> int:
        """Create one new file that does not exist yet and return its byte count."""

    def begin_external_verification(self) -> dict[str, Any]:
        """Record the workspace version a detached verification command will check."""

    def refresh_workspace_evidence(self) -> dict[str, Any]:
        """Return a fresh content fingerprint and invalidate stale verification."""

    def finish_external_verification(
        self,
        evidence: dict[str, Any],
        *,
        command: str,
        succeeded: bool,
        allow_generation_rebind: bool = False,
    ) -> dict[str, Any]:
        """Count a detached check only while its checked files are current.

        ``allow_generation_rebind`` is reserved for a durable check observed by a
        newly started service after the same registered project identity is restored.
        The content fingerprint remains mandatory.
        """


__all__ = [
    "FileSnapshot",
    "TextReplacement",
    "WorkspaceAccess",
    "describe_workspace_error",
]
