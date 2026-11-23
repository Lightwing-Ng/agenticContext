"""Fresh semantic bank revalidation preserves rows without historical-byte claims.

Code version: v1.0.3-codex.0
"""

import json
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from copy import deepcopy
from datetime import datetime

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_macos_recovery_revalidation as revalidation
from app.core import wechat_media_event_index as event_index
from app.core.wechat_archive import _file_identity
from app.core.wechat_archive_relocation import receipt_identity
from app.core.wechat_media_event_index import MEDIA_EVENT_SCHEMA, VERSION
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_macos_recovery_bank_upgrades as upgrades

mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
app_stubs = upgrades.app_stubs


def _empty_media(history):
    media = history.with_name("media_events.sqlite3")
    with sqlite3.connect(media) as connection:
        connection.executescript(MEDIA_EVENT_SCHEMA)
        connection.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            {
                "schema_version": "1",
                "proof_version": VERSION,
                "published": "1",
                "primary_disjoint": "1",
                "history_file_identity": json.dumps(_file_identity(history)),
            }.items(),
        )
    return media


def _change_ctime(media):
    before = bank._identity(media)
    media.chmod(0o400)
    assert before["ctime_ns"] != bank._identity(media)["ctime_ns"]


@pytest.fixture
def stale_bank(bank_archive, monkeypatch):
    fixture = bank_archive
    fixture.media = _empty_media(fixture.history)
    fixtures._publish(fixture)
    _change_ctime(fixture.media)
    monkeypatch.setattr(revalidation, "_clonefile", shutil.copyfile)
    return fixture


def test_fresh_validation_does_not_create_files_or_claim_historical_byte_equivalence(
    stale_bank,
):
    fixture = stale_bank
    before = (
        fixture.history.read_bytes(),
        fixture.bank_path.read_bytes(),
        fixture.media.read_bytes(),
    )
    paths = set(fixture.history.parent.iterdir())
    report = revalidation.validate_mac_recovery_bank_revalidation(fixture.root)
    assert report["historical_byte_equivalence"] == "not_established"
    assert report["messages"] == 12
    assert report["current_media_events"] == 0
    assert report["existing_timeline_targets"] == 0
    assert report["account_ids"] == [fixtures.OWNER]
    assert not report["published"]
    assert set(fixture.history.parent.iterdir()) == paths
    assert (
        fixture.history.read_bytes(),
        fixture.bank_path.read_bytes(),
        fixture.media.read_bytes(),
    ) == before


def test_candidate_changes_only_input_pin_and_uses_normal_publisher_cas(stale_bank):
    fixture = stale_bank
    original = fixture.bank_path.read_bytes()
    history_bytes, media_bytes = (
        fixture.history.read_bytes(),
        fixture.media.read_bytes(),
    )
    candidate = fixture.history.with_name("semantic-candidate.sqlite3")
    preserved = fixture.history.with_name("preserved-sealed-bank.sqlite3")
    receipt = revalidation.stage_mac_recovery_bank_revalidation(
        fixture.root, candidate, preserved_path=preserved
    )
    assert fixture.bank_path.read_bytes() == preserved.read_bytes() == original
    assert stat.S_IMODE(preserved.stat().st_mode) == 0o400
    with (
        closing(sqlite3.connect(candidate)) as current,
        current,
        closing(sqlite3.connect(preserved)) as old,
        old,
    ):
        tables = [
            row[0]
            for row in old.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='archive_info' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            assert (
                current.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                == old.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            )
        old_info = dict(old.execute("SELECT * FROM archive_info"))
        new_info = dict(current.execute("SELECT * FROM archive_info"))
        assert {
            key: value for key, value in old_info.items() if key != "input_identities"
        } == {
            key: value for key, value in new_info.items() if key != "input_identities"
        }
        bank._validate_bank(current, fixture.history)
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    assert preserved.read_bytes() == original
    assert fixture.history.read_bytes() == history_bytes
    assert fixture.media.read_bytes() == media_bytes


@pytest.mark.parametrize("defect", ["history_identity", "payload", "no_drift"])
def test_unsupported_drift_or_bad_old_payload_fails_before_any_clone(
    stale_bank, monkeypatch, defect
):
    fixture = stale_bank
    if defect == "history_identity":
        fixture.history.touch()
    elif defect == "payload":
        with sqlite3.connect(fixture.bank_path) as connection:
            connection.execute("UPDATE messages SET text='unsealed drift'")
    else:
        with sqlite3.connect(fixture.bank_path) as connection:
            connection.execute(
                "UPDATE archive_info SET value=? WHERE key='input_identities'",
                (
                    bank._json(
                        {
                            key: value
                            for key, value in bank._inputs(fixture.history).items()
                            if key != fixture.bank_path.name
                        }
                    ),
                ),
            )
    monkeypatch.setattr(
        revalidation, "_clonefile", lambda *_: pytest.fail("Unverified bank was cloned")
    )
    with pytest.raises(ValueError):
        revalidation.stage_mac_recovery_bank_revalidation(
            fixture.root,
            fixture.history.with_name("denied.sqlite3"),
            preserved_path=fixture.history.with_name("not-preserved.sqlite3"),
        )


def test_exact_existing_shadow_stubs_remain_valid(app_stubs):
    fixture = app_stubs
    media = _empty_media(fixture.history)
    upgrades._publish(fixture)
    _change_ctime(media)
    report = revalidation.validate_mac_recovery_bank_revalidation(fixture.root)
    assert report["messages"] == report["validated_shadow_overlaps"] == 3


def _comparison(fixture):
    connection = revalidation._readonly(fixture.history)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "ATTACH DATABASE ? AS macos_recovery_bank",
        (fixture.bank_path.resolve().as_uri() + "?mode=ro&immutable=1",),
    )
    connection.execute(
        "CREATE TEMP TABLE test_conversations AS SELECT * FROM main.conversations"
    )
    connection.execute("CREATE TEMP TABLE test_messages AS SELECT * FROM main.messages")
    revalidation._retained_events(connection)
    return connection


