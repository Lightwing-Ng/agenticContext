"""Retained native structure supplements presentation without replacing primary rows.

Code version: v1.1.1-codex.0
"""

from contextlib import closing
import hashlib
import json
import sqlite3
from unittest.mock import patch
from xml.sax.saxutils import escape

import pytest

from app.core.wechat_archive import _identifier, wechat_archive_path
from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.wechat_context_evidence import WechatContextEvidence
from app.core.wechat_forwarded import project_native_forwarded
from app.core.wechat_history_browser import _native_quoted_reply_presentation
from app.core.wechat_reconcile import _json
from app.core.wechat_text import extract_wechat_text
from tests.test_wechat_reconcile import merge, record, source


def evidence_fixture(store, *, forwarded=False):
    metadata = source()
    peer = "peer-direct"
    if forwarded:
        embedded = (
            '<recordinfo><datalist><dataitem datatype="1"><sourcename>Original author</sourcename>'
            "<dataitemsource><fromusr>wxid_original</fromusr></dataitemsource>"
            "<datadesc>Original body</datadesc></dataitem></datalist></recordinfo>"
        )
        envelope = (
            "<msg><appmsg><type>19</type><recorditem>"
            + escape(embedded)
            + "</recorditem></appmsg></msg>"
        )
    else:
        envelope = (
            "<msg><appmsg><type>57</type><title>Actual reply</title><refermsg><type>1</type>"
            "<fromusr>wxid_original</fromusr><displayname>Earlier speaker</displayname>"
            "<content>Earlier &lt;b&gt;literal&lt;/b&gt; body</content></refermsg></appmsg></msg>"
        )
    parsed = extract_wechat_text(49, envelope)
    locator = {
        "database": "message_0.sqlite",
        "table": "Chat_" + hashlib.md5(peer.encode()).hexdigest(),
        "row": "native-one",
    }
    original = record(
        metadata,
        "native-one",
        peer=peer,
        text=parsed["text"],
        message_type=49,
        text_extraction_kind="app_text",
        source_locator=locator,
    )
    merge(store, [metadata], [original], "snapshot-one")
    before = primary_rows(store)
    structured = {**original, **parsed}
    result = merge(store, [metadata], [structured], "snapshot-two")
    assert result["inserted_messages"] == 0 and result["new_evidence"] == 1
    assert primary_rows(store) == before
    return metadata, parsed, original, before


def primary_rows(store):
    with closing(sqlite3.connect(wechat_archive_path(store))) as connection, connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute("SELECT * FROM messages ORDER BY id")
        ]


def edit_observation(store, change, *, row_change=None, duplicate=False):
    with closing(sqlite3.connect(wechat_archive_path(store))) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute("SELECT * FROM message_evidence LIMIT 1").fetchone()
        )
        observation = json.loads(row["observation"])
        change(observation)
        row["extraction_record_id"] = observation["record_id"]
        row["observation"] = _json(observation)
        if row_change:
            row_change(row)
        row["id"] = _identifier(
            row["source_id"], row["extraction_record_id"], row["observation"]
        )
        if not duplicate:
            connection.execute("DELETE FROM message_evidence")
        connection.execute(
            "INSERT INTO message_evidence VALUES(?,?,?,?,?,?,?)", tuple(row.values())
        )


