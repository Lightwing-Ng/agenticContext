"""Recover audited resource children inside their proven HTML forwarding parent.

Code version: v1.0.3-codex.0
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import gzip
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import stat

from .wechat_archive import _file_identity, _identifier, wechat_archive_path
from .wechat_export_merge import _clonefile
from .wechat_media_event_index import _source_proofs, _validated_conversations
from .wechat_media_placeholders import _KINDS


VERSION = "v1.0.2-codex.0"
TABLE = "html_forwarded_resources"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
 id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
 account_key TEXT NOT NULL, source_snapshot_id INTEGER NOT NULL,
 source_id TEXT NOT NULL, parent_sha256 TEXT NOT NULL,
 ancestor_ordinals TEXT NOT NULL, ordinal INTEGER NOT NULL,
 node TEXT NOT NULL, proof TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS html_forwarded_resources_parent
 ON {TABLE}(parent_id,ordinal);
"""
_EXTRA = {"49": "app", "42": "contact", "48": "location"}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


def _resident(path):
    try:
        state = Path(path).lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(state.st_mode) and not getattr(state, "st_flags", 0) & 0x40000000
    )


def _primary(text, provenance, audit):
    """Use the exact retained primary, never an inferred forwarding caption."""
    value = text.split("\n\n[Forwarded text:", 1)[0]
    if (
        _sha(value) == provenance.get("primary_text_sha256") == audit["text_sha256"]
        and len(value) == audit["text_length"]
    ):
        return value
    return None


def _known_nodes(parents, audit):
    known = {}

    def add(value, root, path, text):
        locator = value.get("source_locator", value.get("locator", {}))
        ordinal = locator.get("message_ordinal")
        node = audit.get(ordinal)
        if (
            node is None
            or any(locator.get(k) != v for k, v in node["source_locator"].items())
            or not isinstance(text, str)
            or _sha(text) != node["text_sha256"]
            or len(text) != node["text_length"]
        ):
            return
        binding = {"root": root, "path": path, "text": text}
        if ordinal in known and known[ordinal] != binding:
            known[ordinal] = None
        else:
            known[ordinal] = binding
        children = value.get("forwarded_components", [])
        if len(path) >= 16 or not isinstance(children, list):
            return
        for child in children:
            if isinstance(child, dict):
                child_ordinal = child.get("source_locator", {}).get("message_ordinal")
                add(child, root, (*path, child_ordinal), child.get("text"))

    for row in parents:
        provenance = json.loads(row["provenance"])
        ordinal = provenance.get("locator", {}).get("message_ordinal")
        node = audit.get(ordinal)
        if node is None or node.get("role") != "top_level":
            continue
        primary = _primary(row["text"], provenance, node)
        if primary is not None:
            add(provenance, row, (), primary)
    return known


def recover_nodes(nodes, parents):
    """Keep proven active spans without carrying parents past unknown controls."""
    audit = {node["source_locator"]["message_ordinal"]: node for node in nodes}
    known = _known_nodes(parents, audit)
    stack, recovered, last = [], [], None

    def control(node, text):
        return len(text) == node["text_length"] and _sha(text) == node["text_sha256"]

    def end_span(cut, reason):
        while stack:
            frame = stack.pop()
            for entry in frame["entries"]:
                entry["end_unavailable"] = True
                entry["boundaries"].append(
                    {
                        "open": frame["open"],
                        "close": None,
                        "cut": cut,
                        "cut_reason": reason,
                        "caption_sha256": _sha(frame["binding"]["text"]),
                    }
                )
            if stack:
                stack[-1]["entries"].extend(frame["entries"])
            else:
                recovered.extend(frame["entries"])

    for node in nodes:
        ordinal = node["source_locator"]["message_ordinal"]
        if node["role"] == "boundary":
            if stack and control(node, stack[-1]["binding"]["text"] + " Ends >>"):
                frame = stack.pop()
                for entry in frame["entries"]:
                    entry["boundaries"].append(
                        {
                            "open": frame["open"],
                            "close": node,
                            "caption_sha256": _sha(frame["binding"]["text"]),
                        }
                    )
                if stack:
                    stack[-1]["entries"].extend(frame["entries"])
                else:
                    recovered.extend(frame["entries"])
            elif last and control(node, "<< " + last["text"]):
                expected_parent = stack[-1]["binding"] if stack else None
                if (
                    expected_parent is None
                    and last["path"]
                    or expected_parent is not None
                    and (
                        last["root"]["id"] != expected_parent["root"]["id"]
                        or len(last["path"]) != len(expected_parent["path"]) + 1
                        or last["path"][:-1] != expected_parent["path"]
                    )
                ):
                    end_span(node, "canonical_parent_conflict")
                    last = None
                else:
                    stack.append({"binding": last, "open": node, "entries": []})
            else:
                end_span(node, "unmatched_boundary")
                last = None
            continue
        if node["role"] == "top_level" and stack:
            end_span(node, "unclosed_before_top_level")
        if node["status"] == "omitted_forwarded_non_text" and stack:
            if node["text_kind"] == "deferred" or (
                node["text_kind"] == "rendered_app_text" and not node["text_length"]
            ):
                stack[-1]["entries"].append(
                    {
                        "node": node,
                        "binding": stack[-1]["binding"],
                        "boundaries": [],
                    }
                )
        last = known.get(ordinal)
    end_span(None, "end_of_audit")
    return recovered


