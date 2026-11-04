"""Inventory and match all retained HTML packets against native iPad evidence.

Code version: v1.0.3-codex.0
"""

from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import os
import re
import sqlite3
import tempfile
import time

from .wechat_archive import wechat_archive_path
from .wechat_contact_embedded import _identity, _readonly
from .wechat_forwarded import forwarded_component_clock


VERSION = "v1.0.3-codex.0"
_HEX = re.compile(r"[0-9a-f]{32}")
_ORDINAL = re.compile(r"0|[1-9][0-9]*")


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def packet_hint(proof, *, native):
    """Build a discovery hint; only the runtime source validator can accept a link."""
    roots = proof.get("text_components" if native else "forwarded_components")
    if not isinstance(roots, list) or not roots:
        return None
    pending = [
        (part, (number,)) for number, part in reversed(list(enumerate(roots, 1)))
    ]
    leaves, paths, unknown, resources, nested, invalid = [], [], 0, 0, False, False
    count = 0
    while pending:
        part, path = pending.pop()
        count += 1
        if not isinstance(part, dict) or count > 20_000 or len(path) > 32:
            invalid = True
            break
        kind = part.get("kind" if native else "text_kind")
        children = part.get("text_components" if native else "forwarded_components", [])
        if children:
            nested = True
            if not isinstance(children, list) or kind not in {
                "forwarded_group",
                "forwarded_text",
            }:
                invalid = True
                continue
            pending.extend(
                (child, (*path, number))
                for number, child in reversed(list(enumerate(children, 1)))
            )
            continue
        if (
            native
            and kind == "forwarded_resource"
            or not native
            and (
                kind in {"resource_placeholder", "deferred"}
                or str(part.get("type", "")) in {"3", "34", "43", "47", "62"}
            )
        ):
            resources += 1
            continue
        if not native and isinstance(part.get("text"), str) and part["text"].strip():
            unknown += int(not forwarded_component_clock(part)[0])
        if kind not in (
            {"forwarded_text"} if native else {"plain_text", "forwarded_rendered_text"}
        ):
            invalid = True
            continue
        text = part.get("text")
        if (
            not isinstance(text, str)
            or not text.strip()
            or part.get("quoted_components")
        ):
            invalid = True
            continue
        data = part.get("source_data_id") if native else part.get("export_msgid")
        if not native and isinstance(data, str):
            data = data.rsplit("_", 1)[-1]
        author = part.get("source_sender_id" if native else "sender_native_id")
        leaves.append((data, author, _hash(text)))
        paths.append(path)
        if native and not forwarded_component_clock(part, native=True)[0]:
            unknown += 1
    identifiers = [leaf[0] for leaf in leaves]
    id_kind = (
        "opaque"
        if identifiers
        and all(
            isinstance(value, str) and _HEX.fullmatch(value) for value in identifiers
        )
        else "ordinal"
        if identifiers
        and all(
            isinstance(value, str) and _ORDINAL.fullmatch(value)
            for value in identifiers
        )
        else "unsupported"
    )
    if len(set(map(str, identifiers))) != len(identifiers):
        invalid = True
    signature = (
        _hash(_encoded(leaves))
        if leaves and not invalid and id_kind != "unsupported"
        else ""
    )
    return {
        "shape": "unsupported" if invalid else "nested" if nested else "flat",
        "signature": signature,
        "body_signature": _hash(_encoded([leaf[1:] for leaf in leaves]))
        if leaves
        else "",
        "id_kind": id_kind,
        "leaf_count": len(leaves),
        "unknown_count": unknown,
        "author_count": len({leaf[1] for leaf in leaves}),
        "resources": resources,
        "paths": paths,
    }


