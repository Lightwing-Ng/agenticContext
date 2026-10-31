"""Additive native-text reconciliation preserves original account evidence."""

# Code version: v1.1.0-codex.0

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import sqlite3

import pytest

from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_reconcile import reconcile_wechat_records
from tests.test_wechat_archive import make_archive


_CLOCK = "2021-08-07T10:59:03+00:00"
_UNIX = int(datetime.fromisoformat(_CLOCK).timestamp())
_SID = "9223372036854775806"


def source(owner="wxid_account_a", *, unknown_hash=None):
    account_hash = unknown_hash or hashlib.md5(owner.encode()).hexdigest()
    return {
        "source_id": ("ios-unconfirmed-" if unknown_hash else "ios-") + account_hash,
        "account_hash": account_hash,
        "source_account_id": None if unknown_hash else owner,
        "source_account_alias": "Synthetic account",
        "account_hash_verified": unknown_hash is None,
        "source_file": "Synthetic Finder backup",
        "backup_id": "synthetic-device-backup",
    }


def record(metadata, record_id="native-1", *, peer="peer-direct", **changes):
    result = {
        "source_id": metadata["source_id"],
        "account_partition": metadata["source_id"],
        "source_account_id": metadata["source_account_id"],
        "extraction_record_id": record_id,
        "conversation_id": peer,
        "conversation_hash": hashlib.md5(peer.encode()).hexdigest(),
        "conversation_display_name": "Synthetic conversation",
        "conversation_kind": "group" if peer.endswith("@chatroom") else "direct",
        "sender_id": "wxid_peer",
        "sender_display_name": "Synthetic sender",
        "direction": "received",
        "text": "  原始文字\nwith whitespace  ",
        "message_type": 1,
        "text_extraction_kind": "plain_text",
        "server_id": _SID,
        "sent_at_utc_iso": _CLOCK,
        "sent_at_raw": str(_UNIX),
        "unix_timestamp": _UNIX,
        "source_locator": {
            "database": "message_0.sqlite",
            "table": "Chat_fixture",
            "row": record_id,
        },
        "flags": [],
    }
    result.update(changes)
    return result


def rows(store, table="messages"):
    path = wechat_archive_path(store)
    if not path.exists():
        return []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        return connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def merge(store, metadata, observations, snapshot="snapshot-one"):
    return reconcile_wechat_records(
        store, sources=metadata, records=iter(observations), snapshot_id=snapshot
    )


def test_initial_reconciliation_and_exact_replay_are_idempotent(tmp_path):
    metadata = source()
    observation = record(metadata)
    result = merge(tmp_path, [metadata], [observation])
    assert result["inserted_messages"] == result["messages_after"] == 1
    assert result["new_evidence"] == 0
    assert result["integrity_check"] == "ok"
    first = rows(tmp_path)[0]
    assert first[6] == observation["text"]
    assert json.loads(first[11])["server_id"] == _SID
    before = wechat_archive_path(tmp_path).read_bytes()
    replay = merge(tmp_path, [metadata], [observation])
    assert replay["replayed_observations"] == 1
    assert replay["inserted_messages"] == replay["new_evidence"] == 0
    assert not replay["changed"]
    assert wechat_archive_path(tmp_path).read_bytes() == before
    assert len(rows(tmp_path, "source_snapshots")) == 1


def test_shorter_snapshot_preserves_imported_sources_and_message_bytes(tmp_path):
    archive = make_archive(tmp_path / "archive")
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    old_messages = rows(store)
    old_partitions = rows(store, "partitions")
    metadata = source()
    observation = record(
        metadata,
        "new-extraction-locator",
        text="你好 wxid_account_a direct <img src=https://invalid.example/track>",
    )
    result = merge(store, [metadata], [observation])
    assert result["messages_before"] == result["messages_after"] == 8
    assert result["inserted_messages"] == 0
    assert result["matched_messages"] == result["new_evidence"] == 1
    assert result["old_messages_unchanged"] and result["old_partitions_unchanged"]
    assert rows(store) == old_messages
    assert rows(store, "partitions") == old_partitions
    evidence = rows(store, "message_evidence")
    assert len(evidence) == 1
    assert "text" not in json.loads(evidence[0][5])
    assert "content_sha256" in json.loads(evidence[0][5])
    replay = merge(store, [metadata], [observation])
    assert replay["replayed_observations"] == 1 and not replay["changed"]


