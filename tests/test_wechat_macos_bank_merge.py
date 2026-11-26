"""Sealed-bank salvage preserves newer publications and exact source observations.

Code version: v1.0.5-codex.0
"""

from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import pytest

from app.core import wechat_macos_bank_merge as merge
from app.core import wechat_macos_recovery_bank as bank
from app.core.owner_only_files import owner_only_problem
from app.core.wechat_text import native_components_text
from scripts import refresh_wechat_globally as runner
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_macos_reparse as repairs


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


def _rows(path, table):
    with closing(sqlite3.connect(path)) as connection, connection:
        return connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()


def _packets(fixture, kind):
    if kind in {"quoted_card", "later_quoted_card"}:
        raw = (
            "<msg><appmsg><type>57</type><title>Literal reply stays intact</title>"
            "<refermsg><type>49</type><svrid>451</svrid>"
            "<fromusr>wxid_synthetic_original</fromusr>"
            "<displayname>Original source</displayname>"
            "<createtime>1700000000</createtime><content>"
            "<msg><appmsg><type>76</type><title>Literal music title</title>"
            "</appmsg><appinfo><appname>Source application</appname></appinfo>"
            "</msg></content></refermsg></appmsg></msg>"
        )
        new = deepcopy(fixture.group)
        new["messages"] = [fixtures._message(96, 49, raw=raw)]
        old = deepcopy(new)
        reference = old["messages"][0]["native_text"]["quoted_reference"]
        reference.pop("app_card_metadata")
        if kind == "later_quoted_card":
            new["source"].update(
                source_id="macos-synthetic-later-quoted-card-source",
                snapshot_id="synthetic-later-quoted-card-snapshot",
                main_sha256="e" * 64,
            )
        return old, new
    if kind == "pat":
        old = deepcopy(fixture.group)
        old["messages"] = [{
            "local_id": 93, "local_type": (62 << 32) | 49,
            "server_id": "593", "create_time": 1700000093,
            "content_sha256": "7" * 64, "real_sender_id": 18,
            "sender_id": "wxid_synthetic_internal", "sender_label": "wxid_synthetic_internal",
            "direction": "received", "sender_proof": fixtures._proof(18, "wxid_synthetic_internal"),
            "text": "[App message]", "resource": {
                "kind": "app", "label": "[App message]",
                "native_time": 1700000093, "app_subtype": "62",
            },
        }]
        new = deepcopy(old)
        message = new["messages"][0]
        message.pop("resource")
        message.pop("sender_proof")
        message.update(
            sender_id="", sender_label="System Message", direction="unknown",
            text="Native actor tickled native target",
        )
        message["native_text"] = {
            "text": message["text"], "text_extraction_kind": "system_text",
            "original_message_type": 49, "appmsg_subtype": 62,
            "flags": ["system_notice"], "system_message_subtype": "pat",
            "system_source_ids": {
                "patinfo/fromusername": "wxid_synthetic_actor",
                "patinfo/chatusername": fixtures.ROOM,
                "patinfo/pattedusername": "wxid_synthetic_target",
            },
        }
        return old, new
    if kind in {"card", "later_card"}:
        old, new = repairs._app_card_packets(fixture)
        if kind == "later_card":
            new["source"].update(
                source_id="macos-synthetic-later-card-source",
                snapshot_id="synthetic-later-card-snapshot",
                main_sha256="d" * 64,
            )
        return old, new
    return repairs._packets(fixture, kind)