def inventory(root, target, *, progress=lambda value: None):
    """Scan provenance once into a body-free index without changing archive inputs."""
    history = wechat_archive_path(root)
    before = _identity(history)
    target = Path(target)
    if target.exists():
        raise ValueError("A fresh inventory destination is required.")
    started, scanned, counts = time.monotonic(), 0, Counter()
    with (
        closing(_readonly(history)) as source,
        closing(sqlite3.connect(target)) as output,
    ):
        snapshots = {
            row["id"]: (dict(row), json.loads(row["metadata"]))
            for row in source.execute("SELECT * FROM source_snapshots")
        }
        output.executescript(
            "CREATE TABLE packets(id TEXT PRIMARY KEY,source_kind TEXT,source_id TEXT,snapshot INTEGER,"
            "account_key TEXT,peer_id TEXT,chat_kind TEXT,forwarder TEXT,shape TEXT,id_kind TEXT,"
            "signature TEXT,body_signature TEXT,leaf_count INTEGER,unknown_count INTEGER,"
            "author_count INTEGER,resources INTEGER,provenance_sha256 TEXT) WITHOUT ROWID;"
            "CREATE INDEX packets_signature ON packets(signature,source_kind);"
            "CREATE INDEX packets_body ON packets(body_signature,source_kind);"
            "CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID;"
        )
        cursor = source.execute(
            "SELECT m.id,m.provenance,c.partition_id,c.account_key,c.peer_id,c.kind "
            "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "WHERE instr(m.provenance,'forwarded_components')>0 OR instr(m.provenance,'text_components')>0"
        )
        batch = []
        for row in cursor:
            scanned += 1
            try:
                proof = json.loads(row["provenance"])
                snapshot = proof.get("source_snapshot")
                meta = snapshots.get(snapshot, ({}, {}))[1]
                native = (
                    meta.get("source_label") == "iPad archive"
                    and proof.get("message_type") == 49
                    and proof.get("text_extraction_kind") == "forwarded_text"
                )
                html = meta.get("source_label") == "HTML export" and bool(
                    proof.get("forwarded_components")
                )
                if not (native or html):
                    counts["other_envelopes"] += 1
                    continue
                hint = packet_hint(proof, native=native)
                if hint is None:
                    counts["empty_packets"] += 1
                    continue
                category = "ipad" if native else "html"
                counts[category + "_packets"] += 1
                if html:
                    counts["html_unknown_children"] += hint["unknown_count"]
                    counts["html_packets_with_unknown"] += int(
                        bool(hint["unknown_count"])
                    )
                forwarder = proof.get("sender_native_id") if html else ""
                batch.append(
                    (
                        row["id"],
                        category,
                        row["partition_id"],
                        snapshot,
                        row["account_key"],
                        row["peer_id"],
                        row["kind"],
                        forwarder,
                        hint["shape"],
                        hint["id_kind"],
                        hint["signature"],
                        hint["body_signature"],
                        hint["leaf_count"],
                        hint["unknown_count"],
                        hint["author_count"],
                        hint["resources"],
                        _hash(row["provenance"]),
                    )
                )
                if len(batch) >= 200:
                    output.executemany(
                        "INSERT INTO packets VALUES("
                        + ",".join("?" for _ in range(17))
                        + ")",
                        batch,
                    )
                    output.commit()
                    batch.clear()
                    progress({"scanned_envelopes": scanned, **counts})
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                counts["invalid_metadata"] += 1
        if batch:
            output.executemany(
                "INSERT INTO packets VALUES(" + ",".join("?" for _ in range(17)) + ")",
                batch,
            )
        if _identity(history) != before:
            raise ValueError("Archive changed during full packet inventory.")
        summary = {
            "version": VERSION,
            "scanned_envelopes": scanned,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            **counts,
        }
        output.executemany(
            "INSERT INTO info VALUES(?,?)",
            [("source_identity", _encoded(before)), ("summary", _encoded(summary))],
        )
        output.commit()
    return summary


