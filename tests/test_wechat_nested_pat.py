"""Preserve observed nested forwarding wrappers and typed pat display evidence.

Code version: v1.0.1-codex.0
"""

from html import escape

import pytest

from app.core import wechat_text
from app.core.wechat_text import extract_wechat_text, is_native_pat_projection
from app.core.wechat_reconcile import native_text_metadata
from app.core.wechat_mac_refresh_projection import _project_message
from app.core.wechat_mac_refresh_source import RefreshError


def _app(kind, body):
    return f"<msg><appmsg><type>{kind}</type>{body}</appmsg></msg>"


def _record(items):
    return f"<recordinfo><datalist>{items}</datalist></recordinfo>"


def _text(value):
    return (
        '<dataitem datatype="1"><datadesc>' + escape(value) + "</datadesc></dataitem>"
    )


@pytest.mark.parametrize("representation", ["inline", "escaped", "cdata"])
def test_recordxml_wrapper_retains_nested_first_item_before_following_text(
    representation,
):
    nested = _record(_text("  Nested first\n\nLiteral • bullet  "))
    payload = (
        nested
        if representation == "inline"
        else escape(nested)
        if representation == "escaped"
        else "<![CDATA[" + nested + "]]>"
    )
    record = _record(
        '<dataitem datatype="17"><recordxml>'
        + payload
        + "</recordxml><datadesc>Inner history</datadesc></dataitem>"
        + _text("Following source item")
    )
    result = extract_wechat_text(
        49, _app(19, "<recorditem>" + record + "</recorditem>")
    )
    assert (
        result["text"]
        == "  Nested first\n\nLiteral • bullet  \n\nFollowing source item"
    )
    assert result["omitted_forwarded_items"] == 0
    group, following = result["text_components"]
    assert group["kind"] == "forwarded_group"
    assert group["forwarded_item_path"] == [1]
    assert group["forwarded_record_title"] == "Inner history"
    assert group["text_components"][0]["forwarded_item_path"] == [1, 1]
    assert following["forwarded_item_path"] == [2]


def test_recordxml_and_recordinfo_containers_can_nest_several_levels():
    record = _record(_text("Deep literal"))
    for depth in range(4):
        holder = "recordxml" if depth % 2 else "recordinfo"
        payload = escape(record) if holder == "recordxml" else record[12:-13]
        record = _record(
            f'<dataitem datatype="17"><{holder}>{payload}</{holder}></dataitem>'
        )
    result = extract_wechat_text(
        49, _app(19, "<recorditem>" + record + "</recorditem>")
    )
    assert result["text"] == "Deep literal"
    component = result["text_components"][0]
    path = [1]
    while component["kind"] == "forwarded_group":
        assert component["forwarded_item_path"] == path
        component = component["text_components"][0]
        path.append(1)
    assert component["forwarded_item_path"] == [1, 1, 1, 1, 1]


@pytest.mark.parametrize(
    "holder",
    [
        "<recordxml>{record}{record}</recordxml>",
        "<recordxml>conflicting prefix{record}</recordxml>",
        '<recordxml unexpected="1">{record}</recordxml>',
        "<recordxml>{record}</recordxml>{record}",
        "<recordxml><wrong>{record}</wrong></recordxml>",
        "<recordxml>&lt;!DOCTYPE recordinfo&gt;{record}</recordxml>",
    ],
)
def test_ambiguous_or_unsupported_nested_holder_keeps_other_items_and_gap(holder):
    nested = _record(_text("Must not be promoted"))
    record = _record(
        '<dataitem datatype="17">'
        + holder.format(record=nested)
        + "</dataitem>"
        + _text("Retained neighbor")
    )
    result = extract_wechat_text(
        49, _app(19, "<recorditem>" + record + "</recorditem>")
    )
    assert result["text"] == "Retained neighbor"
    assert result["omitted_forwarded_items"] == 1
    assert "forwarded_record_partially_unreadable" in result["flags"]
    assert result["text_components"][0]["forwarded_item_path"] == [2]


def test_recordxml_recursion_uses_existing_forwarding_depth_limit(monkeypatch):
    monkeypatch.setattr(wechat_text, "MAX_FORWARD_DEPTH", 1)
    nested = _record(_text("Out of budget"))
    for _ in range(2):
        nested = _record(
            '<dataitem datatype="17"><recordxml>'
            + escape(nested)
            + "</recordxml></dataitem>"
        )
    record = _record(
        _text("Retained")
        + '<dataitem datatype="17"><recordxml>'
        + escape(nested)
        + "</recordxml></dataitem>"
    )
    result = extract_wechat_text(
        49, _app(19, "<recorditem>" + record + "</recorditem>")
    )
    assert result["text"] == "Retained"
    assert result["omitted_forwarded_items"] == 1
    assert "forwarded_record_partially_unreadable" in result["flags"]


