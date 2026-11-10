"""Retained schema-4 revision proofs remain closed and source-preserving.

Code version: v1.1.0-codex.0
"""

from copy import deepcopy
import json
import sqlite3

import pytest

from app.core import wechat_macos_bank_v4_reader as reader
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_mac_refresh_projection import _project_message
from app.core.wechat_text import native_components_text
from tests import test_wechat_macos_recovery_bank as native


mac_archive = native.mac_archive
bank_archive = native.bank_archive


def _messages(kind):
    if kind == "card":
        raw = "<msg><appmsg><type>5</type><title>Retained title</title><sourcedisplayname>Publisher</sourcedisplayname></appmsg></msg>"
        new = _project_message(
            {
                "local_id": 94,
                "local_type": (5 << 32) | 49,
                "server_id": 594,
                "create_time": 1700000094,
                "real_sender_id": 3,
                "message_content": raw,
                "compression_marker": 0,
            },
            native.OWNER,
            "group",
            {3: {"rowid": 3, "user_name": native.MEMBER, "is_session": 0}},
        )
        old = deepcopy(new)
        old["resource"] = {
            key: value
            for key, value in new["resource"].items()
            if key in {"kind", "label", "native_time", "app_subtype", "safe_title"}
        }
        return old, new
    if kind == "forward":
        raw = '<msg><appmsg><type>19</type><title>Source record</title><recorditem><recordinfo><datalist><dataitem datatype="1"><datadesc>Newly decoded leaf</datadesc></dataitem><dataitem datatype="1"><datadesc>Retained neighbor</datadesc></dataitem></datalist></recordinfo></recorditem></appmsg></msg>'
        new = native._message(90, 49, raw=raw)
        old = deepcopy(new)
        old["native_text"]["text_components"] = old["native_text"]["text_components"][
            1:
        ]
        old["native_text"]["omitted_forwarded_items"] = 1
        old["native_text"]["flags"] = [
            "forwarded_text_only",
            "forwarded_non_text_or_unreadable_items_omitted",
            "forwarded_record_partially_unreadable",
        ]
        old["native_text"]["text"] = native_components_text(
            old["native_text"]["text_components"]
        )
        old["text"] = old["native_text"]["text"]
        return old, new
    raw = "<msg><appmsg><type>57</type><title>Literal reply</title><refermsg><type>49</type><svrid>451</svrid><fromusr>wxid_synthetic_original</fromusr><displayname>Original source</displayname><createtime>1700000200</createtime><content>&lt;msg&gt;&lt;appmsg&gt;&lt;type&gt;57&lt;/type&gt;&lt;title&gt;Exact quoted body&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>"
    old = native._message(90, 49, raw=raw)
    new = deepcopy(old)
    if kind == "quoted_card":
        new["native_text"]["quoted_reference"]["app_card_metadata"] = {
            "schema_version": 1,
            "appmsg_subtype": 76,
            "safe_title": "Retained quoted title",
        }
        return old, new
    metadata = new["native_text"]
    metadata["text_components"].append(
        {
            "kind": "quoted_text",
            "text": "Exact quoted body",
            "appmsg_subtype": 57,
            "text_extraction_kind": "quoted_reply",
            "flags": ["quoted_native_reply_body"],
            **metadata["quoted_reference"],
        }
    )
    metadata["flags"] = ["quoted_reply_envelope", "quoted_native_reply_body"]
    metadata["text"] = native_components_text(metadata["text_components"])
    new["text"] = metadata["text"]
    return old, new


