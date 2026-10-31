"""Known text envelopes retain source evidence without accessing media.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from html import escape

import pytest

from app.core import wechat_text
from app.core.wechat_text import extract_wechat_text


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
def test_forwarded_records_keep_only_text_items_and_preserve_nested_source_ids(
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
    assert result["omitted_forwarded_items"] == 2
    assert len(result["text_components"]) == 2
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
    assert result["text_components"][0]["forwarded_item_path"] == [1, 1]
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