@pytest.mark.parametrize("representation", ["escaped", "cdata", "inline"])
@pytest.mark.parametrize("inner_reference", [True, False])
def test_a_quoted_reply_uses_the_embedded_replies_own_literal_body(
    representation, inner_reference
):
    body = "@42\u2005引用正文\n  Literal • detail  "
    embedded = _app(
        57,
        "<title>"
        + escape(body)
        + "</title>"
        + (
            "<refermsg><type>1</type><content>Do not misattribute inner words</content>"
            "<svrid>41</svrid><fromusr>wxid_inner_author</fromusr></refermsg>"
            if inner_reference
            else ""
        ),
    )
    payload = (
        escape(embedded)
        if representation == "escaped"
        else "<![CDATA[" + embedded + "]]>"
        if representation == "cdata"
        else embedded
    )
    outer = _app(
        57,
        "<title>✔️</title><refermsg><type>49</type>"
        "<fromusr>12345@chatroom</fromusr><chatusr>wxid_outer_original</chatusr>"
        "<displayname>Original speaker</displayname><createtime>1700000042</createtime>"
        "<svrid>9223372036854775806</svrid><content>"
        + payload
        + "</content></refermsg>",
    )
    result = extract_wechat_text(49, outer)
    assert result["text"] == "✔️\n\n" + body
    quoted = result["text_components"][1]
    assert quoted["kind"] == "quoted_text"
    assert quoted["original_message_type"] == 49
    assert quoted["appmsg_subtype"] == 57
    assert quoted["source_sender_id"] == "12345@chatroom"
    assert quoted["source_chat_id"] == "wxid_outer_original"
    assert quoted["source_ids"] == {"svrid": "9223372036854775806"}
    assert "inner words" not in result["text"]
    assert "wxid_inner_author" not in str(result)
    assert "quoted_native_reply_body" in result["flags"]
    native_text_metadata(result, {})


@pytest.mark.parametrize(
    "embedded",
    [
        _app(57, "<title>one</title><title>two</title>"),
        _app(57, "<title><hidden>not a literal body</hidden></title>"),
        _app(57, "<title> </title>"),
        _app(6, "<title>private-file.pdf</title>"),
        _app(5, "<title>Unknown app title</title>"),
        "<msg><appmsg><type>57</type><title>unterminated",
    ],
)
def test_unknown_or_ambiguous_quoted_app_titles_remain_unavailable(embedded):
    result = extract_wechat_text(
        49,
        _app(
            57,
            "<title>Reply</title><refermsg><type>49</type><content>"
            + escape(embedded)
            + "</content></refermsg>",
        ),
    )
    assert result["text"] == "Reply"
    assert result["text_components"] == [{"kind": "reply_text", "text": "Reply"}]


def _pat(
    *,
    title="Actor tickled Target's shoulder",
    actor="wxid_actor",
    target="wxid_target",
    chat="12345@chatroom",
    suffix="shoulder",
    version="2",
    template=None,
):
    template = (
        template
        if template is not None
        else "${" + actor + "} tickled ${" + target + "}'s " + suffix
    )
    return _app(
        62,
        "<title>"
        + escape(title)
        + "</title><patinfo>"
        + "<fromusername>"
        + escape(actor)
        + "</fromusername>"
        + "<chatusername>"
        + escape(chat)
        + "</chatusername>"
        + "<pattedusername>"
        + escape(target)
        + "</pattedusername>"
        + "<patsuffix>"
        + escape(suffix)
        + "</patsuffix>"
        + "<patsuffixversion>"
        + escape(version)
        + "</patsuffixversion>"
        + "<template>"
        + escape(template)
        + "</template></patinfo>"
        + "<appattach><aeskey>DO_NOT_LEAK_KEY</aeskey></appattach>"
        + "<url>https://invalid.example/private-object</url>",
    )


def test_pat_preserves_literal_display_and_separate_native_participant_assertions():
    title = "  Actor tickled Target's <b>literal</b> shoulder\n"
    result = extract_wechat_text(49, _pat(title=title))
    assert result == {
        "text": title,
        "text_extraction_kind": "system_text",
        "original_message_type": 49,
        "appmsg_subtype": 62,
        "flags": ["system_notice"],
        "system_message_subtype": "pat",
        "system_source_ids": {
            "patinfo/fromusername": "wxid_actor",
            "patinfo/chatusername": "12345@chatroom",
            "patinfo/pattedusername": "wxid_target",
        },
    }
    assert "DO_NOT_LEAK" not in str(result) and "private-object" not in str(result)


def test_pat_preserves_literal_display_when_the_suffix_is_absent():
    title = "Actor tickled Target"
    result = extract_wechat_text(
        49,
        _pat(title=title, template="${wxid_actor} tickled ${wxid_target}").replace(
            "<patsuffix>shoulder</patsuffix>", ""
        ),
    )
    assert result["text"] == title
    assert result["system_message_subtype"] == "pat"


