"""Reviewed replay preserves a stronger prior quote and fails closed on conflict.

Code version: v1.0.0-codex.0
"""

from copy import deepcopy
import json
import sqlite3

import pytest

from app.core import wechat_secondary_completions as common
from app.core import wechat_quote_enrichment_replay as replay
from tests import test_wechat_secondary_completions as fixtures
from tests import test_wechat_macos_recovery_bank as native
from tests import test_wechat_resource_sender_evidence as sender


bank_archive = fixtures.bank_archive
completion_fixture = fixtures.completion_fixture
mac_archive = fixtures.mac_archive
source_binding = fixtures.source_binding
witness = fixtures.witness


@pytest.fixture
def replay_case(completion_fixture):
    fixture = completion_fixture
    packet = deepcopy(fixture["packet"])
    raw = (
        "<msg><appmsg><title>Verified reply</title><type>57</type><refermsg>"
        "<type>1</type><svrid>504</svrid><fromusr>" + native.ROOM + "</fromusr>"
        "<chatusr>" + native.MEMBER + "</chatusr>"
        "<displayname>Literal original</displayname><createtime>1700000004</createtime>"
        "<content>Known quoted body</content></refermsg></appmsg></msg>"
    )
    old = native._message(10, 49, sender=sender.PRIMARY, raw=raw)
    old.update(real_sender_id=9, sender_proof=native._proof(9, sender.PRIMARY))
    header = {key: value for key, value in packet.items() if key != "messages"}
    old_event = {**header, **old}
    new_header = deepcopy(header)
    new_header["source"].update(
        source_id="macos-less-complete-native-source",
        snapshot_id="synthetic-less-complete-native-snapshot",
        main_sha256="3" * 64,
    )
    new_header["account_binding"]["source"] = {
        key: new_header["source"][key]
        for key in new_header["account_binding"]["source"]
    }
    new = deepcopy(old)
    new["native_text"]["text_components"] = [
        component
        for component in new["native_text"]["text_components"]
        if component["kind"] == "reply_text"
    ]
    new["text"] = new["native_text"]["text"] = "Verified reply"
    new["native_text"]["flags"] = ["quoted_reply_envelope", "quoted_text_unavailable"]
    derived = sqlite3.connect(":memory:")
    derived.row_factory = sqlite3.Row
    derived.executescript(common._SCHEMA)
    common._append_tables(derived)
    with (
        common._readonly(fixture["history"]) as history,
        common._readonly(fixture["catalog"]) as catalog,
    ):
        snapshots = common._snapshot_resolver(history, catalog)
        original = dict(
            catalog.execute(
                "SELECT * FROM messages WHERE id=?", (fixture["quote"]["id"],)
            ).fetchone()
        )
        conversation = dict(
            catalog.execute(
                "SELECT * FROM conversations WHERE id=?", (original["conversation_id"],)
            ).fetchone()
        )
        completed = common._complete_resource(
            original, conversation, [old_event], snapshots
        )
    assert completed is not None and completed[2] == "native_quote"
    projected = {**completed[0], "time_sort": float(completed[0]["time_sort"])}
    for source_header, message in ((header, old), (new_header, new)):
        key = common._sha(source_header)
        derived.execute(
            "INSERT INTO sources VALUES(?,?,?)", (key, common._json(source_header), key)
        )
        witness_key = common._sha([key, message["local_id"], message["content_sha256"]])
        derived.execute(
            "INSERT INTO witnesses VALUES(?,?,?,?)",
            (witness_key, key, common._json(message), common._sha(message)),
        )
    old_key, new_key = common._sha(header), common._sha(new_header)
    old_witness = common._sha([old_key, old["local_id"], old["content_sha256"]])
    new_witness = common._sha([new_key, new["local_id"], new["content_sha256"]])
    row = fixture["quote"]
    derived.execute(
        "INSERT INTO incremental_resource_conflicts VALUES(?,?,?,?,?,?,?,?)",
        (
            1,
            row["id"],
            common._sha(projected),
            old_key,
            old_witness,
            new_key,
            new_witness,
            replay._REASON,
        ),
    )
    derived.execute(
        "INSERT INTO ambiguities VALUES(?,?,?)",
        (row["id"], row["conversation_id"], replay._REASON),
    )
    derived.commit()
    decision = {
        "ledger_id": 1,
        "message_id": row["id"],
        "prior_row_sha256": common._sha(projected),
        "prior_source_key": old_key,
        "prior_witness_key": old_witness,
        "source_key": new_key,
        "witness_key": new_witness,
        "prior_native_sha256": common._sha(old),
        "native_sha256": common._sha(new),
        "integrity": True,
        "event_scope": True,
        "same_raw_content_sha256": True,
        "same_physical_native_row": False,
        "same_source_account": True,
        "sender_conflict": False,
        "hard_fact_conflict_fields": [],
        "reply_relation": "exact",
        "quoted_body_relation": "old_present_new_absent",
        "native_clock_delta": 0,
        "decision": "same_reply_less_complete_observation",
    }
    yield fixture, derived, decision, projected
    derived.close()


def _run(case, *, cache=None):
    fixture, derived, decision, _projected = case
    with (
        common._readonly(fixture["history"]) as history,
        common._readonly(fixture["catalog"]) as catalog,
    ):
        return replay.replay_quote_enrichment(
            derived,
            history,
            catalog,
            decision,
            snapshots=common._snapshot_resolver(history, catalog),
            source_cache={} if cache is None else cache,
        )


