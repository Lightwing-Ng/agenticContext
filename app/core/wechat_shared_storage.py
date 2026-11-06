"""Share exact conversation bodies without merging source-account observations.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import sqlite3


_COLUMNS = (
    "id", "conversation_id", "ordinal", "sender_id", "sender_label", "direction",
    "text", "time_sort", "timestamp_value", "timestamp_label", "time_basis",
    "provenance", "quality_flags", "search_text",
)
_MARKER = "shared_message_storage"
_VERSION = "1"
_MAX_OBSERVATIONS = 2_000_000
_VIEW = """CREATE VIEW messages AS SELECT
 m.id,m.conversation_id,m.ordinal,m.sender_id,m.sender_label,m.direction,
 CASE WHEN m.content_ref IS NULL THEN m.text ELSE b.text END AS text,
 m.time_sort,m.timestamp_value,m.timestamp_label,m.time_basis,
 m.provenance,m.quality_flags,
 CASE WHEN m.content_ref IS NULL THEN m.search_text
      ELSE b.search_text || m.search_text END AS search_text
 FROM message_storage m LEFT JOIN shared_message_bodies b ON b.id=m.content_ref"""


def _sql(value):
    return " ".join(value.strip().rstrip(";").split())


def _triggers():
    names = ",".join(_COLUMNS)
    values = ",".join("NEW." + name for name in _COLUMNS)
    assignments = ",".join(name + "=NEW." + name for name in _COLUMNS)
    return {
        "shared_messages_insert": (
            "CREATE TRIGGER shared_messages_insert INSTEAD OF INSERT ON messages "
            f"BEGIN INSERT INTO message_storage({names},content_ref) "
            f"VALUES({values},NULL); END"
        ),
        "shared_messages_update": (
            "CREATE TRIGGER shared_messages_update INSTEAD OF UPDATE ON messages "
            f"BEGIN UPDATE message_storage SET {assignments},content_ref=NULL "
            "WHERE id=OLD.id; END"
        ),
        "shared_messages_delete": (
            "CREATE TRIGGER shared_messages_delete INSTEAD OF DELETE ON messages "
            "BEGIN DELETE FROM message_storage WHERE id=OLD.id; END"
        ),
        "shared_body_no_update": (
            "CREATE TRIGGER shared_body_no_update BEFORE UPDATE ON shared_message_bodies "
            "BEGIN SELECT RAISE(ABORT,'Shared message bodies are immutable'); END"
        ),
        "shared_body_no_replace": (
            "CREATE TRIGGER shared_body_no_replace BEFORE INSERT ON shared_message_bodies "
            "WHEN EXISTS(SELECT 1 FROM shared_message_bodies WHERE id=NEW.id) "
            "BEGIN SELECT RAISE(ABORT,'Shared message bodies cannot be replaced'); END"
        ),
        "shared_body_no_dangling_delete": (
            "CREATE TRIGGER shared_body_no_dangling_delete BEFORE DELETE ON shared_message_bodies "
            "WHEN EXISTS(SELECT 1 FROM message_storage WHERE content_ref=OLD.id) "
            "BEGIN SELECT RAISE(ABORT,'A shared message body is still referenced'); END"
        ),
        **{
            "shared_body_reference_" + operation.lower(): (
                "CREATE TRIGGER shared_body_reference_" + operation.lower()
                + " BEFORE " + operation + " ON message_storage "
                "WHEN NEW.content_ref IS NOT NULL AND (NEW.text!='' OR NOT EXISTS("
                "SELECT 1 FROM shared_message_bodies WHERE id=NEW.content_ref)) "
                "BEGIN SELECT RAISE(ABORT,'Invalid shared message body reference'); END"
            )
            for operation in ("INSERT", "UPDATE")
        },
    }


def shared_storage_installed(connection: sqlite3.Connection) -> bool:
    """Accept only this version's complete compatibility schema, never an arbitrary view."""
    row = connection.execute(
        "SELECT type,sql FROM sqlite_master WHERE name='messages'"
    ).fetchone()
    marker = connection.execute(
        "SELECT value FROM archive_info WHERE key=?", (_MARKER,)
    ).fetchone()
    if row is not None and row[0] == "table" and marker is None:
        return False
    if (
        row is None or row[0] != "view" or _sql(row[1]) != _sql(_VIEW)
        or marker is None or marker[0] != _VERSION
    ):
        raise ValueError("Unsupported shared WeChat message storage schema.")
    for table, columns in (
        ("messages", _COLUMNS),
        ("message_storage", (*_COLUMNS, "content_ref")),
        ("shared_message_bodies", ("id", "text", "search_text")),
    ):
        if table != "messages":
            ordinary = connection.execute(
                "SELECT type,sql FROM sqlite_master WHERE name=?", (table,)
            ).fetchone()
            if ordinary is None or ordinary[0] != "table" or "VIRTUAL" in ordinary[1].upper():
                raise ValueError("Shared WeChat message storage needs ordinary backing tables.")
        actual = tuple(
            item[1] for item in connection.execute(f"PRAGMA table_info({table})")
        )
        if actual != columns:
            raise ValueError("Shared WeChat message storage columns are incomplete.")
    for name, expected in _triggers().items():
        actual = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        if actual is None or _sql(actual[0]) != _sql(expected):
            raise ValueError("Shared WeChat message storage guards are incomplete.")
    return True


