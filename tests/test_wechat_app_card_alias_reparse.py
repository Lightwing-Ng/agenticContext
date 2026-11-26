"""Card enrichment across archive aliases retains exact native event authority.

Code version: v1.1.2-codex.0
"""

from contextlib import closing
import json
import sqlite3

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_macos_reparse as repair
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_macos_reparse as cards


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


_KINDS = ("app", "quoted", "nested_quoted")


def _alias_packets(fixture, kind="app"):
    if kind == "app":
        old, incoming = cards._app_card_packets(fixture)
    else:
        old, incoming = cards._quoted_card_packets(fixture, nested=kind == "nested_quoted")
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        original = dict(connection.execute(
            "SELECT * FROM conversations WHERE id=?", (old["session_id"],)
        ).fetchone())
        partition = dict(connection.execute(
            "SELECT * FROM partitions WHERE id=?", (original["partition_id"],)
        ).fetchone())
        partition["id"] += "-independent-archive"
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)", tuple(partition.values())
        )
        original.update(
            id="wechat:" + fixtures._hash("independent-card-conversation"),
            partition_id=partition["id"],
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(original.values()),
        )
        incoming["session_id"] = original["id"]
        for anchor in incoming["anchors"]:
            row = dict(connection.execute(
                "SELECT * FROM messages WHERE id=?", (anchor["archive_message_id"],)
            ).fetchone())
            row.update(
                id=fixtures._hash("alias-anchor-" + row["id"]),
                conversation_id=incoming["session_id"],
            )
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row.values()),
            )
            anchor["archive_message_id"] = row["id"]
        for packet in (old, incoming):
            bank._validate_packet(packet, (fixtures.OWNER,))
            bank._binding(connection, packet)
    return old, incoming


def _rows(path, table):
    with closing(sqlite3.connect(path)) as connection, connection:
        return connection.execute("SELECT * FROM " + table + " ORDER BY 1,2").fetchall()


