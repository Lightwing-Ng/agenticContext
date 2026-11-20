"""Read an owner-attested relocation of exact WeChat archive artifacts.

Code version: v1.3.0-codex.0

This optional receipt maps retired file identities to separately verified local
files. Ordinary reads cannot mint it, and changes to any attested file invalidate
every use of that file. The original catalogs and their byte receipts stay intact.
"""

import hashlib
import hmac
import json
from pathlib import Path

from .wechat_browser_snapshot import _read_private, _read_signing_key, _unique


FILENAME = "archive-relocation-v1.proof"
DOMAIN = b"agenticContext:wechat-archive-relocation-v1\0"
NAVIGATION_FILENAME = "navigation-relocation-v1.proof"
NAVIGATION_DOMAIN = b"agenticContext:wechat-navigation-relocation-v1\0"
VERSION = 2
MAX_BYTES = 32 * 1024
HISTORY = "history.sqlite3"
CATALOG = "resource_references.sqlite3"
_DEPENDENCIES = {
    CATALOG: {HISTORY},
    "media_events.sqlite3": {HISTORY},
    "secondary_message_completions.sqlite3": {HISTORY, CATALOG},
    "macos_recovery.sqlite3": {
        HISTORY, CATALOG, "media_events.sqlite3", "media_placeholders.json",
        "resource_revalidated.sqlite3", "macos_recovery.json",
    },
}
_PIN = {"dev", "ino", "size", "mtime_ns", "ctime_ns"}


def receipt_identity(history):
    from .wechat_macos_recovery_bank import _identity

    path = Path(history).with_name(FILENAME)
    try:
        return _identity(path, optional=True)
    except ValueError:
        return {"unavailable": True}


def _read(history):
    before = receipt_identity(history)
    if before is None or before.get("unavailable"):
        return None
    directory = Path(history).parent
    key, key_identity = _read_signing_key(directory / "browser_cache" / "signing.key")
    raw, _ = _read_private(directory / FILENAME, MAX_BYTES)
    signature, payload = raw[:32], raw[32:]
    if not hmac.compare_digest(signature, hmac.digest(key, DOMAIN + payload, "sha256")):
        return None
    value = json.loads(payload, object_pairs_hook=_unique)
    if (
        not isinstance(value, dict) or set(value) != {"version", "files"}
        or type(value["version"]) is not int or value["version"] not in {1, VERSION}
        or not isinstance(value["files"], dict)
        or set(value["files"]) - (set(_DEPENDENCIES) | set.union(*_DEPENDENCIES.values()))
        or not all(
            _valid_record(record, allow_aliases=value["version"] == VERSION)
            for record in value["files"].values()
        )
        or receipt_identity(history) != before
        or _read_signing_key(directory / "browser_cache" / "signing.key")[1] != key_identity
    ):
        return None
    return before, value["files"]


def _is_pin(pin):
    return (
        isinstance(pin, dict) and set(pin) == _PIN
        and all(type(item) is int for item in pin.values())
    )


def _valid_record(record, *, allow_aliases=False):
    if (
        not isinstance(record, dict)
        or not _is_pin(record.get("previous"))
        or not _is_pin(record.get("current"))
    ):
        return False
    if set(record) == {"previous", "current"}:
        return True
    if not allow_aliases or set(record) != {"previous", "current", "previous_aliases"}:
        return False
    aliases = record["previous_aliases"]
    if (
        not isinstance(aliases, list) or not 1 <= len(aliases) <= 8
        or not all(_is_pin(alias) for alias in aliases)
    ):
        return False
    origins = [record["previous"], *aliases]
    return len({frozenset(origin.items()) for origin in origins}) == len(origins)


def _pin_for(pin, model):
    if not _is_pin(pin):
        raise ValueError("A relocation needs complete exact file identities.")
    if not isinstance(model, dict):
        raise ValueError("A relocated input needs a closed file identity.")
    if set(model) == _PIN:
        return pin
    if set(model) == {"dev", "inode", "size", "mtime_ns", "ctime_ns"}:
        return {key: pin["ino" if key == "inode" else key] for key in model}
    if set(model) == {"dev", "inode", "size", "mtime_ns"}:
        return {key: pin["ino" if key == "inode" else key] for key in model}
    raise ValueError("Unsupported relocated file identity.")


