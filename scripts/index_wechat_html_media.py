"""Publish a global, payload-free timeline from preserved HTML node audits.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.wechat_archive import _file_identity, _identifier, _time_fields
from app.core.wechat_html_export import (
    _local_reference,
    _native_avatar_id,
    normalize_html_clock,
)
from app.core.wechat_media_event_index import MEDIA_EVENT_SCHEMA, VERSION
from app.core.wechat_media_placeholders import _KINDS, _frame, media_placeholder_id


CODE_VERSION = "v1.0.1-codex.0"
MAX_COMPRESSED_AUDIT = 32 * 1024**2
MAX_NODE_LINE = 256 * 1024
EXTRA_KINDS = {
    "49": ("app", "[App message]"),
    "42": ("contact", "[Contact card]"),
    "48": ("location", "[Location]"),
}


def encoded(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def safe_bytes(path, maximum, digest=None):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError(f"Invalid bounded metadata input: {path}")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum or digest and hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"Metadata receipt changed: {path}")
    return raw


def projected(entry, header, members, owner, peer):
    node = entry["node"]
    empty_app = (
        node.get("text_kind") == "rendered_app_text"
        and str(node.get("type")) == "49"
        and node.get("text_length") == 0
        and node.get("text_sha256") == hashlib.sha256(b"").hexdigest()
    )
    frame = _frame(entry)
    if frame["page"] == 0:
        expected = header["member_path"]
    else:
        declaration = header.get("declared_async_counts", {})
        if frame["page"] > declaration.get("numberOfPages", 0):
            raise ValueError("An audited media page exceeds its original declaration.")
        expected = _local_reference(
            header["member_path"], declaration["data_path"] + f"/msg-{frame['page']}.js"
        )
    member = members.get(frame["member_path"])
    if expected != frame["member_path"] or member is None:
        raise ValueError("A media frame lacks its exact published member receipt.")
    identifier = media_placeholder_id(entry)
    direction = node.get("direction")
    classes = node.get("classes", [])
    if (
        node.get("role") != "top_level"
        or node.get("status") != "deferred_top_level"
        or (node.get("text_kind") != "deferred" and not empty_app)
        or direction not in {"sent", "received", "unknown"}
        or not isinstance(classes, list)
        or "msg" not in classes
        or not isinstance(node.get("raw_timestamp"), str)
    ):
        raise ValueError("An audited event lacks an original top-level frame.")
    sender = node.get("sender_native_id") or ""
    avatar_sender = _native_avatar_id(node.get("avatar_path", ""))
    flags = list(node.get("flags", []))
    if sender and avatar_sender and sender != avatar_sender:
        raise ValueError(
            "An audited sender conflicts with its original avatar identifier."
        )
    if direction == "sent" and sender and sender != owner:
        raise ValueError("An audited sent event crosses its source owner.")
    if (
        direction == "received"
        and header["conversation_kind"] == "direct"
        and sender
        and sender != peer
    ):
        raise ValueError("An audited received event crosses its direct peer.")
    if direction in {"sent", "received"}:
        side, other = ("right", "left") if direction == "sent" else ("left", "right")
        if side not in classes or other in classes:
            raise ValueError("An audited direction conflicts with its original frame.")
    if not sender:
        flags.append("sender_id_unavailable")
    source_type = str(node.get("type", ""))
    kind, label = (
        _KINDS.get(source_type)
        or EXTRA_KINDS.get(source_type)
        or ("other", "[Other message]")
    )
    clock, precision = normalize_html_clock(node["raw_timestamp"])
    ordering, timestamp, timestamp_label, basis = _time_fields(
        {"sent_at_raw": clock, "timeline_timestamp_usable": bool(clock)}
    )
    flags = list(
        dict.fromkeys(
            [
                *flags,
                "resource_placeholder",
                "media_payload_not_read",
                "html_media_node_audit_placeholder",
            ]
        )
    )
    if precision == "minute":
        flags.append("export_timestamp_precision_minutes")
    locator = {
        **frame,
        "archive_sha256": entry["archive_sha256"],
        "member": member,
        "export_msgid": str(node.get("export_msgid", "")),
        "export_msgid_namespace": "MesLocalID",
        "export_msgtype": source_type,
    }
    reference = dict(
        id=identifier,
        kind=kind,
        availability="metadata_only",
        source_id=entry["source_id"],
        source_snapshot_id=entry["source_snapshot_id"],
        account_id=owner,
        peer_id=peer,
        archive_sha256=entry["archive_sha256"],
        member_sha256=member["sha256"],
        audit_sha256=entry["audit_sha256"],
        frame=frame,
        export_msgid=str(node.get("export_msgid", "")),
        export_msgid_namespace="MesLocalID",
    )
    provenance = dict(
        record_id=identifier,
        source_snapshot=entry["source_snapshot_id"],
        source_time=node["raw_timestamp"],
        source_clock_precision=precision or "unknown",
        locator=locator,
        export_msgid_namespace="MesLocalID",
        text_extraction_kind="resource_placeholder",
        media_reference=reference,
        audit_receipt_basis="preserved_source_artifact_digest; no_payload_read",
        original_node=node,
    )
    row = (
        identifier,
        entry["conversation_id"],
        frame["message_ordinal"],
        sender or None,
        node.get("sender_label") or sender or "Unknown sender",
        direction,
        label,
        ordering,
        timestamp,
        timestamp_label,
        basis,
        encoded(provenance),
        encoded(flags),
        (label + "\n" + node.get("sender_label", "")).casefold(),
    )
    return row, kind


def build(arguments):
    os.umask(0o077)
    work = arguments.inventory.resolve().parent
    inventory_raw = safe_bytes(arguments.inventory, 8 * 1024**2)
    inventory = json.loads(inventory_raw)
    history = arguments.local_store.resolve() / "messaging/wechat/history.sqlite3"
    if history.stat().st_flags & 0x40000000:
        raise ValueError("The primary archive must already be resident.")
    history_identity = _file_identity(history)
    stage = work / "media-events.candidate.v1.0.0.sqlite3"
    if stage.exists():
        raise FileExistsError(
            "Retain the previous candidate; choose a new workstation for another build."
        )
    report = dict(
        version=CODE_VERSION,
        inventory_sha256=hashlib.sha256(inventory_raw).hexdigest(),
        sources=[],
        conversations=[],
        pending=[],
        media_or_zip_opened=False,
        native_catalog_hydrated=False,
        history_identity=history_identity,
    )
    totals, types, added_sources = Counter(), Counter(), set()
    with (
        closing(
            sqlite3.connect(history.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as original,
        closing(sqlite3.connect(stage)) as output,
    ):
        original.row_factory = sqlite3.Row
        output.executescript(MEDIA_EVENT_SCHEMA)
        output.execute("PRAGMA journal_mode=DELETE")
        output.execute("PRAGMA synchronous=FULL")
        for archive in inventory["archives"]:
            parsed = json.loads(
                safe_bytes(
                    Path(archive["parsed_export_path"]),
                    8 * 1024**2,
                    archive["parsed_export_sha256"],
                )
            )
            if parsed["archive_sha256"] != archive["archive_sha256"]:
                raise ValueError("The audit belongs to another archive.")
            members = {m["member_path"]: m for m in archive["selected_member_receipts"]}
            source_count = 0
            for conversation in archive["conversations"]:
                header = conversation["expected_header"]
                sid = conversation["selected_source_snapshot_id"]
                source = conversation["selected_source_id"]
                snapshot = original.execute(
                    "SELECT * FROM source_snapshots WHERE id=? AND source_id=?",
                    (sid, source),
                ).fetchone()
                partition = original.execute(
                    "SELECT * FROM partitions WHERE id=?", (source,)
                ).fetchone()
                assert snapshot is not None and partition is not None
                metadata = json.loads(snapshot["metadata"])
                owner = partition["account_id"]
                assert (
                    metadata["source_account_id"]
                    == owner
                    == archive["source_account_id"]
                )
                assert (
                    metadata["owner_verified"] is True
                    and metadata["owner_evidence"]["native_username"] == owner
                )
                assert (
                    snapshot["snapshot_id"]
                    == archive["archive_sha256"]
                    == metadata["source_sha256"]
                )
                source_members = {
                    m["member_path"]: m for m in metadata["selected_text_members"]
                }
                cid = conversation["selected_conversation_id"]
                if cid is None:
                    cid = "wechat:" + _identifier(source, header["member_path"])
                current = original.execute(
                    "SELECT * FROM conversations WHERE id=?", (cid,)
                ).fetchone()
                peer = conversation["peer_id"]
                if current is None:
                    assert conversation["requires_media_only_conversation"]
                    current = dict(
                        id=cid,
                        partition_id=source,
                        account_key="account:" + owner,
                        peer_id=peer,
                        title=header["title"],
                        kind=header["conversation_kind"],
                    )
                else:
                    current = dict(current)
                    assert (
                        current["partition_id"] == source
                        and current["peer_id"] == peer
                        and current["account_key"] == "account:" + owner
                    )
                assert current["kind"] == header["conversation_kind"]
                raw = safe_bytes(
                    Path(conversation["audit_path"]),
                    MAX_COMPRESSED_AUDIT,
                    conversation["expected_audit_sha256"],
                )
                totals["compressed_audit_bytes_read"] += len(raw)
                batch, count, seen_ordinals, dispositions, kinds = (
                    [],
                    0,
                    set(),
                    Counter(),
                    Counter(),
                )
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                    first = stream.readline(MAX_NODE_LINE + 1)
                    assert (
                        len(first) <= MAX_NODE_LINE
                        and json.loads(first)["conversation"] == header
                    )
                    while line := stream.readline(MAX_NODE_LINE + 1):
                        assert len(line) <= MAX_NODE_LINE
                        node = json.loads(line)
                        count += 1
                        ordinal = node["source_locator"]["message_ordinal"]
                        assert ordinal not in seen_ordinals
                        seen_ordinals.add(ordinal)
                        dispositions[node["status"]] += 1
                        if node["status"] != "deferred_top_level":
                            continue
                        frame_member = node["source_locator"]["member_path"]
                        member = source_members.get(frame_member)
                        if member is None or member["sha256"] != members.get(
                            frame_member, {}
                        ).get("sha256"):
                            report["pending"].append(
                                dict(
                                    source_id=source,
                                    conversation_id=cid,
                                    ordinal=ordinal,
                                    reason="Member receipt not published in this source.",
                                )
                            )
                            continue
                        entry = dict(
                            source_snapshot_id=sid,
                            source_id=source,
                            archive_sha256=archive["archive_sha256"],
                            member_sha256=member["sha256"],
                            audit_sha256=conversation["expected_audit_sha256"],
                            conversation_id=cid,
                            node=node,
                        )
                        try:
                            row, kind = projected(
                                entry, header, source_members, owner, peer
                            )
                        except ValueError as error:
                            report["pending"].append(
                                dict(
                                    source_id=source,
                                    conversation_id=cid,
                                    ordinal=ordinal,
                                    reason=str(error),
                                )
                            )
                            continue
                        assert (
                            original.execute(
                                "SELECT 1 FROM messages WHERE id=?", (row[0],)
                            ).fetchone()
                            is None
                        )
                        output.execute(
                            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            row,
                        )
                        batch.append((row[0], row[7], row[8], row[6]))
                        kinds[kind] += 1
                        types[str(node.get("type", ""))] += 1
                assert (
                    count
                    == conversation["expected_records"]
                    == header["counts"]["all_message_nodes"]
                )
                assert seen_ordinals == set(range(1, count + 1))
                assert dispositions["deferred_top_level"] == header["counts"].get(
                    "deferred_top_level", 0
                )
                totals["audited_nodes"] += count
                totals["deferred_top_level"] += dispositions["deferred_top_level"]
                if not batch:
                    continue
                if sid not in added_sources:
                    output.execute(
                        "INSERT INTO sources VALUES(?,?,?,?,?)",
                        (
                            sid,
                            snapshot["snapshot_id"],
                            source,
                            archive["archive_sha256"],
                            hashlib.sha256(snapshot["metadata"].encode()).hexdigest(),
                        ),
                    )
                    added_sources.add(sid)
                first = min(batch, key=lambda x: (x[1] == 0, x[1], x[0]))
                last = max(batch, key=lambda x: (x[1], x[0]))
                output.execute(
                    "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cid,
                        source,
                        "account:" + owner,
                        peer,
                        current["title"],
                        current["kind"],
                        len(batch),
                        first[1],
                        last[1],
                        first[2],
                        last[2],
                        last[3],
                    ),
                )
                main = source_members[header["member_path"]]
                output.execute(
                    "INSERT INTO conversation_proofs VALUES(?,?,?,?,?)",
                    (
                        cid,
                        sid,
                        header["member_path"],
                        main["sha256"],
                        conversation["expected_audit_sha256"],
                    ),
                )
                report["conversations"].append(
                    dict(
                        id=cid,
                        source_id=source,
                        added=len(batch),
                        kinds=dict(kinds),
                        media_only=conversation["requires_media_only_conversation"],
                    )
                )
                totals["published_events"] += len(batch)
                totals["published_conversations"] += 1
                source_count += len(batch)
                output.commit()
            report["sources"].append(
                dict(
                    source_id=archive["base_source_id"],
                    account_id=archive["source_account_id"],
                    published_events=source_count,
                )
            )
            print(encoded(report["sources"][-1]), flush=True)
        assert _file_identity(history) == history_identity
        output.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("proof_version", VERSION),
                ("published", "1"),
                ("primary_disjoint", "1"),
                ("history_file_identity", encoded(history_identity)),
            ],
        )
        output.commit()
        assert output.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            output.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            == totals["published_events"]
        )
    report.update(
        totals=dict(totals),
        source_type_counts=dict(types),
        candidate=str(stage),
        candidate_bytes=stage.stat().st_size,
    )
    (work / "build-report.v1.0.0.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        encoded(
            {k: v for k, v in report.items() if k not in {"conversations", "pending"}}
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--local-store", type=Path, required=True)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
