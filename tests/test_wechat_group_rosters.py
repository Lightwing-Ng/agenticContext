"""Complete roster observations preserve identity, scope, and unknown exit reasons.

Code version: v1.3.0-codex.0
"""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from threading import Barrier

import pytest

from app.core.wechat_group_rosters import WechatGroupRosterRepository


OWNER = "wxid_roster_owner_a"
OTHER_OWNER = "wxid_roster_owner_b"
ROOM = "123456789@chatroom"
OTHER_ROOM = "987654321@chatroom"
EARLY = "2026-11-18T09:00:00+08:00"
LATE = "2026-11-18T10:00:00+08:00"


def snapshot(
    *members, account=OWNER, peer=ROOM, observed_at=EARLY,
):
    rows = [
        {"username": username, "display_name": label}
        for username, label in (members or (("wxid_member_a", "Member A"),))
    ]
    rows.append({"username": account, "display_name": "Account owner"})
    digest = hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return {
        "account_id": account,
        "peer_id": peer,
        "observed_at": observed_at,
        "members": rows,
        "member_count": len(rows),
        "source": {
            "account_directory": account + "_synthetic",
            "adapter_version": "v1.0.0-codex.0",
            "main_sha256": digest,
            "wal_sha256": hashlib.sha256(b"").hexdigest(),
            "database_relative_path": "db_storage/contact/contact.db",
            "table": "chat_room",
            "integrity_check": "ok",
            "authenticated_pages": True,
            "member_set_cross_checked": True,
            "completeness_scope": "committed_local_roster",
            "roster_count": len(rows),
            "owner_id": account,
            "membership_sha256": hashlib.sha256(json.dumps(
                sorted(row["username"] for row in rows), ensure_ascii=False,
                separators=(",", ":"),
            ).encode()).hexdigest(),
            "room_ext_sha256": digest,
            "wal_tail_status": "none",
            "wal_tail_complete": True,
        },
        "complete": True,
    }


def _database(store):
    return next(store.rglob("*.sqlite3"))


