"""The local JSON action protocol: parsing one model response, rendering one result.

This is pure text transformation with no browser, workspace, or service dependency, so
every entry point that has to read or present a controller action shares it: the Agent
run loop, the Web layer's response rendering, and the tests that pin the format.
"""

# Code version: v1.0.1-codex.0

from __future__ import annotations

import json
import re
from typing import Any

MAX_ACTION_JSON_CHARS = 800_000

_FENCED_JSON_RE = re.compile(
    r"```json\s*\n(.*?)\n\s*```", re.DOTALL
)

_PRE_CODE_RE = re.compile(
    r"<pre>\s*<code(?:\s[^>]*)?>(.*?)</code>\s*</pre>", re.DOTALL | re.IGNORECASE
)


def _truncate_text(value: str, maximum: int) -> str:
    """Bound one display-only detail without depending on workspace execution."""
    text = str(value or "")
    if len(text) <= maximum:
        return text
    omitted = len(text) - maximum
    return text[:maximum] + f"\n[truncated {omitted:,} characters]"


class _StrictActionJSONError(ValueError):
    """Reject JSON extensions or object ambiguity before action validation."""


def _strict_action_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _StrictActionJSONError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def _reject_action_json_constant(value: str) -> None:
    raise _StrictActionJSONError(f"non-finite JSON constant: {value}")


def _strict_action_json_loads(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_strict_action_object,
        parse_constant=_reject_action_json_constant,
    )


def _mask_regions(text: str, regions: list[tuple[int, int]]) -> str:
    """Replace selected spans with spaces so raw_decode cannot repair them."""
    if not regions:
        return text
    chars = list(text)
    for start, end in regions:
        for index in range(max(0, start), min(len(chars), end)):
            chars[index] = " "
    return "".join(chars)


def parse_agent_action(response: str) -> dict[str, Any]:
    """Parse one JSON controller action across provider formatting variants.

    Collect valid candidates from the whole response in document order:
    1. Literal ```json fences (never repaired on JSONDecodeError).
    2. Literal <pre><code> blocks (never repaired on JSONDecodeError).
    3. Strict json.loads on the remaining response.
    4. raw_decode scanning of the remaining response.

    Byte-equivalent action objects are de-duplicated. Any two distinct action
    objects, duplicate object keys, non-finite constants, or malformed
    structured blocks reject the entire response before execution.
    """
    text = str(response or "").strip()
    if len(text) > MAX_ACTION_JSON_CHARS:
        raise ValueError("The Web provider returned an action that exceeds the controller limit.")

    ordered: list[tuple[int, dict[str, Any]]] = []
    candidate_signatures: set[str] = set()
    decoder = json.JSONDecoder(
        object_pairs_hook=_strict_action_object,
        parse_constant=_reject_action_json_constant,
    )
    masked_regions: list[tuple[int, int]] = []
    malformed_structured = False

    def register(payload: Any, position: int) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
            raise ValueError(
                "The Web provider returned a JSON value that is not a controller action."
            )
        signature = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if signature not in candidate_signatures:
            candidate_signatures.add(signature)
            ordered.append((position, payload))

    def resolve_candidates() -> dict[str, Any]:
        candidates = [payload for _position, payload in sorted(ordered, key=lambda item: item[0])]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ValueError("The Web provider returned more than one JSON controller action.")
        raise ValueError("The Web provider must return exactly one JSON controller action.")

    for match in _FENCED_JSON_RE.finditer(text):
        masked_regions.append((match.start(), match.end()))
        try:
            register(_strict_action_json_loads(match.group(1).strip()), match.start())
        except (json.JSONDecodeError, _StrictActionJSONError):
            malformed_structured = True

    for match in _PRE_CODE_RE.finditer(text):
        masked_regions.append((match.start(), match.end()))
        try:
            register(_strict_action_json_loads(match.group(1).strip()), match.start())
        except (json.JSONDecodeError, _StrictActionJSONError):
            malformed_structured = True

    for opening in re.finditer(r"```json\b", text, flags=re.IGNORECASE):
        if not any(start <= opening.start() < end for start, end in masked_regions):
            malformed_structured = True
    for opening in re.finditer(
        r"<pre>\s*<code(?:\s[^>]*)?>",
        text,
        flags=re.IGNORECASE,
    ):
        if not any(start <= opening.start() < end for start, end in masked_regions):
            malformed_structured = True

    if malformed_structured:
        raise ValueError(
            "The Web provider returned a structured JSON block that is not valid strict JSON. "
            "Use replace_base64 or write_base64 for content containing HTML quotes or backslashes."
        )

    remainder = _mask_regions(text, masked_regions)
    try:
        register(_strict_action_json_loads(remainder.strip()), 0)
    except _StrictActionJSONError as exc:
        raise ValueError(
            f"The Web provider returned JSON that is not valid strict JSON: {exc}."
        ) from exc
    except json.JSONDecodeError:
        cursor = 0
        while cursor < len(remainder):
            object_start = remainder.find("{", cursor)
            array_start = remainder.find("[", cursor)
            starts = [index for index in (object_start, array_start) if index >= 0]
            start = min(starts) if starts else -1
            if start < 0:
                break
            try:
                payload, end = decoder.raw_decode(remainder, start)
            except _StrictActionJSONError as exc:
                raise ValueError(
                    f"The Web provider returned JSON that is not valid strict JSON: {exc}."
                ) from exc
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "The Web provider returned a malformed JSON-like value before its "
                    "controller action."
                ) from exc
            register(payload, start)
            cursor = end

    return resolve_candidates()


def render_final_action(payload: dict[str, Any]) -> str:
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise ValueError("The final action requires a summary.")
    parts = [summary]
    verification = payload.get("verification")
    if isinstance(verification, list) and verification:
        parts.append("\nVerification\n" + "\n".join(f"- {str(item)}" for item in verification[:20]))
    limitations = payload.get("limitations")
    if isinstance(limitations, list) and limitations:
        parts.append("\nLimitations\n" + "\n".join(f"- {str(item)}" for item in limitations[:20]))
    return "\n".join(parts).strip()


def activity_detail(action: dict[str, Any]) -> str:
    if str(action.get("action") or "").strip().lower() == "browser_acceptance":
        root = str(action.get("root") or ".").strip() or "."
        target = str(action.get("target") or "/").strip() or "/"
        return _truncate_text(f"{root} → {target} (desktop + narrow Chromium)", 180)
    for key in ("path", "query", "command"):
        value = str(action.get(key) or "").strip()
        if value:
            return _truncate_text(value, 180)
    return "Local controller"


__all__ = [
    "MAX_ACTION_JSON_CHARS",
    "activity_detail",
    "parse_agent_action",
    "render_final_action",
]
