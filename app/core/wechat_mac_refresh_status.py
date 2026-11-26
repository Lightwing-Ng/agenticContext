"""Validate bounded, presentation-only WeChat refresh progress.

Code version: v1.3.0-codex.0
"""

from __future__ import annotations


PHASES = frozenset(
    {"preparing", "members", "reading", "comparing", "checking", "saving", "verifying", "complete"}
)
COUNTERS = (
    "parts_total",
    "parts_completed",
    "part_current",
    "part_rows_total",
    "part_rows_processed",
    "checked",
    "candidates",
    "skipped_parts",
    "unavailable_parts",
    "excluded",
    "upgraded",
    "reparsed",
    "conflicts",
)
PROGRESS_FIELDS = frozenset({"phase", "partial", "member_snapshot", *COUNTERS})
MAX_COUNTER = 2**53 - 1

FAILED_MESSAGE = "Chat history could not be refreshed safely. Try again shortly."
ACCOUNT_TIMEOUT_MESSAGE = (
    "WeChat account refresh reached its time limit. Reload the conversation list "
    "to check for updates before trying again."
)
STORAGE_FAILURE_MESSAGE = (
    "Not enough free disk space to refresh this chat. Free up space and try again."
)
_PUBLICATION_FAILURES = {
    "Another process is publishing the Mac recovery bank.":
        "Another archive update is publishing. Try again after it finishes.",
    "Canonical inputs changed before reuse or publication.":
        "The saved archive changed before publication. Reload the conversation and try again.",
    "A reused baseline requires private owned parent directories.":
        "The staged archive could not be verified in its private directory.",
    "Open-file inspection is required before baseline reuse.":
        "The local service could not check for open files before saving the archive.",
    "Baseline open-file inspection did not complete.":
        "The local service could not check for open files before saving the archive.",
    "The baseline has an open handle or cannot be inspected safely.":
        "The staged archive is still open or its file handles could not be verified. Try again shortly.",
    "The verified candidate changed before reuse or publication.":
        "The staged archive changed before publication. Try again shortly.",
    "The candidate or canonical inputs changed during storage rebinding.":
        "The staged archive changed before publication. Try again shortly.",
}
SAFE_FAILURE_MESSAGES = frozenset(
    {FAILED_MESSAGE, ACCOUNT_TIMEOUT_MESSAGE, STORAGE_FAILURE_MESSAGE, *_PUBLICATION_FAILURES.values()}
)


def publication_failure_message(error):
    """Return fixed copy for recognized refusals, never the exception text."""
    if type(error) is not ValueError or len(error.args) != 1 or type(error.args[0]) is not str:
        return FAILED_MESSAGE
    return _PUBLICATION_FAILURES.get(error.args[0], FAILED_MESSAGE)


def safe_progress(value, *, require_phase=False):
    """Project only known enums, booleans, and exact JavaScript-safe counters."""
    if not isinstance(value, dict):
        raise ValueError("Refresh progress must be a presentation object.")
    result = {}
    if "phase" in value:
        if not isinstance(value["phase"], str) or value["phase"] not in PHASES:
            raise ValueError("Refresh progress has an unsupported phase.")
        result["phase"] = value["phase"]
    elif require_phase:
        raise ValueError("Refresh progress needs a known phase.")
    for field in COUNTERS:
        if field in value:
            counter = value[field]
            if type(counter) is not int or not 0 <= counter <= MAX_COUNTER:
                raise ValueError("Refresh progress has an invalid counter.")
            result[field] = counter
    if "partial" in value:
        if type(value["partial"]) is not bool:
            raise ValueError("Refresh progress needs an exact coverage flag.")
        result["partial"] = value["partial"]
    if "member_snapshot" in value:
        status = value["member_snapshot"]
        if status not in ("saved", "unavailable", "failed"):
            raise ValueError("Refresh progress has an unsupported member snapshot result.")
        result["member_snapshot"] = status
    total = result.get("parts_total")
    if total is not None and any(
        result.get(field, 0) > total
        for field in ("parts_completed", "part_current", "skipped_parts")
    ):
        raise ValueError("Refresh progress exceeds its known source count.")
    rows = result.get("part_rows_total")
    if rows is not None and result.get("part_rows_processed", 0) > rows:
        raise ValueError("Refresh progress exceeds its known source row count.")
    return result
