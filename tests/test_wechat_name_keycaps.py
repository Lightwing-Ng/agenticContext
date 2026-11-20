"""Visible WeChat names repair reversed keycaps without changing source strings.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy
import csv
from html import unescape
from html.parser import HTMLParser
from io import StringIO
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

from app.core.chat_history_browser import build_chat_history_markdown, query_chat_history
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.web.app import create_app
from app.web.presentation import format_wechat_source_clock
from tests.test_wechat_archive import make_archive
from tests.test_wechat_html_quotes import html_quote_evidence


REVERSED = "\u20e3\ufe0f"
STANDARD = "\ufe0f\u20e3"
TEMPLATES = Path(__file__).resolve().parents[1] / "app/web/templates"


def _environment():
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(("html", "xml")),
    )
    environment.filters["wechat_source_clock"] = format_wechat_source_clock
    return environment


def _macro():
    page = SimpleNamespace(messaging={}, session_detail=False, sessions=())
    return _environment().get_template("_wechat_browser.html").make_module({"text_page": page}).display_name


@pytest.mark.parametrize("base", tuple("0123456789#*"))
def test_each_keycap_base_has_one_display_only_repair(base):
    display = _macro()
    source = "Name " + base + REVERSED
    expected = "Name " + base + STANDARD
    assert str(display(source)) == expected
    assert str(display(expected)) == expected
    assert source == "Name " + base + REVERSED


@pytest.mark.parametrize(
    "name",
    [
        "Ordinary source name",
        "Name 1\ufe0f\u20e3",
        "Name 1\ufe0e\u20e3",
        "Name 1\u20e3",
        "Name 1\u20e3\ufe0e",
        "Name A\u20e3\ufe0f",
        "Name １\u20e3\ufe0f",
        "Name ♥\ufe0f",
        "Name 👨‍👩‍👧‍👦",
        "Name 👩🏽‍💻",
        "Name 🇨🇳",
        "Name e\u0301 ﬃ",
        "  Literal  spacing\tand\nlines  ",
    ],
)
def test_other_names_and_unicode_sequences_remain_literal(name):
    assert str(_macro()(name)) == name


def test_repaired_name_keeps_jinja_autoescape_and_never_creates_elements():
    source = '<img src=x onerror="alert(1)"> & Name 1' + REVERSED
    rendered = str(_macro()(source))
    assert unescape(rendered) == source.replace("1" + REVERSED, "1" + STANDARD)
    assert "<img" not in rendered and "&lt;img" in rendered
    assert "&amp;lt;img" not in rendered


class _RenderedNames(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = {key: [] for key in ("wechat-speaker", "wechat-quoted-speaker", "wechat-message-text", "wechat-quoted-text", "source_detail")}

    def handle_starttag(self, tag, attrs):
        if tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            return
        classes = dict(attrs).get("class", "").split()
        kind = next((key for key in self.parts if key in classes), None)
        self.stack.append((tag, "source_detail" if tag == "dd" else kind, []))

    def handle_data(self, data):
        for _tag, kind, values in self.stack:
            if kind:
                values.append(data)

    def handle_endtag(self, tag):
        for position in range(len(self.stack) - 1, -1, -1):
            if self.stack[position][0] == tag:
                closing = self.stack[position:]
                self.stack = self.stack[:position]
                for _tag, kind, values in closing:
                    if kind:
                        self.parts[kind].append("".join(values))
                return


def _message(identifier, author, *, viewer="", quote=None, record=None, body="Body 0" + REVERSED):
    context = {
        "kind": "group", "direction": "received", "viewer_display_name": viewer,
        "quoted_reply": quote, "forwarded_record": record, "display_text": body,
        "source_file": "Synthetic source", "account_id": "", "account_label": "Synthetic account",
        "identity_status": "Unresolved", "source_label": "HTML export", "peer_id": "synthetic@chatroom",
        "sender_id": "synthetic-source-id", "timestamp_label": "", "time_basis": "Unavailable source time",
        "provenance": {}, "quality_flags": [], "system_notices": (), "is_system_notice": False,
    }
    return SimpleNamespace(stable_id=identifier, author_label=author, content_text=body, last_seen_at="", messaging=context)


def _record(children):
    return {"title": "Forwarded conversation", "source_title": "", "authority": "source_verified_html_export_tree", "items": tuple(children)}


@pytest.mark.parametrize("quoted_viewer", [False, True])
def test_complete_template_repairs_primary_quote_and_nested_forward_names_only(quoted_viewer):
    primary_quote = {"speaker": "Quoted 3" + REVERSED, "viewer_display_name": "Quoted viewer 4" + REVERSED if quoted_viewer else "", "text": "Quoted body 5" + REVERSED}
    primary = _message("primary", "Source 1" + REVERSED, viewer="Viewer 2" + REVERSED, quote=primary_quote)
    child_quote = {"speaker": "Forward quoted #" + REVERSED, "text": "Forward quote body *" + REVERSED}
    child = _message("child", "Forward source 6" + REVERSED, viewer="Forward viewer 7" + REVERSED, quote=child_quote)
    grandchild = _message("grandchild", "Nested source 8" + REVERSED, viewer="Nested viewer 9" + REVERSED)
    nested = _message("nested-envelope", "Nested envelope", record=_record([grandchild]))
    envelope = _message("forward-envelope", "Forwarding envelope", record=_record([child, nested]))
    messages = (primary, envelope)
    preserved = deepcopy(messages)
    page = SimpleNamespace(messaging={}, session_detail=True, current_session=SimpleNamespace(messaging=primary.messaging), items=messages)
    rendered = _environment().get_template("_wechat_browser.html").render(text_page=page)
    parsed = _RenderedNames()
    parsed.feed(rendered)
    assert parsed.parts["wechat-speaker"] == ["Viewer 2" + STANDARD, "Forwarding envelope", "Forward viewer 7" + STANDARD, "Nested envelope", "Nested viewer 9" + STANDARD]
    assert parsed.parts["wechat-quoted-speaker"] == ["Quoted viewer 4" + STANDARD if quoted_viewer else "Quoted 3" + STANDARD, "Forward quoted #" + STANDARD]
    assert parsed.parts["wechat-quoted-text"] == ["Quoted body 5" + REVERSED, "Forward quote body *" + REVERSED]
    assert parsed.parts["wechat-message-text"].count("Body 0" + REVERSED) == 3
    for source in ("Source 1", "Forward source 6", "Nested source 8"):
        assert any(value == source + REVERSED for value in parsed.parts["source_detail"])
    assert messages == preserved


def test_real_browser_route_repairs_names_while_model_markdown_csv_and_database_stay_raw(tmp_path):
    archive = make_archive(tmp_path / "archive")
    store = tmp_path / "store"
    import_wechat_archive(archive, store, source_kinds={"xlsx-3": "direct"})
    history = wechat_archive_path(store)
    source_author = "Current 1" + REVERSED
    quote_author = "Earlier 2" + REVERSED
    quote_body = "Quoted body 3" + REVERSED
    reply = "Reply body 4" + REVERSED
    envelope = f"「{quote_author}：{quote_body}」"
    text = envelope + "\n\n" + reply
    evidence = html_quote_evidence(envelope=envelope, reply=reply)
    with sqlite3.connect(history) as connection:
        session = connection.execute("SELECT id FROM conversations WHERE partition_id='xlsx-3'").fetchone()[0]
        identifier = connection.execute("SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal LIMIT 1", (session,)).fetchone()[0]
        connection.execute("UPDATE partitions SET source_label='HTML export',source_file='synthetic.html' WHERE id='xlsx-3'")
        connection.execute("UPDATE messages SET sender_label=?,text=?,provenance=? WHERE id=?", (source_author, text, json.dumps(evidence), identifier))
    before = history.read_bytes()
    page = query_chat_history(store, source="wechat", account="source:xlsx-3", session=session)
    message = next(item for item in page.items if item.stable_id == identifier)
    assert message.author_label == source_author
    assert message.content_text == text
    assert message.messaging["quoted_reply"]["speaker"] == quote_author
    assert message.messaging["quoted_reply"]["text"] == quote_body
    assert message.messaging["provenance"] == evidence
    assert text in build_chat_history_markdown(page)
    app = create_app(store)
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.get("/browser", query_string={"view": "text", "source": "wechat", "account": "source:xlsx-3", "session": session})
    assert response.status_code == 200
    parsed = _RenderedNames()
    parsed.feed(response.get_data(as_text=True))
    assert "Current 1" + STANDARD in parsed.parts["wechat-speaker"]
    assert "Earlier 2" + STANDARD in parsed.parts["wechat-quoted-speaker"]
    assert quote_body in parsed.parts["wechat-quoted-text"]
    assert reply in parsed.parts["wechat-message-text"]
    exported = client.get(f"/browser/wechat/session/{session}/export.csv", query_string={"account": "source:xlsx-3"})
    assert exported.status_code == 200
    rows = list(csv.DictReader(StringIO(exported.get_data(as_text=True).lstrip("\ufeff"))))
    assert any(source_author in row["Speaker ID"] and reply in row["Statement"] for row in rows)
    assert history.read_bytes() == before


def test_conversation_list_and_heading_repair_names_without_mutating_sources(tmp_path):
    archive = make_archive(tmp_path / "archive")
    store = tmp_path / "store"
    import_wechat_archive(archive, store, source_kinds={"xlsx-3": "direct"})
    history = wechat_archive_path(store)
    original = "Peer 2" + REVERSED
    expected = "Peer 2" + STANDARD
    with sqlite3.connect(history) as connection:
        session = connection.execute(
            "SELECT id FROM conversations WHERE partition_id='xlsx-3'"
        ).fetchone()[0]
        connection.execute("UPDATE conversations SET title=? WHERE id=?", (original, session))
    preserved = history.read_bytes()
    app = create_app(store)
    app.config.update(TESTING=True)
    client = app.test_client()
    parameters = {"view": "text", "source": "wechat", "account": "source:xlsx-3"}
    listing = client.get("/browser", query_string=parameters)
    detail = client.get("/browser", query_string={**parameters, "session": session})
    assert listing.status_code == detail.status_code == 200
    assert expected + "</a>" in listing.get_data(as_text=True)
    assert expected + "</h2>" in detail.get_data(as_text=True)
    page = query_chat_history(store, source="wechat", account="source:xlsx-3", session=session)
    assert page.current_session.conversation_title == original
    assert history.read_bytes() == preserved
