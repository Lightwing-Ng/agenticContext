"""Bank revalidation admits exact proved contexts before streaming bounded rows.

Code version: v1.0.1-codex.0
"""

from contextlib import closing
from copy import deepcopy
from datetime import datetime
import json
import sqlite3

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_macos_recovery_revalidation as revalidation
from tests import test_wechat_archived_bank_revalidation as archived
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_macos_recovery_revalidation as existing
from tests import test_wechat_revalidation_event_scope as events
from tests import test_wechat_sealed_bank_revalidation as sealed


mac_archive = existing.mac_archive
bank_archive = existing.bank_archive
stale_bank = existing.stale_bank
sealed_case = sealed.sealed_case
archived_case = archived.archived_case


def _compare(connection, *, admission=None, **options):
    return revalidation._compare_base(
        connection, "temp.test_conversations", "temp.test_messages",
        admission=admission, **options,
    )


class _StreamBoundary:
    """Observe or change only fixture TEMP rows after admission, before streaming."""

    def __init__(self, connection, callback=None):
        self.connection = connection
        self.callback = callback
        self.streamed = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.upper().split())
        if normalized.startswith("SELECT * FROM TEMP.TEST_MESSAGES WHERE CONVERSATION_ID"):
            self.streamed.append(parameters[0])
            if self.callback is not None and len(self.streamed) == 1:
                self.callback(self.connection, parameters[0])
        return self.connection.execute(sql, parameters)


def test_default_admission_records_exact_unique_context_and_stream_counts(stale_bank):
    fixture = stale_bank
    evidence = {"status": "verified", "scope_counts": {"unproved": 0}}
    with closing(existing._comparison(fixture)) as connection:
        assert _compare(connection, admission=evidence) == (4, 0)
    assert evidence == {
        "max_base_messages": 5_000_000,
        "max_scope_messages": bank.MAX_SCOPE_MESSAGES,
        "scope_counts": {fixture.direct_session: 2, fixture.group_session: 2},
        "admitted_messages": 4,
        "admitted_scopes": 2,
        "streamed_messages": 4,
        "streamed_scope_counts": {fixture.direct_session: 2, fixture.group_session: 2},
        "status": "verified",
    }


def test_unproved_same_peer_context_does_not_consume_or_change_the_admission(stale_bank, monkeypatch):
    fixture = stale_bank
    monkeypatch.setattr(bank, "MAX_SCOPE_MESSAGES", 2)
    evidence = {}
    with closing(existing._comparison(fixture)) as connection:
        retained = events._bank_row(connection, fixture)
        conversation = events._other_conversation(connection, retained["conversation_id"])
        observed = [events._observation(
            connection, retained, conversation=conversation, identifier=f"unproved-source-{index}",
        ) for index in range(3)]
        monitored = _StreamBoundary(connection)
        assert _compare(monitored, admission=evidence, max_base_messages=4) == (4, 0)
        assert set(monitored.streamed) == {fixture.direct_session, fixture.group_session}
        assert conversation not in evidence["scope_counts"]
        assert [dict(row) for row in connection.execute(
            "SELECT * FROM temp.test_messages WHERE conversation_id=? ORDER BY id", (conversation,),
        )] == observed
    assert evidence["admitted_messages"] == evidence["streamed_messages"] == 4


def _anchor_message(fixture, *, conflict):
    with closing(sqlite3.connect(fixture.history)) as connection:
        connection.row_factory = sqlite3.Row
        original = connection.execute(
            "SELECT * FROM messages WHERE id=?", (fixture.direct["anchors"][0]["archive_message_id"],),
        ).fetchone()
    text = "A conflicting native observation" if conflict else original["text"]
    message = fixtures._message(100, 1, raw=text, sender=fixtures.PEER)
    message.update(
        server_id=str(json.loads(original["provenance"])["server_id"]),
        create_time=int(datetime.fromisoformat(original["timestamp_value"]).timestamp()),
        real_sender_id=2,
        sender_proof=fixtures._proof(2, fixtures.PEER, is_session=1),
        native_text={
            "text": text,
            "flags": [],
            "original_message_type": 1,
            "text_extraction_kind": "plain_text",
        },
    )
    return message


@pytest.fixture(params=("dedup", "gap"))
def proof_only_bank(bank_archive, request):
    fixture = bank_archive
    packet = deepcopy(fixture.direct)
    packet["messages"] = [_anchor_message(fixture, conflict=request.param == "gap")]
    fixture.media = existing._empty_media(fixture.history)
    fixtures._publish(fixture, packets=[packet])
    fixture.proof_kind = request.param
    return fixture