def _install(connection):
    if shared_storage_installed(connection):
        return
    connection.execute("ALTER TABLE messages RENAME TO message_storage")
    connection.execute("ALTER TABLE message_storage ADD COLUMN content_ref INTEGER")
    connection.execute(
        "CREATE TABLE shared_message_bodies ("
        "id INTEGER PRIMARY KEY,text TEXT NOT NULL,search_text TEXT NOT NULL)"
    )
    connection.execute(_VIEW)
    for statement in _triggers().values():
        connection.execute(statement)
    connection.execute(
        "INSERT INTO archive_info(key,value) VALUES(?,?)", (_MARKER, _VERSION)
    )


def _scope(connection, conversation_ids):
    if (
        not isinstance(conversation_ids, (list, tuple))
        or not 2 <= len(conversation_ids) <= 100
        or any(not isinstance(value, str) or not value for value in conversation_ids)
        or len(set(conversation_ids)) != len(conversation_ids)
    ):
        raise ValueError("Select two to 100 distinct, verified conversations.")
    result = {}
    identities = []
    for identifier in conversation_ids:
        row = connection.execute(
            "SELECT c.kind,c.peer_id,c.account_key,p.id,p.account_id,p.identity_status,p.account_key "
            "FROM conversations c JOIN partitions p ON p.id=c.partition_id WHERE c.id=?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise ValueError("A selected WeChat conversation is missing.")
        kind, peer, account_key, partition, owner, status, partition_key = row
        if (
            kind not in {"group", "direct"} or not isinstance(peer, str) or not peer
            or not isinstance(owner, str) or not owner
            or account_key != "account:" + owner
            or partition_key != account_key
            or partition != "ios-" + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
            or status != "Verified archive ID"
        ):
            raise ValueError("Body sharing requires independently verified native accounts.")
        result[identifier] = owner
        identities.append((kind, owner, peer))
    kinds = {identity[0] for identity in identities}
    if kinds == {"group"}:
        peers = {identity[2] for identity in identities}
        if (
            len(peers) != 1 or not next(iter(peers)).endswith("@chatroom")
            or len(set(result.values())) < 2
        ):
            raise ValueError("Body sharing requires different accounts in the same verified group.")
    elif kinds == {"direct"}:
        if (
            len(identities) != 2
            or identities[0][1] == identities[1][1]
            or identities[0][2] != identities[1][1]
            or identities[1][2] != identities[0][1]
        ):
            raise ValueError("Private body sharing requires two reciprocal, verified account owners.")
    else:
        raise ValueError("A shared-body scope cannot mix group and private conversations.")
    return result


def discover_reciprocal_private_scopes(connection):
    """Discover native owner-to-owner pairs from metadata, never names or bodies."""
    rows = connection.execute(
        "SELECT c.id,p.account_id,c.peer_id FROM conversations c "
        "JOIN partitions p ON p.id=c.partition_id WHERE c.kind='direct' "
        "AND p.identity_status='Verified archive ID' ORDER BY c.id"
    ).fetchall()
    owners = {row[1] for row in rows if isinstance(row[1], str) and row[1]}
    pairs = defaultdict(list)
    for identifier, owner, peer in rows:
        if owner and peer in owners and owner != peer:
            pairs[tuple(sorted((owner, peer)))].append(identifier)
    scopes = []
    skipped = []
    for pair, identifiers in sorted(pairs.items()):
        try:
            _scope(connection, identifiers)
        except ValueError:
            skipped.append({"conversation_ids": identifiers, "reason": "Not an unambiguous reciprocal native pair"})
        else:
            scopes.append(identifiers)
    return {"scopes": scopes, "skipped": skipped}


def _stored_payload_bytes(connection, scope):
    """Measure scoped inline bytes and the pool without scanning unrelated messages."""
    installed = shared_storage_installed(connection)
    table = "message_storage" if installed else "messages"
    inline = sum(
        connection.execute(
            "SELECT COALESCE(SUM(length(CAST(text AS BLOB))+length(CAST(search_text AS BLOB))),0) "
            f"FROM {table} WHERE conversation_id=?", (identifier,),
        ).fetchone()[0]
        for identifier in scope
    )
    pool = (
        connection.execute(
            "SELECT COALESCE(SUM(length(CAST(text AS BLOB))+length(CAST(search_text AS BLOB))),0) "
            "FROM shared_message_bodies"
        ).fetchone()[0]
        if installed else 0
    )
    return inline + pool


def _incremental_savings(connection, shared, scope):
    """Charge each new pooled body once and count only observations still inline."""
    installed = shared_storage_installed(connection)
    existing = {}
    if installed:
        for text, prefix in connection.execute("SELECT text,search_text FROM shared_message_bodies"):
            if not isinstance(text, str) or prefix != text.casefold():
                raise ValueError("A retained shared WeChat body is damaged.")
            key = _body_key(text)
            if key in existing and existing[key] != text:
                raise ValueError("A shared WeChat body digest collision was detected.")
            existing[key] = text
    new_bodies = {}
    inline_ids = set()
    already_shared = set()
    if installed:
        for conversation in scope:
            already_shared.update(
                row[0] for row in connection.execute(
                    "SELECT id FROM message_storage WHERE conversation_id=? AND content_ref IS NOT NULL",
                    (conversation,),
                )
            )
    savings = 0
    for entry in shared:
        key = _body_key(entry["text"])
        retained = existing.get(key)
        if retained is not None and retained != entry["text"]:
            raise ValueError("A shared WeChat body digest collision was detected.")
        size = len(entry["text"].encode()) + len(entry["prefix"].encode())
        for identifier in entry["ids"]:
            if identifier in inline_ids:
                continue
            if identifier not in already_shared:
                inline_ids.add(identifier)
                savings += size
                if retained is None:
                    prior = new_bodies.setdefault(key, entry["text"])
                    if prior != entry["text"]:
                        raise ValueError("A shared WeChat body digest collision was detected.")
    savings -= sum(len(text.encode()) + len(text.casefold().encode()) for text in new_bodies.values())
    return savings


def _rows(connection, scope):
    count = 0
    for identifier in sorted(scope):
        for row in connection.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (identifier,)
        ):
            count += 1
            if count > _MAX_OBSERVATIONS:
                raise ValueError("The selected shared-body scope is too large.")
            yield tuple(row)


