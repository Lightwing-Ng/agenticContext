"""Keep expected public copy separate from private exception diagnostics.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any


logger = logging.getLogger(__name__)


def public_exception_message(
    exc: Exception,
    *,
    fallback: str,
    allowed: tuple[str, ...] = (),
    operation: str,
) -> str:
    """Return trusted application copy, never an exception's diagnostic payload.

    Callers supply fixed copy or messages composed solely from server-owned enums.
    Even expected exception classes can wrap filesystem or provider diagnostics.
    """
    if len(exc.args) == 1 and type(exc.args[0]) is str:
        for message in allowed:
            if exc.args[0] == message:
                return message
    logger.warning(
        "Request operation failed: %s",
        operation,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return fallback


def public_diagnostic_payload(
    payload: Mapping[str, Any],
    *,
    fallback: str,
    allowed: tuple[str, ...] = (),
    operation: str,
) -> dict[str, Any]:
    """Sanitize one known status schema without walking conversation content.

    Route owners pass the diagnostic node itself. Response text, messages, turns,
    and nested tool data remain outside this flat status-field boundary.
    """
    result = dict(payload)
    for key in (
        "last_error",
        "error",
        "cleanup_warning",
        "resource_cleanup_warning",
        "catalog_error",
        "error_traceback",
    ):
        value = result.get(key)
        if value:
            result[key] = (
                ""
                if key == "error_traceback"
                else public_exception_message(
                    RuntimeError(value),
                    fallback=fallback,
                    allowed=allowed,
                    operation=operation,
                )
            )
    if result.get("phase") in {"failed", "error", "interrupted"} and result.get("message"):
        result["message"] = public_exception_message(
            RuntimeError(result["message"]),
            fallback=fallback,
            allowed=allowed,
            operation=operation,
        )
    return result