@pytest.mark.parametrize(
    "kind",
    ("quote", "forward", "pat", "card", "later_card", "quoted_card", "later_quoted_card"),
)
def test_sealed_candidate_merges_repairs_and_additions_without_replacing_new_user_data(
    bank_archive, kind
):
    fixture = bank_archive
    old, new = _packets(fixture, kind)
    fixtures._publish(fixture, packets=[old], name="shared-merge-baseline")
    new["messages"].append(fixtures._message(95, 1, raw="Synthetic global-only event"))
    incoming, _stale_receipt = fixtures._stage(
        fixture, packets=[new], name="sealed-unpublished-global"
    )
    fixtures._publish(fixture, packets=[fixture.direct], name="newer-user-publication")
    if kind == "later_card":
        # Preserve a real transitional writer tag without retagging the input in merge code.
        with closing(sqlite3.connect(fixture.bank_path)) as connection, connection:
            connection.execute(
                "UPDATE archive_info SET value='v1.5.0-codex.0' WHERE key='code_version'"
            )
    prior_occurrences = _rows(fixture.bank_path, "occurrences")
    prior_messages = _rows(fixture.bank_path, "messages")
    original_bank = fixture.bank_path.read_bytes()
    original_incoming, original_history = incoming.read_bytes(), fixture.history.read_bytes()
    candidate = fixture.history.with_name("rebased-global.sqlite3")
    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        before = runner.bank_summary(fixture.bank_path)
        receipt = merge.stage_merged_mac_recovery_bank(
            fixture.root, incoming, candidate, account_ids=(fixtures.OWNER,),
            publication_guard=guard,
        )
        assert fixture.bank_path.read_bytes() == original_bank
        runner.verify_preservation(fixture.bank_path, candidate, before, receipt)
        bank.publish_mac_recovery_bank(
            fixture.root, candidate, receipt, publication_guard=guard
        )
    assert receipt["added"] == 1 and receipt["reparsed"] == 1
    assert receipt["merge"]["synthesized_latest_projections"] == 1
    assert set(prior_occurrences) <= set(_rows(fixture.bank_path, "occurrences"))
    current_messages = _rows(fixture.bank_path, "messages")
    old_repaired_id = next(
        row[0] for row in prior_messages
        if row[1] == old["session_id"]
        and str(json.loads(row[11]).get("server_id")) == old["messages"][0]["server_id"]
    )
    assert {
        row for row in prior_messages if row[0] != old_repaired_id
    } <= set(current_messages)
    assert any(row[6] == "Synthetic global-only event" for row in current_messages)
    assert incoming.read_bytes() == original_incoming
    assert fixture.history.read_bytes() == original_history
    if kind in {"quoted_card", "later_quoted_card"}:
        prior = next(row for row in prior_messages if row[0] == old_repaired_id)
        current = next(row for row in current_messages if row[0] == old_repaired_id)
        prior_proof, current_proof = (json.loads(row[11]) for row in (prior, current))
        assert current[:11] == prior[:11]
        assert current_proof["macos_recovery"] == prior_proof["macos_recovery"]
        assert current_proof["quoted_reference"] == new["messages"][0]["native_text"]["quoted_reference"]
        origin = current_proof["quoted_app_card_metadata_origin"]
        for field in ("source_id", "snapshot_id", "main_sha256", "wal_sha256"):
            assert origin[field] == new["source"][field]
        replay = fixtures._publish(fixture, packets=[old], name="legacy-quoted-card-replay")
        assert replay["added"] == replay["reparsed"] == replay["conflicts"] == 0
        assert _rows(fixture.bank_path, "messages") == current_messages
    with closing(sqlite3.connect(fixture.bank_path)) as connection, connection:
        bank._validate_bank(connection, fixture.history)
        assert dict(connection.execute("SELECT key,value FROM archive_info"))["code_version"] == bank.WRITER_VERSION


@pytest.mark.parametrize("kind", ("quote", "later_card", "quoted_card", "later_quoted_card"))
def test_merging_an_older_sealed_projection_does_not_roll_back_a_newer_repair(bank_archive, kind):
    fixture = bank_archive
    old, new = _packets(fixture, kind)
    fixtures._publish(fixture, packets=[old], name="before-current-repair")
    incoming, _receipt = fixtures._stage(fixture, packets=[old], name="older-sealed-global")
    fixtures._publish(fixture, packets=[new], name="newer-user-repair")
    if kind == "later_card":
        with closing(sqlite3.connect(fixture.bank_path)) as connection, connection:
            connection.execute(
                "UPDATE archive_info SET value='v1.5.0-codex.0' WHERE key='code_version'"
            )
    prior_messages = _rows(fixture.bank_path, "messages")
    prior_revisions = _rows(fixture.bank_path, "projection_revisions")
    candidate = fixture.history.with_name("older-rebased.sqlite3")

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        before = runner.bank_summary(fixture.bank_path)
        receipt = merge.stage_merged_mac_recovery_bank(
            fixture.root, incoming, candidate, account_ids=(fixtures.OWNER,),
            publication_guard=guard,
        )
        runner.verify_preservation(fixture.bank_path, candidate, before, receipt)

    assert receipt["added"] == receipt["reparsed"] == receipt["conflicts"] == 0
    assert _rows(candidate, "messages") == prior_messages
    assert _rows(candidate, "projection_revisions") == prior_revisions


