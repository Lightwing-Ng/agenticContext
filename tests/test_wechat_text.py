"""Known text envelopes retain source evidence without accessing media.

Code version: v1.4.1-codex.0
"""

from __future__ import annotations

from html import escape
from xml.etree import ElementTree

import pytest

from app.core import wechat_text
from app.core.wechat_text import extract_wechat_text, project_system_notice_text


def _app(subtype: str | int, body: str) -> str:
    return f"<msg><appmsg><type>{subtype}</type>{body}</appmsg></msg>"


def _record(items: str) -> str:
    return f"<recordinfo><datalist>{items}</datalist></recordinfo>"


def test_plain_text_remains_exact_and_is_never_interpreted_as_xml():
    raw = "  你好😀\r\n第二行\n<appmsg>&literal; 9223372036854775807\n"
    result = extract_wechat_text(1, raw)
    assert result == {
        "text": raw,
        "text_extraction_kind": "plain_text",
        "original_message_type": 1,
        "appmsg_subtype": None,
        "flags": [],
    }


@pytest.mark.parametrize("message_type", [3, 34, 42, 43, 47, 48, 50, 64, 9999])
def test_non_text_types_are_excluded_even_when_their_payload_has_readable_labels(
    message_type,
):
    assert extract_wechat_text(message_type, "confidential-file.pdf") is None


@pytest.mark.parametrize(
    "message_type,raw", [(True, "text"), ("1", "text"), (1, None), (1, " \n")]
)
def test_invalid_inputs_and_empty_records_have_no_text(message_type, raw):
    assert extract_wechat_text(message_type, raw) is None


def test_app_text_retains_unicode_newlines_and_literal_html_without_rendering():
    raw = _app(
        1, "<title><![CDATA[Hello <b>world</b>\n你好]]></title><des>说明&amp;补充</des>"
    )
    result = extract_wechat_text(49, raw)
    assert result["text"] == "Hello <b>world</b>\n你好\n说明&补充"
    assert result["original_message_type"] == 49
    assert result["appmsg_subtype"] == 1
    assert result["text_extraction_kind"] == "app_text"


def test_identical_app_title_and_description_are_not_repeated():
    assert (
        extract_wechat_text(49, _app(1, "<title>hello</title><des>hello</des>"))["text"]
        == "hello"
    )


@pytest.mark.parametrize("subtype", [2, 3, 4, 5, 6, 8, 24, 51, 2000])
def test_app_media_file_and_link_metadata_is_not_treated_as_conversation_text(subtype):
    assert (
        extract_wechat_text(
            49,
            _app(subtype, "<title>private.pdf</title><des>human-looking label</des>"),
        )
        is None
    )


@pytest.mark.parametrize("representation", ["cdata", "escaped", "inline"])
def test_forwarded_records_keep_text_and_metadata_only_resources_in_source_order(
    representation,
):
    record = _record(
        '<dataitem datatype="1" dataid="18446744073709551615">'
        "<sourcename>Forwarded speaker</sourcename><sourcetime>source clock</sourcetime>"
        "<srcMsgCreateTime>9007199254740993</srcMsgCreateTime>"
        "<dataitemsource><fromusr>wxid_original_sender</fromusr><msgid>9223372036854775807</msgid></dataitemsource>"
        "<datadesc>文字😀\n下一行</datadesc></dataitem>"
        '<dataitem datatype="2"><datadesc>photo caption must not leak</datadesc><fullpath>/private/image.jpg</fullpath></dataitem>'
        '<dataitem datatype="8"><datadesc>file caption must not leak</datadesc><datatitle>private.pdf</datatitle></dataitem>'
        '<dataitem datatype="1"><datadesc>second text</datadesc></dataitem>'
    )
    payload = (
        f"<![CDATA[{record}]]>"
        if representation == "cdata"
        else escape(record)
        if representation == "escaped"
        else record
    )
    result = extract_wechat_text(
        49,
        _app(19, f"<title>Forwarded record</title><recorditem>{payload}</recorditem>"),
    )
    assert result["text"] == "文字😀\n下一行\n\nsecond text"
    assert result["original_message_type"] == 49
    assert result["appmsg_subtype"] == 19
    assert result["omitted_forwarded_items"] == 0
    assert len(result["text_components"]) == 4
    assert [item["kind"] for item in result["text_components"]] == [
        "forwarded_text",
        "forwarded_resource",
        "forwarded_resource",
        "forwarded_text",
    ]
    assert [item["resource_kind"] for item in result["text_components"][1:3]] == [
        "image",
        "file",
    ]
    first = result["text_components"][0]
    assert first["forwarded_item_path"] == [1]
    assert first["source_data_id"] == "18446744073709551615"
    assert first["source_ids"] == {"dataitemsource/msgid": "9223372036854775807"}
    assert first["source_create_time"] == "9007199254740993"
    assert first["source_sender_id"] == "wxid_original_sender"
    assert "conversation_id" not in first
    assert "private.pdf" not in str(result) and "photo caption" not in str(result)


def test_nested_forwarding_keeps_a_locator_without_promoting_nested_messages():
    nested = _record(
        '<dataitem datatype="1"><datadesc>nested text</datadesc></dataitem>'
    )
    record = _record(f'<dataitem datatype="17">{nested}</dataitem>')
    result = extract_wechat_text(49, _app(19, f"<recorditem>{record}</recorditem>"))
    assert result["text"] == "nested text"
    group = result["text_components"][0]
    assert group["kind"] == "forwarded_group"
    assert group["forwarded_item_path"] == [1]
    assert group["text_components"][0]["forwarded_item_path"] == [1, 1]
    assert "forwarded_text_only" in result["flags"]


