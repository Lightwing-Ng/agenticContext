"""Compact record fields retain source coordinates without raw receipt dumps.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy

from jinja2 import Environment
from markupsafe import Markup
import pytest

from app.core.wechat_record_presentation import (
    MAX_FIELD_CHARACTERS,
    MAX_RECORD_FIELDS,
    compact_record_fields,
)


def test_html_coordinates_keep_local_namespace_and_exact_unicode_without_receipts():
    provenance = {
        "record_id": "original-source-id",
        "export_msgid_namespace": "MesLocalID",
        "source_time": "2021-8-7 下午7:31",
        "locator": {
            "archive_sha256": "a" * 64,
            "export_msgid": "0030",
            "export_msgid_namespace": "MesLocalID",
            "export_msgtype": "1",
            "member_path": "Bay Αιγαίο/黄洁凤.html",
            "member": {
                "sha256": "b" * 64,
                "zip_filename": "garbled-name",
                "header_offset": 11312,
            },
            "message_ordinal": 30,
            "page_message_ordinal": 30,
            "page": 0,
        },
    }
    before = deepcopy(provenance)
    fields = compact_record_fields(provenance)
    assert fields == (
        {"label": "Source file", "value": "Bay Αιγαίο/黄洁凤.html"},
        {"label": "Local export ID", "value": "0030 (MesLocalID)"},
        {"label": "Export page", "value": "0"},
        {"label": "Source position", "value": "30"},
    )
    assert provenance == before
    fields[0]["value"] = "A changed display field"
    assert provenance == before


def test_native_locator_keeps_distinct_local_coordinates_and_deduplicates_database_path():
    provenance = {
        "server_id": "9223372036854775806",
        "locator": {
            "database": "message_4.sqlite",
            "database_relative_path": "Documents/account/DB/message_4.sqlite",
            "database_sha256": "c" * 64,
            "table": "Chat_source_table",
            "local_id": 1200,
            "row_id": 1201,
            "native_direction": 1,
            "native_compression_marker": 0,
        },
    }
    assert compact_record_fields(provenance) == (
        {"label": "Database", "value": "Documents/account/DB/message_4.sqlite"},
        {"label": "Table", "value": "Chat_source_table"},
        {"label": "Device message ID", "value": "1200"},
        {"label": "Row", "value": "1201"},
    )
    # The template already renders the server ID outside this locator projection.
    assert provenance["server_id"] == "9223372036854775806"


def test_equal_device_row_coordinates_share_one_field_but_zero_stays_visible():
    assert compact_record_fields({"locator": {"local_id": 0, "row_id": 0}}) == (
        {"label": "Device message ID", "value": "0"},
    )
    assert compact_record_fields({"locator": {"local_id": "0007", "row_id": 7}}) == (
        {"label": "Device message ID", "value": "0007"},
        {"label": "Row", "value": "7"},
    )


def test_async_page_and_source_positions_remain_distinct():
    assert compact_record_fields(
        {"locator": {"page": 2, "message_ordinal": 2001, "page_message_ordinal": 1}}
    ) == (
        {"label": "Export page", "value": "2"},
        {"label": "Source position", "value": "2001"},
        {"label": "Page position", "value": "1"},
    )


def test_a_local_export_id_is_not_collapsed_into_a_matching_source_position():
    assert compact_record_fields(
        {
            "locator": {
                "export_msgid": "30",
                "export_msgid_namespace": "MesLocalID",
                "message_ordinal": 30,
                "page_message_ordinal": 30,
            }
        }
    ) == (
        {"label": "Local export ID", "value": "30 (MesLocalID)"},
        {"label": "Source position", "value": "30"},
    )


@pytest.mark.parametrize("namespace", ["MesLocalID", "ServerId", "unknown"])
def test_export_identifiers_preserve_the_observed_namespace_without_promoting_authority(
    namespace,
):
    fields = compact_record_fields(
        {"locator": {"export_msgid": "42", "export_msgid_namespace": namespace}}
    )
    assert fields == (
        {
            "label": "Local export ID"
            if namespace == "MesLocalID"
            else "Export message ID",
            "value": f"42 ({namespace})",
        },
    )


def test_conflicting_export_namespaces_remain_explicit():
    assert compact_record_fields(
        {
            "export_msgid_namespace": "ServerId",
            "locator": {"export_msgid": "42", "export_msgid_namespace": "MesLocalID"},
        }
    ) == ({"label": "Export message ID", "value": "42 (MesLocalID / ServerId)"},)


def test_numeric_paths_and_namespaces_are_not_coerced_into_textual_metadata():
    assert compact_record_fields(
        {
            "export_msgid_namespace": 0,
            "locator": {
                "member_path": 7,
                "database": 9,
                "table": 12,
                "sheet": False,
                "export_msgid": 30,
                "export_msgid_namespace": True,
            },
        }
    ) == ({"label": "Export message ID", "value": "30"},)


def test_pdf_and_spreadsheet_coordinates_do_not_need_native_or_html_fields():
    assert compact_record_fields(
        {"locator": {"path": "聊天.pdf", "page": 15, "row": 7}}
    ) == (
        {"label": "Source file", "value": "聊天.pdf"},
        {"label": "Row", "value": "7"},
        {"label": "Export page", "value": "15"},
    )
    assert compact_record_fields(
        {"locator": {"sheet": "History", "row": "native-one"}}
    ) == (
        {"label": "Row", "value": "native-one"},
        {"label": "Sheet", "value": "History"},
    )


@pytest.mark.parametrize(
    "invalid", [None, True, 17, "{}", [], {"locator": None}, {"locator": []}]
)
def test_malformed_provenance_has_no_dump_fallback(invalid):
    assert compact_record_fields(invalid) == ()


def test_booleans_floats_and_containers_are_not_identifiers_or_positions():
    assert (
        compact_record_fields(
            {
                "locator": {
                    "local_id": True,
                    "row_id": 2.5,
                    "page": False,
                    "export_msgid": {"payload": "not an ID"},
                    "table": ["not a table"],
                    "message_ordinal": "3",
                    "page_message_ordinal": -1,
                }
            }
        )
        == ()
    )


def test_unknown_or_oversized_fields_are_not_serialized_or_traversed():
    recursive = {}
    recursive["recursive"] = recursive
    provenance = {
        "locator": {
            "member": recursive,
            "member_path": "x" * (MAX_FIELD_CHARACTERS + 1),
            "local_id": 10**500,
            "row": 9,
            "other": recursive,
        }
    }
    assert compact_record_fields(provenance) == ({"label": "Row", "value": "9"},)
    assert provenance["locator"]["member"] is recursive


def test_large_unknown_record_surfaces_only_a_bounded_coordinate_allowlist():
    locator = {
        "member_path": "Export/聊天.html",
        "database": "message_4.sqlite",
        "table": "Chat_fixture",
        "local_id": 9,
        "row_id": 10,
        "export_msgid": "42",
        "page": 2,
        "message_ordinal": 1001,
        "page_message_ordinal": 1,
        "sheet": "History",
        **{f"unknown-{index}": {"raw": "x" * 5000} for index in range(100)},
    }
    fields = compact_record_fields({"locator": locator})
    assert len(fields) == MAX_RECORD_FIELDS
    assert all(
        type(field["label"]) is str and type(field["value"]) is str for field in fields
    )
    assert all(len(field["value"]) <= MAX_FIELD_CHARACTERS for field in fields)
    assert all("unknown" not in field["value"] for field in fields)


def test_projected_source_text_remains_plain_and_autoescaped():
    value = '<script>alert("source")</script> & 黄洁凤.html'
    fields = compact_record_fields({"locator": {"member_path": value}})
    assert type(fields[0]["value"]) is str
    assert fields[0]["value"] == value
    rendered = (
        Environment(autoescape=True)
        .from_string("{{ fields[0].value }}")
        .render(fields=fields)
    )
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
    assert "&amp;" in rendered and "黄洁凤.html" in rendered
    assert compact_record_fields({"locator": {"member_path": Markup(value)}}) == ()
