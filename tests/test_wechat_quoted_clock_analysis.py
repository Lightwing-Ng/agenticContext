"""Contact cache upgrades recover original clocks without borrowing reply authors.

Code version: v1.1.4-codex.0
"""

from contextlib import closing

from datetime import datetime
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.wechat_archive import _identifier, wechat_archive_path
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from app.core.wechat_text import native_components_text
from tests.test_wechat_contacts import bind, insert_contact
from tests.test_wechat_reconcile import merge, record, source


OWNER = "wxid_quoted_clock_owner"
TARGET = "wxid_quoted_clock_target"
OTHER = "wxid_quoted_clock_original_author"
MEMBER = "wxid_quoted_clock_associated_identity"
ROOM = "888800001111@chatroom"
BODY = "Original statement; preserve  two spaces\nAnd a literal second line."
REPLY = "The current author's separate reply."
MEMBER_BODY = "The associated identity's own statement."
ORIGINAL_TIME = "2026-01-02T10:00:00+08:00"
REPLY_TIME = "2026-01-02T10:02:57+08:00"
ORIGINAL_SERVER = "9000000000000000001"
LEGACY_CLOCK_RULE = "v0.0.0-synthetic-unknown-clocks"


def _native_record(metadata, name, sender, text, timestamp, server, **fields):
    account_hash = metadata["account_hash"]
    return record(
        metadata,
        name,
        peer=ROOM,
        sender_id=sender,
        sender_display_name="Literal source nickname",
        text=text,
        sent_at_utc_iso=timestamp,
        sent_at_raw=timestamp,
        unix_timestamp=int(datetime.fromisoformat(timestamp).timestamp()),
        server_id=server,
        source_locator={
            "database": "message_0.sqlite",
            "database_relative_path": f"Documents/{account_hash}/DB/message_0.sqlite",
            "table": "Chat_" + hashlib.md5(ROOM.encode()).hexdigest(),
            "local_id": {"original": 1, "reply": 2, "member": 3}[name],
        },
        **fields,
    )


