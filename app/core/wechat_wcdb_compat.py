"""Decode bounded WCDB cells through the existing compatible interpreter resolver.

Code version: v1.0.0-codex.0

Only an unavailable standard-library backend delegates to the fixed local
worker. Message bytes travel through stdin, never command arguments or files.
The original decoder remains authoritative for framing, dictionaries and limits.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import RLock

from .wechat_wcdb import (
    MAX_TEXT_BYTES, MissingDictionaryError, WCDBBackendUnavailableError,
    WCDBDecodeError, decode_wcdb_text,
)


VERSION = "v1.0.0-codex.0"
MAX_INPUT_BYTES = MAX_TEXT_BYTES + 65_536
MAX_REQUEST_BYTES = 4 * 1024**2
MAX_RESPONSE_BYTES = 16 * 1024**2
MAX_WORKER_SECONDS = 20
MAX_CACHE_ENTRIES = 32
MAX_CACHE_BYTES = 8 * 1024**2
_CACHE = OrderedDict()
_CACHE_BYTES = 0
_LOCK = RLock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@lru_cache(maxsize=4)
def _runtime(configured):
    from .wechat_mac_refresh_worker import _compatible_python, _worker_environment

    root = Path(__file__).resolve().parents[2]
    environment = _worker_environment(root)
    return _compatible_python(root, environment)


def _delegated(raw, marker, limit):
    from .wechat_mac_refresh_worker import _worker_environment

    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256(raw).hexdigest()
    request = _json({
        "schema_version": 1, "native_sha256": digest,
        "value_base64": base64.b64encode(raw).decode(),
        "marker": marker, "limit": limit,
    }).encode()
    if len(request) > MAX_REQUEST_BYTES:
        raise WCDBDecodeError("A compatible WCDB request exceeds its byte budget.")
    try:
        executable = _runtime(os.environ.get("AGENTIC_CONTEXT_WECHAT_PYTHON", ""))
        result = subprocess.run(
            [executable, "-B", "-m", "app.core.wechat_wcdb_compat"],
            input=request, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=root, env=_worker_environment(root),
            timeout=MAX_WORKER_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
        raise WCDBBackendUnavailableError(
            "The bounded compatible WCDB decoder is unavailable."
        ) from error
    if result.returncode or len(result.stdout) > MAX_RESPONSE_BYTES:
        raise WCDBDecodeError("The compatible WCDB decoder returned an invalid frame.")
    try:
        response = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise WCDBDecodeError("The compatible WCDB decoder returned invalid metadata.") from error
    if (
        not isinstance(response, dict)
        or type(response.get("schema_version")) is not int
        or response["schema_version"] != 1
        or response.get("native_sha256") != digest
    ):
        raise WCDBDecodeError("A compatible WCDB response lost its exact input hash.")
    if response.get("status") == "missing_dictionary":
        dictionary = response.get("dictionary_id")
        if type(dictionary) is not int or dictionary <= 0:
            raise WCDBDecodeError("The compatible dictionary requirement is invalid.")
        raise MissingDictionaryError(dictionary)
    text = response.get("decoded_text")
    if response.get("status") != "ok" or not isinstance(text, str):
        raise WCDBDecodeError("The compatible WCDB decoder rejected the retained cell.")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError as error:
        raise WCDBDecodeError("A compatible WCDB response is not valid UTF-8.") from error
    if (
        len(encoded) > limit
        or response.get("decoded_sha256") != hashlib.sha256(encoded).hexdigest()
    ):
        raise WCDBDecodeError("A compatible WCDB response changed its text or bounds.")
    return text


def decode_wcdb_text_compatible(value, marker, *, limit=MAX_TEXT_BYTES):
    """Reuse the native decoder, delegating only its missing-backend condition."""
    global _CACHE_BYTES

    try:
        return decode_wcdb_text(value, marker, limit=limit)
    except WCDBBackendUnavailableError:
        pass
    if (
        not isinstance(value, (bytes, bytearray, memoryview))
        or len(value) > MAX_INPUT_BYTES
        or type(limit) is not int
        or not 0 <= limit <= MAX_TEXT_BYTES
    ):
        raise WCDBDecodeError("A compatible WCDB cell exceeds its explicit bounds.")
    raw = bytes(value)
    key = hashlib.sha256(raw).hexdigest(), marker, limit
    with _LOCK:
        if key in _CACHE:
            text, _size = _CACHE[key]
            _CACHE.move_to_end(key)
            return text
    text = _delegated(raw, marker, limit)
    size = len(text.encode())
    if size <= MAX_CACHE_BYTES:
        with _LOCK:
            previous = _CACHE.pop(key, None)
            if previous is not None:
                _CACHE_BYTES -= previous[1]
            _CACHE[key] = text, size
            _CACHE_BYTES += size
            while len(_CACHE) > MAX_CACHE_ENTRIES or _CACHE_BYTES > MAX_CACHE_BYTES:
                _CACHE_BYTES -= _CACHE.popitem(last=False)[1][1]
    return text


def _worker_main():
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    response = {"schema_version": 1, "status": "rejected", "native_sha256": ""}
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Request byte budget exceeded.")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {
            "schema_version", "native_sha256", "value_base64", "marker", "limit"
        } or type(request["schema_version"]) is not int or request["schema_version"] != 1:
            raise ValueError("Unsupported worker request.")
        value = base64.b64decode(request["value_base64"], validate=True)
        digest = hashlib.sha256(value).hexdigest()
        response["native_sha256"] = digest
        if (
            len(value) > MAX_INPUT_BYTES
            or digest != request["native_sha256"]
            or type(request["limit"]) is not int
            or not 0 <= request["limit"] <= MAX_TEXT_BYTES
        ):
            raise ValueError("The worker input changed its bounds or digest.")
        text = decode_wcdb_text(value, request["marker"], limit=request["limit"])
        response.update(
            status="ok", decoded_text=text,
            decoded_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
    except MissingDictionaryError as error:
        response.update(status="missing_dictionary", dictionary_id=error.dictionary_id)
    except (ValueError, TypeError, KeyError, UnicodeError, RuntimeError):
        pass
    encoded = _json(response).encode()
    if len(encoded) > MAX_RESPONSE_BYTES:
        return 1
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