def test_forwarding_depth_limit_preserves_other_readable_text(monkeypatch):
    monkeypatch.setattr(wechat_text, "MAX_FORWARD_DEPTH", 1)
    nested = _record(
        '<dataitem datatype="17">'
        + _record('<dataitem datatype="1"><datadesc>too deep</datadesc></dataitem>')
        + "</dataitem>"
    )
    record = _record(
        '<dataitem datatype="1"><datadesc>retained</datadesc></dataitem>'
        + f'<dataitem datatype="17">{nested}</dataitem>'
    )
    result = extract_wechat_text(49, _app(19, f"<recorditem>{record}</recorditem>"))
    assert result["text"] == "retained"
    assert result["omitted_forwarded_items"] == 1
    assert "forwarded_record_partially_unreadable" in result["flags"]


def test_all_media_forwarding_does_not_import_the_outer_record_title():
    record = _record('<dataitem datatype="2"><datadesc>image</datadesc></dataitem>')
    assert (
        extract_wechat_text(
            49,
            _app(19, f"<title>record title</title><recorditem>{record}</recorditem>"),
        )
        is None
    )


def test_mixed_nested_media_retains_boundary_and_each_items_own_metadata():
    nested = _record(
        '<dataitem datatype="2"><sourcename>Image author</sourcename>'
        "<srcMsgCreateTime>1628330403</srcMsgCreateTime>"
        "<dataitemsource><fromusr>wxid_image_author</fromusr></dataitemsource>"
        "<datadesc>Never a caption utterance</datadesc><fullpath>/private/photo</fullpath></dataitem>"
    )
    record = _record(
        '<dataitem datatype="1"><datadesc>Before</datadesc></dataitem>'
        '<dataitem datatype="17"><sourcename>Nested forwarder</sourcename>'
        "<sourcetime>Own wrapper clock</sourcetime><datadesc>Nested title</datadesc>"
        + nested
        + '</dataitem><dataitem datatype="1"><datadesc>After</datadesc></dataitem>'
    )
    result = extract_wechat_text(49, _forwarded_app(record))
    assert result["text"] == "Before\n\nAfter"
    first, group, last = result["text_components"]
    assert [first["text"], group["text"], last["text"]] == ["Before", "", "After"]
    assert group["kind"] == "forwarded_group"
    assert group["source_sender_label"] == "Nested forwarder"
    assert group["source_timestamp"] == "Own wrapper clock"
    assert group["forwarded_record_title"] == "Nested title"
    image = group["text_components"][0]
    assert image["text"] == "" and image["resource_kind"] == "image"
    assert image["source_sender_id"] == "wxid_image_author"
    assert image["source_create_time"] == "1628330403"
    assert image["forwarded_item_path"] == [2, 1]
    assert "Never a caption" not in str(result) and "/private/photo" not in str(result)


def test_explicit_quote_inside_forwarding_preserves_roles_and_literal_xml_stays_text():
    reply = "  Current reply，😀\nsecond line  "
    quote = _app(
        57,
        f"<title>{escape(reply)}</title><refermsg><type>1</type>"
        "<fromusr>wxid_prior</fromusr><displayname>Earlier participant</displayname>"
        "<content>Original &lt;b&gt;literal&lt;/b&gt; text</content></refermsg>",
    )
    record = _record(
        '<dataitem datatype="1"><sourcename>Current participant</sourcename>'
        + quote
        + '</dataitem><dataitem datatype="1"><datadesc>'
        + escape(quote)
        + "</datadesc></dataitem>"
        '<dataitem datatype="8">' + quote + "</dataitem>"
    )
    result = extract_wechat_text(49, _forwarded_app(record))
    current, literal, file = result["text_components"]
    assert current["kind"] == "forwarded_quote"
    assert current["source_sender_label"] == "Current participant"
    assert current["text_components"][0] == {"kind": "reply_text", "text": reply}
    assert current["quoted_reference"]["source_sender_id"] == "wxid_prior"
    assert literal["kind"] == "forwarded_text" and literal["text"] == quote
    assert file["kind"] == "forwarded_resource" and file["resource_kind"] == "file"
    assert result["text"] == current["text"] + "\n\n" + quote


def test_nested_forwarded_long_text_remains_exact_without_display_truncation():
    text = "  Literal & <img> 😀\n" * 4_000 + "THE FINAL CHARACTER"
    nested = _record(
        '<dataitem datatype="1"><datadesc>' + escape(text) + "</datadesc></dataitem>"
    )
    record = _record('<dataitem datatype="17">' + nested + "</dataitem>")
    result = extract_wechat_text(49, _forwarded_app(record))
    assert result["text"] == text
    assert result["text_components"][0]["text_components"][0]["text"] == text


def test_explicit_quote_cannot_replace_a_different_existing_datadesc_body():
    original = "  Previously extracted literal body，😀  "
    xml = _forwarded_app(
        _record(
            '<dataitem datatype="1"><datadesc>' + escape(original) + "</datadesc>"
            "<appmsg><type>57</type><title>A different reply</title><refermsg><type>1</type>"
            "<content>Quoted content</content></refermsg></appmsg></dataitem>"
        )
    )
    result = extract_wechat_text(49, xml)
    assert result["text"] == original
    assert result["text_components"][0]["kind"] == "forwarded_text"
    assert "forwarded_record_partially_unreadable" in result["flags"]