def test_dedup_and_gap_only_contexts_remain_admitted_without_bank_conversation_rows(proof_only_bank):
    fixture = proof_only_bank
    evidence = {}
    with closing(existing._comparison(fixture)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM macos_recovery_bank.conversations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM macos_recovery_bank.messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM macos_recovery_bank.conversation_proofs").fetchone()[0] == 1
        assert _compare(connection, admission=evidence, max_base_messages=2) == (2, 0)
        expected_targets = 1 if fixture.proof_kind == "dedup" else 0
        assert connection.execute("SELECT COUNT(*) FROM temp.revalidation_existing WHERE seen=1").fetchone()[0] == expected_targets
        assert connection.execute("SELECT COUNT(*) FROM macos_recovery_bank.gaps").fetchone()[0] == (fixture.proof_kind == "gap")
    assert evidence["scope_counts"] == {fixture.direct_session: 2}
    assert evidence["streamed_scope_counts"] == evidence["scope_counts"]


def test_multiple_verified_source_proofs_do_not_repeat_a_context(bank_archive):
    fixture = bank_archive
    repeated = deepcopy(fixture.direct)
    repeated["source"]["snapshot_id"] = "synthetic-second-sealed-snapshot"
    fixture.media = existing._empty_media(fixture.history)
    fixtures._publish(fixture, packets=[fixture.direct, repeated])
    evidence = {}
    with closing(existing._comparison(fixture)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM macos_recovery_bank.conversation_proofs").fetchone()[0] == 2
        monitored = _StreamBoundary(connection)
        assert _compare(monitored, admission=evidence, max_base_messages=2) == (2, 0)
        assert monitored.streamed == [fixture.direct_session]
    assert evidence["scope_counts"] == evidence["streamed_scope_counts"] == {fixture.direct_session: 2}


@pytest.mark.parametrize("defect", ("missing", "owner", "peer", "duplicate"))
def test_every_proved_context_requires_its_exact_comparison_membership(stale_bank, defect):
    fixture = stale_bank
    with closing(existing._comparison(fixture)) as connection:
        if defect == "missing":
            connection.execute("DELETE FROM temp.test_conversations WHERE id=?", (fixture.group_session,))
        elif defect == "owner":
            connection.execute(
                "UPDATE temp.test_conversations SET account_key='account:foreign' WHERE id=?", (fixture.group_session,),
            )
        elif defect == "peer":
            connection.execute(
                "UPDATE temp.test_conversations SET peer_id='foreign-peer' WHERE id=?", (fixture.group_session,),
            )
        else:
            connection.execute(
                "INSERT INTO temp.test_conversations SELECT * FROM temp.test_conversations WHERE id=?",
                (fixture.group_session,),
            )
        monitored = _StreamBoundary(connection)
        with pytest.raises(ValueError):
            _compare(monitored)
        assert monitored.streamed == []


@pytest.mark.parametrize("limit", (True, False, 0, -1, 1.5, "5", None))
def test_global_admission_requires_a_strict_positive_integer(stale_bank, limit):
    with closing(existing._comparison(stale_bank)) as connection:
        monitored = _StreamBoundary(connection)
        with pytest.raises(ValueError):
            _compare(monitored, max_base_messages=limit)
        assert monitored.streamed == []


def test_each_context_may_reach_its_scope_limit_within_the_separate_global_limit(stale_bank, monkeypatch):
    fixture = stale_bank
    monkeypatch.setattr(bank, "MAX_SCOPE_MESSAGES", 2)
    evidence = {}
    with closing(existing._comparison(fixture)) as connection:
        assert _compare(connection, max_base_messages=4, admission=evidence) == (4, 0)
    assert evidence["max_scope_messages"] == 2
    assert evidence["admitted_messages"] == evidence["streamed_messages"] == 4
    assert all(count == 2 for count in evidence["streamed_scope_counts"].values())


@pytest.mark.parametrize("scope_limit,total_limit", ((1, 4), (2, 3)))
def test_scope_and_aggregate_overflow_fail_before_any_body_stream(stale_bank, monkeypatch, scope_limit, total_limit):
    monkeypatch.setattr(bank, "MAX_SCOPE_MESSAGES", scope_limit)
    with closing(existing._comparison(stale_bank)) as connection:
        monitored = _StreamBoundary(connection)
        with pytest.raises(ValueError):
            _compare(monitored, max_base_messages=total_limit)
        assert monitored.streamed == []


@pytest.mark.parametrize("mutation", ("extra", "missing"))
def test_stream_must_match_the_precounted_scope_and_total_admission(stale_bank, mutation):
    fixture = stale_bank

    def change(connection, conversation):
        if mutation == "missing":
            connection.execute(
                "DELETE FROM temp.test_messages WHERE rowid=(SELECT rowid FROM temp.test_messages "
                "WHERE conversation_id=? LIMIT 1)", (conversation,),
            )
        else:
            original = dict(connection.execute(
                "SELECT * FROM temp.test_messages WHERE conversation_id=? LIMIT 1", (conversation,),
            ).fetchone())
            original["id"] = "synthetic-row-arriving-after-admission"
            original["provenance"] = json.dumps({"server_id": "999999", "message_type": 1})
            connection.execute(
                "INSERT INTO temp.test_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(original.values()),
            )

    with closing(existing._comparison(fixture)) as connection:
        monitored = _StreamBoundary(connection, change)
        with pytest.raises(ValueError):
            _compare(monitored, max_base_messages=4)
        assert monitored.streamed


def test_sealed_validation_pins_its_explicit_admission_to_current_inputs(sealed_case):
    case = sealed_case
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        evidence = sealed._validate(case, guard, max_base_messages=8_516_147)
        admission = evidence["base_message_admission"]
        assert admission["max_base_messages"] == 8_516_147
        assert admission["admitted_messages"] == admission["streamed_messages"] == evidence["current_base_rows_checked"]
        assert admission["scope_counts"] == admission["streamed_scope_counts"]
        assert admission["input_identities"] == evidence["input_identities"] == bank._inputs(case.fixture.history)
    case.clones.assert_not_called()
    sealed._unchanged(case)


def test_archived_staging_forwards_the_explicit_admission_without_changing_preservation(archived_case):
    case = archived_case
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        receipt = archived._stage(case, guard, max_base_messages=8_516_147)
        admission = receipt["revalidation"]["base_message_admission"]
        assert admission["max_base_messages"] == 8_516_147
        assert admission["admitted_messages"] == admission["streamed_messages"]
        assert admission["scope_counts"] == admission["streamed_scope_counts"]
        assert admission["input_identities"] == receipt["input_identities"] == bank._inputs(case.fixture.history)
        case.clones.assert_called_once_with(case.source, case.candidate)
    archived._unchanged(case)