@pytest.mark.parametrize("kind", _KINDS)
def test_alias_card_enrichment_preserves_receipt_and_journals_idempotent_replays(bank_archive, kind):
    fixture = bank_archive
    old, incoming = _alias_packets(fixture, kind)
    fixtures._publish(fixture, packets=[old], name="canonical-card-baseline")
    before = _rows(fixture.bank_path, "messages")[0]
    occurrences = _rows(fixture.bank_path, "occurrences")
    sources = _rows(fixture.bank_path, "sources")
    original_bank = fixture.bank_path.read_bytes()
    with closing(sqlite3.connect(fixture.bank_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        canonical = dict(connection.execute("SELECT * FROM messages").fetchone())
    fresh = dict(zip(canonical, bank._row(incoming, incoming["messages"][0]), strict=True))
    assert canonical["conversation_id"] != fresh["conversation_id"]
    assert repair._identity(canonical) == repair._identity(fresh)

    candidate, receipt = fixtures._stage(fixture, packets=[incoming], name="alias-card-enriched")

    assert receipt["reparsed"] == 1
    assert receipt["added"] == receipt["upgraded"] == receipt["conflicts"] == 0
    assert fixture.bank_path.read_bytes() == original_bank
    current = _rows(candidate, "messages")[0]
    assert current[:11] == before[:11]
    proof, original_proof = json.loads(current[11]), json.loads(before[11])
    assert proof["macos_recovery"] == original_proof["macos_recovery"]
    expected_reason = "app_card_metadata_completion" if kind == "app" else "quoted_card_metadata_completion"
    origin = proof[repair.METADATA_ORIGIN_FIELDS[expected_reason]]
    assert set(origin) == set(repair._METADATA_SOURCE_FIELDS)
    fresh_native = json.loads(fresh["provenance"])["macos_recovery"]
    assert origin == {field:fresh_native[field] for field in repair._METADATA_SOURCE_FIELDS}
    assert _rows(candidate, "occurrences") == occurrences
    assert set(sources) <= set(_rows(candidate, "sources"))
    with closing(sqlite3.connect(candidate)) as connection, connection:
        journal = connection.execute(
            "SELECT reason,prior_row,source_key FROM projection_revisions"
        ).fetchone()
        assert journal[0] == expected_reason
        assert json.loads(journal[1])["provenance"] == before[11]
        envelope = json.loads(connection.execute(
            "SELECT proof FROM sources WHERE id=?", (journal[2],)
        ).fetchone()[0])
        assert envelope["session_id"] == incoming["session_id"]
        assert envelope["account_id"] == old["account_id"]
        assert envelope["peer_id"] == old["peer_id"]
        repair.validate(connection)
        bank._validate_bank(connection, fixture.history)
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    for index, packet in enumerate((incoming, old, incoming, old)):
        replay = fixtures._publish(fixture, packets=[packet], name=f"alias-card-replay-{index}")
        assert replay["added"] == replay["upgraded"] == replay["reparsed"] == 0
        assert _rows(fixture.bank_path, "messages")[0] == current
        assert _rows(fixture.bank_path, "occurrences") == occurrences
        assert len(_rows(fixture.bank_path, "projection_revisions")) == 1


@pytest.mark.parametrize("field", repair._RAW_FIELDS + ("sender_id", "sender_label", "direction"))
@pytest.mark.parametrize("kind", _KINDS)
def test_alias_enrichment_cannot_hide_native_or_sender_drift(bank_archive, field, kind):
    fixture = bank_archive
    old, incoming = _alias_packets(fixture, kind)
    fixtures._publish(fixture, packets=[old], name="pinned-card-baseline")
    with closing(sqlite3.connect(fixture.bank_path)) as source, closing(sqlite3.connect(":memory:")) as memory:
        source.backup(memory)
        memory.row_factory = sqlite3.Row
        canonical = dict(memory.execute("SELECT * FROM messages").fetchone())
        fresh = dict(zip(canonical, bank._row(incoming, incoming["messages"][0]), strict=True))
        proof = json.loads(fresh["provenance"])
        if field in repair._RAW_FIELDS:
            native = proof["macos_recovery"]
            native[field] = native[field] + 1 if type(native[field]) is int else native[field] + "-changed"
            if field == "server_id":
                proof["server_id"] = native[field]
        else:
            fresh[field] = "sent" if field == "direction" else fresh[field] + "-changed"
        fresh["provenance"] = bank._json(proof)
        # Both original rows remain self-consistent; the exact event gate must reject drift.
        repair._identity(fresh)
        projected = [fresh[key] for key in canonical]
        before = memory.execute("SELECT * FROM messages").fetchall()
        with pytest.raises(ValueError, match="exact native event"):
            repair.apply(memory, canonical["id"], projected, "unaccepted-source")
        assert memory.execute("SELECT * FROM messages").fetchall() == before
        assert not repair._has_table(memory)


@pytest.mark.parametrize("defect", ("missing_session", "wrong_session", "root_server"))
@pytest.mark.parametrize("kind", _KINDS)
def test_alias_enrichment_validates_incoming_coordinates_before_retaining_columns(bank_archive, defect, kind):
    fixture = bank_archive
    old, incoming = _alias_packets(fixture, kind)
    fixtures._publish(fixture, packets=[old], name="coherent-card-baseline")
    with closing(sqlite3.connect(fixture.bank_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        canonical = dict(connection.execute("SELECT * FROM messages").fetchone())
    fresh = dict(zip(canonical, bank._row(incoming, incoming["messages"][0]), strict=True))
    proof = json.loads(fresh["provenance"])
    if defect == "missing_session":
        proof["macos_recovery"].pop("session_id")
    elif defect == "wrong_session":
        proof["macos_recovery"]["session_id"] = canonical["conversation_id"]
    else:
        proof["server_id"] = "different-native-event"
    fresh["provenance"] = bank._json(proof)
    with pytest.raises(ValueError, match="coordinates cross"):
        repair._row_for_candidate(canonical, [fresh[key] for key in canonical])
