"""Stage independently receipted native media metadata without reading Message.

Code version: v1.0.0-codex.0
"""

from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3

from .wechat_archive import (
    _identifier,
    _time_fields,
    wechat_archive_path,
    wechat_history_partition_fingerprint,
)
from .wechat_export_merge import _clonefile
from .wechat_native_resource_revalidation import _anchor, _relative, _resident
from .wechat_primary_source_receipts import primary_source_database_receipts
from .wechat_resource_index import _KINDS, _LABELS, _json, _readonly


VERSION = "v1.0.0-codex.0"
MAX_EVIDENCE_BYTES = 16 * 1024**2
MAX_ROWS = 100_000
MAX_PROOF_BYTES = 128 * 1024**2
_SHA = re.compile(r"[0-9a-f]{64}")
_FIELDS = ("MesLocalID", "MesSvrID", "CreateTime", "Des", "Type")


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence(path):
    identity = _resident(path)
    if identity["size"] > MAX_EVIDENCE_BYTES:
        raise ValueError("Native media evidence exceeds its metadata budget.")
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or value.get("message_payload_column_read") is not False
        or value.get("media_files_read") != 0
        or not isinstance(value.get("rows"), list)
        or not 1 <= len(value["rows"]) <= MAX_ROWS
    ):
        raise ValueError("Select an explicit payload-free native metadata audit.")
    return value, identity


def _source(history, evidence):
    owner, peer = evidence.get("account_id"), evidence.get("peer_id")
    snapshot_id = evidence.get("source_snapshot_id")
    if not isinstance(owner, str) or not owner or not isinstance(peer, str) or not peer:
        raise ValueError("Native media evidence needs an explicit account and peer.")
    snapshot = history.execute(
        "SELECT * FROM source_snapshots WHERE id=?", (snapshot_id,)
    ).fetchone()
    if snapshot is None or (
        snapshot["source_id"] != evidence.get("source_id")
        or snapshot["snapshot_id"] != evidence.get("snapshot_id")
        or hashlib.sha256(snapshot["metadata"].encode()).hexdigest()
        != evidence.get("source_metadata_sha256")
    ):
        raise ValueError("Native media evidence changed its primary snapshot receipt.")
    scopes = history.execute(
        "SELECT c.* FROM conversations c WHERE c.account_key=? AND c.peer_id=? "
        "AND EXISTS(SELECT 1 FROM messages m WHERE m.conversation_id=c.id "
        "AND CASE WHEN json_valid(m.provenance) THEN "
        "json_extract(m.provenance,'$.source_snapshot')=? ELSE 0 END) LIMIT 129",
        ("account:" + owner, peer, snapshot_id),
    ).fetchall()
    if not scopes or len(scopes) > 128:
        raise ValueError("Native media evidence has no bounded primary anchor scope.")
    receipts = []
    for scope in scopes:
        receipts.append(
            primary_source_database_receipts(history, dict(scope), snapshot_id)
        )
    result = receipts[0]
    result = {**result, "source_title": scopes[0]["title"] or peer}
    path = _relative(evidence.get("database_relative_path"), owner)
    digest = evidence.get("source_database_sha256")
    if (
        not isinstance(digest, str)
        or _SHA.fullmatch(digest) is None
        or result["owner"] != owner
        or result["native_partition_id"] != evidence.get("source_id")
        or result["backup_id"] != evidence.get("snapshot_id")
        or result["receipts"].get((result["backup_id"], path)) != digest
        or result["file_ids"].get(path) != evidence.get("database_file_id")
        or any(
            proof["receipts"].get((result["backup_id"], path)) != digest
            for proof in receipts
        )
    ):
        raise ValueError(
            "The media database differs from independent primary receipts."
        )
    table = "Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
    if evidence.get("table") != table:
        raise ValueError("The media table belongs to another peer.")
    database = Path(evidence.get("source_database", ""))
    identity = _resident(database)
    if (
        not database.is_absolute()
        or tuple(database.parts[-len(PurePosixPath(path).parts) :])
        != PurePosixPath(path).parts
        or type(evidence.get("source_database_bytes")) is not int
        or identity["size"] != evidence["source_database_bytes"]
        or _sha256(database) != digest
    ):
        raise ValueError(
            "The resident media database failed its exact full-file receipt."
        )
    return result, path, table, database, identity