def test_explicit_quotes_inside_groups_share_the_total_forwarding_depth_budget(
    monkeypatch,
):
    monkeypatch.setattr(wechat_text, "MAX_FORWARD_DEPTH", 1)
    deeper = _forwarded_app(
        _record('<dataitem datatype="1"><datadesc>Too deep text</datadesc></dataitem>')
    )
    quote = _quoted_app(deeper, reply="Retained reply")
    grouped = _record('<dataitem datatype="1">' + quote + "</dataitem>")
    result = extract_wechat_text(
        49,
        _forwarded_app(_record('<dataitem datatype="17">' + grouped + "</dataitem>")),
    )
    assert result["text"] == "Retained reply"
    assert "Too deep text" not in str(result)


def test_quoted_text_retains_reply_and_source_only_reference_metadata():
    raw = _app(
        57,
        "<title>我的回复\n第二行</title><refermsg><type>1</type><fromusr>wxid_quoted</fromusr><displayname>Earlier speaker</displayname><svrid>18446744073709551615</svrid><createtime>9007199254740993</createtime><content><![CDATA[Earlier <i>literal</i> text]]></content></refermsg>",
    )
    result = extract_wechat_text(49, raw)
    assert result["text"] == "我的回复\n第二行\n\nEarlier <i>literal</i> text"
    assert [part["kind"] for part in result["text_components"]] == [
        "reply_text",
        "quoted_text",
    ]
    assert result["quoted_reference"]["source_ids"] == {"svrid": "18446744073709551615"}
    assert result["quoted_reference"]["source_timestamp"] == "9007199254740993"
    assert result["original_message_type"] == 49


@pytest.mark.parametrize("quoted_type", [3, 43, 49])
def test_quoted_non_text_content_and_nested_markup_are_not_imported(quoted_type):
    raw = _app(
        57,
        f"<title>Actual reply</title><refermsg><type>{quoted_type}</type><content><![CDATA[<msg><appmsg><title>private.pdf</title></appmsg></msg>]]></content></refermsg>",
    )
    result = extract_wechat_text(49, raw)
    assert result["text"] == "Actual reply"
    assert len(result["text_components"]) == 1
    assert "quoted_non_text_content_omitted" in result["flags"]


def _quoted_app(content, *, representation="escaped", reply="Actual reply"):
    payload = (
        f"<![CDATA[{content}]]>"
        if representation == "cdata"
        else escape(content)
        if representation == "escaped"
        else content
    )
    return _app(
        57,
        f"<title>{escape(reply)}</title><refermsg><type>49</type>"
        "<fromusr>wxid_forwarder</fromusr><displayname>Forwarding speaker</displayname>"
        "<chatusr>wxid_source_chat</chatusr><createtime>9007199254740993</createtime>"
        "<svrid>18446744073709551615</svrid>"
        f"<content>{payload}</content></refermsg>",
    )


def _forwarded_app(record):
    return _app(
        19,
        "<title>A forwarded conversation</title>"
        f"<recorditem>{escape(record)}</recorditem>",
    )


@pytest.mark.parametrize("representation", ["cdata", "escaped", "inline"])
def test_quoted_forwarded_text_keeps_reply_forwarder_and_original_author_separate(
    representation,
):
    original = '<img src=x onerror="alert(1)"> & literal 😀\nnext line'
    record = _record(
        '<dataitem datatype="1" dataid="original-item">'
        "<sourcename>Original participant</sourcename>"
        "<dataitemsource><fromusr>wxid_original</fromusr>"
        "<msgid>9223372036854775807</msgid></dataitemsource>"
        f"<datadesc>{escape(original)}</datadesc></dataitem>"
        '<dataitem datatype="8"><datatitle>private.pdf</datatitle></dataitem>'
    )
    reply = "  My <b>literal</b> reply\nsecond line  "
    result = extract_wechat_text(
        49,
        _quoted_app(_forwarded_app(record), representation=representation, reply=reply),
    )
    assert result["text"] == reply + "\n\n" + original
    assert result["text_extraction_kind"] == "quoted_reply"
    assert result["appmsg_subtype"] == 57
    assert result["text_components"][0] == {"kind": "reply_text", "text": reply}
    reference = result["quoted_reference"]
    assert reference["source_sender_id"] == "wxid_forwarder"
    assert reference["source_sender_label"] == "Forwarding speaker"
    assert reference["source_ids"] == {"svrid": "18446744073709551615"}
    quoted = result["text_components"][1]
    assert quoted["kind"] == "quoted_forwarded_text"
    assert quoted["appmsg_subtype"] == 19
    assert quoted["source_sender_id"] == reference["source_sender_id"]
    assert quoted["forwarded_record_title"] == "A forwarded conversation"
    assert quoted["omitted_forwarded_items"] == 0
    original_part = quoted["text_components"][0]
    assert original_part["kind"] == "forwarded_text"
    assert original_part["text"] == original
    assert original_part["source_sender_id"] == "wxid_original"
    assert original_part["source_sender_label"] == "Original participant"
    assert original_part["source_ids"] == {
        "dataitemsource/msgid": "9223372036854775807"
    }
    assert original_part["forwarded_item_path"] == [1]
    assert "quoted_forwarded_text_only" in result["flags"]
    assert "quoted_forwarded_resource_metadata_only" in result["flags"]
    assert "quoted_non_text_content_omitted" not in result["flags"]
    assert "private.pdf" not in str(result)
    assert "conversation_id" not in quoted and "sender_id" not in quoted


