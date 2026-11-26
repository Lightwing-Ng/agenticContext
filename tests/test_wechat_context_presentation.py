"""WeChat message presentation preserves source text and separate speaker roles.

Code version: v1.1.0-codex.0
"""

from copy import deepcopy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_history_browser import (
    _html_quoted_reply_presentation,
    _present_html_forwarded_quotes,
    query_wechat_history,
)
from app.core.wechat_html_export import parse_html_export
from app.web.presentation import format_wechat_source_clock
from tests.test_wechat_archive import make_archive
from tests.test_wechat_html_export import boundary, fixture_zip, message
from tests.test_wechat_html_forwarded import _component, _evidence, _project
from tests.wechat_browser_markup import parse_browser_markup


def test_quoted_reply_inside_forward_uses_quote_roles_without_rewriting_source():
    child = _component(
        text="Actual reply <script>stays literal</script>",
        text_kind="quoted_reply",
        quoted_components=[
            {
                "kind": "rendered_quote",
                "text": "「Earlier participant：Quoted original」",
            }
        ],
    )
    evidence = _evidence(children=[child])
    preserved = deepcopy(evidence)
    record = _project(evidence)
    raw = record["items"][0].content_text
    _present_html_forwarded_quotes(record)
    item = record["items"][0]
    assert item.messaging["quoted_reply"] == {
        "speaker": "Earlier participant",
        "text": "Quoted original",
    }
    assert item.messaging["display_text"] == child["text"]
    assert item.author_label == "Source friend"
    assert item.content_text == raw
    assert item.last_seen_at == "2021-08-05T21:37:44"
    assert evidence == preserved


def test_quote_inside_nested_forward_keeps_each_default_closed_container():
    inner = _component(
        text="Inner reply",
        text_kind="quoted_reply",
        quoted_components=[
            {"kind": "rendered_quote", "text": "「Original：Earlier text」"}
        ],
    )
    outer = _component(text="Outer forwarding label", forwarded_components=[inner])
    record = _project(_evidence(children=[outer]))
    _present_html_forwarded_quotes(record)
    nested = record["items"][0].messaging["forwarded_record"]
    assert record["title"] == "Source forwarding title"
    assert nested["title"] == "Outer forwarding label"
    assert nested["items"][0].messaging["display_text"] == "Inner reply"
    assert nested["items"][0].messaging["quoted_reply"]["text"] == "Earlier text"


def test_unverified_tree_and_literal_quote_markers_do_not_acquire_roles():
    record = _project(_evidence(children=[_component(text="「A：Literal example」")]))
    _present_html_forwarded_quotes(record)
    assert "quoted_reply" not in record["items"][0].messaging
    claimed = _project(
        _evidence(
            children=[
                _component(
                    text="Reply",
                    text_kind="quoted_reply",
                    quoted_components=[
                        {"kind": "rendered_quote", "text": "「A：Old」"}
                    ],
                )
            ]
        )
    )
    claimed["authority"] = "unverified_source"
    _present_html_forwarded_quotes(claimed)
    assert "quoted_reply" not in claimed["items"][0].messaging


def test_top_level_quote_can_retain_its_own_verified_forwarded_children():
    envelope = "「Earlier participant：Original question」"
    reply = "Current reply"
    evidence = _evidence(primary=reply, kind="quoted_reply")
    evidence[0]["quoted_components"] = [{"kind": "rendered_quote", "text": envelope}]
    evidence = (evidence[0], envelope + "\n\n" + evidence[1], evidence[2])
    record = _project(evidence)
    assert record is not None
    raw = deepcopy(evidence)
    result = _html_quoted_reply_presentation(
        evidence[1], evidence[0], forwarded_record=record
    )
    assert result["display_text"] == reply
    assert result["quoted_reply"]["speaker"] == "Earlier participant"
    assert result["quoted_reply"]["text"] == "Original question"
    assert evidence == raw
    tampered = deepcopy(record)
    tampered["title"] += " changed"
    assert (
        _html_quoted_reply_presentation(
            evidence[1], evidence[0], forwarded_record=tampered
        )
        == {}
    )
    assert (
        evidence[0]["primary_text_sha256"] == hashlib.sha256(reply.encode()).hexdigest()
    )