@pytest.mark.parametrize("same_id", [True, False])
def test_new_base_overlap_is_rejected_even_when_the_message_is_identical(
    stale_bank, same_id
):
    fixture = stale_bank
    with closing(_comparison(fixture)) as connection:
        values = list(
            connection.execute(
                "SELECT * FROM macos_recovery_bank.messages LIMIT 1"
            ).fetchone()
        )
        if not same_id:
            values[0] = "new-base-alias"
        connection.execute(
            "INSERT INTO temp.test_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )
        with pytest.raises(ValueError, match="overlaps a retained bank"):
            revalidation._compare_base(
                connection, "temp.test_conversations", "temp.test_messages"
            )


@pytest.fixture
def deduplicated_bank(bank_archive):
    fixture = bank_archive
    media = _empty_media(fixture.history)
    packet = deepcopy(fixture.direct)
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM messages WHERE id=?",
            (packet["anchors"][0]["archive_message_id"],),
        ).fetchone()
    message = fixtures._message(100, 1, raw=row["text"], sender=fixtures.PEER)
    message.update(
        server_id=str(json.loads(row["provenance"])["server_id"]),
        create_time=int(datetime.fromisoformat(row["timestamp_value"]).timestamp()),
        real_sender_id=2,
        sender_proof=fixtures._proof(2, fixtures.PEER, is_session=1),
        native_text={
            "text": row["text"],
            "flags": [],
            "original_message_type": 1,
            "text_extraction_kind": "plain_text",
        },
    )
    packet["messages"].append(message)
    fixtures._publish(fixture, packets=[packet])
    _change_ctime(media)
    with sqlite3.connect(fixture.bank_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM occurrences WHERE canonical_store='existing_timeline'"
            ).fetchone()[0]
            == 1
        )
    fixture.target = row["id"]
    return fixture


def test_existing_timeline_occurrences_are_revalidated_not_just_bank_messages(
    deduplicated_bank,
):
    report = revalidation.validate_mac_recovery_bank_revalidation(
        deduplicated_bank.root
    )
    assert report["existing_timeline_targets"] == 1


