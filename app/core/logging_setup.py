"""Structured logging configuration for the application.

Code version: v1.2.0-codex.1
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import LOGS_ROOT
from .owner_only_permissions import (
    ensure_owner_only_directory,
    ensure_owner_only_file,
    open_owner_only_append,
)


_CONFIGURED = False
_RUN_ID = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
_JOB_ID: ContextVar[str] = ContextVar("cachelikes_job_id", default="-")

_STANDARD_RECORD_FIELDS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}

_REDACTED = "[REDACTED]"
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "authorizationheader",
        "authtoken",
        "bearer",
        "clientsecret",
        "cookie",
        "cookieheader",
        "cookies",
        "csrftoken",
        "idtoken",
        "password",
        "passwd",
        "proxyauthorization",
        "refreshtoken",
        "secret",
        "securitytoken",
        "sessiontoken",
        "setcookie",
        "token",
        "xcsrftoken",
        "xsessiontoken",
        "xsrftoken",
    }
)
_SENSITIVE_TEXT_KEY = (
    r"(?:access[ _-]?token|api[ _-]?key|authorization(?:[ _-]?header)?|auth[ _-]?token|bearer|"
    r"client[ _-]?secret|cookie(?:[ _-]?header)?|csrf[ _-]?token|id[ _-]?token|"
    r"password|passwd|proxy[ _-]?authorization|refresh[ _-]?token|secret|"
    r"security[ _-]?token|session[ _-]?token|set[ _-]?cookie|token|"
    r"x[ _-]?csrf[ _-]?token|x[ _-]?session[ _-]?token|x[ _-]?srf[ _-]?token)"
)
_QUOTED_SECRET_PATTERN = re.compile(
    rf"(?i)(?P<prefix>(?:\\?[\"'])?{_SENSITIVE_TEXT_KEY}(?:\\?[\"'])?\s*[:=]\s*"
    rf"(?P<quote>\\?[\"']))(?P<value>.*?)(?P=quote)"
)
_HEADER_SECRET_PATTERN = re.compile(
    r"(?im)(?P<prefix>(?:^|[\r\n])[ \t]*(?:-[ \t]*)?"
    r"(?:authorization|proxy-authorization|cookie|set-cookie)[ \t]*:[ \t]*)"
    r"(?P<value>[^\r\n]+)"
)
_INLINE_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)(?P<prefix>\b(?:authorization|proxy[ _-]?authorization)\s*[:=]\s*Bearer\s+)"
    rf"(?!{re.escape(_REDACTED)})(?P<value>[^\s,;\}}]+)"
)
_INLINE_COOKIE_PATTERN = re.compile(
    r"(?i)(?P<prefix>\b(?:cookie|set[ _-]?cookie)\s*[:=]\s*)"
    rf"(?!{re.escape(_REDACTED)})(?P<value>(?=[^\r\n]*=)[^\r\n]+)"
)
_UNQUOTED_SECRET_PATTERN = re.compile(
    rf"(?i)(?P<prefix>\b{_SENSITIVE_TEXT_KEY}\s*[:=]\s*)"
    rf"(?!{re.escape(_REDACTED)})(?P<value>(?:Bearer\s+)?[^\s,;\}}]+)"
)
_TOKEN_LIKE_BEARER_PATTERN = re.compile(
    r"(?i)(?P<prefix>\bBearer\s+)"
    rf"(?!{re.escape(_REDACTED)})(?P<value>(?=[A-Za-z0-9._~+/=-]{{8,}}\b)(?=[^\s]*[0-9._~+/=-])"
    r"[A-Za-z0-9._~+/=-]+)"
)


def _normalized_field_name(value: Any) -> str:
    """Return a comparison key for structured sensitive-field detection."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _is_sensitive_field_name(value: Any) -> bool:
    """Return whether a structured log field conventionally carries credentials."""
    return _normalized_field_name(value) in _SENSITIVE_FIELD_NAMES


def _redact_pattern_value(match: re.Match[str]) -> str:
    """Preserve a credential label while replacing only its value."""
    return f"{match.group('prefix')}{_REDACTED}"


def _redact_quoted_pattern_value(match: re.Match[str]) -> str:
    """Preserve serialized quoting around a redacted credential value."""
    return f"{match.group('prefix')}{_REDACTED}{match.group('quote')}"