def test_same_server_id_and_peer_never_merge_accounts(tmp_path):
    first = source("wxid_account_a")
    second = source("wxid_account_b")
    result = merge(tmp_path, [first, second], [record(first), record(second)])
    assert result["inserted_messages"] == 2
    assert result["source_partitions"] == result["conversations"] == 2
    assert len({row[1] for row in rows(tmp_path, "partitions")}) == 2


def test_strong_server_identity_corroborates_and_keeps_conflicting_bodies(tmp_path):
    metadata = source()
    first = record(metadata)
    merge(tmp_path, [metadata], [first])
    old = rows(tmp_path)
    corroboration = record(metadata, "second-locator")
    result = merge(tmp_path, [metadata], [corroboration], "snapshot-two")
    assert result["matched_messages"] == 1 and result["inserted_messages"] == 0
    assert rows(tmp_path) == old
    changed = record(metadata, "conflict-locator", text="A conflicting source body")
    result = merge(tmp_path, [metadata], [changed], "snapshot-two")
    assert result["conflict_variants"] == result["inserted_messages"] == 1
    assert len(rows(tmp_path)) == 2
    assert all(row in rows(tmp_path) for row in old)
    replay = merge(tmp_path, [metadata], [changed], "snapshot-two")
    assert not replay["changed"] and replay["replayed_observations"] == 1


@pytest.mark.parametrize("server_id", ["0", None, "", 0])
def test_missing_server_ids_do_not_collapse_repeated_text(tmp_path, server_id):
    metadata = source()
    first = record(metadata, "local-one", server_id=server_id)
    second = record(metadata, "local-two", server_id=server_id)
    result = merge(tmp_path, [metadata], [first, second])
    assert result["inserted_messages"] == 2
    assert result["matched_messages"] == 0
    assert len(rows(tmp_path)) == 2
    assert not merge(tmp_path, [metadata], [first, second])["changed"]


@pytest.mark.parametrize("server_id", ["0", _SID])
def test_reused_extraction_identity_keeps_an_idempotent_conflict_variant(
    tmp_path, server_id
):
    metadata = source()
    first = record(metadata, server_id=server_id)
    merge(tmp_path, [metadata], [first])
    old = rows(tmp_path)[0]
    changed = {**first, "text": "Changed at the same extraction locator"}
    result = merge(tmp_path, [metadata], [changed])
    assert result["identity_conflicts"] == result["conflict_variants"] == 1
    assert result["messages_after"] == 2 and old in rows(tmp_path)
    replay = merge(tmp_path, [metadata], [changed])
    assert not replay["changed"] and replay["messages_after"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"sender_id": "wxid_another"},
        {"direction": "sent"},
        {
            "unix_timestamp": _UNIX + 1,
            "sent_at_utc_iso": "2021-08-07T10:59:04+00:00",
        },
    ],
)
def test_usable_sender_direction_or_time_conflicts_are_retained(tmp_path, change):
    metadata = source()
    merge(tmp_path, [metadata], [record(metadata)])
    result = merge(
        tmp_path, [metadata], [record(metadata, "different-locator", **change)]
    )
    assert result["conflict_variants"] == result["inserted_messages"] == 1
    assert result["messages_after"] == 2


def test_new_sender_label_is_additional_evidence_not_an_overwrite(tmp_path):
    metadata = source()
    merge(tmp_path, [metadata], [record(metadata)])
    old = rows(tmp_path)
    changed = record(metadata, sender_display_name="A later observed nickname")
    result = merge(tmp_path, [metadata], [changed])
    assert result["matched_messages"] == result["new_evidence"] == 1
    assert rows(tmp_path) == old
    assert "A later observed nickname" in rows(tmp_path, "message_evidence")[0][5]
    assert not merge(tmp_path, [metadata], [changed])["changed"]


def test_signed_native_server_ids_are_exact_nonzero_identities(tmp_path):
    metadata = source()
    first = record(metadata, server_id="-9223372036854775807")
    merge(tmp_path, [metadata], [first])
    second = record(metadata, "another-locator", server_id=first["server_id"])
    result = merge(tmp_path, [metadata], [second], "snapshot-two")
    assert result["matched_messages"] == 1 and result["inserted_messages"] == 0
    assert json.loads(rows(tmp_path)[0][11])["server_id"] == first["server_id"]


