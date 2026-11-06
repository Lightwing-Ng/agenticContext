"""Recover quoted clocks only from uniquely identified, source-scoped originals.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re

from .wechat_contact_embedded import native_detail
from .wechat_contacts import canonical_contact_id
from .wechat_forwarded_origins import _native_scope, _native_time, _source_snapshots


VERSION = "v1.2.1-codex.0"
RECOVERED_TIME_BASIS = "Recovered original message time; "
MAX_REFERENCES = 10_000
MAX_SCOPED_ROWS = 1_000_000
MAX_CONVERSATIONS = 256
MAX_SOURCE_BYTES = 128 * 1024**2
_SERVER_KEYS = {"svrid", "srcMsgSvrId", "dataitemsource/svrid", "dataitemsource/msgid"}
_SERVER = re.compile(r"-?[1-9][0-9]*")
EVIDENCE_COLUMNS = (
    "reference_id",
    "status",
    "original_message_id",
    "source_id",
    "source_snapshot_id",
    "original_server_id",
    "original_sender_id",
    "original_peer_id",
    "body_sha256",
    "quoted_body_sha256",
    "text_match",
    "timestamp_value",
    "time_basis",
    "reference_before_json",
)
EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS quoted_clock_evidence (
 reference_id TEXT PRIMARY KEY, status TEXT NOT NULL, original_message_id TEXT NOT NULL,
 source_id TEXT NOT NULL, source_snapshot_id TEXT NOT NULL,
 original_server_id TEXT NOT NULL, original_sender_id TEXT NOT NULL,
 original_peer_id TEXT NOT NULL, body_sha256 TEXT NOT NULL,
 quoted_body_sha256 TEXT NOT NULL, text_match TEXT NOT NULL,
 timestamp_value TEXT NOT NULL, time_basis TEXT NOT NULL,
 reference_before_json TEXT NOT NULL
) WITHOUT ROWID;
"""


class _BoundedRecovery(ValueError):
    """Leave all clocks unchanged when a scoped proof exceeds its budget."""


def ensure_clock_evidence_schema(output):
    output.execute(EVIDENCE_SCHEMA)


def copy_clock_evidence(retained, output):
    """Retain reversible exclusion proofs when accepted source identities match."""
    ensure_clock_evidence_schema(output)
    if not retained.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quoted_clock_evidence'"
    ).fetchone():
        return 0
    columns = tuple(
        row[1] for row in retained.execute("PRAGMA table_info(quoted_clock_evidence)")
    )
    if columns != EVIDENCE_COLUMNS:
        return 0
    count = 0
    cursor = retained.execute(
        "SELECT * FROM quoted_clock_evidence ORDER BY reference_id"
    )
    while rows := cursor.fetchmany(200):
        output.executemany(
            "INSERT OR REPLACE INTO quoted_clock_evidence VALUES("
            + ",".join("?" for _ in EVIDENCE_COLUMNS)
            + ")",
            [tuple(row) for row in rows],
        )
        count += len(rows)
    return count


def _server_id(values):
    if not isinstance(values, dict):
        return None
    found = []
    for key, value in values.items():
        if key not in _SERVER_KEYS:
            continue
        if not isinstance(value, str) or _SERVER.fullmatch(value) is None:
            return None
        found.append(value)
    return next(iter(set(found))) if len(set(found)) == 1 else None


def _summary(status, updates=(), rejected=(), verified=0, unresolved=0):
    return {
        "status": status,
        "recovered": len(updates),
        "verified": verified,
        "unresolved": unresolved,
        "updated_ids": list(updates),
        "rejected_author_ids": list(rejected),
    }