@pytest.mark.parametrize("defect", ["missing", "changed", "conflicting_alias"])
def test_missing_or_changed_deduplicated_targets_fail_closed(deduplicated_bank, defect):
    fixture = deduplicated_bank
    with closing(_comparison(fixture)) as connection:
        if defect == "missing":
            connection.execute(
                "DELETE FROM temp.test_messages WHERE id=?", (fixture.target,)
            )
        elif defect == "changed":
            connection.execute(
                "UPDATE temp.test_messages SET text='different body' WHERE id=?",
                (fixture.target,),
            )
        else:
            values = list(
                connection.execute(
                    "SELECT * FROM temp.test_messages WHERE id=?", (fixture.target,)
                ).fetchone()
            )
            values[0], values[6] = "conflicting-base-alias", "different body"
            connection.execute(
                "INSERT INTO temp.test_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        with pytest.raises(ValueError, match="deduplication"):
            revalidation._compare_base(
                connection, "temp.test_conversations", "temp.test_messages"
            )


def test_warm_media_cache_cannot_hide_a_changed_invalid_current_receipt(stale_bank):
    fixture = stale_bank
    revalidation.validate_mac_recovery_bank_revalidation(fixture.root)
    before = fixture.media.stat()
    key = (
        str(fixture.media.resolve()),
        tuple(_file_identity(fixture.media).items()),
        str(fixture.history.resolve()),
        tuple(_file_identity(fixture.history).items()),
        str(receipt_identity(fixture.history)),
    )
    assert key in event_index._VALIDATED
    fixture.media.chmod(0o600)
    with closing(sqlite3.connect(fixture.media)) as connection, connection:
        connection.execute("UPDATE archive_info SET value='0' WHERE key='published'")
    os.utime(fixture.media, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = fixture.media.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    with pytest.raises(ValueError, match="current media events timeline"):
        revalidation.validate_mac_recovery_bank_revalidation(fixture.root)


def test_changed_historical_bytes_can_only_be_described_as_fresh_semantic_validation(
    stale_bank,
):
    fixture = stale_bank
    before = fixture.media.stat()
    prior_sha = bank._file_sha(fixture.media)
    fixture.media.chmod(0o600)
    with sqlite3.connect(fixture.media) as connection:
        connection.execute(
            "INSERT INTO archive_info VALUES('new_observation','current')"
        )
    os.utime(fixture.media, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert fixture.media.stat().st_size == before.st_size
    current_sha = bank._file_sha(fixture.media)
    assert current_sha != prior_sha
    report = revalidation.validate_mac_recovery_bank_revalidation(fixture.root)
    assert report["current_input_sha256"] == current_sha
    assert report["historical_byte_equivalence"] == "not_established"


def test_preflight_input_drift_refuses_to_clone_or_change_originals(
    stale_bank, monkeypatch
):
    fixture = stale_bank
    original = fixture.bank_path.read_bytes()
    compare = revalidation._compare_base

    def drift(*args, **kwargs):
        result = compare(*args, **kwargs)
        fixture.media.chmod(0o600)
        return result

    monkeypatch.setattr(revalidation, "_compare_base", drift)
    monkeypatch.setattr(
        revalidation,
        "_clonefile",
        lambda *_: pytest.fail("Drifting inputs were cloned"),
    )
    with pytest.raises(ValueError, match="input changed during verification"):
        revalidation.stage_mac_recovery_bank_revalidation(
            fixture.root,
            fixture.history.with_name("raced.sqlite3"),
            preserved_path=fixture.history.with_name("never-preserved.sqlite3"),
        )
    assert fixture.bank_path.read_bytes() == original


def test_canonical_publisher_rejects_input_drift_after_revalidation(stale_bank):
    fixture = stale_bank
    original = fixture.bank_path.read_bytes()
    candidate = fixture.history.with_name("cas-candidate.sqlite3")
    preserved = fixture.history.with_name("cas-preserved.sqlite3")
    receipt = revalidation.stage_mac_recovery_bank_revalidation(
        fixture.root, candidate, preserved_path=preserved
    )
    fixture.media.chmod(0o600)
    with pytest.raises(ValueError, match="Canonical inputs changed before reuse or publication"):
        bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    assert fixture.bank_path.read_bytes() == preserved.read_bytes() == original
    assert candidate.exists()


@pytest.mark.parametrize("failed_clone", [1, 2])
def test_clone_failures_keep_original_and_any_completed_preservation(
    stale_bank, monkeypatch, failed_clone
):
    fixture = stale_bank
    original = fixture.bank_path.read_bytes()
    candidate = fixture.history.with_name("failed-candidate.sqlite3")
    preserved = fixture.history.with_name("failed-preserved.sqlite3")
    attempts = []

    def clone(source, target):
        attempts.append(target)
        if len(attempts) == failed_clone:
            raise ValueError("Synthetic clone failure")
        shutil.copyfile(source, target)

    monkeypatch.setattr(revalidation, "_clonefile", clone)
    with pytest.raises(ValueError, match="Synthetic clone failure"):
        revalidation.stage_mac_recovery_bank_revalidation(
            fixture.root, candidate, preserved_path=preserved
        )
    assert fixture.bank_path.read_bytes() == original
    assert not candidate.exists()
    if failed_clone == 2:
        assert preserved.read_bytes() == original
        assert stat.S_IMODE(preserved.stat().st_mode) == 0o400


def test_existing_review_outputs_are_preserved(stale_bank):
    fixture = stale_bank
    candidate = fixture.history.with_name("existing-candidate.sqlite3")
    preserved = fixture.history.with_name("not-created.sqlite3")
    candidate.write_bytes(b"Existing review artifact")
    with pytest.raises(FileExistsError):
        revalidation.stage_mac_recovery_bank_revalidation(
            fixture.root, candidate, preserved_path=preserved
        )
    assert candidate.read_bytes() == b"Existing review artifact"
    assert not preserved.exists()
