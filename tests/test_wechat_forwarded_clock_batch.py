"""Complete packet audits publish only source-bound, conflict-free clock pointers.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from app.core import wechat_forwarded_clock_batch as batch
from app.core import wechat_forwarded_clocks as clocks
from app.core.wechat_archive import _identifier
from app.core.wechat_text import native_components_text
from tests import test_wechat_forwarded_clocks as fixtures


native_html_store = fixtures.native_html_store
statement_store = fixtures.statement_store
forwarded_origin_store = fixtures.forwarded_origin_store
pinned_clock_store = fixtures.pinned_clock_store


@pytest.fixture
def batch_store(pinned_clock_store):
    fixture = pinned_clock_store
    fixture.inventory = fixture.store.parent / "clock-inventory.sqlite3"
    fixture.assessment = fixture.store.parent / "clock-assessment.sqlite3"
    fixture.catalog = fixture.history.parent / "forwarded_clock_links.sqlite3"
    return fixture


def _audit(fixture):
    census = batch.inventory(fixture.store, fixture.inventory)
    assessment = batch.assess_inventory(fixture.store, fixture.inventory, fixture.assessment)
    return census, assessment


def _assessment_rows(fixture):
    with sqlite3.connect(fixture.assessment) as connection:
        connection.row_factory = sqlite3.Row
        return {row["target_message_id"]: dict(row) for row in connection.execute("SELECT * FROM assessments")}


def _clone_native(fixture, name, change):
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute("SELECT * FROM messages WHERE id=?", (fixture.native_parent,)).fetchone())
        proof = json.loads(row["provenance"])
        proof["record_id"] = "independent-native-clock-copy-" + name
        proof["server_id"] = str(int(proof["server_id"]) + int(hashlib.sha256(name.encode()).hexdigest()[:8], 16))
        change(proof)
        text = native_components_text(proof["text_components"])
        row.update(id=_identifier(fixture.native_source, proof["record_id"]), text=text, search_text=text.casefold(), provenance=json.dumps(proof))
        columns = ",".join(row)
        connection.execute("INSERT INTO messages(" + columns + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
    return row["id"]


def _source_bytes(fixture):
    return fixture.history.read_bytes(), fixture.registry.read_bytes(), fixture.clock_registry.read_bytes()


def test_complete_census_assessment_publication_and_runtime_readback_preserve_sources(batch_store):
    fixture = batch_store
    before = _source_bytes(fixture)
    census, assessment = _audit(fixture)
    assert census["html_packets"] == census["ipad_packets"] == 1
    assert census["html_packets_with_unknown"] == 1
    assert census["html_unknown_children"] == 9
    assert assessment["assessed_html_packets"] == assessment["recovered_packets"] == 1
    assert assessment["unavailable_html_children"] == assessment["recovered_html_children"] == 9
    assert assessment["remaining_unavailable_html_children"] == 0
    with sqlite3.connect(fixture.inventory) as connection:
        assert set(connection.execute("SELECT id,source_kind FROM packets")) == {
            (fixture.html_parent, "html"), (fixture.native_parent, "ipad")
        }
        assert connection.execute("SELECT resources FROM packets WHERE id=?", (fixture.native_parent,)).fetchone()[0] == 1
        assert not {"text", "body", "provenance"} & {row[1] for row in connection.execute("PRAGMA table_info(packets)")}
    published = batch.publish_assessment(fixture.store, fixture.assessment)
    assert published["verified_targets"] == 1
    assert published["verified_recovered_children"] == 9
    assert published["catalog_sha256"] == hashlib.sha256(fixture.catalog.read_bytes()).hexdigest()
    receipt = hashlib.sha256(fixture.assessment.read_bytes()).hexdigest()
    assert published["receipt_sha256"] == receipt
    with sqlite3.connect(fixture.catalog) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        info = dict(connection.execute("SELECT key,value FROM info"))
        assert info["candidate_agreement_receipt_sha256"] == receipt
        assert json.loads(info["source_identity"]) == list(batch._identity(fixture.history))
    result = clocks.WechatForwardedClocks(fixture.store).project(fixtures._rows(fixture))[fixture.html_parent]
    assert len(result) == 9
    assert result[(1,)]["timestamp_value"] == "2020-08-12T13:46:00"
    assert result[(1,)]["proof"]["target_source_clock"] == "1970-01-01 08:00:00"
    assert before == _source_bytes(fixture)


@pytest.mark.parametrize("raw", ["", "Unknown original time", "not a source clock", "2021-05-03"])
def test_unavailable_census_uses_canonical_parser_for_every_nonempty_or_missing_clock(batch_store, raw):
    fixture = batch_store

    def clocks_missing(proof):
        for leaf in proof["forwarded_components"]:
            leaf["raw_timestamp"] = raw

    fixtures._mutate(fixture, native=False, callback=clocks_missing)
    before = _source_bytes(fixture)
    census, assessment = _audit(fixture)
    assert census["html_unknown_children"] == assessment["unavailable_html_children"] == 9
    assert _assessment_rows(fixture)[fixture.html_parent]["unavailable_children"] == 9
    assert assessment["recovered_html_children"] == (9 if raw == "" else 0)
    assert before == _source_bytes(fixture)


@pytest.mark.parametrize("partial", [False, True])
def test_all_validated_native_copies_block_contradictory_clocks_including_partial_copies(batch_store, partial):
    fixture = batch_store

    def contradictory(proof):
        texts = [part for part in proof["text_components"] if part["kind"] == "forwarded_text"]
        stamp = datetime.fromisoformat(texts[0]["source_timestamp"]) + timedelta(seconds=1)
        texts[0]["source_timestamp"] = stamp.isoformat(" ")
        if partial:
            texts[1]["source_timestamp"] = "1970-01-01 08:00:00"

    clone = _clone_native(fixture, "conflicting", contradictory)
    before = _source_bytes(fixture)
    census, assessment = _audit(fixture)
    assert census["ipad_packets"] == 2
    assert assessment["native_clock_conflict_packets"] == 1
    assert assessment["recovered_html_children"] == 0
    assert assessment["remaining_unavailable_html_children"] == 9
    row = _assessment_rows(fixture)[fixture.html_parent]
    assert row["status"] == "native_clock_conflict"
    assert row["native_candidates"] == row["valid_candidates"] == 2
    assert row["eligible_candidates"] == (1 if partial else 2)
    with sqlite3.connect(fixture.assessment) as connection:
        assert connection.execute("SELECT count(*) FROM links").fetchone()[0] == 0
        vector = json.loads(connection.execute("SELECT vector_json FROM native_validation WHERE native_message_id=?", (clone,)).fetchone()[0])
    if partial:
        assert vector[0][3] is not None and vector[1][3] is None
    published = batch.publish_assessment(fixture.store, fixture.assessment)
    assert published["verified_targets"] == published["verified_recovered_children"] == 0
    assert clocks.WechatForwardedClocks(fixture.store).project(fixtures._rows(fixture)) == {}
    assert before == _source_bytes(fixture)


def test_naive_copy_cannot_hide_conflicting_explicit_instants(batch_store):
    fixture = batch_store
    for name, hours in (("utc", 0), ("east-eight", 8)):
        def explicit(proof, hours=hours):
            first = next(part for part in proof["text_components"] if part["kind"] == "forwarded_text")
            first["source_timestamp"] = datetime.fromisoformat(first["source_timestamp"]).replace(tzinfo=timezone(timedelta(hours=hours))).isoformat()
        _clone_native(fixture, name, explicit)
    _census, assessment = _audit(fixture)
    assert assessment["native_clock_conflict_packets"] == 1
    assert _assessment_rows(fixture)[fixture.html_parent]["valid_candidates"] == 3
    assert assessment["recovered_html_children"] == 0


def test_agreeing_native_copies_prefer_explicit_epoch_without_inventing_an_html_timezone(batch_store):
    fixture = batch_store

    def known_epoch(proof):
        for part in proof["text_components"]:
            if part["kind"] == "forwarded_text":
                stamp = datetime.fromisoformat(part["source_timestamp"]).replace(tzinfo=timezone(timedelta(hours=8)))
                part["source_create_time"] = str(int(stamp.timestamp()))

    explicit = _clone_native(fixture, "explicit-epoch", known_epoch)
    before = _source_bytes(fixture)
    _census, assessment = _audit(fixture)
    assert assessment["recovered_packets"] == 1
    row = _assessment_rows(fixture)[fixture.html_parent]
    assert row["valid_candidates"] == row["eligible_candidates"] == 2
    with sqlite3.connect(fixture.assessment) as connection:
        assert connection.execute("SELECT native_message_id FROM links").fetchone()[0] == explicit
    batch.publish_assessment(fixture.store, fixture.assessment)
    projected = clocks.WechatForwardedClocks(fixture.store).project(fixtures._rows(fixture))[fixture.html_parent]
    assert projected[(1,)]["timestamp_value"] == "2020-08-12T13:46:00+08:00"
    assert projected[(1,)]["proof"]["target_source_clock"] == "1970-01-01 08:00:00"
    assert before == _source_bytes(fixture)


def test_unmatched_html_packet_remains_in_audit_and_keeps_its_original_unknown_clock(batch_store):
    fixture = batch_store
    second = fixtures._add_html_target(fixture)
    proof = json.loads(second["provenance"])
    proof["forwarded_components"][0]["export_msgid"] = "414_" + "f" * 32
    with sqlite3.connect(fixture.history) as connection:
        connection.execute("UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), second["id"]))
    second["provenance"] = json.dumps(proof)
    before = _source_bytes(fixture)
    census, assessment = _audit(fixture)
    assert census["html_packets"] == assessment["assessed_html_packets"] == 2
    assert assessment["unavailable_html_children"] == 18
    assert assessment["recovered_html_children"] == assessment["remaining_unavailable_html_children"] == 9
    rows = _assessment_rows(fixture)
    assert rows[fixture.html_parent]["status"] == "recovered"
    assert rows[second["id"]]["status"] == "unconfirmed"
    batch.publish_assessment(fixture.store, fixture.assessment)
    result = clocks.WechatForwardedClocks(fixture.store).project([*fixtures._rows(fixture), second])
    assert set(result) == {fixture.html_parent}
    assert before == _source_bytes(fixture)


@pytest.mark.parametrize("fault", ["missing_summary", "wrong_count", "missing_identity", "deleted_native", "source_changed"])
def test_incomplete_or_stale_inventory_cannot_create_a_complete_assessment(batch_store, fault):
    fixture = batch_store
    batch.inventory(fixture.store, fixture.inventory)
    with sqlite3.connect(fixture.inventory) as connection:
        if fault == "missing_summary":
            connection.execute("DELETE FROM info WHERE key='summary'")
        elif fault == "wrong_count":
            connection.execute("UPDATE info SET value=json_set(value,'$.ipad_packets',99) WHERE key='summary'")
        elif fault == "missing_identity":
            connection.execute("DELETE FROM info WHERE key='source_identity'")
        elif fault == "deleted_native":
            connection.execute("DELETE FROM packets WHERE source_kind='ipad'")
    if fault == "source_changed":
        with sqlite3.connect(fixture.history) as connection:
            connection.execute("UPDATE messages SET sender_label=sender_label || ' changed' WHERE id=?", (fixture.native_parent,))
    before = _source_bytes(fixture)
    with pytest.raises(ValueError, match="Inventory"):
        batch.assess_inventory(fixture.store, fixture.inventory, fixture.assessment)
    assert not fixture.catalog.exists()
    assert before == _source_bytes(fixture)


@pytest.mark.parametrize("fault", ["missing_state", "wrong_summary", "missing_assessment", "missing_link", "unconfirmed_link", "orphan_target", "wrong_target_count", "source_changed"])
def test_incomplete_or_stale_assessment_preserves_existing_runtime_authority(batch_store, fault):
    fixture = batch_store
    _audit(fixture)
    fixtures._indexed_links(fixture, fixtures._links(fixture, [fixture.html_parent]))
    catalog_before = fixture.catalog.read_bytes()
    with sqlite3.connect(fixture.assessment) as connection:
        if fault == "missing_state":
            connection.execute("DELETE FROM info WHERE key='state'")
        elif fault == "wrong_summary":
            connection.execute("UPDATE info SET value=json_set(value,'$.recovered_html_children',10) WHERE key='assessment_summary'")
        elif fault == "missing_assessment":
            connection.execute("DELETE FROM assessments")
        elif fault == "missing_link":
            connection.execute("DELETE FROM links")
        elif fault == "unconfirmed_link":
            connection.execute("UPDATE assessments SET status='unconfirmed'")
        elif fault == "orphan_target":
            connection.execute("UPDATE links SET target_message_id=?", ("f" * 64,))
        elif fault == "wrong_target_count":
            connection.execute("UPDATE assessments SET recovered_children=8")
            connection.execute("UPDATE info SET value=json_set(value,'$.recovered_html_children',8) WHERE key='assessment_summary'")
    if fault == "source_changed":
        with sqlite3.connect(fixture.history) as connection:
            connection.execute("UPDATE messages SET sender_label=sender_label || ' changed' WHERE id=?", (fixture.native_parent,))
    before = _source_bytes(fixture)
    with pytest.raises(ValueError, match="[Aa]ssessment"):
        batch.publish_assessment(fixture.store, fixture.assessment)
    assert fixture.catalog.read_bytes() == catalog_before
    assert before == _source_bytes(fixture)


def test_changed_signature_cannot_remove_a_conflicting_copy_from_the_complete_census(batch_store):
    fixture = batch_store

    def contradictory(proof):
        first = next(part for part in proof["text_components"] if part["kind"] == "forwarded_text")
        first["source_timestamp"] = (datetime.fromisoformat(first["source_timestamp"]) + timedelta(seconds=1)).isoformat(" ")

    clone = _clone_native(fixture, "census-conflict", contradictory)
    batch.inventory(fixture.store, fixture.inventory)
    with sqlite3.connect(fixture.inventory) as connection:
        connection.execute("UPDATE packets SET signature='' WHERE id=?", (clone,))
    with pytest.raises(ValueError, match="[Ii]nventory|[Cc]ensus|[Pp]acket"):
        batch.assess_inventory(fixture.store, fixture.inventory, fixture.assessment)
    assert not fixture.catalog.exists()


def test_bad_assessment_pin_is_rejected_before_replacing_a_good_catalog(batch_store):
    fixture = batch_store
    _audit(fixture)
    fixtures._indexed_links(fixture, fixtures._links(fixture, [fixture.html_parent]))
    before = fixture.catalog.read_bytes()
    with sqlite3.connect(fixture.assessment) as connection:
        connection.execute("UPDATE links SET native_text_sha256=?", ("0" * 64,))
    with pytest.raises(ValueError):
        batch.publish_assessment(fixture.store, fixture.assessment)
    assert fixture.catalog.read_bytes() == before


def test_cli_audit_requires_explicit_apply_to_publish_pointers(batch_store, monkeypatch, capsys):
    fixture = batch_store
    path = Path(__file__).resolve().parents[1] / "scripts/recover_wechat_forwarded_clocks.py"
    spec = importlib.util.spec_from_file_location("recover_wechat_forwarded_clocks_test", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    arguments = [str(path), "--local-store", str(fixture.store), "--inventory", str(fixture.inventory), "--assessment", str(fixture.assessment)]
    before = _source_bytes(fixture)
    monkeypatch.setattr(sys, "argv", arguments)
    script.main()
    assert fixture.assessment.exists() and not fixture.catalog.exists()
    assert "Use --apply" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", [*arguments, "--apply"])
    script.main()
    output = capsys.readouterr().out
    assert json.loads(output.splitlines()[-1])["verified_recovered_children"] == 9
    assert fixture.catalog.exists()
    assert before == _source_bytes(fixture)


def test_byte_identical_catalog_readback_recovers_from_one_transient_empty_projection(batch_store, monkeypatch):
    fixture = batch_store
    _audit(fixture)
    before = _source_bytes(fixture)
    original, attempts, sleeps = clocks.WechatForwardedClocks.project, [], []

    def temporarily_unavailable(projector, rows):
        attempts.append(rows[0]["id"])
        return {} if len(attempts) == 1 else original(projector, rows)

    monkeypatch.setattr(clocks.WechatForwardedClocks, "project", temporarily_unavailable)
    monkeypatch.setattr(batch.time, "sleep", sleeps.append)
    published = batch.publish_assessment(fixture.store, fixture.assessment)
    assert attempts == [fixture.html_parent, fixture.html_parent]
    assert sleeps == [0.1]
    assert published["verified_recovered_children"] == 9
    assert published["catalog_sha256"] == hashlib.sha256(fixture.catalog.read_bytes()).hexdigest()
    assert before == _source_bytes(fixture)


def _change_publication_evidence(fixture, changed):
    if changed == "catalog":
        with sqlite3.connect(fixture.catalog) as connection:
            connection.execute("INSERT INTO info VALUES('changed_during_readback','true')")
    else:
        with sqlite3.connect(fixture.history) as connection:
            connection.execute("UPDATE messages SET sender_label=sender_label || ' changed during readback' WHERE id=?", (fixture.native_parent,))


@pytest.mark.parametrize("changed", ["catalog", "archive"])
def test_readback_never_retries_after_publication_evidence_changes(batch_store, monkeypatch, changed):
    fixture = batch_store
    _audit(fixture)
    attempts, sleeps = [], []

    def changed_while_unavailable(_projector, rows):
        attempts.append(rows[0]["id"])
        _change_publication_evidence(fixture, changed)
        return {}

    monkeypatch.setattr(clocks.WechatForwardedClocks, "project", changed_while_unavailable)
    monkeypatch.setattr(batch.time, "sleep", sleeps.append)
    with pytest.raises(ValueError, match="changed"):
        batch.publish_assessment(fixture.store, fixture.assessment)
    assert attempts == [fixture.html_parent]
    assert sleeps == []


@pytest.mark.parametrize("changed", ["catalog", "archive"])
def test_retry_delay_cannot_hide_a_changed_catalog_or_archive(batch_store, monkeypatch, changed):
    fixture = batch_store
    _audit(fixture)
    original, attempts, sleeps = clocks.WechatForwardedClocks.project, [], []

    def temporarily_unavailable(projector, rows):
        attempts.append(rows[0]["id"])
        return {} if len(attempts) == 1 else original(projector, rows)

    def changed_during_delay(delay):
        sleeps.append(delay)
        _change_publication_evidence(fixture, changed)

    monkeypatch.setattr(clocks.WechatForwardedClocks, "project", temporarily_unavailable)
    monkeypatch.setattr(batch.time, "sleep", changed_during_delay)
    with pytest.raises(ValueError, match="changed|differs"):
        batch.publish_assessment(fixture.store, fixture.assessment)
    assert sleeps == [0.1]
    assert len(attempts) <= 2


def test_stable_but_unavailable_readback_stops_after_three_attempts(batch_store, monkeypatch):
    fixture = batch_store
    _audit(fixture)
    before = _source_bytes(fixture)
    attempts, sleeps = [], []

    def unavailable(_projector, rows):
        attempts.append(rows[0]["id"])
        return {}

    monkeypatch.setattr(clocks.WechatForwardedClocks, "project", unavailable)
    monkeypatch.setattr(batch.time, "sleep", sleeps.append)
    with pytest.raises(ValueError, match="did not read back"):
        batch.publish_assessment(fixture.store, fixture.assessment)
    assert attempts == [fixture.html_parent] * 3
    assert sleeps == [0.1, 0.1]
    assert before == _source_bytes(fixture)


def test_successful_projection_cannot_mask_a_catalog_change_before_publication_returns(batch_store, monkeypatch):
    fixture = batch_store
    _audit(fixture)
    original = clocks.WechatForwardedClocks.project

    def changed_after_successful_projection(projector, rows):
        result = original(projector, rows)
        assert result
        _change_publication_evidence(fixture, "catalog")
        return result

    monkeypatch.setattr(clocks.WechatForwardedClocks, "project", changed_after_successful_projection)
    with pytest.raises(ValueError, match="changed|differs"):
        batch.publish_assessment(fixture.store, fixture.assessment)