def _validate_boundaries(value, parent):
    """Recheck the bound parent captions without opening the original audit again."""
    provenance = json.loads(parent["provenance"])
    primary = parent["text"].split("\n\n[Forwarded text:", 1)[0]
    if _sha(primary) != provenance.get("primary_text_sha256"):
        return False
    path = value["ancestor_ordinals"]
    if (
        not isinstance(path, list)
        or len(path) > 16
        or any(type(n) is not int for n in path)
    ):
        return False
    captions = [primary]
    current = provenance
    for ordinal in path:
        children = [
            child
            for child in current.get("forwarded_components", [])
            if child.get("source_locator", {}).get("message_ordinal") == ordinal
        ]
        if len(children) != 1 or not isinstance(children[0].get("text"), str):
            return False
        current = children[0]
        captions.append(current["text"])
    boundaries = value["proof"].get("boundaries", [])
    if len(boundaries) != len(captions):
        return False
    if any(boundary.get("close") is None for boundary in boundaries) != value[
        "proof"
    ].get("forwarding_end_unavailable", False):
        return False
    inner_open, inner_close = value["ordinal"], value["ordinal"]
    members = value["proof"].get(
        "frame_member_receipts", {value["proof"]["member_path"]: None}
    )
    for caption, boundary in zip(reversed(captions), boundaries, strict=True):
        if boundary.get("caption_sha256") != _sha(caption):
            return False
        opening, closing = boundary["open"], boundary["close"]
        controls = [(opening, "<< " + caption)]
        if closing is not None:
            controls.append((closing, caption + " Ends >>"))
        for node, expected in controls:
            if (
                node.get("role") != "boundary"
                or node.get("source_locator", {}).get("member_path") not in members
                or len(expected) != node.get("text_length")
                or _sha(expected) != node.get("text_sha256")
            ):
                return False
        left = opening["source_locator"]["message_ordinal"]
        if closing is not None:
            right = closing["source_locator"]["message_ordinal"]
        else:
            cut = boundary.get("cut")
            if boundary.get("cut_reason") not in {
                "unmatched_boundary",
                "canonical_parent_conflict",
                "unclosed_before_top_level",
                "end_of_audit",
            }:
                return False
            if cut is None:
                if boundary["cut_reason"] != "end_of_audit":
                    return False
                right = value["proof"]["audit_records"] + 1
            else:
                if cut.get("source_locator", {}).get(
                    "member_path"
                ) not in members or cut.get("role") not in {"boundary", "top_level"}:
                    return False
                right = cut["source_locator"]["message_ordinal"]
        if not left < inner_open <= inner_close <= right or value["ordinal"] >= right:
            return False
        inner_open, inner_close = left, right
    return True