@pytest.mark.parametrize(
    "content",
    [
        _app(6, "<title>private.pdf</title>"),
        _app(57, "<title>Another reply must not become this reply</title>"),
        '<recordinfo><datalist><dataitem datatype="1"><datadesc>Not explicit subtype 19</datadesc></dataitem></datalist></recordinfo>',
        "<config>"
        + _forwarded_app(
            _record(
                '<dataitem datatype="1"><datadesc>not a message root</datadesc></dataitem>'
            )
        )
        + "</config>",
        _app(19, "<type>19</type><recorditem>ambiguous</recorditem>"),
    ],
)
def test_quoted_app_whitelist_does_not_promote_unknown_media_or_ambiguous_content(
    content,
):
    result = extract_wechat_text(49, _quoted_app(content))
    assert result["text"] == "Actual reply"
    assert result["text_components"] == [{"kind": "reply_text", "text": "Actual reply"}]
    assert "quoted_non_text_content_omitted" in result["flags"]


def test_plain_quoted_xml_looking_text_is_not_reinterpreted_as_forwarding():
    content = _forwarded_app(
        _record('<dataitem datatype="1"><datadesc>literal XML</datadesc></dataitem>')
    )
    raw = _quoted_app(content).replace(
        "<refermsg><type>49</type>", "<refermsg><type>1</type>"
    )
    result = extract_wechat_text(49, raw)
    assert result["text_components"][1]["kind"] == "quoted_text"
    assert result["text_components"][1]["text"] == content


def test_text_reply_to_all_media_forwarding_retains_metadata_without_adding_body_text():
    result = extract_wechat_text(
        49,
        _quoted_app(
            _forwarded_app(
                _record(
                    '<dataitem datatype="2"><datadesc>Not text</datadesc></dataitem>'
                )
            )
        ),
    )
    assert result["text"] == "Actual reply"
    quoted = result["text_components"][1]
    assert quoted["kind"] == "quoted_forwarded_text" and quoted["text"] == ""
    assert quoted["text_components"][0]["resource_kind"] == "image"
    assert "Not text" not in str(result)


def test_quoted_forwarding_shares_xml_byte_and_node_budgets(monkeypatch):
    record = _record(
        '<dataitem datatype="1"><datadesc>Embedded text</datadesc></dataitem>'
    )
    forwarded = _forwarded_app(record)
    raw = _quoted_app(forwarded)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            wechat_text,
            "MAX_XML_BYTES",
            len(raw.encode()) + len(forwarded.encode()) - 1,
        )
        result = extract_wechat_text(49, raw)
        assert result["text"] == "Actual reply"
        assert len(result["text_components"]) == 1
    with monkeypatch.context() as scoped:
        outer_nodes = sum(1 for _ in ElementTree.fromstring(raw).iter())
        inner_nodes = sum(1 for _ in ElementTree.fromstring(forwarded).iter())
        scoped.setattr(wechat_text, "MAX_XML_NODES", outer_nodes + inner_nodes - 1)
        result = extract_wechat_text(49, raw)
        assert result["text"] == "Actual reply"
        assert len(result["text_components"]) == 1
    result = extract_wechat_text(49, raw)
    assert result["text"] == "Actual reply\n\nEmbedded text"


def test_quoted_forwarding_rejects_nested_dtd_without_opening_external_resources(
    monkeypatch,
):
    declaration = '<!DOCTYPE msg [<!ENTITY leak SYSTEM "file:///private/secret">]>'
    content = declaration + _app(19, "<title>&leak;</title>")
    raw = _quoted_app(content)

    def forbidden(*args, **kwargs):
        raise AssertionError("Quoted forwarding must not open external resources")

    with monkeypatch.context() as scoped:
        scoped.setattr("builtins.open", forbidden)
        result = extract_wechat_text(49, raw)
    assert result["text"] == "Actual reply"
    assert len(result["text_components"]) == 1


@pytest.mark.parametrize("message_type", [10000, 10002])
def test_plain_and_recognized_xml_system_display_text_are_retained(message_type):
    raw = "你撤回了一条消息\n保留换行"
    assert extract_wechat_text(message_type, raw)["text"] == raw
    xml = '<sysmsg type="revokemsg"><revokemsg><newmsgid>9223372036854775807</newmsgid><replacemsg>某人撤回了一条消息</replacemsg></revokemsg></sysmsg>'
    result = extract_wechat_text(message_type, xml)
    assert result["text"] == "某人撤回了一条消息"
    assert result["original_message_type"] == message_type
    assert result["system_message_subtype"] == "revokemsg"
    assert result["system_source_ids"] == {"revokemsg/newmsgid": "9223372036854775807"}


def test_system_template_only_uses_its_explicit_plain_display_field():
    xml = '<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template><plain>Display notice</plain><template>$unresolved$</template></content_template></sysmsgtemplate></sysmsg>'
    assert extract_wechat_text(10000, xml)["text"] == "Display notice"


