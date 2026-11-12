"""Reuse the complete Mac recovery bank proof across browser restarts.

Code version: v1.0.0-codex.0
"""

import hmac
import json
from pathlib import Path
import sqlite3
import sys

from .wechat_browser_snapshot import (
    _cache_storage,
    _code_fingerprint,
    _directory_identity,
    _json,
    _read_private,
    _read_signing_key,
    _state,
    _unique,
    _write_private_cache,
)


_FILENAME = "macos-recovery-bank.validation"
_DOMAIN = b"agenticContext:macos-recovery-bank-validation-v1\x00"
_SIGNATURE_BYTES = 32
_MAX_PROOF_BYTES = 128 * 1024


def proof_pin(path, history, identity, inputs):
    """Capture the inputs and verifier before starting the expensive proof."""
    try:
        return {
            "bank": str(path.resolve()),
            "history": str(Path(history).resolve()),
            "identity": identity,
            "inputs": inputs,
            "code": _code_fingerprint(),
            "python": sys.version,
            "sqlite": sqlite3.sqlite_version,
        }
    except (OSError, ValueError, TypeError):
        return None


def _signature(key, payload):
    return hmac.digest(key, _DOMAIN + payload, "sha256")


def _directory_pin(directory):
    # Ignore link counts and timestamps changed by atomic cache publication.
    state = _directory_identity(directory)
    return state[:3], state[4]


def _unchanged(directory, identity, key_identity, pin, sources_unchanged):
    return (
        _directory_pin(directory) == identity
        and _state((directory / "signing.key").lstat()) == key_identity
        and _code_fingerprint() == pin["code"]
        and sources_unchanged()
    )


def read_proof(directory, pin, sources_unchanged):
    """Return the whole-bank validation metadata on an authenticated exact hit."""
    if pin is None:
        return None
    try:
        identity = _directory_pin(directory)
        key, key_identity = _read_signing_key(directory / "signing.key")
        path = directory / _FILENAME
        encoded, proof_identity = _read_private(path, _MAX_PROOF_BYTES)
        signature, payload = encoded[:_SIGNATURE_BYTES], encoded[_SIGNATURE_BYTES:]
        if not hmac.compare_digest(signature, _signature(key, payload)):
            return None
        value = json.loads(payload, object_pairs_hook=_unique)
        if (
            not isinstance(value, dict)
            or value.keys() != {"pin", "metadata"}
            or value["pin"] != pin
            or not isinstance(value["metadata"], dict)
            or not _unchanged(directory, identity, key_identity, pin, sources_unchanged)
            or _state(path.lstat()) != proof_identity
        ):
            return None
        return value["metadata"]
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def write_proof(directory, pin, metadata, sources_unchanged):
    """Publish only the result of a completed proof whose inputs remain pinned."""
    if pin is None:
        return
    try:
        if _code_fingerprint() != pin["code"] or not sources_unchanged():
            return
        payload = _json({"pin": pin, "metadata": metadata})
        if len(payload) + _SIGNATURE_BYTES > _MAX_PROOF_BYTES:
            return
        directory, key, key_identity = _cache_storage(directory)
        identity = _directory_pin(directory)

        def unchanged():
            return _unchanged(directory, identity, key_identity, pin, sources_unchanged)

        _write_private_cache(
            directory / _FILENAME, _signature(key, payload) + payload,
            unchanged, prefix=".macos-recovery-validation-",
        )
    except (OSError, ValueError, TypeError, RecursionError):
        # A cache miss must leave the existing verified read available.
        return