def _record(connection, prior, following, source_key, reason):
    proof = json.loads(following["provenance"])
    proof["macos_semantic_reparse"] = {
        "policy": reader.POLICY,
        "sequence": 1,
        "prior_row_sha256": bank._sha(prior),
        "previous_digest": "",
    }
    following["provenance"] = bank._json(proof)
    record = {
        "message_id": prior["id"],
        "sequence": 1,
        "prior_row": bank._json(prior),
        "prior_signature": bank._event_signature(prior),
        "next_row_sha256": bank._sha(following),
        "next_signature": bank._event_signature(following),
        "native_identity_sha256": reader._identity(prior)[1],
        "source_key": source_key,
        "parser_revision": "v1.5.0-codex.0",
        "reason": reason,
        "previous_digest": "",
    }
    record["digest"] = bank._sha({"policy": reader.POLICY, **record})
    connection.execute("DELETE FROM projection_revisions")
    connection.execute(
        "INSERT INTO projection_revisions VALUES("
        + ",".join("?" for _ in record)
        + ")",
        tuple(record.values()),
    )
    columns = list(prior)
    connection.execute(
        "UPDATE messages SET "
        + ",".join(column + "=?" for column in columns)
        + " WHERE id=?",
        (*[following[column] for column in columns], prior["id"]),
    )
    connection.execute(
        "UPDATE event_keys SET signature=? WHERE message_id=?",
        (record["next_signature"], prior["id"]),
    )


def _seal(connection):
    connection.execute(
        "UPDATE archive_info SET value=? WHERE key='payload_sha256'",
        (bank._rows_digest(connection),),
    )
    connection.commit()