def _system_template(
    *,
    template="$username$ invited you to a group chat with $others$",
    nickname="Inviting member",
    others="Member A, Member B",
):
    return (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
        '<content_template type="tmpl_type_profile"><plain/>'
        f"<template>{escape(template)}</template><link_list>"
        '<link name="username" type="link_profile"><memberlist><member>'
        "<username>wxid_inviter</username>"
        f"<nickname>{escape(nickname)}</nickname>"
        "</member></memberlist></link>"
        '<link name="others" type="link_plain">'
        f"<plain>{escape(others)}</plain></link>"
        "</link_list></content_template></sysmsgtemplate></sysmsg>"
    )


@pytest.mark.parametrize("message_type", [10000, 10002])
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_system_invitation_template_uses_display_fields_and_verified_room(
    message_type, ending
):
    xml = _system_template()
    raw = "\ufeff 123456789@chatroom:\t" + ending + xml
    expected = "Inviting member invited you to a group chat with Member A, Member B"
    assert project_system_notice_text(raw, peer_id="123456789@chatroom") == expected
    result = extract_wechat_text(message_type, raw)
    assert result["text"] == expected
    assert result["original_message_type"] == message_type
    assert result["system_message_subtype"] == "sysmsgtemplate"
    assert result["flags"] == ["system_notice"]
    assert "wxid_inviter" not in result["text"]
    assert project_system_notice_text(raw, peer_id="987654321@chatroom") is None
    assert project_system_notice_text(xml, peer_id="123456789@chatroom") == expected
    assert extract_wechat_text(1, raw)["text"] == raw


def test_system_template_substitution_is_literal_and_preserves_source_punctuation():
    xml = _system_template(
        template="  $username$ / $username$\n邀请你加入群聊：$others$。 ",
        nickname="<script>& literal $others$",
        others="Member A、Member B",
    )
    assert project_system_notice_text(xml) == (
        "  <script>& literal $others$ / <script>& literal $others$\n"
        "邀请你加入群聊：Member A、Member B。 "
    )


def _system_profile_link(name, members, *, separator=None):
    member_xml = "".join(
        "<member>"
        f"<username>{escape(username)}</username>"
        f"<nickname>{escape(nickname)}</nickname></member>"
        for username, nickname in members
    )
    separator_xml = (
        f"<separator>{escape(separator)}</separator>" if separator is not None else ""
    )
    return (
        f'<link name="{name}" type="link_profile"><memberlist>'
        + member_xml
        + "</memberlist>"
        + separator_xml
        + "</link>"
    )


def _system_link_template(template, links, *, content_type="tmpl_type_profile"):
    return (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
        f'<content_template type="{content_type}"><plain/>'
        f"<template>{escape(template)}</template><link_list>{links}</link_list>"
        "</content_template></sysmsgtemplate></sysmsg>"
    )


@pytest.mark.parametrize("separator", [",", "、", "\n / "])
def test_system_profile_lists_use_only_their_explicit_literal_separator(separator):
    links = _system_profile_link(
        "names",
        [("wxid_member_a", "Member A"), ("wxid_member_b", "Member B")],
        separator=separator,
    )
    xml = _system_link_template("You invited $names$ to the group chat.", links)
    expected = "You invited Member A" + separator + "Member B to the group chat."
    assert project_system_notice_text(xml) == expected
    assert extract_wechat_text(10002, xml)["text"] == expected


@pytest.mark.parametrize(
    "links",
    [
        _system_profile_link(
            "names", [("wxid_member_a", "Member A"), ("wxid_member_b", "Member B")]
        ),
        _system_profile_link(
            "names", [("wxid_member_a", "Member A")], separator=","
        ).replace("</link>", "<separator>,</separator></link>"),
        _system_profile_link(
            "names", [("wxid_member_a", "Member A")], separator=","
        ).replace(
            "<separator>,</separator>", "<separator><plain>,</plain></separator>"
        ),
        _system_profile_link(
            "names",
            [("wxid_member_a", "Member A"), ("wxid_member_a", "Other label")],
            separator=",",
        ),
    ],
)
def test_ambiguous_system_profile_lists_remain_unprojected(links):
    xml = _system_link_template("You invited $names$ to the group chat.", links)
    assert project_system_notice_text(xml) is None
    assert extract_wechat_text(10002, xml) is None


