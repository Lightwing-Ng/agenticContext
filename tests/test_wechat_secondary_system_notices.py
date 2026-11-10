"""Secondary invitation plans require independent owner, actor and clock evidence.

Code version: v1.2.3-codex.0
"""

from copy import deepcopy
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from app.core import wechat_secondary_system_notices as notices
from app.core.wechat_archive import _SCHEMA
from tests import test_wechat_macos_recovery_account_binding as account
from tests import test_wechat_macos_recovery_bank as native
from tests.test_wechat_wcdb_compat import has_real_compatible_decoder


mac_archive = native.mac_archive
bank_archive = native.bank_archive
source_binding = account.source_binding
PRIMARY = "wxid_primary_invitation_owner"


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _primary_row(message, session, snapshot=1):
    epoch = message["create_time"]
    stamp = datetime.fromtimestamp(epoch, timezone(timedelta(hours=8)))
    return {
        "id": _sha("primary-" + message["server_id"]),
        "conversation_id": session,
        "ordinal": message["local_id"] + 100,
        "sender_id": message["sender_id"],
        "sender_label": message["sender_id"],
        "direction": "received",
        "text": message["text"],
        "time_sort": epoch + 28800,
        "timestamp_value": stamp.isoformat(),
        "timestamp_label": f"{stamp.day} {stamp.strftime('%b %Y %H:%M:%S')}",
        "time_basis": "UTC+08:00",
        "provenance": json.dumps({
            "message_type": 1,
            "text_extraction_kind": "plain_text",
            "server_id": message["server_id"],
            "unix_timestamp": epoch,
            "source_snapshot": snapshot,
            "locator": {
                "table": "Chat_" + hashlib.md5(native.ROOM.encode()).hexdigest(),
            },
        }),
        "quality_flags": "[]",
        "search_text": message["text"].casefold(),
    }


@pytest.fixture
def invitation_scope(source_binding):
    packet = deepcopy(source_binding.packet)
    packet.update(
        peer_id=native.ROOM,
        session_id="wechat:" + _sha("foreign-invitation-session"),
        conversation_kind="group",
        peer_proof=native._proof(10, native.ROOM, is_session=1),
    )
    packet["source"]["table"] = (
        "Msg_" + hashlib.md5(native.ROOM.encode()).hexdigest()
    )
    raw = (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template>'
        "<plain>Inviting member invited you to a group chat with Member A</plain>"
        "</content_template></sysmsgtemplate></sysmsg>"
    )
    packet["messages"] = [
        native._message(10, 1, raw="An exact lower anchor"),
        native._message(20, 10000, raw=raw),
        native._message(30, 1, raw="An exact upper anchor"),
    ]
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    connection.execute(
        "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
        "source_id TEXT,metadata TEXT)"
    )
    partition_id = "ios-" + hashlib.md5(PRIMARY.encode()).hexdigest()
    connection.execute(
        "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
        (partition_id, "account:" + PRIMARY, PRIMARY, "Primary account",
         "Verified archive ID", "iPhone archive", "source", "payload"),
    )
    session = "wechat:" + _sha("primary-invitation-session")
    rows = [_primary_row(message, session) for message in (
        packet["messages"][0], packet["messages"][2]
    )]
    connection.execute(
        "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (session, partition_id, "account:" + PRIMARY, native.ROOM, "A group",
         "group", 2, rows[0]["time_sort"], rows[1]["time_sort"],
         rows[0]["timestamp_value"], rows[1]["timestamp_value"], rows[1]["text"]),
    )
    connection.execute(
        "INSERT INTO source_snapshots VALUES(1,?,?,?)",
        ("primary-snapshot", partition_id, json.dumps({
            "source_id": partition_id,
            "source_account_id": PRIMARY,
            "account_hash": partition_id[4:],
            "account_hash_verified": True,
        })),
    )
    connection.executemany(
        "INSERT INTO messages VALUES(" + ",".join("?" for _ in notices._COLUMNS) + ")",
        [tuple(row[key] for key in notices._COLUMNS) for row in rows],
    )
    rows = [dict(row) for row in connection.execute("SELECT * FROM messages ORDER BY ordinal")]
    conversation = dict(connection.execute("SELECT * FROM conversations").fetchone())
    try:
        yield connection, conversation, packet, rows
    finally:
        connection.close()