def _rows(database, table, evidence_rows):
    checked = []
    local_ids = set()
    with _readonly(database) as source:
        ordinary = source.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?", (table,)
        ).fetchone()
        columns = {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}
        if (
            ordinary is None
            or ordinary["type"] != "table"
            or "VIRTUAL" in str(ordinary["sql"]).upper()
            or not set(_FIELDS) <= columns
        ):
            raise ValueError(
                "The native media source needs an ordinary metadata table."
            )
        for value in evidence_rows:
            if not isinstance(value, dict) or any(key not in value for key in _FIELDS):
                raise ValueError("A media audit row lacks its exact native fields.")
            local = value["MesLocalID"]
            if (
                type(local) is not int
                or local < 1
                or local in local_ids
                or not isinstance(value["MesSvrID"], str)
                or re.fullmatch(r"-?[0-9]{1,20}", value["MesSvrID"]) is None
                or any(
                    type(value[key]) is not int for key in ("CreateTime", "Des", "Type")
                )
                or value["Type"] not in _KINDS
            ):
                raise ValueError(
                    "A native media audit row has invalid or repeated identity."
                )
            local_ids.add(local)
            optional = [key for key in ("Status", "ImgStatus") if key in value]
            if any(
                key not in columns or type(value[key]) is not int for key in optional
            ):
                raise ValueError(
                    "A media status must retain its exact recorded integer."
                )
            projected = [
                'CAST("MesSvrID" AS TEXT) AS "MesSvrID"'
                if key == "MesSvrID"
                else f'"{key}"'
                for key in (*_FIELDS, *optional)
            ]
            actual = source.execute(
                f'SELECT {",".join(projected)} FROM "{table}" WHERE MesLocalID=? LIMIT 2',
                (local,),
            ).fetchall()
            if len(actual) != 1 or any(
                actual[0][key] != value[key] for key in (*_FIELDS, *optional)
            ):
                raise ValueError(
                    "A native media audit differs from its recorded metadata row."
                )
            checked.append({key: value[key] for key in (*_FIELDS, *optional)})
    return checked


def _keys(proof, direction, owner, peer):
    locator = proof.get("locator")
    if not isinstance(locator, dict):
        return None, None
    table = "Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
    if locator.get("table") != table:
        return None, None
    try:
        local, signature = _anchor(proof, direction, locator)
    except (ValueError, TypeError, KeyError):
        return None, None
    server, epoch, typ, side = signature
    positive = (owner, peer, server, epoch, typ, side) if server else None
    path = locator.get("database_relative_path") or proof.get("source_relative_path")
    digest = locator.get("database_sha256") or proof.get("source_sha256")
    backup = proof.get("backup_id")
    exact = None
    if isinstance(backup, str) and isinstance(digest, str) and _SHA.fullmatch(digest):
        try:
            path = _relative(path, owner)
        except (ValueError, TypeError):
            pass
        else:
            exact = (owner, peer, backup, path, digest, table, local, epoch, typ, side)
    return positive, exact


