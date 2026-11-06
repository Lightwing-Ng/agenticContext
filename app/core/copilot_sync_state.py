"""Persist Copilot history coverage independently of cached conversation content."""

# Code version: v1.0.0-codex.0

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa

from .config import LOCAL_STORE_ROOT
from .resource_persistence import read_parquet_rows, write_parquet_rows_atomic


COPILOT_SYNC_STATE_SCHEMA_VERSION = 1
COPILOT_SYNC_STATE_FILENAME = "sync-state.parquet"
COPILOT_SYNC_STATE_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("started_at", pa.string(), nullable=False),
        pa.field("finished_at", pa.string(), nullable=False),
        pa.field("discovery_complete", pa.bool_(), nullable=False),
        pa.field("sync_complete", pa.bool_(), nullable=False),
        pa.field("expected_sessions", pa.int64()),
        pa.field("discovered_sessions", pa.int64(), nullable=False),
        pa.field("processed_sessions", pa.int64(), nullable=False),
        pa.field("failed_sessions", pa.int64(), nullable=False),
        pa.field("completion_basis", pa.string(), nullable=False),
        pa.field("reason", pa.string(), nullable=False),
    ]
)
_STATUSES = frozenset({"running", "complete", "incomplete", "stopped", "failed", "unverified"})


def copilot_sync_state_path(local_store_root: Path | str = LOCAL_STORE_ROOT) -> Path:
    """Return the provider-local coverage checkpoint, separate from message rows."""

    return Path(local_store_root).expanduser() / "llm" / "copilot" / COPILOT_SYNC_STATE_FILENAME


def _unverified_state(reason: str, *, status: str = "unverified") -> dict[str, Any]:
    return {
        "schema_version": COPILOT_SYNC_STATE_SCHEMA_VERSION,
        "status": status,
        "started_at": "",
        "finished_at": "",
        "discovery_complete": False,
        "sync_complete": False,
        "expected_sessions": None,
        "discovered_sessions": 0,
        "processed_sessions": 0,
        "failed_sessions": 0,
        "completion_basis": "",
        "reason": reason,
    }


def _validated_state(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Copilot sync state must be a mapping.")
    row = {**_unverified_state(""), **value}
    if (
        isinstance(row["schema_version"], bool)
        or not isinstance(row["schema_version"], int)
        or row["schema_version"] != COPILOT_SYNC_STATE_SCHEMA_VERSION
    ):
        raise ValueError("Copilot sync state has an unsupported schema version.")
    if not isinstance(row["status"], str) or row["status"] not in _STATUSES:
        raise ValueError("Copilot sync state has an unsupported status.")
    for key in ("started_at", "finished_at", "completion_basis", "reason"):
        if not isinstance(row[key], str):
            raise ValueError(f"Copilot sync state contains an invalid {key}.")
        row[key] = row[key].replace("\x00", "").strip()
    for key in ("discovery_complete", "sync_complete"):
        if not isinstance(row[key], bool):
            raise ValueError(f"Copilot sync state contains an invalid {key}.")
    for key in ("expected_sessions", "discovered_sessions", "processed_sessions", "failed_sessions"):
        count = row[key]
        if key == "expected_sessions" and count is None:
            continue
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 9_223_372_036_854_775_807:
            raise ValueError(f"Copilot sync state contains an invalid {key}.")
    if row["processed_sessions"] > row["discovered_sessions"]:
        raise ValueError("Copilot sync state processed more sessions than were discovered.")
    if row["sync_complete"] or row["status"] == "complete":
        if (
            row["status"] != "complete"
            or not row["sync_complete"]
            or not row["discovery_complete"]
            or row["processed_sessions"] != row["discovered_sessions"]
            or row["failed_sessions"]
            or not row["completion_basis"]
            or (
                row["expected_sessions"] is not None
                and row["expected_sessions"] != row["discovered_sessions"]
            )
        ):
            raise ValueError("Copilot sync completion is inconsistent with its coverage evidence.")
    return {name: row[name] for name in COPILOT_SYNC_STATE_SCHEMA.names}


def read_copilot_sync_state(local_store_root: Path | str = LOCAL_STORE_ROOT) -> dict[str, Any]:
    """Read coverage conservatively without creating or repairing any local files."""

    path = copilot_sync_state_path(local_store_root)
    if not path.exists():
        return _unverified_state("History completeness has not been verified.")
    rows = read_parquet_rows(path)
    if rows is None or len(rows) != 1 or set(rows[0]) != set(COPILOT_SYNC_STATE_SCHEMA.names):
        return _unverified_state("The saved history coverage state is unreadable or invalid.", status="invalid")
    try:
        return _validated_state(rows[0])
    except (TypeError, ValueError):
        return _unverified_state("The saved history coverage state is unreadable or invalid.", status="invalid")


def write_copilot_sync_state(
    local_store_root: Path | str,
    value: Mapping[str, Any],
) -> None:
    """Atomically replace one validated coverage checkpoint without touching history."""

    row = _validated_state(value)
    write_parquet_rows_atomic(copilot_sync_state_path(local_store_root), [row], COPILOT_SYNC_STATE_SCHEMA)