@pytest.fixture
def quoted_clock_store(tmp_path):
    def build(*, original_author=TARGET, associated=False, explicit_quote_author=False):
        metadata = {**source(OWNER), "source_label": "iPad archive"}
        reference = {
            "original_message_type": 1,
            "source_sender_id": TARGET if explicit_quote_author else ROOM,
            "source_chat_id": TARGET,
            "source_sender_label": "Historical quoted nickname",
            "source_timestamp": "",
            "source_ids": {"svrid": ORIGINAL_SERVER},
        }
        components = [
            {"kind": "reply_text", "text": REPLY},
            {"kind": "quoted_text", "text": BODY, **reference},
        ]
        records = [
            _native_record(
                metadata,
                "original",
                original_author,
                BODY,
                ORIGINAL_TIME,
                ORIGINAL_SERVER,
            ),
            _native_record(
                metadata,
                "reply",
                TARGET,
                native_components_text(components),
                REPLY_TIME,
                "9000000000000000002",
                message_type=49,
                appmsg_subtype=57,
                text_extraction_kind="quoted_reply",
                text_components=components,
                quoted_reference=reference,
            ),
        ]
        if associated:
            records.append(
                _native_record(
                    metadata,
                    "member",
                    MEMBER,
                    MEMBER_BODY,
                    "2026-01-02T10:03:35+08:00",
                    "9000000000000000003",
                )
            )
        merge(tmp_path, [metadata], records, "synthetic-ipad-clock-snapshot")
        history = wechat_archive_path(tmp_path)
        registry = contact_registry_path(tmp_path)
        with closing(sqlite3.connect(registry)) as connection, connection:
            initialize_contact_registry(connection)
            contacts = {}
            for sender in (TARGET, OTHER, MEMBER):
                identifier = insert_contact(connection, sender)
                contacts[sender] = identifier
                bind(
                    connection,
                    identifier,
                    source=metadata["source_id"],
                    sender=sender,
                )
        if associated:
            history.with_name("contact_merges.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "associations": [
                            {
                                "target_contact_id": contacts[TARGET],
                                "member_contact_ids": [
                                    contacts[TARGET],
                                    contacts[MEMBER],
                                ],
                                "authority": "user_confirmed_same_person",
                                "evidence_id": hashlib.sha256(
                                    b"Synthetic explicit association confirmation"
                                ).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
        parent = _identifier(metadata["source_id"], "reply")
        return SimpleNamespace(
            store=tmp_path,
            history=history,
            registry=registry,
            contacts=contacts,
            target=contacts[TARGET],
            member=contacts[MEMBER],
            original=_identifier(metadata["source_id"], "original"),
            parent=parent,
            quote=parent + "_quoted_2",
            source_id=metadata["source_id"],
            preserved=(history.read_bytes(), registry.read_bytes()),
        )

    return build


def _old_index(fixture):
    with (
        patch.object(analysis, "QUOTED_CLOCK_VERSION", LEGACY_CLOCK_RULE, create=True),
        patch.object(analysis, "unverified_room_quote_ids", return_value=[]),
        patch.object(
            analysis,
            "recover_quoted_reference_clocks",
            return_value={
                "status": "ready",
                "recovered": 0,
                "verified": 0,
                "rejected_author_ids": [],
            },
            create=True,
        ),
    ):
        result = analysis.query_contact_analysis(fixture.store, fixture.target)
    assert result["status"] == "ready"
    return result


def _cache(fixture):
    return analysis.contact_analysis_cache_path(fixture.store, fixture.target)


def _reference_state(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        return {
            "references": connection.execute(
                "SELECT id,parent_message_id,component_path,anchor_id,conversation_id,"
                "source_id,account_key,ordinal,source_clock FROM refs ORDER BY id"
            ).fetchall(),
            "groups": connection.execute(
                "SELECT * FROM statement_groups ORDER BY id"
            ).fetchall(),
            "members": connection.execute(
                "SELECT * FROM statement_members ORDER BY reference_id"
            ).fetchall(),
        }


def _ref(path, identifier):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        found = connection.execute(
            "SELECT * FROM refs WHERE id=?", (identifier,)
        ).fetchone()
        return dict(found) if found else None


def _no_source_rescan(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("A complete accepted-reference index must be replayed.")

    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, name, fail)


@pytest.mark.parametrize("explicit_quote_author", [False, True])
def test_complete_index_recovers_quoted_original_clock_without_rescanning(
    quoted_clock_store, monkeypatch, explicit_quote_author
):
    fixture = quoted_clock_store(explicit_quote_author=explicit_quote_author)
    old = _old_index(fixture)
    cache = _cache(fixture)
    before = _reference_state(cache)
    unknown = _ref(cache, fixture.quote)
    assert unknown["has_time"] == 0 and unknown["timestamp_value"] == ""
    assert unknown["source_clock"] == ""
    _no_source_rescan(monkeypatch)

    updated = analysis.query_contact_analysis(fixture.store, fixture.target)

    assert updated["status"] == "ready"
    assert updated["source_fingerprint"] != old["source_fingerprint"]
    recovered = _ref(cache, fixture.quote)
    assert recovered["has_time"] == 1
    assert recovered["timestamp_value"] == ORIGINAL_TIME
    assert recovered["timestamp_value"] != REPLY_TIME
    assert recovered["timestamp_label"] == "2 Jan 2026 10:00:00"
    assert recovered["time_basis"].startswith("Recovered original message time; ")
    assert recovered["source_clock"] == ""
    after = _reference_state(cache)
    assert after["references"] == before["references"]
    assert updated["total_count"] == 2
    if explicit_quote_author:
        assert old["total_count"] == 3
        assert len(after["groups"]) == len(before["groups"]) - 1
        assert {row for row in after["members"] if row[0] != fixture.quote} == {
            row for row in before["members"] if row[0] != fixture.quote
        }
    else:
        assert after == before
        assert updated["total_count"] == old["total_count"]
    with closing(sqlite3.connect(cache)) as connection, connection:
        memberships = dict(
            connection.execute(
                "SELECT reference_id,statement_id FROM statement_members"
            )
        )
    assert memberships[fixture.quote] == memberships[fixture.original]
    occurrences = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            fixture.target,
            source_fingerprint=updated["source_fingerprint"],
            source_occurrences=True,
        )
    )
    exported_quote = next(item for item in occurrences if item["id"] == fixture.quote)
    assert exported_quote["content_text"] == BODY
    assert exported_quote["timestamp_value"] == ORIGINAL_TIME
    assert exported_quote["source_clock"] == ""
    assert exported_quote["provenance"]["source_time"] == ""
    with closing(sqlite3.connect(cache)) as connection, connection:
        proof = connection.execute(
            "SELECT original_message_id,original_server_id,original_sender_id "
            "FROM quoted_clock_evidence WHERE reference_id=?",
            (fixture.quote,),
        ).fetchone()
    assert proof == (fixture.original, ORIGINAL_SERVER, TARGET)
    assert (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    ) == fixture.preserved


def test_wrong_quoted_author_is_excluded_without_losing_the_persons_reply(
    quoted_clock_store, monkeypatch
):
    fixture = quoted_clock_store(original_author=OTHER)
    old = _old_index(fixture)
    cache = _cache(fixture)
    assert _ref(cache, fixture.quote)["timestamp_value"] == ""
    assert old["total_count"] == 2
    actual_author = analysis.query_contact_analysis(
        fixture.store, fixture.contacts[OTHER]
    )
    assert actual_author["total_count"] == 1
    assert actual_author["items"][0]["content_text"] == BODY
    assert actual_author["items"][0]["timestamp_value"] == ORIGINAL_TIME
    _no_source_rescan(monkeypatch)

    result = analysis.query_contact_analysis(fixture.store, fixture.target)

    assert result["status"] == "ready" and result["total_count"] == 1
    assert [item["content_text"] for item in result["items"]] == [REPLY]
    assert result["items"][0]["timestamp_value"] == REPLY_TIME
    assert _ref(cache, fixture.parent) is not None
    assert _ref(cache, fixture.quote) is None
    with closing(sqlite3.connect(cache)) as connection, connection:
        proof = connection.execute(
            "SELECT status,original_message_id,original_sender_id,reference_before_json "
            "FROM quoted_clock_evidence WHERE reference_id=?",
            (fixture.quote,),
        ).fetchone()
    assert proof[:3] == ("wrong_author", fixture.original, OTHER)
    assert json.loads(proof[3])["id"] == fixture.quote
    assert (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    ) == fixture.preserved


@pytest.mark.parametrize("original_author", [TARGET, OTHER])
def test_recovered_and_rejected_proofs_survive_a_compatible_cache_upgrade(
    quoted_clock_store, monkeypatch, original_author
):
    fixture = quoted_clock_store(original_author=original_author)
    _old_index(fixture)
    cache = _cache(fixture)
    analysis.query_contact_analysis(fixture.store, fixture.target)
    corrected = _reference_state(cache)
    with closing(sqlite3.connect(cache)) as connection, connection:
        proof_before = connection.execute(
            "SELECT * FROM quoted_clock_evidence ORDER BY reference_id"
        ).fetchall()
    assert proof_before
    _no_source_rescan(monkeypatch)
    with patch.object(analysis, "QUOTED_CLOCK_VERSION", "v999.0.0-synthetic-upgrade"):
        rebuilt = analysis.query_contact_analysis(fixture.store, fixture.target)
    assert rebuilt["status"] == "ready"
    assert rebuilt["total_count"] == (2 if original_author == TARGET else 1)
    assert _reference_state(cache) == corrected
    with closing(sqlite3.connect(cache)) as connection, connection:
        assert (
            connection.execute(
                "SELECT * FROM quoted_clock_evidence ORDER BY reference_id"
            ).fetchall()
            == proof_before
        )
    assert (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    ) == fixture.preserved


def test_person_union_keeps_native_identities_after_quote_attribution_repair(
    quoted_clock_store,
):
    fixture = quoted_clock_store(original_author=OTHER, associated=True)
    old = _old_index(fixture)
    assert old["total_count"] == 3

    result = analysis.query_contact_analysis(fixture.store, fixture.member)

    assert result["status"] == "ready" and result["total_count"] == 2
    assert {item["content_text"] for item in result["items"]} == {REPLY, MEMBER_BODY}
    assert {item["sender_id"] for item in result["items"]} == {TARGET, MEMBER}
    for item in result["items"]:
        expected = fixture.target if item["sender_id"] == TARGET else fixture.member
        assert item["source_contact_id"] == expected
        assert item["source_contact_ids"] == [expected]
    assert BODY not in {item["content_text"] for item in result["items"]}
    assert (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    ) == fixture.preserved


def test_excluding_a_preferred_quote_keeps_the_valid_group_member(quoted_clock_store):
    fixture = quoted_clock_store(original_author=OTHER)
    _old_index(fixture)
    cache = _cache(fixture)
    with closing(sqlite3.connect(cache)) as connection, connection:
        group = connection.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?",
            (fixture.quote,),
        ).fetchone()[0]
        parent_group = connection.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?",
            (fixture.parent,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
            (group, fixture.parent),
        )
        connection.execute("DELETE FROM statement_groups WHERE id=?", (parent_group,))
        analysis._discard_misattributed_quoted_references(connection, [fixture.quote])
        assert connection.execute(
            "SELECT reference_id FROM statement_groups WHERE id=?", (group,)
        ).fetchone() == (fixture.parent,)
        assert connection.execute(
            "SELECT reference_id FROM statement_members WHERE statement_id=?", (group,)
        ).fetchall() == [(fixture.parent,)]
        assert (
            connection.execute(
                "SELECT id FROM refs WHERE id=?", (fixture.quote,)
            ).fetchone()
            is None
        )
        assert connection.execute(
            "SELECT id FROM refs WHERE id=?", (fixture.parent,)
        ).fetchone() == (fixture.parent,)
    assert (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    ) == fixture.preserved


