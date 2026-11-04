"""Preserve verified legacy HTTP catalogs without claiming transcript completeness.

Code version: v1.0.0-codex.0

HTTP catalog IDs are source identities. Conversation URLs are explicitly derived
from the observed native legacy route. updatedAt describes conversation metadata,
never a message date. Response enumeration does not establish pagination order.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .resource_persistence import write_parquet_rows_atomic


COPILOT_LEGACY_CATALOG_SCHEMA_VERSION = 1
COPILOT_LEGACY_CATALOG_FILENAME = "legacy-catalog.parquet"
HTTP_SOURCE_KIND = "edge-http-cache-response"
METADATA_TIME_KIND = "conversation-updatedAt; not a message timestamp"
URL_BASIS = "derived from observed native legacy route and exact provider ID"
HTTP_CATALOG_ROUTE = "https://copilot.microsoft.com/c/api/conversations"
COPILOT_LEGACY_CATALOG_SCHEMA = pa.schema([
    pa.field("schema_version", pa.int16(), nullable=False),
    pa.field("conversation_id", pa.string(), nullable=False),
    pa.field("conversation_url", pa.string(), nullable=False),
    pa.field("conversation_url_basis", pa.string(), nullable=False),
    pa.field("latest_observed_title", pa.string(), nullable=False),
    pa.field("metadata_updated_at", pa.string(), nullable=False),
    pa.field("metadata_time_kind", pa.string(), nullable=False),
    pa.field("title_versions_json", pa.string(), nullable=False),
    pa.field("observations_json", pa.string(), nullable=False),
    pa.field("source_kind", pa.string(), nullable=False),
    pa.field("source_manifest_path", pa.string(), nullable=False),
    pa.field("source_manifest_sha256", pa.string(), nullable=False),
    pa.field("directory_sessions", pa.int64(), nullable=False),
    pa.field("directory_physical_records", pa.int64(), nullable=False),
    pa.field("directory_responses", pa.int64(), nullable=False),
    pa.field("catalog_complete", pa.bool_(), nullable=False),
    pa.field("cursor_chain_proven", pa.bool_(), nullable=False),
])
_IDENTITY = re.compile(r"[A-Za-z0-9]{1,128}")
_SHA256 = re.compile(r"[a-f0-9]{64}")


def copilot_legacy_catalog_path(root: Path | str) -> Path:
    return Path(root) / "llm" / "copilot" / COPILOT_LEGACY_CATALOG_FILENAME


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Legacy catalog JSON contains duplicate object keys.")
        result[key] = value
    return result


def _load_json(raw: bytes | str) -> Any:
    return json.loads(raw, object_pairs_hook=_unique_object)


def _time(value: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None


def _validate_record(record: object) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("Legacy HTTP catalog contains an unsupported record.")
    required = {"id", "title", "type", "updatedAt", "hasUnreadMessages", "group", "continuedAt", "isPinned"}
    if (
        not required.issubset(record)
        or not isinstance(record["id"], str) or not _IDENTITY.fullmatch(record["id"])
        or not isinstance(record["title"], str)
        or record["type"] != "chat"
        or not isinstance(record["updatedAt"], str)
        or type(record["hasUnreadMessages"]) is not bool
        or type(record["isPinned"]) is not bool
        or record["group"] is not None
        or (record["continuedAt"] is not None and not isinstance(record["continuedAt"], str))
    ):
        raise ValueError("Legacy HTTP catalog contains an unsupported record structure.")
    return record


def _catalog_row(identity: str, observations: list[dict[str, Any]], manifest: Mapping[str, Any]) -> dict[str, Any]:
    dated = [(stamp, item) for item in observations if (stamp := _time(item["record"]["updatedAt"])) is not None]
    newest = max((stamp for stamp, _ in dated), default=None)
    titles: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in observations:
        record = item["record"]
        stamp = _time(record["updatedAt"])
        titles[record["title"]].append({
            "response_ordinal": item["response_ordinal"],
            "result_index": item["result_index"],
            "updatedAt": record["updatedAt"],
            "observation_kind": (
                "unknown_metadata_time" if stamp is None else
                "latest_observed_metadata" if stamp == newest else "older_observed_metadata"
            ),
        })
    latest = sorted({item["record"]["title"] for stamp, item in dated if stamp == newest})
    latest_title = latest[0] if len(latest) == 1 else ""
    latest_times = sorted({item["record"]["updatedAt"] for stamp, item in dated if stamp == newest})
    return {
        "schema_version": 1,
        "conversation_id": identity,
        "conversation_url": f"https://copilot.microsoft.com/chats/{identity}",
        "conversation_url_basis": URL_BASIS,
        "latest_observed_title": latest_title,
        "metadata_updated_at": latest_times[0] if latest_times else "",
        "metadata_time_kind": METADATA_TIME_KIND,
        "title_versions_json": _json({
            "current_title_verified": False,
            "latest_observed_titles": latest,
            "versions": [{"title": title, "observations": refs} for title, refs in sorted(titles.items())],
        }),
        "observations_json": _json(observations),
        "source_kind": HTTP_SOURCE_KIND,
        "source_manifest_path": manifest["path"],
        "source_manifest_sha256": manifest["sha256"],
        "directory_sessions": manifest["directory_sessions"],
        "directory_physical_records": manifest["directory_physical_records"],
        "directory_responses": manifest["directory_responses"],
        "catalog_complete": False,
        "cursor_chain_proven": False,
    }


def build_copilot_legacy_catalog(manifest_path: Path, *, expected_manifest_sha256: str) -> dict[str, Any]:
    """Verify every retained entity and wire body before merging only exact IDs."""

    manifest_path = Path(manifest_path).resolve()
    raw = manifest_path.read_bytes()
    if not _SHA256.fullmatch(expected_manifest_sha256) or hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ValueError("Legacy HTTP catalog manifest hash does not match its preserved evidence.")
    manifest = _load_json(raw)
    responses = manifest.get("responses") if isinstance(manifest, dict) else None
    if not isinstance(responses, list) or not responses:
        raise ValueError("Legacy HTTP catalog manifest has no retained responses.")
    evidence = {"path": str(manifest_path), "sha256": expected_manifest_sha256}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pages = []
    seen_ordinals = set()
    for response in sorted(responses, key=lambda item: item["response_ordinal"]):
        ordinal = response["response_ordinal"]
        if type(ordinal) is not int or ordinal < 0 or ordinal in seen_ordinals:
            raise ValueError("Legacy HTTP catalog response ordinals are invalid or repeated.")
        seen_ordinals.add(ordinal)

        def verified_body(path_key: str, hash_key: str, size_key: str) -> tuple[Path, bytes]:
            target = Path(response[path_key])
            target = (target if target.is_absolute() else manifest_path.parent / target).resolve()
            if manifest_path.parent not in target.parents:
                raise ValueError("Legacy HTTP catalog evidence path escapes its preserved directory.")
            content = target.read_bytes()
            if len(content) != response[size_key] or hashlib.sha256(content).hexdigest() != response[hash_key]:
                raise ValueError("Legacy HTTP catalog response bytes failed preserved-evidence verification.")
            return target, content

        body_path, body = verified_body("body_path", "decoded_body_sha256", "decoded_body_bytes")
        wire_path, wire = verified_body("wire_body_path", "wire_body_sha256", "wire_body_bytes")
        if response["content_encoding"] != "gzip" or gzip.decompress(wire) != body:
            raise ValueError("Legacy HTTP catalog wire body does not match the decoded entity.")
        payload = _load_json(body)
        if (
            not isinstance(payload, dict) or not isinstance(payload.get("results"), list)
            or "next" not in payload or (payload["next"] is not None and not isinstance(payload["next"], str))
            or len(payload["results"]) != response["provider_catalog_records"]
            or response["redacted_request_route"] != HTTP_CATALOG_ROUTE
            or response.get("request_query_values_omitted") is not True
        ):
            raise ValueError("Retained HTTP response is not the verified native legacy catalog contract.")
        provenance = {
            "response_ordinal": ordinal,
            "decoded_body_path": str(body_path),
            "decoded_body_sha256": response["decoded_body_sha256"],
            "wire_body_path": str(wire_path),
            "wire_body_sha256": response["wire_body_sha256"],
            "request_route": response["redacted_request_route"],
            "request_query_values_omitted": True,
        }
        pages.append({**provenance, "payload": payload})
        for index, candidate in enumerate(payload["results"]):
            record = _validate_record(candidate)
            grouped[record["id"]].append({**provenance, "result_index": index, "record": record})
    if not grouped:
        raise ValueError("Retained HTTP catalogs contain no stable conversation identities.")
    counts = {
        "directory_sessions": len(grouped),
        "directory_physical_records": sum(len(page["payload"]["results"]) for page in pages),
        "directory_responses": len(pages),
    }
    rows = [_catalog_row(identity, observations, {**evidence, **counts}) for identity, observations in sorted(grouped.items())]
    return {
        "schema_version": 1,
        "source_kind": HTTP_SOURCE_KIND,
        "evidence_manifest": evidence,
        "catalog_complete": False,
        "cursor_chain_proven": False,
        "current_title_freshness_verified": False,
        "recovered_body_sessions": 0,
        "recovered_messages": 0,
        "response_count": len(pages),
        "physical_records": counts["directory_physical_records"],
        "distinct_sessions": len(rows),
        "raw_pages": pages,
        "conversations": rows,
    }


def _validated_rows(rows: object) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("Legacy catalog has no verified identities.")
    seen = set()
    manifest_refs = set()
    physical_refs = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(COPILOT_LEGACY_CATALOG_SCHEMA.names):
            raise ValueError("Legacy catalog has an unsupported schema.")
        identity = row["conversation_id"]
        if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity) or identity in seen:
            raise ValueError("Legacy catalog has invalid or repeated conversation identities.")
        seen.add(identity)
        manifest_ref = {
            "path": row["source_manifest_path"], "sha256": row["source_manifest_sha256"],
            **{key: row[key] for key in ("directory_sessions", "directory_physical_records", "directory_responses")},
        }
        if not isinstance(manifest_ref["path"], str) or not manifest_ref["path"] or not isinstance(manifest_ref["sha256"], str) or not _SHA256.fullmatch(manifest_ref["sha256"]):
            raise ValueError("Legacy catalog source provenance is invalid.")
        manifest_refs.add(tuple(manifest_ref.values()))
        if (
            type(row["schema_version"]) is not int or row["schema_version"] != 1
            or row["catalog_complete"] is not False or row["cursor_chain_proven"] is not False
            or any(type(row[key]) is not int or row[key] <= 0 for key in ("directory_sessions", "directory_physical_records", "directory_responses"))
        ):
            raise ValueError("Legacy catalog directory counts or completion flags are invalid.")
        observations = _load_json(row["observations_json"])
        if not isinstance(observations, list) or not observations:
            raise ValueError("Legacy catalog source observations are absent.")
        for item in observations:
            record = _validate_record(item.get("record") if isinstance(item, dict) else None)
            if record["id"] != identity:
                raise ValueError("Legacy catalog source identity differs from its merged identity.")
            for key in ("response_ordinal", "result_index"):
                if type(item.get(key)) is not int or item[key] < 0:
                    raise ValueError("Legacy catalog observation positions are invalid.")
            for key in ("decoded_body_sha256", "wire_body_sha256"):
                if not isinstance(item.get(key), str) or not _SHA256.fullmatch(item[key]):
                    raise ValueError("Legacy catalog observation hashes are invalid.")
            if item.get("request_route") != HTTP_CATALOG_ROUTE or item.get("request_query_values_omitted") is not True:
                raise ValueError("Legacy catalog observation request provenance is invalid.")
            for key in ("decoded_body_path", "wire_body_path"):
                if not isinstance(item.get(key), str) or not item[key]:
                    raise ValueError("Legacy catalog observation paths are invalid.")
            ref = (item["response_ordinal"], item["result_index"])
            if ref in physical_refs:
                raise ValueError("Legacy catalog repeats a physical source observation.")
            physical_refs.add(ref)
        if row != _catalog_row(identity, observations, manifest_ref):
            raise ValueError("Legacy catalog metadata disagrees with its retained observations.")
    if len(manifest_refs) != 1:
        raise ValueError("Legacy catalog contains inconsistent source manifests.")
    if len(rows) != manifest_ref["directory_sessions"] or len(physical_refs) != manifest_ref["directory_physical_records"]:
        raise ValueError("Legacy catalog is truncated relative to its retained directory counts.")
    if len({ordinal for ordinal, _ in physical_refs}) > manifest_ref["directory_responses"]:
        raise ValueError("Legacy catalog references more responses than its retained directory count.")
    return sorted(rows, key=lambda row: row["conversation_id"])


def write_copilot_legacy_catalog(catalog: Mapping[str, Any], destination: Path) -> None:
    """Write a reviewed catalog to an explicit destination; no production default."""

    rows = _validated_rows(catalog.get("conversations"))
    write_parquet_rows_atomic(Path(destination), rows, COPILOT_LEGACY_CATALOG_SCHEMA)
    actual = pq.read_table(destination)
    if not actual.schema.equals(COPILOT_LEGACY_CATALOG_SCHEMA) or actual.to_pylist() != rows:
        raise RuntimeError("Legacy catalog Parquet readback did not match its reviewed rows.")


def read_copilot_legacy_catalog(root: Path | str) -> dict[str, Any]:
    """Read durable coverage evidence conservatively, without creating any files."""

    path = copilot_legacy_catalog_path(root)
    result = {
        "path": str(path), "status": "missing", "present": path.exists() or path.is_symlink(),
        "conversation_ids": [], "sessions": None, "physical_records": None,
        "catalog_complete": False, "cursor_chain_proven": False,
        "reason": "No retained legacy catalog is available.",
    }
    if not result["present"]:
        return result
    try:
        table = pq.read_table(path)
        if not table.schema.equals(COPILOT_LEGACY_CATALOG_SCHEMA):
            raise ValueError("Legacy catalog Parquet schema is invalid.")
        rows = _validated_rows(table.to_pylist())
    except (OSError, TypeError, ValueError, KeyError, RecursionError, pa.ArrowException):
        return {**result, "status": "invalid", "reason": "Retained legacy catalog is unreadable or invalid; coverage remains unverified."}
    return {
        **result, "status": "valid", "conversation_ids": [row["conversation_id"] for row in rows],
        "sessions": len(rows),
        "physical_records": sum(len(_load_json(row["observations_json"])) for row in rows),
        "reason": "Retained legacy catalog identities are available; full body and cursor coverage remain unverified.",
    }
