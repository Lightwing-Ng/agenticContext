"""Additive references remain in the scoped, paginated WeChat timeline.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.wechat_archive import (
    WechatArchiveRepository,
    _SCHEMA,
    _file_identity,
    _identifier,
    import_wechat_archive,
    wechat_archive_path,
    wechat_history_partition_fingerprint,
)
from tests.test_wechat_archive import make_archive


@pytest.fixture
def store(tmp_path):
    archive = make_archive(tmp_path / "archive", repeated=3)
    root = tmp_path / "store"
    import_wechat_archive(archive, root)
    return root


def _session(store, owner="wxid_account_a", kind="direct"):
    return next(
        row
        for row in WechatArchiveRepository(store).query(account="account:" + owner)[
            "conversations"
        ]
        if row["kind"] == kind
    )


def _reference(session, name, time, *, identifier=None):
    identifier = identifier or hashlib.sha256(name.encode()).hexdigest()
    owner = session["account_key"].removeprefix("account:")
    return (
        identifier,
        session["id"],
        time,
        None,
        "Unknown sender",
        "received",
        name,
        time,
        f"2021-08-07T19:00:{time:02d}+08:00",
        f"7 Aug 2021 19:00:{time:02d}",
        "Native source time",
        json.dumps(
            {
                "message_type": 3,
                "resource_reference": {
                    "kind": "image",
                    "label": name,
                    "account_id": owner,
                    "peer_id": session["peer_id"],
                },
            }
        ),
        "[]",
        name.casefold(),
    )


def _sidecar(store, entries):
    history = wechat_archive_path(store)
    path = history.with_name("resource_references.sqlite3")
    with sqlite3.connect(history) as primary:
        fingerprint = wechat_history_partition_fingerprint(primary)
    groups = {}
    for session, message in entries:
        groups.setdefault(session["id"], [session, []])[1].append(message)
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("history_partitions_sha256", fingerprint),
                ("history_file_identity", json.dumps(_file_identity(history))),
            ],
        )
        for session, messages in groups.values():
            first = min(messages, key=lambda row: row[7])
            last = max(messages, key=lambda row: row[7])
            connection.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session["id"],
                    session["partition_id"],
                    session["account_key"],
                    session["peer_id"],
                    session["title"],
                    session["kind"],
                    len(messages),
                    first[7],
                    last[7],
                    first[8],
                    last[8],
                    last[6],
                ),
            )
            connection.executemany(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", messages
            )
    return path


def _space_text_records(store, session):
    with sqlite3.connect(wechat_archive_path(store)) as connection:
        ids = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal",
                (session["id"],),
            )
        ]
        for identifier, time in zip(ids, (10, 30, 50), strict=True):
            connection.execute(
                "UPDATE messages SET time_sort=?,text=?,search_text=? WHERE id=?",
                (time, f"Text {time}", f"text {time}", identifier),
            )
        connection.execute(
            "UPDATE conversations SET first_sort=10,last_sort=50,latest_text='Text 50' WHERE id=?",
            (session["id"],),
        )
    return ids


def test_resources_share_order_pagination_counts_and_text_export(store):
    session = _session(store)
    _space_text_records(store, session)
    references = [_reference(session, f"Image {time}", time) for time in (20, 40)]
    resource_path = _sidecar(store, [(session, row) for row in references])
    history = wechat_archive_path(store)
    before = history.read_bytes(), resource_path.read_bytes()
    repository = WechatArchiveRepository(store)
    pages = [
        repository.query(session=session["id"], page=page, page_size=2)
        for page in (1, 2, 3)
    ]
    assert all(page["resource_index_status"] == "ready" for page in pages)
    assert [[row["text"] for row in page["messages"]] for page in pages] == [
        ["Text 10", "Image 20"],
        ["Text 30", "Image 40"],
        ["Text 50"],
    ]
    assert {page["count"] for page in pages} == {5}
    assert {page["message_count"] for page in pages} == {5}
    assert {page["pages"] for page in pages} == {3}
    index = repository.query(account=session["account_key"])
    selected = next(row for row in index["conversations"] if row["id"] == session["id"])
    assert selected["message_count"] == 5
    assert index["message_count"] == 8
    page = query_chat_history(store, source="wechat", session=session["id"])
    exported = build_chat_history_markdown(page)
    assert "Image 20" in exported and "Image 40" in exported
    assert "- Messages: 5\n" in exported
    assert (history.read_bytes(), resource_path.read_bytes()) == before


def test_resource_only_conversation_search_and_account_scope(store):
    session = _session(store)
    only = {
        **session,
        "peer_id": "native-resource-only@chatroom",
        "kind": "group",
        "title": "Resource room",
    }
    only["id"] = "wechat:" + _identifier(only["partition_id"], only["peer_id"])
    other = _session(store, "wxid_account_b")
    _sidecar(
        store,
        [
            (only, _reference(only, "Unique attachment", 25)),
            (other, _reference(other, "Other account attachment", 35)),
        ],
    )
    repository = WechatArchiveRepository(store)
    found = repository.query(account=session["account_key"], query="unique attachment")
    assert found["resource_index_status"] == "ready"
    assert [row["id"] for row in found["conversations"]] == [only["id"]]
    assert found["message_count"] == 1
    scoped = repository.query(account=other["account_key"], session=only["id"])
    assert scoped["messages"] == []
    assert scoped["current"] is None
    detail = repository.query(
        account=session["account_key"], kind="group", session=only["id"]
    )
    assert detail["count"] == 1
    assert detail["current"]["kind"] == "group"


@pytest.mark.parametrize("conflicting_table", [False, True])
def test_unknown_peers_retain_only_their_exact_native_table_bucket(
    store, conflicting_table
):
    session = _session(store)
    native_hash = "a" * 32
    unknown = {
        **session,
        "peer_id": "",
        "kind": "unclassified",
        "title": "Unidentified resource room",
        "id": "wechat:" + _identifier(session["partition_id"], native_hash),
    }
    entries = []
    for index in range(2):
        message = list(_reference(unknown, f"Image {index}", 20 + index))
        provenance = json.loads(message[11])
        table_hash = "b" * 32 if conflicting_table and index else native_hash
        provenance["locator"] = {"table": "Chat_" + table_hash, "local_id": index + 1}
        message[11] = json.dumps(provenance)
        entries.append((unknown, tuple(message)))
    _sidecar(store, entries)
    page = WechatArchiveRepository(store).query(session=unknown["id"])
    assert page["resource_index_status"] == (
        "invalid" if conflicting_table else "ready"
    )
    if conflicting_table:
        assert page["current"] is None
    else:
        assert page["count"] == 2
        assert page["current"]["peer_id"] == ""
        assert page["current"]["kind"] == "unclassified"


def test_duplicate_ids_do_not_inflate_counts_or_resource_reveal(store):
    session = _session(store)
    text_ids = _space_text_records(store, session)
    duplicate = _reference(session, "Duplicate placeholder", 5, identifier=text_ids[0])
    actual = _reference(session, "Actual image", 40)
    _sidecar(store, [(session, duplicate), (session, actual)])
    repository = WechatArchiveRepository(store)
    page = repository.query(session=session["id"])
    assert page["resource_index_status"] == "ready"
    assert page["count"] == page["current"]["message_count"] == 4
    assert [row["text"] for row in page["messages"]] == [
        "Text 10",
        "Text 30",
        "Actual image",
        "Text 50",
    ]
    assert page["current"]["first_sort"] == 10
    assert repository.resource_message(text_ids[0]) is None
    resource = repository.resource_message(actual[0])
    assert resource["account_id"] == "wxid_account_a"
    assert resource["peer_id"] == session["peer_id"]
    assert repository.resource_message("../../unsafe") is None


@pytest.mark.parametrize(
    "change",
    ["history", "fingerprint", "schema", "provenance", "account", "flags", "identity"],
)
def test_stale_or_invalid_sidecar_leaves_primary_text_available(store, change):
    session = _session(store)
    resource = _reference(session, "Image reference", 20)
    path = _sidecar(store, [(session, resource)])
    if change == "history":
        with sqlite3.connect(wechat_archive_path(store)) as connection:
            connection.execute(
                "UPDATE partitions SET account_label='Reindexed label' WHERE id=?",
                (session["partition_id"],),
            )
    else:
        with sqlite3.connect(path) as connection:
            if change == "fingerprint":
                connection.execute(
                    "UPDATE archive_info SET value=? WHERE key='history_partitions_sha256'",
                    ("0" * 64,),
                )
            elif change == "schema":
                connection.execute("ALTER TABLE messages ADD COLUMN unexpected TEXT")
            elif change == "provenance":
                connection.execute("UPDATE messages SET provenance='invalid json'")
            elif change == "flags":
                connection.execute("UPDATE messages SET quality_flags='invalid json'")
            elif change == "identity":
                connection.execute(
                    "DELETE FROM archive_info WHERE key='history_file_identity'"
                )
            else:
                connection.execute(
                    "UPDATE conversations SET account_key='account:other'"
                )
    repository = WechatArchiveRepository(store)
    page = repository.query(session=session["id"])
    assert page["resource_index_status"] == (
        "stale" if change in {"history", "fingerprint"} else "invalid"
    )
    assert len(page["messages"]) == session["message_count"]
    assert repository.resource_message(resource[0]) is None
    assert path.exists()


def test_absent_or_unreadable_sidecar_never_blocks_text(store):
    session = _session(store)
    repository = WechatArchiveRepository(store)
    assert repository.query(session=session["id"])["resource_index_status"] == "absent"
    path = wechat_archive_path(store).with_name("resource_references.sqlite3")
    path.write_text("not a SQLite database")
    page = repository.query(session=session["id"])
    assert page["resource_index_status"] == "invalid"
    assert len(page["messages"]) == session["message_count"]


def test_optional_resource_coverage_reports_only_known_native_accounts(store):
    session = _session(store)
    repository = WechatArchiveRepository(store)
    assert repository.query()["resource_index_accounts"] is None
    path = _sidecar(store, [(session, _reference(session, "Image reference", 20))])
    assert repository.query()["resource_index_accounts"] is None
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO archive_info VALUES('report',?)",
            (
                json.dumps(
                    {
                        "accounts": [
                            {"account_id": "wxid_account_b", "messages": 0},
                            {"account_id": "wxid_account_a", "messages": 1},
                        ],
                    }
                ),
            ),
        )
    page = repository.query(session=session["id"])
    assert page["resource_index_status"] == "ready"
    assert page["resource_index_accounts"] == ("wxid_account_a", "wxid_account_b")
    with sqlite3.connect(wechat_archive_path(store)) as connection:
        connection.execute(
            "UPDATE partitions SET account_label='Changed label' WHERE id=?",
            (session["partition_id"],),
        )
    page = repository.query(session=session["id"])
    assert page["resource_index_status"] == "stale"
    assert page["resource_index_accounts"] is None


@pytest.mark.parametrize(
    "report",
    [
        "not-json",
        [],
        {},
        {"accounts": "wrong"},
        {"accounts": ["wxid_account_a"]},
        {"accounts": [{"account_id": "wxid_unknown"}]},
        {
            "accounts": [
                {"account_id": "wxid_account_a"},
                {"account_id": "wxid_account_a"},
            ]
        },
    ],
)
def test_malformed_optional_coverage_does_not_invalidate_otherwise_ready_index(
    store, report
):
    session = _session(store)
    path = _sidecar(store, [(session, _reference(session, "Image reference", 20))])
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO archive_info VALUES('report',?)",
            (report if isinstance(report, str) else json.dumps(report),),
        )
    page = WechatArchiveRepository(store).query(session=session["id"])
    assert page["resource_index_status"] == "ready"
    assert page["resource_index_accounts"] is None
    assert page["count"] == session["message_count"] + 1


def test_validation_is_cached_for_a_pinned_pair_of_immutable_files(store, monkeypatch):
    session = _session(store)
    _sidecar(store, [(session, _reference(session, "Image reference", 20))])
    calls = []
    original = WechatArchiveRepository._validate_resource_conversations

    def counted(connection):
        calls.append(True)
        return original(connection)

    monkeypatch.setattr(
        WechatArchiveRepository,
        "_validate_resource_conversations",
        staticmethod(counted),
    )
    repository = WechatArchiveRepository(store)
    assert repository.query(session=session["id"])["resource_index_status"] == "ready"
    assert (
        repository.query(account=session["account_key"])["resource_index_status"]
        == "ready"
    )
    assert len(calls) == 1
