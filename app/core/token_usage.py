"""Provider-neutral observable token accounting.

Code version: v1.0.0-codex.0

Browser providers and the MCP Tunnel expose different request lifecycles, but both
need the same cached tokenizer without importing either orchestration runtime. Hidden
provider prompts, reasoning tokens, and billing adjustments remain out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import tiktoken


OPENAI_AGENTIC_TOKEN_ENCODING = "o200k_base"


@lru_cache(maxsize=1)
def openai_agentic_token_encoding() -> Any | None:
    """Return the tokenizer without making metrics depend on a cache download."""
    try:
        return tiktoken.get_encoding(OPENAI_AGENTIC_TOKEN_ENCODING)
    except Exception:  # A metric must not depend on external TLS trust or reachability.
        return None


def openai_agentic_token_count(value: str) -> int:
    """Count text with o200k_base, or estimate locally when unavailable."""
    text = str(value or "")
    encoding = openai_agentic_token_encoding()
    if encoding is None:
        return (len(text.encode("utf-8")) + 3) // 4
    return len(encoding.encode(text, disallowed_special=()))


@dataclass(slots=True)
class OpenAIEquivalentTokenCounter:
    """Accumulate observable input-plus-output tokens for one Agent task."""

    total_tokens: int = 0
    transcript_tokens: int = 0

    def include_context(self, value: str) -> int:
        """Add one text attachment to every subsequent request context."""
        self.transcript_tokens += openai_agentic_token_count(value)
        return self.transcript_tokens

    def record_exchange(self, outbound: str, inbound: str) -> int:
        """Record one request using OpenAI-style cumulative total-token semantics."""
        input_tokens = self.transcript_tokens + openai_agentic_token_count(outbound)
        output_tokens = openai_agentic_token_count(inbound)
        self.total_tokens += input_tokens + output_tokens
        self.transcript_tokens = input_tokens + output_tokens
        return self.total_tokens


__all__ = [
    "OPENAI_AGENTIC_TOKEN_ENCODING",
    "OpenAIEquivalentTokenCounter",
    "openai_agentic_token_count",
    "openai_agentic_token_encoding",
]