def _native_vector(source, row, clock_rules):
    proof = json.loads(row["provenance"])
    owner, _snapshot, metadata = clock_rules._native_identity(source, row, proof)
    if metadata.get("source_label") != "iPad archive":
        raise ValueError("The candidate is not an iPad observation.")
    parts = clock_rules._native_parts({**proof, "_expected_text": row["text"]})
    details = clock_rules._native_packet(proof, row["id"], owner, row["peer_id"])
    if details is None or len(parts) != len(details):
        raise ValueError("Native packet text positions are incomplete.")
    vector = []
    for part, detail in zip(parts, details):
        stamp = (
            clock_rules._native_clock(part, detail)
            if detail.get("timestamp_value")
            else None
        )
        vector.append(
            (
                part["source_data_id"],
                detail["sender_id"],
                _hash(detail["text"]),
                stamp.replace(tzinfo=None).isoformat() if stamp else None,
                int(stamp.timestamp()) if stamp and stamp.tzinfo is not None else None,
            )
        )
    return vector


def clock_vectors_agree(vectors):
    """A missing timezone never resolves a disagreement between explicit instants."""
    if not vectors or len({len(vector) for vector in vectors}) != 1:
        return False
    for position in range(len(vectors[0])):
        values = [vector[position] for vector in vectors]
        if len({_encoded(value[:3]) for value in values}) != 1:
            return False
        if len({value[3] for value in values if value[3] is not None}) > 1:
            return False
        if len({value[4] for value in values if value[4] is not None}) > 1:
            return False
    return True


