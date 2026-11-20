"""Persist and resume an interrupted coordinated WeChat exchange publication.

Code version: v1.1.1-codex.0
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from .wechat_archive import _file_identity


PUBLICATION_JOURNAL_NAME = ".wechat-exchange-publication.json"
_TARGETS = {
    "history.sqlite3", "resource_references.sqlite3",
    "resource_revalidated.sqlite3", "media_events.sqlite3",
}
_STATE_FILES = _TARGETS | {"contacts.sqlite3", "exclusions.json", "source_exclusions.json"}


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular(path):
    state = path.lstat()
    if (
        not stat.S_ISREG(state.st_mode)
        or state.st_nlink != 1
        or getattr(state, "st_flags", 0) & 0x40000000
    ):
        raise ValueError("Exchange recovery requires resident files without links.")
    if any(
        os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("Exchange recovery refuses an active database journal.")
    return state


def _pin(path):
    _regular(path)
    before = _file_identity(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024**2):
            digest.update(chunk)
    if _file_identity(path) != before:
        raise ValueError("An exchange recovery input changed during verification.")
    return {"identity": before, "sha256": digest.hexdigest()}


def _valid_pin(pin):
    return (
        isinstance(pin, dict)
        and set(pin) == {"identity", "sha256"}
        and isinstance(pin["identity"], dict)
        and set(pin["identity"]) == {"dev", "inode", "size", "mtime_ns"}
        and all(type(value) is int for value in pin["identity"].values())
        and isinstance(pin["sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]) is not None
    )


def _temporary_name(name, prefix):
    return (
        isinstance(name, str)
        and re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+\.sqlite3", name)
        is not None
    )


def prepare_publication(directory, replacements, backups, state):
    """Durably publish recovery instructions before replacing any canonical file."""
    directory = Path(directory)
    journal = directory / PUBLICATION_JOURNAL_NAME
    if os.path.lexists(journal):
        raise ValueError("An earlier exchange publication requires recovery.")
    entries = []
    for target, candidate in replacements.items():
        backup = backups.get(target)
        old = _pin(target) if target.exists() else None
        backup_pin = _pin(backup) if backup is not None else None
        if old is not None and (
            backup_pin is None or old["sha256"] != backup_pin["sha256"]
        ):
            raise ValueError("An exchange rollback clone differs from its original.")
        entries.append({
            "target": target.name,
            "candidate": candidate.name,
            "backup": backup.name if backup is not None else None,
            "old": old,
            "new": _pin(candidate),
            "backup_pin": backup_pin,
        })
    payload = {
        "version": 1,
        "phase": "forward",
        "entries": entries,
        "retained": [],
        "untouched": {
            name: pin for name, pin in state.items()
            if name not in {path.name for path in replacements}
        },
    }
    for path in [*replacements.values(), *backups.values()]:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    _write_payload(directory, payload)


def _write_payload(directory, payload):
    journal = directory / PUBLICATION_JOURNAL_NAME
    descriptor, name = tempfile.mkstemp(prefix=".wechat-exchange-journal-", dir=directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, journal)
        _sync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def discard_publication(directory):
    """Remove the journal only after a verified publication or in-process rollback."""
    journal = Path(directory) / PUBLICATION_JOURNAL_NAME
    if os.path.lexists(journal):
        _regular(journal)
        journal.unlink()
        _sync_directory(directory)


def _load(directory):
    journal = directory / PUBLICATION_JOURNAL_NAME
    if _regular(journal).st_size > 65_536:
        raise ValueError("The exchange recovery journal exceeds its bounded schema.")
    payload = json.loads(journal.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "phase", "entries", "untouched", "retained"}
        or type(payload["version"]) is not int or payload["version"] != 1
        or not isinstance(payload["phase"], str)
        or payload["phase"] not in {"forward", "rollback-pending", "rollback"}
        or not isinstance(payload["entries"], list)
        or not 1 <= len(payload["entries"]) <= len(_TARGETS)
        or not isinstance(payload["untouched"], dict)
        or not isinstance(payload["retained"], list)
        or len(payload["retained"]) > 2 * len(_TARGETS)
    ):
        raise ValueError("The exchange recovery journal has an invalid schema.")
    names, artifacts = set(), set()
    for entry in payload["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "target", "candidate", "backup", "old", "new", "backup_pin",
        }:
            raise ValueError("The exchange recovery journal has an invalid entry.")
        name = entry["target"]
        if not isinstance(name, str) or name not in _TARGETS or name in names:
            raise ValueError("Exchange recovery targets must be unique canonical files.")
        names.add(name)
        rollback = payload["phase"] == "rollback"
        prefix = (
            ".wechat-exchange-rollback-stage-" if rollback
            else ".wechat-exchange-stage-" if name == "history.sqlite3"
            else ".wechat-exchange-sidecar-"
        )
        if rollback and entry["new"] is None:
            if entry["candidate"] is not None:
                raise ValueError("An absent rollback target cannot claim a candidate.")
            paths = []
        else:
            if not _temporary_name(entry["candidate"], prefix) or not _valid_pin(entry["new"]):
                raise ValueError("An exchange recovery candidate has an invalid mapping.")
            paths = [entry["candidate"]]
        if rollback:
            if (
                entry["old"] is not None and not _valid_pin(entry["old"])
                or entry["backup"] is not None or entry["backup_pin"] is not None
            ):
                raise ValueError("A rollback recovery entry has an invalid prior state.")
        elif entry["old"] is None:
            if entry["backup"] is not None or entry["backup_pin"] is not None:
                raise ValueError("An absent exchange original cannot claim a backup.")
        elif (
            not _valid_pin(entry["old"])
            or not _valid_pin(entry["backup_pin"])
            or not _temporary_name(entry["backup"], ".wechat-exchange-rollback-")
            or entry["old"]["sha256"] != entry["backup_pin"]["sha256"]
        ):
            raise ValueError("An exchange recovery backup has an invalid mapping.")
        else:
            paths.append(entry["backup"])
        if artifacts.intersection(paths) or len(paths) != len(set(paths)):
            raise ValueError("Exchange recovery artifacts must have distinct mappings.")
        artifacts.update(paths)
    for retained in payload["retained"]:
        if (
            not isinstance(retained, dict) or set(retained) != {"name", "pin"}
            or not _valid_pin(retained["pin"])
            or not any(_temporary_name(retained["name"], prefix) for prefix in (
                ".wechat-exchange-stage-", ".wechat-exchange-sidecar-",
                ".wechat-exchange-rollback-",
            ))
            or retained["name"] in artifacts
        ):
            raise ValueError("An exchange recovery retained artifact has an invalid mapping.")
        artifacts.add(retained["name"])
    if (
        payload["entries"][0]["target"] != "history.sqlite3"
        or set(payload["untouched"]) != _STATE_FILES - names
    ):
        raise ValueError("The exchange recovery journal omits canonical state pins.")
    return payload


def mark_rollback_pending(directory):
    """Persist rollback intent before creating or modifying restoration stages."""
    directory = Path(directory)
    payload = _load(directory)
    if payload["phase"] != "rollback-pending":
        payload["phase"] = "rollback-pending"
        _write_payload(directory, payload)


def _preflight(directory, payload, read_state, no_writers):
    state = read_state()
    if any(state.get(name) != pin for name, pin in payload["untouched"].items()):
        raise ValueError("Untouched archive state changed before exchange recovery.")
    pending, artifacts, verified = [], [], {}
    for entry in payload["entries"]:
        target = directory / entry["target"]
        candidate = directory / entry["candidate"] if entry["candidate"] is not None else None
        if os.path.lexists(target):
            no_writers(target)
            actual = _pin(target)
        else:
            actual = None
        if actual not in (entry["old"], entry["new"]):
            raise ValueError("A canonical archive changed before exchange recovery.")
        verified[target] = actual
        if candidate is not None and os.path.lexists(candidate):
            no_writers(candidate)
            if _pin(candidate) != entry["new"]:
                raise ValueError("An exchange recovery candidate changed.")
            artifacts.append((candidate, entry["new"]))
            verified[candidate] = entry["new"]
        elif candidate is not None and actual != entry["new"]:
            raise ValueError("A required exchange recovery candidate is missing.")
        elif candidate is not None:
            verified[candidate] = None
        if entry["backup"] is not None:
            backup = directory / entry["backup"]
            no_writers(backup)
            if _pin(backup) != entry["backup_pin"]:
                raise ValueError("An exchange recovery backup changed.")
            artifacts.append((backup, entry["backup_pin"]))
            verified[backup] = entry["backup_pin"]
        if actual != entry["new"]:
            pending.append((target, candidate, actual, entry["new"]))
    for retained in payload["retained"]:
        path = directory / retained["name"]
        no_writers(path)
        if _pin(path) != retained["pin"]:
            raise ValueError("An exchange recovery retained artifact changed.")
        artifacts.append((path, retained["pin"]))
        verified[path] = retained["pin"]
    # All files and unrelated source pins pass before the first recovery mutation.
    state = read_state()
    if any(state.get(name) != pin for name, pin in payload["untouched"].items()):
        raise ValueError("Untouched archive state changed during exchange recovery.")
    return pending, artifacts, verified


def capture_rollback_stages(replacements):
    """Keep exact stage pins across the builder-to-journal transition."""
    return replacements, {
        target: _pin(candidate) if candidate is not None else None
        for target, candidate in replacements.items()
    }


def _verify_snapshot(verified, no_writers):
    for path, expected in verified.items():
        if os.path.lexists(path):
            no_writers(path)
            actual = _pin(path)
        else:
            actual = None
        if actual != expected:
            raise ValueError("An exchange rollback input changed during staging.")


def _prepare_rollback(
    directory, previous, replacements, stage_pins, artifacts, verified,
    read_state, no_writers,
):
    scope = {directory / entry["target"] for entry in previous["entries"]}
    if set(replacements) != scope or set(stage_pins) != scope:
        raise ValueError("Rollback stages must preserve the exact publication scope.")
    entries = []
    stage_snapshot = {}
    for target, candidate in replacements.items():
        entries.append({
            "target": target.name,
            "candidate": candidate.name if candidate is not None else None,
            "backup": None,
            "old": verified[target],
            "new": stage_pins[target],
            "backup_pin": None,
        })
        if candidate is not None:
            stage_snapshot[candidate] = stage_pins[target]
            with candidate.open("rb") as stream:
                os.fsync(stream.fileno())
    payload = {
        "version": 1,
        "phase": "rollback",
        "entries": entries,
        "retained": [{"name": path.name, "pin": pin} for path, pin in artifacts],
        "untouched": previous["untouched"],
    }
    # A slow builder must not promote newly observed bytes into an allowed prior state.
    _verify_snapshot({**verified, **stage_snapshot}, no_writers)
    state = read_state()
    if any(state.get(name) != pin for name, pin in previous["untouched"].items()):
        raise ValueError("Untouched archive state changed before rollback staging.")
    _write_payload(directory, payload)


def resume_publication(directory, read_state, no_writers, rollback_builder):
    """Complete only a fully pinned publication while the caller holds maintenance."""
    directory = Path(directory)
    journal = directory / PUBLICATION_JOURNAL_NAME
    if not os.path.lexists(journal):
        return False
    journal_pin = _pin(journal)
    payload = _load(directory)
    if _pin(journal) != journal_pin:
        raise ValueError("The exchange recovery journal changed during verification.")
    pending, artifacts, verified = _preflight(directory, payload, read_state, no_writers)
    verified[journal] = journal_pin
    if payload["phase"] == "rollback-pending":
        replacements, stage_pins = rollback_builder(payload)
        _prepare_rollback(
            directory, payload, replacements, stage_pins, artifacts, verified,
            read_state, no_writers,
        )
        return resume_publication(directory, read_state, no_writers, rollback_builder)
    for target, candidate, before, expected in pending:
        if (_pin(target) if os.path.lexists(target) else None) != before:
            raise ValueError("An exchange recovery target changed during publication.")
        if candidate is not None and _pin(candidate) != expected:
            raise ValueError("An exchange recovery candidate changed during publication.")
        if candidate is None:
            target.unlink()
        else:
            os.replace(candidate, target)
    _sync_directory(directory)
    for entry in payload["entries"]:
        target = directory / entry["target"]
        if (_pin(target) if os.path.lexists(target) else None) != entry["new"]:
            raise ValueError("An exchange recovery target failed its exact readback.")
        if entry["new"] is None:
            continue
        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("An exchange recovery database failed integrity checks.")
    state = read_state()
    if any(state.get(name) != pin for name, pin in payload["untouched"].items()):
        raise ValueError("Untouched archive state changed during exchange recovery.")
    discard_publication(directory)
    for path, expected in artifacts:
        if os.path.lexists(path) and _pin(path) == expected:
            no_writers(path)
            path.unlink()
    _sync_directory(directory)
    return True