def _row_digest(digest, row):
    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _body_key(text):
    return hashlib.sha256(text.encode()).digest()


def _plan(connection, scope):
    bodies = {}
    before = 0
    digest = hashlib.sha256()
    observations = 0
    for row in _rows(connection, scope):
        _row_digest(digest, row)
        observations += 1
        text, search = row[6], row[13]
        if not isinstance(text, str) or not isinstance(search, str):
            raise ValueError("A shared WeChat body is missing or damaged.")
        before += len(text.encode()) + len(search.encode())
        prefix = text.casefold()
        if not text or not search.startswith(prefix):
            continue
        key = _body_key(text)
        entry = bodies.setdefault(
            key, {"text": text, "prefix": prefix, "owners": set(), "ids": [], "suffixes": []}
        )
        if entry["text"] != text:
            raise ValueError("A shared WeChat body digest collision was detected.")
        entry["owners"].add(scope[row[1]])
        entry["ids"].append(row[0])
        entry["suffixes"].append(search[len(prefix):])
    shared = [entry for entry in bodies.values() if len(entry["owners"]) >= 2]
    already_shared = set()
    if shared_storage_installed(connection):
        for identifier in scope:
            already_shared.update(
                row[0] for row in connection.execute(
                    "SELECT id FROM message_storage WHERE conversation_id=? AND content_ref IS NOT NULL",
                    (identifier,),
                )
            )
    saved = sum(
        (len(entry["ids"]) - 1)
        * (len(entry["text"].encode()) + len(entry["prefix"].encode()))
        for entry in shared
    )
    report = {
        "storage_version": "v1.1.0-codex.0",
        "observations": observations,
        "accounts": len(set(scope.values())),
        "shared_payloads": len(shared),
        "shared_observations": sum(len(entry["ids"]) for entry in shared),
        "observations_to_rewrite": sum(
            message_id not in already_shared
            for entry in shared for message_id in entry["ids"]
        ),
        "payload_bytes_before": before,
        "payload_bytes_after": before - saved,
        "payload_bytes_saved": saved,
        "logical_sha256": digest.hexdigest(),
        "conversation_counts": {
            identifier: connection.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (identifier,)
            ).fetchone()[0]
            for identifier in sorted(scope)
        },
    }
    return report, shared


