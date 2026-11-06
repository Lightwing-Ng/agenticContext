"""Fold contact-level forwarded statement copies without inventing event identity.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re
import sqlite3

from .wechat_contacts import canonical_contact_id
from .wechat_forwarded_origins import _metadata, _native_scope, _native_time, _stamp
from .wechat_html_export import normalize_html_clock
from .wechat_statement_reviews import (
    _constraint,
    _html_component,
    _html_frame,
    _member_clock,
    _native_component,
)


VERSION = "v1.0.1-codex.0"
BASIS = "contact_forwarded_statement_projection"
MAX_REFERENCES = 200_000
MAX_PACKET_BYTES = 16 * 1024**2
MAX_SOURCE_BYTES = 256 * 1024**2
_SERVER = re.compile(r"-?[1-9][0-9]*")
_TABLES = (
    "forwarded_copy_projection_groups",
    "forwarded_copy_projection_members",
    "forwarded_copy_projection_evidence",
)
_EVIDENCE_FIELDS = (
    "reference_id",
    "statement_id",
    "representative_reference_id",
    "projection_key",
    "basis",
    "source_pin_sha256",
    "evidence_json",
)
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS forwarded_copy_projection_groups("
    "id TEXT PRIMARY KEY,reference_id TEXT NOT NULL UNIQUE,conversation_key TEXT NOT NULL) "
    "WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS forwarded_copy_projection_members("
    "reference_id TEXT PRIMARY KEY,statement_id TEXT NOT NULL) WITHOUT ROWID",
    "CREATE TABLE IF NOT EXISTS forwarded_copy_projection_evidence("
    "reference_id TEXT PRIMARY KEY,statement_id TEXT NOT NULL,"
    "representative_reference_id TEXT NOT NULL,projection_key TEXT NOT NULL,"
    "basis TEXT NOT NULL,source_pin_sha256 TEXT NOT NULL,evidence_json TEXT NOT NULL) "
    "WITHOUT ROWID",
)


class _Bounded(ValueError):
    """A complete contact projection must fit its declared metadata budget."""


class _Changed(ValueError):
    """An accepted reference no longer identifies its retained source object."""


class _SnapshotRows:
    """Return an independent cursor over source metadata already pinned in this pass."""

    def __init__(self, rows):
        self.rows = rows
        self.position = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.position >= len(self.rows):
            raise StopIteration
        row = self.rows[self.position]
        self.position += 1
        return row

    def fetchall(self):
        result = self.rows[self.position :]
        self.position = len(self.rows)
        return result


class _PreparedSource:
    """Reuse only the exact snapshot inventory read by native source routing."""

    def __init__(self, source, snapshots):
        self.source = source
        self.snapshots = snapshots

    def execute(self, query, parameters=()):
        if not parameters and " ".join(query.split()).upper() == (
            "SELECT ID,SNAPSHOT_ID,SOURCE_ID,METADATA FROM SOURCE_SNAPSHOTS"
        ):
            return _SnapshotRows(self.snapshots)
        return self.source.execute(query, parameters)

    def __getattr__(self, name):
        return getattr(self.source, name)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _rows(cursor):
    fields = [value[0] for value in cursor.description]
    return [dict(zip(fields, row, strict=True)) for row in cursor]


def _tables(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _ensure(connection):
    for statement in _SCHEMA:
        connection.execute(statement)


def _evidence(connection):
    byte_size = "+".join(
        "COALESCE(length(CAST(" + field + " AS BLOB)),0)" for field in _EVIDENCE_FIELDS
    )
    count, size = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(" + byte_size + "),0) FROM " + _TABLES[2]
    ).fetchone()
    if count > MAX_REFERENCES or size > MAX_SOURCE_BYTES:
        raise _Bounded("Forwarded projection evidence exceeds its metadata budget.")
    return _rows(
        connection.execute("SELECT * FROM " + _TABLES[2] + " ORDER BY reference_id")
    )


def copy_forwarded_copy_evidence(retained, output):
    """Copy projection metadata into a person union without its restoration journal."""
    if _TABLES[2] not in _tables(retained):
        return 0
    fields = tuple(
        row[1] for row in retained.execute("PRAGMA table_info(" + _TABLES[2] + ")")
    )
    if fields != _EVIDENCE_FIELDS:
        raise ValueError("Unsupported forwarded projection evidence.")
    rows = _evidence(retained)
    output.execute(_SCHEMA[2])
    for start in range(0, len(rows), 400):
        output.executemany(
            "INSERT OR REPLACE INTO " + _TABLES[2] + " VALUES(?,?,?,?,?,?,?)",
            [
                tuple(row[field] for field in _EVIDENCE_FIELDS)
                for row in rows[start : start + 400]
            ],
        )
    return len(rows)


def restore_forwarded_copy_groups(retained, output):
    """Undo only this display projection before replaying source or manual proofs."""
    present = set(_TABLES) & _tables(retained)
    if not present or present == {_TABLES[2]}:
        return {"restored_groups": 0, "restored_members": 0}
    if present != set(_TABLES):
        raise ValueError("The forwarded projection journal is incomplete.")
    groups = retained.execute(
        "SELECT * FROM " + _TABLES[0] + " ORDER BY id LIMIT ?", (MAX_REFERENCES + 1,)
    ).fetchall()
    members = retained.execute(
        "SELECT * FROM " + _TABLES[1] + " ORDER BY reference_id LIMIT ?",
        (MAX_REFERENCES + 1,),
    ).fetchall()
    evidence = _evidence(retained)
    if not groups and not members and not evidence:
        return {"restored_groups": 0, "restored_members": 0}
    group_map, member_map = {row[0]: tuple(row) for row in groups}, dict(members)
    proofs = {row["reference_id"]: row for row in evidence}
    if (
        len(groups) > MAX_REFERENCES
        or len(members) > MAX_REFERENCES
        or len(group_map) != len(groups)
        or len(member_map) != len(members)
        or set(member_map) != set(proofs)
        or set(member_map.values()) != set(group_map)
        or any(member_map.get(row[1]) != row[0] for row in groups)
    ):
        raise ValueError("The forwarded projection journal is inconsistent.")
    current_groups = set()
    for reference, proof in proofs.items():
        value = output.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?",
            (reference,),
        ).fetchone()
        if (
            value is None
            or value[0] != proof["statement_id"]
            or proof["basis"] != BASIS
            or value[0] not in group_map
            or output.execute("SELECT 1 FROM refs WHERE id=?", (reference,)).fetchone()
            is None
        ):
            raise ValueError(
                "A forwarded projection member changed before restoration."
            )
        ref = _rows(output.execute("SELECT * FROM refs WHERE id=?", (reference,)))[0]
        pinned = json.loads(proof["evidence_json"])["source_pins"]
        if _sha(_json(ref)) != pinned["reference_sha256"]:
            raise ValueError("A forwarded projection source reference changed.")
        current_groups.add(value[0])
    actual = {
        row[0]
        for group in current_groups
        for row in output.execute(
            "SELECT reference_id FROM statement_members WHERE statement_id=?", (group,)
        )
    }
    if actual != set(member_map):
        raise ValueError("A forwarded projection contains undeclared members.")
    for group in current_groups:
        value = output.execute(
            "SELECT reference_id FROM statement_groups WHERE id=?", (group,)
        ).fetchone()
        expected = {
            proof["representative_reference_id"]
            for proof in evidence
            if proof["statement_id"] == group
        }
        if value is None or expected != {value[0]}:
            raise ValueError("A forwarded projection representative changed.")
    output.execute("SAVEPOINT restore_forwarded_projection")
    try:
        _ensure(output)
        output.executemany(
            "DELETE FROM statement_groups WHERE id=?", [(g,) for g in current_groups]
        )
        output.executemany(
            "INSERT INTO statement_groups VALUES(?,?,?)", [tuple(r) for r in groups]
        )
        output.executemany(
            "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
            [(group, reference) for reference, group in members],
        )
        for table in _TABLES:
            output.execute("DELETE FROM " + table)
        output.execute("RELEASE restore_forwarded_projection")
    except BaseException:
        output.execute("ROLLBACK TO restore_forwarded_projection")
        output.execute("RELEASE restore_forwarded_projection")
        raise
    return {"restored_groups": len(groups), "restored_members": len(members)}


def _load(source, output, row_select, prepare_rows, check_cancelled):
    reference_fields = [row[1] for row in output.execute("PRAGMA table_info(refs)")]
    reference_size = "+".join(
        'COALESCE(length(CAST("' + field + '" AS BLOB)),0)'
        for field in reference_fields
    )
    count, total = output.execute(
        "SELECT COUNT(*),COALESCE(SUM(" + reference_size + "),0) FROM refs"
    ).fetchone()
    if count > MAX_REFERENCES or total > MAX_SOURCE_BYTES:
        raise _Bounded("The accepted references exceed their metadata budget.")
    for table in ("statement_members", "statement_groups"):
        columns = [row[1] for row in output.execute("PRAGMA table_info(" + table + ")")]
        size = "+".join(
            'COALESCE(length(CAST("' + field.replace('"', '""') + '" AS BLOB)),0)'
            for field in columns
        )
        rows, byte_count = output.execute(
            "SELECT COUNT(*),COALESCE(SUM(" + size + "),0) FROM " + table
        ).fetchone()
        total += byte_count
        if rows > MAX_REFERENCES or total > MAX_SOURCE_BYTES:
            raise _Bounded("The accepted grouping exceeds its metadata budget.")
    references = _rows(
        output.execute("SELECT * FROM refs ORDER BY id LIMIT ?", (MAX_REFERENCES + 1,))
    )
    if len(references) > MAX_REFERENCES:
        raise _Bounded("The contact exceeds its accepted-reference budget.")
    memberships = dict(
        output.execute("SELECT reference_id,statement_id FROM statement_members")
    )
    groups = {
        row["id"]: row
        for row in _rows(output.execute("SELECT * FROM statement_groups"))
    }
    if (
        set(memberships) != {row["id"] for row in references}
        or set(memberships.values()) != set(groups)
        or any(
            memberships.get(group["reference_id"]) != group["id"]
            for group in groups.values()
        )
    ):
        raise ValueError("The accepted contact grouping is inconsistent.")
    parents = sorted({row["parent_message_id"] for row in references})
    fields = {row[1] for row in source.execute("PRAGMA main.table_info(messages)")}
    byte_size = "+".join(
        "COALESCE(length(CAST(" + field + " AS BLOB)),0)"
        for field in ("text", "provenance", "search_text")
        if field in fields
    )
    loaded, snapshots = {}, {}
    for start in range(0, len(parents), 400):
        check_cancelled()
        ids = parents[start : start + 400]
        marks = ",".join("?" for _ in ids)
        sizes = source.execute(
            "SELECT id,"
            + byte_size
            + " FROM main.messages WHERE id IN ("
            + marks
            + ")",
            ids,
        ).fetchall()
        if len(sizes) != len(ids):
            raise ValueError("An accepted source parent is missing.")
        for _, size in sizes:
            total += size
            if size > MAX_PACKET_BYTES or total > MAX_SOURCE_BYTES:
                raise _Bounded("The accepted source metadata exceeds its byte budget.")
        rows = _rows(source.execute(row_select + "WHERE m.id IN (" + marks + ")", ids))
        if len(rows) != len(ids):
            raise ValueError("An accepted source frame is missing.")
        for row in rows:
            proof = json.loads(row["provenance"])
            if not isinstance(proof, dict):
                raise ValueError("An accepted source parent has invalid provenance.")
            loaded[row["id"]] = (row, proof)
    wanted = sorted(
        {
            p.get("source_snapshot")
            for _, p in loaded.values()
            if type(p.get("source_snapshot")) is int
        }
    )
    prepared_snapshots = []
    for key in wanted:
        value = source.execute(
            "SELECT length(CAST(metadata AS BLOB)) FROM source_snapshots WHERE id=?",
            (key,),
        ).fetchone()
        if value is None:
            continue
        total += value[0]
        if value[0] > MAX_PACKET_BYTES or total > MAX_SOURCE_BYTES:
            raise _Bounded("The accepted snapshot metadata exceeds its byte budget.")
        snapshot = _rows(
            source.execute("SELECT * FROM source_snapshots WHERE id=?", (key,))
        )[0]
        prepared_snapshots.append(
            {
                field: snapshot[field]
                for field in ("id", "snapshot_id", "source_id", "metadata")
            }
        )
        metadata = json.loads(snapshot["metadata"])
        if not isinstance(metadata, dict):
            raise ValueError("A source snapshot has invalid metadata.")
        snapshot["metadata"] = metadata
        snapshots[key] = snapshot
    prepared_source = _PreparedSource(source, prepared_snapshots)
    for start in range(0, len(parents), 400):
        check_cancelled()
        rows = [loaded[identifier][0] for identifier in parents[start : start + 400]]
        prepare_rows(prepared_source, rows)
        for row in rows:
            proof = json.loads(row["provenance"])
            if not isinstance(proof, dict):
                raise ValueError("A prepared source parent has invalid provenance.")
            loaded[row["id"]] = (row, proof)
    snapshot_pins = {key: _sha(_json(value)) for key, value in snapshots.items()}
    for identifier, (row, proof) in list(loaded.items()):
        check_cancelled()
        snapshot_key = proof.get("source_snapshot")
        parent_pin = {
            "parent_text_sha256": _sha(row["text"]),
            "provenance_sha256": _sha(_json(proof)),
            "snapshot_id": snapshots.get(snapshot_key, {}).get("snapshot_id", ""),
            "snapshot_metadata_sha256": snapshot_pins.get(snapshot_key, _sha("{}")),
            "parent_frame_sha256": _sha(
                _json(
                    {
                        field: row[field]
                        for field in (
                            "id",
                            "source_id",
                            "account_id",
                            "account_key",
                            "container_account_key",
                            "peer_id",
                            "conversation_id",
                            "kind",
                            "identity_status",
                            "sender_id",
                            "direction",
                            "timestamp_value",
                            "ordinal",
                        )
                    }
                )
            ),
        }
        loaded[identifier] = (row, proof, parent_pin)
    return references, memberships, groups, loaded, snapshots


def _candidate(
    reference,
    row,
    proof,
    parent_pin,
    snapshots,
    contact_id,
    username,
    native_thread_key,
):
    path = json.loads(reference["component_path"])
    if not isinstance(path, list):
        raise ValueError("An accepted component path is invalid.")
    if path and (
        path[0] == "native"
        and not all(type(n) is int and n > 0 for n in path[1:])
        or path[0] != "native"
        and not all(type(n) is int and n > 0 for n in path)
    ):
        return None, "unsupported_component"
    if (
        reference["source_id"] != row["source_id"]
        or reference["account_key"] != row["account_key"]
        or reference["conversation_id"] != row["conversation_id"]
        or reference["anchor_id"] != "wechat_message_" + reference["id"]
    ):
        raise _Changed("An accepted reference changed its source frame.")
    numbers = path[1:] if path[:1] == ["native"] else path
    expected = row["id"] + "".join("_forwarded_" + str(n) for n in numbers)
    if reference["id"] != expected:
        raise _Changed("An accepted reference changed its component identity.")
    owner, peer, kind = row["account_id"] or "", row["peer_id"], row["kind"]
    if (
        not owner
        or row["account_key"] != "account:" + owner
        or row["container_account_key"] != row["account_key"]
        or kind not in {"direct", "group"}
        or not isinstance(peer, str)
        or not peer
        or any(char.isspace() or char == "\0" for char in peer)
        or (kind == "group") != peer.endswith(("@chatroom", "@im.chatroom"))
    ):
        return None, "unverified_receiving_scope"
    native = row["source_id"].startswith("ios-")
    snapshot = snapshots.get(proof.get("source_snapshot"), {})
    snapshot_id = snapshot.get("snapshot_id")
    if (
        snapshot.get("source_id") != row["source_id"]
        or not isinstance(snapshot_id, str)
        or not snapshot_id
        or len(snapshot_id) > 512
        or any(ord(char) < 32 for char in snapshot_id)
    ):
        return None, "unverified_source_snapshot"
    metadata = _metadata(row, proof, snapshots)
    if native:
        if (
            _native_scope(row, proof, snapshots, native_thread_key) is None
            or _native_time(row, proof) is None
            or row["direction"] not in {"sent", "received"}
            or not row["sender_id"]
            or (row["direction"] == "sent" and row["sender_id"] != owner)
            or (
                kind == "direct"
                and row["sender_id"] != (owner if row["direction"] == "sent" else peer)
            )
            or bool(path) != (path[:1] == ["native"])
        ):
            return None, "unverified_native_source"
        if path:
            if (
                proof.get("message_type") != 49
                or proof.get("text_extraction_kind") != "forwarded_text"
                or proof.get("appmsg_subtype") not in {None, 19}
            ):
                return None, "unsupported_native_role"
            part = _native_component(proof, numbers)
            author, body = part.get("source_sender_id"), part.get("text")
            stamp, precision = _member_clock(part, native=True)
            constraint = _constraint(part)
            role = "forward"
        else:
            if (
                proof.get("message_type") != 1
                or proof.get("text_extraction_kind") != "plain_text"
            ):
                return None, "unsupported_native_role"
            author, body = row["sender_id"], row["text"]
            stamp, precision = _stamp(row["timestamp_value"], seconds=True), "second"
            server = proof.get("server_id")
            if type(server) not in {str, int} or _SERVER.fullmatch(str(server)) is None:
                return None, "unproven_native_primary"
            constraint, role = (str(server), peer), "primary"
    else:
        locator = _html_frame(row, proof, snapshots, embedded=bool(path))
        if path:
            if proof.get("text_extraction_kind") not in {
                "forwarded_text",
                "rendered_app_text",
            }:
                return None, "unsupported_html_role"
            part = _html_component(proof, path, locator)
            author, body = part.get("sender_native_id"), part.get("text")
            stamp, precision = _member_clock(part, native=False)
            constraint, role = _constraint(part), "forward"
        else:
            if proof.get("text_extraction_kind") != "plain_text" or proof.get(
                "primary_text_sha256"
            ) != _sha(row["text"]):
                return None, "unproven_html_primary"
            author, body = proof.get("sender_native_id"), row["text"]
            normalized, precision = normalize_html_clock(
                proof.get("source_time", row["timestamp_value"])
            )
            stamp, constraint, role = (
                _stamp(normalized, seconds=True),
                ("", ""),
                "html_primary",
            )
    if (
        author != username
        or canonical_contact_id(author, authority="native_message_sender") != contact_id
    ):
        raise _Changed("An accepted statement changed its canonical author.")
    if (
        not isinstance(body, str)
        or not body.strip()
        or stamp is None
        or precision != "second"
    ):
        return None, "unknown_or_minute_clock"
    if (
        _stamp(reference["timestamp_value"], seconds=True) != stamp
        or not reference["has_time"]
    ):
        return None, "different_projected_clock"
    body_hash = _sha(body)
    wall = stamp.replace(tzinfo=None).isoformat()
    # A contact's compact statement projection can share repeated assertions
    # across receiving contexts. It never assigns them a common native event.
    key = _sha(_json([contact_id, body_hash, wall]))
    pin = {**parent_pin, "reference_sha256": _sha(_json(reference))}
    rank = (
        (0 if metadata.get("source_label") == "iPad archive" else 1)
        if role == "primary"
        else (2 if metadata.get("source_label") == "iPad archive" else 3)
        if native
        else 4
    )
    return {
        "id": reference["id"],
        "parent": row["id"],
        "key": key,
        "owner": owner,
        "peer": peer,
        "kind": kind,
        "source_id": reference["source_id"],
        "account_key": reference["account_key"],
        "conversation_id": reference["conversation_id"],
        "component_path": path,
        "anchor_id": reference["anchor_id"],
        "source_clock": reference["source_clock"],
        "source_snapshot_id": snapshot_id,
        "native_event_identity_inferred": False,
        "body_sha256": body_hash,
        "wall": wall,
        "epoch": stamp.timestamp() if stamp.tzinfo is not None else None,
        "constraint": constraint,
        "role": role,
        "native": native,
        "rank": rank,
        "parent_epoch": _native_time(row, proof) if native else None,
        "pin": pin,
    }, ""


def fold_contact_forwarded_copies(
    source,
    output,
    contact_id,
    username,
    *,
    row_select,
    prepare_rows,
    native_thread_key,
    check_cancelled=lambda: None,
):
    """Explicitly fold forwarded assertions across verified receiving contexts.

    This is a reversible contact display projection. It neither discovers an
    absent original event nor changes a raw message, source clock, or account.
    Canonical contact-index builds never apply this text-copy grouping implicitly.
    """
    summary = {
        "status": "no_matches",
        "statements_joined": 0,
        "projection_groups": 0,
        "refs_retained": 0,
        "skipped_reason_counts": {},
    }
    skipped = Counter()
    try:
        source_changes = source.total_changes
        if (
            canonical_contact_id(username, authority="native_message_sender")
            != contact_id
        ):
            raise ValueError("The contact's native identity changed.")
        summary["refs_retained"] = output.execute(
            "SELECT COUNT(*) FROM refs"
        ).fetchone()[0]
        refs, memberships, groups, loaded, snapshots = _load(
            source, output, row_select, prepare_rows, check_cancelled
        )
        summary["refs_retained"] = len(refs)
        candidates, cohorts, group_members = {}, defaultdict(list), defaultdict(set)
        for reference in refs:
            check_cancelled()
            group_members[memberships[reference["id"]]].add(reference["id"])
            row, proof, parent_pin = loaded[reference["parent_message_id"]]
            try:
                value, reason = _candidate(
                    reference,
                    row,
                    proof,
                    parent_pin,
                    snapshots,
                    contact_id,
                    username,
                    native_thread_key,
                )
            except _Changed:
                raise
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                value, reason = None, "unsupported_source_evidence"
            if value is None:
                skipped[reason] += 1
            else:
                candidates[value["id"]] = value
                cohorts[value["key"]].append(value)
        if source.total_changes != source_changes:
            raise _Changed("The source changed during forwarded projection planning.")
        tables = _tables(output)
        manual_members = (
            {
                row[0]
                for row in output.execute(
                    "SELECT reference_id FROM reviewed_statement_members"
                )
            }
            if "reviewed_statement_members" in tables
            else set()
        )
        if (
            _TABLES[0] in tables
            and output.execute("SELECT 1 FROM " + _TABLES[0] + " LIMIT 1").fetchone()
        ):
            if not set(_TABLES) <= tables:
                raise ValueError("The forwarded projection journal is incomplete.")
            proofs = _evidence(output)
            proof_ids = {proof["reference_id"] for proof in proofs}
            journal_ids = {
                row[0]
                for row in output.execute("SELECT reference_id FROM " + _TABLES[1])
            }
            used_groups = {proof["statement_id"] for proof in proofs}
            actual_ids = set().union(*(group_members[g] for g in used_groups))
            if proof_ids != journal_ids or actual_ids != proof_ids:
                raise ValueError(
                    "An applied forwarded projection has undeclared members."
                )
            for proof in proofs:
                candidate = candidates.get(proof["reference_id"])
                if (
                    candidate is None
                    or proof["source_pin_sha256"] != _sha(_json(candidate["pin"]))
                    or memberships[proof["reference_id"]] != proof["statement_id"]
                    or groups[proof["statement_id"]]["reference_id"]
                    != proof["representative_reference_id"]
                    or proof["basis"] != BASIS
                ):
                    raise ValueError(
                        "An applied forwarded projection changed; restore it before replay."
                    )
            return {
                **summary,
                "status": "already_applied",
                "projection_groups": len({p["statement_id"] for p in proofs}),
                "skipped_reason_counts": dict(skipped),
            }
        plans = []
        for key, values in sorted(cohorts.items()):
            check_cancelled()
            if not any(v["role"] == "forward" for v in values) or len(values) < 2:
                continue
            if not any(v["native"] for v in values):
                skipped["no_native_backup"] += 1
                continue
            ids = {v["id"] for v in values}
            cohort_groups = {memberships[i] for i in ids}
            if set().union(*(group_members[g] for g in cohort_groups)) != ids:
                skipped["unaccounted_existing_group_members"] += 1
                continue
            if any(
                count > 1
                for count in Counter(
                    v["parent"] for v in values if v["role"] == "forward"
                ).values()
            ):
                skipped["same_packet_multiplicity"] += 1
                continue
            if len({v["epoch"] for v in values if v["epoch"] is not None}) > 1:
                skipped["conflicting_explicit_instants"] += 1
                continue
            if any(
                len({v["constraint"][field] for v in values if v["constraint"][field]})
                > 1
                for field in (0, 1)
            ):
                skipped["conflicting_original_identity"] += 1
                continue
            primaries = [v for v in values if v["role"] == "primary"]
            if len({(*v["constraint"], v["epoch"]) for v in primaries}) > 1:
                skipped["multiple_native_primary_events"] += 1
                continue
            primary_groups = {memberships[v["id"]] for v in primaries}
            if any(
                v["role"] == "html_primary"
                and memberships[v["id"]] not in primary_groups
                for v in values
            ):
                skipped["unproven_html_primary"] += 1
                continue
            reviewed_roots = {
                groups[group]["reference_id"]
                for group in cohort_groups
                if group_members[group] & manual_members
            }
            if len(reviewed_roots) > 1:
                skipped["multiple_user_reviewed_representatives"] += 1
                continue
            if reviewed_roots:
                preferred = candidates[next(iter(reviewed_roots))]
            else:
                preferred = min(
                    values,
                    key=lambda v: (
                        v["rank"],
                        v["parent_epoch"]
                        if v["parent_epoch"] is not None
                        else float("inf"),
                        v["id"],
                    ),
                )
            winner = memberships[preferred["id"]]
            if (
                len(cohort_groups) == 1
                and groups[winner]["reference_id"] == preferred["id"]
            ):
                continue
            plans.append((key, values, preferred, winner, cohort_groups))
    except _Bounded:
        return {
            **summary,
            "status": "bounded",
            "skipped_reason_counts": {"metadata_budget": 1},
        }
    except (
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        OverflowError,
        sqlite3.DatabaseError,
    ):
        return {
            **summary,
            "status": "rejected",
            "skipped_reason_counts": {"changed_or_invalid_source": 1},
        }
    if not plans:
        return {**summary, "skipped_reason_counts": dict(skipped)}
    output.execute("SAVEPOINT fold_forwarded_projection")
    try:
        _ensure(output)
        for key, values, preferred, winner, cohort_groups in plans:
            for group in sorted(cohort_groups):
                output.execute(
                    "INSERT INTO " + _TABLES[0] + " VALUES(?,?,?)",
                    tuple(
                        groups[group][field]
                        for field in ("id", "reference_id", "conversation_key")
                    ),
                )
            for value in values:
                identifier = value["id"]
                output.execute(
                    "INSERT INTO " + _TABLES[1] + " VALUES(?,?)",
                    (identifier, memberships[identifier]),
                )
                evidence = {k: v for k, v in value.items() if k != "pin"}
                evidence.update(version=VERSION, basis=BASIS, source_pins=value["pin"])
                output.execute(
                    "INSERT INTO " + _TABLES[2] + " VALUES(?,?,?,?,?,?,?)",
                    (
                        identifier,
                        winner,
                        preferred["id"],
                        key,
                        BASIS,
                        _sha(_json(value["pin"])),
                        _json(evidence),
                    ),
                )
                output.execute(
                    "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
                    (winner, identifier),
                )
            output.execute(
                "UPDATE statement_groups SET reference_id=? WHERE id=?",
                (preferred["id"], winner),
            )
            output.executemany(
                "DELETE FROM statement_groups WHERE id=?",
                [(g,) for g in cohort_groups if g != winner],
            )
        check_cancelled()
        if source.total_changes != source_changes:
            raise _Changed(
                "The source changed before forwarded projection publication."
            )
        output.execute("RELEASE fold_forwarded_projection")
    except BaseException:
        output.execute("ROLLBACK TO fold_forwarded_projection")
        output.execute("RELEASE fold_forwarded_projection")
        raise
    return {
        **summary,
        "status": "applied",
        "statements_joined": sum(len(p[4]) - 1 for p in plans),
        "projection_groups": len(plans),
        "skipped_reason_counts": dict(skipped),
    }
