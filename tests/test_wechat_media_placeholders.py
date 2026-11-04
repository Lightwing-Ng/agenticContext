"""Audited missing-media frames remain source-scoped, payload-free timeline events.

Code version: v1.0.2-codex.0
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from html.parser import HTMLParser
import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from flask import Flask, render_template
import pytest

from app.core.chat_history_browser import query_chat_history
from app.core.wechat_archive import WechatArchiveRepository, _SCHEMA, _identifier
from app.core.wechat_media_placeholders import (
    attach_media_placeholders,
    media_placeholder_id,
)
from app.core.wechat_session_export import (
    WechatSessionExport,
    WechatSessionSourceChanged,
)
from app.web.presentation import format_wechat_source_clock
from app.web.wechat_session_csv import session_csv_chunks


OWNER = "wxid_placeholder_owner"
PEER = "wxid_placeholder_peer"
ARCHIVE_SHA = hashlib.sha256(b"Synthetic immutable HTML source").hexdigest()
MEMBER_SHA = hashlib.sha256(b"Synthetic audited HTML conversation").hexdigest()
AUDIT_SHA = hashlib.sha256(b"Synthetic complete node disposition audit").hexdigest()
SOURCE = "html-" + ARCHIVE_SHA[:24]
MEMBER = "Synthetic/Chat.html"
CONVERSATION = "wechat:" + _identifier(SOURCE, MEMBER)
MEDIA_ORDINALS = (8, 13)


def _clock(ordinal):
    return f"2023-12-29 13:00:{ordinal:02d}"


def _entry(ordinal):
    return {
        "source_snapshot_id": 1,
        "source_id": SOURCE,
        "archive_sha256": ARCHIVE_SHA,
        "member_sha256": MEMBER_SHA,
        "audit_sha256": AUDIT_SHA,
        "conversation_id": CONVERSATION,
        "node": {
            "role": "top_level",
            "status": "deferred_top_level",
            "source_locator": {
                "member_path": MEMBER,
                "message_ordinal": ordinal,
                "page": 0,
                "page_message_ordinal": ordinal,
            },
            "export_msgid": str(ordinal),
            "type": "image",
            "text_kind": "deferred",
            "classes": ["media", "msg", "raw-img", "right"],
            "flags": ["export_msgid_is_meslocalid", "export_time_zone_unavailable"],
            "text_length": 0,
            "text_sha256": hashlib.sha256(b"").hexdigest(),
            "direction": "sent",
            "raw_timestamp": _clock(ordinal),
            "sender_export_id": "synthetic.owner.alias",
            "sender_native_id": OWNER,
            "avatar_path": f"Chat_files/Portrait/{OWNER}.jpg",
            "sender_label": "Synthetic owner",
        },
    }


@pytest.fixture
def placeholder_store(tmp_path):
    store = tmp_path / "store"
    history = store / "messaging/wechat/history.sqlite3"
    history.parent.mkdir(parents=True)
    member = {"member_path": MEMBER, "sha256": MEMBER_SHA, "size": 4096}
    metadata = {
        "source_id": SOURCE,
        "source_sha256": ARCHIVE_SHA,
        "source_account_id": OWNER,
        "owner_verified": True,
        "owner_evidence": {"native_username": OWNER},
        "source_file": "Synthetic HTML source.zip",
        "source_label": "HTML export",
        "selected_text_members": [member],
        "source_conversation_catalog": [
            {
                "member_path": MEMBER,
                "index_avatar_native_id": PEER,
                "conversation_kind": "direct",
                "reviewed_resolution": {"main_html_sha256": MEMBER_SHA},
                "counts": {
                    "top_level_nodes": 20,
                    "all_message_nodes": 20,
                    "retained_top_level_text": 18,
                    "deferred_top_level": 2,
                },
                "declared_async_counts": {"numberOfPages": 0},
            }
        ],
    }
    text_rows = []
    for ordinal in range(1, 21):
        if ordinal in MEDIA_ORDINALS:
            continue
        stamp = datetime.fromisoformat(_clock(ordinal))
        record = f"synthetic-text-frame-{ordinal}"
        text = f"Retained text {ordinal}"
        provenance = {
            "record_id": record,
            "source_snapshot": 1,
            "source_time": _clock(ordinal),
            "text_extraction_kind": "plain_text",
            "export_msgid_namespace": "MesLocalID",
            "sender_native_id": OWNER,
            "sender_export_id": "synthetic.owner.alias",
            "original_timezone": "unknown",
            "locator": {
                "member_path": MEMBER,
                "archive_sha256": ARCHIVE_SHA,
                "member": member,
                "message_ordinal": ordinal,
                "page_message_ordinal": ordinal,
                "page": 0,
                "export_msgid": str(ordinal),
                "export_msgtype": "1",
                "export_msgid_namespace": "MesLocalID",
            },
        }
        text_rows.append(
            (
                _identifier(SOURCE, record),
                CONVERSATION,
                ordinal,
                OWNER,
                "Synthetic owner",
                "sent",
                text,
                stamp.replace(tzinfo=UTC).timestamp(),
                stamp.isoformat(),
                f"29 Dec 2023 13:00:{ordinal:02d}",
                "Source clock; timezone unknown",
                json.dumps(provenance),
                "[]",
                text.casefold(),
            )
        )
    with sqlite3.connect(history) as connection:
        connection.executescript(_SCHEMA)
        connection.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
            "source_id TEXT,metadata TEXT)"
        )
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                SOURCE,
                "account:" + OWNER,
                OWNER,
                "Synthetic owner",
                "Corroborated export ID",
                "HTML export",
                "Synthetic HTML source.zip",
                "",
            ),
        )
        connection.execute(
            "INSERT INTO source_snapshots VALUES(1,?,?,?)",
            (ARCHIVE_SHA, SOURCE, json.dumps(metadata)),
        )
        first, last = text_rows[0], text_rows[-1]
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                CONVERSATION,
                SOURCE,
                "account:" + OWNER,
                PEER,
                "Synthetic conversation",
                "direct",
                18,
                first[7],
                last[7],
                first[8],
                last[8],
                last[6],
            ),
        )
        connection.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", text_rows
        )
        connection.execute(
            "INSERT INTO archive_info VALUES('manifest',?)",
            (json.dumps({"schema_version": 1, "messages": 18, "sources": []}),),
        )
    entries = [_entry(ordinal) for ordinal in MEDIA_ORDINALS]
    overlay = history.with_name("media_placeholders.json")
    overlay.write_text(json.dumps({"schema_version": 1, "entries": entries}))
    return SimpleNamespace(
        store=store,
        history=history,
        overlay=overlay,
        entries=entries,
        metadata=metadata,
        conversation=CONVERSATION,
        text_rows=text_rows,
    )


def _attach(fixture):
    connection = sqlite3.connect(fixture.history.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection, attach_media_placeholders(
        connection, fixture.history, "main.conversations", "main.messages"
    )


def test_two_audited_images_extend_the_eighteen_text_events_without_payload_reads(
    placeholder_store,
):
    fixture = placeholder_store
    before = fixture.history.read_bytes(), fixture.overlay.read_bytes()
    connection, result = _attach(fixture)
    try:
        conversations, messages, status = result
        assert result == (
            "placeholder_timeline_conversations",
            "placeholder_timeline_messages",
            "ready",
        )
        rows = [
            dict(row)
            for row in connection.execute(f"SELECT * FROM {messages} ORDER BY ordinal")
        ]
        assert len(rows) == 20
        assert [row["ordinal"] for row in rows] == list(range(1, 21))
        assert (
            connection.execute(f"SELECT message_count FROM {conversations}").fetchone()[
                0
            ]
            == 20
        )
        images = [row for row in rows if row["text"] == "[Image]"]
        assert len(images) == 2
        for entry, row in zip(fixture.entries, images, strict=True):
            assert row["id"] == media_placeholder_id(entry)
            assert row["sender_id"] == OWNER and row["direction"] == "sent"
            assert row["timestamp_value"] == _clock(row["ordinal"]).replace(" ", "T")
            assert row["timestamp_label"] == f"29 Dec 2023 13:00:{row['ordinal']:02d}"
            assert row["time_basis"] == "Source clock; timezone unknown"
            proof = json.loads(row["provenance"])
            assert proof["media_reference"]["availability"] == "metadata_only"
            assert proof["source_snapshot"] == 1
            assert "resource_reference" not in proof
            assert proof["locator"]["export_msgid_namespace"] == "MesLocalID"
    finally:
        connection.close()
    assert (fixture.history.read_bytes(), fixture.overlay.read_bytes()) == before


def test_pagination_search_repeated_queries_and_csv_share_the_twenty_event_timeline(
    placeholder_store,
):
    fixture = placeholder_store
    repository = WechatArchiveRepository(fixture.store)
    pages = [
        repository.query(session=CONVERSATION, page=page, page_size=7)
        for page in (1, 2, 3)
    ]
    rows = [row for page in pages for row in page["messages"]]
    assert {page["count"] for page in pages} == {20}
    assert {page["message_count"] for page in pages} == {20}
    assert {page["pages"] for page in pages} == {3}
    assert [row["ordinal"] for row in rows] == list(range(1, 21))
    assert len({row["id"] for row in rows}) == 20
    repeated = repository.query(session=CONVERSATION, page_size=100)
    assert [row["id"] for row in repeated["messages"]] == [row["id"] for row in rows]
    assert repository.query(session=CONVERSATION, query="[Image]")["count"] == 2
    listed = repository.query(account="account:" + OWNER)
    assert listed["conversations"][0]["message_count"] == listed["message_count"] == 20
    export = WechatSessionExport(
        fixture.store, CONVERSATION, account="account:" + OWNER
    )
    exported = list(
        csv.DictReader(
            io.StringIO("".join(session_csv_chunks(export)).lstrip("\ufeff"))
        )
    )
    assert len(exported) == 20
    assert [row["Statement"] for row in exported] == [row["text"] for row in rows]
    assert [row["Time"] for row in exported] == [
        _clock(index) for index in range(1, 21)
    ]


def test_same_frame_identity_does_not_depend_on_future_time_or_payload_metadata(
    placeholder_store,
):
    fixture = placeholder_store
    entry = fixture.entries[0]
    before_id = media_placeholder_id(entry)
    changed = deepcopy(entry)
    changed["node"].update(
        raw_timestamp="2024-1-1 01:02:03", type="video", export_msgid="999"
    )
    assert media_placeholder_id(changed) == before_id
    connection, (_, messages, status) = _attach(fixture)
    assert status == "ready"
    projected = dict(
        connection.execute(
            f"SELECT * FROM {messages} WHERE id=?", (before_id,)
        ).fetchone()
    )
    connection.close()
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "INSERT INTO messages("
            + ",".join(projected)
            + ") VALUES("
            + ",".join("?" for _ in projected)
            + ")",
            tuple(projected.values()),
        )
        connection.execute("UPDATE conversations SET message_count=19")
    page = WechatArchiveRepository(fixture.store).query(session=CONVERSATION)
    assert page["count"] == page["message_count"] == 20
    assert sum(row["id"] == before_id for row in page["messages"]) == 1


@pytest.mark.parametrize(
    "mismatch",
    [
        "owner",
        "peer",
        "archive",
        "member",
        "snapshot",
        "witness",
        "forwarded_node",
        "frame_outside_audit",
    ],
)
def test_unproved_media_frames_do_not_extend_the_published_timeline(
    placeholder_store, mismatch
):
    fixture = placeholder_store
    entries = deepcopy(fixture.entries)
    metadata = deepcopy(fixture.metadata)
    if mismatch == "owner":
        metadata["owner_evidence"]["native_username"] = "wxid_other_owner"
    elif mismatch == "peer":
        metadata["source_conversation_catalog"][0]["index_avatar_native_id"] = (
            "wxid_other_peer"
        )
    elif mismatch == "archive":
        entries[0]["archive_sha256"] = "a" * 64
    elif mismatch == "member":
        entries[0]["member_sha256"] = "b" * 64
    elif mismatch == "snapshot":
        entries[0]["source_snapshot_id"] = 999
    elif mismatch == "forwarded_node":
        entries[0]["node"]["role"] = "forwarded"
    elif mismatch == "frame_outside_audit":
        entries[0]["node"]["source_locator"].update(
            message_ordinal=21, page_message_ordinal=21
        )
    else:
        with sqlite3.connect(fixture.history) as connection:
            connection.execute(
                "UPDATE messages SET provenance=json_set(provenance,'$.source_snapshot',999)"
            )
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE source_snapshots SET metadata=?", (json.dumps(metadata),)
        )
    fixture.overlay.write_text(json.dumps({"schema_version": 1, "entries": entries}))
    before = fixture.history.read_bytes(), fixture.overlay.read_bytes()
    connection, result = _attach(fixture)
    try:
        assert result == ("main.conversations", "main.messages", "invalid")
        assert (
            connection.execute("SELECT COUNT(*) FROM main.messages").fetchone()[0] == 18
        )
    finally:
        connection.close()
    assert (fixture.history.read_bytes(), fixture.overlay.read_bytes()) == before


def test_top_level_media_ordinal_can_follow_forwarded_child_nodes(placeholder_store):
    fixture = placeholder_store
    metadata = deepcopy(fixture.metadata)
    metadata["source_conversation_catalog"][0]["counts"].update(
        top_level_nodes=20, all_message_nodes=24
    )
    entries = deepcopy(fixture.entries)
    entries[0]["node"]["source_locator"].update(
        message_ordinal=23, page_message_ordinal=23
    )
    entries[0]["node"].update(export_msgid="23", raw_timestamp=_clock(23))
    with sqlite3.connect(fixture.history) as connection:
        connection.execute(
            "UPDATE source_snapshots SET metadata=?", (json.dumps(metadata),)
        )
    fixture.overlay.write_text(json.dumps({"schema_version": 1, "entries": entries}))
    page = WechatArchiveRepository(fixture.store).query(session=CONVERSATION)
    assert page["count"] == page["message_count"] == 20
    assert {row["ordinal"] for row in page["messages"] if row["text"] == "[Image]"} == {
        13,
        23,
    }


def test_cloud_only_overlay_is_not_hydrated(placeholder_store, monkeypatch):
    fixture = placeholder_store
    original_lstat = Path.lstat
    original_open = Path.open

    def flagged_lstat(path):
        result = original_lstat(path)
        if path == fixture.overlay:
            values = {
                name: getattr(result, name)
                for name in dir(result)
                if name.startswith("st_")
            }
            values.update(st_flags=0x40000000)
            return SimpleNamespace(**values)
        return result

    def guarded_open(path, *args, **kwargs):
        if path == fixture.overlay:
            raise AssertionError("A cloud-only media overlay must not be opened.")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", flagged_lstat)
    monkeypatch.setattr(Path, "open", guarded_open)
    connection, result = _attach(fixture)
    try:
        assert result == ("main.conversations", "main.messages", "absent")
    finally:
        connection.close()


def test_optional_multi_gigabyte_cloud_resource_index_is_not_opened(
    placeholder_store, monkeypatch
):
    fixture = placeholder_store
    resources = fixture.history.with_name("resource_references.sqlite3")
    resources.write_bytes(b"Deliberately not an opened SQLite payload")
    original_stat = Path.stat
    original_connect = sqlite3.connect
    statements = []

    def flagged_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == resources:
            values = {
                name: getattr(result, name)
                for name in dir(result)
                if name.startswith("st_")
            }
            values.update(st_flags=0x40000000, st_size=6 * 1024**3)
            return SimpleNamespace(**values)
        return result

    def guarded_connect(database, *args, **kwargs):
        assert "resource_references.sqlite3" not in str(database)
        connection = original_connect(database, *args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(Path, "stat", flagged_stat)
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    page = WechatArchiveRepository(fixture.store).query(session=CONVERSATION)
    assert page["count"] == 20 and page["resource_index_status"] == "ready"
    assert page["resource_index_accounts"] is None
    assert not any(
        "resource_references.sqlite3" in statement for statement in statements
    )


def test_placeholder_export_rejects_metadata_changes_before_first_body_yield(
    placeholder_store,
):
    fixture = placeholder_store
    export = WechatSessionExport(fixture.store, CONVERSATION)
    fixture.overlay.write_text(fixture.overlay.read_text() + "\n")
    with pytest.raises(WechatSessionSourceChanged):
        next(export.items())
    export.close()


class _MessageElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.articles = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "article":
            self.current = {"tags": [], "attributes": [], "text": []}
            self.articles[values.get("id")] = self.current
        if self.current is not None:
            self.current["tags"].append(tag)
            self.current["attributes"].extend(values)

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "article":
            self.current = None


def test_metadata_only_images_reuse_standard_resource_cards_without_payload_actions(
    placeholder_store,
):
    fixture = placeholder_store
    page = query_chat_history(fixture.store, source="wechat", session=CONVERSATION)
    images = [item for item in page.items if item.content_text == "[Image]"]
    assert len(images) == 2
    assert all(
        item.messaging["resource"]["status"] == "metadata_only" for item in images
    )
    app = Flask(
        __name__, template_folder=str(Path(__file__).parents[1] / "app/web/templates")
    )
    app.jinja_env.filters["wechat_source_clock"] = format_wechat_source_clock
    with app.test_request_context("/"):
        rendered = render_template(
            "_wechat_browser.html",
            text_page=page,
            filters=SimpleNamespace(
                account="account:" + OWNER,
                chat_kind="direct",
                sort="newest",
                session_page=1,
            ),
            url_for=lambda *_args, **_kwargs: "/synthetic",
        )
    parsed = _MessageElements()
    parsed.feed(rendered)
    for image in images:
        article = parsed.articles["wechat_message_" + image.stable_id]
        assert "".join(article["text"]).count("Metadata only") == 1
        assert "data-wechat-resource-kind" in article["attributes"]
        assert "data-wechat-resource-status" in article["attributes"]
        assert not set(article["tags"]) & {"img", "video", "audio", "button"}
        assert not set(article["attributes"]) & {
            "href",
            "download",
            "data-wechat-resource-reveal",
        }
        assert "Finder" not in "".join(article["text"])
