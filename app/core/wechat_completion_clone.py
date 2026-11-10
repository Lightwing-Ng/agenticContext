"""Clone only completion catalogs without relaxing preserved-archive policies.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile

from .owner_only_files import ensure_owner_only, owner_only_problem
from .wechat_export_merge import _clonefile


VERSION = "v1.1.0-codex.0"
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"
COPY_BLOCK_BYTES = 8 * 1024**2
_SHA = re.compile(r"[0-9a-f]{64}")
_IDENTITY_FIELDS = ("dev", "ino", "size", "mtime_ns", "ctime_ns")


def sync_completion_file(path):
    """Flush a private derived file using a Windows-compatible writable handle."""
    with Path(path).open("r+b" if IS_WINDOWS else "rb") as stream:
        os.fsync(stream.fileno())


def sync_completion_directory(path):
    """Retain POSIX directory durability without opening a directory on Windows."""
    if IS_WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _identity(path):
    state = Path(path).lstat()
    if (
        not stat.S_ISREG(state.st_mode)
        or getattr(state, "st_flags", 0) & 0x40000000
        or any(
            os.path.lexists(str(path) + suffix)
            for suffix in ("-wal", "-shm", "-journal")
        )
    ):
        raise ValueError("A completion copy needs one stable resident regular file.")
    return {key: getattr(state, "st_" + key) for key in _IDENTITY_FIELDS}


def _descriptor_identity(stream):
    state = os.fstat(stream.fileno())
    return {key: getattr(state, "st_" + key) for key in _IDENTITY_FIELDS}


def _catalog_contract(path):
    connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        info = dict(
            connection.execute(
                "SELECT key,value FROM archive_info WHERE key IN "
                "('schema_version','version','staging_state')"
            )
        )
        if (
            info.get("schema_version") != "1"
            or info.get("version") != "v1.0.0-codex.0"
            or info.get("staging_state") not in {None, "complete"}
        ):
            raise ValueError(
                "Only completed derived completion catalogs may be copied."
            )
        for table, required in (
            (
                "entries",
                {"id", "operation", "projected_sha256", "source_key", "witness_key"},
            ),
            ("messages", {"id", "conversation_id", "provenance"}),
            ("sources", {"id", "header", "sha256"}),
            ("witnesses", {"id", "source_key", "message", "sha256"}),
        ):
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")
            }
            if not required <= columns:
                raise ValueError(
                    "Only derived completion tables may use the copy path."
                )
    except sqlite3.Error as error:
        raise ValueError(
            "Only validated derived completion catalogs may be copied."
        ) from error
    finally:
        connection.close()


def _copy_bytes(source, output, size):
    digest = hashlib.sha256()
    remaining = size
    while block := source.read(min(COPY_BLOCK_BYTES, remaining + 1)):
        remaining -= len(block)
        if remaining < 0:
            raise ValueError("The completion source grew during bounded copying.")
        if output is not None:
            output.write(block)
        digest.update(block)
    return digest.hexdigest()


def clone_completion_catalog(
    source,
    destination,
    *,
    expected_identity,
    expected_sha256=None,
):
    """Keep APFS mandatory on macOS; privately copy only derived catalogs elsewhere.

    Callers retain semantic validation and publication locks. The non-macOS path
    verifies the completion contract and caller identity before reading bytes,
    then publishes its private, verified copy with an atomic no-replace link.
    The source archive/history/bank cloning API remains unchanged.
    """
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if _identity(source) != expected_identity:
        raise ValueError("The validated completion source identity changed.")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or _SHA.fullmatch(expected_sha256) is None
    ):
        raise ValueError("A reviewed completion copy needs its exact SHA-256.")
    if os.path.lexists(destination) or source == destination:
        raise ValueError("Select an absent private completion destination.")
    if IS_MACOS:
        _clonefile(source, destination)
        return
    parent = destination.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode):
        raise ValueError("A completion destination needs a regular directory.")
    _catalog_contract(source)
    if _identity(source) != expected_identity:
        raise ValueError(
            "The validated completion source changed during header checks."
        )
    temporary = None
    temporary_identity = None
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as original:
            if _descriptor_identity(original) != expected_identity:
                raise ValueError("The opened completion source identity changed.")
            if expected_sha256 is None:
                expected_sha256 = _copy_bytes(original, None, expected_identity["size"])
                if _identity(source) != expected_identity:
                    raise ValueError(
                        "The completion source changed during checksum validation."
                    )
                original.seek(0)
            descriptor, name = tempfile.mkstemp(
                prefix=".completion-copy-", suffix=".sqlite3", dir=destination.parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as output:
                temporary_identity = _descriptor_identity(output)
                ensure_owner_only(temporary)
                temporary_identity = _descriptor_identity(output)
                if _identity(temporary) != temporary_identity:
                    raise ValueError("The private completion copy was replaced.")
                digest = _copy_bytes(original, output, expected_identity["size"])
                output.flush()
                os.fsync(output.fileno())
                if (
                    _descriptor_identity(original) != expected_identity
                    or _identity(source) != expected_identity
                    or digest != expected_sha256
                    or output.tell() != expected_identity["size"]
                ):
                    raise ValueError(
                        "The completion source or reviewed SHA-256 changed during copying."
                    )
                copied_identity = _descriptor_identity(output)
                if _identity(temporary) != copied_identity or owner_only_problem(
                    temporary
                ):
                    raise ValueError(
                        "The private completion copy lost its verified boundary."
                    )
            if (
                destination.parent.lstat().st_dev != parent.st_dev
                or destination.parent.lstat().st_ino != parent.st_ino
                or _identity(source) != expected_identity
            ):
                raise ValueError("A private completion input changed before promotion.")
            # Hard-link creation is an atomic absent-name compare-and-swap on
            # both supported non-macOS platforms; it never overwrites a racer.
            os.link(temporary, destination)
            linked = temporary.lstat()
            if (linked.st_dev, linked.st_ino) != (
                copied_identity["dev"],
                copied_identity["ino"],
            ):
                raise ValueError("The private completion temporary name was replaced.")
            temporary.unlink()
            temporary = None
            # Link/unlink changes ctime. Retain the verified inode, byte count,
            # and mtime while checking the final owner-only boundary.
            published = _identity(destination)
            if any(
                published[key] != copied_identity[key]
                for key in ("dev", "ino", "size", "mtime_ns")
            ) or owner_only_problem(destination):
                raise ValueError("The promoted completion copy identity changed.")
    finally:
        if temporary is not None and temporary_identity is not None:
            try:
                state = temporary.lstat()
                if (state.st_dev, state.st_ino) == (
                    temporary_identity["dev"],
                    temporary_identity["ino"],
                ):
                    temporary.unlink()
            except FileNotFoundError:
                pass
