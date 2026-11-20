"""Observed system envelopes project only validated literal notice fields.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import csv
from html import escape
import io
import json
from types import SimpleNamespace

from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

from app.core import wechat_text
from app.core.wechat_history_browser import query_wechat_history
from app.core.wechat_session_export import WechatSessionExport
from app.core.wechat_text import extract_wechat_text, project_system_notice_text
from app.web.presentation import format_wechat_source_clock
from app.web.wechat_session_csv import session_csv_chunks
from tests import test_wechat_macos_recovery_bank as bank_fixtures


mac_archive = bank_fixtures.mac_archive
bank_archive = bank_fixtures.bank_archive
ROOM = bank_fixtures.ROOM
RECALL = "  A source member recalled a message.\nLiteral <script>copy</script> & tail  "
REMOVAL = "  You were removed from the group.\nLiteral source punctuation.  "
QR_VALUE = "https://example.invalid/ignored-qr-target"
COMMON = (
    "<extcommoninfo><groupinfo><type>1</type><id>opaque-source-id</id><count>3</count>"
    "</groupinfo><media_expire_at>1700000500</media_expire_at></extcommoninfo>"
)


def _recall(*, common=False, body=RECALL):
    return (
        '<sysmsg type="revokemsg"><revokemsg><content>'
        + escape(body)
        + "</content><revoketime>1700000000</revoketime></revokemsg>"
        + (COMMON if common else "")
        + "</sysmsg>"
    )


def _removal(*, qr=False, body=REMOVAL):
    return (
        '<sysmsg type="delchatroommember"><delchatroommember><plain>'
        + escape(body)
        + "</plain><text>"
        + escape(body)
        + "</text><link><scene>1</scene><text>Ignored action wording</text>"
        "<memberlist><username>wxid_synthetic_removed</username></memberlist>"
        + ("<qrcode>" + QR_VALUE + "</qrcode>" if qr else "")
        + "</link></delchatroommember></sysmsg>"
    )


def _hidden_qr(tag="qrcode", *, repetitions=1, nickname="Recorded adder"):
    template = "$adder$" * repetitions + " invited you.\n$revoke$"
    return (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
        '<content_template type="tmpl_type_profilewithrevokeqrcode"><plain/>'
        "<template>" + escape(template) + "</template><link_list>"
        '<link name="adder" type="link_profile"><memberlist><member>'
        "<username>wxid_synthetic_adder</username><nickname>"
        + escape(nickname)
        + "</nickname></member></memberlist></link>"
        '<link name="revoke" type="link_revoke_qrcode" hidden="1">'
        "<title>Ignored revoke action</title><username>wxid_synthetic_target</username>"
        "<" + tag + ">" + QR_VALUE + "</" + tag + "></link>"
        "</link_list></content_template></sysmsgtemplate></sysmsg>"
    )


def _cases():
    return [
        (_recall(), RECALL, "revokemsg"),
        (_recall(common=True), RECALL, "revokemsg"),
        (_removal(), REMOVAL, "delchatroommember"),
        (_removal(qr=True), REMOVAL, "delchatroommember"),
        (_hidden_qr(), "Recorded adder invited you.\n", "sysmsgtemplate"),
        (_hidden_qr("qrcodeURL"), "Recorded adder invited you.\n", "sysmsgtemplate"),
    ]


@pytest.mark.parametrize("message_type", [10000, 10002])
@pytest.mark.parametrize("prefixed", [False, True])
@pytest.mark.parametrize("xml,expected,subtype", _cases())
def test_only_observed_literal_notice_fields_are_projected(
    message_type, prefixed, xml, expected, subtype
):
    raw = ROOM + ":\n" + xml if prefixed else xml
    projected = extract_wechat_text(message_type, raw)
    assert projected["text"] == expected
    assert projected["original_message_type"] == message_type
    assert projected["text_extraction_kind"] == "system_text"
    assert projected["system_message_subtype"] == subtype
    assert projected["system_source_ids"] == {}
    assert projected["flags"] == ["system_notice"]
    assert project_system_notice_text(raw, peer_id=ROOM) == expected
    assert extract_wechat_text(1, raw)["text"] == raw
    assert QR_VALUE not in json.dumps(projected)
    assert "opaque-source-id" not in json.dumps(projected)
    assert "Ignored action wording" not in projected["text"]
    if prefixed:
        assert project_system_notice_text(raw, peer_id="999@chatroom") is None


@pytest.mark.parametrize(
    "replacement",
    [
        "<content/>",
        "<content> </content>",
        "<content>One</content><content>Two</content>",
        '<content lang="en">One</content>',
        "<content><b>One</b></content>",
    ],
)
def test_recall_content_must_be_one_nonempty_pure_leaf(replacement):
    xml = _recall(body="One").replace("<content>One</content>", replacement)
    assert extract_wechat_text(10002, xml) is None
    assert project_system_notice_text(xml) is None


@pytest.mark.parametrize(
    "leaf",
    [
        "<replacemsg/>",
        "<replacemsg> </replacemsg>",
        "<replacemsg>One</replacemsg><replacemsg>Two</replacemsg>",
        '<replacemsg lang="en"/>',
        "<replacemsg><b>One</b></replacemsg>",
    ],
)
@pytest.mark.parametrize("location", ["nested", "root"])
def test_declared_unusable_replacemsg_is_not_rescued_by_content(leaf, location):
    xml = _recall()
    close = "</revokemsg>" if location == "nested" else "</sysmsg>"
    xml = xml.replace(close, leaf + close)
    assert extract_wechat_text(10002, xml) is None


def test_existing_valid_replacemsg_and_explicit_template_plain_keep_priority():
    xml = _recall().replace(
        "</revokemsg>", "<replacemsg>Original display wins</replacemsg></revokemsg>"
    )
    assert extract_wechat_text(10002, xml)["text"] == "Original display wins"
    xml = _hidden_qr().replace("<plain/>", "<plain>Authoritative plain notice</plain>")
    xml = xml.replace(
        'type="tmpl_type_profilewithrevokeqrcode"', 'type="unknown_template"'
    )
    assert extract_wechat_text(10002, xml)["text"] == "Authoritative plain notice"


@pytest.mark.parametrize(
    "old,new",
    [
        ("<revoketime>1700000000</revoketime>", "<revoketime>not-digits</revoketime>"),
        (
            "<revoketime>1700000000</revoketime>",
            "<revoketime>1</revoketime><revoketime>2</revoketime>",
        ),
        ("<revokemsg>", '<revokemsg extra="1">'),
        ("<groupinfo>", '<groupinfo extra="1">'),
        ("<type>1</type>", "<type>x</type>"),
        ("<count>3</count>", "<count>-1</count>"),
        (
            "<media_expire_at>1700000500</media_expire_at>",
            "<media_expire_at><nested>1</nested></media_expire_at>",
        ),
        ("<id>opaque-source-id</id>", "<id>one</id><id>two</id>"),
        ("</extcommoninfo>", "<extra>Unreviewed configuration</extra></extcommoninfo>"),
        ("</sysmsg>", COMMON + "</sysmsg>"),
    ],
)
def test_recall_auxiliary_metadata_is_validated_before_being_ignored(old, new):
    xml = _recall(common=True).replace(old, new)
    assert extract_wechat_text(10002, xml) is None


@pytest.mark.parametrize(
    "old,new",
    [
        ("<plain>", '<plain lang="en">'),
        ("</plain>", "<b>Nested markup</b></plain>"),
        ("</plain>", "</plain><plain>Duplicate</plain>"),
        ("</text><link>", " Different</text><link>"),
        ("<scene>1</scene>", "<scene><nested>1</nested></scene>"),
        ("<link>", '<link action="unreviewed">'),
        ("</memberlist>", "<username>wxid_other</username></memberlist>"),
        ("<username>wxid_synthetic_removed</username>", ""),
        ("<qrcode>", '<qrcode extra="1">'),
        ("</qrcode>", "<nested/></qrcode>"),
    ],
)
def test_removal_requires_agreeing_pure_text_and_the_closed_link_shape(old, new):
    xml = _removal(qr=True).replace(old, new)
    assert extract_wechat_text(10002, xml) is None


@pytest.mark.parametrize(
    "replacement",
    [
        "<qrcode>One</qrcode><qrcodeURL>Two</qrcodeURL>",
        "<qrcode>One</qrcode><qrcode>Two</qrcode>",
        '<qrcode extra="1">One</qrcode>',
        "<qrcode><nested>One</nested></qrcode>",
    ],
)
def test_hidden_qrcode_alias_cannot_introduce_another_action_authority(replacement):
    xml = _hidden_qr().replace("<qrcode>" + QR_VALUE + "</qrcode>", replacement)
    assert extract_wechat_text(10002, xml) is None


@pytest.mark.parametrize("xml", [_recall(common=True), _removal(qr=True), _hidden_qr()])
@pytest.mark.parametrize("budget", ["MAX_XML_BYTES", "MAX_XML_DEPTH", "MAX_XML_NODES"])
def test_extensions_keep_input_byte_depth_and_node_budgets(monkeypatch, xml, budget):
    assert extract_wechat_text(10002, xml) is not None
    limit = len(xml.encode()) - 1 if budget == "MAX_XML_BYTES" else 2
    monkeypatch.setattr(wechat_text, budget, limit)
    assert extract_wechat_text(10002, xml) is None


@pytest.mark.parametrize("xml", [_recall(common=True), _removal(qr=True), _hidden_qr()])
def test_extensions_never_open_dtd_or_external_resources(monkeypatch, xml):
    declaration = '<!DOCTYPE sysmsg [<!ENTITY leak SYSTEM "file:///never-opened">]>'

    def forbidden(*args, **kwargs):
        raise AssertionError("System projection must not open a resource")

    monkeypatch.setattr("builtins.open", forbidden)
    assert extract_wechat_text(10002, declaration + xml) is None
    assert project_system_notice_text(declaration + xml) is None
    assert extract_wechat_text(10002, xml) is not None


def test_qrcode_alias_retains_literal_substitution_and_expanded_utf8_budget(
    monkeypatch,
):
    label = "<script>literal</script> $revoke$"
    xml = _hidden_qr(nickname=label)
    assert extract_wechat_text(10002, xml)["text"] == label + " invited you.\n"
    xml = _hidden_qr(repetitions=200, nickname="中" * 20)
    monkeypatch.setattr(wechat_text, "MAX_XML_BYTES", len(xml.encode()) + 1)
    assert extract_wechat_text(10002, xml) is None


@pytest.mark.parametrize("message_type", [10000, 10002])
def test_mac_bank_notice_roles_source_hashes_and_csv_roundtrip_remain_neutral(
    bank_archive, message_type
):
    fixture = bank_archive
    packet = deepcopy(fixture.group)
    packet["messages"] = [
        bank_fixtures._message(90 + number, message_type, raw=xml)
        for number, (xml, _display, _subtype) in enumerate(_cases())
    ]
    before = fixture.history.read_bytes()
    bank_fixtures._publish(fixture, packets=[packet])
    page = query_wechat_history(
        fixture.root, session=fixture.group_session, persist_event_cache=False
    )
    notices = [item for item in page.items if item.messaging["is_system_notice"]]
    assert len(notices) == len(_cases())
    assert page.total_count == 2 + len(notices)
    expected = [display for _xml, display, _subtype in _cases()]
    for item, message in zip(notices, packet["messages"], strict=True):
        assert item.role == "unknown" and item.messaging["sender_id"] == ""
        assert item.author_label == "System Message"
        assert item.messaging["display_text"] == item.content_text == message["text"]
        assert (
            item.messaging["provenance"]["macos_recovery"]["content_sha256"]
            == message["content_sha256"]
        )
    environment = Environment(
        loader=FileSystemLoader("app/web/templates"),
        autoescape=select_autoescape(["html"]),
    )
    environment.filters["wechat_source_clock"] = format_wechat_source_clock
    module = environment.get_template("_wechat_browser.html").make_module(
        {"text_page": SimpleNamespace(messaging={}, session_detail=False, sessions=[])}
    )
    soup = BeautifulSoup(str(module.message_row(notices[0])), "html.parser")
    assert soup.select_one('[role="note"] .wechat-system-notice').get_text() == RECALL
    assert soup.find("script") is None
    export = WechatSessionExport(
        fixture.root, fixture.group_session, write_event_cache=False
    )
    encoded = "".join(session_csv_chunks(export))
    rows = list(csv.DictReader(io.StringIO(encoded.lstrip("\ufeff"), newline="")))
    exported = [row for row in rows if row["Speaker ID"] == "System Message"]
    assert [row["Statement"] for row in exported] == expected
    assert fixture.history.read_bytes() == before