def _matches_previous(record, previous):
    origins = (record["previous"], *record.get("previous_aliases", ()))
    return any(_pin_for(origin, previous) == previous for origin in origins)


def matches_history(history, previous, current):
    """Check a history pin for a reader that verifies its own artifact separately."""
    from .wechat_macos_recovery_bank import _identity

    history = Path(history)
    if history.name != HISTORY:
        return False
    try:
        receipt = _read(history)
        if receipt is None:
            return False
        receipt_pin, files = receipt
        record = files[HISTORY]
        return (
            _matches_previous(record, previous)
            and _pin_for(record["current"], current) == current
            and _identity(history) == record["current"]
            and receipt_identity(history) == receipt_pin
        )
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False


def rebase_navigation_inputs(history, previous, current, encoded, key):
    """Pin a signed historical summary without using it as a current timeline."""
    from .wechat_macos_recovery_bank import _identity

    history = Path(history)
    if history.name != HISTORY or not isinstance(previous, dict) or not isinstance(current, dict):
        return None
    relocation_path = str(history.with_name(FILENAME))
    if set(previous) | {relocation_path} != set(current) or relocation_path in previous:
        return None
    try:
        proof_path = history.parent / "browser_cache" / NAVIGATION_FILENAME
        raw, _ = _read_private(proof_path, MAX_BYTES)
        signature, body = raw[:32], raw[32:]
        if not hmac.compare_digest(signature, hmac.digest(key, NAVIGATION_DOMAIN + body, "sha256")):
            return None
        navigation = json.loads(body, object_pairs_hook=_unique)
        if (
            set(navigation) != {"snapshot_sha256", "contacts"}
            or navigation["snapshot_sha256"] != hashlib.sha256(encoded).hexdigest()
        ):
            return None
        receipt = _read(history)
        if receipt is None or receipt[0] != current[relocation_path]:
            return None
        receipt_pin, files = receipt
        contacts = navigation["contacts"]
        if not _valid_record(contacts):
            return None
        records = {**files, "contacts.sqlite3": contacts}
        rebased = {relocation_path: receipt_pin}
        for path, old in previous.items():
            new = current[path]
            if old == new:
                rebased[path] = old
                continue
            name = Path(path).name
            if path != str(history.parent.resolve() / name):
                return None
            record = records[name]
            if _pin_for(record["previous"], old) != old:
                return None
            local = _identity(history.with_name(name))
            if _pin_for(local, new) != new:
                return None
            if name == "macos_recovery.sqlite3":
                # The normal preview rules admit a later bank without reading its
                # messages. All other sources still need exact relocation evidence.
                rebased[path] = _pin_for(record["current"], new)
            else:
                if local != record["current"]:
                    return None
                rebased[path] = new
        if (
            receipt_identity(history) != receipt_pin
            or _read_private(proof_path, MAX_BYTES)[0] != raw
        ):
            return None
        return rebased
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def matches(history, reader, previous, current):
    """Accept only the exact inputs and reader artifact identified by the receipt."""
    from .wechat_macos_recovery_bank import _identity

    if reader not in _DEPENDENCIES or not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    if set(previous) != _DEPENDENCIES[reader] or set(current) != set(previous):
        return False
    history = Path(history)
    if history.name != HISTORY:
        return False
    try:
        receipt = _read(history)
        if receipt is None:
            return False
        receipt_pin, files = receipt

        def check(name):
            record = files[name]
            on_disk = _identity(history.with_name(name))
            if on_disk != record["current"]:
                raise ValueError("A relocated file changed after verification.")
            return record

        check(reader)
        for name, old in previous.items():
            new = current[name]
            if old is None and new is None:
                if _identity(history.with_name(name), optional=True) is not None:
                    return False
                continue
            record = check(name)
            if not _matches_previous(record, old) or _pin_for(record["current"], new) != new:
                return False
        return receipt_identity(history) == receipt_pin
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return False
