"""Session CSV preserves complete scoped timelines in three compact columns.

Code version: v2.0.0-codex.0
"""

from __future__ import annotations

import csv
from datetime import datetime
import io
import sqlite3
from unittest.mock import patch
from urllib.parse import unquote

import pytest

from app.core.config import CrawlConfig
from app.core.wechat_archive import (
    WechatArchiveRepository,
    import_wechat_archive,
    wechat_archive_path,
)
from app.core.wechat_session_export import (
    WechatSessionExport,
    WechatSessionSourceChanged,
)
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from app.web.wechat_session_csv import session_csv_chunks, session_csv_filename
from tests.test_wechat_archive import make_archive
from tests.test_wechat_resource_timeline import _reference, _session, _sidecar


@pytest.fixture
def session_app(tmp_path):
    root = tmp_path / "store"
    import_wechat_archive(make_archive(tmp_path / "archive", repeated=215), root)
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            root,
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    return app, root


def _rows(response):
    assert response.data.startswith(b"\xef\xbb\xbf")
    reader = csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"), newline=""))
    assert reader.fieldnames == ["Time", "Speaker ID", "Statement"]
    return list(reader)


def _expected_rows(items):
    result = []
    for item in items:
        stamp = item["timestamp_value"]
        if stamp:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            stamp = parsed.strftime("%Y-%m-%d %H:%M:%S")
            offset = parsed.strftime("%z")
            stamp += f" UTC{offset[:3]}:{offset[3:]}" if offset else ""
        label, identifier = item["sender_label"], item["sender_id"] or ""
        speaker = (
            f"{label} ({identifier})"
            if label and identifier and label != identifier
            else label or identifier
        )
        result.append({"Time": stamp, "Speaker ID": speaker, "Statement": item["text"]})
    return result


def _url(session):
    return f"/browser/wechat/session/{session['id']}/export.csv"


def test_csv_exports_all_pages_in_chronological_order_and_preserves_raw_text(
    session_app,
):
    app, root = session_app
    session = _session(root, kind="group")
    path = wechat_archive_path(root)
    special = '中文, "quoted"\r\nnext line 😀'
    with sqlite3.connect(path) as connection:
        identifier = connection.execute(
            "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal LIMIT 1",
            (session["id"],),
        ).fetchone()[0]
        connection.execute(
            "UPDATE messages SET text=?,search_text=?,timestamp_value='',"
            "timestamp_label='Unknown time',time_sort=0,time_basis='Unavailable source time' WHERE id=?",
            (special, special, identifier),
        )
    expected = WechatArchiveRepository(root).query(
        session=session["id"], page_size=1000
    )["messages"]
    before = path.read_bytes()
    response = app.test_client().get(
        _url(session),
        query_string={
            "account": session["account_key"],
            "chat_kind": "group",
            "page": 3,
            "q": "no-match",
            "scope": "page",
            "sort": "newest",
        },
    )
    assert response.status_code == 200 and response.is_streamed
    assert response.mimetype == "text/csv" and response.cache_control.no_store
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    rows = _rows(response)
    assert len(rows) == 215
    assert rows == _expected_rows(expected)
    assert rows[-1]["Time"] == "" and rows[-1]["Statement"] == special
    assert {row["Time"] for row in rows[:-1]} == {"2021-08-07 18:59:03 UTC+08:00"}
    assert {row["Speaker ID"] for row in rows} == {
        "Same display name (wxid_peer)",
        "Same display name (wxid_account_a)",
    }
    assert before == path.read_bytes()


@pytest.mark.parametrize(
    "parameters",
    [
        {"account": "account:wxid_account_b"},
        {"account": "source:xlsx-2"},
        {"chat_kind": "direct"},
        {"chat_kind": "invalid"},
    ],
)
def test_csv_rejects_mismatched_account_or_conversation_kind(session_app, parameters):
    app, root = session_app
    response = app.test_client().get(
        _url(_session(root, kind="group")), query_string=parameters
    )
    assert response.status_code == 404
    assert "Content-Disposition" not in response.headers