def test_zero_id_can_gain_independent_server_identity_without_rewriting_it(tmp_path):
    metadata = source()
    pending = record(metadata, server_id="0")
    merge(tmp_path, [metadata], [pending])
    original = rows(tmp_path)
    assigned = record(metadata, server_id="777")
    another_locator = record(metadata, "another-shard-locator", server_id="777")
    result = merge(tmp_path, [metadata], [assigned, another_locator])
    assert result["matched_messages"] == result["new_evidence"] == 2
    assert result["inserted_messages"] == 0
    assert rows(tmp_path) == original
    replay = merge(tmp_path, [metadata], [another_locator])
    assert not replay["changed"] and replay["replayed_observations"] == 1
    assert rows(tmp_path) == original


def test_changed_reliable_server_id_at_one_locator_is_a_conflict(tmp_path):
    metadata = source()
    merge(tmp_path, [metadata], [record(metadata, server_id="777")])
    result = merge(tmp_path, [metadata], [record(metadata, server_id="888")])
    assert result["inserted_messages"] == result["conflict_variants"] == 1
    assert result["messages_after"] == 2


def test_unconfirmed_native_partitions_stay_separate(tmp_path):
    first = source(unknown_hash="a" * 32)
    second = source(unknown_hash="b" * 32)
    result = merge(tmp_path, [first, second], [record(first), record(second)])
    assert result["inserted_messages"] == result["source_partitions"] == 2
    partitions = rows(tmp_path, "partitions")
    assert all(row[2] is None and row[1].startswith("source:") for row in partitions)


def test_conversation_hash_resolves_an_existing_peer_conversation(tmp_path):
    metadata = source()
    first = record(metadata)
    merge(tmp_path, [metadata], [first])
    old_conversation = rows(tmp_path, "conversations")[0]
    without_peer = record(metadata, "hash-only-locator", conversation_id="")
    result = merge(tmp_path, [metadata], [without_peer], "snapshot-two")
    assert result["added_conversations"] == result["inserted_messages"] == 0
    assert result["matched_messages"] == 1
    assert rows(tmp_path, "conversations")[0] == old_conversation


def test_known_peer_resolves_an_earlier_hash_only_conversation(tmp_path):
    metadata = source()
    peer = "test-group@chatroom"
    first = record(metadata, peer=peer, conversation_id="")
    merge(tmp_path, [metadata], [first])
    old_conversation = rows(tmp_path, "conversations")[0]
    known = record(metadata, "peer-resolved-locator", peer=peer)
    result = merge(tmp_path, [metadata], [known], "snapshot-two")
    assert result["added_conversations"] == result["inserted_messages"] == 0
    assert result["matched_messages"] == 1
    assert rows(tmp_path, "conversations")[0] == old_conversation


@pytest.mark.parametrize(
    ("original_type", "text_kind"),
    [
        (1, "plain_text"),
        (49, "app_text"),
        (49, "text_appmsg"),
        (49, "quoted_reply"),
        (49, "forwarded_text"),
        (10000, "system_text"),
        (10002, "system_text"),
    ],
)
def test_original_text_type_and_extraction_kind_are_preserved(
    tmp_path, original_type, text_kind
):
    metadata = source()
    observation = record(
        metadata, message_type=original_type, text_extraction_kind=text_kind
    )
    result = merge(tmp_path, [metadata], [observation])
    assert result["inserted_messages"] == 1
    provenance = json.loads(rows(tmp_path)[0][11])
    assert provenance["message_type"] == original_type
    assert provenance["text_extraction_kind"] == text_kind


def test_source_exclusion_is_applied_before_record_validation(tmp_path):
    retained = source("wxid_retained")
    banned = source("wxid_excluded")
    merge(tmp_path, [retained], [record(retained)])
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()
    path.with_name("exclusions.json").write_text(
        json.dumps(
            {"schema_version": 1, "sources": [{"source_id": banned["source_id"]}]}
        )
    )
    result = merge(
        tmp_path,
        [{"source_id": banned["source_id"]}],
        [{"source_id": banned["source_id"], "text": object()}],
    )
    assert result["excluded_records"] == 1 and not result["changed"]
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        {"message_type": 3},
        {"message_type": 49, "text_extraction_kind": "file"},
        {"message_type": 10000, "text_extraction_kind": "plain_text"},
        {"source_account_id": "wxid_crossed_account"},
        {"conversation_id": "a-peer-that-does-not-match-the-table"},
    ],
)
def test_invalid_native_observation_preserves_previous_database(tmp_path, change):
    metadata = source()
    merge(tmp_path, [metadata], [record(metadata)])
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        merge(
            tmp_path,
            [metadata],
            [record(metadata, "bad-row", **change)],
            "invalid-snapshot",
        )
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-reconcile-*"))