@pytest.mark.parametrize("forwarded", [False, True])
def test_later_parsed_structure_becomes_presentable_without_rewriting_primary(
    tmp_path, forwarded
):
    metadata, parsed, _, supplied = evidence_fixture(tmp_path, forwarded=forwarded)
    before = wechat_archive_path(tmp_path).read_bytes()
    evidence = WechatContextEvidence(tmp_path)
    result = evidence.project(supplied)
    identifier = supplied[0]["id"]
    assert set(result) == {identifier}
    projected = result[identifier]
    assert projected["authority"] == "retained_native_text_observation"
    assert len(projected["evidence_ids"]) == 1
    assert projected["provenance"]["text_components"] == parsed["text_components"]
    original_provenance = json.loads(supplied[0]["provenance"])
    assert (
        projected["provenance"]["source_snapshot"]
        == original_provenance["source_snapshot"]
    )
    assert original_provenance["text_extraction_kind"] == "app_text"
    if forwarded:
        record = project_native_forwarded(
            projected["provenance"],
            parent_id=identifier,
            account_id=metadata["source_account_id"],
        )
        assert record["items"][0].content_text == "Original body"
    else:
        quote = _native_quoted_reply_presentation(projected["provenance"])
        assert quote["display_text"] == "Actual reply"
        assert quote["quoted_reply"]["text"] == "Earlier <b>literal</b> body"
    assert evidence.statuses[identifier] == "compatible_structure"
    assert primary_rows(tmp_path) == supplied
    assert wechat_archive_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize(
    "failure",
    [
        "body_hash",
        "body_components",
        "source",
        "snapshot",
        "snapshot_name",
        "record",
        "server",
        "sender",
        "direction",
        "time",
        "peer",
        "locator_conflict",
        "metadata_conflict",
        "evidence_id",
    ],
)
def test_invalid_or_conflicting_native_claims_are_not_promoted(tmp_path, failure):
    _, _, _, supplied = evidence_fixture(tmp_path)

    def change(observation):
        if failure == "body_hash":
            observation["content_sha256"] = "f" * 64
        elif failure == "body_components":
            observation["text_components"][0]["text"] = "Invented reply"
        elif failure == "snapshot":
            observation["source_snapshot"] = 999999
        elif failure == "record":
            observation.update({"record_id": "unrelated-record", "server_id": "0"})
        elif failure == "server":
            observation["server_id"] = "999"
        elif failure == "sender":
            observation["sender_id"] = "wxid_other"
        elif failure == "direction":
            observation["direction"] = "sent"
        elif failure == "time":
            observation["unix_timestamp"] += 1
        elif failure == "peer":
            observation["locator"]["table"] = "Chat_" + "f" * 32
        elif failure == "locator_conflict":
            observation["locator"]["row"] = "different-row"
        elif failure == "metadata_conflict":
            observation["locator"]["text_components"] = [
                {"kind": "reply_text", "text": "Conflicting locator reply"}
            ]

    def row_change(row):
        if failure == "source":
            row["source_id"] = "ios-other-account"
        elif failure == "snapshot_name":
            row["snapshot_id"] = "different-snapshot"

    edit_observation(tmp_path, change, row_change=row_change)
    if failure == "evidence_id":
        with closing(sqlite3.connect(wechat_archive_path(tmp_path))) as connection, connection:
            connection.execute("UPDATE message_evidence SET id=?", ("f" * 64,))
    assert WechatContextEvidence(tmp_path).project(supplied) == {}


def test_same_native_server_can_associate_a_different_record_without_matching_by_text_alone(
    tmp_path,
):
    _, _, _, supplied = evidence_fixture(tmp_path)
    edit_observation(
        tmp_path,
        lambda observation: observation.update(
            {
                "record_id": "independent-native-locator",
                "locator": {**observation["locator"], "row": "other-row"},
            }
        ),
    )
    assert WechatContextEvidence(tmp_path).project(supplied)


def test_different_valid_structures_fail_closed_and_malformed_noise_cannot_override_one(
    tmp_path,
):
    _, _, _, supplied = evidence_fixture(tmp_path)
    with closing(sqlite3.connect(wechat_archive_path(tmp_path))) as connection, connection:
        connection.execute(
            "INSERT INTO message_evidence VALUES(?,?,?,?,?,?,?)",
            (
                "e" * 64,
                supplied[0]["id"],
                "invalid-source",
                "bad-snapshot",
                "bad-record",
                "not json",
                "untrusted",
            ),
        )
    assert WechatContextEvidence(tmp_path).project(supplied)

    def differing_structure(observation):
        observation["quoted_reference"]["source_sender_label"] = (
            "Another asserted speaker"
        )
        observation["text_components"][1]["source_sender_label"] = (
            "Another asserted speaker"
        )

    edit_observation(tmp_path, differing_structure, duplicate=True)
    evidence = WechatContextEvidence(tmp_path)
    assert evidence.project(supplied) == {}
    assert evidence.statuses[supplied[0]["id"]] == "conflicting_structures"