@pytest.mark.parametrize(
    ("content_type", "action"),
    [
        (
            "tmpl_type_profilewithrevoke",
            '<link name="revoke" type="link_revoke" hidden="1"><title>Revoke</title>'
            "<usernamelist><username>wxid_member_a</username>"
            "<username>wxid_member_b</username></usernamelist></link>",
        ),
        (
            "tmpl_type_profilewithrevokeqrcode",
            '<link name="revoke" type="link_revoke_qrcode" hidden="1">'
            "<title>Revoke</title><username>wxid_member_a</username>"
            "<qrcodeURL>https://example.invalid/group?secret=opaque</qrcodeURL></link>",
        ),
    ],
)
def test_hidden_system_revoke_actions_are_omitted_without_exposing_their_targets(
    monkeypatch, content_type, action
):
    links = _system_profile_link(
        "names",
        [("wxid_member_a", "Member A"), ("wxid_member_b", "Member B")],
        separator=",",
    )
    xml = _system_link_template(
        "You invited $names$ to the group chat.   $revoke$",
        links + action,
        content_type=content_type,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Hidden actions must not open external resources")

    with monkeypatch.context() as scoped:
        scoped.setattr("builtins.open", forbidden)
        assert project_system_notice_text(xml) == (
            "You invited Member A,Member B to the group chat.   "
        )
        assert extract_wechat_text(10002, xml)["text"] == (
            "You invited Member A,Member B to the group chat.   "
        )
    for old, new in [
        ('hidden="1"', 'hidden="0"'),
        ('name="revoke"', 'name="unknown"'),
        ('type="' + content_type + '"', 'type="tmpl_type_profile"'),
        ("<title>Revoke</title>", "<title><nested>Revoke</nested></title>"),
    ]:
        assert project_system_notice_text(xml.replace(old, new)) is None


def test_system_profile_join_has_its_own_expanded_utf8_budget(monkeypatch):
    links = _system_profile_link(
        "names",
        [(f"wxid_member_{index}", f"Member {index}") for index in range(6)],
        separator="中" * 400,
    )
    xml = _system_link_template("You invited $names$.", links)
    assert project_system_notice_text(xml) == (
        "You invited "
        + ("中" * 400).join(f"Member {index}" for index in range(6))
        + "."
    )
    monkeypatch.setattr(wechat_text, "MAX_XML_BYTES", 4096)
    assert len(xml.encode("utf-8")) < wechat_text.MAX_XML_BYTES
    assert project_system_notice_text(xml) is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('type="tmpl_type_profile"', 'type="unknown_template"'),
        ("<plain/>", "<plain/><plain/>"),
        ("<template>", "<template/><template>"),
        ("<link_list>", "<link_list/><link_list>"),
        ('name="others"', 'name="username"'),
        ('type="link_plain"', 'type="link_unknown"'),
        ("<plain>Member A, Member B</plain>", "<plain><a>Member A</a></plain>"),
        ("<nickname>Inviting member</nickname>", "<nickname/>"),
        ("<username>wxid_inviter</username>", "<username> </username>"),
        (
            "</memberlist>",
            "<member><nickname>Another member</nickname></member></memberlist>",
        ),
        ("$others$", "$missing$"),
        ("$others$", "$others"),
        ("$others$", "Member A"),
        ("<link_list>", "<link_list>unexpected text"),
        ("</sysmsgtemplate>", "</sysmsgtemplate><config/>"),
    ],
)
def test_ambiguous_or_unknown_system_templates_do_not_produce_partial_notices(old, new):
    raw = _system_template().replace(old, new)
    assert project_system_notice_text(raw) is None
    assert extract_wechat_text(10002, "123456789@chatroom:\n" + raw) is None


@pytest.mark.parametrize(
    "prefix",
    ["wxid_sender:\n", "unknown@chatroom:\n", "123@chatroom: ", "123@chatroom\n"],
)
def test_unknown_system_prefixes_are_not_stripped(prefix):
    assert project_system_notice_text(prefix + _system_template()) is None


def test_explicit_system_plain_field_stays_authoritative_for_unknown_templates():
    xml = (
        _system_template()
        .replace("<plain/>", "<plain>Explicit notice &amp; label</plain>")
        .replace("$others$", "$unknown$")
    )
    raw = "123456789@chatroom:\n" + xml
    assert project_system_notice_text(raw, peer_id="123456789@chatroom") == (
        "Explicit notice & label"
    )


def test_prefixed_recall_uses_the_same_projector_without_losing_source_ids():
    xml = (
        '<sysmsg type="revokemsg"><revokemsg><newmsgid>123456789</newmsgid>'
        "<replacemsg>A member recalled a message</replacemsg></revokemsg></sysmsg>"
    )
    raw = "123456789@chatroom:\n" + xml
    assert project_system_notice_text(raw) == "A member recalled a message"
    assert extract_wechat_text(10002, raw)["system_source_ids"] == {
        "revokemsg/newmsgid": "123456789"
    }


@pytest.mark.parametrize(
    "value", [None, 123, True, "ordinary system text", "&lt;sysmsg&gt;"]
)
def test_system_xml_projector_does_not_classify_plain_or_invalid_values(value):
    assert project_system_notice_text(value) is None


def test_system_template_expansion_has_its_own_utf8_output_budget(monkeypatch):
    xml = _system_template(template="$username$" + "$others$" * 200, others="中" * 100)
    assert project_system_notice_text(xml) == "Inviting member" + "中" * 20_000
    monkeypatch.setattr(wechat_text, "MAX_XML_BYTES", 4096)
    # Input is bounded, while repeated substitutions would exceed that bound.
    assert len(xml.encode("utf-8")) < wechat_text.MAX_XML_BYTES
    assert project_system_notice_text(xml) is None
    assert extract_wechat_text(10002, xml) is None


@pytest.mark.parametrize(
    "xml",
    [
        "<sysmsg>",
        '<sysmsg type="config"><config>Not a notice</config></sysmsg>',
        '<!DOCTYPE sysmsg [<!ENTITY leak SYSTEM "file:///private/secret">]>'
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template>'
        "<plain>&leak;</plain></content_template></sysmsgtemplate></sysmsg>",
    ],
)
def test_system_xml_rejects_unknown_malformed_and_external_entity_documents(
    monkeypatch, xml
):
    def forbidden(*args, **kwargs):
        raise AssertionError("System templates must not access external resources")

    with monkeypatch.context() as scoped:
        scoped.setattr("builtins.open", forbidden)
        assert project_system_notice_text(xml) is None
        assert extract_wechat_text(10002, "123456789@chatroom:\n" + xml) is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"enabled": true}',
        '["configuration"]',
        '<sysmsg type="config"><config><plain>not display evidence</plain></config></sysmsg>',
        "123456",
        "\x00configuration",
    ],
)
def test_system_configuration_is_not_a_chat_message(raw):
    assert extract_wechat_text(10000, raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "<msg>",
        "not xml",
        "<msg><appmsg><type>1</type><type>1</type><title>ambiguous</title></appmsg></msg>",
        "<msg><appmsg><type>1</type><title><configuration>nested</configuration></title></appmsg></msg>",
        "<msg>\ud800</msg>",
    ],
)
def test_malformed_or_ambiguous_xml_is_not_guessed(raw):
    assert extract_wechat_text(49, raw) is None