def test_corrupt_source_identity_fails_before_any_publication(tmp_path):
    metadata = source()
    merge(tmp_path, [metadata], [record(metadata)])
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()
    bad = deepcopy(metadata)
    bad["source_account_id"] = "wxid_a_different_owner"
    with pytest.raises(ValueError, match="hash"):
        merge(tmp_path, [bad], [record(bad)])
    assert path.read_bytes() == before


def test_streaming_failure_discards_partial_new_rows(tmp_path):
    metadata = source()
    merge(tmp_path, [metadata], [record(metadata)])
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()

    def interrupted():
        yield record(metadata, "new-row", server_id="5")
        raise RuntimeError("Synthetic extraction interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        reconcile_wechat_records(
            tmp_path,
            sources=[metadata],
            records=interrupted(),
            snapshot_id="interrupted",
        )
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-reconcile-*"))


def test_concurrent_archive_change_is_not_overwritten(tmp_path):
    metadata = source()
    merge(tmp_path, [metadata], [record(metadata)])
    path = wechat_archive_path(tmp_path)

    def concurrently_changed():
        yield record(metadata, "unpublished-row", server_id="5")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO archive_info VALUES('other_writer','retained')"
            )

    with pytest.raises(ValueError, match="changed during"):
        reconcile_wechat_records(
            tmp_path,
            sources=[metadata],
            records=concurrently_changed(),
            snapshot_id="concurrent-snapshot",
        )
    assert len(rows(tmp_path)) == 1
    assert ("other_writer", "retained") in rows(tmp_path, "archive_info")
    assert not list(path.parent.glob(".wechat-reconcile-*"))


def test_concurrent_exclusion_policy_change_discards_the_new_snapshot(tmp_path):
    metadata = source()
    merge(tmp_path, [metadata], [record(metadata)])
    path = wechat_archive_path(tmp_path)
    before = path.read_bytes()

    def policy_changed():
        yield record(metadata, "unpublished-row", server_id="5")
        path.with_name("exclusions.json").write_text(
            json.dumps(
                {"schema_version": 1, "sources": [{"source_id": metadata["source_id"]}]}
            )
        )

    with pytest.raises(ValueError, match="changed during"):
        reconcile_wechat_records(
            tmp_path,
            sources=[metadata],
            records=policy_changed(),
            snapshot_id="policy-change",
        )
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-reconcile-*"))


def test_records_are_consumed_as_a_stream_without_full_text_evidence_copies(tmp_path):
    metadata = source()
    result = reconcile_wechat_records(
        tmp_path,
        sources=[metadata],
        records=(
            record(metadata, f"stream-{index}", server_id=str(index + 1))
            for index in range(2000)
        ),
        snapshot_id="streamed-snapshot",
    )
    assert result["input_records"] == result["inserted_messages"] == 2000
    assert result["messages_after"] == 2000
    assert rows(tmp_path, "message_evidence") == []


def test_larger_cache_budgets_preserve_the_same_evidence(tmp_path):
    metadata = source()
    stores = [tmp_path / "default", tmp_path / "larger"]
    for store, budgets in zip(
        stores,
        ({}, {"database_cache_mib": 64, "server_id_cache_mib": 16}),
    ):
        result = reconcile_wechat_records(
            store,
            sources=[metadata],
            records=(
                record(metadata, f"cache-{index}", server_id=str(index + 1))
                for index in range(200)
            ),
            snapshot_id="same-snapshot",
            **budgets,
        )
        assert result["messages_after"] == 200
    for table in (
        "messages",
        "partitions",
        "conversations",
        "message_evidence",
        "source_snapshots",
    ):
        assert rows(stores[0], table) == rows(stores[1], table)
    assert result["cache_budgets_mib"] == {"database": 64, "server_ids": 16}


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_invalid_cache_budgets_fail_before_consuming_observations(tmp_path, budget):
    def observations():
        pytest.fail("Invalid cache budgets must not consume source observations")
        yield

    with pytest.raises(ValueError, match="cache budgets"):
        reconcile_wechat_records(
            tmp_path,
            sources=[source()],
            records=observations(),
            snapshot_id="invalid-cache",
            database_cache_mib=budget,
        )
    assert not wechat_archive_path(tmp_path).exists()