def test_self_pat_keeps_both_explicit_participant_slots():
    result = extract_wechat_text(49, _pat(target="wxid_actor"))
    assert (
        result["system_source_ids"]["patinfo/fromusername"]
        == result["system_source_ids"]["patinfo/pattedusername"]
    )


@pytest.mark.parametrize(
    "change",
    [
        {"version": "1"},
        {"version": ""},
        {"actor": ""},
        {"target": "display name"},
        {"chat": "wrong room"},
        {"title": ""},
        {"template": "${wxid_target} tickled ${wxid_actor}'s shoulder"},
        {"template": "${wxid_actor} tickled ${wxid_target} ${wxid_other}'s shoulder"},
        {"template": "${wxid_actor} tickled ${wxid_target}'s conflicting suffix"},
    ],
)
def test_unproved_pat_profiles_remain_unsupported(change):
    assert extract_wechat_text(49, _pat(**change)) is None


@pytest.mark.parametrize(
    "old,new",
    [
        ("<patinfo>", '<patinfo extra="1">'),
        (
            "<fromusername>wxid_actor</fromusername>",
            "<fromusername>wxid_actor</fromusername><fromusername>wxid_other</fromusername>",
        ),
        (
            "<patsuffix>shoulder</patsuffix>",
            "<patsuffix><hidden>shoulder</hidden></patsuffix>",
        ),
        ("</patinfo>", "<extra>unknown</extra></patinfo>"),
        ("<type>62</type>", "<type>1</type>"),
    ],
)
def test_pat_does_not_guess_roles_from_ambiguous_structures(old, new):
    result = extract_wechat_text(49, _pat().replace(old, new))
    assert result is None or result.get("system_message_subtype") != "pat"


def test_pat_projection_is_a_closed_exact_shape_with_optional_room_binding():
    native = extract_wechat_text(49, _pat())
    assert is_native_pat_projection(native, 49 | (62 << 32), peer_id="12345@chatroom")
    assert is_native_pat_projection(native, 49)
    assert not is_native_pat_projection(native, 49 | (57 << 32))
    assert not is_native_pat_projection(native, True)
    assert not is_native_pat_projection(native, 10000)
    assert not is_native_pat_projection(native, 49, peer_id="99999@chatroom")
    assert not is_native_pat_projection({**native, "extra": "unproved"}, 49)
    assert not is_native_pat_projection(
        {**native, "flags": ["system_notice", "extra"]}, 49
    )
    assert not is_native_pat_projection({**native, "text": "\x00hidden"}, 49)
    assert not is_native_pat_projection(
        {
            **native,
            "system_source_ids": {**native["system_source_ids"], "extra": "unproved"},
        },
        49,
    )


def _native_row(body, *, local_type=49 | (62 << 32)):
    return {
        "local_id": 42,
        "local_type": local_type,
        "server_id": 9223372036854775806,
        "create_time": 1700000042,
        "real_sender_id": 18,
        "message_content": body,
        "compression_marker": 0,
    }


def test_pat_internal_sender_remains_a_source_coordinate_without_a_human_role():
    names = {18: {"rowid": 18, "user_name": "", "is_session": 0}}
    result = _project_message(
        _native_row(_pat()),
        "wxid_owner",
        "group",
        names,
        peer_id="12345@chatroom",
    )
    assert result["real_sender_id"] == 18
    assert result["sender_id"] == ""
    assert result["sender_label"] == "System Message"
    assert result["direction"] == "unknown"
    assert "sender_proof" not in result
    assert (
        result["native_text"]["system_source_ids"]["patinfo/fromusername"]
        == "wxid_actor"
    )
    assert result["server_id"] == "9223372036854775806"


@pytest.mark.parametrize(
    "body,local_type",
    [
        (_app(57, "<title>Reply</title>"), 49 | (57 << 32)),
        (_pat(version="1"), 49 | (62 << 32)),
        ("Ordinary text", 1),
    ],
)
def test_pat_support_does_not_exempt_ordinary_or_unknown_messages_from_sender_proof(
    body, local_type
):
    with pytest.raises(RefreshError, match="authenticated sender"):
        _project_message(
            _native_row(body, local_type=local_type), "wxid_owner", "group", {}
        )


def test_pat_cannot_cross_a_verified_conversation_or_native_subtype():
    with pytest.raises(RefreshError, match="conversation"):
        _project_message(
            _native_row(_pat()), "wxid_owner", "group", {}, peer_id="99999@chatroom"
        )
    with pytest.raises(RefreshError, match="typed source"):
        _project_message(
            _native_row(_pat(), local_type=49 | (57 << 32)), "wxid_owner", "group", {}
        )