def _quoted_text_match(quoted, original):
    """Compare only the observed bilingual token pair after native event proof."""
    if quoted == original:
        return "exact"
    if quoted == "\n" + original:
        return "single_leading_newline"
    original_count = original.count("[Smirk]") + original.count("[奸笑]")
    if not original_count:
        return None
    candidates = [(quoted, "")]
    if quoted.startswith("\n"):
        candidates.append((quoted[1:], "single_leading_newline_"))
    for candidate, prefix in candidates:
        quoted_count = candidate.count("[Smirk]") + candidate.count("[奸笑]")
        if quoted_count == original_count and candidate.replace(
            "[奸笑]", "[Smirk]"
        ) == original.replace("[奸笑]", "[Smirk]"):
            return prefix + "localized_smirk_token"
    return None


def _reference_preimage(reference, signature, previous):
    """Keep the first reversible preimage when the complete native proof agrees."""
    encoded = json.dumps(reference, ensure_ascii=False, sort_keys=True)
    retained = previous.get(reference["id"])
    if retained is None or tuple(retained[:-1]) != tuple(signature):
        return encoded
    try:
        preimage = json.loads(retained[-1])
    except (ValueError, TypeError, RecursionError):
        return encoded
    derived = {
        "time_sort",
        "has_time",
        "timestamp_value",
        "timestamp_label",
        "time_basis",
    }
    if not isinstance(preimage, dict) or set(preimage) != set(reference):
        return encoded
    original_identity = {
        key: value for key, value in preimage.items() if key not in derived
    }
    current_identity = {
        key: value for key, value in reference.items() if key not in derived
    }
    if json.dumps(original_identity, sort_keys=True) != json.dumps(
        current_identity, sort_keys=True
    ):
        return encoded
    return retained[-1]