def test_bracketed_invitation_retains_foreign_viewer_and_original_source_copy(
    invitation_scope,
):
    history, conversation, packet, rows = invitation_scope
    before = history.total_changes
    plans = notices.build_notice_plans(history, None, packet)
    assert len(plans) == 1
    assert history.total_changes == before
    result = notices.project_notice_plan(plans[0], conversation, rows, plans[0]["event"])
    row = result["message"]
    assert set(row) == set(notices._COLUMNS)
    assert row["conversation_id"] == conversation["id"]
    assert row["sender_id"] == ""
    assert row["direction"] == "unknown"
    assert row["text"] == (
        "Source account " + packet["account_id"] + ": " + packet["messages"][1]["text"]
    )
    assert row["time_sort"] == packet["messages"][1]["create_time"] + 28800
    proof = json.loads(row["provenance"])["secondary_source_invitation"]
    assert proof["source_viewer_account_id"] == packet["account_id"] != PRIMARY
    assert proof["raw_system_text"] == packet["messages"][1]["text"]
    assert proof["raw_system_text_sha256"] == _sha(proof["raw_system_text"])
    assert "source_snapshot" not in json.loads(row["provenance"])


@pytest.mark.parametrize("field", ["text", "sender_id", "time_sort", "provenance"])
def test_changed_primary_anchor_refuses_replay(invitation_scope, field):
    history, conversation, packet, rows = invitation_scope
    plan = notices.build_notice_plans(history, None, packet)[0]
    changed = deepcopy(rows)
    changed[0][field] = 0 if field == "time_sort" else "changed"
    with pytest.raises(ValueError, match="primary row"):
        notices.project_notice_plan(plan, conversation, changed, plan["event"])


@pytest.mark.parametrize("local", [5, 35])
def test_invitation_without_bracketing_source_anchors_is_not_invented(
    invitation_scope, local
):
    history, _conversation, packet, _rows = invitation_scope
    notice = packet["messages"][1]
    notice.update(local_id=local, create_time=1_700_000_000 + local)
    assert notices.build_notice_plans(history, None, packet) == []


def test_inconsistent_primary_native_clock_offsets_refuse_insertion(invitation_scope):
    history, _conversation, packet, rows = invitation_scope
    proof = json.loads(rows[1]["provenance"])
    proof["unix_timestamp"] += 1
    stamp = datetime.fromisoformat(rows[1]["timestamp_value"]) + timedelta(seconds=1)
    history.execute(
        "UPDATE messages SET provenance=?,timestamp_value=?,time_sort=time_sort+1 WHERE id=?",
        (json.dumps(proof), stamp.isoformat(), rows[1]["id"]),
    )
    assert notices.build_notice_plans(history, None, packet) == []


