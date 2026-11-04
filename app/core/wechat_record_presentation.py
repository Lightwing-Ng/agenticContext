"""Project compact source coordinates without changing retained provenance.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from typing import TypedDict


VERSION = "v1.0.0-codex.0"
MAX_FIELD_CHARACTERS = 1024
MAX_RECORD_FIELDS = 10


class RecordField(TypedDict):
    label: str
    value: str


def _text(value: object) -> str:
    if type(value) is str:
        return value if len(value) <= MAX_FIELD_CHARACTERS and value.strip() else ""
    return ""


def _scalar(value: object) -> str:
    """Keep exact source spelling; never stringify containers or imprecise IDs."""
    if type(value) is str:
        return _text(value)
    if type(value) is int and value.bit_length() <= 256:
        return str(value)
    return ""


def _position(value: object) -> str:
    return _scalar(value) if type(value) is int and value >= 0 else ""


def compact_record_fields(provenance: object) -> tuple[RecordField, ...]:
    """Select human-usable coordinates, leaving hashes and raw receipts untouched.

    This display projection does not establish identifier authority. Export IDs
    keep their original namespace, including disagreements between source fields.
    Unknown structures and oversized fields stay only in the original provenance.
    """
    if type(provenance) is not dict or type(provenance.get("locator")) is not dict:
        return ()
    locator = provenance["locator"]
    fields: list[RecordField] = []

    def add(label: str, value: object) -> None:
        text = _scalar(value)
        if text:
            fields.append({"label": label, "value": text})

    def first(*keys: str, allow_integer: bool = False) -> str:
        for key in keys:
            value = (
                _scalar(locator.get(key)) if allow_integer else _text(locator.get(key))
            )
            if value:
                return value
        return ""

    add("Source file", first("member_path", "path", "source_path"))
    add("Database", first("database_relative_path", "database"))
    add("Table", _text(locator.get("table")))
    local_id = _scalar(locator.get("local_id"))
    add("Device message ID", local_id)
    row = first("row_id", "row", allow_integer=True)
    if row != local_id:
        add("Row", row)
    export_id = _scalar(locator.get("export_msgid"))
    if export_id:
        namespaces = tuple(
            dict.fromkeys(
                namespace
                for value in (
                    locator.get("export_msgid_namespace"),
                    provenance.get("export_msgid_namespace"),
                )
                if (namespace := _text(value))
            )
        )
        label = (
            "Local export ID" if namespaces == ("MesLocalID",) else "Export message ID"
        )
        value = export_id + (" (" + " / ".join(namespaces) + ")" if namespaces else "")
        add(label, value)
    add("Export page", _position(locator.get("page")))
    ordinal = _position(locator.get("message_ordinal"))
    add("Source position", ordinal)
    page_ordinal = _position(locator.get("page_message_ordinal"))
    if page_ordinal != ordinal:
        add("Page position", page_ordinal)
    add("Sheet", _text(locator.get("sheet")))
    return tuple(fields[:MAX_RECORD_FIELDS])