def _recover_quoted_reference_scope(
    source,
    output,
    contact_id,
    username,
    *,
    row_select,
    prepare_rows,
    native_thread_key,
    validate_native_source,
    accepted_authors=None,
    check_cancelled=lambda: None,
    reference_scope=None,
):
    """Recover or reject a quote only after resolving its exact native original.

    Lookups use the existing conversation index and explicitly requested server
    IDs. Original bodies, quote bodies, source clocks, and memberships are not
    changed. The caller applies any separately proved attribution exclusions.
    """
    ensure_clock_evidence_schema(output)
    try:
        if (
            canonical_contact_id(username, authority="native_message_sender")
            != contact_id
        ):
            return _summary("unsupported")
    except (ValueError, TypeError):
        return _summary("unsupported")
    allowed = {username} if accepted_authors is None else set(accepted_authors)
    if not allowed or username not in allowed:
        return _summary("unsupported")
    predicate = " AND source_id=? AND conversation_id=?" if reference_scope else ""
    cursor = output.execute(
        "SELECT * FROM refs WHERE CASE WHEN json_valid(component_path) THEN "
        "json_extract(component_path,'$[0]') IN ('native_quote','quote') "
        "ELSE 0 END" + predicate + " ORDER BY id LIMIT ?",
        (*reference_scope, MAX_REFERENCES + 1)
        if reference_scope
        else (MAX_REFERENCES + 1,),
    )
    names = [column[0] for column in cursor.description]
    references = [dict(zip(names, row)) for row in cursor]
    if len(references) > MAX_REFERENCES:
        return _summary("bounded")
    if not references:
        return _summary("ready")
    previous_evidence = {}
    for start in range(0, len(references), 200):
        batch = [reference["id"] for reference in references[start : start + 200]]
        marks = ",".join("?" for _ in batch)
        for row in output.execute(
            "SELECT * FROM quoted_clock_evidence WHERE reference_id IN (" + marks + ")",
            batch,
        ):
            previous_evidence[row[0]] = tuple(row)
    snapshots = _source_snapshots(source)
    partitions = {
        row["id"]: dict(row) for row in source.execute("SELECT * FROM partitions")
    }
    used_bytes = 0

    def fetch(identifiers, *, accepted=True):
        nonlocal used_bytes
        check_cancelled()
        identifiers = sorted(set(identifiers))
        marks = ",".join("?" for _ in identifiers)
        for _, size in source.execute(
            "SELECT id,length(CAST(text AS BLOB))+length(CAST(provenance AS BLOB))"
            "+length(CAST(search_text AS BLOB)) FROM main.messages WHERE id IN ("
            + marks
            + ")",
            identifiers,
        ):
            used_bytes += size
            if used_bytes > MAX_SOURCE_BYTES:
                raise _BoundedRecovery("Quoted clock inputs exceed their byte budget.")
        rows = [
            dict(row)
            for row in source.execute(
                row_select + "WHERE m.id IN (" + marks + ")", identifiers
            )
        ]
        if not accepted:
            return {row["id"]: row for row in rows}
        accepted_ids = {request["id"] for request in prepare_rows(source, rows)}
        return {row["id"]: row for row in rows if row["id"] in accepted_ids}

    def scope(row, proof):
        key = proof.get("source_snapshot")
        snapshot = snapshots.get(key) if type(key) is int else None
        partition = partitions.get(snapshot["source_id"]) if snapshot else None
        if snapshot is None or partition is None:
            return None
        raw_snapshot = {**snapshot, "metadata": json.dumps(snapshot["metadata"])}
        try:
            validate_native_source(row, proof, raw_snapshot, partition)
        except (ValueError, TypeError, KeyError):
            return None
        found = _native_scope(row, proof, snapshots, native_thread_key)
        return (*found, native_thread_key(row)) if found else None

    try:
        parents = sorted({row["parent_message_id"] for row in references})
        parent_rows = {}
        for start in range(0, len(parents), 200):
            parent_rows.update(fetch(parents[start : start + 200]))
        requests, wanted = [], defaultdict(set)
        for reference in references:
            check_cancelled()
            row = parent_rows.get(reference["parent_message_id"])
            if row is None or any(
                row[key] != reference[key]
                for key in ("source_id", "account_key", "conversation_id")
            ):
                continue
            proof = json.loads(row["provenance"])
            native = scope(row, proof)
            if native is None:
                continue
            detail = native_detail(
                proof,
                json.loads(reference["component_path"]),
                parent_id=row["id"],
                account_id=row["account_id"],
                peer_id=row["peer_id"] or "",
            )
            if (
                detail is None
                or detail.get("reference_kind") != "quoted_text"
                or type(detail.get("original_message_type")) is not int
                or detail["original_message_type"] != 1
                or detail["sender_id"] != username
                or detail["id"] != reference["id"]
                or detail["anchor_id"] != reference["anchor_id"]
                or detail.get("original_chat_id")
                not in {"", row["peer_id"], row["account_id"], username}
            ):
                continue
            server = _server_id(detail.get("source_ids"))
            if not server:
                continue
            requests.append((reference, row, proof, detail, native, server))
            wanted[(native, row["account_key"], row["peer_id"], row["kind"])].add(
                server
            )
        originals = defaultdict(dict)
        scoped_rows = 0
        for (native, account, peer, kind), servers in wanted.items():
            check_cancelled()
            conversations = source.execute(
                "SELECT id,message_count FROM conversations WHERE account_key=? AND peer_id=? AND kind=?",
                (account, peer, kind),
            ).fetchall()
            if len(conversations) > MAX_CONVERSATIONS:
                raise _BoundedRecovery("Too many source-scoped conversations.")
            scoped_rows += sum(row[1] for row in conversations)
            if scoped_rows > MAX_SCOPED_ROWS:
                raise _BoundedRecovery("Too many source-scoped original rows.")
            if not conversations:
                continue
            ids = [row[0] for row in conversations]
            marks = ",".join("?" for _ in ids)
            ordered_servers = sorted(servers)
            for offset in range(0, len(ordered_servers), 200):
                batch = ordered_servers[offset : offset + 200]
                server_marks = ",".join("?" for _ in batch)
                candidates = source.execute(
                    "SELECT id FROM main.messages WHERE conversation_id IN ("
                    + marks
                    + ") "
                    "AND CASE WHEN json_valid(provenance) THEN json_type(provenance,'$.source_snapshot')='integer' "
                    "AND json_extract(provenance,'$.source_snapshot')=? AND CAST(json_extract(provenance,'$.server_id') AS TEXT) "
                    "IN (" + server_marks + ") ELSE 0 END",
                    [*ids, native[1], *batch],
                ).fetchall()
                for start in range(0, len(candidates), 200):
                    rows = fetch(
                        [row[0] for row in candidates[start : start + 200]],
                        accepted=False,
                    )
                    for original in rows.values():
                        original_proof = json.loads(original["provenance"])
                        if scope(original, original_proof) != native:
                            continue
                        server = str(original_proof.get("server_id", ""))
                        if _SERVER.fullmatch(server) is None:
                            continue
                        originals[(*native, server)][original["id"]] = (
                            original,
                            original_proof,
                        )
        updates, evidence, rejected, recovered = [], [], [], []
        for reference, row, proof, detail, native, server in requests:
            check_cancelled()
            matches = originals.get((*native, server), {})
            if len(matches) != 1:
                continue
            original, original_proof = next(iter(matches.values()))
            epoch, parent_epoch = (
                _native_time(original, original_proof),
                _native_time(row, proof),
            )
            if (
                type(original_proof.get("message_type")) is not int
                or original_proof["message_type"] != 1
                or original_proof.get("text_extraction_kind") != "plain_text"
                or epoch is None
                or parent_epoch is None
                or epoch > parent_epoch
                or original["id"] == row["id"]
            ):
                continue
            actual = original["sender_id"]
            try:
                canonical_contact_id(actual, authority="native_message_sender")
            except (ValueError, TypeError):
                continue
            text_match = _quoted_text_match(detail["text"], original["text"])
            if text_match is None:
                continue
            status = "recovered" if actual in allowed else "wrong_author"
            basis = RECOVERED_TIME_BASIS + original["time_basis"]
            signature = (
                reference["id"],
                status,
                original["id"],
                original["source_id"],
                snapshots[original_proof["source_snapshot"]]["snapshot_id"],
                server,
                actual,
                original["peer_id"],
                hashlib.sha256(original["text"].encode()).hexdigest(),
                hashlib.sha256(detail["text"].encode()).hexdigest(),
                text_match,
                original["timestamp_value"],
                basis,
            )
            evidence.append(
                (
                    *signature,
                    _reference_preimage(reference, signature, previous_evidence),
                )
            )
            if status == "wrong_author":
                rejected.append(reference["id"])
                continue
            updates.append(
                (
                    epoch,
                    original["timestamp_value"],
                    original["timestamp_label"],
                    basis,
                    reference["id"],
                )
            )
            if not reference["has_time"]:
                recovered.append(reference["id"])
    except _BoundedRecovery:
        return _summary("bounded")
    output.executemany(
        "DELETE FROM quoted_clock_evidence WHERE reference_id=?",
        [(row["id"],) for row in references],
    )
    output.executemany(
        "UPDATE refs SET time_sort=?,has_time=1,timestamp_value=?,timestamp_label=?,time_basis=? WHERE id=?",
        updates,
    )
    output.executemany(
        "INSERT OR REPLACE INTO quoted_clock_evidence VALUES("
        + ",".join("?" for _ in EVIDENCE_COLUMNS)
        + ")",
        evidence,
    )
    return _summary(
        "ready", recovered, rejected, len(evidence), len(references) - len(evidence)
    )


