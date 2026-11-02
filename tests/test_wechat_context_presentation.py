"""Nested rendered quotations retain source text and separate speaker roles.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import hashlib

from app.core.wechat_history_browser import (
    _html_quoted_reply_presentation,
    _present_html_forwarded_quotes,
)
from app.core.wechat_html_export import parse_html_export
from tests.test_wechat_html_export import boundary, fixture_zip, message
from tests.test_wechat_html_forwarded import _component, _evidence, _project


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
