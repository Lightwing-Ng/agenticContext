"""Durable, bounded event chains for one Web Agent run.

Code version: v1.3.1-codex.1
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat as stat_module
from threading import RLock
from typing import Any, Callable
import uuid

from ..state import utc_now


EVENT_CHAIN_VERSION = "1.0.0"
EVENT_FILE_DIRECTORY = "events"
MAX_EVENT_DETAIL_CHARS = 320
MAX_EVENT_DATA_KEYS = 24
MAX_EVENT_LIST_ITEMS = 24
MAX_EVENT_FILE_LINES = 65_536
MAX_PUBLIC_EVENTS = 80
RUN_ID_PATTERN = re.compile(r"^run-[a-f0-9]{16,64}$")
ACTION_ID_PATTERN = re.compile(r"^action-[0-9]{1,6}$")
SENSITIVE_EVENT_KEYS = frozenset(
    {
        "body",
        "command",
        "conversation_url",
        "content",
        "diff",
        "error",
        "html",
        "history",
        "instruction",
        "matches",
        "page_text",
        "prompt",
        "patch",
        "response",
        "source",
        "text",
        "token",
        "transcript",
        "output",
        "url",
        "workspace_path",
    }
)
EVENT_KINDS = frozenset(
    {
        "run.started",
        "page.observation",
        "action.requested",
        "observation",
        "verification",
        "bodycheck",
        "recovery",
        "run.completed",
        "run.failed",
        "run.interrupted",
    }
)
TERMINAL_EVENT_KINDS = frozenset(
    {"run.completed", "run.failed", "run.interrupted"}
)


class EventChainError(ValueError):
    """Raised when persisted event records cannot form one ordered chain."""


def new_run_id() -> str:
    """Return a filesystem-safe identifier for one Agent run."""
    return f"run-{uuid.uuid4().hex}"


def _bounded_text(value: Any, maximum: int = MAX_EVENT_DETAIL_CHARS) -> str:
    return " ".join(str(value or "").replace("\x00", "").split())[:maximum]


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    """Keep event payloads JSON-safe and intentionally smaller than observations."""
    if depth > 2:
        if isinstance(value, (dict, list, tuple)):
            return "[nested value omitted]"
        return _bounded_text(value, 120)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value, 240)
    if isinstance(value, (list, tuple)):
        return [
            _bounded_value(item, depth=depth + 1)
            for item in list(value)[:MAX_EVENT_LIST_ITEMS]
        ]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in list(value.items())[:MAX_EVENT_DATA_KEYS]:
            normalized_key = str(key)[:80]
            if normalized_key.casefold() in SENSITIVE_EVENT_KEYS:
                continue
            normalized[normalized_key] = _bounded_value(item, depth=depth + 1)
        return normalized
    return _bounded_text(value, 120)


def bounded_event_data(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize one event payload without copying raw provider or file content."""
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, value in list(data.items())[:MAX_EVENT_DATA_KEYS]:
        normalized_key = str(key)[:80]
        if normalized_key.casefold() in SENSITIVE_EVENT_KEYS:
            continue
        normalized[normalized_key] = _bounded_value(value)
    return normalized