def recover_quoted_reference_clocks(source, output, contact_id, username, **options):
    """Validate authors and clocks within independent, bounded source conversations.

    An unrelated large conversation must not cancel every other original-message
    proof. Known quote clocks require the same authorship check as missing clocks.
    """
    ensure_clock_evidence_schema(output)
    scopes = output.execute(
        "SELECT DISTINCT source_id,conversation_id FROM refs "
        "WHERE CASE WHEN json_valid(component_path) THEN "
        "json_extract(component_path,'$[0]') IN ('native_quote','quote') "
        "ELSE 0 END ORDER BY source_id,conversation_id"
    ).fetchall()
    results = [
        _recover_quoted_reference_scope(
            source, output, contact_id, username, reference_scope=scope, **options
        )
        for scope in scopes
    ]
    return {
        "status": "bounded"
        if any(result["status"] == "bounded" for result in results)
        else "ready",
        **{
            key: sum(result[key] for result in results)
            for key in ("recovered", "verified", "unresolved")
        },
        **{
            key: [value for result in results for value in result[key]]
            for key in ("updated_ids", "rejected_author_ids")
        },
    }


def unverified_room_quote_ids(
    source,
    output,
    username,
    *,
    row_select,
    prepare_rows,
    accepted_authors=None,
    check_cancelled=lambda: None,
):
    """Exclude ambiguous room roles unless an exact original proved their author.

    Literal chatusr values remain candidate hints. Missing originals, conflicting
    evidence, and recovery budgets cannot promote those hints into authorship.
    The derived exclusion retains its reference preimage for an auditable rebuild.
    """
    ensure_clock_evidence_schema(output)
    allowed = {username} if accepted_authors is None else set(accepted_authors)
    after, rejected = "", []
    while True:
        check_cancelled()
        cursor = output.execute(
            "SELECT * FROM refs WHERE id>? AND CASE WHEN json_valid(component_path) "
            "THEN json_extract(component_path,'$[0]') IN ('native_quote','quote') "
            "ELSE 0 END ORDER BY id LIMIT 200",
            (after,),
        )
        names = [column[0] for column in cursor.description]
        references = [dict(zip(names, row)) for row in cursor]
        if not references:
            return rejected
        after = references[-1]["id"]
        parents = sorted({reference["parent_message_id"] for reference in references})
        marks = ",".join("?" for _ in parents)
        rows = [
            dict(row)
            for row in source.execute(
                row_select + "WHERE m.id IN (" + marks + ")", parents
            )
        ]
        accepted = {request["id"] for request in prepare_rows(source, rows)}
        by_id = {row["id"]: row for row in rows}
        for reference in references:
            check_cancelled()
            row = by_id.get(reference["parent_message_id"])
            if row is None or row["id"] not in accepted:
                continue
            detail = native_detail(
                json.loads(row["provenance"]),
                json.loads(reference["component_path"]),
                parent_id=row["id"],
                account_id=row["account_id"] or "",
                peer_id=row["peer_id"] or "",
            )
            if not detail or not detail.get("author_role_ambiguous"):
                continue
            evidence = output.execute(
                "SELECT * FROM quoted_clock_evidence WHERE reference_id=?",
                (reference["id"],),
            ).fetchone()
            proof = dict(zip(EVIDENCE_COLUMNS, evidence)) if evidence else {}
            if (
                proof.get("status") == "recovered"
                and proof.get("original_sender_id") in allowed
                and proof.get("quoted_body_sha256")
                == hashlib.sha256(detail["text"].encode()).hexdigest()
                and proof.get("original_peer_id") == detail.get("original_chat_id")
            ):
                continue
            rejected.append(reference["id"])
            if proof.get("status") == "wrong_author":
                continue
            output.execute(
                "INSERT OR REPLACE INTO quoted_clock_evidence VALUES("
                + ",".join("?" for _ in EVIDENCE_COLUMNS)
                + ")",
                (
                    reference["id"],
                    "unverified_author",
                    "",
                    "",
                    "",
                    _server_id(detail.get("source_ids")) or "",
                    "",
                    "",
                    "",
                    hashlib.sha256(detail["text"].encode()).hexdigest(),
                    "",
                    "",
                    "",
                    json.dumps(reference, ensure_ascii=False, sort_keys=True),
                ),
            )