def _reseal_new(case, change):
    _fixture, derived, decision, _projected = case
    key = decision["witness_key"]
    record = derived.execute("SELECT * FROM witnesses WHERE id=?", (key,)).fetchone()
    message = json.loads(record["message"])
    change(message)
    digest = common._sha(message)
    new_key = common._sha(
        [record["source_key"], message["local_id"], message["content_sha256"]]
    )
    derived.execute(
        "UPDATE witnesses SET id=?,message=?,sha256=? WHERE id=?",
        (new_key, common._json(message), digest, key),
    )
    derived.execute(
        "UPDATE incremental_resource_conflicts SET witness_key=? WHERE id=1", (new_key,)
    )
    decision.update(witness_key=new_key, native_sha256=digest)


def test_replay_regenerates_the_exact_prior_row_without_database_writes(replay_case):
    fixture, derived, decision, projected = replay_case
    before = list(derived.iterdump())
    sources_before = {key: fixture[key].read_bytes() for key in ("history", "catalog")}
    result = _run(replay_case)
    assert result["message"] == projected
    assert common._sha(result["message"]) == decision["prior_row_sha256"]
    assert (
        result["event"]["native_text"]["text_components"][-1]["kind"] == "quoted_text"
    )
    assert result["strategy"] == "native_quote" and result["ledger_id"] == 1
    assert list(derived.iterdump()) == before
    assert {key: fixture[key].read_bytes() for key in sources_before} == sources_before


def test_validated_account_headers_are_cached_only_for_the_exact_connection(
    replay_case, monkeypatch
):
    from app.core import wechat_macos_recovery_bank as bank

    fixture, derived, decision, _projected = replay_case
    calls = []
    verify = bank._validate_account_binding

    def counted(history, packet):
        calls.append(history)
        return verify(history, packet)

    monkeypatch.setattr(bank, "_validate_account_binding", counted)
    cache = {}
    with (
        common._readonly(fixture["history"]) as history,
        common._readonly(fixture["catalog"]) as catalog,
    ):
        snapshots = common._snapshot_resolver(history, catalog)
        for _index in range(2):
            replay.replay_quote_enrichment(
                derived,
                history,
                catalog,
                decision,
                snapshots=snapshots,
                source_cache=cache,
            )
        assert len(calls) == 2
    _run(replay_case, cache=cache)
    assert len(calls) == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("ledger_id", True),
        ("message_id", "0" * 64),
        ("prior_row_sha256", "0" * 64),
        ("prior_source_key", "0" * 64),
        ("native_sha256", "0" * 64),
        ("native_clock_delta", True),
        ("same_raw_content_sha256", False),
        ("decision", "unreviewed"),
    ],
)
def test_forged_decisions_cannot_select_or_reinterpret_a_ledger(
    replay_case, field, value
):
    replay_case[2][field] = value
    with pytest.raises(ValueError):
        _run(replay_case)


@pytest.mark.parametrize(
    "change", ["reply", "reference", "clock", "actor", "outer_type", "body_present"]
)
def test_resealed_new_conflicts_are_still_rejected(replay_case, change):
    def alter(message):
        native_text = message["native_text"]
        if change == "reply":
            message["text"] = native_text["text"] = native_text["text_components"][0][
                "text"
            ] = "Conflicting reply"
        elif change == "reference":
            native_text["quoted_reference"]["source_sender_id"] = (
                "wxid_other_quoted_actor"
            )
        elif change == "clock":
            message["create_time"] += 2
            replay_case[2]["native_clock_delta"] = 2
        elif change == "actor":
            message.update(
                sender_id="wxid_other_actor",
                real_sender_id=3,
                sender_proof=native._proof(3, "wxid_other_actor"),
            )
        elif change == "outer_type":
            native_text["appmsg_subtype"] = 1
        else:
            native_text["text_components"].append(
                {
                    "kind": "quoted_text",
                    "text": "Different quoted body",
                    **native_text["quoted_reference"],
                }
            )
            message["text"] = native_text["text"] = (
                "Verified reply\n\nDifferent quoted body"
            )

    _reseal_new(replay_case, alter)
    with pytest.raises(ValueError):
        _run(replay_case)


@pytest.mark.parametrize(
    "change",
    ["source_hash", "witness_hash", "ambiguity", "original_owner", "projected_hash"],
)
def test_hash_scope_and_original_ambiguity_boundaries_remain_closed(
    replay_case, change
):
    fixture, derived, decision, _projected = replay_case
    if change == "source_hash":
        derived.execute(
            "UPDATE sources SET sha256=? WHERE id=?", ("0" * 64, decision["source_key"])
        )
    elif change == "witness_hash":
        derived.execute(
            "UPDATE witnesses SET sha256=? WHERE id=?",
            ("0" * 64, decision["witness_key"]),
        )
    elif change == "ambiguity":
        derived.execute(
            "UPDATE ambiguities SET reason='Multiple physical native witnesses match the original event.'"
        )
    elif change == "projected_hash":
        derived.execute(
            "UPDATE incremental_resource_conflicts SET prior_row_sha256=?", ("0" * 64,)
        )
        decision["prior_row_sha256"] = "0" * 64
    else:
        with sqlite3.connect(fixture["catalog"]) as catalog:
            catalog.execute(
                "UPDATE conversations SET account_key='account:wxid_other_original_owner'"
            )
    with pytest.raises(ValueError):
        _run(replay_case)