def assess_inventory(root, inventory_path, output_path, *, progress=lambda value: None):
    """Validate every unavailable HTML packet and retain explicit unresolved outcomes."""
    from . import wechat_forwarded_clocks as clock_rules

    history = wechat_archive_path(root)
    before = _identity(history)
    output_path = Path(output_path)
    if output_path.exists():
        raise ValueError("A fresh assessment destination is required.")
    counts, failures = Counter(), Counter()
    with (
        closing(_readonly(history)) as source,
        closing(sqlite3.connect(inventory_path)) as index,
        closing(sqlite3.connect(output_path)) as output,
    ):
        index.row_factory = sqlite3.Row
        inventory_info = dict(index.execute("SELECT key,value FROM info"))
        if json.loads(inventory_info.get("source_identity", "null")) != list(before):
            raise ValueError("Inventory belongs to a different archive publication.")
        completed = json.loads(inventory_info.get("summary", "null"))
        actual_counts = dict(
            index.execute(
                "SELECT source_kind,count(*) FROM packets GROUP BY source_kind"
            )
        )
        if (
            not isinstance(completed, dict)
            or completed.get("html_packets") != actual_counts.get("html", 0)
            or completed.get("ipad_packets") != actual_counts.get("ipad", 0)
            or not completed.get("scanned_envelopes")
        ):
            raise ValueError("Inventory has no complete, consistent packet census.")
        # Re-evaluate all indexed packet clocks with the same parser used in presentation.
        # This also includes malformed, nonempty HTML clocks in the unavailable census.
        for row in index.execute("SELECT * FROM packets").fetchall():
            raw = source.execute(
                "SELECT provenance FROM messages WHERE id=?", (row["id"],)
            ).fetchone()
            if raw is None or _hash(raw[0]) != row["provenance_sha256"]:
                raise ValueError("Inventory packet source pin differs.")
            hint = packet_hint(json.loads(raw[0]), native=row["source_kind"] == "ipad")
            if hint is None or any(
                hint[field] != row[field]
                for field in (
                    "shape",
                    "signature",
                    "body_signature",
                    "id_kind",
                    "leaf_count",
                    "author_count",
                    "resources",
                )
            ):
                raise ValueError(
                    "Inventory packet discovery evidence differs from its source."
                )
            index.execute(
                "UPDATE packets SET unknown_count=? WHERE id=?",
                (hint["unknown_count"], row["id"]),
            )
        index.commit()
        inventory_identity = _identity(Path(inventory_path))
        inventory_sha256 = _file_hash(inventory_path)
        output.executescript(
            "PRAGMA user_version=1;"
            "CREATE TABLE links(target_message_id TEXT PRIMARY KEY,native_message_id TEXT NOT NULL,"
            "target_text_sha256 TEXT NOT NULL,target_provenance_sha256 TEXT NOT NULL,"
            "native_text_sha256 TEXT NOT NULL,native_provenance_sha256 TEXT NOT NULL) WITHOUT ROWID;"
            "CREATE TABLE assessments(target_message_id TEXT PRIMARY KEY,status TEXT,unavailable_children INTEGER,"
            "native_candidates INTEGER,valid_candidates INTEGER,eligible_candidates INTEGER,"
            "recovered_children INTEGER,reason TEXT) WITHOUT ROWID;"
            "CREATE TABLE native_validation(native_message_id TEXT PRIMARY KEY,vector_json TEXT,reason TEXT) WITHOUT ROWID;"
            "CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID;"
        )
        targets = index.execute(
            "SELECT * FROM packets WHERE source_kind='html' AND unknown_count>0 ORDER BY id"
        ).fetchall()
        for number, target_hint in enumerate(targets, 1):
            counts["assessed_html_packets"] += 1
            counts["unavailable_html_children"] += target_hint["unknown_count"]
            candidates = index.execute(
                "SELECT id FROM packets WHERE source_kind='ipad' AND signature=? AND signature!='' ORDER BY id",
                (target_hint["signature"],),
            ).fetchall()
            vectors, eligible, errors = [], [], Counter()
            target = dict(
                source.execute(clock_rules._SELECT, (target_hint["id"],)).fetchone()
            )
            for candidate in candidates:
                native_id = candidate[0]
                native = dict(
                    source.execute(clock_rules._SELECT, (native_id,)).fetchone()
                )
                saved = output.execute(
                    "SELECT vector_json,reason FROM native_validation WHERE native_message_id=?",
                    (native_id,),
                ).fetchone()
                if saved is None:
                    try:
                        vector, reason = _native_vector(source, native, clock_rules), ""
                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                        AttributeError,
                        RecursionError,
                        OverflowError,
                    ) as error:
                        vector, reason = None, str(error)
                    output.execute(
                        "INSERT INTO native_validation VALUES(?,?,?)",
                        (native_id, _encoded(vector) if vector else "", reason),
                    )
                else:
                    vector, reason = (
                        json.loads(saved[0]) if saved[0] else None,
                        saved[1],
                    )
                if vector is None:
                    errors[reason] += 1
                    continue
                link = {
                    "target_message_id": target["id"],
                    "native_message_id": native_id,
                }
                for prefix, row in (("target", target), ("native", native)):
                    for field in ("text", "provenance"):
                        link[prefix + "_" + field + "_sha256"] = _hash(row[field])
                try:
                    recovered = clock_rules._link(
                        source,
                        target,
                        native,
                        link,
                        "0" * 64,
                        registry_info={
                            "source_identity": _encoded(before),
                            "candidate_agreement_receipt": str(output_path.resolve()),
                            "candidate_agreement_receipt_sha256": "0" * 64,
                        },
                    )
                except (
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    RecursionError,
                    OverflowError,
                ) as error:
                    errors[str(error)] += 1
                    recovered = None
                # Opaque IDs assert the same complete content across receiving copies;
                # ordinal IDs are meaningful only inside a separately proved parent event.
                if target_hint["id_kind"] == "opaque" or recovered:
                    vectors.append(vector)
                if recovered:
                    eligible.append((link, recovered, vector))
            if vectors and not clock_vectors_agree(vectors):
                status, reason, recovered_count = (
                    "native_clock_conflict",
                    "Validated iPad copies assert different child clocks.",
                    0,
                )
            elif eligible:
                link, recovered, _vector = min(
                    eligible,
                    key=lambda value: (
                        -sum(part[4] is not None for part in value[2]),
                        value[0]["native_message_id"],
                    ),
                )
                output.execute(
                    "INSERT INTO links VALUES(?,?,?,?,?,?)",
                    tuple(
                        link[key]
                        for key in (
                            "target_message_id",
                            "native_message_id",
                            "target_text_sha256",
                            "target_provenance_sha256",
                            "native_text_sha256",
                            "native_provenance_sha256",
                        )
                    ),
                )
                status, reason, recovered_count = "recovered", "", len(recovered)
            else:
                status, recovered_count = "unconfirmed", 0
                reason = "; ".join(sorted(errors)) or (
                    "Unsupported complete packet shape or child identifier namespace."
                    if not target_hint["signature"]
                    else "No corresponding iPad packet in the retained archive."
                )
            counts[status + "_packets"] += 1
            counts["recovered_html_children"] += recovered_count
            counts["remaining_unavailable_html_children"] += (
                target_hint["unknown_count"] - recovered_count
            )
            failures.update(errors)
            output.execute(
                "INSERT INTO assessments VALUES(?,?,?,?,?,?,?,?)",
                (
                    target["id"],
                    status,
                    target_hint["unknown_count"],
                    len(candidates),
                    len(vectors),
                    len(eligible),
                    recovered_count,
                    reason,
                ),
            )
            if number % 25 == 0:
                output.commit()
                progress(dict(counts))
        if (
            _identity(history) != before
            or _identity(Path(inventory_path)) != inventory_identity
        ):
            raise ValueError("Archive changed during full packet assessment.")
        summary = {
            "version": VERSION,
            "html_packets": index.execute(
                "SELECT count(*) FROM packets WHERE source_kind='html'"
            ).fetchone()[0],
            "ipad_packets": index.execute(
                "SELECT count(*) FROM packets WHERE source_kind='ipad'"
            ).fetchone()[0],
            **counts,
            "candidate_rejection_reasons": dict(failures),
        }
        output.executemany(
            "INSERT INTO info VALUES(?,?)",
            [
                ("source_identity", _encoded(before)),
                ("assessment_summary", _encoded(summary)),
                ("inventory_sha256", inventory_sha256),
                ("inventory_identity", _encoded(inventory_identity)),
                ("state", "complete"),
            ],
        )
        output.commit()
    return summary