def test_a_different_snapshot_semantic_fork_is_rejected_without_changing_current(bank_archive):
    fixture = bank_archive
    old, incoming_packet = repairs._packets(fixture, "forward")
    user_packet = deepcopy(incoming_packet)
    user_native = user_packet["messages"][0]["native_text"]
    group = user_native["text_components"][0]
    group["text_components"][0]["text"] = "Another retained source interpretation"
    group["text"] = "\n\n".join(
        item["text"] for item in group["text_components"] if item["text"].strip()
    )
    user_native["text"] = native_components_text(user_native["text_components"])
    user_packet["messages"][0]["text"] = user_native["text"]
    incoming_packet["source"].update(
        snapshot_id="independent-global-capture", main_sha256="c" * 64
    )
    fixtures._publish(fixture, packets=[old], name="shared-forward-base")
    incoming, _receipt = fixtures._stage(
        fixture, packets=[incoming_packet], name="sealed-forward-fork"
    )
    fixtures._publish(fixture, packets=[user_packet], name="newer-forward-interpretation")
    original = fixture.bank_path.read_bytes()

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        with pytest.raises(merge.BankMergeError, match="sealed_merge_replay_conflicted"):
            merge.stage_merged_mac_recovery_bank(
                fixture.root, incoming, fixture.history.with_name("forked-merge.sqlite3"),
                account_ids=(fixtures.OWNER,), publication_guard=guard,
            )

    assert fixture.bank_path.read_bytes() == original


@pytest.mark.parametrize("defect", ("history_drift", "unsealed_incoming", "wrong_owner"))
def test_merge_refuses_inputs_outside_the_sealed_proof_boundary(bank_archive, defect):
    fixture = bank_archive
    old, new = repairs._packets(fixture, "quote")
    fixtures._publish(fixture, packets=[old], name="proof-boundary-base")
    incoming, _receipt = fixtures._stage(fixture, packets=[new], name="proof-boundary-input")
    owner = fixtures.OWNER
    if defect == "history_drift":
        fixture.history.touch()
    elif defect == "unsealed_incoming":
        with closing(sqlite3.connect(incoming)) as connection, connection:
            connection.execute("UPDATE messages SET text='Unsealed synthetic edit'")
    else:
        owner = "wxid_outside_selected_merge"
    original = fixture.bank_path.read_bytes()

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        with pytest.raises(ValueError):
            merge.stage_merged_mac_recovery_bank(
                fixture.root, incoming, fixture.history.with_name("rejected-merge.sqlite3"),
                account_ids=(owner,), publication_guard=guard,
            )

    assert fixture.bank_path.read_bytes() == original


def _insert_gap_and_reseal(path, session, *, reason="native_or_projection_conflict"):
    with closing(sqlite3.connect(path)) as connection, connection:
        source = connection.execute("SELECT id FROM sources ORDER BY id LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT INTO gaps VALUES(?,?,?,?,?,?,?)",
            ("a" * 64, session, source, 9_999, "9999", reason, "b" * 64),
        )
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='payload_sha256'",
            (bank._rows_digest(connection),),
        )


@pytest.mark.parametrize("replay", [True, False])
def test_missing_gap_metadata_is_unioned_and_resealed(bank_archive, monkeypatch, replay):
    fixture = bank_archive
    fixtures._publish(fixture, packets=[fixture.group], name="gap-union-base")
    incoming, _receipt = fixtures._stage(fixture, packets=[fixture.group], name="gap-union-input")
    _insert_gap_and_reseal(incoming, fixture.group_session)
    candidate = fixture.history.with_name("gap-union-result.sqlite3")
    if not replay:
        monkeypatch.setattr(merge, "_replay_packets", lambda *_args: (packet for packet in ()))

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        before = runner.bank_summary(fixture.bank_path)
        receipt = merge.stage_merged_mac_recovery_bank(
            fixture.root, incoming, candidate, account_ids=(fixtures.OWNER,),
            publication_guard=guard,
        )
        runner.verify_preservation(fixture.bank_path, candidate, before, receipt)

    assert receipt["merge"]["metadata_rows_added"]["gaps"] == 1
    assert owner_only_problem(candidate) == ""
    assert _rows(candidate, "gaps") == _rows(incoming, "gaps")
    with closing(sqlite3.connect(candidate)) as connection:
        bank._validate_bank(connection, fixture.history)