@pytest.fixture(params=["card", "forward", "quote", "quoted_card"])
def retained_v4(bank_archive, request):
    fixture = bank_archive
    old, new = _messages(request.param)
    old_packet = {**deepcopy(fixture.group), "messages": [old]}
    native._publish(fixture, packets=[old_packet], name="before-retained-revision")
    connection = sqlite3.connect(fixture.bank_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(reader.SCHEMA)
    prior = dict(connection.execute("SELECT * FROM messages").fetchone())
    following = dict(zip(prior, bank._row(old_packet, new), strict=True))
    for column in reader._STABLE_COLUMNS:
        following[column] = prior[column]
    if request.param in {"card", "quoted_card"}:
        proof = json.loads(following["provenance"])
        if request.param == "card":
            proof["app_card_metadata"] = reader._card_metadata(new["resource"])
        origin = (
            "app_card_metadata_origin" if request.param == "card"
            else "quoted_app_card_metadata_origin"
        )
        proof[origin] = {
            key: proof["macos_recovery"][key] for key in reader._METADATA_SOURCE_FIELDS
        }
        following["provenance"] = bank._json(proof)
    reason = {
        "card": "app_card_metadata_completion",
        "forward": "forwarded_completion",
        "quote": "quoted_reply_completion",
        "quoted_card": "quoted_card_metadata_completion",
    }[request.param]
    source_key = connection.execute("SELECT id FROM sources").fetchone()[0]
    _record(connection, prior, following, source_key, reason)
    connection.execute("UPDATE archive_info SET value='4' WHERE key='schema_version'")
    connection.execute(
        "UPDATE archive_info SET value='v1.4.2-codex.0' WHERE key='code_version'"
    )
    _seal(connection)
    try:
        yield fixture, connection, prior, following, source_key, reason
    finally:
        connection.close()


def test_closed_schema4_proofs_validate_without_rewriting_the_bank(retained_v4):
    fixture, connection, _prior, _following, _source, _reason = retained_v4
    before = fixture.bank_path.read_bytes()
    metadata = bank._validate_bank(connection, fixture.history)
    assert metadata["schema_version"] == "4"
    assert metadata["code_version"] == "v1.4.2-codex.0"
    assert fixture.bank_path.read_bytes() == before


def test_schema3_bank_still_validates_without_a_revision_journal(bank_archive):
    fixture = bank_archive
    native._publish(fixture, packets=[fixture.group], name="retained-schema3")
    before = fixture.bank_path.read_bytes()
    with sqlite3.connect(fixture.bank_path) as connection:
        metadata = bank._validate_bank(connection, fixture.history)
        assert metadata["schema_version"] == "3"
        assert not reader._has_table(connection)
    assert fixture.bank_path.read_bytes() == before


def test_unknown_schema4_writer_version_remains_rejected(retained_v4):
    fixture, connection, *_rest = retained_v4
    connection.execute(
        "UPDATE archive_info SET value='v9.0.0-codex.0' WHERE key='code_version'"
    )
    connection.commit()
    with pytest.raises(ValueError, match="Unsupported Mac recovery bank schema"):
        bank._validate_bank(connection, fixture.history)


@pytest.mark.parametrize(
    "defect",
    [
        "digest",
        "unknown_reason",
        "lost_history",
        "native_identity",
        "stable_clock",
        "semantic_overwrite",
        "extra_schema_column",
    ],
)
def test_revision_tampering_is_rejected_even_after_resealing_payload(
    retained_v4, defect
):
    fixture, connection, prior, following, source, reason = retained_v4
    if defect == "digest":
        connection.execute("UPDATE projection_revisions SET digest=?", ("0" * 64,))
    elif defect == "lost_history":
        connection.execute("DELETE FROM projection_revisions")
    elif defect == "extra_schema_column":
        connection.execute("ALTER TABLE projection_revisions ADD COLUMN untrusted TEXT")
    else:
        if defect == "native_identity":
            proof = json.loads(following["provenance"])
            proof["macos_recovery"]["content_sha256"] = "0" * 64
            following["provenance"] = bank._json(proof)
        elif defect == "stable_clock":
            following["time_sort"] += 1
        elif defect == "semantic_overwrite":
            proof = json.loads(following["provenance"])
            if reason == "app_card_metadata_completion":
                proof["resource_reference"]["safe_title"] = "Changed known title"
                proof["app_card_metadata"]["safe_title"] = "Changed known title"
            elif reason == "forwarded_completion":
                proof["text_components"][-1]["text"] = "Changed retained neighbor"
                following["text"] = native_components_text(proof["text_components"])
            else:
                proof["quoted_reference"]["source_sender_label"] = (
                    "Changed quoted source"
                )
            following["provenance"] = bank._json(proof)
        _record(
            connection,
            prior,
            following,
            source,
            "unknown_policy" if defect == "unknown_reason" else reason,
        )
    _seal(connection)
    with pytest.raises(ValueError):
        bank._validate_bank(connection, fixture.history)


@pytest.mark.parametrize("retained_v4", ["quoted_card"], indirect=True)
@pytest.mark.parametrize(
    "defect",
    ["actor", "server", "unknown_label", "original_type", "origin_missing",
     "origin_raw", "origin_source", "origin_extra", "original_receipt"],
)
def test_quoted_card_revision_rejects_resealed_source_or_reference_tampering(
    retained_v4, defect
):
    fixture, connection, prior, following, source, reason = retained_v4
    proof = json.loads(following["provenance"])
    reference = proof["quoted_reference"]
    if defect == "actor":
        reference["source_sender_id"] = "wxid_other_quoted_actor"
    elif defect == "server":
        reference["source_ids"]["svrid"] = "999"
    elif defect == "unknown_label":
        reference["app_card_metadata"]["unvalidated_label"] = "Unknown"
    elif defect == "original_type":
        reference["original_message_type"] = 3
    elif defect == "origin_missing":
        proof.pop("quoted_app_card_metadata_origin")
    elif defect == "original_receipt":
        proof["macos_recovery"]["source_id"] = "altered-original-receipt"
    else:
        origin = proof["quoted_app_card_metadata_origin"]
        if defect == "origin_raw":
            origin["content_sha256"] = "0" * 64
        elif defect == "origin_source":
            origin["source_id"] = "different-authenticated-source"
        elif defect == "origin_extra":
            origin["unvalidated_field"] = True
    following["provenance"] = bank._json(proof)
    _record(connection, prior, following, source, reason)
    _seal(connection)
    with pytest.raises(ValueError):
        bank._validate_bank(connection, fixture.history)
