"""One publication preserves independent native snapshots and account boundaries.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import json
import shutil
import sys

import pytest

from app.core import wechat_native_restore as restore
from app.core.wechat_archive import wechat_archive_path
from tests import test_wechat_native_restore as native


@pytest.fixture(autouse=True)
def fixture_clone_backend(monkeypatch):
    if sys.platform != "darwin":
        # Only disposable synthetic test stores use a non-APFS copy backend.
        monkeypatch.setattr(restore, "_clonefile", shutil.copyfile)


def _batch(metadata, observations, snapshot):
    return {
        "sources": [metadata],
        "records": iter(observations),
        "snapshot_id": snapshot,
    }


def _observations(metadata):
    second = {**metadata, "source_file": "Independent iPad snapshot"}
    first_row = native.record(
        metadata,
        "iphone-row",
        server_id="123456",
        text="  Retained payment-card copy\n¥0005.20  ",
        source_locator={"database": "MM.sqlite", "table": "Chat_fixture", "row_id": 7},
    )
    second_row = native.record(
        second,
        "ipad-row",
        server_id="123456",
        text=first_row["text"],
        source_locator={
            "database": "message_1.sqlite",
            "table": "Chat_fixture",
            "row_id": 92,
        },
    )
    return second, first_row, second_row


def _restore(store, batches, **changes):
    return restore.restore_wechat_text_batches(
        store,
        batches=batches,
        snapshot_id="combined-audit-label",
        apply=True,
        **changes,
    )


def test_two_original_snapshots_share_one_clone_check_and_publication(
    tmp_path, monkeypatch
):
    metadata, _ = native.seed(tmp_path)
    other, iphone, ipad = _observations(metadata)
    before = native.retained(tmp_path)
    cloned, statements, published = [], [], []
    connect, clone, replace = (
        restore.sqlite3.connect,
        restore._clonefile,
        restore.os.replace,
    )

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    def clone_once(original, stage):
        cloned.append((original, stage))
        return clone(original, stage)

    def publish_once(stage, target):
        published.append((stage, target))
        return replace(stage, target)

    with monkeypatch.context() as patch:
        patch.setattr(restore.sqlite3, "connect", traced)
        patch.setattr(restore, "_clonefile", clone_once)
        patch.setattr(restore.os, "replace", publish_once)
        report = _restore(
            tmp_path,
            [
                _batch(metadata, [iphone], "original-iphone-snapshot"),
                _batch(other, [ipad], "original-ipad-snapshot"),
            ],
            page_cache_kib=2097152,
        )
    assert report["published"] and report["integrity_check"] == "ok"
    assert report["input_records"] == report["input_batches"] == 2
    assert report["inserted_messages"] == report["new_evidence"] == 1
    assert report["matched_messages"] == 1
    assert len(cloned) == len(published) == 1
    assert statements.count("PRAGMA integrity_check") == 1
    assert "PRAGMA cache_size=-2097152" in statements
    assert not any("quick_check" in sql for sql in statements)
    assert report["source_snapshot_ids"] == [
        "original-iphone-snapshot",
        "original-ipad-snapshot",
    ]
    snapshots = {row[0]: row for row in native.rows(tmp_path, "source_snapshots")}
    assert {row[1] for row in snapshots.values()} == {
        "snapshot-one",
        "original-iphone-snapshot",
        "original-ipad-snapshot",
    }
    assert all(row[1] != "combined-audit-label" for row in snapshots.values())
    messages = native.rows(tmp_path)
    assert len(messages) == 2
    primary = next(row for row in messages if row[6] == iphone["text"])
    primary_proof = json.loads(primary[11])
    assert snapshots[primary_proof["source_snapshot"]][1] == "original-iphone-snapshot"
    assert primary_proof["locator"] == iphone["source_locator"]
    evidence = native.rows(tmp_path, "message_evidence")
    assert len(evidence) == 1 and evidence[0][1] == primary[0]
    assert evidence[0][3] == "original-ipad-snapshot"
    evidence_proof = json.loads(evidence[0][5])
    assert snapshots[evidence_proof["source_snapshot"]][1] == "original-ipad-snapshot"
    assert evidence_proof["locator"] == ipad["source_locator"]
    assert "text" not in evidence_proof
    assert all(row in messages for row in before["messages"])
    assert native.rows(tmp_path, "partitions") == before["partitions"]


def test_exact_multi_snapshot_replay_keeps_bytes_inode_and_observations(tmp_path):
    metadata, _ = native.seed(tmp_path)
    other, iphone, ipad = _observations(metadata)

    def batches():
        return [
            _batch(metadata, [iphone], "original-iphone-snapshot"),
            _batch(other, [ipad], "original-ipad-snapshot"),
        ]

    _restore(tmp_path, batches())
    path = wechat_archive_path(tmp_path)
    before, identity, retained = (
        path.read_bytes(),
        path.stat().st_ino,
        native.retained(tmp_path),
    )
    replay = _restore(tmp_path, batches())
    assert not replay["changed"] and not replay["published"]
    assert replay["replayed_observations"] == 2
    assert replay["inserted_messages"] == replay["new_evidence"] == 0
    assert path.read_bytes() == before and path.stat().st_ino == identity
    assert native.retained(tmp_path) == retained


def test_invalid_later_batch_metadata_fails_before_cloning_or_reading_records(
    tmp_path, monkeypatch
):
    metadata, _ = native.seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()
    other = {**metadata, "source_account_id": "wxid_different_owner"}

    def no_records():
        raise AssertionError(
            "Invalid batch metadata must fail before reading observations."
        )
        yield

    def no_clone(*args):
        raise AssertionError(
            "Invalid batch metadata must fail before creating a stage."
        )

    monkeypatch.setattr(restore, "_clonefile", no_clone)
    with pytest.raises(ValueError, match="source account does not match its hash"):
        _restore(
            tmp_path,
            [
                _batch(metadata, no_records(), "first-snapshot"),
                _batch(other, [], "second-snapshot"),
            ],
        )
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-native-restore-*"))


def test_later_cross_account_record_cannot_publish_valid_earlier_batch(tmp_path):
    metadata, _ = native.seed(tmp_path)
    different = native.source("wxid_another_account")
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="lacks declared source metadata"):
        _restore(
            tmp_path,
            [
                _batch(
                    metadata, [native.record(metadata, "new", server_id="25")], "first"
                ),
                _batch(different, [native.record(metadata, "wrong")], "second"),
            ],
        )
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-native-restore-*"))


def test_same_server_conflict_in_another_snapshot_retains_both_bodies(tmp_path):
    metadata, _ = native.seed(tmp_path)
    other, iphone, ipad = _observations(metadata)
    ipad["text"] = "A conflicting literal source body"
    report = _restore(
        tmp_path,
        [_batch(metadata, [iphone], "iphone"), _batch(other, [ipad], "ipad")],
    )
    assert report["inserted_messages"] == 2 and report["conflict_variants"] == 1
    assert report["new_evidence"] == 0
    assert {row[6] for row in native.rows(tmp_path)} >= {iphone["text"], ipad["text"]}
    assert {row[1] for row in native.rows(tmp_path, "source_snapshots")} == {
        "snapshot-one",
        "iphone",
        "ipad",
    }


def test_equal_server_and_body_remain_separate_between_verified_accounts(tmp_path):
    first = native.source()
    second = native.source("wxid_another_account")
    report = _restore(
        tmp_path,
        [
            _batch(first, [native.record(first, "first")], "first-device"),
            _batch(second, [native.record(second, "second")], "second-device"),
        ],
    )
    assert report["inserted_messages"] == report["added_partitions"] == 2
    assert report["new_evidence"] == 0
    assert len(native.rows(tmp_path)) == 2
    assert {row[2] for row in native.rows(tmp_path, "partitions")} == {
        first["source_account_id"],
        second["source_account_id"],
    }


@pytest.mark.parametrize("budget", [True, False, 0, -1, 2097153, 1.5, "262144", None])
def test_invalid_page_cache_budget_cannot_create_a_stage(tmp_path, budget):
    metadata, _ = native.seed(tmp_path)
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="page cache"):
        _restore(tmp_path, [_batch(metadata, [], "native")], page_cache_kib=budget)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-native-restore-*"))


@pytest.mark.parametrize(
    "batches",
    [
        [],
        [{"sources": [], "records": []}],
        [{"sources": [], "records": [], "snapshot_id": ""}],
    ],
)
def test_missing_snapshot_scope_is_rejected(tmp_path, batches):
    with pytest.raises(ValueError):
        _restore(tmp_path, batches)
    assert not wechat_archive_path(tmp_path).exists()
