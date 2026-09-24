"""Provider-neutral observable token accounting.

Code version: v1.1.0-codex.0

Browser providers and the MCP Tunnel expose different request lifecycles, but both
need the same cached tokenizer without importing either orchestration runtime. Hidden
provider prompts, reasoning tokens, and billing adjustments remain out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import threading
import time
from typing import Any

import tiktoken


OPENAI_AGENTIC_TOKEN_ENCODING = "o200k_base"
TOKEN_METHOD_UTF8_QUARTER_ESTIMATE = "utf8_quarter_estimate"
TOKEN_METHOD_MIXED_ESTIMATE = "mixed_estimate"
TOKEN_ENCODING_RETRY_SECONDS = 30.0
_ENCODING_LOCK = threading.Lock()
_ENCODING_RETRY_AFTER = 0.0
_ENCODING_RETRY_STARTED = False
_READY_ENCODING: Any | None = None


@lru_cache(maxsize=1)
def _cached_openai_agentic_token_encoding() -> Any:
    # A failed load raises and therefore never enters the successful cache.
    global _READY_ENCODING
    encoding = tiktoken.get_encoding(OPENAI_AGENTIC_TOKEN_ENCODING)
    _READY_ENCODING = encoding
    return encoding


def cached_openai_agentic_token_encoding() -> Any | None:
    """Read an available encoding without starting a load or waiting for one."""
    if _cached_openai_agentic_token_encoding.cache_info().currsize:
        return _READY_ENCODING
    return None


def _retry_openai_agentic_token_encoding() -> None:
    global _ENCODING_RETRY_AFTER, _ENCODING_RETRY_STARTED

    try:
        _cached_openai_agentic_token_encoding()
    except Exception:
        retry_after = time.monotonic() + TOKEN_ENCODING_RETRY_SECONDS
    else:
        retry_after = 0.0
    with _ENCODING_LOCK:
        _ENCODING_RETRY_AFTER = retry_after
        _ENCODING_RETRY_STARTED = False


def openai_agentic_token_encoding() -> Any | None:
    """Start any missing tokenizer load in the background; never block the caller."""
    global _ENCODING_RETRY_AFTER, _ENCODING_RETRY_STARTED

    ready = cached_openai_agentic_token_encoding()
    if ready is not None:
        return ready
    if time.monotonic() < _ENCODING_RETRY_AFTER:
        return None
    with _ENCODING_LOCK:
        ready = cached_openai_agentic_token_encoding()
        if ready is not None:
            return ready
        if time.monotonic() < _ENCODING_RETRY_AFTER or _ENCODING_RETRY_STARTED:
            return None
        _ENCODING_RETRY_STARTED = True
        try:
            threading.Thread(
                target=_retry_openai_agentic_token_encoding,
                daemon=True,
            ).start()
        except Exception:
            _ENCODING_RETRY_STARTED = False
            _ENCODING_RETRY_AFTER = time.monotonic() + TOKEN_ENCODING_RETRY_SECONDS
        return None


def _utf8_quarter_token_count(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def openai_agentic_token_count(value: str) -> int:
    """Count text with o200k_base, or estimate locally when unavailable."""
    text = str(value or "")
    encoding = openai_agentic_token_encoding()
    if encoding is None:
        return _utf8_quarter_token_count(text)
    return len(encoding.encode(text, disallowed_special=()))


@dataclass(slots=True)
class OpenAIEquivalentTokenCounter:
    """Accumulate one task with a fixed method across loads and continuations."""

    total_tokens: int = 0
    transcript_tokens: int = 0
    token_method: str = ""
    _encoding: Any | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.token_method in {
            TOKEN_METHOD_UTF8_QUARTER_ESTIMATE,
            TOKEN_METHOD_MIXED_ESTIMATE,
        }:
            return
        if self.token_method not in {"", OPENAI_AGENTIC_TOKEN_ENCODING}:
            self.token_method = ""
        if not self.token_method and (self.total_tokens or self.transcript_tokens):
            self.token_method = TOKEN_METHOD_MIXED_ESTIMATE
            return
        # Warm up without fixing the task method until its first text count.
        openai_agentic_token_encoding()

    def _count(self, value: str) -> int:
        text = str(value or "")
        if self.token_method in {"", OPENAI_AGENTIC_TOKEN_ENCODING} and self._encoding is None:
            self._encoding = openai_agentic_token_encoding()
            if self._encoding is None:
                self.token_method = (
                    TOKEN_METHOD_MIXED_ESTIMATE
                    if self.token_method == OPENAI_AGENTIC_TOKEN_ENCODING
                    else TOKEN_METHOD_UTF8_QUARTER_ESTIMATE
                )
            else:
                self.token_method = OPENAI_AGENTIC_TOKEN_ENCODING
        if self._encoding is None:
            return _utf8_quarter_token_count(text)
        return len(self._encoding.encode(text, disallowed_special=()))

    def include_context(self, value: str) -> int:
        """Add one text attachment to every subsequent request context."""
        self.transcript_tokens += self._count(value)
        return self.transcript_tokens

    def record_exchange(self, outbound: str, inbound: str) -> int:
        """Record one request using OpenAI-style cumulative total-token semantics."""
        input_tokens = self.transcript_tokens + self._count(outbound)
        output_tokens = self._count(inbound)
        self.total_tokens += input_tokens + output_tokens
        self.transcript_tokens = input_tokens + output_tokens
        return self.total_tokens


__all__ = [
    "OPENAI_AGENTIC_TOKEN_ENCODING",
    "TOKEN_METHOD_MIXED_ESTIMATE",
    "TOKEN_METHOD_UTF8_QUARTER_ESTIMATE",
    "OpenAIEquivalentTokenCounter",
    "cached_openai_agentic_token_encoding",
    "openai_agentic_token_count",
    "openai_agentic_token_encoding",
]