def analyze_shared_conversation_bodies(connection, conversation_ids):
    """Estimate exact body/search-prefix savings without modifying any database bytes."""
    shared_storage_installed(connection)
    scope = _scope(connection, conversation_ids)
    report, shared = _plan(connection, scope)
    stored = _stored_payload_bytes(connection, scope)
    savings = _incremental_savings(connection, shared, scope)
    report.update(
        stored_payload_bytes_before=stored,
        stored_payload_bytes_after=stored - savings,
        incremental_payload_bytes_saved=savings,
    )
    return report


def compact_shared_conversation_bodies(connection, conversation_ids):
    """Transactionally intern exact bytes; preserve every source row and account gap.

    The caller owns publication and any surrounding transaction. A savepoint ensures
    failed verification also restores the original schema. New imports stay inline
    until the next explicit compaction; passive reads never rewrite the archive.
    """
    scope = _scope(connection, conversation_ids)
    shared_storage_installed(connection)
    connection.execute("SAVEPOINT wechat_shared_bodies")
    try:
        before, shared = _plan(connection, scope)
        stored = _stored_payload_bytes(connection, scope)
        savings = _incremental_savings(connection, shared, scope)
        before.update(
            stored_payload_bytes_before=stored,
            stored_payload_bytes_after=stored - savings,
            incremental_payload_bytes_saved=savings,
        )
        if shared:
            _install(connection)
            # Reuse a prior pool entry by exact text, including across repeated scopes.
            existing = defaultdict(list)
            for identifier, text, prefix in connection.execute(
                "SELECT id,text,search_text FROM shared_message_bodies"
            ):
                if not isinstance(text, str) or prefix != text.casefold():
                    raise ValueError("A retained shared WeChat body is damaged.")
                existing[_body_key(text)].append((identifier, text))
            for entry in shared:
                matches = existing[_body_key(entry["text"])]
                if any(text != entry["text"] for _identifier, text in matches):
                    raise ValueError("A shared WeChat body digest collision was detected.")
                if matches:
                    identifier = matches[0][0]
                else:
                    identifier = connection.execute(
                        "INSERT INTO shared_message_bodies(text,search_text) VALUES(?,?)",
                        (entry["text"], entry["prefix"]),
                    ).lastrowid
                    matches.append((identifier, entry["text"]))
                connection.executemany(
                    "UPDATE message_storage SET text='',search_text=?,content_ref=? "
                    "WHERE id=? AND (content_ref IS NOT ? OR search_text IS NOT ?)",
                    (
                        (suffix, identifier, message_id, identifier, suffix)
                        for message_id, suffix in zip(entry["ids"], entry["suffixes"], strict=True)
                    ),
                )
            after_digest = hashlib.sha256()
            for row in _rows(connection, scope):
                _row_digest(after_digest, row)
            if after_digest.hexdigest() != before["logical_sha256"]:
                raise ValueError("Shared-body compaction changed a retained message.")
            shared_storage_installed(connection)
        if _stored_payload_bytes(connection, scope) != before["stored_payload_bytes_after"]:
            raise ValueError("Shared-body byte accounting differs from retained storage.")
        connection.execute("RELEASE wechat_shared_bodies")
        return before
    except BaseException:
        connection.execute("ROLLBACK TO wechat_shared_bodies")
        connection.execute("RELEASE wechat_shared_bodies")
        raise


