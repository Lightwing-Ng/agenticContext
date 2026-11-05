"""Published media metadata extends every verified account's complete timeline.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.wechat_archive import WechatArchiveRepository, _file_identity, _identifier
from app.core.wechat_contact_analysis import message_source_location
from app.core.wechat_media_event_index import (
    MEDIA_EVENT_SCHEMA,
    VERSION,
    attach_media_event_index,
    locate_media_event,
)
from app.core.wechat_media_placeholders import _project, media_placeholder_id
from app.core.wechat_media_placeholders import attach_media_placeholders
from app.core.wechat_session_export import (
    WechatSessionExport,
    WechatSessionSourceChanged,
)
from app.web.wechat_session_csv import session_csv_chunks
from tests import test_wechat_media_placeholders as original


placeholder_store = original.placeholder_store
MEDIA_PER_CONVERSATION = 720


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _insert(connection, table, row):
    connection.execute(
        "INSERT INTO "
        + table
        + "("
        + ",".join(row)
        + ") VALUES("
        + ",".join("?" for _ in row)
        + ")",
        tuple(row.values()),
    )


def _global_fixture(fixture):
    """Prepare two source owners and three separately audited conversation members."""
    owners = (original.OWNER, original.OWNER, "wxid_global_second_owner")
    archives = (
        original.ARCHIVE_SHA,
        original.ARCHIVE_SHA,
        _hash("Second immutable HTML archive"),
    )
    members = (original.MEMBER, "Synthetic/Second.html", "Synthetic/Third.html")
    peers = (original.PEER, "wxid_second_peer", "wxid_third_peer")
    specs, metadata_by_snapshot = [], {}
    for number in range(3):
        archive = archives[number]
        source = "html-" + archive[:24]
        member = members[number]
        member_sha = original.MEMBER_SHA if number == 0 else _hash(member)
        snapshot = 1 if number < 2 else 2
        spec = SimpleNamespace(
            owner=owners[number],
            archive=archive,
            source=source,
            member=member,
            member_sha=member_sha,
            snapshot=snapshot,
            peer=peers[number],
            conversation="wechat:" + _identifier(source, member),
            entries=[],
            rows=[],
            audit_sha=_hash("Complete global media audit " + str(number)),
        )
        metadata = metadata_by_snapshot.setdefault(
            snapshot,
            {
                **deepcopy(fixture.metadata),
                "source_id": source,
                "source_sha256": archive,
                "source_account_id": spec.owner,
                "owner_evidence": {"native_username": spec.owner},
                "selected_text_members": [],
                "source_conversation_catalog": [],
            },
        )
        metadata["selected_text_members"].append(
            {
                "member_path": member,
                "sha256": member_sha,
                "size": 4096,
            }
        )
        metadata["source_conversation_catalog"].append(
            {
                "member_path": member,
                "index_avatar_native_id": spec.peer,
                "conversation_kind": "direct",
                "reviewed_resolution": {"main_html_sha256": member_sha},
                "counts": {
                    "top_level_nodes": 740,
                    "all_message_nodes": 740,
                    "retained_top_level_text": 18,
                    "deferred_top_level": MEDIA_PER_CONVERSATION,
                },
                "declared_async_counts": {"numberOfPages": 0},
            }
        )
        for ordinal in range(21, 21 + MEDIA_PER_CONVERSATION):
            entry = deepcopy(original._entry(ordinal))
            entry.update(
                source_snapshot_id=snapshot,
                source_id=source,
                archive_sha256=archive,
                member_sha256=member_sha,
                audit_sha256=spec.audit_sha,
                conversation_id=spec.conversation,
            )
            node = entry["node"]
            node["source_locator"]["member_path"] = member
            node.update(
                raw_timestamp=(
                    datetime(2023, 12, 29, 13) + timedelta(seconds=ordinal)
                ).isoformat(" "),
                sender_native_id=spec.owner,
                avatar_path=f"Chat_files/Portrait/{spec.owner}.jpg",
            )
            spec.entries.append(entry)
        specs.append(spec)

    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        initial_conversation = dict(
            connection.execute("SELECT * FROM conversations").fetchone()
        )
        initial_partition = dict(
            connection.execute("SELECT * FROM partitions").fetchone()
        )
        initial_text = [
            dict(row)
            for row in connection.execute("SELECT * FROM messages ORDER BY ordinal")
        ]
        for snapshot, metadata in metadata_by_snapshot.items():
            raw = json.dumps(metadata)
            spec = next(item for item in specs if item.snapshot == snapshot)
            if snapshot == 1:
                connection.execute(
                    "UPDATE source_snapshots SET metadata=? WHERE id=1", (raw,)
                )
            else:
                connection.execute(
                    "INSERT INTO source_snapshots VALUES(?,?,?,?)",
                    (snapshot, spec.archive, spec.source, raw),
                )
            for item in specs:
                if item.snapshot == snapshot:
                    item.metadata = raw
        for number, spec in enumerate(specs):
            if number == 2:
                _insert(
                    connection,
                    "partitions",
                    {
                        **initial_partition,
                        "id": spec.source,
                        "account_key": "account:" + spec.owner,
                        "account_id": spec.owner,
                        "account_label": "Second synthetic owner",
                    },
                )
            if number:
                _insert(
                    connection,
                    "conversations",
                    {
                        **initial_conversation,
                        "id": spec.conversation,
                        "partition_id": spec.source,
                        "account_key": "account:" + spec.owner,
                        "peer_id": spec.peer,
                        "title": "Global synthetic conversation " + str(number),
                    },
                )
                for text in initial_text:
                    row = deepcopy(text)
                    proof = json.loads(row["provenance"])
                    proof.update(
                        source_snapshot=spec.snapshot,
                        sender_native_id=spec.owner,
                        record_id="global-text-"
                        + str(number)
                        + "-"
                        + str(row["ordinal"]),
                    )
                    proof["locator"].update(
                        member_path=spec.member,
                        archive_sha256=spec.archive,
                        member={
                            "member_path": spec.member,
                            "sha256": spec.member_sha,
                            "size": 4096,
                        },
                    )
                    row.update(
                        id=_identifier(spec.source, proof["record_id"]),
                        conversation_id=spec.conversation,
                        sender_id=spec.owner,
                        provenance=json.dumps(proof),
                    )
                    _insert(connection, "messages", row)
        cache = {}
        for spec in specs:
            spec.rows = [_project(connection, entry, cache) for entry in spec.entries]
    fixture.overlay.write_text(
        json.dumps({"schema_version": 1, "entries": specs[0].entries[:56]})
    )
    return specs


@pytest.fixture
def global_media_store(placeholder_store):
    fixture = placeholder_store
    specs = _global_fixture(fixture)
    index_path = fixture.history.with_name("media_events.sqlite3")
    with (
        closing(sqlite3.connect(fixture.history)) as source,
        source,
        closing(sqlite3.connect(index_path)) as index,
        index,
    ):
        source.row_factory = sqlite3.Row
        index.executescript(MEDIA_EVENT_SCHEMA)
        info = {
            "schema_version": "1",
            "proof_version": VERSION,
            "published": "1",
            "primary_disjoint": "1",
            "history_file_identity": json.dumps(_file_identity(fixture.history)),
        }
        index.executemany("INSERT INTO archive_info VALUES(?,?)", info.items())
        for spec in specs:
            index.execute(
                "INSERT OR IGNORE INTO sources VALUES(?,?,?,?,?)",
                (
                    spec.snapshot,
                    spec.archive,
                    spec.source,
                    spec.archive,
                    _hash(spec.metadata),
                ),
            )
            index.execute(
                "INSERT INTO conversation_proofs VALUES(?,?,?,?,?)",
                (
                    spec.conversation,
                    spec.snapshot,
                    spec.member,
                    spec.member_sha,
                    spec.audit_sha,
                ),
            )
            index.executemany(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", spec.rows
            )
            conversation = dict(
                source.execute(
                    "SELECT * FROM conversations WHERE id=?", (spec.conversation,)
                ).fetchone()
            )
            first, last = spec.rows[0], spec.rows[-1]
            conversation.update(
                message_count=MEDIA_PER_CONVERSATION,
                first_sort=first[7],
                last_sort=last[7],
                first_time=first[8],
                last_time=last[8],
                latest_text=last[6],
            )
            _insert(index, "conversations", conversation)
    return SimpleNamespace(
        **vars(fixture),
        specs=specs,
        index=index_path,
        before=(
            fixture.history.read_bytes(),
            fixture.overlay.read_bytes(),
            index_path.read_bytes(),
        ),
    )


def _attach(fixture, *, conversation=None, legacy=True):
    connection = sqlite3.connect(
        fixture.history.as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=MEMORY")
    tables = ("main.conversations", "main.messages")
    if legacy:
        *tables, status = attach_media_placeholders(
            connection,
            fixture.history,
            *tables,
            conversation_id=conversation,
        )
        assert status in {"ready", "absent"}
    return connection, attach_media_event_index(
        connection,
        fixture.history,
        *tables,
        conversation_id=conversation,
    )


def _unchanged(fixture):
    assert fixture.before == (
        fixture.history.read_bytes(),
        fixture.overlay.read_bytes(),
        fixture.index.read_bytes(),
    )


def test_global_metadata_covers_two_accounts_and_every_conversation_without_json_cap(
    global_media_store,
):
    fixture = global_media_store
    connection, (conversations, messages, status) = _attach(fixture)
    try:
        assert status == "ready"
        assert (
            connection.execute(f"SELECT COUNT(*) FROM {messages}").fetchone()[0] == 2214
        )
        counts = dict(
            connection.execute(f"SELECT id,message_count FROM {conversations}")
        )
        assert counts == {spec.conversation: 738 for spec in fixture.specs}
        assert (
            connection.execute(f"SELECT COUNT(DISTINCT id) FROM {messages}").fetchone()[
                0
            ]
            == 2214
        )
        assert (
            connection.execute(
                f"SELECT COUNT(*) FROM {messages} WHERE text='[Image]'"
            ).fetchone()[0]
            == 2160
        )
        accounts = dict(
            connection.execute(
                f"SELECT account_key,SUM(message_count) FROM {conversations} GROUP BY account_key"
            )
        )
        assert accounts == {
            "account:" + fixture.specs[0].owner: 1476,
            "account:" + fixture.specs[2].owner: 738,
        }
    finally:
        connection.close()
    _unchanged(fixture)


def test_same_fifty_six_json_frames_keep_one_stable_event_and_one_count(
    global_media_store,
):
    fixture = global_media_store
    connection, (conversations, messages, status) = _attach(fixture)
    try:
        assert status == "ready"
        for entry in fixture.specs[0].entries[:56]:
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {messages} WHERE id=?",
                    (media_placeholder_id(entry),),
                ).fetchone()[0]
                == 1
            )
        assert (
            connection.execute(
                f"SELECT message_count FROM {conversations} WHERE id=?",
                (fixture.conversation,),
            ).fetchone()[0]
            == 738
        )
    finally:
        connection.close()
    _unchanged(fixture)


def test_session_pages_use_the_conversation_index_and_bounded_sql_work(
    global_media_store,
):
    fixture = global_media_store
    spec = fixture.specs[2]
    connection, (_, messages, status) = _attach(fixture, conversation=spec.conversation)
    ticks = []
    try:
        assert status == "ready"
        plan = " ".join(
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM media_event_index.messages WHERE conversation_id=? ORDER BY time_sort,ordinal LIMIT 7 OFFSET 700",
                (spec.conversation,),
            )
        )
        assert "SEARCH" in plan and "message_conversation_time" in plan
        connection.set_progress_handler(lambda: ticks.append(1) and 0, 1000)
        rows = connection.execute(
            f"SELECT id,conversation_id FROM {messages} WHERE conversation_id=? ORDER BY (time_sort=0),time_sort,ordinal,id LIMIT 7 OFFSET 700",
            (spec.conversation,),
        ).fetchall()
        assert len(rows) == 7 and {row[1] for row in rows} == {spec.conversation}
        assert len(ticks) < 100
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()
    repository = WechatArchiveRepository(fixture.store)
    pages = [
        repository.query(
            session=spec.conversation,
            account="account:" + spec.owner,
            page=page,
            page_size=300,
        )
        for page in (1, 2, 3)
    ]
    assert {page["count"] for page in pages} == {738}
    rows = [row for page in pages for row in page["messages"]]
    assert len(rows) == len({row["id"] for row in rows}) == 738
    assert sum(row["text"] == "[Image]" for row in rows) == 720
    assert all(row["conversation_id"] == spec.conversation for row in rows)
    wrong_account = repository.query(
        session=spec.conversation, account="account:" + fixture.specs[0].owner
    )
    assert wrong_account["current"] is None and wrong_account["count"] == 0
    with pytest.raises(ValueError):
        WechatSessionExport(
            fixture.store,
            spec.conversation,
            account="account:" + fixture.specs[0].owner,
        )
    _unchanged(fixture)


def test_csv_and_message_navigation_share_each_account_scoped_media_timeline(
    global_media_store, monkeypatch
):
    fixture = global_media_store
    original_open = Path.open

    def no_payload(path, *args, **kwargs):
        assert path.suffix.casefold() not in {
            ".jpg",
            ".jpeg",
            ".png",
            ".mp4",
            ".mp3",
            ".dat",
            ".zip",
        }
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_payload)
    for spec in fixture.specs:
        selected = media_placeholder_id(spec.entries[-1])
        assert locate_media_event(fixture.history, selected) == spec.conversation
        located = message_source_location(fixture.store, selected, page_size=100)
        assert located["conversation_id"] == spec.conversation
        assert located["account_key"] == "account:" + spec.owner
        assert located["page"] == 8
        export = WechatSessionExport(
            fixture.store, spec.conversation, account="account:" + spec.owner
        )
        assert fixture.index in export.sources
        exported = list(
            csv.DictReader(
                io.StringIO("".join(session_csv_chunks(export)).lstrip("\ufeff"))
            )
        )
        assert len(exported) == 738
        assert sum(row["Statement"] == "[Image]" for row in exported) == 720
        assert exported[-1]["Time"] == spec.entries[-1]["node"]["raw_timestamp"]
    _unchanged(fixture)


@pytest.mark.parametrize(
    "mismatch", ["account", "peer", "member", "snapshot", "unpublished"]
)
def test_invalid_source_or_member_receipts_never_attach_partial_global_events(
    global_media_store, mismatch
):
    fixture = global_media_store
    with sqlite3.connect(fixture.index) as index:
        if mismatch == "account":
            index.execute("UPDATE conversations SET account_key='account:wxid_wrong'")
        elif mismatch == "peer":
            index.execute("UPDATE conversations SET peer_id='wxid_wrong_peer'")
        elif mismatch == "member":
            index.execute("UPDATE conversation_proofs SET member_sha256=?", ("f" * 64,))
        elif mismatch == "snapshot":
            index.execute("UPDATE sources SET metadata_sha256=?", ("f" * 64,))
        else:
            index.execute("UPDATE archive_info SET value='0' WHERE key='published'")
    before = fixture.history.read_bytes()
    connection, result = _attach(fixture, legacy=False)
    try:
        assert result == ("main.conversations", "main.messages", "invalid")
        assert (
            connection.execute("SELECT COUNT(*) FROM main.messages").fetchone()[0] == 54
        )
        assert (
            locate_media_event(
                fixture.history, media_placeholder_id(fixture.specs[0].entries[0])
            )
            is None
        )
    finally:
        connection.close()
    assert fixture.history.read_bytes() == before


def test_cloud_only_global_index_is_not_opened_or_hydrated(
    global_media_store, monkeypatch
):
    fixture = global_media_store
    original_lstat, original_connect = Path.lstat, sqlite3.connect

    def cloud_state(path):
        state = original_lstat(path)
        if path == fixture.index:
            return SimpleNamespace(
                **{
                    name: getattr(state, name)
                    for name in dir(state)
                    if name.startswith("st_") and name != "st_flags"
                },
                st_flags=0x40000000,
            )
        return state

    def no_index_open(path, *args, **kwargs):
        assert "media_events.sqlite3" not in str(path)
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", cloud_state)
    monkeypatch.setattr(sqlite3, "connect", no_index_open)
    connection, result = _attach(fixture, legacy=False)
    try:
        assert result == ("main.conversations", "main.messages", "absent")
        assert (
            locate_media_event(
                fixture.history, media_placeholder_id(fixture.specs[0].entries[0])
            )
            is None
        )
    finally:
        connection.close()


def test_export_stops_when_global_media_publication_changes(global_media_store):
    fixture = global_media_store
    spec = fixture.specs[2]
    export = WechatSessionExport(
        fixture.store, spec.conversation, account="account:" + spec.owner
    )
    assert fixture.index in export.sources
    with sqlite3.connect(fixture.index) as index:
        index.execute("INSERT INTO archive_info VALUES('synthetic_revision','changed')")
    with pytest.raises(WechatSessionSourceChanged):
        next(export.items())
    export.close()
    assert fixture.history.read_bytes() == fixture.before[0]


def test_verified_media_only_member_can_supply_a_session_without_any_primary_text(
    global_media_store,
):
    fixture = global_media_store
    spec = fixture.specs[2]
    with sqlite3.connect(fixture.history) as history:
        history.execute(
            "DELETE FROM messages WHERE conversation_id=?", (spec.conversation,)
        )
        history.execute("DELETE FROM conversations WHERE id=?", (spec.conversation,))
    with sqlite3.connect(fixture.index) as index:
        index.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture.history)),),
        )
    before = fixture.history.read_bytes(), fixture.index.read_bytes()
    page = WechatArchiveRepository(fixture.store).query(
        session=spec.conversation,
        account="account:" + spec.owner,
        page_size=7,
    )
    assert page["current"]["id"] == spec.conversation
    assert page["count"] == page["message_count"] == 720
    assert page["pages"] == 103
    assert len(page["messages"]) == 7
    assert all(row["text"] == "[Image]" for row in page["messages"])
    assert before == (fixture.history.read_bytes(), fixture.index.read_bytes())