def _read_audit(path, expected, *, allow_hydration):
    path = Path(path)
    state = path.lstat()
    dataless = bool(getattr(state, "st_flags", 0) & 0x40000000)
    if (
        not stat.S_ISREG(state.st_mode)
        or path.parent.name != "audit_nodes"
        or not path.name.endswith(".jsonl.gz")
        or state.st_size > 32 * 1024**2
        or (dataless and not allow_hydration)
    ):
        raise ValueError(
            "Only bounded, explicitly authorized audit metadata may be read."
        )
    if state.st_size != expected.get("compressed_bytes", state.st_size):
        raise ValueError("The original audit metadata size changed.")
    with path.open("rb") as stream:
        raw = stream.read(32 * 1024**2 + 1)
    if len(raw) > 32 * 1024**2:
        raise ValueError("The audit grew beyond its authorized metadata bound.")
    if _sha(raw) != expected["expected_audit_sha256"]:
        raise ValueError("The original node audit digest changed.")
    nodes, total = [], 0
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        header = json.loads(stream.readline(256 * 1024))
        if header != {"conversation": expected["expected_header"]}:
            raise ValueError("The node audit belongs to another conversation.")
        while line := stream.readline(256 * 1024 + 1):
            total += len(line)
            if len(line) > 256 * 1024 or total > 512 * 1024**2:
                raise ValueError("The node audit exceeds its decoded metadata budget.")
            nodes.append(json.loads(line))
    if len(nodes) != expected["expected_records"] or [
        n["source_locator"]["message_ordinal"] for n in nodes
    ] != list(range(1, len(nodes) + 1)):
        raise ValueError("The original audit frame ordering changed.")
    return nodes, {
        "path": str(path),
        "bytes": len(raw),
        "sha256": _sha(raw),
        "previously_dataless": dataless,
    }