def summarize_observation(observation: dict[str, Any] | None) -> dict[str, Any]:
    """Extract evidence metadata while excluding raw action output and source text."""
    if not isinstance(observation, dict):
        return {}
    summary: dict[str, Any] = {}
    scalar_keys = (
        "ok",
        "stopped",
        "retry",
        "repeated_response",
        "exit_code",
        "duration_seconds",
        "mutated_workspace",
        "workspace_scan_complete",
        "workspace_changed",
        "workspace_generation",
        "edit_generation",
        "evidence_complete",
        "bodycheck_current",
        "verification_current",
        "action",
        "engine",
        "error_type",
        "truncated",
    )
    for key in scalar_keys:
        if key in observation:
            summary[key] = _bounded_value(observation[key])
    if observation.get("error"):
        summary["error_present"] = True
    if isinstance(observation.get("matches"), list):
        summary["match_count"] = len(observation["matches"])
    if isinstance(observation.get("checks"), list):
        summary["checks"] = [
            {
                "name": _bounded_text(item.get("name"), 120),
                "ok": bool(item.get("ok")),
            }
            for item in observation["checks"][:MAX_EVENT_LIST_ITEMS]
            if isinstance(item, dict)
        ]
    if isinstance(observation.get("instruction_files"), list):
        summary["instruction_file_count"] = len(observation["instruction_files"])
    if isinstance(observation.get("successful_checks"), list):
        summary["successful_check_count"] = len(observation["successful_checks"])
    if isinstance(observation.get("output"), str):
        summary["output_chars"] = len(observation["output"])
    for key in (
        "path",
        "changed_characters",
        "bytes",
        "deleted_bytes",
        "workspace_identity",
        "read_receipt",
        "delete_digest",
        "snapshot_id",
    ):
        if key in observation:
            summary[key] = _bounded_value(observation[key])
    return bounded_event_data(summary)


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One ordered, bounded event in a run's local audit chain."""

    event_id: str
    run_id: str
    sequence: int
    kind: str
    capability: str
    action_id: str
    parent_event_id: str
    occurred_at: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the persisted representation."""
        return {
            "version": EVENT_CHAIN_VERSION,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "capability": self.capability,
            "action_id": self.action_id,
            "parent_event_id": self.parent_event_id,
            "occurred_at": self.occurred_at,
            "status": self.status,
            "detail": self.detail,
            "data": bounded_event_data(self.data),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentEvent":
        """Parse one persisted event without accepting arbitrary structure."""
        if not isinstance(payload, dict):
            raise EventChainError("event record must be an object")
        if payload.get("version") != EVENT_CHAIN_VERSION:
            raise EventChainError("event record has an unsupported version")
        try:
            event = cls(
                event_id=_bounded_text(payload["event_id"], 80),
                run_id=_bounded_text(payload["run_id"], 80),
                sequence=int(payload["sequence"]),
                kind=_bounded_text(payload["kind"], 80),
                capability=_bounded_text(payload["capability"], 120),
                action_id=_bounded_text(payload.get("action_id"), 80),
                parent_event_id=_bounded_text(payload.get("parent_event_id"), 80),
                occurred_at=_bounded_text(payload["occurred_at"], 80),
                status=_bounded_text(payload["status"], 40),
                detail=_bounded_text(payload.get("detail"), MAX_EVENT_DETAIL_CHARS),
                data=bounded_event_data(payload.get("data")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EventChainError("event record contains invalid fields") from exc
        if not event.event_id or not event.run_id or event.sequence < 1:
            raise EventChainError("event record contains empty identity fields")
        return event


class AgentEventChain:
    """Append and validate one run-scoped chain under the Agent runtime root."""

    def __init__(
        self,
        runtime_root: Path,
        run_id: str,
        *,
        now: Callable[[], str] = utc_now,
    ) -> None:
        normalized_run_id = str(run_id or "").strip()
        if not RUN_ID_PATTERN.fullmatch(normalized_run_id):
            raise ValueError("Agent event chains require a filesystem-safe run id.")
        self.runtime_root = Path(runtime_root).expanduser()
        self.run_id = normalized_run_id
        self._now = now
        self._lock = RLock()
        self._events: list[AgentEvent] = []
        self._state = "ready"
        self._error = ""
        self._next_action_number = 1
        self._path = self.runtime_root / EVENT_FILE_DIRECTORY / f"{self.run_id}.jsonl"
        self._load()

    @property
    def path(self) -> Path:
        """Return the app-owned event path for diagnostics, never for user input."""
        return self._path

    def _load(self) -> None:
        for directory in (self.runtime_root, self._path.parent):
            try:
                directory_stat = directory.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                self._state = "degraded"
                self._error = _bounded_text(exc)
                return
            if stat_module.S_ISLNK(directory_stat.st_mode) or not stat_module.S_ISDIR(
                directory_stat.st_mode
            ):
                self._state = "invalid"
                self._error = "event directory is not a regular directory"
                return
        try:
            event_stat = self._path.lstat()
            if stat_module.S_ISLNK(event_stat.st_mode) or not stat_module.S_ISREG(
                event_stat.st_mode
            ) or event_stat.st_nlink != 1:
                self._state = "invalid"
                self._error = "event file is not an owner-only regular file"
                return
        except FileNotFoundError:
            return
        except OSError as exc:
            self._state = "degraded"
            self._error = _bounded_text(exc)
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            self._state = "degraded"
            self._error = _bounded_text(exc)
            return
        if len(lines) > MAX_EVENT_FILE_LINES:
            self._state = "invalid"
            self._error = "event file exceeds the bounded line limit"
            return
        try:
            self._events = [
                AgentEvent.from_dict(json.loads(line))
                for line in lines
                if line.strip()
            ]
            self._validate_chain(self._events)
        except (json.JSONDecodeError, EventChainError) as exc:
            self._state = "invalid"
            self._error = _bounded_text(exc)
            return
        action_numbers = [
            int(event.action_id.removeprefix("action-"))
            for event in self._events
            if event.kind == "action.requested" and ACTION_ID_PATTERN.fullmatch(event.action_id)
        ]
        if action_numbers:
            self._next_action_number = max(action_numbers) + 1

    @staticmethod
    def _validate_chain(events: list[AgentEvent]) -> None:
        if not events:
            return
        seen_events: set[str] = set()
        requested: dict[str, AgentEvent] = {}
        observed: set[str] = set()
        terminal_seen = False
        previous: AgentEvent | None = None
        for expected_sequence, event in enumerate(events, start=1):
            if event.sequence != expected_sequence:
                raise EventChainError("event sequence is not contiguous")
            if event.event_id in seen_events:
                raise EventChainError("event ids must be unique")
            if event.run_id != events[0].run_id:
                raise EventChainError("event records belong to different runs")
            if event.kind not in EVENT_KINDS:
                raise EventChainError(f"unsupported event kind: {event.kind}")
            if previous is not None and event.parent_event_id not in {
                "",
                previous.event_id,
            }:
                raise EventChainError("event parent does not point to the preceding event")
            if terminal_seen and event.kind != "recovery":
                raise EventChainError("only recovery events may follow a terminal event")
            if event.kind == "run.started" and expected_sequence != 1:
                raise EventChainError("run.started must be the first event")
            if expected_sequence == 1 and event.kind != "run.started":
                raise EventChainError("event chain must start with run.started")
            if event.kind == "action.requested":
                if not ACTION_ID_PATTERN.fullmatch(event.action_id):
                    raise EventChainError("action.requested requires a stable action id")
                if event.action_id in requested:
                    raise EventChainError("action ids must be unique")
                requested[event.action_id] = event
            elif event.kind in {"observation", "verification", "bodycheck"}:
                if event.action_id not in requested:
                    raise EventChainError(
                        f"{event.kind} references an unknown action id"
                    )
                if event.kind == "observation":
                    if event.action_id in observed:
                        raise EventChainError("each action may have one observation")
                    observed.add(event.action_id)
                elif event.action_id not in observed:
                    raise EventChainError(
                        f"{event.kind} must follow its action observation"
                    )
            if event.kind in TERMINAL_EVENT_KINDS:
                if terminal_seen:
                    raise EventChainError("a run may have only one terminal event")
                terminal_seen = True
            seen_events.add(event.event_id)
            previous = event

    def _write_event(self, event: AgentEvent) -> bool:
        try:
            event_directory = self._path.parent
            for directory in (self.runtime_root, event_directory):
                if directory.exists():
                    directory_stat = directory.lstat()
                    if stat_module.S_ISLNK(directory_stat.st_mode) or not stat_module.S_ISDIR(
                        directory_stat.st_mode
                    ):
                        raise OSError("event directory is not a regular directory")
            event_directory.mkdir(parents=True, exist_ok=True)
            self.runtime_root.chmod(0o700)
            event_directory.chmod(0o700)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._path, flags, 0o600)
            try:
                with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                    descriptor = -1
                    handle.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            os.chmod(self._path, 0o600)
            if os.name == "posix":
                directory_fd = os.open(
                    event_directory,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return True
        except OSError as exc:
            self._state = "degraded"
            self._error = _bounded_text(exc)
            return False

    def append(
        self,
        kind: str,
        *,
        capability: str = "",
        action_id: str = "",
        parent_event_id: str = "",
        status: str = "info",
        detail: str = "",
        data: dict[str, Any] | None = None,
    ) -> AgentEvent | None:
        """Append one validated event only after durable persistence succeeds."""
        normalized_kind = str(kind or "").strip()
        if normalized_kind not in EVENT_KINDS:
            raise EventChainError(f"unsupported event kind: {normalized_kind}")
        with self._lock:
            if self._state != "ready":
                return None
            if len(self._events) >= MAX_EVENT_FILE_LINES:
                self._state = "degraded"
                self._error = "event file reached the bounded line limit"
                return None
            parent = parent_event_id or (self._events[-1].event_id if self._events else "")
            event = AgentEvent(
                event_id=f"event-{uuid.uuid4().hex}",
                run_id=self.run_id,
                sequence=len(self._events) + 1,
                kind=normalized_kind,
                capability=_bounded_text(capability, 120),
                action_id=_bounded_text(action_id, 80),
                parent_event_id=_bounded_text(parent, 80),
                occurred_at=_bounded_text(self._now(), 80),
                status=_bounded_text(status, 40) or "info",
                detail=_bounded_text(detail),
                data=bounded_event_data(data),
            )
            candidate_events = [*self._events, event]
            try:
                self._validate_chain(candidate_events)
            except EventChainError:
                self._state = "invalid"
                self._error = "new event would break the ordered chain"
                return None
            if not self._write_event(event):
                return None
            self._events.append(event)
            return event

    def start(self, *, capability: str = "agent.run", data: dict[str, Any] | None = None) -> AgentEvent | None:
        """Record the run root before any provider or controller work begins."""
        return self.append(
            "run.started",
            capability=capability,
            status="started",
            detail="Agent run started.",
            data=data,
        )

    def begin_action(
        self,
        capability: str,
        *,
        turn: int,
        action_name: str,
        data: dict[str, Any] | None = None,
    ) -> tuple[str, AgentEvent | None]:
        """Create a stable action id and its action.requested event."""
        action_id = f"action-{self._next_action_number:04d}"
        self._next_action_number += 1
        event = self.append(
            "action.requested",
            capability=capability,
            action_id=action_id,
            status="requested",
            detail=f"Requested {action_name} action for turn {int(turn):,}.",
            data={**(data or {}), "turn": int(turn), "action": action_name},
        )
        return action_id, event

    def page_observation(
        self,
        capability: str,
        *,
        status: str = "observed",
        detail: str = "Bounded page observation recorded.",
        data: dict[str, Any] | None = None,
    ) -> AgentEvent | None:
        """Record bounded provider or browser state without raw page content."""
        return self.append(
            "page.observation",
            capability=capability,
            status=status,
            detail=detail,
            data=data,
        )

    def observation(
        self,
        action_id: str,
        capability: str,
        observation: dict[str, Any] | None,
        *,
        status: str | None = None,
        detail: str = "Controller observation recorded.",
    ) -> AgentEvent | None:
        """Record one compact controller observation for one action."""
        safe = summarize_observation(observation)
        return self.append(
            "observation",
            capability=capability,
            action_id=action_id,
            status=status or ("completed" if safe.get("ok") else "failed"),
            detail=detail,
            data=safe,
        )

    def verification(
        self,
        action_id: str,
        capability: str,
        observation: dict[str, Any] | None,
        *,
        status: str | None = None,
        detail: str = "Verification result recorded.",
    ) -> AgentEvent | None:
        """Record verification evidence after the action observation."""
        safe = summarize_observation(observation)
        return self.append(
            "verification",
            capability=capability,
            action_id=action_id,
            status=status or ("passed" if safe.get("ok") else "failed"),
            detail=detail,
            data=safe,
        )

    def bodycheck(
        self,
        action_id: str,
        capability: str,
        observation: dict[str, Any] | None,
        *,
        status: str | None = None,
        detail: str = "Bodycheck result recorded.",
    ) -> AgentEvent | None:
        """Record bodycheck evidence after its controller observation."""
        safe = summarize_observation(observation)
        return self.append(
            "bodycheck",
            capability=capability,
            action_id=action_id,
            status=status or ("passed" if safe.get("ok") else "failed"),
            detail=detail,
            data=safe,
        )

    def terminal(
        self,
        kind: str,
        *,
        status: str,
        detail: str,
        action_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> AgentEvent | None:
        """Record one run terminal event."""
        if kind not in TERMINAL_EVENT_KINDS:
            raise EventChainError("terminal event kind is required")
        return self.append(
            kind,
            action_id=action_id,
            status=status,
            detail=detail,
            data=data,
        )

    def recovery(
        self,
        action: str,
        *,
        status: str,
        detail: str,
        data: dict[str, Any] | None = None,
    ) -> AgentEvent | None:
        """Record a local doctor recovery operation."""
        return self.append(
            "recovery",
            capability="agent.recovery.doctor",
            status=status,
            detail=detail,
            data={"action": action, **(data or {})},
        )

    def summary(self) -> dict[str, Any]:
        """Return bounded chain health and the last event metadata."""
        with self._lock:
            last = self._events[-1] if self._events else None
            observed = {
                event.action_id
                for event in self._events
                if event.kind == "observation"
            }
            pending_action_id = next(
                (
                    event.action_id
                    for event in reversed(self._events)
                    if event.kind == "action.requested"
                    and event.action_id not in observed
                ),
                "",
            )
            return {
                "version": EVENT_CHAIN_VERSION,
                "run_id": self.run_id,
                "count": len(self._events),
                "state": self._state,
                "error": self._error,
                "last_event": self._public_event(last) if last else None,
                "pending_action_id": pending_action_id,
            }

    def has_terminal_event(self) -> bool:
        """Return whether this run already has a terminal event before recovery metadata."""
        with self._lock:
            return any(event.kind in TERMINAL_EVENT_KINDS for event in self._events)

    def latest_delivery_checkpoint(self) -> dict[str, Any]:
        """Return the newest content-free provider-delivery boundary in this chain."""
        with self._lock:
            for event in reversed(self._events):
                phase = str(event.data.get("delivery_phase") or "")
                if event.kind == "page.observation" and phase:
                    return dict(event.data)
        return {}

    @staticmethod
    def _public_event(event: AgentEvent | None) -> dict[str, Any] | None:
        if event is None:
            return None
        return {
            "event_id": event.event_id,
            "sequence": event.sequence,
            "kind": event.kind,
            "capability": event.capability,
            "action_id": event.action_id,
            "occurred_at": event.occurred_at,
            "status": event.status,
            "detail": event.detail,
        }

    def public_events(self, *, limit: int = MAX_PUBLIC_EVENTS) -> list[dict[str, Any]]:
        """Return bounded event metadata for the local doctor panel."""
        bounded_limit = max(1, min(int(limit), MAX_PUBLIC_EVENTS))
        with self._lock:
            return [
                self._public_event(event)
                for event in self._events[-bounded_limit:]
            ]


def event_chain_for_snapshot(
    runtime_root: Path,
    run_id: str,
) -> AgentEventChain | None:
    """Load an existing chain only when a persisted snapshot has a valid run id."""
    if not RUN_ID_PATTERN.fullmatch(str(run_id or "").strip()):
        return None
    try:
        return AgentEventChain(runtime_root, str(run_id).strip())
    except (OSError, ValueError):
        return None
