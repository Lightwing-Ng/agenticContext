"""Verified native HTTP catalogs remain durable, lossless, and incomplete evidence."""

# Code version: v1.1.0-codex.0

import copy
import gzip
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pytest

from app.core.copilot_legacy_catalog import (
    COPILOT_LEGACY_CATALOG_SCHEMA,
    HTTP_CATALOG_ROUTE,
    METADATA_TIME_KIND,
    build_copilot_legacy_catalog,
    copilot_legacy_catalog_path,
    read_copilot_legacy_catalog,
    write_copilot_legacy_catalog,
)
from app.core.resource_persistence import write_resource_rows_atomic


def _record(identity: str = "A" * 21, title: str = "Synthetic title", updated: str = "2026-10-21T01:00:00Z") -> dict:
    return {
        "id": identity, "title": title, "type": "chat", "updatedAt": updated,
        "hasUnreadMessages": False, "group": None, "continuedAt": None, "isPinned": False,
    }


def _evidence(root: Path, pages: list[dict] | None = None) -> tuple[Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    pages = pages if pages is not None else [
        {"results": [_record(), _record("B" * 21)], "next": "opaque-source-cursor"},
        {"results": [_record(), _record("C" * 21)], "next": None},
    ]
    responses = []
    for ordinal, payload in enumerate(pages, 1):
        body = json.dumps(payload, ensure_ascii=False).encode()
        wire = gzip.compress(body, mtime=0)
        body_path = root / f"response-{ordinal:03}.body.json"
        wire_path = root / f"response-{ordinal:03}.wire-body.bin"
        body_path.write_bytes(body)
        wire_path.write_bytes(wire)
        responses.append({
            "response_ordinal": ordinal, "body_path": body_path.name, "wire_body_path": wire_path.name,
            "decoded_body_sha256": hashlib.sha256(body).hexdigest(), "wire_body_sha256": hashlib.sha256(wire).hexdigest(),
            "decoded_body_bytes": len(body), "wire_body_bytes": len(wire), "content_encoding": "gzip",
            "provider_catalog_records": len(payload["results"]), "redacted_request_route": HTTP_CATALOG_ROUTE,
            "request_query_values_omitted": True,
        })
    path = root / "manifest.json"
    path.write_text(json.dumps({"responses": list(reversed(responses))}), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _build(root: Path, pages: list[dict] | None = None) -> dict:
    path, digest = _evidence(root, pages)
    return build_copilot_legacy_catalog(path, expected_manifest_sha256=digest)


def test_verified_catalog_merge_retains_every_physical_record_without_title_identity(tmp_path: Path) -> None:
    catalog = _build(tmp_path / "evidence")
    again = build_copilot_legacy_catalog(Path(catalog["evidence_manifest"]["path"]), expected_manifest_sha256=catalog["evidence_manifest"]["sha256"])
    assert catalog == again
    assert catalog["distinct_sessions"] == 3 and catalog["physical_records"] == 4
    assert [page["response_ordinal"] for page in catalog["raw_pages"]] == [1, 2]
    assert catalog["raw_pages"][0]["payload"]["next"] == "opaque-source-cursor"
    assert not catalog["catalog_complete"] and not catalog["cursor_chain_proven"]
    assert catalog["recovered_body_sessions"] == catalog["recovered_messages"] == 0
    rows = catalog["conversations"]
    assert len({row["conversation_id"] for row in rows}) == 3
    assert len({row["latest_observed_title"] for row in rows}) == 1
    observations = [item for row in rows for item in json.loads(row["observations_json"])]
    assert len(observations) == 4
    assert all(item["record"] == _record(item["record"]["id"]) for item in observations)
    assert all(item["request_query_values_omitted"] for item in observations)
    assert all("derived" in row["conversation_url_basis"] for row in rows)
    assert all(row["metadata_time_kind"] == METADATA_TIME_KIND for row in rows)


def test_title_versions_are_only_ordered_metadata_observations_not_verified_current_titles(tmp_path: Path) -> None:
    older = _record(title="Older synthetic title", updated="2026-10-20T01:00:00Z")
    newer = _record(title="Newer synthetic title")
    newer["extra_metadata"] = {"preserved": [1, None, "raw"]}
    catalog = _build(tmp_path / "evidence", [{"results": [older], "next": "cursor"}, {"results": [newer], "next": None}])
    row = catalog["conversations"][0]
    versions = json.loads(row["title_versions_json"])
    assert row["latest_observed_title"] == newer["title"]
    assert versions["current_title_verified"] is False
    assert {item["observation_kind"] for version in versions["versions"] for item in version["observations"]} == {"older_observed_metadata", "latest_observed_metadata"}
    assert json.loads(row["observations_json"])[1]["record"] == newer


def test_same_timestamp_title_conflict_and_unknown_time_are_explicit(tmp_path: Path) -> None:
    catalog = _build(tmp_path / "evidence", [{"results": [_record(title="One"), _record(title="Two"), _record("B" * 21, updated="timezone-unknown")], "next": None}])
    first, second = catalog["conversations"]
    assert first["latest_observed_title"] == ""
    assert json.loads(first["title_versions_json"])["latest_observed_titles"] == ["One", "Two"]
    assert second["metadata_updated_at"] == ""
    assert json.loads(second["observations_json"])[0]["record"]["updatedAt"] == "timezone-unknown"
    assert "message timestamp" in second["metadata_time_kind"]


def test_reader_is_read_only_and_sqlite_retains_truncation_detection_counts(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    state = read_copilot_legacy_catalog(root)
    assert state["status"] == "missing" and state["sessions"] is None and not root.exists()
    catalog = _build(tmp_path / "evidence")
    path = copilot_legacy_catalog_path(root)
    write_copilot_legacy_catalog(catalog, path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    state = read_copilot_legacy_catalog(root)
    assert state["status"] == "valid" and state["present"] is True
    assert state["conversation_ids"] == ["A" * 21, "B" * 21, "C" * 21]
    assert state["sessions"] == 3 and state["physical_records"] == 4
    assert state["catalog_complete"] is False
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    write_resource_rows_atomic(path, catalog["conversations"][:-1], COPILOT_LEGACY_CATALOG_SCHEMA)
    assert read_copilot_legacy_catalog(root)["status"] == "invalid"


def test_legacy_parquet_catalog_remains_readable_without_creating_sqlite(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    catalog = _build(tmp_path / "evidence")
    path = copilot_legacy_catalog_path(root)
    legacy = path.with_suffix(".parquet")
    write_resource_rows_atomic(legacy, catalog["conversations"], COPILOT_LEGACY_CATALOG_SCHEMA)
    original = legacy.read_bytes()

    state = read_copilot_legacy_catalog(root)

    assert state["status"] == "valid" and state["sessions"] == 3
    assert not path.exists()
    assert legacy.read_bytes() == original


@pytest.mark.parametrize("kind", ["empty", "wrong-schema", "bad-bytes", "broken-symlink", "duplicate-id", "bad-observations", "deep-observations", "claims-complete", "wrong-source-id"])
def test_present_invalid_catalog_never_becomes_an_empty_valid_directory(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "runtime"
    catalog = _build(tmp_path / "evidence")
    rows = copy.deepcopy(catalog["conversations"])
    path = copilot_legacy_catalog_path(root)
    path.parent.mkdir(parents=True)
    if kind == "broken-symlink":
        path.symlink_to(path.parent / "missing.sqlite3")
    elif kind == "bad-bytes":
        path.write_bytes(b"Synthetic damaged database.")
    elif kind == "wrong-schema":
        write_resource_rows_atomic(path, [{"id": "synthetic"}], pa.schema([pa.field("id", pa.string())]))
    else:
        if kind == "empty":
            rows = []
        elif kind == "duplicate-id":
            rows.append(dict(rows[0]))
        elif kind == "bad-observations":
            rows[0]["observations_json"] = "[]"
        elif kind == "deep-observations":
            rows[0]["observations_json"] = "[" * 10_100 + "]" * 10_100
        elif kind == "claims-complete":
            rows[0]["catalog_complete"] = True
        elif kind == "wrong-source-id":
            observations = json.loads(rows[0]["observations_json"])
            observations[0]["record"]["id"] = "Different"
            rows[0]["observations_json"] = json.dumps(observations)
        write_resource_rows_atomic(path, rows, COPILOT_LEGACY_CATALOG_SCHEMA)
    before = path.read_bytes() if kind != "broken-symlink" else None
    state = read_copilot_legacy_catalog(root)
    assert state["status"] == "invalid" and state["present"] is True
    assert state["sessions"] is None and state["conversation_ids"] == []
    assert state["catalog_complete"] is False
    assert (path.read_bytes() if kind != "broken-symlink" else None) == before


@pytest.mark.parametrize("part", ["manifest", "entity", "wire"])
def test_modified_evidence_is_rejected_before_directory_creation(tmp_path: Path, part: str) -> None:
    path, digest = _evidence(tmp_path / "evidence")
    target = path if part == "manifest" else path.parent / ("response-001.body.json" if part == "entity" else "response-001.wire-body.bin")
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ValueError, match="verification|hash"):
        build_copilot_legacy_catalog(path, expected_manifest_sha256=digest)


def test_history_response_is_not_misclassified_as_catalog(tmp_path: Path) -> None:
    pages = [{"results": [{"id": "message", "author": {"type": "human"}, "content": [{"type": "text", "text": "Synthetic body."}]}], "next": None}]
    path, digest = _evidence(tmp_path / "evidence", pages)
    with pytest.raises(ValueError, match="record structure"):
        build_copilot_legacy_catalog(path, expected_manifest_sha256=digest)