def test_html_forwarded_dom_reference_keeps_roles_without_a_native_type(tmp_path):
    quoted = message(
        "Actual reply",
        kind="",
        local_id="80_child",
        classes="chat left fmsg",
        native="original_sender",
    ).replace(
        '<span class="msg-text"><pre>Actual reply</pre></span>',
        '<span class="msg-text refermsg"><pre>「Earlier：Original text」</pre></span>'
        '<span class="msg-text"><pre>Actual reply</pre></span>',
    )
    parsed = parse_html_export(
        fixture_zip(
            tmp_path,
            nodes=[
                message("Forwarded title", kind="49", local_id="80"),
                boundary("<< History"),
                quoted,
                boundary("History Ends >>"),
            ],
        )
    )
    child = parsed["conversations"][0]["messages"][0]["forwarded_components"][0]
    assert child["type"] == ""
    assert child["text_kind"] == "quoted_reply"
    assert child["text"] == "Actual reply"
    assert child["quoted_components"] == [
        {"kind": "rendered_quote", "text": "「Earlier：Original text」"}
    ]
    assert "forwarded_original_type_unavailable" in child["flags"]


def test_legacy_news_digest_displays_articles_only_for_received_native_news(tmp_path):
    raw = (
        '<mmreader><category type="20" count="3"><name>腾讯新闻</name>'
        '<topnew><digest>Legacy cover summary</digest></topnew>'
        '<item><title>First headline</title><digest><![CDATA[First <b>summary</b>]]></digest>'
        '<url>https://example.invalid/article</url></item>'
        '<newitem><title>First headline</title><digest><![CDATA[First <b>summary</b>]]></digest>'
        '<url>https://example.invalid/article</url></newitem>'
        '<newitem><title>Second headline</title><digest></digest></newitem>'
        '<item><title>Distinct legacy headline</title></item>'
        '</category></mmreader>'
    )
    store = tmp_path / "store"
    import_wechat_archive(make_archive(tmp_path / "archive", repeated=3), store)
    account = "account:wxid_account_a"
    with sqlite3.connect(wechat_archive_path(store)) as connection:
        session = connection.execute(
            "SELECT id FROM conversations WHERE account_key=? AND kind='direct'", (account,)
        ).fetchone()[0]
        connection.execute("UPDATE conversations SET peer_id='newsapp' WHERE id=?", (session,))
        rows = connection.execute(
            "SELECT id,provenance FROM messages WHERE conversation_id=? ORDER BY ordinal", (session,)
        ).fetchall()
        source_types = (("newsapp", 1), ("wxid_peer", 1), ("newsapp", "1"))
        for index, ((identifier, original), (sender, message_type)) in enumerate(
            zip(rows, source_types, strict=True), start=1
        ):
            provenance = json.loads(original)
            provenance.update(
                message_type=message_type,
                text_extraction_kind="plain_text",
                server_id=str(index),
            )
            connection.execute(
                "UPDATE messages SET text=?,search_text=?,sender_id=?,direction='received',"
                "provenance=? WHERE id=?",
                (raw, raw.casefold(), sender, json.dumps(provenance), identifier),
            )

    result = query_wechat_history(store, account=account, session=session, persist_event_cache=False)
    items = {item.stable_id: item for item in result.items}
    native, spoofed_sender, spoofed_type = (items[identifier] for identifier, _ in rows)
    expected = (
        "腾讯新闻\n\nFirst headline\nFirst <b>summary</b>\n\n"
        "Second headline\n\nDistinct legacy headline"
    )
    assert native.messaging["display_text"] == expected
    assert native.content_text == spoofed_sender.content_text == spoofed_type.content_text == raw
    assert spoofed_sender.messaging["display_text"] == raw
    assert spoofed_type.messaging["display_text"] == raw

    environment = Environment(
        loader=FileSystemLoader("app/web/templates"), autoescape=select_autoescape(["html"])
    )
    environment.filters["wechat_source_clock"] = format_wechat_source_clock
    module = environment.get_template("_wechat_browser.html").make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])}
    )
    markup = parse_browser_markup(str(module.message_row(native)).encode())
    body = markup.find(".//div[@class='wechat-message-text']")
    assert body is not None and body.text == expected
    assert len(body) == 0