def test_external_entities_and_nested_declarations_never_open_files_or_network(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("The envelope parser must not access external resources")

    xml = '<!DOCTYPE msg [<!ENTITY leak SYSTEM "file:///private/secret">]><msg><appmsg><type>1</type><title>&leak;</title></appmsg></msg>'
    record = '<!DOCTYPE recordinfo [<!ENTITY leak SYSTEM "https://invalid.example/secret">]><recordinfo><datalist><dataitem datatype="1"><datadesc>&leak;</datadesc></dataitem></datalist></recordinfo>'
    with monkeypatch.context() as scoped:
        scoped.setattr("builtins.open", forbidden)
        assert extract_wechat_text(49, xml) is None
        assert (
            extract_wechat_text(
                49, _app(19, f"<recorditem><![CDATA[{record}]]></recorditem>")
            )
            is None
        )


def test_xml_size_depth_and_node_budgets_reject_excessive_documents(monkeypatch):
    monkeypatch.setattr(wechat_text, "MAX_XML_BYTES", 256)
    assert extract_wechat_text(49, _app(1, "<title>" + "中" * 100 + "</title>")) is None
    monkeypatch.setattr(wechat_text, "MAX_XML_BYTES", 100_000)
    monkeypatch.setattr(wechat_text, "MAX_XML_DEPTH", 8)
    assert (
        extract_wechat_text(
            49, _app(1, "<title>valid</title>" + "<nested>" * 8 + "</nested>" * 8)
        )
        is None
    )
    monkeypatch.setattr(wechat_text, "MAX_XML_DEPTH", 32)
    monkeypatch.setattr(wechat_text, "MAX_XML_NODES", 8)
    assert (
        extract_wechat_text(49, _app(1, "<title>valid</title>" + "<item/>" * 8)) is None
    )


def test_embedded_xml_counts_toward_the_same_parse_budget(monkeypatch):
    record = _record('<dataitem datatype="1"><datadesc>text</datadesc></dataitem>')
    raw = _app(19, f"<recorditem><![CDATA[{record}]]></recorditem>")
    monkeypatch.setattr(wechat_text, "MAX_XML_BYTES", len(raw.encode()))
    assert extract_wechat_text(49, raw) is None


_INVITATIONS = (
    (
        "Group Chat Invitation",
        '"Original sender" invited you to join the group chat "Source group 😀". Enter to view details.',
    ),
    (
        "邀请你加入群聊",
        '"原始发言人"邀请你加入群聊"原始群聊 😀",进入可查看详情。',
    ),
    (
        "邀请你加入群组",
        "「原始发言人」邀请你加入群组「原始群组 😀」,进入查看详情。",
    ),
    (
        "Group Chat Invitation",
        '"Original sender" invited you to join the group chat "". Enter to view details.',
    ),
    (
        "邀请你加入群聊",
        '"原始发言人"邀请你加入群聊原始群聊,进入可查看详情。',
    ),
)


@pytest.mark.parametrize("title,description", _INVITATIONS)
@pytest.mark.parametrize("separator", [" ", "\n"])
def test_complete_invitation_template_classifier_keeps_source_text_untouched(
    title, description, separator
):
    text = title + separator + description
    assert wechat_text.is_group_invitation_text(text)
    assert text == title + separator + description


@pytest.mark.parametrize(
    "text",
    [
        None,
        49,
        "",
        "Group Chat Invitation",
        "邀请你加入群聊",
        "邀请你加入群组",
        "邀请你加入群聊 「甲」邀请你加入群组「乙」,进入查看详情。",
        "Please invite me to the group chat.",
        "我来邀请你入群。",
        'Sender:Group Chat Invitation "A" invited you to join the group chat "G". Enter to view details.',
        'Group Chat Invitation "A" invited you to join the group chat "G".',
        'Group Chat Invitation "A" invited you to join the group chat "G". Enter to view details. Extra source text',
        '邀请你加入群聊 "甲"邀请你加入群聊"乙",进入可查看详情。 转发人的补充说明',
        'Invitation "A" invited you to join the group chat "G". Enter to view details.',
        'Group Chat Invitation "A" invited you to join the group chat "G". Enter to view details.\n'
        'Group Chat Invitation "B" invited you to join the group chat "H". Enter to view details.',
        'Group Chat Invitation "A\x00" invited you to join the group chat "G". Enter to view details.',
        'Group Chat Invitation "'
        + "A" * 8192
        + '" invited you to join the group chat "G". Enter to view details.',
    ],
)
def test_invitation_classifier_rejects_discussions_partial_and_prefixed_transcripts(
    text,
):
    assert not wechat_text.is_group_invitation_text(text)


@pytest.mark.parametrize("title,description", _INVITATIONS)
@pytest.mark.parametrize("with_url", [False, True])
def test_native_subtype_five_invitation_retains_only_literal_declared_fields(
    title, description, with_url
):
    url = "https://example.invalid/invitation?literal=1&key=source"
    body = f"<title>{escape(title)}</title><des>{escape(description)}</des>"
    if with_url:
        body += f"<url>{escape(url)}</url>"
    result = extract_wechat_text(49, _app(5, body))
    fields = {"title": title, "description": description}
    if with_url:
        fields["url"] = url
    assert result == {
        "text": title + "\n" + description + ("\n" + url if with_url else ""),
        "text_extraction_kind": "app_text",
        "original_message_type": 49,
        "appmsg_subtype": 5,
        "flags": ["invitation_card_metadata"],
        "invitation_card_fields": fields,
    }


@pytest.mark.parametrize("tag", ["type", "title", "des", "url"])
def test_invitation_xml_rejects_duplicate_relevant_fields_even_when_identical(tag):
    title, description = _INVITATIONS[0]
    fields = {
        "type": "5",
        "title": title,
        "des": description,
        "url": "https://example.invalid/source",
    }
    body = "".join(f"<{key}>{escape(value)}</{key}>" for key, value in fields.items())
    body += f"<{tag}>{escape(fields[tag])}</{tag}>"
    assert extract_wechat_text(49, "<msg><appmsg>" + body + "</appmsg></msg>") is None


@pytest.mark.parametrize("tag", ["title", "des", "url"])
def test_invitation_xml_rejects_nested_fields_instead_of_flattening_markup(tag):
    title, description = _INVITATIONS[0]
    fields = {
        "title": title,
        "des": description,
        "url": "https://example.invalid/source",
    }
    body = "".join(
        f"<{key}><nested>{escape(value)}</nested></{key}>"
        if key == tag
        else f"<{key}>{escape(value)}</{key}>"
        for key, value in fields.items()
    )
    assert extract_wechat_text(49, _app(5, body)) is None


@pytest.mark.parametrize(
    "body",
    [
        "<title>Group Chat Invitation</title>",
        "<title>邀请你加入群聊</title>",
        "<title>Group Chat Invitation &quot;A&quot; invited you to join the group chat &quot;G&quot;. Enter to view details.</title>",
        "<title>Ordinary link</title><des>&quot;A&quot; invited you to join the group chat &quot;G&quot;. Enter to view details.</des>",
        "<title>Group Chat Invitation</title><des>My invitation discussion.</des>",
    ],
)
def test_invitation_heading_alone_or_ordinary_link_metadata_stays_unsupported(body):
    assert extract_wechat_text(49, _app(5, body)) is None


@pytest.mark.parametrize("subtype", [2, 3, 4, 6, 8, 24, 51, 2000, 99999])
def test_full_invitation_copy_does_not_authorize_unknown_native_subtypes(subtype):
    title, description = _INVITATIONS[0]
    raw = _app(subtype, f"<title>{title}</title><des>{escape(description)}</des>")
    assert extract_wechat_text(49, raw) is None


def test_invitation_looking_typed_message_remains_plain_text():
    title, description = _INVITATIONS[0]
    raw = title + " " + description
    assert extract_wechat_text(1, raw) == {
        "text": raw,
        "text_extraction_kind": "plain_text",
        "original_message_type": 1,
        "appmsg_subtype": None,
        "flags": [],
    }


def test_invitation_cdata_html_and_url_are_inert_text_without_external_io(monkeypatch):
    description = '"<script>literal</script>" invited you to join the group chat "<img src=source>". Enter to view details.'
    url = "javascript:literal-source-value"
    raw = _app(
        5,
        "<title>Group Chat Invitation</title>"
        + f"<des><![CDATA[{description}]]></des><url><![CDATA[{url}]]></url>",
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Invitation extraction must not open any source or URL")

    with monkeypatch.context() as scoped:
        scoped.setattr("builtins.open", forbidden)
        result = extract_wechat_text(49, raw)
    assert result["text"] == "Group Chat Invitation\n" + description + "\n" + url
    assert result["invitation_card_fields"] == {
        "title": "Group Chat Invitation",
        "description": description,
        "url": url,
    }


@pytest.mark.parametrize(
    "raw",
    [
        '<!DOCTYPE msg [<!ENTITY leak SYSTEM "file:///private/secret">]><msg><appmsg><type>5</type><title>Group Chat Invitation</title><des>&leak;</des></appmsg></msg>',
        '<!DOCTYPE msg [<!ENTITY leak SYSTEM "https://example.invalid/source">]><msg><appmsg><type>5</type><title>Group Chat Invitation</title><des>&leak;</des></appmsg></msg>',
        "<msg><appmsg><type>5</type><title>Group Chat Invitation</title><des>broken",
    ],
)
def test_invitation_xml_keeps_declaration_and_malformed_input_guards(raw):
    assert extract_wechat_text(49, raw) is None


def test_invitation_branch_uses_the_existing_cumulative_xml_budget(monkeypatch):
    title, description = _INVITATIONS[0]
    raw = _app(5, f"<title>{title}</title><des>{escape(description)}</des>")
    monkeypatch.setattr(wechat_text, "MAX_XML_BYTES", len(raw.encode()) - 1)
    assert extract_wechat_text(49, raw) is None