def _with_native_order(value):
    usernames = [member["username"] for member in value["members"]]
    value["source"].update(
        adapter_version="v1.1.0-codex.0",
        member_order_sha256=hashlib.sha256(json.dumps(
            usernames, ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest(),
    )
    return value


def test_first_complete_roster_is_a_baseline_and_later_differences_keep_unknown_reasons(
    tmp_path,
):
    repository = WechatGroupRosterRepository(tmp_path)
    absent = repository.project(OWNER, ROOM)
    assert {key: absent[key] for key in (
        "status", "snapshot_count", "current", "previous", "changes",
    )} == {
        "status": "absent",
        "snapshot_count": 0,
        "current": None,
        "previous": None,
        "changes": (),
    }
    assert not list(tmp_path.iterdir())
    first = snapshot(
        ("wxid_member_a", "Same display name"),
        ("wxid_member_b", "Same display name"),
    )
    first_id = repository.save(first)
    baseline = repository.project(OWNER, ROOM)
    assert baseline["status"] == "ready"
    assert baseline["snapshot_count"] == 1
    assert baseline["previous"] is None
    assert baseline["changes"] == ()
    assert baseline["current"]["member_count"] == 3
    assert baseline["current"]["observed_at"] == EARLY
    assert "18 Nov 2026" in baseline["current"]["observed_label"]

    later = snapshot(
        ("wxid_member_a", "Renamed <b>member</b> 😀"),
        ("wxid_member_c", "Same display name"),
        observed_at=LATE,
    )
    assert repository.save(later) != first_id
    projection = repository.project(OWNER, ROOM)
    assert projection["snapshot_count"] == 2
    assert projection["previous"]["observed_at"] == EARLY
    assert projection["current"]["observed_at"] == LATE
    assert {
        (change["kind"], change["username"], change["reason"])
        for change in projection["changes"]
    } == {
        ("newly_listed", "wxid_member_c", "unknown"),
        ("no_longer_listed", "wxid_member_b", "unknown"),
    }
    assert all(
        change["interval_start"] == EARLY and change["interval_end"] == LATE
        for change in projection["changes"]
    )
    assert {
        member["username"]: member["display_name"]
        for member in projection["current"]["members"]
    } == {
        "wxid_member_a": "Renamed <b>member</b> 😀",
        "wxid_member_c": "Same display name",
        OWNER: "Account owner",
    }


def test_replay_is_idempotent_and_name_or_order_changes_do_not_change_membership(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    first = snapshot(
        ("wxid_member_b", "Member B"), ("wxid_member_a", "Member A"),
    )
    original = deepcopy(first)
    identifier = repository.save(first)
    database = _database(tmp_path)
    before = database.read_bytes()
    assert repository.save(first) == identifier
    reordered = deepcopy(first)
    reordered["members"].reverse()
    assert repository.save(reordered) == identifier
    assert first == original
    assert database.read_bytes() == before
    assert repository.project(OWNER, ROOM)["snapshot_count"] == 1
    later = snapshot(
        ("wxid_member_a", "New A"), ("wxid_member_b", "New B"),
        observed_at=LATE,
    )
    repository.save(later)
    result = repository.project(OWNER, ROOM)
    assert result["snapshot_count"] == 2
    assert result["changes"] == ()


def test_native_order_round_trips_on_the_legacy_membership_stream(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    stays = ("wxid_member_b", "Stays")
    departed = ("wxid_member_a", "Departed")
    joined = ("wxid_member_c", "Joined")
    legacy = repository.save(snapshot(stays, departed))
    current = _with_native_order(snapshot(joined, stays, observed_at=LATE))
    identifier = repository.save(current)
    assert repository.save(deepcopy(current)) == identifier

    result = repository.project(OWNER, ROOM)
    assert result["status"] == "ready"
    assert [item["username"] for item in result["current"]["members"]] == [joined[0], stays[0], OWNER]
    assert result["current"]["id"] == identifier
    assert result["previous"]["id"] == legacy
    assert [item["username"] for item in result["previous"]["members"]] == [departed[0], stays[0], OWNER]
    changes = {("newly_listed", joined[0]), ("no_longer_listed", departed[0])}
    assert {(event["kind"], event["username"]) for event in result["changes"]} == changes
    assert {(event["kind"], event["username"]) for event in repository.history(ROOM)["events"]} == changes


def test_change_in_native_order_updates_roster_presentation_only(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    first = ("wxid_member_a", "Member A")
    second = ("wxid_member_b", "Member B")
    repository.save(_with_native_order(snapshot(first, second)))
    repository.save(_with_native_order(snapshot(second, first, observed_at=LATE)))

    result = repository.project(OWNER, ROOM)
    assert result["snapshot_count"] == 2
    assert [item["username"] for item in result["current"]["members"]] == [second[0], first[0], OWNER]
    assert result["changes"] == ()
    assert repository.history(ROOM)["events"] == ()


@pytest.mark.parametrize("invalid", ["missing", "wrong digest", "changed order"])
def test_native_order_requires_its_exact_digest(tmp_path, invalid):
    repository = WechatGroupRosterRepository(tmp_path)
    current = _with_native_order(snapshot(("wxid_member_a", "Member A"), ("wxid_member_b", "Member B")))
    if invalid == "missing":
        current["source"].pop("member_order_sha256")
    elif invalid == "wrong digest":
        current["source"]["member_order_sha256"] = "0" * 64
    else:
        current["members"].reverse()

    with pytest.raises(ValueError, match="matching source-order digest"):
        repository.save(current)
    assert not repository.path.exists()


def test_unchanged_capture_advances_the_next_difference_observation_interval(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    repository.save(snapshot())
    repository.save(snapshot(observed_at=LATE))
    middle = repository.project(OWNER, ROOM)
    assert middle["snapshot_count"] == 2
    assert middle["changes"] == ()
    repository.save(snapshot(
        ("wxid_member_a", "Member A"), ("wxid_new", "New member"),
        observed_at="2026-11-18T11:00:00+08:00",
    ))
    change, = repository.project(OWNER, ROOM)["changes"]
    assert change["interval_start"] == LATE
    assert change["interval_end"] == "2026-11-18T11:00:00+08:00"


def test_account_and_room_scopes_have_independent_baselines(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    repository.save(snapshot())
    repository.save(snapshot(("wxid_other_room", "Room member"), peer=OTHER_ROOM))
    repository.save(snapshot(("wxid_other_owner", "Account member"), account=OTHER_OWNER))
    repository.save(snapshot(("wxid_new", "New member"), observed_at=LATE))
    for owner, room, username in (
        (OWNER, OTHER_ROOM, "wxid_other_room"),
        (OTHER_OWNER, ROOM, "wxid_other_owner"),
    ):
        result = repository.project(owner, room)
        assert result["snapshot_count"] == 1
        assert result["changes"] == ()
        assert {row["username"] for row in result["current"]["members"]} == {username, owner}
    assert repository.project(OTHER_OWNER, OTHER_ROOM)["status"] == "absent"
    assert repository.project(OWNER, ROOM)["snapshot_count"] == 2


def test_changed_native_source_stream_establishes_a_separate_baseline(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    repository.save(snapshot())
    later = snapshot(("wxid_other_cache", "Another local cache"), observed_at=LATE)
    later["source"]["account_directory"] = OWNER + "_other_device"
    repository.save(later)
    result = repository.project(OWNER, ROOM)
    assert result["snapshot_count"] == 2
    assert result["current"]["observed_at"] == LATE
    assert result["changes"] == ()


def test_room_history_retains_changes_after_unchanged_captures_in_each_source(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    assert repository.history(ROOM)["status"] == "absent"
    unchanged = ("wxid_stays", "Stays")
    departed = ("wxid_departed", "Same name")
    joined = ("wxid_joined", "Same name")
    newest = "2026-11-19T09:00:00+08:00"
    first = repository.save(snapshot(unchanged, departed))
    second = repository.save(snapshot(unchanged, observed_at=LATE))
    repository.save(snapshot((unchanged[0], "Renamed"), observed_at=newest))
    repository.save(snapshot(unchanged, account=OTHER_OWNER, observed_at=LATE))
    repository.save(snapshot(unchanged, joined, account=OTHER_OWNER, observed_at=newest))
    other_stream = snapshot(joined, observed_at=newest)
    other_stream["source"]["account_directory"] = OWNER + "_other_device"
    repository.save(other_stream)
    repository.save(snapshot(departed, peer=OTHER_ROOM))
    repository.save(snapshot(joined, peer=OTHER_ROOM, observed_at=LATE))

    database = _database(tmp_path)
    before = database.read_bytes()
    history = repository.history(ROOM)
    assert history["status"] == "ready"
    assert history["total"] == 2
    assert [
        (event["kind"], event["username"], event["account_id"],
         event["interval_start"], event["interval_end"])
        for event in history["events"]
    ] == [
        ("newly_listed", joined[0], OTHER_OWNER, LATE, newest),
        ("no_longer_listed", departed[0], OWNER, EARLY, LATE),
    ]
    event = history["events"][1]
    assert event["display_name"] == departed[1]
    assert event["reason"] == "unknown"
    assert event["interval_start_label"] == "18 Nov 2026 09:00:00"
    assert event["interval_end_label"] == "18 Nov 2026 10:00:00"
    assert (event["previous_snapshot_id"], event["current_snapshot_id"]) == (first, second)
    assert database.read_bytes() == before


def test_room_history_limits_display_to_the_latest_changes(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.wechat_group_rosters._MAX_HISTORY_EVENTS", 1)
    repository = WechatGroupRosterRepository(tmp_path)
    staying = ("wxid_stays", "Stays")
    repository.save(snapshot(staying))
    repository.save(snapshot(staying, ("wxid_joins", "Joins"), observed_at=LATE))
    repository.save(snapshot(staying, observed_at="2026-11-19T09:00:00+08:00"))

    history = repository.history(ROOM)
    assert history["status"] == "ready"
    assert history["total"] == 2
    latest, = history["events"]
    assert (latest["kind"], latest["username"]) == ("no_longer_listed", "wxid_joins")


@pytest.mark.parametrize("intervening", [None, "presence", "join_notice", "other_subject"])
def test_overlapping_departures_keep_first_absence_until_member_reappears(tmp_path, intervening):
    repository = WechatGroupRosterRepository(tmp_path)
    departed = ("wxid_departed", "Departed")
    staying = ("wxid_stays", "Stays")
    later = "2026-11-18T11:00:00+08:00"
    reappeared = "2026-11-18T10:30:00+08:00"
    repository.save(snapshot(departed, staying, account=OWNER))
    last_seen = repository.save(snapshot(
        departed, staying, account=OTHER_OWNER, observed_at="2026-11-18T09:30:00+08:00",
    ))
    # The source with the later positive observation first confirms the absence.
    first_missing = repository.save(snapshot(staying, account=OTHER_OWNER, observed_at=LATE))
    if intervening == "presence":
        repository.save(snapshot(
            departed, staying, account="wxid_third_roster_source", observed_at=reappeared,
        ))
    repository.save(snapshot(staying, account=OWNER, observed_at=later))
    notices = ()
    if intervening in {"join_notice", "other_subject"}:
        notices = ({
            "kind": "joined",
            "timestamp_value": reappeared,
            "subject_ids": (departed[0] if intervening == "join_notice" else "wxid_another_member",),
        },)

    history = repository.history(ROOM, membership_notices=notices)
    events = history["events"]
    if intervening in {"presence", "join_notice"}:
        assert history["total"] == 2
        assert [event["interval_end"] for event in events] == [later, LATE]
    else:
        assert history["total"] == 1
        event, = events
        assert event["kind"] == "no_longer_listed"
        assert event["reason"] == "unknown"
        assert event["account_id"] == OTHER_OWNER
        assert event["interval_start"] == "2026-11-18T09:30:00+08:00"
        assert event["interval_end"] == LATE
        assert (event["previous_snapshot_id"], event["current_snapshot_id"]) == (last_seen, first_missing)
    comparison, = repository.project(OWNER, ROOM)["changes"]
    assert (comparison["username"], comparison["interval_end"]) == (departed[0], later)


@pytest.mark.parametrize("field,value", [("payload", "{}"), ("stream_id", "0" * 64)])
def test_room_history_cannot_compare_through_an_invalid_observation(tmp_path, field, value):
    repository = WechatGroupRosterRepository(tmp_path)
    repository.save(snapshot())
    between = repository.save(snapshot(observed_at=LATE))
    repository.save(snapshot(
        ("wxid_other", "Another member"), observed_at="2026-11-19T09:00:00+08:00",
    ))
    with sqlite3.connect(_database(tmp_path)) as connection:
        connection.execute(f"UPDATE snapshots SET {field}=? WHERE id=?", (value, between))

    history = repository.history(ROOM)
    assert history["status"] == "invalid"
    assert history["total"] == 0
    assert history["events"] == ()


def test_concurrent_room_captures_retain_both_complete_observations(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    repository.save(snapshot())
    captures = (
        snapshot(("wxid_other_room", "Room member"), peer=OTHER_ROOM),
        snapshot(("wxid_other_owner", "Account member"), account=OTHER_OWNER),
    )

    def save_capture(capture):
        return WechatGroupRosterRepository(tmp_path).save(capture)

    with ThreadPoolExecutor(max_workers=2) as executor:
        identifiers = tuple(executor.map(save_capture, captures))
    assert len(set(identifiers)) == 2
    for capture in captures:
        result = repository.project(capture["account_id"], capture["peer_id"])
        assert result["status"] == "ready"
        assert result["snapshot_count"] == 1
        assert result["current"]["member_count"] == 2
        assert result["changes"] == ()


def test_simultaneous_first_captures_publish_one_complete_schema(tmp_path):
    barrier = Barrier(2)
    captures = (snapshot(), snapshot(account=OTHER_OWNER))

    def save_first_capture(capture):
        barrier.wait(timeout=5)
        return WechatGroupRosterRepository(tmp_path).save(capture)

    with ThreadPoolExecutor(max_workers=2) as executor:
        identifiers = tuple(executor.map(save_first_capture, captures))
    assert len(set(identifiers)) == 2
    repository = WechatGroupRosterRepository(tmp_path)
    for owner in (OWNER, OTHER_OWNER):
        result = repository.project(owner, ROOM)
        assert result["status"] == "ready"
        assert result["snapshot_count"] == 1
        assert result["changes"] == ()


def test_invalid_first_capture_cannot_create_an_empty_roster_database(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    invalid = snapshot()
    invalid["complete"] = False
    with pytest.raises(ValueError):
        repository.save(invalid)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "invalidity",
    [
        "incomplete", "missing_complete", "empty", "wrong_count", "duplicate_id",
        "missing_id", "blank_id", "padded_id", "room_as_member", "nul_id",
        "naive_time", "bad_time", "unclassified_account", "private_peer",
        "unauthenticated_source", "failed_integrity", "bad_digest", "wrong_database",
        "wrong_source_owner",
        "missing_source_member",
        "uncorroborated_members", "wrong_completeness", "wrong_roster_count",
        "wrong_membership_digest", "missing_owner", "missing_room_digest",
        "missing_wal_status", "incorrect_wal_completion",
    ],
)
def test_unusable_rosters_cannot_replace_a_complete_baseline(tmp_path, invalidity):
    repository = WechatGroupRosterRepository(tmp_path)
    repository.save(snapshot())
    before = _database(tmp_path).read_bytes()
    invalid = snapshot(("wxid_replacement", "New member"), observed_at=LATE)
    if invalidity == "incomplete":
        invalid["complete"] = False
    elif invalidity == "missing_complete":
        invalid.pop("complete")
    elif invalidity == "empty":
        invalid.update(members=[], member_count=0)
    elif invalidity == "wrong_count":
        invalid["member_count"] = len(invalid["members"]) + 1
    elif invalidity == "duplicate_id":
        invalid["members"].append(dict(invalid["members"][0], display_name="Different label"))
        invalid["member_count"] = len(invalid["members"])
    elif invalidity == "missing_id":
        invalid["members"][0].pop("username")
    elif invalidity in {"blank_id", "padded_id", "room_as_member", "nul_id"}:
        invalid["members"][0]["username"] = {
            "blank_id": "", "padded_id": " wxid_replacement ",
            "room_as_member": ROOM, "nul_id": "wxid_member\0suffix",
        }[invalidity]
    elif invalidity == "naive_time":
        invalid["observed_at"] = "2026-11-18T10:00:00"
    elif invalidity == "bad_time":
        invalid["observed_at"] = "unknown"
    elif invalidity == "unclassified_account":
        invalid["account_id"] = "unverified account name"
    elif invalidity == "private_peer":
        invalid["peer_id"] = "wxid_private_contact"
    elif invalidity == "unauthenticated_source":
        invalid["source"]["authenticated_pages"] = False
    elif invalidity == "failed_integrity":
        invalid["source"]["integrity_check"] = "failed"
    elif invalidity == "bad_digest":
        invalid["source"]["main_sha256"] = "invalid"
    elif invalidity == "wrong_database":
        invalid["source"]["database_relative_path"] = "../contact.db"
    elif invalidity == "wrong_source_owner":
        invalid["source"]["account_directory"] = OTHER_OWNER + "_synthetic"
    elif invalidity == "missing_source_member":
        invalid["members"] = [member for member in invalid["members"] if member["username"] != OWNER]
        invalid["member_count"] = len(invalid["members"])
    elif invalidity == "uncorroborated_members":
        invalid["source"]["member_set_cross_checked"] = False
    elif invalidity == "wrong_completeness":
        invalid["source"]["completeness_scope"] = "unknown"
    elif invalidity == "wrong_roster_count":
        invalid["source"]["roster_count"] = 999
    elif invalidity == "wrong_membership_digest":
        invalid["source"]["membership_sha256"] = "0" * 64
    elif invalidity == "missing_owner":
        invalid["source"]["owner_id"] = "wxid_not_in_roster"
    elif invalidity == "missing_room_digest":
        invalid["source"].pop("room_ext_sha256")
    elif invalidity == "missing_wal_status":
        invalid["source"].pop("wal_tail_status")
    else:
        invalid["source"]["wal_tail_complete"] = False
    with pytest.raises(ValueError):
        repository.save(invalid)
    assert _database(tmp_path).read_bytes() == before
    result = repository.project(OWNER, ROOM)
    assert result["snapshot_count"] == 1
    assert result["current"]["observed_at"] == EARLY
    assert result["changes"] == ()


@pytest.mark.parametrize("observed_at", [EARLY, "2026-11-18T08:00:00+08:00"])
def test_conflicting_or_older_observation_cannot_overwrite_current_roster(tmp_path, observed_at):
    repository = WechatGroupRosterRepository(tmp_path)
    repository.save(snapshot())
    before = _database(tmp_path).read_bytes()
    with pytest.raises(ValueError):
        repository.save(snapshot(("wxid_conflicting", "Conflicting member"), observed_at=observed_at))
    assert _database(tmp_path).read_bytes() == before
    assert repository.project(OWNER, ROOM)["snapshot_count"] == 1


def test_departed_member_reappearing_records_a_new_observation_with_unknown_reason(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    repository.save(snapshot(("wxid_staying", "Stays"), ("wxid_returning", "Returns")))
    repository.save(snapshot(("wxid_staying", "Stays"), observed_at=LATE))
    left, = repository.project(OWNER, ROOM)["changes"]
    assert (left["kind"], left["username"], left["reason"]) == (
        "no_longer_listed", "wxid_returning", "unknown",
    )
    repository.save(snapshot(
        ("wxid_staying", "Stays"), ("wxid_returning", "New display name"),
        observed_at="2026-11-18T11:00:00+08:00",
    ))
    returned, = repository.project(OWNER, ROOM)["changes"]
    assert (returned["kind"], returned["username"], returned["reason"]) == (
        "newly_listed", "wxid_returning", "unknown",
    )


def test_unreadable_roster_database_is_reported_invalid(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    repository.save(snapshot())
    database = _database(tmp_path)
    database.write_bytes(b"Synthetic unreadable roster database")
    result = repository.project(OWNER, ROOM)
    assert result["status"] == "invalid"
    assert result["current"] is None
    assert result["changes"] == ()
    assert database.read_bytes() == b"Synthetic unreadable roster database"


def test_changed_indexed_observation_time_cannot_reorder_authenticated_snapshots(tmp_path):
    repository = WechatGroupRosterRepository(tmp_path)
    repository.save(snapshot())
    repository.save(snapshot(("wxid_new", "New member"), observed_at=LATE))
    with sqlite3.connect(_database(tmp_path)) as connection:
        connection.execute(
            "UPDATE snapshots SET observed_at='2027-01-01T00:00:00+00:00' "
            "WHERE id=(SELECT id FROM snapshots ORDER BY observed_at LIMIT 1)"
        )
    result = repository.project(OWNER, ROOM)
    assert result["status"] == "invalid"
    assert result["current"] is None
    assert result["changes"] == ()
