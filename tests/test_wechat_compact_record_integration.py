"""Compact source disclosure preserves raw evidence and nested message behavior.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from flask import Flask, render_template

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_history_browser import _present_record_fields
from app.web.presentation import format_wechat_source_clock
from tests.test_wechat_archive import make_archive


def _message(key, provenance):
    return SimpleNamespace(
        stable_id=key,
        author_label="Synthetic author",
        content_text="Literal source text",
        last_seen_at="2021-08-07T19:31:17",
        messaging={
            "direction": "received",
            "account_id": "wxid_synthetic_owner",
            "account_label": "Synthetic source",
            "kind": "direct",
            "peer_id": "wxid_synthetic_peer",
            "sender_id": "wxid_synthetic_peer",
            "timestamp_label": "7 Aug 2021 19:31:17",
            "time_basis": "Source clock; timezone unknown",
            "source_file": "Synthetic source.zip",
            "identity_status": "Verified archive ID",
            "source_label": "HTML export",
            "quality_flags": (),
            "provenance": provenance,
        },
    )


def _render(items):
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parents[1] / "app/web/templates"),
    )
    app.jinja_env.filters["wechat_source_clock"] = format_wechat_source_clock
    with app.test_request_context("/"):
        return render_template(
            "_wechat_browser.html",
            text_page=SimpleNamespace(
                session_detail=True,
                current_session=SimpleNamespace(messaging=items[0].messaging),
                items=items,
            ),
            filters=SimpleNamespace(account="", chat_kind="direct", sort="newest"),
            url_for=lambda *_args, **_kwargs: "/synthetic",
        )


class _Fields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tag = ""
        self.label = ""
        self.fields = []

    def handle_starttag(self, tag, attrs):
        if tag in {"dt", "dd"}:
            self.tag = tag
            self.parts = []

    def handle_data(self, value):
        if self.tag:
            self.parts.append(value)

    def handle_endtag(self, tag):
        if self.tag != tag:
            return
        text = "".join(self.parts)
        if tag == "dt":
            self.label = text
        else:
            self.fields.append((self.label, text))
        self.tag = ""


def test_raw_receipts_leave_the_document_but_source_coordinates_remain_escaped():
    provenance = {
        "server_id": "9223372036854775806",
        "locator": {
            "member_path": "Source/中文<script>alert(1)</script>.html",
            "member": {"zip_filename": "obsolete-garbled-name", "crc32": "deadbeef"},
            "archive_sha256": "a" * 64,
            "export_msgid": "30",
            "export_msgid_namespace": "MesLocalID",
            "message_ordinal": 30,
            "page_message_ordinal": 30,
            "page": 0,
        },
    }
    original = deepcopy(provenance)
    item = _message("compact-source", provenance)
    _present_record_fields([item])
    rendered = _render([item])
    parser = _Fields()
    parser.feed(rendered)
    fields = dict(parser.fields)
    assert fields["Source file"] == provenance["locator"]["member_path"]
    assert fields["Local export ID"] == "30 (MesLocalID)"
    assert fields["Message ID"] == provenance["server_id"]
    assert fields["Time basis"] == "Source clock; timezone unknown"
    assert "Record" not in fields and "Page position" not in fields
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
    assert "archive_sha256" not in rendered and "obsolete-garbled-name" not in rendered
    assert "deadbeef" not in rendered
    assert provenance == original


def test_forwarded_and_quoted_transcripts_share_compact_fields_without_changing_evidence():
    root = _message("root", {"locator": {"database": "message.sqlite", "local_id": 1}})
    forwarded = _message(
        "forwarded", {"locator": {"member_path": "来源/child.html", "page": 1}}
    )
    quoted = _message("quoted", {"locator": {"table": "Chat_source", "row_id": 12}})
    root.messaging["forwarded_record"] = {"title": "Chat history", "items": [forwarded]}
    root.messaging["quoted_reply"] = {
        "forwarded_record": {"title": "Quoted history", "items": [quoted]}
    }
    original = [
        deepcopy(item.messaging["provenance"]) for item in (root, forwarded, quoted)
    ]
    _present_record_fields([root])
    rendered = _render([root])
    assert "message.sqlite" in rendered and "来源/child.html" in rendered
    assert "Chat_source" in rendered
    for item, evidence in zip((root, forwarded, quoted), original, strict=True):
        assert item.messaging["record_fields"]
        assert item.messaging["provenance"] == evidence


def test_unknown_locator_shapes_add_no_empty_record_row():
    item = _message("unknown-source", {"locator": {"future_format": {"value": 12}}})
    _present_record_fields([item])
    rendered = _render([item])
    assert item.messaging["record_fields"] == ()
    assert "<dt>Record</dt>" not in rendered
    assert "Literal source text" in rendered


def test_published_archive_and_export_keep_the_complete_original_record(tmp_path):
    store = tmp_path / "store"
    import_wechat_archive(make_archive(tmp_path / "input", repeated=3), store)
    sessions = query_chat_history(
        store, source="wechat", account="account:wxid_account_a"
    )
    session = next(x for x in sessions.sessions if x.messaging["kind"] == "direct")
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        identifier, raw = connection.execute(
            "SELECT id,provenance FROM messages WHERE conversation_id=? ORDER BY ordinal LIMIT 1",
            (session.stable_id,),
        ).fetchone()
        provenance = json.loads(raw)
        provenance["locator"] = {
            "member_path": "Synthetic/Chat.html",
            "member": {"sha256": "b" * 64, "zip_filename": "exact-original-name"},
            "export_msgid": "17",
            "export_msgid_namespace": "MesLocalID",
        }
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(provenance), identifier),
        )
    before = history.read_bytes()
    page = query_chat_history(store, source="wechat", session=session.stable_id)
    item = next(x for x in page.items if x.stable_id == identifier)
    assert item.messaging["provenance"] == provenance
    assert {x["label"] for x in item.messaging["record_fields"]} == {
        "Source file",
        "Local export ID",
    }
    exported = build_chat_history_markdown(page)
    assert "exact-original-name" in exported and "b" * 64 in exported
    assert history.read_bytes() == before