def _existing(history, catalog, owner, peer):
    primary, indexed = (set(), set()), (set(), set())
    row_signatures = {}
    snapshots = {
        row[0]: (row[1], row[2])
        for row in history.execute(
            "SELECT s.id,s.snapshot_id,s.source_id FROM source_snapshots s "
            "JOIN partitions p ON p.id=s.source_id WHERE p.account_key=? AND p.account_id=?",
            ("account:" + owner, owner),
        )
    }
    for connection, target, is_primary in (
        (history, primary, True),
        (catalog, indexed, False),
    ):
        total = 0
        for number, row in enumerate(
            connection.execute(
                "SELECT m.provenance,m.direction FROM messages m JOIN conversations c "
                "ON c.id=m.conversation_id WHERE c.account_key=? AND c.peer_id=? LIMIT ?",
                ("account:" + owner, peer, MAX_ROWS + 1),
            ),
            1,
        ):
            total += len(row["provenance"].encode())
            if number > MAX_ROWS or total > MAX_PROOF_BYTES:
                raise ValueError(
                    "A media deduplication scope exceeds its proof budget."
                )
            proof = json.loads(row["provenance"])
            if not isinstance(proof, dict):
                raise ValueError(
                    "A retained media observation lacks object provenance."
                )
            if is_primary:
                snapshot = snapshots.get(proof.get("source_snapshot"))
                native = (
                    "ios-"
                    + hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
                )
                if snapshot is None or snapshot[1] != native:
                    continue
                proof = {**proof, "backup_id": snapshot[0]}
            positive, exact = _keys(proof, row["direction"], owner, peer)
            if positive:
                target[0].add(positive)
            if exact:
                target[1].add(exact)
                identity = exact[:7]
                signature = (positive[2] if positive else None, *exact[7:])
                if identity in row_signatures and row_signatures[identity] != signature:
                    raise ValueError(
                        "One retained native media row has conflicting metadata."
                    )
                row_signatures[identity] = signature
    return primary, indexed, row_signatures


def _project(evidence, raw, receipt, relative, table, conversation):
    owner, peer = receipt["owner"], evidence["peer_id"]
    local, epoch, typ = raw["MesLocalID"], raw["CreateTime"], raw["Type"]
    server = raw["MesSvrID"]
    server = server if 0 < int(server) <= 9223372036854775807 else None
    direction = {0: "sent", 1: "received"}.get(raw["Des"], "unknown")
    sender = (
        owner
        if direction == "sent"
        else peer
        if direction == "received" and conversation["kind"] == "direct"
        else None
    )
    source_path = "AppDomain-com.tencent.xin/" + relative
    identifier = _identifier(
        "wechat-resource-reference-v1",
        owner,
        receipt["backup_id"],
        source_path,
        table,
        str(local),
    )
    kind = _KINDS[typ]
    label = _LABELS[kind]
    reference = dict(
        kind=kind,
        label=label,
        filename="",
        file_extension="",
        checksum_md5="",
        byte_size=None,
        account_id=owner,
        peer_id=peer,
        unix_timestamp=epoch,
    )
    proof = dict(
        record_id=identifier,
        source_snapshot=evidence["source_snapshot_id"],
        source_snapshot_metadata_sha256=evidence["source_metadata_sha256"],
        locator=dict(
            database=PurePosixPath(relative).name,
            table=table,
            local_id=local,
            row_id=local,
            text_origin="native_resource_metadata",
        ),
        backup_id=receipt["backup_id"],
        source_relative_path=source_path,
        source_sha256=evidence["source_database_sha256"],
        source_checksum_basis=receipt["basis"],
        unix_timestamp=epoch,
        server_id=server,
        message_type=typ,
        text_extraction_kind="resource_placeholder",
        resource_reference=reference,
        recorded_native_metadata=raw,
    )
    try:
        clock = datetime.fromtimestamp(epoch, UTC).isoformat() if epoch > 0 else ""
    except (ValueError, OverflowError, OSError):
        clock = ""
    flags = ["resource_placeholder", "media_payload_not_read"]
    if sender is None:
        flags.append("sender_id_unavailable")
    if direction == "unknown":
        flags.append("direction_unavailable")
    return (
        identifier,
        conversation["id"],
        local,
        sender,
        sender or "Unknown sender",
        direction,
        label,
        *_time_fields({"sent_at_utc_iso": clock}),
        _json(proof),
        _json(flags),
        "\n".join((label, sender or "", conversation["title"], peer)).casefold(),
    ), proof


