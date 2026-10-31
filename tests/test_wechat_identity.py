"""Synthetic account and contact protobuf metadata remains bounded and source-specific.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import pytest

from app.core import wechat_identity
from app.core.wechat_identity import (
    parse_contact_remark,
    parse_login_info,
    parse_room_members,
)


def _varint(value: int) -> bytes:
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _blob(number: int, value: bytes | str) -> bytes:
    value = value.encode() if isinstance(value, str) else value
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _login(*records: bytes) -> bytes:
    payload = b"".join(_varint(len(record)) + record for record in records)
    return _blob(1, payload)


def test_login_accounts_keep_aliases_and_names_independent():
    data = _login(
        _blob(1, "wxid_account_a") + _blob(2, "alias_a") + _blob(3, "同名😀"),
        _blob(1, "wxid_account_b") + _blob(2, "alias_b") + _blob(3, "同名😀"),
    )
    assert parse_login_info(data) == [
        {
            "source_account_id": "wxid_account_a",
            "source_account_alias": "alias_a",
            "display_name": "同名😀",
        },
        {
            "source_account_id": "wxid_account_b",
            "source_account_alias": "alias_b",
            "display_name": "同名😀",
        },
    ]


def test_empty_identifiers_are_not_replaced_by_nicknames_or_aliases():
    assert parse_login_info(
        _login(_blob(2, "not-an-account-id") + _blob(3, "Nickname"))
    ) == [
        {
            "source_account_id": "",
            "source_account_alias": "not-an-account-id",
            "display_name": "Nickname",
        }
    ]


def test_missing_optional_metadata_is_explicitly_empty():
    assert parse_login_info(b"") == []
    assert parse_login_info(_blob(1, b"")) == []
    assert parse_login_info(_login(_blob(1, "wxid_a")))[0] == {
        "source_account_id": "wxid_a",
        "source_account_alias": "",
        "display_name": "",
    }
    assert parse_contact_remark(b"") == {"nickname": "", "alias": "", "remark": ""}
    assert parse_room_members(b"") == {}
    assert parse_room_members(_blob(6, b"")) == {}


def test_contact_nickname_alias_and_remark_are_not_merged_or_normalized():
    data = _blob(3, "备注\n原文 ") + _blob(1, "昵称😀") + _blob(2, "alias-raw")
    assert parse_contact_remark(data) == {
        "nickname": "昵称😀",
        "alias": "alias-raw",
        "remark": "备注\n原文 ",
    }


def test_unknown_varint_fixed64_binary_and_fixed32_fields_are_skipped():
    unknown = (
        _varint(10 << 3)
        + _varint((1 << 64) - 1)
        + _varint(11 << 3 | 1)
        + b"\xff" * 8
        + _blob(12, b"\xff\xfe\x00binary avatar")
        + _varint(13 << 3 | 5)
        + b"\xff" * 4
    )
    assert (
        parse_contact_remark(unknown + _blob(1, "nickname"))["nickname"] == "nickname"
    )
    assert (
        parse_login_info(_login(unknown + _blob(1, "wxid_exact")))[0][
            "source_account_id"
        ]
        == "wxid_exact"
    )


@pytest.mark.parametrize(
    "data",
    [
        b"\x00",  # Field number zero.
        b"\x0f",  # Unsupported wire type seven.
        b"\x0b",  # Group wire types are not part of this metadata contract.
        b"\x80",  # Truncated tag varint.
        b"\x50" + b"\xff" * 10,  # An oversized unsigned varint value.
        _varint((1 << 29) << 3 | 2) + b"\x00",  # Field number above the protobuf limit.
        b"\x0a\x08short",  # Truncated length-delimited field.
        b"\x49short",  # Truncated fixed64 field.
        b"\x4d12",  # Truncated fixed32 field.
        b"\x08\x01",  # A known string field encoded as an integer.
    ],
)
def test_malformed_wire_data_is_rejected(data):
    with pytest.raises(ValueError):
        parse_contact_remark(data)


@pytest.mark.parametrize("data", [None, "protobuf", bytearray(b"")])
def test_metadata_requires_an_explicit_bytes_input(data):
    with pytest.raises(ValueError, match="bytes"):
        parse_contact_remark(data)


def test_known_text_requires_valid_utf8_and_no_nul():
    for value in [b"\xff", b"name\x00suffix"]:
        with pytest.raises(ValueError):
            parse_contact_remark(_blob(1, value))
        with pytest.raises(ValueError):
            parse_login_info(_login(_blob(1, value)))


def test_conflicting_scalar_values_are_not_silently_overwritten():
    with pytest.raises(ValueError, match="Conflicting"):
        parse_contact_remark(_blob(1, "first") + _blob(1, "second"))
    with pytest.raises(ValueError, match="Conflicting"):
        parse_login_info(_blob(1, b"first") + _blob(1, b"second"))
    assert (
        parse_contact_remark(_blob(1, "same") + _blob(1, "same"))["nickname"] == "same"
    )


@pytest.mark.parametrize("payload", [b"\x00", b"\x0along", b"\x80"])
def test_login_user_lengths_are_checked_before_decoding(payload):
    with pytest.raises(ValueError):
        parse_login_info(_blob(1, payload))


def test_protobuf_byte_field_account_and_text_limits(monkeypatch):
    monkeypatch.setattr(wechat_identity, "MAX_PROTO_BYTES", 16)
    with pytest.raises(ValueError, match="byte limit"):
        parse_contact_remark(_blob(1, "x" * 17))
    monkeypatch.setattr(wechat_identity, "MAX_PROTO_BYTES", 100_000)
    monkeypatch.setattr(wechat_identity, "MAX_PROTO_FIELDS", 2)
    with pytest.raises(ValueError, match="field limit"):
        parse_contact_remark(_blob(1, "a") + _blob(2, "b") + _blob(3, "c"))
    monkeypatch.setattr(wechat_identity, "MAX_PROTO_FIELDS", 100)
    monkeypatch.setattr(wechat_identity, "MAX_ACCOUNTS", 1)
    with pytest.raises(ValueError, match="account limit"):
        parse_login_info(_login(_blob(1, "a"), _blob(1, "b")))
    with pytest.raises(ValueError, match="text exceeds"):
        parse_contact_remark(_blob(1, "a" * (wechat_identity.MAX_TEXT_BYTES + 1)))


def test_room_members_preserve_unicode_empty_labels_and_large_identifier_strings():
    xml = '<RoomData><Member UserName="wxid_a"><DisplayName><![CDATA[成员😀\n<b>literal</b>]]></DisplayName></Member><Member UserName="18446744073709551615"/><Member><DisplayName>Missing ID</DisplayName></Member></RoomData>'
    assert parse_room_members(_blob(6, xml)) == {
        "wxid_a": "成员😀\n<b>literal</b>",
        "18446744073709551615": "",
    }


def test_room_avatar_fields_are_never_decoded_or_opened():
    data = _blob(2, b"\xffbinary avatar") + _blob(
        6,
        '<RoomData><Member UserName="wxid_a"><DisplayName>Known member</DisplayName></Member></RoomData>',
    )
    assert parse_room_members(data) == {"wxid_a": "Known member"}


@pytest.mark.parametrize(
    "xml",
    [
        "<OtherRoot/>",
        "<RoomData>",
        '<RoomData><Member UserName="a"><DisplayName>one</DisplayName><DisplayName>two</DisplayName></Member></RoomData>',
        '<RoomData><Member UserName="a"><DisplayName><config>nested</config></DisplayName></Member></RoomData>',
        '<RoomData><Member UserName="a"><DisplayName>one</DisplayName></Member><Member UserName="a"><DisplayName>two</DisplayName></Member></RoomData>',
    ],
)
def test_ambiguous_or_invalid_room_metadata_is_rejected(xml):
    with pytest.raises(ValueError):
        parse_room_members(_blob(6, xml))


def test_identical_duplicate_room_member_names_are_harmless():
    member = '<Member UserName="a"><DisplayName>same</DisplayName></Member>'
    assert parse_room_members(_blob(6, f"<RoomData>{member}{member}</RoomData>")) == {
        "a": "same"
    }


def test_room_xml_never_resolves_external_entities(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("The identity parser must not open external resources")

    xml = '<!DOCTYPE RoomData [<!ENTITY value SYSTEM "file:///private/secret">]><RoomData><Member UserName="a"><DisplayName>&value;</DisplayName></Member></RoomData>'
    with monkeypatch.context() as scoped:
        scoped.setattr("builtins.open", forbidden)
        with pytest.raises(ValueError, match="declarations and entities"):
            parse_room_members(_blob(6, xml))


def test_room_xml_requires_utf8_and_enforces_size_depth_nodes_and_members(monkeypatch):
    with pytest.raises(ValueError, match="UTF-8"):
        parse_room_members(_blob(6, b"\xff"))
    monkeypatch.setattr(wechat_identity, "MAX_ROOM_XML_BYTES", 16)
    with pytest.raises(ValueError, match="byte limit"):
        parse_room_members(_blob(6, "<RoomData>" + "中" * 10 + "</RoomData>"))
    monkeypatch.setattr(wechat_identity, "MAX_ROOM_XML_BYTES", 100_000)
    monkeypatch.setattr(wechat_identity, "MAX_ROOM_XML_DEPTH", 4)
    with pytest.raises(ValueError, match="structural limits"):
        parse_room_members(
            _blob(6, "<RoomData>" + "<x>" * 4 + "</x>" * 4 + "</RoomData>")
        )
    monkeypatch.setattr(wechat_identity, "MAX_ROOM_XML_DEPTH", 16)
    monkeypatch.setattr(wechat_identity, "MAX_ROOM_XML_NODES", 2)
    with pytest.raises(ValueError, match="structural limits"):
        parse_room_members(
            _blob(
                6, '<RoomData><Member UserName="a"/><Member UserName="b"/></RoomData>'
            )
        )
    monkeypatch.setattr(wechat_identity, "MAX_ROOM_XML_NODES", 100)
    monkeypatch.setattr(wechat_identity, "MAX_ROOM_MEMBERS", 1)
    with pytest.raises(ValueError, match="member limit"):
        parse_room_members(
            _blob(
                6, '<RoomData><Member UserName="a"/><Member UserName="b"/></RoomData>'
            )
        )
