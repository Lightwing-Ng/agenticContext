"""MCP message provenance follows verified snapshots without rewriting partitions.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.core.wechat_contact_analysis import _sources, contact_analysis_cache_path
from app.core.wechat_mcp_archive import _snapshot_source_rows
from tests.test_wechat_mcp_archive import _call, _service, build_wechat_store


def _snapshot_store(tmp_path: Path) -> dict:
    store = build_wechat_store(tmp_path / "store")
    owner = "wxid_snapshot_owner"
    account_hash = hashlib.md5(owner.encode(), usedforsecurity=False).hexdigest()
    source_id = "ios-" + account_hash
    account_key = "account:" + owner
    first = {
        "source_id": source_id,
        "source_account_id": owner,
        "account_hash": account_hash,
        "source_label": "iPad archive",
        "source_file": "iPad backup · 7 Nov 2026",
    }
    second = {
        **first,
        "source_label": "iPhone archive",
        "source_file": "iPhone backup · 6 Nov 2026",
    }
    with sqlite3.connect(store["history"]) as connection:
        connection.execute(
            "UPDATE partitions SET id=?,account_key=?,account_id=?,"
            "source_label='iPhone archive',source_file='Finder backup · 30 Oct 2026'",
            (source_id, account_key, owner),
        )
        connection.execute(
            "UPDATE conversations SET partition_id=?,account_key=?",
            (source_id, account_key),
        )
        connection.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
            "source_id TEXT,metadata TEXT)"
        )
        connection.executemany(
            "INSERT INTO source_snapshots VALUES(?,?,?,?)",
            [
                (53, "snapshot-ipad", source_id, json.dumps(first)),
                (54, "snapshot-iphone", source_id, json.dumps(second)),
            ],
        )
        for label, snapshot in (("two", 53), ("side", 54), ("three", 54)):
            identifier = store["message_ids"][label]
            proof = json.loads(connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (identifier,),
            ).fetchone()[0])
            proof["source_snapshot"] = snapshot
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(proof), identifier),
            )
    cache = contact_analysis_cache_path(store["root"], store["ada_id"])
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "UPDATE refs SET source_id=?,account_key=?", (source_id, account_key),
        )
    _refresh_cache(store)
    return {**store, "owner": owner, "source_id": source_id, "snapshot": first}


def _refresh_cache(store: dict) -> None:
    cache = contact_analysis_cache_path(store["root"], store["ada_id"])
    with sqlite3.connect(cache) as connection:
        connection.execute(
            "UPDATE info SET value=? WHERE key='fingerprint'", (_sources(store["root"])[2],),
        )


def _database_digests(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*.sqlite3")
    }


def test_context_neighbors_search_and_contact_references_use_each_snapshot(tmp_path: Path):
    store = _snapshot_store(tmp_path)
    service = _service(tmp_path, store["root"])
    before = _database_digests(store["root"])
    result, context = _call(service, "wechat_get_message_context", {
        "message_id": store["message_ids"]["two"], "before": 1, "after": 2,
    })
    assert result["isError"] is False, context
    assert [item["source_file"] for item in context["messages"]] == [
        "Finder backup · 30 Oct 2026", "iPad backup · 7 Nov 2026",
        "iPhone backup · 6 Nov 2026", "iPhone backup · 6 Nov 2026",
    ]
    for scope, expected in (
        ({"conversation_id": store["conversation_id"]}, {
            "one": "Finder backup · 30 Oct 2026", "two": "iPad backup · 7 Nov 2026",
            "side": "iPhone backup · 6 Nov 2026", "three": "iPhone backup · 6 Nov 2026",
            "undated": "Finder backup · 30 Oct 2026",
        }),
        ({"contact_id": store["ada_id"]}, {
            "one": "Finder backup · 30 Oct 2026", "two": "iPad backup · 7 Nov 2026",
            "three": "iPhone backup · 6 Nov 2026", "undated": "Finder backup · 30 Oct 2026",
        }),
    ):
        result, page = _call(service, "wechat_search_messages", {**scope, "sort": "oldest"})
        assert result["isError"] is False, page
        assert {item["text"]: item["source_file"] for item in page["messages"]} == expected
        ipad = next(item for item in page["messages"] if item["text"] == "two")
        assert ipad["source_label"] == "iPad archive"
        assert ipad["provenance"]["source_snapshot"] == 53
    with sqlite3.connect(store["history"]) as connection:
        assert connection.execute("SELECT source_label,source_file FROM partitions").fetchone() == (
            "iPhone archive", "Finder backup · 30 Oct 2026",
        )
    assert _database_digests(store["root"]) == before


@pytest.mark.parametrize("invalid", [
    "foreign_snapshot_source", "foreign_metadata_source", "foreign_owner",
    "foreign_account_key", "foreign_hash", "invalid_json", "non_object", "missing_snapshot",
])
def test_invalid_snapshot_keeps_partition_source_in_every_mcp_path(tmp_path: Path, invalid: str):
    store = _snapshot_store(tmp_path)
    metadata = dict(store["snapshot"])
    snapshot_source = store["source_id"]
    if invalid == "foreign_snapshot_source":
        snapshot_source = "foreign-source"
    elif invalid == "foreign_metadata_source":
        metadata["source_id"] = "foreign-source"
    elif invalid == "foreign_owner":
        metadata["source_account_id"] = "wxid_someone_else"
    elif invalid == "foreign_account_key":
        metadata["account_key"] = "account:wxid_someone_else"
    elif invalid == "foreign_hash":
        metadata["account_hash"] = "0" * 32
    raw_metadata = (
        "{" if invalid == "invalid_json" else
        "[]" if invalid == "non_object" else json.dumps(metadata)
    )
    with sqlite3.connect(store["history"]) as connection:
        connection.execute(
            "UPDATE source_snapshots SET source_id=?,metadata=? WHERE id=53",
            (snapshot_source, raw_metadata),
        )
        if invalid == "missing_snapshot":
            connection.execute("DELETE FROM source_snapshots WHERE id=53")
    _refresh_cache(store)
    before = _database_digests(store["root"])
    service = _service(tmp_path, store["root"])
    for tool, arguments in (
        ("wechat_get_message_context", {"message_id": store["message_ids"]["two"]}),
        ("wechat_search_messages", {"conversation_id": store["conversation_id"], "query": "two"}),
        ("wechat_search_messages", {"contact_id": store["ada_id"], "query": "two"}),
    ):
        result, body = _call(service, tool, arguments)
        assert result["isError"] is False, body
        target = next(item for item in body["messages"] if item["text"] == "two")
        assert target["source_label"] == "iPhone archive"
        assert target["source_file"] == "Finder backup · 30 Oct 2026"
        assert target["provenance"]["source_snapshot"] == 53
    assert _database_digests(store["root"]) == before


def test_snapshot_sources_are_batched_and_preserve_legacy_rows():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
        "source_id TEXT,metadata TEXT)"
    )
    connection.executemany("INSERT INTO source_snapshots VALUES(?,?,?,?)", [
        (index, str(index), "src-test", json.dumps({"source_file": f"Snapshot {index}"}))
        for index in range(1, 502)
    ])
    rows = [
        {"source_id": "src-test", "source_file": "legacy.xlsx", "account_id": "",
         "provenance": json.dumps({"source_snapshot": index})}
        for index in range(1, 502)
    ]
    rows.append({"source_id": "src-test", "source_file": "legacy.xlsx", "provenance": "{}"})
    statements = []
    connection.set_trace_callback(statements.append)
    projected = _snapshot_source_rows(connection, rows)
    assert projected[0]["source_file"] == "Snapshot 1"
    assert projected[-2]["source_file"] == "Snapshot 501"
    assert projected[-1]["source_file"] == "legacy.xlsx"
    assert rows[0]["source_file"] == "legacy.xlsx"
    assert len([sql for sql in statements if "FROM main.source_snapshots" in sql]) == 2
    connection.close()