def test_unequal_append_only_metadata_collision_aborts_the_merge(bank_archive):
    fixture = bank_archive
    fixtures._publish(fixture, packets=[fixture.group], name="gap-collision-base")
    _insert_gap_and_reseal(fixture.bank_path, fixture.group_session)
    incoming, _receipt = fixtures._stage(
        fixture, packets=[fixture.group], name="gap-collision-input"
    )
    with closing(sqlite3.connect(incoming)) as connection, connection:
        connection.execute("UPDATE gaps SET reason='another-sealed-gap-observation'")
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='payload_sha256'",
            (bank._rows_digest(connection),),
        )
    original = fixture.bank_path.read_bytes()

    with bank.mac_recovery_publication_guard(fixture.root) as guard:
        with pytest.raises(merge.BankMergeError, match="merge_metadata_collision_gaps"):
            merge.stage_merged_mac_recovery_bank(
                fixture.root, incoming, fixture.history.with_name("collision-result.sqlite3"),
                account_ids=(fixtures.OWNER,), publication_guard=guard,
            )

    assert fixture.bank_path.read_bytes() == original


def test_replay_chunks_preserve_utf8_bytes_order_and_both_budgets():
    packet = {"source": "synthetic bounded metadata"}
    messages = [{"text": "é" * 12, "ordinal": index} for index in range(5)]
    byte_limit = len(bank._json({**packet, "messages": messages[:2]}).encode())

    chunks = list(merge._packet_chunks(packet, iter(messages), max_messages=2, max_bytes=byte_limit))

    assert [len(chunk["messages"]) for chunk in chunks] == [2, 2, 1]
    assert [message for chunk in chunks for message in chunk["messages"]] == messages
    assert all(len(bank._json(chunk).encode()) <= byte_limit for chunk in chunks)


def test_one_oversized_observation_is_not_partially_replayed():
    with pytest.raises(merge.BankMergeError, match="one_merge_observation_exceeds_packet_budget"):
        list(merge._packet_chunks({"source": "synthetic"}, [{"text": "x" * 100}], max_bytes=64))


def test_occurrence_replay_uses_index_order_without_sorting_proof_bodies(bank_archive):
    packet = {key: value for key, value in bank_archive.group.items() if key != "messages"}
    source_key = bank._sha(packet)
    observed = []

    class RecordedConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            if query.startswith("SELECT proof FROM occurrences"):
                observed.append((query, parameters))
            return super().execute(query, parameters)

    connection = RecordedConnection(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(bank._SCHEMA)
    connection.execute(
        "INSERT INTO sources VALUES(?,?,?,?,?,?)",
        (source_key, packet["account_id"], packet["peer_id"], packet["source"]["source_id"],
         packet["source"]["snapshot_id"], bank._json(packet)),
    )
    message = bank_archive.group["messages"][0]
    connection.execute(
        "INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("synthetic-locator", source_key, packet["session_id"], message["local_id"],
         message["server_id"], message["content_sha256"], "synthetic-event", "bank",
         "synthetic-signature", bank._json(message)),
    )
    counters = {
        "replayed_occurrences": 0, "replayed_packets": 0,
        "synthesized_latest_projections": 0,
    }
    try:
        chunks = list(merge._replay_packets(connection, (fixtures.OWNER,), lambda: None, counters))
        assert chunks[0]["messages"] == [message]
        assert len(observed) == 1
        query, parameters = observed[0]
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query, parameters)]
        assert any("SEARCH occurrences USING INDEX" in row for row in plan)
        assert not any("TEMP B-TREE" in row for row in plan)
    finally:
        connection.close()
