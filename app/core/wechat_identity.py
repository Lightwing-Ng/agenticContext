"""Decode bounded WeChat account and contact metadata without reading media.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import re
from typing import Iterator
from xml.etree import ElementTree


MAX_PROTO_BYTES = 8 * 1024 * 1024
MAX_PROTO_FIELDS = 100_000
MAX_TEXT_BYTES = 64 * 1024
MAX_ACCOUNTS = 4096
MAX_ROOM_XML_BYTES = 2 * 1024 * 1024
MAX_ROOM_XML_DEPTH = 16
MAX_ROOM_XML_NODES = 100_000
MAX_ROOM_MEMBERS = 20_000
_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


def _varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if position >= len(data):
            raise ValueError("Truncated protobuf varint.")
        byte = data[position]
        position += 1
        if index == 9 and byte > 1:
            raise ValueError("Protobuf varint exceeds 64 bits.")
        value |= (byte & 0x7F) << (index * 7)
        if not byte & 0x80:
            return value, position
    raise ValueError("Protobuf varint exceeds 64 bits.")


def _fields(data: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    if not isinstance(data, bytes):
        raise ValueError("Protobuf input must be bytes.")
    if len(data) > MAX_PROTO_BYTES:
        raise ValueError("Protobuf input exceeds the byte limit.")
    position = 0
    count = 0
    while position < len(data):
        count += 1
        if count > MAX_PROTO_FIELDS:
            raise ValueError("Protobuf input exceeds the field limit.")
        tag, position = _varint(data, position)
        number, wire = tag >> 3, tag & 7
        if not 0 < number <= (1 << 29) - 1:
            raise ValueError("Invalid protobuf field number.")
        if wire == 0:
            value, position = _varint(data, position)
            yield number, wire, value
            continue
        if wire == 1:
            size = 8
        elif wire == 2:
            size, position = _varint(data, position)
        elif wire == 5:
            size = 4
        else:
            raise ValueError("Unsupported protobuf wire type.")
        if size > len(data) - position:
            raise ValueError("Truncated protobuf field.")
        value = data[position : position + size]
        position += size
        yield number, wire, value


def _selected_blobs(data: bytes, numbers: set[int]) -> dict[int, bytes]:
    result = {}
    for number, wire, value in _fields(data):
        if number not in numbers:
            continue
        if wire != 2 or not isinstance(value, bytes):
            raise ValueError("A text field has an invalid protobuf wire type.")
        if number in result and result[number] != value:
            raise ValueError("Conflicting duplicate protobuf field.")
        result[number] = value
    return result


def _text(data: bytes, *, limit: int = MAX_TEXT_BYTES) -> str:
    if len(data) > limit:
        raise ValueError("Metadata text exceeds the byte limit.")
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Metadata text is not valid UTF-8.") from error
    if "\x00" in value:
        raise ValueError("Metadata text contains a NUL character.")
    return value


def parse_login_info(data: bytes) -> list[dict[str, str]]:
    """Decode LoginInfo2's field-1 vector of length-prefixed account records.

    Returned account names still require independent MD5 directory verification.
    Empty identifiers remain empty; display names or aliases never replace them.
    """
    payload = _selected_blobs(data, {1}).get(1, b"")
    position = 0
    result = []
    while position < len(payload):
        if len(result) >= MAX_ACCOUNTS:
            raise ValueError("Login metadata exceeds the account limit.")
        size, position = _varint(payload, position)
        if size == 0 or size > len(payload) - position:
            raise ValueError("Invalid length-prefixed account record.")
        fields = _selected_blobs(payload[position : position + size], {1, 2, 3})
        position += size
        result.append(
            {
                "source_account_id": _text(fields.get(1, b"")),
                "source_account_alias": _text(fields.get(2, b"")),
                "display_name": _text(fields.get(3, b"")),
            }
        )
    return result


def parse_contact_remark(data: bytes) -> dict[str, str]:
    """Return source nickname, alias, and remark independently."""
    fields = _selected_blobs(data, {1, 2, 3})
    return {
        "nickname": _text(fields.get(1, b"")),
        "alias": _text(fields.get(2, b"")),
        "remark": _text(fields.get(3, b"")),
    }


def _room_root(data: bytes) -> ElementTree.Element:
    raw = _text(data, limit=MAX_ROOM_XML_BYTES)
    if _DECLARATION.search(raw):
        raise ValueError("Room XML declarations and entities are unsupported.")
    parser = ElementTree.XMLPullParser(events=("start", "end"))
    root = None
    depth = 0
    count = 0
    try:
        value = raw.lstrip("\ufeff \t\r\n")
        for offset in range(0, len(value), 4096):
            parser.feed(value[offset : offset + 4096])
            for event, element in parser.read_events():
                if event == "start":
                    depth += 1
                    count += 1
                    if depth > MAX_ROOM_XML_DEPTH or count > MAX_ROOM_XML_NODES:
                        raise ValueError("Room XML exceeds its structural limits.")
                    if root is None:
                        root = element
                else:
                    depth -= 1
        parser.close()
    except ElementTree.ParseError as error:
        raise ValueError("Room metadata is not valid XML.") from error
    if root is None or depth or root.tag != "RoomData":
        raise ValueError("Unsupported room metadata root.")
    return root


def parse_room_members(data: bytes) -> dict[str, str]:
    """Decode RoomData from contact protobuf field 6 without using avatar fields."""
    payload = _selected_blobs(data, {6}).get(6, b"")
    if not payload:
        return {}
    root = _room_root(payload)
    members = {}
    count = 0
    for member in root.findall("Member"):
        count += 1
        if count > MAX_ROOM_MEMBERS:
            raise ValueError("Room metadata exceeds the member limit.")
        username = member.get("UserName", "")
        if not username:
            continue
        names = member.findall("DisplayName")
        if len(names) > 1 or (names and len(names[0])):
            raise ValueError("Ambiguous room member display name.")
        display_name = (names[0].text or "") if names else ""
        if username in members and members[username] != display_name:
            raise ValueError("Conflicting duplicate room member.")
        members[username] = display_name
    return members