@pytest.mark.parametrize("mutation", ["actor", "body", "owner", "table"])
def test_similar_text_or_neighboring_context_cannot_replace_exact_anchor_evidence(
    invitation_scope, mutation
):
    history, _conversation, packet, rows = invitation_scope
    if mutation == "actor":
        history.execute("UPDATE messages SET sender_id='wxid_other' WHERE id=?", (rows[0]["id"],))
    elif mutation == "body":
        history.execute("UPDATE messages SET text='Similar text' WHERE id=?", (rows[0]["id"],))
    elif mutation == "owner":
        history.execute("UPDATE source_snapshots SET metadata='{}'")
    else:
        proof = json.loads(rows[0]["provenance"])
        proof["locator"]["table"] = "Chat_" + "0" * 32
        history.execute("UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), rows[0]["id"]))
    assert notices.build_notice_plans(history, None, packet) == []


def test_existing_exact_server_event_is_preserved_without_an_added_notice(
    invitation_scope,
):
    history, _conversation, packet, rows = invitation_scope
    proof = json.loads(rows[0]["provenance"])
    proof["server_id"] = packet["messages"][1]["server_id"]
    history.execute("UPDATE messages SET provenance=? WHERE id=?", (json.dumps(proof), rows[0]["id"]))
    assert notices.build_notice_plans(history, None, packet) == []


def test_group_rename_is_not_added_as_an_invitation(invitation_scope):
    history, _conversation, packet, _rows = invitation_scope
    packet["messages"][1] = native._message(
        20, 10000,
        raw='<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template>'
        '<plain>"Member" changed the group name to "Another group"</plain>'
        '</content_template></sysmsgtemplate></sysmsg>',
    )
    assert notices.build_notice_plans(history, None, packet) == []


def test_tampered_plan_or_foreign_event_never_rewrites_a_valid_plan(invitation_scope):
    history, conversation, packet, rows = invitation_scope
    plan = notices.build_notice_plans(history, None, packet)[0]
    changed = deepcopy(plan)
    changed["message"]["text"] = "You invited someone"
    with pytest.raises(ValueError, match="projection"):
        notices.project_notice_plan(changed, conversation, rows, changed["event"])
    event = deepcopy(plan["event"])
    event["peer_id"] = "999@chatroom"
    with pytest.raises(ValueError):
        notices.project_notice_plan(plan, conversation, rows, event)


def _participation_case(
    invitation_scope, *, inviter=PRIMARY, nickname="Inviting member",
    other_label="Literal member",
):
    history, conversation, packet, rows = invitation_scope
    raw = (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
        '<content_template type="tmpl_type_profile"><plain/>'
        '<template>$username$ invited you to a group chat with $others$</template>'
        '<link_list><link name="username" type="link_profile"><memberlist><member>'
        '<username>' + inviter + '</username><nickname>' + nickname + '</nickname>'
        '</member></memberlist></link><link name="others" type="link_profile">'
        '<memberlist><member><username/><nickname>' + other_label + '</nickname></member>'
        '</memberlist></link></link_list></content_template></sysmsgtemplate></sysmsg>'
    )
    message = native._message(5, 10000, raw=raw)
    packet["messages"][1] = message
    first = _primary_row(native._message(1, 1, raw="Primary-only retained earlier text"), conversation["id"])
    history.execute(
        "INSERT INTO messages VALUES(" + ",".join("?" for _ in notices._COLUMNS) + ")",
        tuple(first[key] for key in notices._COLUMNS),
    )
    history.execute(
        "UPDATE conversations SET first_sort=?,first_time=?,message_count=3",
        (first["time_sort"], first["timestamp_value"]),
    )
    conversation = dict(history.execute("SELECT * FROM conversations").fetchone())
    witness = {
        "source_viewer_account_id": packet["account_id"],
        "peer_id": packet["peer_id"], "source": deepcopy(packet["source"]),
        **{key: message[key] for key in (
            "local_id", "server_id", "local_type", "create_time", "content_sha256"
        )},
        "native_storage_class": "text", "compression_marker": 0,
        "native_content_base64": base64.b64encode(raw.encode()).decode(),
        "decoded_text": raw, "decoded_text_sha256": _sha(raw),
    }
    return history, conversation, packet, rows, witness


def test_explicit_inviter_uid_and_two_following_anchors_admit_initial_invitation(
    invitation_scope,
):
    history, conversation, packet, rows, witness = _participation_case(invitation_scope)
    assert notices.build_notice_plans(history, None, packet) == []
    plans = notices.build_notice_plans(
        history, None, packet, native_system_roles=[witness]
    )
    assert len(plans) == 1
    plan = plans[0]
    assert plan["anchor_mode"] == "authenticated_participant"
    result = notices.project_notice_plan(plan, conversation, rows, plan["event"])
    assert result["message"]["text"] == (
        "You invited " + packet["account_id"] + " to a group chat with Literal member"
    )
    assert result["proof"]["raw_system_text"] == packet["messages"][1]["text"]
    assert result["proof"]["raw_system_template"] == (
        "$username$ invited you to a group chat with $others$"
    )
    assert result["proof"]["native_owner_participation"]["visible_participant_roles"] == ["username"]
    assert result["proof"]["viewer_rendering"] == "typed_profile_roles"


def test_a_nickname_equal_to_primary_uid_never_proves_participation(invitation_scope):
    history, _conversation, packet, _rows, witness = _participation_case(
        invitation_scope, inviter="wxid_unrelated", nickname=PRIMARY
    )
    assert notices.build_notice_plans(history, None, packet, native_system_roles=[witness]) == []


def test_viewer_adapter_never_replaces_pronouns_or_placeholders_inside_literal_names(
    invitation_scope,
):
    history, _conversation, packet, _rows, witness = _participation_case(
        invitation_scope, other_label="Literal you and $username$"
    )
    plan = notices.build_notice_plans(
        history, None, packet, native_system_roles=[witness]
    )[0]
    assert plan["message"]["text"] == (
        "You invited " + packet["account_id"]
        + " to a group chat with Literal you and $username$"
    )


def test_unambiguous_non_deictic_source_copy_needs_no_technical_viewer_caption(
    invitation_scope,
):
    history, _conversation, packet, _rows = invitation_scope
    literal = "Inviting member invited Another member to the group chat"
    packet["messages"][1] = native._message(
        20, 10000,
        raw='<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template><plain>'
        + literal + '</plain></content_template></sysmsgtemplate></sysmsg>',
    )
    plan = notices.build_notice_plans(history, None, packet)[0]
    assert plan["message"]["text"] == literal
    assert plan["proof"]["viewer_rendering"] == "literal_source_copy"


@pytest.mark.parametrize("field", ["decoded_text", "native_content_base64", "server_id", "source"])
def test_role_proof_must_reproduce_its_exact_authenticated_native_body(
    invitation_scope, field
):
    history, _conversation, packet, _rows, witness = _participation_case(invitation_scope)
    witness[field] = {} if field == "source" else "changed"
    assert notices.build_notice_plans(history, None, packet, native_system_roles=[witness]) == []


def test_explicit_participation_still_requires_two_anchors_and_primary_time_range(
    invitation_scope,
):
    history, _conversation, packet, rows, witness = _participation_case(invitation_scope)
    history.execute("UPDATE conversations SET first_sort=?", (rows[0]["time_sort"],))
    assert notices.build_notice_plans(history, None, packet, native_system_roles=[witness]) == []
    history.execute("UPDATE conversations SET first_sort=0")
    history.execute("DELETE FROM messages WHERE id=?", (rows[1]["id"],))
    assert notices.build_notice_plans(history, None, packet, native_system_roles=[witness]) == []


def test_compressed_role_envelope_is_verified_before_rendering(invitation_scope):
    history, _conversation, packet, _rows, witness = _participation_case(invitation_scope)
    compressed = base64.b64decode(
        "KLUv/WAmAT0HAFLMIxlwp+1h+lluLwlYe51oGxWqAUKC82G/9H+8aTjy5XeYd8e0/gbc"
        "3eGhGbwAWSiW8bCVk1Q47Zuy2qBtVQhbkCRmqwBSNAwDe99TCrwHDwlvPT4Rdvwm9uYo"
        "TCGpDTOW3pC0jKC/BLxGKDLc6giRmMdrjjo7kFqVchT68qzPqcDEHPTNUUh/GKhab4YjX"
        "44BIiAQHAVhD/KJzHOiO8pgKcZdaDBkHevCgvbBpODuOBNNWrO0x2SeAssGbanv8qtsED"
        "wjZKMSN3ACOMoXC6EjgZnRT7hAJ3F/GKOguzmYEIgqPcCKEg=="
    )
    witness.update(
        native_storage_class="blob", compression_marker=4,
        native_content_base64=base64.b64encode(compressed).decode(),
        content_sha256=hashlib.sha256(compressed).hexdigest(),
    )
    packet["messages"][1]["content_sha256"] = witness["content_sha256"]
    plans = notices.build_notice_plans(
        history, None, packet, native_system_roles=[witness]
    )
    if has_real_compatible_decoder():
        assert len(plans) == 1
    else:
        assert plans == []


def test_compressed_role_never_trusts_metadata_when_no_backend_is_available(
    invitation_scope, monkeypatch
):
    history, _conversation, packet, _rows, witness = _participation_case(invitation_scope)
    def unavailable(*_args, **_kwargs):
        raise notices.WCDBBackendUnavailableError("Synthetic unavailable compatible backend")
    monkeypatch.setattr(notices, "decode_wcdb_text_compatible", unavailable)
    assert notices.build_notice_plans(
        history, None, packet, native_system_roles=[witness]
    ) == []


def test_decoder_transport_updates_keep_staged_proof_v1_2_1_byte_compatible(
    invitation_scope,
):
    history, conversation, packet, rows = invitation_scope
    plan = notices.build_notice_plans(history, None, packet)[0]
    staged = json.loads(json.dumps(plan))
    assert notices.CODE_VERSION == "v1.2.2-codex.0"
    assert staged["proof"]["version"] == "v1.2.1-codex.0"
    result = notices.project_notice_plan(staged, conversation, rows, staged["event"])
    assert result["message"] == staged["message"]


def _insert_shifted_anchor(history, row, *, salt):
    row = deepcopy(row)
    row["id"] = _sha(salt + row["id"])
    proof = json.loads(row["provenance"])
    proof["unix_timestamp"] += 1
    stamp = datetime.fromisoformat(row["timestamp_value"]) + timedelta(seconds=1)
    row.update(
        provenance=json.dumps(proof), timestamp_value=stamp.isoformat(),
        time_sort=row["time_sort"] + 1,
    )
    history.execute(
        "INSERT INTO messages VALUES(" + ",".join("?" for _ in notices._COLUMNS) + ")",
        tuple(row[key] for key in notices._COLUMNS),
    )


def test_nearest_consistent_following_pair_is_not_fenced_by_later_clock_variations(
    invitation_scope,
):
    history, conversation, packet, _rows, witness = _participation_case(invitation_scope)
    for local in (40, 50):
        message = native._message(local, 1, raw="A later independent anchor " + str(local))
        packet["messages"].append(message)
        _insert_shifted_anchor(
            history, _primary_row(message, conversation["id"]), salt="later"
        )
    plan = notices.build_notice_plans(
        history, None, packet, native_system_roles=[witness]
    )[0]
    assert plan["proof"]["primary_clock_delta_seconds"] == 0
    assert [anchor["witness"]["local_id"] for anchor in plan["anchors"]] == [10, 30]


def test_equally_near_conflicting_primary_clocks_remain_unresolved(invitation_scope):
    history, _conversation, packet, rows, witness = _participation_case(invitation_scope)
    for row in rows:
        _insert_shifted_anchor(history, row, salt="conflicting-clock")
    assert notices.build_notice_plans(
        history, None, packet, native_system_roles=[witness]
    ) == []
