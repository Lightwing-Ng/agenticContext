"""Mutable per-task controller state: read receipts and verification order."""

# Code version: v1.0.0-claude.0

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


_MAX_AGENT_RUN_REVISION = (1 << 53) - 1


@dataclass(slots=True)
class ActionState:
    """Track controller edits and observed workspace evidence for one loop."""

    edit_generation: int = 0
    workspace_generation: int = 0
    bodycheck_generation: int = -1
    bodycheck_workspace_generation: int = -1
    verification_generation: int = -1
    verification_workspace_generation: int = -1
    workspace_snapshot_id: str = ""
    bodycheck_snapshot_id: str = ""
    verification_snapshot_id: str = ""
    evidence_complete: bool = False
    successful_checks: list[str] = field(default_factory=list)
    read_receipts: dict[
        str,
        tuple[str, tuple[int, int, int, int, int], int],
    ] = field(default_factory=dict)

    @property
    def bodycheck_current(self) -> bool:
        return bool(
            self.evidence_complete
            and self.workspace_snapshot_id
            and self.bodycheck_snapshot_id == self.workspace_snapshot_id
            and self.bodycheck_generation == self.edit_generation
            and self.bodycheck_workspace_generation == self.workspace_generation
        )

    @property
    def verification_current(self) -> bool:
        return bool(
            self.evidence_complete
            and self.workspace_snapshot_id
            and self.verification_snapshot_id == self.workspace_snapshot_id
            and self.verification_generation == self.edit_generation
            and self.verification_workspace_generation == self.workspace_generation
        )

    def checkpoint(self) -> dict[str, Any]:
        """Return versioned, content-free state that can survive worker restart."""
        return {
            "version": "1.0.0",
            "edit_generation": self.edit_generation,
            "workspace_generation": self.workspace_generation,
            "bodycheck_generation": self.bodycheck_generation,
            "bodycheck_workspace_generation": self.bodycheck_workspace_generation,
            "verification_generation": self.verification_generation,
            "verification_workspace_generation": self.verification_workspace_generation,
            "workspace_snapshot_id": self.workspace_snapshot_id,
            "bodycheck_snapshot_id": self.bodycheck_snapshot_id,
            "verification_snapshot_id": self.verification_snapshot_id,
            "evidence_complete": self.evidence_complete,
        }

    @classmethod
    def from_checkpoint(cls, payload: Any) -> "ActionState":
        """Restore only bounded generation counters and SHA-256 snapshot ids."""
        if not isinstance(payload, dict) or payload.get("version") != "1.0.0":
            return cls()

        def generation(name: str) -> int:
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(name)
            if not -1 <= value <= _MAX_AGENT_RUN_REVISION:
                raise ValueError(name)
            return value

        def snapshot_id(name: str) -> str:
            value = str(payload.get(name) or "").strip().casefold()
            if value and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(name)
            return value

        if not isinstance(payload.get("evidence_complete"), bool):
            return cls()
        try:
            state = cls(
                edit_generation=max(0, generation("edit_generation")),
                workspace_generation=max(0, generation("workspace_generation")),
                bodycheck_generation=generation("bodycheck_generation"),
                bodycheck_workspace_generation=generation(
                    "bodycheck_workspace_generation"
                ),
                verification_generation=generation("verification_generation"),
                verification_workspace_generation=generation(
                    "verification_workspace_generation"
                ),
                workspace_snapshot_id=snapshot_id("workspace_snapshot_id"),
                bodycheck_snapshot_id=snapshot_id("bodycheck_snapshot_id"),
                verification_snapshot_id=snapshot_id("verification_snapshot_id"),
                evidence_complete=payload["evidence_complete"],
            )
        except ValueError:
            return cls()
        if not state.workspace_snapshot_id:
            state.evidence_complete = False
        return state