def _validated_scopes(connection, conversation_scopes):
    if not isinstance(conversation_scopes, (list, tuple)) or not 1 <= len(conversation_scopes) <= 1_000:
        raise ValueError("Select one to 1,000 independently verified conversation scopes.")
    scopes = []
    seen = set()
    for identifiers in conversation_scopes:
        scope = _scope(connection, identifiers)
        if seen.intersection(scope):
            raise ValueError("Shared-body batch conversation scopes must not overlap.")
        seen.update(scope)
        scopes.append(scope)
    return scopes


def analyze_shared_conversation_scopes(connection, conversation_scopes):
    """Report one publication's exact marginal gain across disjoint safe scopes."""
    scopes = _validated_scopes(connection, conversation_scopes)
    reports, shared = [], []
    combined = {}
    observations = 0
    for scope in scopes:
        report, entries = _plan(connection, scope)
        observations += report["observations"]
        if observations > _MAX_OBSERVATIONS:
            raise ValueError("The combined shared-body batch is too large.")
        reports.append(report)
        shared.extend(entries)
        combined.update(scope)
    stored = _stored_payload_bytes(connection, combined)
    savings = _incremental_savings(connection, shared, combined)
    counts = {identifier: count for report in reports for identifier, count in report["conversation_counts"].items()}
    return {
        "storage_version": "v1.1.0-codex.0",
        "scopes": reports,
        "observations": observations,
        "accounts": len(set(combined.values())),
        "shared_payloads": len({_body_key(entry["text"]) for entry in shared}),
        "shared_observations": sum(report["shared_observations"] for report in reports),
        "observations_to_rewrite": sum(report["observations_to_rewrite"] for report in reports),
        "payload_bytes_before": sum(report["payload_bytes_before"] for report in reports),
        "payload_bytes_after": sum(report["payload_bytes_after"] for report in reports),
        "payload_bytes_saved": sum(report["payload_bytes_saved"] for report in reports),
        "stored_payload_bytes_before": stored,
        "stored_payload_bytes_after": stored - savings,
        "incremental_payload_bytes_saved": savings,
        "logical_sha256": hashlib.sha256(json.dumps(
            [report["logical_sha256"] for report in reports], separators=(",", ":")
        ).encode()).hexdigest(),
        "conversation_counts": counts,
    }


def compact_shared_conversation_scopes(connection, conversation_scopes):
    """Compact all scopes atomically and verify the combined measured byte delta."""
    _validated_scopes(connection, conversation_scopes)
    connection.execute("SAVEPOINT wechat_shared_batch")
    try:
        before = analyze_shared_conversation_scopes(connection, conversation_scopes)
        for identifiers in conversation_scopes:
            compact_shared_conversation_bodies(connection, identifiers)
        after = analyze_shared_conversation_scopes(connection, conversation_scopes)
        if (
            after["logical_sha256"] != before["logical_sha256"]
            or after["stored_payload_bytes_before"] != before["stored_payload_bytes_after"]
            or after["observations_to_rewrite"]
        ):
            raise ValueError("Shared-body batch changed retained content or byte accounting.")
        connection.execute("RELEASE wechat_shared_batch")
        return before
    except BaseException:
        connection.execute("ROLLBACK TO wechat_shared_batch")
        connection.execute("RELEASE wechat_shared_batch")
        raise


def analyze_shared_group_bodies(connection, conversation_ids):
    """Retain the original group API while accepting verified reciprocal private scopes."""
    return analyze_shared_conversation_bodies(connection, conversation_ids)


def compact_shared_group_bodies(connection, conversation_ids):
    """Retain the original group API while accepting verified reciprocal private scopes."""
    return compact_shared_conversation_bodies(connection, conversation_ids)
