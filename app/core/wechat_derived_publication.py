"""Publish a scoped retirement and preserve only independently current resource pins.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .wechat_archive import _file_identity, wechat_history_partition_fingerprint
from .wechat_derived_retirement import (
    _inputs,
    _json,
    _no_writers,
    _pair,
    _readonly,
    file_identity,
)
from .wechat_export_merge import _clonefile


VERSION = "v1.0.0-codex.0"
_SIDECARS = (
    "resource_references.sqlite3", "media_events.sqlite3", "resource_revalidated.sqlite3"
)
_PIN = "history_file_identity"


def _safe(path):
    path = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Publication paths cannot use symbolic links.")
    return path


def _resident(path):
    _safe(path)
    if any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Finish every database journal before publication.")
    return file_identity(path)


def _same_identity(actual, expected):
    return (isinstance(expected, dict) and set(actual) == set(expected)
            and all(type(value) is int for value in expected.values()) and actual == expected)


def _sync(path):
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _space(path, minimum):
    if type(minimum) is not int or minimum < 0:
        raise ValueError("A publication requires its original nonnegative free-space floor.")
    if shutil.disk_usage(path).free < minimum + 8 * 1024**2:
        raise ValueError("Publication would exceed the free-space floor and metadata reserve.")


def _clone(path):
    before = _resident(path)
    descriptor, name = tempfile.mkstemp(
        prefix=".wechat-derived-publish-", suffix=".sqlite3", dir=path.parent
    )
    os.close(descriptor)
    clone = Path(name)
    clone.unlink()
    try:
        _clonefile(path, clone)
        clone.chmod(0o600)
        _resident(clone)
        if _resident(path) != before:
            raise ValueError("A publication input changed during its APFS clone.")
        if clone.stat().st_dev != path.stat().st_dev:
            raise ValueError("Publication requires same-volume APFS clones.")
        _sync(clone)
        return clone
    except BaseException:
        clone.unlink(missing_ok=True)
        raise


def _info(path):
    _resident(path)
    with closing(_readonly(path)) as connection:
        return dict(connection.execute("SELECT key,value FROM archive_info"))


def _rebind(path, previous, pin):
    expected = {**previous, _PIN: _json(pin)}
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        cursor = connection.execute(
            "UPDATE archive_info SET value=? WHERE key=?", (expected[_PIN], _PIN)
        )
        if cursor.rowcount != 1:
            raise ValueError("A current resource index lost its primary identity pin.")
        connection.commit()
    _sync(path)
    if _info(path) != expected:
        raise ValueError("Resource publication changed metadata beyond its primary pin.")


def _aliases(connection):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='derived_message_aliases'"
    ).fetchone():
        return 0
    return connection.execute("SELECT COUNT(*) FROM derived_message_aliases").fetchone()[0]


def _manifest(connection):
    row = connection.execute("SELECT value FROM archive_info WHERE key='manifest'").fetchone()
    return json.loads(row[0]) if row else None


def _verify_candidate(history, candidate, plan, report):
    with closing(_readonly(history)) as source, closing(_readonly(candidate)) as output:
        fingerprint = wechat_history_partition_fingerprint(source)
        if wechat_history_partition_fingerprint(output) != fingerprint:
            raise ValueError("Retirement changed the source-account partition fingerprint.")
        retired_by_source = Counter()
        affected_conversations = Counter()
        for match in plan["matches"]:
            pair = _pair(source, match)
            if pair is not None:
                retired_by_source[pair["partition"]["id"]] += 1
                affected_conversations[pair["conversation"]["id"]] += 1
        before, after = _manifest(source), _manifest(output)
        if (not isinstance(before, dict) or not isinstance(after, dict)
                or before.get("messages") != report.get("messages_before")
                or after.get("messages") != report.get("messages_after")
                or report["messages_before"] - report["messages_after"] != report.get("matches")
                or sum(retired_by_source.values()) != report["matches"]):
            raise ValueError("The staged retirement manifest differs from its receipt.")
        expected = json.loads(_json(before))
        expected["messages"] = after["messages"]
        for source_id, count in retired_by_source.items():
            entries = [item for item in expected.get("sources", [])
                       if isinstance(item, dict) and item.get("source_id") == source_id]
            if len(entries) != 1 or type(entries[0].get("messages")) is not int or entries[0]["messages"] < count:
                raise ValueError("An affected source manifest count cannot be verified.")
            entries[0]["messages"] -= count
            actual = output.execute(
                "SELECT COALESCE(SUM(message_count),0) FROM conversations WHERE partition_id=?", (source_id,)
            ).fetchone()[0]
            if actual != entries[0]["messages"]:
                raise ValueError("Affected conversation counters differ from the source manifest.")
        if expected != after:
            raise ValueError("Retirement changed manifest fields beyond scoped message counts.")
        for identifier in affected_conversations:
            declared = output.execute("SELECT message_count FROM conversations WHERE id=?", (identifier,)).fetchone()
            actual = output.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (identifier,)).fetchone()[0]
            if declared is None or declared[0] != actual:
                raise ValueError("An affected conversation counter does not match its indexed rows.")
        aliases = _aliases(source) + report["matches"]
        if _aliases(output) != aliases:
            raise ValueError("The staged alias count differs from its scoped retirement.")
        for match in plan["matches"]:
            if _pair(output, match) is not None:
                raise ValueError("A planned derived row still exists in the candidate.")
        return fingerprint, aliases, after


def _indexed_column(connection, table, column):
    if table == "messages" and any(
        row[1] == column and row[5] == 1 for row in connection.execute('PRAGMA table_info("messages")')
    ):
        return True
    for row in connection.execute('PRAGMA index_list("' + table + '")'):
        if len(row) > 4 and row[4]:
            continue
        name = row[1].replace('"', '""')
        columns = list(connection.execute('PRAGMA index_info("' + name + '")'))
        if columns and columns[0][2] == column:
            return True
    return False


def _no_retired_resources(path, retired):
    with closing(_readonly(path)) as connection:
        for table, column in (("messages", "id"), ("html_forwarded_resources", "parent_id")):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists and table == "html_forwarded_resources":
                continue
            if not exists or not _indexed_column(connection, table, column):
                raise ValueError("Current resource membership needs bounded indexed probes.")
            for offset in range(0, len(retired), 400):
                identifiers = retired[offset:offset + 400]
                if connection.execute(
                    f'SELECT 1 FROM "{table}" WHERE "{column}" IN ('
                    + ",".join("?" for _ in identifiers) + ") LIMIT 1", identifiers,
                ).fetchone():
                    raise ValueError("A current resource index references a retired derived ID.")


def _prepare_sidecars(history, baseline, fingerprint, retired):
    current, states = {}, {}
    for name in _SIDECARS:
        path = _safe(history.with_name(name))
        if not path.exists():
            states[name] = "absent; unchanged"
            continue
        if getattr(path.stat(), "st_flags", 0) & 0x40000000:
            states[name] = "cloud-only; unchanged, not revalidated"
            continue
        identity = _resident(path)
        try:
            info = _info(path)
            pin = json.loads(info.get(_PIN, "null"))
        except (ValueError, sqlite3.Error):
            states[name] = "unverified; unchanged"
            continue
        if (not _same_identity(baseline, pin) or info.get("schema_version") != "1"
                or info.get("history_partitions_sha256", fingerprint) != fingerprint):
            states[name] = "already stale; unchanged"
            continue
        _no_writers(path)
        _no_retired_resources(path, retired)
        if _resident(path) != identity:
            raise ValueError("A current resource index changed during validation.")
        current[path] = (identity, info)
        states[name] = "rebound; original resource content retained"
    return current, states


def _check_inputs(root, plan, candidate, identity, current):
    history, registry = _inputs(root, plan)
    for path in (history, registry, candidate, *current):
        expected = (plan["history_identity"] if path == history else
                    plan["registry_identity"] if path == registry else
                    identity if path == candidate else current[path][0])
        if not _same_identity(_resident(path), expected):
            raise ValueError("A publication input changed before its atomic replacement.")
        _no_writers(path)


def _restore(history, backups, current, aliases, manifest):
    os.replace(backups[history], history)
    pin = _file_identity(history)
    for path, (_, info) in current.items():
        _rebind(backups[path], info, pin)
        os.replace(backups[path], path)
    _sync_directory(history.parent)
    with closing(_readonly(history)) as source:
        if _aliases(source) != aliases or _manifest(source) != manifest:
            raise RuntimeError("The primary rollback failed its logical readback.")
    for path, (_, info) in current.items():
        if _info(path) != {**info, _PIN: _json(pin)}:
            raise RuntimeError("A resource rollback failed its identity readback.")


def publish_retirement(root, plan, staged, *, receipt_path):
    """Publish one audited stage with fail-closed resource pins and scoped rollback.

    Only same-volume APFS clones provide temporary full rollback. The durable
    rollback remains the stage's compressed scoped observations outside runtime.
    A rename sequence may briefly expose mismatched pins, which readers reject.
    """
    from .job_lock import CACHE_TASK_LOCK_NAME, CacheMaintenanceLock, CacheTaskLock

    if not isinstance(plan, dict) or not isinstance(staged, dict):
        raise ValueError("Publication requires an explicit plan and staging receipt.")
    root = _safe(root)
    receipt = _safe(receipt_path)
    if (os.path.lexists(receipt) or not receipt.parent.is_dir()
            or receipt.resolve().is_relative_to(root.resolve())):
        raise ValueError("Use a new receipt in an existing external directory.")
    report = staged.get("report", {})
    plan_sha = hashlib.sha256(_json(plan).encode()).hexdigest()
    if (not isinstance(report, dict) or report.get("published") is not False
            or report.get("dry_run") is not False or report.get("plan_sha256") != plan_sha
            or not isinstance(staged.get("candidate_path"), str)):
        raise ValueError("The staging receipt does not match the reviewed plan.")
    history, registry = _inputs(root, plan)
    history, registry = _safe(history), _safe(registry)
    candidate = _safe(staged["candidate_path"])
    if (candidate.parent != history.parent or candidate in {history, registry}
            or not candidate.name.startswith(".wechat-derived-retire-")):
        raise ValueError("The candidate must be its untouched staged sibling.")
    identity = _resident(candidate)
    if not _same_identity(identity, report.get("candidate_identity")) or identity["dev"] != plan["history_identity"]["dev"]:
        raise ValueError("The staged candidate identity changed.")
    rollback = _safe(report.get("rollback_path", ""))
    rollback_identity = _resident(rollback)
    if (rollback.resolve().is_relative_to(root.resolve())
            or _digest(rollback) != report.get("rollback_sha256")):
        raise ValueError("The compressed scoped rollback is unavailable or changed.")
    with gzip.open(rollback, "rt", encoding="utf-8") as stream:
        header = json.loads(stream.readline(1024 * 1024))
    if header.get("plan_sha256") != plan_sha or header.get("history_identity") != plan["history_identity"]:
        raise ValueError("The scoped rollback belongs to another publication.")
    maintenance = CacheMaintenanceLock(CacheTaskLock(root / CACHE_TASK_LOCK_NAME))
    if not maintenance.acquire("wechat-derived-retirement-publication"):
        raise ValueError("Another cache task owns the publication maintenance boundary.")
    backups, stages, started, keep_backups, receipt_created = {}, {}, False, False, False
    try:
        fingerprint, aliases, manifest = _verify_candidate(history, candidate, plan, report)
        baseline = _file_identity(history)
        retired = sorted({match["derived_id"] for match in plan["matches"]})
        current, states = _prepare_sidecars(history, baseline, fingerprint, retired)
        with closing(_readonly(history)) as source:
            original_aliases, original_manifest = _aliases(source), _manifest(source)
        _check_inputs(root, plan, candidate, identity, current)
        minimum = plan.get("minimum_free_bytes", 1024**3)
        _space(history.parent, minimum)
        for path in (history, *current):
            backups[path] = _clone(path)
        replacement = _file_identity(candidate)
        for path, (_, info) in current.items():
            _space(history.parent, minimum)
            stages[path] = _clone(path)
            _rebind(stages[path], info, replacement)
        _sync(candidate)
        _sync_directory(history.parent)
        _check_inputs(root, plan, candidate, identity, current)
        _space(history.parent, minimum)
        if _resident(rollback) != rollback_identity:
            raise ValueError("The compressed scoped rollback changed before publication.")
        started = True
        os.replace(candidate, history)
        for path, temporary in stages.items():
            os.replace(temporary, path)
        _sync_directory(history.parent)
        if _file_identity(history) != replacement or file_identity(registry) != plan["registry_identity"]:
            raise ValueError("Published history or registry differs from its verified stage.")
        with closing(_readonly(history)) as source:
            if (_aliases(source) != aliases or _manifest(source) != manifest
                    or wechat_history_partition_fingerprint(source) != fingerprint):
                raise ValueError("Published retirement failed its scoped readback.")
            for match in plan["matches"]:
                if _pair(source, match) is not None:
                    raise ValueError("A retired observation reappeared after publication.")
        for path, (_, info) in current.items():
            if _info(path) != {**info, _PIN: _json(replacement)}:
                raise ValueError("Published resource pins differ from their verified stage.")
        if _resident(rollback) != rollback_identity:
            raise ValueError("The compressed scoped rollback changed during publication.")
        result = dict(version=VERSION, published=True, plan_sha256=plan_sha,
                      history_identity=file_identity(history), alias_count=aliases,
                      aliases_added=report["matches"], sidecars=states,
                      rollback_path=str(rollback), rollback_sha256=report["rollback_sha256"],
                      receipt_path=str(receipt), filesystem_bytes_reclaimed=0)
        with receipt.open("x", encoding="utf-8") as stream:
            receipt_created = True
            stream.write(_json(result) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        receipt.chmod(0o600)
        _sync_directory(receipt.parent)
        return result
    except BaseException:
        if started:
            try:
                _restore(history, backups, current, original_aliases, original_manifest)
            except BaseException as restore_error:
                keep_backups = True
                raise RuntimeError(
                    "Publication rollback needs review; retain temporary clones: "
                    + ", ".join(str(path) for path in backups.values() if path.exists())
                ) from restore_error
        if receipt_created:
            receipt.unlink(missing_ok=True)
            _sync_directory(receipt.parent)
        raise
    finally:
        try:
            if not keep_backups:
                for path in (*backups.values(), *stages.values()):
                    path.unlink(missing_ok=True)
                _sync_directory(history.parent)
        finally:
            maintenance.release()
