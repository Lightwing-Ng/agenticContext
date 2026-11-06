"""Add preserved, copied legacy Copilot messages without replacing cached history.

Code version: v1.0.0-codex.0

The capture JSON is the source of truth. Cache message keys identify local ordered
positions, not provider message IDs. Raw Markdown and unknown provider dates remain
unchanged in the preserved capture. This importer never establishes full coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .copilot_history_store import copilot_history_path
from .copilot_sync_state import (
    copilot_sync_state_path,
    read_copilot_sync_state,
    write_copilot_sync_state,
)
from .history_rows import sort_history_rows
from .job_lock import CACHE_TASK_LOCK_NAME, CacheMaintenanceLock, CacheTaskLock
from .resource_persistence import (
    COPILOT_HISTORY_SCHEMA,
    COPILOT_HISTORY_SCHEMA_VERSION,
    read_parquet_rows,
    write_parquet_rows_atomic,
)


CAPTURE_METHOD = "edge-sidebar-accessibility-and-copy-message"
MESSAGE_KEY_BASIS = "local legacy-copy position; provider message IDs unavailable"
_LEGACY_URL = re.compile(r"https://copilot\.microsoft\.com/chats/([A-Za-z0-9]{1,128})")


def normalize_copilot_legacy_url(value: object) -> str:
    """Accept only the exact observed first-party legacy conversation URL shape."""

    return value if isinstance(value, str) and _LEGACY_URL.fullmatch(value) else ""


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"Legacy Copilot capture has invalid {label}.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"Legacy Copilot capture has invalid {label}.") from exc
    return value


def validate_copilot_legacy_capture(value: object) -> dict[str, Any]:
    """Validate capture provenance and ordered bodies without normalizing content."""

    if not isinstance(value, Mapping) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("Legacy Copilot capture has an unsupported schema version.")
    capture = dict(value)
    url = normalize_copilot_legacy_url(capture.get("conversation_url"))
    if not url or capture.get("conversation_id") != _LEGACY_URL.fullmatch(url).group(1):
        raise ValueError("Legacy Copilot capture URL and conversation identity disagree.")
    if capture.get("capture_method") != CAPTURE_METHOD:
        raise ValueError("Legacy Copilot capture has an unsupported capture method.")
    for field in ("title", "raw_ax", "captured_at"):
        _required_text(capture.get(field), field)
    try:
        captured = datetime.fromisoformat(capture["captured_at"])
    except ValueError as exc:
        raise ValueError("Legacy Copilot capture time must be a timezone-qualified UTC datetime.") from exc
    if captured.tzinfo is None or captured.utcoffset() != UTC.utcoffset(captured):
        raise ValueError("Legacy Copilot capture time must be a timezone-qualified UTC datetime.")
    ordinal = capture.get("catalog_ordinal")
    if type(ordinal) is not int or ordinal < 0:
        raise ValueError("Legacy Copilot capture has an invalid catalog ordinal.")
    if "source_dates" in capture and (
        not isinstance(capture["source_dates"], list)
        or any(not isinstance(item, str) for item in capture["source_dates"])
    ):
        raise ValueError("Legacy Copilot source dates must remain a list of raw strings.")
    messages = capture.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("Legacy Copilot capture contains no copied messages.")
    for message in messages:
        if not isinstance(message, Mapping) or message.get("role") not in ("user", "assistant"):
            raise ValueError("Legacy Copilot capture contains an unsupported message role.")
        _required_text(message.get("content"), "copied message content")
        if "message_id" in message or "provider_message_id" in message:
            raise ValueError("Legacy Copilot copied messages cannot claim unavailable provider message IDs.")
    return capture


def _capture_rows(capture: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    turn = 0
    for position, message in enumerate(capture["messages"]):
        role = message["role"]
        turn += int(role == "user")
        content = message["content"]
        rows.append({
            "schema_version": COPILOT_HISTORY_SCHEMA_VERSION,
            "platform": "copilot",
            "conversation_id": capture["conversation_id"],
            "conversation_url": capture["conversation_url"],
            "conversation_title": capture["title"],
            "message_key": f"{capture['conversation_id']}:legacy-copy-position:{position}:{role}",
            "turn_index": max(1, turn),
            "message_index": position,
            "role": role,
            "author_label": "You" if role == "user" else "Copilot",
            "content_text": content,
            "content_html": "",
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "source_links": [],
            "model_label": "",
            "first_seen_at": capture["captured_at"],
            "last_seen_at": "",
        })
    return rows


def plan_copilot_legacy_import(
    existing_rows: list[dict[str, Any]],
    capture: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Plan a strictly additive import, rejecting changed or truncated recaptures."""

    capture = validate_copilot_legacy_capture(capture)
    seen = set()
    for row in existing_rows:
        if (
            not isinstance(row, Mapping)
            or set(row) != set(COPILOT_HISTORY_SCHEMA.names)
            or row["platform"] != "copilot"
            or not isinstance(row["message_key"], str)
            or not row["message_key"]
            or row["message_key"] in seen
        ):
            raise ValueError("Existing Copilot history is malformed; history was preserved.")
        seen.add(row["message_key"])
    incoming = _capture_rows(capture)
    existing_session = [row for row in existing_rows if row["conversation_id"] == capture["conversation_id"]]
    if existing_session:
        # Capture timestamps may change on a repeated read; source bodies and identity may not.
        comparable = [name for name in COPILOT_HISTORY_SCHEMA.names if name != "first_seen_at"]
        if (
            len(existing_session) != len(incoming)
            or any(
                any(old[name] != new[name] for name in comparable)
                for old, new in zip(sort_history_rows(existing_session), incoming)
            )
        ):
            raise ValueError("Legacy Copilot capture conflicts with an existing session; history was preserved.")
        added = []
    else:
        added = incoming
        if any(row["message_key"] in seen for row in added):
            raise ValueError("Legacy Copilot message identities conflict; history was preserved.")
    roles = [message["role"] for message in capture["messages"]]
    paired = len(roles) % 2 == 0 and roles == ["user", "assistant"] * (len(roles) // 2)
    return sort_history_rows([*existing_rows, *added]), {
        "before_sessions": len({row["conversation_id"] for row in existing_rows}),
        "before_messages": len(existing_rows),
        "added_sessions": int(bool(added)),
        "added_messages": len(added),
        "duplicate_sessions": int(not added),
        "copied_messages": len(incoming),
        "role_sequence_requires_review": not paired,
        "sync_complete": False,
        "applied": False,
    }


def _optional_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _partial_coverage(root: Path, known_catalog_sessions: int) -> dict[str, Any]:
    coverage = read_copilot_sync_state(root)
    if coverage["status"] == "invalid":
        raise RuntimeError("Copilot coverage state is unreadable; legacy history was not modified.")
    expected = coverage.get("expected_sessions")
    return {
        **coverage,
        "status": "incomplete",
        "discovery_complete": False,
        "sync_complete": False,
        "expected_sessions": max(known_catalog_sessions, expected or 0),
        "discovered_sessions": max(known_catalog_sessions, coverage["discovered_sessions"]),
        "completion_basis": "Partial legacy native-copy recovery; full body coverage remains unverified.",
        "reason": "Copied legacy messages were recovered. Full catalog and transcript coverage remain unverified.",
    }


def import_copilot_legacy_capture(
    root: Path,
    capture_path: Path,
    *,
    known_catalog_sessions: int,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview without writes, or back up and atomically add one validated capture.

    Applying requires an idle cache maintenance gate. Coverage is marked partial
    before the history replacement so an interrupted import cannot claim success.
    """

    if type(known_catalog_sessions) is not int or known_catalog_sessions <= 0:
        raise ValueError("The known legacy catalog session count must be a positive integer.")
    root = Path(root)
    capture_path = Path(capture_path)
    source = capture_path.read_bytes()
    capture = validate_copilot_legacy_capture(json.loads(source))
    if capture["catalog_ordinal"] > known_catalog_sessions:
        raise ValueError("Legacy Copilot catalog ordinal exceeds the known catalog size.")
    lock = CacheMaintenanceLock(CacheTaskLock(root / CACHE_TASK_LOCK_NAME), root)
    if apply and not lock.acquire("copilot-legacy-recovery"):
        raise RuntimeError("A cache task is running; legacy history was not modified.")
    try:
        path = copilot_history_path(root)
        state_path = copilot_sync_state_path(root)
        original = _optional_bytes(path)
        original_state = _optional_bytes(state_path)
        existing = read_parquet_rows(path)
        if original is not None and existing is None:
            raise RuntimeError("Copilot history is unreadable; legacy history was not modified.")
        rows, result = plan_copilot_legacy_import(existing or [], capture)
        coverage = _partial_coverage(root, known_catalog_sessions)
        result.update(
            after_sessions=result["before_sessions"] + result["added_sessions"],
            after_messages=len(rows),
            known_catalog_sessions=known_catalog_sessions,
            capture_sha256=hashlib.sha256(source).hexdigest(),
        )
        state_changed = read_copilot_sync_state(root) != coverage
        if not apply or not (result["added_messages"] or state_changed):
            return result
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup_dir = root / "recovery" / f"copilot-legacy-import-{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        backups = {"capture.json": source}
        if original is not None:
            backups["history.parquet"] = original
        if original_state is not None:
            backups["sync-state.parquet"] = original_state
        for name, content in backups.items():
            target = backup_dir / name
            target.write_bytes(content)
            if target.read_bytes() != content:
                raise RuntimeError("Legacy Copilot recovery backup verification failed.")
        audit = {
            **result,
            "conversation_id": capture["conversation_id"],
            "conversation_url": capture["conversation_url"],
            "capture_method": CAPTURE_METHOD,
            "captured_at": capture["captured_at"],
            "message_key_basis": MESSAGE_KEY_BASIS,
            "message_keys": [row["message_key"] for row in _capture_rows(capture)],
            "previous_history_sha256": hashlib.sha256(original).hexdigest() if original is not None else None,
            "previous_sync_state_sha256": hashlib.sha256(original_state).hexdigest() if original_state is not None else None,
        }
        audit_path = backup_dir / "import.json"
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        if (
            _optional_bytes(path) != original
            or _optional_bytes(state_path) != original_state
            or capture_path.read_bytes() != source
        ):
            raise RuntimeError("Copilot history or capture changed during review; history was not modified.")
        if state_changed:
            write_copilot_sync_state(root, coverage)
            if read_copilot_sync_state(root) != coverage:
                raise RuntimeError("Legacy Copilot partial coverage readback failed; history was not modified.")
        if result["added_messages"]:
            write_parquet_rows_atomic(path, rows, COPILOT_HISTORY_SCHEMA)
            if read_parquet_rows(path) != rows:
                raise RuntimeError("Legacy Copilot history readback failed; the original backup was retained.")
        result.update(applied=True, backup_dir=str(backup_dir))
        audit.update(result)
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    finally:
        if apply:
            lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--known-catalog-sessions", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(import_copilot_legacy_capture(
        args.root, args.capture, known_catalog_sessions=args.known_catalog_sessions, apply=args.apply,
    ), indent=2))


if __name__ == "__main__":
    main()