def test_shared_primary_structure_cannot_be_replaced_by_retained_evidence(tmp_path):
    _, _, _, _ = evidence_fixture(tmp_path)
    with closing(sqlite3.connect(wechat_archive_path(tmp_path))) as connection, connection:
        provenance = json.loads(
            connection.execute("SELECT provenance FROM messages").fetchone()[0]
        )
        provenance["quoted_reference"] = {"source_sender_id": "wxid_primary_assertion"}
        connection.execute("UPDATE messages SET provenance=?", (_json(provenance),))
    assert WechatContextEvidence(tmp_path).project(primary_rows(tmp_path)) == {}


def test_source_receipt_and_current_primary_body_remain_required(tmp_path):
    _, _, _, supplied = evidence_fixture(tmp_path)
    with closing(sqlite3.connect(wechat_archive_path(tmp_path))) as connection, connection:
        connection.execute("UPDATE messages SET text='Changed primary body'")
    evidence = WechatContextEvidence(tmp_path)
    assert evidence.project(supplied) == {}
    assert evidence.statuses[supplied[0]["id"]] == "primary_changed"


def test_batch_reader_requires_existing_index_and_uses_bounded_indexed_queries(
    tmp_path,
):
    _, _, _, supplied = evidence_fixture(tmp_path)
    from app.core import wechat_context_evidence as module

    queries = []
    original = module._read_only

    def traced(path):
        connection = original(path)
        connection.set_trace_callback(queries.append)
        return connection

    with patch.object(module, "_read_only", traced):
        assert WechatContextEvidence(tmp_path).project(supplied)
    selections = [query for query in queries if query.startswith("WITH requested")]
    assert len(selections) == 1
    with closing(sqlite3.connect(wechat_archive_path(tmp_path))) as connection, connection:
        plan = "\n".join(
            str(row)
            for row in connection.execute("EXPLAIN QUERY PLAN " + selections[0])
        )
        assert "USING INDEX evidence_message" in plan
        assert "SCAN e" not in plan
        connection.execute("DROP INDEX evidence_message")
    queries.clear()
    with patch.object(module, "_read_only", traced):
        assert WechatContextEvidence(tmp_path).project(supplied) == {}
    assert not any(
        "FROM requested" in query or "CREATE INDEX" in query for query in queries
    )


def test_export_cross_references_do_not_assert_html_or_native_structure(tmp_path):
    _, _, _, supplied = evidence_fixture(tmp_path)
    edit_observation(
        tmp_path,
        lambda observation: observation.update(
            {
                "kind": "export_cross_reference",
                "cross_reference": {
                    "quoted_components": [{"text": "Not a verified parsed quote"}]
                },
            }
        ),
    )
    assert WechatContextEvidence(tmp_path).project(supplied) == {}


def test_excessive_evidence_is_bounded_without_choosing_an_arbitrary_first_candidate(
    tmp_path,
):
    _, _, _, supplied = evidence_fixture(tmp_path)
    for index in range(32):
        edit_observation(
            tmp_path,
            lambda observation, index=index: observation.update(
                {"record_id": f"another-record-{index}"}
            ),
            duplicate=True,
        )
    evidence = WechatContextEvidence(tmp_path)
    assert evidence.project(supplied) == {}
    assert evidence.statuses[supplied[0]["id"]] == "evidence_limit"


