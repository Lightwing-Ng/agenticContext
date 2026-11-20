"""Keep bounded, presentation-only web refresh receipts across process restarts.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

from .owner_only_files import (
    ensure_owner_only,
    ensure_owner_only_directory,
    owner_only_directory_problem,
    owner_only_problem,
    write_owner_only_text,
)
from .platform_lock import lock_file
from .wechat_mac_refresh_source import RefreshIssue
from .wechat_mac_refresh_status import FAILED_MESSAGE, MAX_COUNTER, SAFE_FAILURE_MESSAGES, safe_progress


_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,128}")
_RECEIPT = re.compile(r"[0-9a-f]{64}\.json")
_TEMP_RECEIPT = re.compile(r"\.[0-9a-f]{64}\.json\.tmp")
_STATES = {"running", "succeeded", "failed", "interrupted"}
_MAX_RECEIPTS = 32
_MAX_BYTES = 4_096
_RETAIN_SECONDS = 7 * 24 * 60 * 60
_SAFE_OPEN = (
    getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
)
_MESSAGES = {
    *(issue.value for issue in RefreshIssue),
    *SAFE_FAILURE_MESSAGES,
    "Refreshing chat history.",
    "Chat history refreshed.",
    "Available chat history refreshed; some sources or messages remain unavailable.",
}


def interrupted_status(snapshot, *, publication_possible):
    """Report interrupted work without inferring an archive outcome."""
    return {
        **snapshot,
        "state": "interrupted",
        "partial": True,
        "publication_unknown": publication_possible,
        "message": (
            "The local service stopped before confirming this refresh. Reload the saved chat "
            "history to check for updates before trying again."
            if publication_possible else
            "The local service stopped before this refresh finished. You can try again."
        ),
    }


def _presentation(snapshot):
    state = snapshot["state"]
    if state not in _STATES:
        raise ValueError("The refresh receipt has an invalid state.")
    result = {"state": state, **safe_progress(snapshot, require_phase=True)}
    message = snapshot.get("message")
    result["message"] = (
        message if isinstance(message, str) and message in _MESSAGES else FAILED_MESSAGE
    )
    for field in ("added", "total"):
        value = snapshot[field]
        if type(value) is not int or not 0 <= value <= MAX_COUNTER:
            raise ValueError("The refresh receipt has an invalid count.")
        result[field] = value
    return result


class WechatRefreshReceipts:
    """Store only validated public progress and coordinate one active web job."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.active_path = directory / ".active.lock"

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _owner_directory(self, *, create=False):
        if self.directory.parent.is_symlink():
            raise OSError("The refresh receipt parent is not a regular directory.")
        if create:
            try:
                self.directory.mkdir(mode=0o700, parents=True)
            except FileExistsError:
                pass
            else:
                ensure_owner_only_directory(self.directory)
        if owner_only_directory_problem(self.directory):
            raise OSError("The refresh receipt directory is unavailable.")

    def _open_active(self, *, create=False):
        flags = os.O_RDWR | _SAFE_OPEN
        if create:
            flags |= os.O_CREAT
        descriptor = os.open(self.active_path, flags, 0o600)
        try:
            if create:
                ensure_owner_only(self.active_path)
            if owner_only_problem(self.active_path) or not os.path.samestat(
                os.fstat(descriptor), self.active_path.lstat(),
            ):
                raise OSError("The refresh receipt lease is not an unchanged private file.")
            return os.fdopen(descriptor, "r+b")
        except BaseException:
            os.close(descriptor)
            raise

    def claim(self, token):
        """Return a held lease, or None if another web job is still active."""
        self._owner_directory(create=True)
        handle = self._open_active(create=True)
        try:
            lock_file(handle, blocking=False)
        except BlockingIOError:
            handle.close()
            return None
        except BaseException:
            handle.close()
            raise
        try:
            handle.seek(0)
            handle.write(self._key(token).encode("ascii"))
            handle.truncate()
            handle.flush()
            self._prune()
            return handle
        except BaseException:
            handle.close()
            raise

    def _prune(self):
        now = time.time()
        receipts = []
        for path in self.directory.iterdir():
            if _RECEIPT.fullmatch(path.name) and not owner_only_problem(path):
                receipts.append((path.stat().st_mtime, path))
            elif _TEMP_RECEIPT.fullmatch(path.name) and not owner_only_problem(path):
                path.unlink(missing_ok=True)
        for index, (modified, path) in enumerate(sorted(receipts, reverse=True)):
            if index >= _MAX_RECEIPTS - 1 or now - modified > _RETAIN_SECONDS:
                path.unlink(missing_ok=True)

    def write(self, token, snapshot, *, publication_possible):
        self._owner_directory()
        record = {
            "schema_version": 1,
            "snapshot": _presentation(snapshot),
            "publication_possible": publication_possible,
        }
        write_owner_only_text(self.directory / f"{self._key(token)}.json", json.dumps(record))

    def _has_live_job(self, key):
        try:
            with self._open_active() as handle:
                try:
                    lock_file(handle, blocking=False)
                except BlockingIOError:
                    handle.seek(0)
                    return handle.read(65) == key.encode("ascii")
        except FileNotFoundError:
            pass
        return False

    def _read_receipt(self, path):
        if owner_only_problem(path):
            return None
        with os.fdopen(os.open(path, os.O_RDONLY | _SAFE_OPEN), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not os.path.samestat(opened, path.lstat()) or owner_only_problem(path)
                or time.time() - opened.st_mtime > _RETAIN_SECONDS
            ):
                return None
            payload = stream.read(_MAX_BYTES + 1)
        if len(payload) > _MAX_BYTES:
            return None
        record = json.loads(payload)
        if (
            type(record["schema_version"]) is not int or record["schema_version"] != 1
            or type(record["publication_possible"]) is not bool
        ):
            return None
        return _presentation(record["snapshot"]), record["publication_possible"]

    def read(self, token):
        if not _TOKEN.fullmatch(token):
            return None
        key = self._key(token)
        path = self.directory / f"{key}.json"
        try:
            self._owner_directory()
            receipt = self._read_receipt(path)
            if receipt is None:
                return None
            snapshot, publication_possible = receipt
            if snapshot["state"] == "running":
                if self._has_live_job(key):
                    return snapshot
                # The original worker can finish between our read and the lease check.
                latest = self._read_receipt(path)
                if latest is not None:
                    snapshot, publication_possible = latest
            if snapshot["state"] in {"running", "interrupted"}:
                return interrupted_status(snapshot, publication_possible=publication_possible)
            return snapshot
        except (KeyError, OSError, TypeError, ValueError):
            return None