def publish_assessment(root, assessment_path):
    """Atomically publish a complete assessment as the indexed runtime authority."""
    from . import wechat_forwarded_clocks as clock_rules

    root = Path(root)
    history = wechat_archive_path(root)
    before = _identity(history)
    assessment_path = Path(assessment_path).resolve()
    assessment_before = _identity(assessment_path)
    receipt_hash = _file_hash(assessment_path)
    columns = (
        "target_message_id",
        "native_message_id",
        "target_text_sha256",
        "target_provenance_sha256",
        "native_text_sha256",
        "native_provenance_sha256",
    )
    with closing(_readonly(assessment_path)) as assessment:
        info = dict(assessment.execute("SELECT key,value FROM info"))
        summary = json.loads(info.get("assessment_summary", "null"))
        links = [
            tuple(row)
            for row in assessment.execute(
                "SELECT " + ",".join(columns) + " FROM links ORDER BY target_message_id"
            )
        ]
        assessments = assessment.execute(
            "SELECT count(*),sum(unavailable_children),sum(recovered_children) FROM assessments"
        ).fetchone()
        expected = dict(
            assessment.execute(
                "SELECT target_message_id,recovered_children FROM assessments WHERE status='recovered'"
            )
        )
        if (
            info.get("state") != "complete"
            or json.loads(info.get("source_identity", "null")) != list(before)
            or not isinstance(summary, dict)
            or len(links) != summary.get("recovered_packets", 0)
            or assessments[0] != summary.get("assessed_html_packets")
            or assessments[1] != summary.get("unavailable_html_children")
            or assessments[2] != summary.get("recovered_html_children")
            or len(expected) != len(links)
            or {link[0] for link in links} != set(expected)
            or assessment.execute(
                "SELECT count(*) FROM assessments WHERE unavailable_children<0 "
                "OR recovered_children<0 OR recovered_children>unavailable_children "
                "OR (status!='recovered' AND recovered_children!=0)"
            ).fetchone()[0]
        ):
            raise ValueError(
                "Assessment is incomplete or belongs to a different source."
            )
    target = history.parent / "forwarded_clock_links.sqlite3"
    previous = _identity(target) if target.exists() else None
    descriptor, name = tempfile.mkstemp(
        prefix=".forwarded-clock-catalog-", suffix=".sqlite3", dir=target.parent
    )
    os.close(descriptor)
    staged = Path(name)
    try:
        with closing(sqlite3.connect(staged)) as output:
            output.execute("PRAGMA user_version=1")
            output.execute(
                "CREATE TABLE links(target_message_id TEXT PRIMARY KEY,native_message_id TEXT NOT NULL,"
                "target_text_sha256 TEXT NOT NULL,target_provenance_sha256 TEXT NOT NULL,"
                "native_text_sha256 TEXT NOT NULL,native_provenance_sha256 TEXT NOT NULL) WITHOUT ROWID"
            )
            output.execute(
                "CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID"
            )
            output.executemany("INSERT INTO links VALUES(?,?,?,?,?,?)", links)
            output.executemany(
                "INSERT INTO info VALUES(?,?)",
                [
                    ("source_identity", _encoded(before)),
                    ("candidate_agreement_receipt", str(assessment_path)),
                    ("candidate_agreement_receipt_sha256", receipt_hash),
                    ("batch_version", VERSION),
                    ("summary", _encoded(summary)),
                ],
            )
            output.commit()
            if output.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Clock catalog integrity verification failed.")
            if output.execute("SELECT count(*) FROM links").fetchone()[0] != len(links):
                raise ValueError("Clock catalog is incomplete.")
        staged_digest = _file_hash(staged)
        with closing(_readonly(history)) as source:
            receipt = {
                "source_identity": _encoded(before),
                "candidate_agreement_receipt": str(assessment_path),
                "candidate_agreement_receipt_sha256": receipt_hash,
            }
            for values in links:
                link = dict(zip(columns, values))
                html = source.execute(clock_rules._SELECT, (values[0],)).fetchone()
                native = source.execute(clock_rules._SELECT, (values[1],)).fetchone()
                if html is None or native is None:
                    raise ValueError("Clock publication source is missing.")
                recovered = clock_rules._link(
                    source,
                    dict(html),
                    dict(native),
                    link,
                    staged_digest,
                    registry_info=receipt,
                )
                if len(recovered) != expected[values[0]]:
                    raise ValueError("Clock publication differs from its assessment.")
        if (
            _identity(history) != before
            or _identity(assessment_path) != assessment_before
            or (_identity(target) if target.exists() else None) != previous
        ):
            raise ValueError("Clock publication inputs changed before publication.")
        os.replace(staged, target)
    finally:
        if staged.exists():
            staged.unlink()
    recovered, verified_targets = 0, 0
    with closing(_readonly(history)) as source:
        projector = clock_rules.WechatForwardedClocks(root)
        for link in links:
            row = dict(source.execute(clock_rules._SELECT, (link[0],)).fetchone())
            for attempt in range(3):
                if (
                    _identity(history) != before
                    or _file_hash(target) != staged_digest
                ):
                    raise ValueError("Published clock evidence changed during readback.")
                result = projector.project([row]).get(link[0], {})
                if result:
                    break
                if (
                    _identity(history) != before
                    or _file_hash(target) != staged_digest
                ):
                    raise ValueError("Published clock evidence changed during readback.")
                # Cloud file metadata can settle immediately after atomic replacement.
                # Retry only byte-identical authority and unchanged archive evidence.
                if attempt < 2:
                    time.sleep(0.1)
            if not result or len(result) != expected[link[0]]:
                raise ValueError("Published clock evidence did not read back.")
            recovered += len(result)
            verified_targets += 1
    if (
        recovered != summary["recovered_html_children"]
        or _identity(history) != before
        or _file_hash(target) != staged_digest
    ):
        raise ValueError(
            "Published clock evidence differs from the complete assessment."
        )
    return {
        **summary,
        "catalog": str(target.resolve()),
        "catalog_sha256": _file_hash(target),
        "verified_targets": verified_targets,
        "verified_recovered_children": recovered,
        "receipt_sha256": receipt_hash,
    }