def _conversation(history, catalog, receipt, peer):
    native = receipt["native_partition_id"]
    rows = history.execute(
        "SELECT * FROM conversations WHERE partition_id=? AND peer_id=?", (native, peer)
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("A native media scope has ambiguous primary conversations.")
    if rows:
        result = dict(rows[0])
    else:
        result = dict(
            id="wechat:" + _identifier(native, peer),
            partition_id=native,
            account_key="account:" + receipt["owner"],
            peer_id=peer,
            title=receipt.get("source_title") or peer,
            kind="group" if peer.endswith("@chatroom") else "direct",
        )
    existing = catalog.execute(
        "SELECT * FROM conversations WHERE id=?", (result["id"],)
    ).fetchone()
    if existing and any(
        existing[key] != result[key]
        for key in ("partition_id", "account_key", "peer_id", "kind")
    ):
        raise ValueError("A native media supplement crosses an existing catalog scope.")
    return result


def stage_native_media_supplement(
    local_store_root, base_catalog_path, candidate_path, *, evidence_paths
):
    """Create a new sealed candidate from exact primary and source-database proof."""
    history_path = wechat_archive_path(local_store_root).absolute()
    base, candidate = (
        Path(base_catalog_path).absolute(),
        Path(candidate_path).absolute(),
    )
    paths = tuple(Path(path).absolute() for path in evidence_paths)
    if not 1 <= len(paths) <= 128 or len(set(paths)) != len(paths):
        raise ValueError("Select bounded, distinct native media evidence paths.")
    if (
        candidate.exists()
        or candidate.is_symlink()
        or not candidate.parent.is_dir()
        or candidate.parent.is_symlink()
    ):
        raise ValueError("Select a new candidate in an existing regular directory.")
    identities = {path: _resident(path) for path in (history_path, base)}
    base_digest = _sha256(base)
    pending, affected, source_reports, caches = [], {}, [], {}
    evidence_rows = evidence_bytes = 0
    with _readonly(history_path) as history, _readonly(base) as catalog:
        info = dict(catalog.execute("SELECT key,value FROM archive_info"))
        if (
            info.get("schema_version") != "1"
            or json.loads(info.get("history_file_identity", "null"))
            != identities[history_path]
            or info.get("history_partitions_sha256")
            != wechat_history_partition_fingerprint(history)
        ):
            raise ValueError(
                "The base resource catalog is not pinned to the current primary."
            )
        base_count = catalog.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        prior_report = json.loads(info.get("report", "{}"))
        if not isinstance(prior_report, dict):
            raise ValueError("The base resource catalog lacks object count receipts.")
        kinds = Counter(prior_report.get("kinds", {}))
        accounts = Counter(
            {
                row["account_id"]: row["messages"]
                for row in prior_report.get("accounts", [])
            }
        )
        if sum(kinds.values()) != base_count or sum(accounts.values()) != base_count:
            raise ValueError("The base resource catalog lacks exact count receipts.")
        for path in paths:
            evidence, identities[path] = _evidence(path)
            evidence_rows += len(evidence["rows"])
            evidence_bytes += identities[path]["size"]
            if evidence_rows > MAX_ROWS or evidence_bytes > MAX_PROOF_BYTES:
                raise ValueError(
                    "The selected media evidence exceeds its total budget."
                )
            receipt, relative, table, database, identities_db = _source(
                history, evidence
            )
            identities[database] = identities_db
            checked = _rows(database, table, evidence["rows"])
            conversation = _conversation(history, catalog, receipt, evidence["peer_id"])
            owner, peer = receipt["owner"], evidence["peer_id"]
            key = owner, peer
            if key not in caches:
                primary, indexed, row_signatures = _existing(
                    history, catalog, owner, peer
                )
                caches[key] = primary, indexed, (set(), set()), row_signatures
            primary, indexed, added, row_signatures = caches[key]
            report = dict(
                evidence=str(path),
                account_id=owner,
                peer_id=peer,
                source_snapshot_id=evidence["source_snapshot_id"],
                source_database_sha256=evidence["source_database_sha256"],
                anchors_checked=receipt["anchors_checked"],
                audited_rows=len(checked),
                primary_duplicates=0,
                catalog_duplicates=0,
                repeated_evidence=0,
                added=0,
                kinds={},
            )
            source_kinds = Counter()
            for raw in checked:
                row, proof = _project(
                    evidence, raw, receipt, relative, table, conversation
                )
                positive, exact = _keys(proof, row[5], owner, peer)
                identity = exact[:7]
                signature = (positive[2] if positive else None, *exact[7:])
                if identity in row_signatures and row_signatures[identity] != signature:
                    raise ValueError(
                        "A freshly checked native media row conflicts with retained metadata."
                    )
                row_signatures[identity] = signature
                duplicate = next(
                    (
                        name
                        for name, entries in (
                            ("primary_duplicates", primary),
                            ("catalog_duplicates", indexed),
                            ("repeated_evidence", added),
                        )
                        if positive in entries[0] or exact in entries[1]
                    ),
                    None,
                )
                if duplicate:
                    report[duplicate] += 1
                    continue
                if (
                    history.execute(
                        "SELECT 1 FROM messages WHERE id=?", (row[0],)
                    ).fetchone()
                    or catalog.execute(
                        "SELECT 1 FROM messages WHERE id=?", (row[0],)
                    ).fetchone()
                ):
                    raise ValueError(
                        "A supplemental media ID has unrelated existing evidence."
                    )
                pending.append(row)
                affected[conversation["id"]] = conversation
                if positive:
                    added[0].add(positive)
                added[1].add(exact)
                source_kinds[proof["resource_reference"]["kind"]] += 1
                kinds[proof["resource_reference"]["kind"]] += 1
                accounts[owner] += 1
            report.update(added=sum(source_kinds.values()), kinds=dict(source_kinds))
            source_reports.append(report)
    if any(_resident(path) != identity for path, identity in identities.items()):
        raise ValueError("A native media source changed during verification.")
    _clonefile(base, candidate)
    candidate.chmod(0o600)
    if _sha256(candidate) != base_digest:
        raise ValueError("The media candidate is not an exact base-catalog clone.")
    with closing(sqlite3.connect(candidate)) as output:
        output.row_factory = sqlite3.Row
        output.execute("PRAGMA journal_mode=DELETE")
        output.execute("PRAGMA synchronous=FULL")
        output.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pending
        )
        for identifier, conversation in affected.items():
            count = output.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (identifier,)
            ).fetchone()[0]
            first = output.execute(
                "SELECT time_sort,timestamp_value FROM messages WHERE conversation_id=? ORDER BY (time_sort=0),time_sort,ordinal,id LIMIT 1",
                (identifier,),
            ).fetchone()
            last = output.execute(
                "SELECT time_sort,timestamp_value,text FROM messages WHERE conversation_id=? ORDER BY time_sort DESC,ordinal DESC,id DESC LIMIT 1",
                (identifier,),
            ).fetchone()
            output.execute(
                "INSERT OR REPLACE INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    conversation[key]
                    for key in (
                        "id",
                        "partition_id",
                        "account_key",
                        "peer_id",
                        "title",
                        "kind",
                    )
                )
                + (count, first[0], last[0], first[1], last[1], last[2][:300]),
            )
        total = output.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if total != base_count + len(pending):
            raise ValueError("The media candidate changed unrelated message counts.")
        report = {
            **prior_report,
            "version": VERSION,
            "messages": total,
            "conversations": output.execute(
                "SELECT COUNT(*) FROM conversations"
            ).fetchone()[0],
            "kinds": dict(kinds),
            "accounts": [
                {"account_id": owner, "messages": number}
                for owner, number in sorted(accounts.items())
            ],
            "native_media_supplement": source_reports,
        }
        output.execute(
            "INSERT OR REPLACE INTO archive_info VALUES('messages',?)", (str(total),)
        )
        output.execute(
            "INSERT OR REPLACE INTO archive_info VALUES('report',?)", (_json(report),)
        )
        output.commit()
        if [tuple(row) for row in output.execute("PRAGMA quick_check")] != [("ok",)]:
            raise ValueError("The native media candidate failed its integrity check.")
    if any(_resident(path) != identity for path, identity in identities.items()):
        raise ValueError("A native media source changed before candidate completion.")
    with candidate.open("rb") as stream:
        os.fsync(stream.fileno())
    return dict(
        version=VERSION,
        candidate=str(candidate),
        base_catalog=str(base),
        base_sha256=base_digest,
        candidate_sha256=_sha256(candidate),
        added=len(pending),
        sources=source_reports,
        affected_conversations=len(affected),
        messages=total,
        media_payload_files_read=0,
        message_payload_columns_read=0,
        published=False,
    )
