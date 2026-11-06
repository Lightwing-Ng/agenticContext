"""Keep read-only archive evidence usable without weakening its boundaries.

Code version: v1.1.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from app.core import wechat_contact_analysis as contact_analysis
from app.core import wechat_mcp_archive as archive_queries
from app.core.wechat_archive import (
    _SCHEMA,
    _file_identity,
    wechat_history_partition_fingerprint,
)
from app.core.wechat_mcp_archive import (
    _archive_connection,
    _attach_sender_contacts,
    archive_order_key,
)
from tests.test_wechat_mcp_archive import _call, _service, build_wechat_store
from tests.test_wechat_forwarded_contact_analysis import (
    CID as FORWARDED_CONTACT_ID,
    OTHER_CID as OTHER_FORWARDED_CONTACT_ID,
    forwarded_activity_store,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database_digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _digest(path)
        for path in root.rglob("*.sqlite3")
    }


def _message_row(
    identifier: str,
    conversation: str,
    ordinal: int,
    *,
    timestamp: str = "2020-01-02T00:00:00+08:00",
    text: str = "Synthetic evidence",
    provenance: dict | None = None,
) -> tuple:
    return (
        identifier,
        conversation,
        ordinal,
        "wxid_ada",
        "Ada",
        "received",
        text,
        archive_order_key(timestamp) if timestamp else 0,
        timestamp,
        timestamp or "Unknown time",
        "UTC+08:00" if timestamp else "Unavailable source time",
        json.dumps(provenance or {"record_id": identifier}),
        json.dumps(["imported"] if timestamp else ["missing_time"]),
        text.casefold(),
    )


def _resident_resource_store(tmp_path: Path) -> dict:
    store = build_wechat_store(tmp_path / "store")
    owner = "wxid_resource_owner"
    source_id = "ios-" + hashlib.md5(
        owner.encode(), usedforsecurity=False
    ).hexdigest()
    account_key = "account:" + owner
    peer_id = "wxid_ada"
    with sqlite3.connect(store["history"]) as connection:
        connection.execute(
            "UPDATE partitions SET id=?,account_key=?,account_id=? WHERE id='src-test'",
            (source_id, account_key, owner),
        )
        connection.execute(
            "UPDATE conversations SET partition_id=?,account_key=?,peer_id=?",
            (source_id, account_key, peer_id),
        )
        session = connection.execute("SELECT * FROM conversations").fetchone()
        fingerprint = wechat_history_partition_fingerprint(connection)
    resource = store["history"].with_name("resource_references.sqlite3")
    identifier = hashlib.sha256(b"resident-resource").hexdigest()
    row = _message_row(
        identifier,
        store["conversation_id"],
        8,
        timestamp="2020-01-02T00:30:00+08:00",
        text="[Image] Retained resource evidence",
        provenance={
            "resource_reference": {
                "kind": "image",
                "account_id": owner,
                "peer_id": peer_id,
            }
        },
    )
    resource_session = list(session)
    resource_session[6:12] = [1, row[7], row[7], row[8], row[8], row[6]]
    with closing(sqlite3.connect(resource)) as connection, connection:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("history_partitions_sha256", fingerprint),
                ("history_file_identity", json.dumps(_file_identity(store["history"]))),
            ],
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            resource_session,
        )
        connection.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    return {**store, "resource": resource, "resource_message_id": identifier}


def test_resident_resource_timeline_remains_readable_without_database_writes(
    tmp_path: Path,
) -> None:
    store = _resident_resource_store(tmp_path)
    before = _database_digests(store["root"])
    service = _service(tmp_path, store["root"])
    page, body = _call(
        service,
        "wechat_search_messages",
        {"conversation_id": store["conversation_id"], "sort": "oldest"},
    )
    assert page["isError"] is False, body
    assert body["total_count"] == 6
    assert [item["text"] for item in body["messages"]] == [
        "one",
        "two",
        "[Image] Retained resource evidence",
        "side",
        "three",
        "undated",
    ]
    context, context_body = _call(
        service,
        "wechat_get_message_context",
        {"message_id": store["message_ids"]["two"], "before": 1, "after": 1},
    )
    assert context["isError"] is False, context_body
    assert [item["text"] for item in context_body["messages"]] == [
        "one",
        "two",
        "[Image] Retained resource evidence",
    ]
    assert _database_digests(store["root"]) == before


def test_resource_timeline_connection_still_refuses_main_database_writes(
    tmp_path: Path,
) -> None:
    store = _resident_resource_store(tmp_path)
    before = _database_digests(store["root"])
    with _archive_connection(store["root"]) as opened:
        connection = opened[0]
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM main.messages")
        assert connection.execute("SELECT COUNT(*) FROM main.messages").fetchone()[0] == 5
    assert _database_digests(store["root"]) == before


def _bind_test_senders(store: dict) -> None:
    with sqlite3.connect(store["contacts"]) as connection:
        for sender, contact in (("wxid_ada", store["ada_id"]), ("wxid_bey", store["bey_id"])):
            connection.execute(
                "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                ("src-test", sender, "", "", contact, "native_message_sender", "{}"),
            )


def _shared_parent_messages(store: dict, *, embedded: bool) -> list[dict]:
    parent = store["message_ids"]["one"]
    return [
        {
            "id": parent + "_reference_" + str(index),
            "parent_message_id": parent,
            "source_id": "src-test",
            "conversation_id": store["conversation_id"],
            "sender_id": sender,
            "sender_label": "Shared label",
            "is_embedded": embedded,
            "reference_kind": "html_forwarded" if embedded else "primary",
            "component_path": [index] if embedded else [],
        }
        for index, sender in enumerate(("wxid_ada", "wxid_bey"), start=1)
    ]


def test_shared_parent_reference_ids_do_not_override_each_sender(
    tmp_path: Path,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    _bind_test_senders(store)
    before = _database_digests(store["root"])
    messages = _shared_parent_messages(store, embedded=False)
    _attach_sender_contacts(store["root"], messages)
    assert [message["sender_contact"]["contact_id"] for message in messages] == [
        store["ada_id"],
        store["bey_id"],
    ]
    assert all(message["sender_contact"]["identity_match"] == "verified" for message in messages)
    assert _database_digests(store["root"]) == before


def test_embedded_sender_hints_do_not_inherit_unrelated_outer_sender_bindings(
    tmp_path: Path,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    _bind_test_senders(store)
    before = _database_digests(store["root"])
    messages = _shared_parent_messages(store, embedded=True)
    _attach_sender_contacts(store["root"], messages)
    for message in messages:
        contact = message["sender_contact"]
        assert contact["identity_match"] == "unresolved"
        assert contact["resolution_reason"] == "embedded_sender_requires_source_proof"
        assert contact.get("contact_id") not in {store["ada_id"], store["bey_id"]}
    assert _database_digests(store["root"]) == before


def test_verified_forwarded_authors_keep_source_proof_through_mcp_queries(
    tmp_path: Path,
) -> None:
    fixture = forwarded_activity_store.__wrapped__(tmp_path / "forwarded")
    expected = {}
    for contact_id in (FORWARDED_CONTACT_ID, OTHER_FORWARDED_CONTACT_ID):
        page = contact_analysis.query_contact_analysis(fixture["store"], contact_id)
        assert page["status"] == "ready"
        assert page["items"]
        expected[contact_id] = {item["id"] for item in page["items"]}
    before = _database_digests(fixture["store"])
    service = _service(tmp_path, fixture["store"])
    for contact_id, identifiers in expected.items():
        result, body = _call(
            service,
            "wechat_search_messages",
            {"contact_id": contact_id, "page_size": 20},
        )
        assert result["isError"] is False, body
        assert {message["id"] for message in body["messages"]} == identifiers
        for message in body["messages"]:
            assert message["is_embedded"] is True
            assert message["parent_message_id"] == fixture["request"]["id"]
            assert message["sender_contact"]["identity_match"] == "verified"
            assert message["sender_contact"]["contact_id"] == contact_id
    assert _database_digests(fixture["store"]) == before


def test_context_identity_uses_original_message_proof_before_public_text_truncation(
    tmp_path: Path,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    identifier = store["message_ids"]["one"]
    text = "Retained authored evidence " * 1_000
    with sqlite3.connect(store["history"]) as connection:
        connection.execute(
            "UPDATE messages SET text=?,search_text=?,sender_id='' WHERE id=?",
            (text, text.casefold(), identifier),
        )
    with sqlite3.connect(store["contacts"]) as connection:
        connection.execute(
            "INSERT INTO message_contact_bindings VALUES(?,?,?,?,?,?,?,?)",
            (
                "src-test",
                identifier,
                hashlib.sha256(text.encode()).hexdigest(),
                "2020-01-01T00:00:00+08:00",
                "one",
                store["ada_id"],
                "corroborated_message_sender",
                json.dumps({"method": "verified_native_message_evidence"}),
            ),
        )
    before = _database_digests(store["root"])
    result, body = _call(
        _service(tmp_path, store["root"]),
        "wechat_get_message_context",
        {"message_id": identifier, "before": 0, "after": 0},
    )
    assert result["isError"] is False, body
    message = body["messages"][0]
    assert message["text_truncated"] is True
    assert message["text"] == text[:20_000]
    assert message["sender_contact"]["identity_match"] == "verified"
    assert message["sender_contact"]["contact_id"] == store["ada_id"]
    assert _database_digests(store["root"]) == before


def test_archive_coverage_reports_published_time_bounds_and_undated_conversations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    undated = "wechat:" + hashlib.sha256(b"undated-conversation").hexdigest()
    with sqlite3.connect(store["history"]) as connection:
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                undated, "src-test", "source:src-test", "", "Unknown clock",
                "direct", 2, 0, 0, "", "", "undated",
            ),
        )
        for index in range(2):
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _message_row(
                    hashlib.sha256(f"undated-{index}".encode()).hexdigest(),
                    undated,
                    index,
                    timestamp="",
                ),
            )
    before = _database_digests(store["root"])
    real_connect = sqlite3.connect

    def connect_without_message_bodies(*args, **kwargs):
        connection = real_connect(*args, **kwargs)

        def guard(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and table == "messages" and column == "text":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(guard)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect_without_message_bodies)
    service = _service(tmp_path, store["root"])
    result, body = _call(service, "wechat_archive_status")
    assert result["isError"] is False, body
    coverage = body["coverage"]
    assert coverage["status"] == "ready"
    assert coverage["scope"] == "published_history"
    assert coverage["basis"] == "published_conversation_summaries"
    assert coverage["first_timestamp_value"] == "2020-01-01T00:00:00+08:00"
    assert coverage["last_timestamp_value"] == "2020-01-03T00:00:00+08:00"
    assert coverage["first_time_basis"] == "UTC+08:00"
    assert coverage["last_time_basis"] == "UTC+08:00"
    assert coverage["conversation_count"] == 2
    assert coverage["timed_conversation_count"] == 1
    assert coverage["undated_conversation_count"] == 1
    assert coverage["message_count"] == 7
    assert coverage["limitations"]
    assert "unknown_time_count" not in coverage
    assert "source_time_anomaly_count" not in coverage
    assert _database_digests(store["root"]) == before


def test_empty_published_archive_coverage_does_not_fabricate_time_bounds(
    tmp_path: Path,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    with sqlite3.connect(store["history"]) as connection:
        connection.execute("DELETE FROM messages")
        connection.execute("DELETE FROM conversations")
    before = _database_digests(store["root"])
    result, body = _call(_service(tmp_path, store["root"]), "wechat_archive_status")
    assert result["isError"] is False, body
    coverage = body["coverage"]
    assert coverage["status"] == "empty"
    assert coverage["conversation_count"] == 0
    assert coverage["message_count"] == 0
    assert not coverage["first_timestamp_value"]
    assert not coverage["last_timestamp_value"]
    assert _database_digests(store["root"]) == before


def test_coverage_retains_flagged_source_clock_instead_of_hiding_its_year(
    tmp_path: Path,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    timestamp = "0007-09-22T14:55:00+08:00"
    basis = "Source clock anomaly: raw year retained"
    with sqlite3.connect(store["history"]) as connection:
        connection.execute(
            "UPDATE messages SET time_sort=?,timestamp_value=?,time_basis=?,"
            "quality_flags=? WHERE id=?",
            (
                archive_order_key(timestamp),
                timestamp,
                basis,
                json.dumps(["source_clock_anomaly"]),
                store["message_ids"]["one"],
            ),
        )
        connection.execute(
            "UPDATE conversations SET first_sort=?,first_time=?",
            (archive_order_key(timestamp), timestamp),
        )
    before = _database_digests(store["root"])
    result, body = _call(_service(tmp_path, store["root"]), "wechat_archive_status")
    assert result["isError"] is False, body
    coverage = body["coverage"]
    assert coverage["first_timestamp_value"] == timestamp
    assert coverage["first_time_basis"] == basis
    assert coverage["first_quality_flags"] == ["source_clock_anomaly"]
    assert coverage["timed_conversation_count"] == 1
    assert coverage["undated_conversation_count"] == 0
    assert _database_digests(store["root"]) == before


def test_context_has_deterministic_ties_unknown_times_and_bounded_neighbors(
    tmp_path: Path,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    rows = [
        _message_row(
            hashlib.sha256(f"context-{index}".encode()).hexdigest(),
            store["conversation_id"],
            index // 2,
            timestamp="2020-01-02T00:00:00+08:00" if index < 55 else "",
            text=f"Evidence {index}",
        )
        for index in range(60)
    ]
    with sqlite3.connect(store["history"]) as connection:
        connection.execute("DELETE FROM messages")
        connection.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    ordered = sorted(rows, key=lambda row: (row[7] == 0, row[7], row[2], row[0]))
    before = _database_digests(store["root"])
    service = _service(tmp_path, store["root"])
    result, body = _call(
        service,
        "wechat_get_message_context",
        {"message_id": ordered[27][0], "before": 20, "after": 20},
    )
    assert result["isError"] is False, body
    assert [item["id"] for item in body["messages"]] == [row[0] for row in ordered[7:48]]
    assert body["before"] == body["after"] == body["target_index"] == 20
    assert len(body["messages"]) == 41
    unknown_result, unknown_body = _call(
        service,
        "wechat_get_message_context",
        {"message_id": ordered[54][0], "before": 2, "after": 3},
    )
    assert unknown_result["isError"] is False, unknown_body
    assert [item["id"] for item in unknown_body["messages"]] == [row[0] for row in ordered[52:58]]
    assert all(item["timestamp_value"] == "" for item in unknown_body["messages"][-3:])
    assert _database_digests(store["root"]) == before


@pytest.mark.parametrize("argument", [{"before": 21}, {"after": 21}])
def test_context_refuses_requests_beyond_its_neighbor_limit(
    tmp_path: Path, argument: dict,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    result, body = _call(
        _service(tmp_path, store["root"]),
        "wechat_get_message_context",
        {"message_id": store["message_ids"]["one"], **argument},
    )
    assert result["isError"] is True
    assert body["code"] == "invalid_arguments"


def test_context_rejects_path_arguments_and_linked_resource_index(tmp_path: Path) -> None:
    store = _resident_resource_store(tmp_path)
    service = _service(tmp_path, store["root"])
    before = _database_digests(store["root"])
    result, body = _call(
        service,
        "wechat_get_message_context",
        {
            "message_id": store["message_ids"]["one"],
            "archive_path": "../../private/history.sqlite3",
        },
    )
    assert result["isError"] is True
    assert body["code"] == "invalid_arguments"
    assert _database_digests(store["root"]) == before
    preserved = store["resource"].with_name("preserved-resource.sqlite3")
    store["resource"].rename(preserved)
    store["resource"].symlink_to(preserved)
    linked, linked_body = _call(
        service,
        "wechat_get_message_context",
        {"message_id": store["message_ids"]["one"]},
    )
    assert linked["isError"] is True
    assert linked_body["code"] == "archive_refused"
    assert _digest(preserved) == before["messaging/wechat/resource_references.sqlite3"]


@pytest.mark.parametrize("changed", ["history", "contacts", "display_names"])
def test_contact_search_refuses_a_publication_changed_during_the_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    changed_path = (
        store["history"].with_name("display_names.json")
        if changed == "display_names"
        else store[changed]
    )
    before = _database_digests(store["root"])
    matching_evidence = archive_queries._matching_evidence
    changed_once = False

    def change_publication(*args, **kwargs):
        nonlocal changed_once
        result = matching_evidence(*args, **kwargs)
        if not changed_once:
            stat = changed_path.stat()
            os.utime(changed_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
            changed_once = True
        return result

    monkeypatch.setattr(archive_queries, "_matching_evidence", change_publication)
    result, body = _call(
        _service(tmp_path, store["root"]), "wechat_find_contacts", {"query": "Ada"},
    )
    assert changed_once
    assert result["isError"] is True
    assert body["code"] == "source_changed"
    assert "contacts" not in body
    assert _database_digests(store["root"]) == before


@pytest.mark.parametrize("database", ["history", "contacts"])
@pytest.mark.parametrize("link_kind", ["hardlink", "symlink"])
def test_contact_scoped_search_revalidates_required_database_boundaries(
    tmp_path: Path, database: str, link_kind: str,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    path = store[database]
    digest = _digest(path)
    preserved = path.with_name("preserved-" + path.name)
    path.rename(preserved)
    if link_kind == "hardlink":
        path.hardlink_to(preserved)
    else:
        path.symlink_to(preserved)
    result, body = _call(
        _service(tmp_path, store["root"]),
        "wechat_search_messages",
        {"contact_id": store["ada_id"]},
    )
    assert result["isError"] is True
    assert body["code"] == "archive_refused"
    assert "messages" not in body
    assert _digest(preserved) == digest


def test_interrupted_query_returns_an_explicit_timeout_instead_of_empty_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = build_wechat_store(tmp_path / "store")
    before = _database_digests(store["root"])
    readonly = archive_queries._readonly

    def interrupted_readonly(path):
        connection = readonly(path)
        connection.set_progress_handler(lambda: 1, 1)
        return connection

    monkeypatch.setattr(archive_queries, "_readonly", interrupted_readonly)
    result, body = _call(_service(tmp_path, store["root"]), "wechat_archive_status")
    assert result["isError"] is True
    assert body["code"] == "query_timeout"
    assert "messages" not in body
    assert "message_count" not in body
    assert _database_digests(store["root"]) == before