def _redact_log_text(value: Any) -> str:
    """Remove recognized browser credentials from one rendered log string."""
    text = str(value or "")
    text = _QUOTED_SECRET_PATTERN.sub(_redact_quoted_pattern_value, text)
    text = _HEADER_SECRET_PATTERN.sub(_redact_pattern_value, text)
    text = _INLINE_AUTHORIZATION_PATTERN.sub(_redact_pattern_value, text)
    text = _INLINE_COOKIE_PATTERN.sub(_redact_pattern_value, text)
    text = _UNQUOTED_SECRET_PATTERN.sub(_redact_pattern_value, text)
    return _TOKEN_LIKE_BEARER_PATTERN.sub(_redact_pattern_value, text)


def _redact_log_value(value: Any) -> Any:
    """Recursively sanitize structured values before JSON serialization."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _redact_log_text(raw_key)
            sanitized[key] = (
                _REDACTED
                if _is_sensitive_field_name(raw_key)
                else _redact_log_value(raw_value)
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (_redact_log_value(item) for item in value),
            key=lambda item: str(item),
        )
    if isinstance(value, str):
        return _redact_log_text(value)
    if isinstance(value, bytes):
        return _redact_log_text(value.decode("utf-8", errors="replace"))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_log_text(value)


def _secure_rotating_log_files(path: Path) -> None:
    """Tighten the active log and its numeric rotation files without changing content."""
    if path.exists():
        ensure_owner_only_file(path)
    rotation_prefix = f"{path.name}."
    for candidate in path.parent.glob(f"{rotation_prefix}*"):
        rotation_index = candidate.name.removeprefix(rotation_prefix)
        if rotation_index.isdigit() and candidate.is_file():
            ensure_owner_only_file(candidate)


class OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    """Keep the active structured log and every generated rotation owner-only."""

    def _open(self) -> Any:
        if self.mode != "a":
            raise ValueError("Private rotating logs require append mode.")
        return open_owner_only_append(
            Path(self.baseFilename), encoding=self.encoding, errors=self.errors
        )

    def doRollover(self) -> None:
        super().doRollover()
        _secure_rotating_log_files(Path(self.baseFilename))


class JsonFormatter(logging.Formatter):
    """Render log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_log_text(record.getMessage()),
            "run_id": getattr(record, "run_id", _RUN_ID),
            "job_id": getattr(record, "job_id", _JOB_ID.get()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "thread": record.threadName,
            "process": record.processName,
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = _redact_log_text(
                self.formatException(record.exc_info)
            )

        if record.stack_info:
            payload["stack"] = _redact_log_text(self.formatStack(record.stack_info))

        return json.dumps(_redact_log_value(payload), ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Render concise console logs with key context."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts = [
            timestamp,
            record.levelname,
            record.name,
            f"job_id={_redact_log_text(getattr(record, 'job_id', _JOB_ID.get()))}",
            _redact_log_text(record.getMessage()),
        ]
        if record.exc_info:
            parts.append(_redact_log_text(self.formatException(record.exc_info)))
        if record.stack_info:
            parts.append(_redact_log_text(self.formatStack(record.stack_info)))
        return " | ".join(parts)


def configure_logging(app_version: str) -> Path:
    """Configure process-wide logging once and return the log file path."""
    global _CONFIGURED
    log_file = LOGS_ROOT / "cachelikes.log.jsonl"
    ensure_owner_only_directory(LOGS_ROOT)

    if _CONFIGURED:
        _secure_rotating_log_files(log_file)
        return log_file

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    file_handler = OwnerOnlyRotatingFileHandler(
        log_file,
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(JsonFormatter())
    _secure_rotating_log_files(log_file)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ConsoleFormatter())

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logging.captureWarnings(True)
    logging.getLogger("werkzeug").setLevel(logging.INFO)

    _CONFIGURED = True
    logging.getLogger("app.bootstrap").info(
        "Structured logging configured.",
        extra={
            "run_id": _RUN_ID,
            "job_id": _JOB_ID.get(),
            "app_version": app_version,
            "log_file": str(log_file),
            "logs_root": str(LOGS_ROOT),
        },
    )
    return log_file


def set_job_id(job_id: str) -> Any:
    """Bind the current job identifier to the logging context."""
    return _JOB_ID.set(job_id)


def reset_job_id(token: Any) -> None:
    """Restore the previous job identifier after a job completes."""
    _JOB_ID.reset(token)


def get_log_file_path() -> Path:
    """Return the primary JSON log file path."""
    return LOGS_ROOT / "cachelikes.log.jsonl"