def test_unknown_source_partitions_remain_separate_and_filename_is_safe(session_app):
    app, root = session_app
    sessions = WechatArchiveRepository(root).query(account="source:xlsx-2")[
        "conversations"
    ]
    session = sessions[0]
    title = '讨论组 / "title"\r\nunsafe😀'
    with sqlite3.connect(wechat_archive_path(root)) as connection:
        connection.execute(
            "UPDATE conversations SET title=? WHERE id=?", (title, session["id"])
        )
    response = app.test_client().get(
        _url(session), query_string={"account": "source:xlsx-2"}
    )
    assert response.status_code == 200
    rows = _rows(response)
    assert len(rows) == 430
    expected = WechatArchiveRepository(root).query(
        account="source:xlsx-2", session=session["id"], page_size=1000
    )
    assert rows == _expected_rows(expected["messages"])
    assert not any("wxid_account_a" in row["Statement"] for row in rows)
    disposition = response.headers["Content-Disposition"]
    assert "attachment;" in disposition and "filename*=UTF-8''" in disposition
    filename = unquote(disposition.split("filename*=UTF-8''")[1])
    assert "讨论组" in filename and session["id"][-12:] in filename
    assert all(character not in filename for character in ("/", '"', "\r", "\n"))
    assert len(session_csv_filename({"title": "中" * 1000, "id": session["id"]})) <= 97


def test_csv_includes_valid_timeline_resource_references_without_reading_media(
    session_app,
):
    app, root = session_app
    session = _session(root, kind="group")
    reference = _reference(session, "[Image]", 17)
    _sidecar(root, [(session, reference)])
    expected = WechatArchiveRepository(root).query(
        session=session["id"], page_size=1000
    )
    response = app.test_client().get(_url(session))
    rows = _rows(response)
    assert len(rows) == expected["count"] == 216
    assert rows == _expected_rows(expected["messages"])
    resource = next(row for row in rows if row["Statement"] == "[Image]")
    assert resource == {
        "Time": "2021-08-07 19:00:17 UTC+08:00",
        "Speaker ID": "Unknown sender",
        "Statement": "[Image]",
    }


def test_csv_rejects_invalid_or_busy_sources_before_download(session_app):
    app, root = session_app
    client = app.test_client()
    for session in ("invalid", "wechat:" + "0" * 64):
        assert (
            client.get(f"/browser/wechat/session/{session}/export.csv").status_code
            == 404
        )
    session = _session(root, kind="group")
    journal = wechat_archive_path(root).with_name("history.sqlite3-journal")
    journal.write_bytes(b"active publication")
    response = client.get(_url(session))
    assert response.status_code == 503
    assert "Content-Disposition" not in response.headers


def test_source_change_stops_before_next_batch_and_closes_cursor(session_app):
    _, root = session_app
    session = _session(root, kind="group")
    export = WechatSessionExport(root, session["id"])
    items = export.items()
    assert len([next(items) for _ in range(200)]) == 200
    journal = wechat_archive_path(root).with_name("history.sqlite3-journal")
    journal.write_bytes(b"publication started")
    with pytest.raises(WechatSessionSourceChanged):
        next(items)
    assert export.connection is None


def test_cancelled_download_closes_iterator_before_connection(session_app):
    _, root = session_app
    export = WechatSessionExport(root, _session(root, kind="group")["id"])
    chunks = session_csv_chunks(export)
    next(chunks)
    next(chunks)
    chunks.close()
    assert export.connection is None


def test_equal_clock_and_ordinal_have_deterministic_identity_order(session_app):
    app, root = session_app
    session = _session(root, kind="group")
    with sqlite3.connect(wechat_archive_path(root)) as connection:
        connection.execute(
            "UPDATE messages SET ordinal=1,time_sort=1,text=id WHERE conversation_id=?",
            (session["id"],),
        )
    rows = _rows(app.test_client().get(_url(session)))
    identifiers = [row["Statement"] for row in rows]
    assert identifiers == sorted(identifiers)