def test_quote_exclusion_cannot_remove_a_primary_message(quoted_clock_store):
    fixture = quoted_clock_store(original_author=OTHER)
    _old_index(fixture)
    cache = _cache(fixture)
    before = _reference_state(cache)
    with closing(sqlite3.connect(cache)) as connection, connection:
        analysis._discard_misattributed_quoted_references(connection, [fixture.parent])
    assert _reference_state(cache) == before
    assert (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    ) == fixture.preserved


def test_recovered_quote_does_not_move_an_unrelated_primary_group(quoted_clock_store):
    fixture = quoted_clock_store()
    result = analysis.query_contact_analysis(fixture.store, fixture.target)
    assert result["status"] == "ready"
    cache = _cache(fixture)
    with closing(sqlite3.connect(cache)) as connection, connection:
        original_group = connection.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?",
            (fixture.original,),
        ).fetchone()[0]
        reply_group = connection.execute(
            "SELECT statement_id FROM statement_members WHERE reference_id=?",
            (fixture.parent,),
        ).fetchone()[0]
        assert original_group != reply_group
        connection.execute(
            "UPDATE statement_members SET statement_id=? WHERE reference_id=?",
            (reply_group, fixture.quote),
        )
        connection.execute(
            "UPDATE statement_groups SET reference_id=? WHERE id=?",
            (fixture.quote, reply_group),
        )
    before = _reference_state(cache)

    with closing(sqlite3.connect(cache)) as connection, connection:
        analysis._link_recovered_quoted_origins(connection)

    assert _reference_state(cache) == before
    with closing(sqlite3.connect(cache)) as connection, connection:
        members = dict(
            connection.execute(
                "SELECT reference_id,statement_id FROM statement_members"
            )
        )
    assert members[fixture.parent] == members[fixture.quote] == reply_group
    assert members[fixture.original] == original_group
    assert (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
    ) == fixture.preserved