def stage_html_forwarded_resources(
    local_store_root,
    base_index,
    candidate_path,
    inventory_path,
    *,
    audit_resolver=None,
    allow_metadata_hydration=False,
    audit_budget_bytes=256 * 1024**2,
    progress=None,
):
    """Clone a proven catalog and add embedded children without publishing or text writes."""
    history = wechat_archive_path(local_store_root).absolute()
    base, candidate, inventory_path = map(
        Path, (base_index, candidate_path, inventory_path)
    )
    if candidate.exists() or candidate.is_symlink():
        raise FileExistsError("Keep existing candidates unchanged.")
    if not all(_resident(p) for p in (history, base, inventory_path)):
        raise ValueError(
            "Primary, base catalog and inventory must already be resident."
        )
    if inventory_path.stat().st_size > 8 * 1024**2:
        raise ValueError("The source inventory exceeds its metadata budget.")
    inventory_raw = inventory_path.read_bytes()
    inventory = json.loads(inventory_raw)
    identities = _file_identity(history), _file_identity(base)
    report = {
        "version": VERSION,
        "inventory_sha256": _sha(inventory_raw),
        "audit_inputs": [],
        "sources": [],
        "restored_occurrences": 0,
        "stored_children": 0,
        "pending_occurrences": 0,
        "media_payload_files_read": 0,
        "raw_archives_opened": 0,
        "published": False,
    }
    _clonefile(base, candidate)
    try:
        with (
            closing(
                sqlite3.connect(history.as_uri() + "?mode=ro&immutable=1", uri=True)
            ) as original,
            closing(sqlite3.connect(candidate)) as output,
        ):
            original.row_factory = sqlite3.Row
            original.execute(
                "ATTACH DATABASE ? AS media_event_index",
                (base.resolve().as_uri() + "?mode=ro&immutable=1",),
            )
            _validated_conversations(original, identities[0])
            sources = _source_proofs(original)
            output.executescript(_SCHEMA)
            output.execute("PRAGMA journal_mode=DELETE")
            output.execute("PRAGMA synchronous=FULL")
            restored, audited, byte_count = set(), set(), 0
            for archive in inventory["archives"]:
                snapshot = archive["base_source_snapshot_id"]
                receipt = sources.get(snapshot)
                if (
                    receipt is None
                    or receipt[0]["archive_sha256"] != archive["archive_sha256"]
                ):
                    raise ValueError(
                        "The inventory source is not in the proven base catalog."
                    )
                source_count, pending_count = 0, 0
                for conversation in archive["conversations"]:
                    count = conversation["expected_header"]["counts"].get(
                        "omitted_forwarded_non_text", 0
                    )
                    if not count:
                        continue
                    if (
                        byte_count + conversation["compressed_bytes"]
                        > audit_budget_bytes
                    ):
                        raise ValueError(
                            "The cumulative audit metadata budget would be exceeded."
                        )
                    path = (
                        audit_resolver(conversation)
                        if audit_resolver
                        else Path(conversation["audit_path"])
                    )
                    nodes, audit_receipt = _read_audit(
                        path, conversation, allow_hydration=allow_metadata_hydration
                    )
                    byte_count += audit_receipt["bytes"]
                    report["audit_inputs"].append(audit_receipt)
                    member = conversation["expected_header"]["member_path"]
                    if (
                        receipt[3].get(member)
                        != conversation["main_member_receipt"]["sha256"]
                    ):
                        raise ValueError(
                            "The nested audit lost its original member receipt."
                        )
                    parents = [
                        dict(row)
                        for row in original.execute(
                            "SELECT m.id,m.text,m.provenance,m.conversation_id,c.account_key,c.partition_id "
                            "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                            "WHERE c.id=? AND (json_array_length(m.provenance,'$.forwarded_components')>0 "
                            "OR json_extract(m.provenance,'$.text_extraction_kind')='rendered_app_text')",
                            (conversation["selected_conversation_id"],),
                        )
                    ]
                    valid_parents = []
                    for parent in parents:
                        proof = json.loads(parent["provenance"])
                        if (
                            parent["partition_id"] == receipt[0]["source_id"]
                            and parent["account_key"] == receipt[2]["account_key"]
                            and proof.get("source_snapshot") == snapshot
                            and proof.get("locator", {}).get("member_path")
                            in receipt[3]
                        ):
                            valid_parents.append(parent)
                    entries = recover_nodes(nodes, valid_parents)
                    found = set()
                    for entry in entries:
                        node, binding = entry["node"], entry["binding"]
                        root = binding["root"]
                        ordinal = node["source_locator"]["message_ordinal"]
                        frame_member = node["source_locator"]["member_path"]
                        root_member = json.loads(root["provenance"])["locator"][
                            "member_path"
                        ]
                        frame_members = {frame_member, root_member}
                        for boundary in entry["boundaries"]:
                            for side in ("open", "close", "cut"):
                                if boundary.get(side) is not None:
                                    frame_members.add(
                                        boundary[side]["source_locator"]["member_path"]
                                    )
                        if any(path not in receipt[3] for path in frame_members):
                            continue
                        identifier = _identifier(
                            "wechat-html-nested-resource-v1",
                            archive["archive_sha256"],
                            root["partition_id"],
                            frame_member,
                            str(ordinal),
                        )
                        proof = {
                            "audit_sha256": audit_receipt["sha256"],
                            "member_path": frame_member,
                            "member_sha256": receipt[3][frame_member],
                            "root_member_path": root_member,
                            "main_member_path": member,
                            "main_member_sha256": conversation["main_member_receipt"][
                                "sha256"
                            ],
                            "frame_member_receipts": {
                                path: receipt[3][path] for path in sorted(frame_members)
                            },
                            "source_metadata_sha256": receipt[0]["metadata_sha256"],
                            "boundaries": entry["boundaries"],
                            "audit_records": conversation["expected_records"],
                            "forwarding_end_unavailable": entry.get(
                                "end_unavailable", False
                            ),
                        }
                        if not _validate_boundaries(
                            {
                                "ancestor_ordinals": list(binding["path"]),
                                "ordinal": ordinal,
                                "proof": proof,
                            },
                            root,
                        ):
                            continue
                        value = (
                            identifier,
                            root["id"],
                            root["conversation_id"],
                            root["account_key"],
                            snapshot,
                            root["partition_id"],
                            _sha(root["provenance"]),
                            _json(binding["path"]),
                            ordinal,
                            _json(node),
                            _json(proof),
                        )
                        existing = output.execute(
                            f"SELECT * FROM {TABLE} WHERE id=?", (identifier,)
                        ).fetchone()
                        if existing is not None and existing != value:
                            raise ValueError(
                                "A nested resource conflicts with existing evidence."
                            )
                        output.execute(
                            f"INSERT OR IGNORE INTO {TABLE} VALUES({','.join('?' for _ in value)})",
                            value,
                        )
                        found.add(ordinal)
                        restored.add((archive["archive_sha256"], member, ordinal))
                    omitted = {
                        n["source_locator"]["message_ordinal"]
                        for n in nodes
                        if n["status"] == "omitted_forwarded_non_text"
                    }
                    if len(omitted) != count:
                        raise ValueError("The original nested omission count changed.")
                    audited.update(
                        (archive["archive_sha256"], member, n) for n in omitted
                    )
                    source_count += len(found)
                    pending_count += len(omitted - found)
                report["sources"].append(
                    {
                        "source_id": archive["base_source_id"],
                        "restored": source_count,
                        "pending": pending_count,
                    }
                )
                if progress:
                    progress(report["sources"][-1])
            output.execute(
                "INSERT OR REPLACE INTO archive_info VALUES('html_forwarded_resources_version',?)",
                (VERSION,),
            )
            output.commit()
            if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError(
                    "The nested metadata candidate failed its integrity check."
                )
            report.update(
                restored_occurrences=len(restored),
                pending_occurrences=len(audited - restored),
                stored_children=output.execute(
                    f"SELECT COUNT(*) FROM {TABLE}"
                ).fetchone()[0],
                audit_metadata_bytes_read=byte_count,
                candidate=str(candidate),
            )
        if (_file_identity(history), _file_identity(base)) != identities:
            raise ValueError("An input changed during nested resource recovery.")
        return report
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise


class WechatHtmlForwardedResources:
    """Read only visible parents in bounded batches; cloud inputs stay unopened."""

    def __init__(self, local_store_root):
        self.history = wechat_archive_path(local_store_root).absolute()

    def project(self, rows, *, index_path=None):
        index = (
            Path(index_path)
            if index_path
            else self.history.with_name("media_events.sqlite3")
        )
        if not rows or not _resident(index) or not _resident(self.history):
            return {}
        result = defaultdict(list)
        try:
            with closing(
                sqlite3.connect(
                    self.history.as_uri() + "?mode=ro&immutable=1", uri=True
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute(
                    "ATTACH DATABASE ? AS media_event_index",
                    (index.as_uri() + "?mode=ro&immutable=1",),
                )
                info = dict(
                    connection.execute(
                        "SELECT key,value FROM media_event_index.archive_info"
                    )
                )
                if info.get("html_forwarded_resources_version") != VERSION:
                    return {}
                _validated_conversations(connection, _file_identity(self.history))
                sources = _source_proofs(connection)
                for start in range(0, len(rows), 200):
                    parents = {r["id"]: r for r in rows[start : start + 200]}
                    values = connection.execute(
                        f"SELECT * FROM media_event_index.{TABLE} WHERE parent_id IN ({','.join('?' for _ in parents)}) ORDER BY ordinal",
                        tuple(parents),
                    )
                    for value in values:
                        value = dict(value)
                        parent = parents[value["parent_id"]]
                        prov = json.loads(parent["provenance"])
                        source = sources.get(value["source_snapshot_id"])
                        proof, node = (
                            json.loads(value["proof"]),
                            json.loads(value["node"]),
                        )
                        if (
                            source is None
                            or _sha(parent["provenance"]) != value["parent_sha256"]
                            or parent["conversation_id"] != value["conversation_id"]
                            or prov.get("source_snapshot")
                            != value["source_snapshot_id"]
                            or source[0]["source_id"] != value["source_id"]
                            or source[2]["account_key"] != value["account_key"]
                            or source[0]["metadata_sha256"]
                            != proof["source_metadata_sha256"]
                            or source[3].get(proof["member_path"])
                            != proof["member_sha256"]
                            or source[3].get(proof["main_member_path"])
                            != proof["main_member_sha256"]
                            or any(
                                source[3].get(path) != sha
                                for path, sha in proof["frame_member_receipts"].items()
                            )
                            or prov.get("locator", {}).get("member_path")
                            != proof["root_member_path"]
                            or node.get("source_locator", {}).get("member_path")
                            != proof["member_path"]
                            or node.get("status") != "omitted_forwarded_non_text"
                            or node.get("source_locator", {}).get("message_ordinal")
                            != value["ordinal"]
                        ):
                            raise ValueError(
                                "A nested resource no longer matches its original parent."
                            )
                        value["node"], value["proof"] = node, proof
                        value["ancestor_ordinals"] = json.loads(
                            value["ancestor_ordinals"]
                        )
                        expected_id = _identifier(
                            "wechat-html-nested-resource-v1",
                            source[0]["archive_sha256"],
                            value["source_id"],
                            proof["member_path"],
                            str(value["ordinal"]),
                        )
                        if value["id"] != expected_id or not _validate_boundaries(
                            value, parent
                        ):
                            raise ValueError(
                                "The original forwarded boundary proof changed."
                            )
                        result[value["parent_id"]].append(value)
            return dict(result)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            sqlite3.Error,
            RecursionError,
        ):
            return {}

    def apply(self, items, rows):
        entries = self.project(rows)
        by_id = {row["id"]: row for row in rows}
        for item in items:
            values = entries.get(item.stable_id)
            if not values:
                continue
            record = item.messaging.get("forwarded_record")
            if record is None:
                from .wechat_forwarded import project_html_forwarded

                provenance = json.loads(by_id[item.stable_id]["provenance"])
                record = project_html_forwarded(
                    provenance,
                    parent_id=item.stable_id,
                    account_id=item.messaging["account_id"],
                    text=item.content_text,
                    components=provenance.get("forwarded_components", []),
                    source_proof_status="source_proof_verified",
                )
                if record is None and not provenance.get("forwarded_components"):
                    record = {
                        "title": item.content_text,
                        "source_title": item.content_text,
                        "items": (),
                        "authority": "source_verified_html_export_tree",
                    }
            if (
                not isinstance(record, dict)
                or record.get("authority") != "source_verified_html_export_tree"
            ):
                continue
            if _merge(record, values, account_id=item.messaging["account_id"]):
                item.messaging["forwarded_record"] = record
                item.messaging["display_text"] = ""


def _merge(record, values, *, account_id):
    from .wechat_forwarded import _child, forwarded_component_clock
    from .wechat_resources import RESOURCE_LABELS

    records = {(): record}

    def visit(group, path=()):
        for child in group.get("items", ()):
            node = child.messaging.get("provenance", {}).get(
                "embedded_html_forwarded_component", {}
            )
            ordinal = node.get("source_locator", {}).get("message_ordinal")
            nested = child.messaging.get("forwarded_record")
            if (
                isinstance(nested, dict)
                and nested.get("authority") == "source_verified_html_export_tree"
                and ordinal is not None
            ):
                records[(*path, ordinal)] = nested
                visit(nested, (*path, ordinal))

    visit(record)
    identities = {
        id(group): {child.stable_id for child in group["items"]}
        for group in records.values()
    }
    owners = {
        id(group): {
            child.messaging.get("provenance", {}).get("parent_message_id")
            for child in group["items"]
        }
        for group in records.values()
    }
    additions = defaultdict(list)
    for value in values:
        group = records.get(tuple(value["ancestor_ordinals"]))
        if (
            group is None
            or value["id"] in identities[id(group)]
            or owners[id(group)] - {value["parent_id"]}
        ):
            continue
        node = value["node"]
        kind = _KINDS.get(
            str(node.get("type")), (_EXTRA.get(str(node.get("type")), "unknown"), "")
        )[0]
        label = RESOURCE_LABELS.get(
            kind,
            {"contact": "Contact card", "location": "Location"}.get(
                kind, "Other message"
            ),
        )
        timestamp, clock_label, basis, flags = forwarded_component_clock(node)
        child = _child(
            stable_id=value["id"],
            ordinal=value["ordinal"],
            text="",
            author=node.get("sender_label") or "Unknown original sender",
            sender=node.get("sender_export_id", ""),
            direction=node.get("direction", "unknown"),
            timestamp=timestamp,
            label=clock_label,
            basis=basis,
            account_id=account_id,
            provenance={
                "embedded_html_forwarded_component": node,
                "parent_message_id": value["parent_id"],
                "locator": node["source_locator"],
                "source_time": node.get("raw_timestamp", ""),
                "html_forwarded_resource_proof": value["proof"],
            },
            flags=(
                *flags,
                "html_forwarded_resource_audit_placeholder",
                "original_conversation_unavailable",
                *(
                    ("forwarding_end_unavailable",)
                    if value["proof"].get("forwarding_end_unavailable")
                    else ()
                ),
            ),
            resource={
                "kind": kind,
                "kind_label": label,
                "label": label,
                "status": "metadata_only",
                "status_label": "Metadata only",
                "action_label": "",
                "can_reveal": False,
            },
        )
        child.messaging["sender_identifier_namespace"] = "unverified_export_field"
        additions[id(group)].append(child)
        identities[id(group)].add(value["id"])
    for group in records.values():
        if not additions[id(group)]:
            continue
        group["items"] = tuple(
            sorted(
                (*group["items"], *additions[id(group)]),
                key=lambda value: (
                    value.messaging.get("provenance", {})
                    .get("embedded_html_forwarded_component", {})
                    .get("source_locator", {})
                    .get("message_ordinal", 0)
                ),
            )
        )
    return sum(len(values) for values in additions.values())
