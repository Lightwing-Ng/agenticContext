"""Refresh a pinned contact cache after proved top-level export retirement.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .wechat_archive import wechat_archive_path
from .wechat_contacts import contact_registry_path
from .wechat_derived_redundancy import file_identity


VERSION = "v1.0.1-codex.0"
_ID = re.compile(r"[0-9a-f]{64}")
_CORE = {"refs", "statement_groups", "statement_members", "info"}
_MAX_CACHE_BYTES = 512 * 1024**2


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _object(value):
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("A retained retirement receipt is not an object.")
    return result


def _after_state(root, before):
    from .wechat_contact_analysis import _identity, _source_state

    state = _source_state(root)
    if state is None:
        raise ValueError("The published source state is unavailable.")
    paths = (wechat_archive_path(root), contact_registry_path(root))
    inputs = hashlib.sha256(_json({str(path.resolve()): _identity(path) for path in paths}).encode()).hexdigest()
    if (
        state[2] != before.get("expected_source_fingerprint_after")
        or inputs != before.get("expected_source_inputs_after")
        or "expected_source_checkpoint_after" in before
        and state[3] != before["expected_source_checkpoint_after"]
    ):
        raise ValueError("The expected post-retirement source rules or inputs changed.")
    return state[2], inputs, state[3]


def _aliases(history, matches):
    if not isinstance(matches, list) or not matches or len(matches) > 250_000:
        raise ValueError("Select a nonempty bounded proved retirement plan.")
    selected = {}
    for match in matches:
        old, native = match.get("derived_id"), match.get("native_id")
        if (
            not isinstance(old, str) or _ID.fullmatch(old) is None
            or not isinstance(native, str) or _ID.fullmatch(native) is None
            or old == native or old in selected
            or not isinstance(match.get("derived_sha256"), str)
            or _ID.fullmatch(match["derived_sha256"]) is None
            or not isinstance(match.get("native_sha256"), str)
            or _ID.fullmatch(match["native_sha256"]) is None
        ):
            raise ValueError("The retirement plan has invalid or repeated exact IDs.")
        alias = history.execute("SELECT * FROM derived_message_aliases WHERE old_id=?", (old,)).fetchone()
        if (
            alias is None
            or alias["native_id"] != native
            or _object(alias["metadata"]).get("original_sha256") != match.get("derived_sha256")
            or _object(alias["proof"]) != match.get("evidence")
            or history.execute("SELECT 1 FROM messages WHERE id=?", (old,)).fetchone()
            or history.execute("SELECT 1 FROM messages WHERE id=?", (native,)).fetchone() is None
        ):
            raise ValueError("Published aliases do not prove the complete pinned retirement plan.")
        selected[old] = native
    return selected


def _no_evidence_dependencies(connection, retired):
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if name in _CORE or name.startswith("sqlite_"):
            continue
        table = '"' + name.replace('"', '""') + '"'
        for row in connection.execute("SELECT * FROM " + table):
            for value in row:
                if isinstance(value, str) and retired.intersection(_ID.findall(value)):
                    raise ValueError("Retired references have derived evidence dependencies; rebuild this cache.")


def _unchanged_rows(connection, table, where=""):
    for left, right in (("retained", "main"), ("main", "retained")):
        query = f"SELECT * FROM {left}.{table}" + where + f" EXCEPT SELECT * FROM {right}.{table}" + where
        if connection.execute("SELECT 1 FROM (" + query + ") LIMIT 1").fetchone():
            raise ValueError("A surviving contact-cache row changed during the bounded refresh.")


def refresh_retired_contact_cache(root, contact_id, *, plan, before):
    """Publish only an exact deletion bridge; never scan source message bodies.

    The caller must independently prove unchanged source rules and supply their
    expected post-publication fingerprint and source-input hash in ``before``.
    This function rejects any missing pin instead of claiming a new cache build.
    ``contact_cache_identity`` uses the same complete dictionary as file_identity.
    The cache is derived and rebuildable; source rollback belongs to publication.
    """
    from .wechat_contact_analysis import INDEX_VERSION, NATIVE_EMBEDDED_VERSION, contact_analysis_cache_path
    from .wechat_export_merge import _clonefile

    root = Path(root).expanduser()
    target = contact_analysis_cache_path(root, contact_id)
    expected_info = before.get("contact_cache_info")
    expected_identity = before.get("contact_cache_identity")
    if (
        not isinstance(expected_info, dict)
        or file_identity(target) != expected_identity
        or expected_identity["size"] > _MAX_CACHE_BYTES
        or expected_info.get("fingerprint") != before.get("source_fingerprint")
        or expected_info.get("source_inputs") != before.get("source_inputs")
        or expected_info.get("version") != INDEX_VERSION
        or expected_info.get("native_embedded_version") != NATIVE_EMBEDDED_VERSION
    ):
        raise ValueError("The current contact cache is not the exact ready pre-publication cache.")
    registry = contact_registry_path(root)
    if file_identity(registry) != plan.get("registry_identity"):
        raise ValueError("The contact registry changed after the retirement plan.")
    after_state = _after_state(root, before)
    history = wechat_archive_path(root)
    history_pin = file_identity(history)
    with closing(_readonly(history)) as source:
        mappings = _aliases(source, plan.get("matches", []))
    with closing(_readonly(target)) as retained:
        if dict(retained.execute("SELECT key,value FROM info")) != expected_info:
            raise ValueError("Contact-cache metadata changed after capture.")
        if retained.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise ValueError("A contact cache with write triggers requires a full rebuild.")
        if (
            retained.execute(
                "SELECT 1 FROM refs r LEFT JOIN statement_members sm ON sm.reference_id=r.id "
                "WHERE sm.reference_id IS NULL LIMIT 1"
            ).fetchone()
            or retained.execute(
                "SELECT 1 FROM statement_members sm LEFT JOIN refs r ON r.id=sm.reference_id "
                "LEFT JOIN statement_groups g ON g.id=sm.statement_id "
                "WHERE r.id IS NULL OR g.id IS NULL LIMIT 1"
            ).fetchone()
        ):
            raise ValueError("The retained contact cache has incomplete reference membership.")
        proofs = {match["derived_id"]: match["evidence"] for match in plan["matches"]}
        existing = {}
        for old, native in mappings.items():
            row = retained.execute("SELECT * FROM refs WHERE id=?", (old,)).fetchone()
            if row is None:
                if retained.execute("SELECT 1 FROM refs WHERE parent_message_id=?", (old,)).fetchone():
                    raise ValueError("A retired parent has nested contact references.")
                continue
            if (
                row["component_path"] != "[]"
                or row["parent_message_id"] != old
                or row["source_id"] != proofs[old].get("derived_source_id")
                or row["conversation_id"] != proofs[old].get("derived_conversation_id")
                or retained.execute("SELECT COUNT(*) FROM refs WHERE parent_message_id=?", (old,)).fetchone()[0] != 1
            ):
                raise ValueError("Only retired top-level references can use an incremental refresh.")
            native_row = retained.execute("SELECT * FROM refs WHERE id=?", (native,)).fetchone()
            if (
                native_row is None
                or native_row["component_path"] != "[]"
                or native_row["parent_message_id"] != native
                or not native_row["source_id"].startswith("ios-")
                or native_row["source_id"] != proofs[old].get("native_source_id")
                or native_row["conversation_id"] != proofs[old].get("native_conversation_id")
            ):
                raise ValueError("A retained native counterpart is missing from the ready cache.")
            existing[old] = native
        _no_evidence_dependencies(retained, set(mappings))
        refs_before = retained.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
        groups_before = retained.execute("SELECT COUNT(*) FROM statement_groups").fetchone()[0]

    descriptor, name = tempfile.mkstemp(prefix=".derived-contact-refresh-", suffix=".sqlite3", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    try:
        _clonefile(target, temporary)
        with closing(sqlite3.connect(temporary, uri=True)) as output:
            output.execute("PRAGMA journal_mode=DELETE")
            output.execute("PRAGMA synchronous=FULL")
            output.execute("PRAGMA temp_store=MEMORY")
            output.execute("ATTACH DATABASE ? AS retained", (target.resolve().as_uri() + "?mode=ro&immutable=1",))
            output.execute("CREATE TEMP TABLE retired_ids(id TEXT PRIMARY KEY,native_id TEXT NOT NULL) WITHOUT ROWID")
            output.executemany("INSERT INTO retired_ids VALUES(?,?)", existing.items())
            output.execute("DELETE FROM statement_members WHERE reference_id IN (SELECT id FROM retired_ids)")
            output.execute("DELETE FROM refs WHERE id IN (SELECT id FROM retired_ids)")
            output.execute("DELETE FROM statement_groups WHERE id NOT IN (SELECT statement_id FROM statement_members)")
            invalid = output.execute(
                "SELECT id FROM statement_groups WHERE reference_id IN (SELECT id FROM retired_ids)"
            ).fetchall()
            for (group,) in invalid:
                replacement = output.execute(
                    "SELECT r.id FROM refs r JOIN statement_members sm ON sm.reference_id=r.id "
                    "WHERE sm.statement_id=? AND r.source_id GLOB 'ios-*' "
                    "ORDER BY r.id IN (SELECT native_id FROM retired_ids) DESC,r.id LIMIT 1", (group,),
                ).fetchone()
                if replacement is None:
                    raise ValueError("A surviving group has no already accepted native representative.")
                output.execute("UPDATE statement_groups SET reference_id=? WHERE id=?", (replacement[0], group))
            output.execute("UPDATE info SET value=? WHERE key='fingerprint'", (after_state[0],))
            output.execute("UPDATE info SET value=? WHERE key='source_inputs'", (after_state[1],))
            _unchanged_rows(output, "refs", " WHERE id NOT IN (SELECT id FROM retired_ids)")
            _unchanged_rows(output, "statement_members", " WHERE reference_id NOT IN (SELECT id FROM retired_ids)")
            _unchanged_rows(output, "info", " WHERE key NOT IN ('fingerprint','source_inputs')")
            for (table,) in output.execute("SELECT name FROM main.sqlite_master WHERE type='table'"):
                if table not in _CORE and not table.startswith("sqlite_"):
                    escaped = '"' + table.replace('"', '""') + '"'
                    _unchanged_rows(output, escaped)
            if output.execute(
                "SELECT 1 FROM statement_groups g LEFT JOIN statement_members sm "
                "ON sm.statement_id=g.id AND sm.reference_id=g.reference_id WHERE sm.reference_id IS NULL LIMIT 1"
            ).fetchone():
                raise ValueError("A refreshed statement group lost its accepted representative.")
            refs_after = output.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
            groups_after = output.execute("SELECT COUNT(*) FROM statement_groups").fetchone()[0]
            if refs_before - refs_after != len(existing):
                raise ValueError("The contact refresh changed an unexpected number of references.")
            output.commit()
            if output.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("The refreshed contact cache failed SQLite validation.")
        if (
            file_identity(target) != expected_identity
            or file_identity(history) != history_pin
            or file_identity(registry) != plan.get("registry_identity")
            or _after_state(root, before) != after_state
        ):
            raise ValueError("A pinned cache or source changed before contact refresh publication.")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return {
            "version": VERSION, "published": True, "contact_id": contact_id,
            "removed_references": len(existing), "references_before": refs_before,
            "references_after": refs_after, "groups_before": groups_before,
            "groups_after": groups_after, "source_fingerprint": after_state[0],
        }
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            owned = Path(str(temporary) + suffix)
            if owned.exists():
                owned.unlink()


def refresh_retired_native_candidates(root, plan):
    """Rebind exact native-author hints when no retired parent was indexed.

    This is an optional, metadata-only bridge. An unrelated stale fingerprint,
    missing file, or cloud-only hint cache is left alone for its normal rebuild.
    Every unchanged hint and all non-fingerprint metadata are compared before
    publication. Original message bodies are never selected.
    """
    from .wechat_contact_embedded import CANDIDATE_INDEX_VERSION, _identity
    from .wechat_export_merge import _clonefile

    root = Path(root).expanduser()
    history, registry = wechat_archive_path(root), contact_registry_path(root)
    target = history.parent / "contact_activity" / "native-author-candidates.sqlite3"
    if not target.exists():
        return {"version": VERSION, "published": False, "reason": "cache_missing"}
    try:
        cache_pin = file_identity(target)
    except (ValueError, OSError):
        return {"version": VERSION, "published": False, "reason": "cache_unavailable"}
    previous = plan.get("history_identity")
    fields = ("dev", "inode", "size", "mtime_ns", "ctime_ns")
    if not isinstance(previous, dict) or any(type(previous.get(key)) is not int for key in fields):
        raise ValueError("The native hint bridge needs the exact pre-retirement history identity.")
    old_fingerprint = hashlib.sha256(json.dumps([
        CANDIDATE_INDEX_VERSION, str(history.resolve()), tuple(previous[key] for key in fields),
    ]).encode()).hexdigest()
    new_identity = _identity(history)
    new_fingerprint = hashlib.sha256(json.dumps([
        CANDIDATE_INDEX_VERSION, str(history.resolve()), new_identity,
    ]).encode()).hexdigest()
    with closing(_readonly(target)) as cache:
        info = dict(cache.execute("SELECT key,value FROM info"))
        if info.get("fingerprint") == new_fingerprint:
            return {"version": VERSION, "published": False, "reason": "already_current"}
        if info.get("fingerprint") != old_fingerprint:
            return {"version": VERSION, "published": False, "reason": "different_source_fingerprint"}
        if cache.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise ValueError("A native hint cache with write triggers requires a full rebuild.")
        if [row[1] for row in cache.execute("PRAGMA table_info(candidates)")] != ["author", "parent_id"]:
            raise ValueError("The native hint cache has an unsupported schema.")
        if file_identity(registry) != plan.get("registry_identity"):
            raise ValueError("The contact registry changed after the retirement plan.")
        with closing(_readonly(history)) as source:
            mappings = _aliases(source, plan.get("matches", []))
        cache.execute("PRAGMA temp_store=MEMORY")
        cache.execute("CREATE TEMP TABLE retired_ids(id TEXT PRIMARY KEY) WITHOUT ROWID")
        cache.executemany("INSERT INTO retired_ids VALUES(?)", ((key,) for key in mappings))
        # CROSS JOIN fixes the scan direction: scan the hint table only once and
        # probe the in-memory retired-ID primary key for each accepted hint.
        if cache.execute(
            "SELECT 1 FROM main.candidates c CROSS JOIN temp.retired_ids r "
            "ON r.id=c.parent_id LIMIT 1"
        ).fetchone():
            raise ValueError("A retired message is still a native author candidate; rebuild the hints.")
        candidates_before = cache.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    if file_identity(target) != cache_pin or _identity(history) != new_identity:
        raise ValueError("The native hints or published source changed during bridge validation.")
    descriptor, name = tempfile.mkstemp(prefix=".derived-native-hint-refresh-", suffix=".sqlite3", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    try:
        _clonefile(target, temporary)
        with closing(sqlite3.connect(temporary, uri=True)) as output:
            output.execute("PRAGMA journal_mode=DELETE")
            output.execute("PRAGMA synchronous=FULL")
            output.execute("PRAGMA temp_store=MEMORY")
            output.execute("ATTACH DATABASE ? AS retained", (target.resolve().as_uri() + "?mode=ro&immutable=1",))
            if dict(output.execute("SELECT key,value FROM info")) != info:
                raise ValueError("The native hint cache changed before cloning.")
            output.execute("UPDATE info SET value=? WHERE key='fingerprint'", (new_fingerprint,))
            _unchanged_rows(output, "candidates")
            _unchanged_rows(output, "info", " WHERE key!='fingerprint'")
            candidates_after = output.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            if candidates_after != candidates_before:
                raise ValueError("The native hint bridge changed candidate membership.")
            output.commit()
            if output.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("The refreshed native hint cache failed SQLite validation.")
        if (
            file_identity(target) != cache_pin
            or _identity(history) != new_identity
            or file_identity(registry) != plan.get("registry_identity")
        ):
            raise ValueError("A native hint cache or source changed before bridge publication.")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return {
            "version": VERSION, "published": True, "reason": "exact_retirement_bridge",
            "checked_retired_parents": len(mappings), "candidates": candidates_after,
            "fingerprint": new_fingerprint,
        }
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            owned = Path(str(temporary) + suffix)
            if owned.exists():
                owned.unlink()
