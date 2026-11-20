"""Revalidation compares server events within their exact source conversation.

Code version: v1.1.0-codex.0
"""

from contextlib import closing
import json

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_macos_recovery_revalidation as revalidation
from tests import test_wechat_macos_recovery_revalidation as existing


mac_archive = existing.mac_archive
bank_archive = existing.bank_archive
stale_bank = existing.stale_bank
deduplicated_bank = existing.deduplicated_bank


def _bank_row(connection, fixture, *, session=None, structured=False):
    structure = " AND json_type(provenance,'$.text_components')='array'" if structured else ""
    query = (
        "SELECT * FROM macos_recovery_bank.messages WHERE conversation_id=?"
        + structure
        + " ORDER BY ordinal,id LIMIT 1"
    )
    return dict(connection.execute(
        query, (session or fixture.direct_session,),
    ).fetchone())


def _other_conversation(connection, original):
    row = dict(connection.execute(
        "SELECT * FROM temp.test_conversations WHERE id=?", (original,),
    ).fetchone())
    row["id"] = "wechat:synthetic-distinct-source-conversation"
    connection.execute(
        "INSERT INTO temp.test_conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(row.values()),
    )
    return row["id"]


def _observation(connection, original, *, conversation=None, same_signature=False, identifier=None):
    row = dict(original)
    row["id"] = identifier or "synthetic-independent-source-observation"
    row["conversation_id"] = conversation or original["conversation_id"]
    if not same_signature:
        row["text"] = "A distinct raw observation with the same conversation-scoped server number."
        row["search_text"] = row["text"].casefold()
    connection.execute(
        "INSERT INTO temp.test_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(row.values()),
    )
    return row


def _compare(connection):
    return revalidation._compare_base(
        connection, "temp.test_conversations", "temp.test_messages"
    )


@pytest.mark.parametrize("shape", ("different_body", "equal_text_distinct_metadata"))
def test_same_owner_peer_and_server_in_another_conversation_remain_distinct(stale_bank, shape):
    fixture = stale_bank
    bank_before = fixture.bank_path.read_bytes()
    with closing(existing._comparison(fixture)) as connection:
        proved_rows = connection.execute("SELECT COUNT(*) FROM temp.test_messages").fetchone()[0]
        retained = _bank_row(
            connection, fixture, session=fixture.group_session,
            structured=shape == "equal_text_distinct_metadata",
        )
        conversation = _other_conversation(connection, retained["conversation_id"])
        observed = _observation(
            connection, retained, conversation=conversation,
            same_signature=shape == "equal_text_distinct_metadata",
        )
        if shape == "equal_text_distinct_metadata":
            proof = json.loads(observed["provenance"])
            assert proof.pop("text_components")
            proof.pop("original_message_type", None)
            proof.pop("macos_recovery", None)
            observed["provenance"] = json.dumps(proof)
            observed["sender_label"] = "Independent source label"
            connection.execute(
                "UPDATE temp.test_messages SET provenance=?,sender_label=? WHERE id=?",
                (observed["provenance"], observed["sender_label"], observed["id"]),
            )
            assert observed["text"] == retained["text"]
            assert observed["timestamp_value"] == retained["timestamp_value"]
            assert observed["sender_id"] == retained["sender_id"]
            assert observed["direction"] == retained["direction"]
        assert bank._event_signature(observed) != bank._event_signature(retained)
        rows, shadows = _compare(connection)
        assert rows == proved_rows
        assert shadows == 0
        assert connection.execute("SELECT COUNT(*) FROM temp.revalidation_existing").fetchone()[0] == 0
        assert dict(connection.execute(
            "SELECT * FROM temp.test_messages WHERE id=?", (observed["id"],),
        ).fetchone()) == observed
        assert dict(connection.execute(
            "SELECT * FROM macos_recovery_bank.messages WHERE id=?", (retained["id"],),
        ).fetchone()) == retained
    assert fixture.bank_path.read_bytes() == bank_before


@pytest.mark.parametrize("same_signature", (True, False))
def test_new_alias_in_the_same_conversation_is_still_rejected(stale_bank, same_signature):
    with closing(existing._comparison(stale_bank)) as connection:
        _observation(connection, _bank_row(connection, stale_bank), same_signature=same_signature)
        with pytest.raises(ValueError, match="overlaps a retained bank"):
            _compare(connection)


def test_other_conversation_cannot_reuse_a_retained_bank_message_id(stale_bank):
    with closing(existing._comparison(stale_bank)) as connection:
        retained = _bank_row(connection, stale_bank)
        conversation = _other_conversation(connection, retained["conversation_id"])
        _observation(
            connection, retained, conversation=conversation,
            same_signature=True, identifier=retained["id"],
        )
        with pytest.raises(ValueError, match="reuses a bank ID in another conversation"):
            _compare(connection)


def _deduplicated_target(connection, fixture):
    return dict(connection.execute(
        "SELECT * FROM temp.test_messages WHERE id=?", (fixture.target,),
    ).fetchone())


def test_other_conversation_does_not_conflict_with_an_exact_retained_target(deduplicated_bank):
    fixture = deduplicated_bank
    with closing(existing._comparison(fixture)) as connection:
        target = _deduplicated_target(connection, fixture)
        conversation = _other_conversation(connection, target["conversation_id"])
        observed = _observation(connection, target, conversation=conversation)
        assert json.loads(observed["provenance"])["server_id"] == json.loads(target["provenance"])["server_id"]
        assert bank._event_signature(observed) != bank._event_signature(target)
        _compare(connection)
        assert connection.execute(
            "SELECT seen FROM temp.revalidation_existing WHERE id=?", (fixture.target,),
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM temp.revalidation_existing").fetchone()[0] == 1


@pytest.mark.parametrize("defect", ("missing", "changed"))
def test_other_conversation_cannot_replace_the_original_retained_target(deduplicated_bank, defect):
    fixture = deduplicated_bank
    with closing(existing._comparison(fixture)) as connection:
        target = _deduplicated_target(connection, fixture)
        conversation = _other_conversation(connection, target["conversation_id"])
        _observation(connection, target, conversation=conversation, same_signature=True)
        if defect == "missing":
            connection.execute("DELETE FROM temp.test_messages WHERE id=?", (fixture.target,))
        else:
            connection.execute(
                "UPDATE temp.test_messages SET text='Changed original target' WHERE id=?", (fixture.target,),
            )
        with pytest.raises(ValueError, match="deduplication"):
            _compare(connection)


def test_same_conversation_conflicting_alias_still_conflicts_with_retained_target(deduplicated_bank):
    fixture = deduplicated_bank
    with closing(existing._comparison(fixture)) as connection:
        _observation(connection, _deduplicated_target(connection, fixture))
        with pytest.raises(ValueError, match="variant conflicts with a retained deduplication event"):
            _compare(connection)


def test_other_conversation_cannot_reuse_the_exact_retained_target_id(deduplicated_bank):
    fixture = deduplicated_bank
    with closing(existing._comparison(fixture)) as connection:
        target = _deduplicated_target(connection, fixture)
        conversation = _other_conversation(connection, target["conversation_id"])
        _observation(
            connection, target, conversation=conversation,
            same_signature=True, identifier=target["id"],
        )
        with pytest.raises(ValueError, match="deduplication target changed"):
            _compare(connection)