def test_history_presentation_uses_retained_structure_while_raw_source_and_export_stay_original(
    tmp_path,
):
    _, _, _, supplied = evidence_fixture(tmp_path)
    before = wechat_archive_path(tmp_path).read_bytes()
    page = query_chat_history(
        tmp_path, source="wechat", session=supplied[0]["conversation_id"]
    )
    assert len(page.items) == 1
    item = page.items[0]
    assert item.content_text == supplied[0]["text"]
    assert item.messaging["provenance"] == json.loads(supplied[0]["provenance"])
    assert item.messaging["provenance"]["text_extraction_kind"] == "app_text"
    assert "text_components" not in item.messaging["provenance"]
    assert item.messaging["display_text"] == "Actual reply"
    assert item.messaging["quoted_reply"]["text"] == "Earlier <b>literal</b> body"
    assert (
        item.messaging["context_evidence"]["authority"]
        == "retained_native_text_observation"
    )
    markdown = build_chat_history_markdown(page)
    assert supplied[0]["text"] in markdown
    assert "text_components" not in markdown
    assert primary_rows(tmp_path) == supplied
    assert wechat_archive_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize(
    "provenance",
    [
        {"message_type": 1, "text_extraction_kind": "plain_text"},
        {
            "export_msgid_namespace": "MesLocalID",
            "text_extraction_kind": "quoted_reply",
        },
        {"message_type": 49, "text_extraction_kind": "resource_placeholder"},
    ],
)
def test_noneligible_pages_do_not_open_or_scan_the_database(tmp_path, provenance):
    row = {"id": "a" * 64, "text": "Literal text", "provenance": provenance}
    with patch(
        "app.core.wechat_context_evidence._read_only",
        side_effect=AssertionError("No database reads expected"),
    ):
        assert WechatContextEvidence(tmp_path).project([row]) == {}


@pytest.mark.parametrize("legacy_locator", [False, True])
def test_exact_flat_forward_can_gain_resource_metadata_and_explicit_group_boundaries(
    tmp_path, legacy_locator
):
    metadata = source()
    peer = "peer-direct"
    native = (
        '<recordinfo><datalist><dataitem datatype="2" dataid="image-item"/>'
        '<dataitem datatype="17" dataid="group-item"><recordinfo><datalist>'
        '<dataitem datatype="1" dataid="text-item"><sourcename>Original speaker</sourcename>'
        "<dataitemsource><fromusr>wxid_original</fromusr></dataitemsource>"
        "<datadesc>Inner native text</datadesc></dataitem>"
        "</datalist></recordinfo></dataitem></datalist></recordinfo>"
    )
    parsed = extract_wechat_text(
        49,
        "<msg><appmsg><type>19</type><recorditem>"
        + escape(native)
        + "</recorditem></appmsg></msg>",
    )
    leaves = parsed["text_components"][1]["text_components"]
    locator = {
        "database": "message_0.sqlite",
        "table": "Chat_" + hashlib.md5(peer.encode()).hexdigest(),
        "row": "native-flat",
    }
    original = record(
        metadata,
        "native-flat",
        peer=peer,
        message_type=49,
        source_locator=locator,
        **{**parsed, "text_components": leaves},
    )
    if legacy_locator:
        for key in ("text_components", "appmsg_subtype"):
            original["source_locator"][key] = original.pop(key)
    merge(tmp_path, [metadata], [original], "snapshot-one")
    before = primary_rows(tmp_path)
    fresh_locator = {
        key: value
        for key, value in locator.items()
        if key not in {"text_components", "appmsg_subtype"}
    }
    enriched = record(
        metadata,
        "native-flat",
        peer=peer,
        message_type=49,
        source_locator=fresh_locator,
        **parsed,
    )
    assert merge(tmp_path, [metadata], [enriched], "snapshot-two")["new_evidence"] == 1
    database_bytes = wechat_archive_path(tmp_path).read_bytes()
    selected = WechatContextEvidence(tmp_path).project(before)[before[0]["id"]]
    assert selected["provenance"]["text_components"] == parsed["text_components"]
    assert "text_components" not in selected["provenance"]["locator"]
    page = query_chat_history(
        tmp_path, source="wechat", session=before[0]["conversation_id"]
    )
    item = page.items[0]
    assert item.content_text == "Inner native text"
    assert item.messaging["provenance"] == json.loads(before[0]["provenance"])
    children = item.messaging["forwarded_record"]["items"]
    assert children[0].messaging["resource"]["kind"] == "image"
    assert (
        children[1].messaging["forwarded_record"]["items"][0].content_text
        == "Inner native text"
    )
    assert primary_rows(tmp_path) == before
    assert wechat_archive_path(tmp_path).read_bytes() == database_bytes
