"""Native-owner system completion keeps the existing bank projection contract.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_native_system_notice_strategy as own
from app.core import wechat_secondary_system_notices as foreign
from tests import test_wechat_macos_recovery_account_binding as account
from tests import test_wechat_macos_recovery_bank as native

mac_archive = native.mac_archive
bank_archive = native.bank_archive
source_binding = account.source_binding

RAW = (
    '<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template type="tmpl_type_profile"><plain/>'
    "<template>$username$ invited you to a group chat with $others$</template>"
    '<link_list><link name="username" type="link_profile"><memberlist><member>'
    "<username/><nickname>Literal member &amp; You</nickname></member></memberlist></link>"
    '<link name="others" type="link_profile"><memberlist><member>'
    "<username>wxid_synthetic_member</username><nickname>Other member</nickname>"
    "</member></memberlist></link></link_list></content_template></sysmsgtemplate></sysmsg>"
)


@pytest.fixture
def owner_scope(source_binding):
    packet = deepcopy(source_binding.packet)
    packet.update(
        session_id=source_binding.fixture.group["session_id"],
        peer_id=native.ROOM,
        conversation_kind="group",
        peer_proof=native._proof(10, native.ROOM, is_session=1),
    )
    packet["source"]["table"] = "Msg_" + hashlib.md5(native.ROOM.encode()).hexdigest()
    message = native._message(90, 10000, raw=RAW)
    packet["messages"] = [message]
    witness = {
        "source_viewer_account_id": packet["account_id"],
        "peer_id": packet["peer_id"],
        "source": deepcopy(packet["source"]),
        **{
            key: message[key]
            for key in (
                "local_id",
                "server_id",
                "local_type",
                "create_time",
                "content_sha256",
            )
        },
        "native_storage_class": "text",
        "compression_marker": 0,
        "native_content_base64": base64.b64encode(RAW.encode()).decode(),
        "decoded_text": RAW,
        "decoded_text_sha256": hashlib.sha256(RAW.encode()).hexdigest(),
    }
    connection = sqlite3.connect(source_binding.history)
    connection.row_factory = sqlite3.Row
    try:
        conversation = dict(
            connection.execute(
                "SELECT * FROM conversations WHERE id=?", (packet["session_id"],)
            ).fetchone()
        )
        yield connection, conversation, packet, witness
    finally:
        connection.close()


def _plans(scope):
    connection, _conversation, packet, witness = scope
    return own.build_native_owner_notice_plans(
        connection, None, packet, native_system_roles=[witness]
    )


def test_native_owner_plan_reuses_every_bank_row_field_and_source_epoch(owner_scope):
    connection, conversation, packet, witness = owner_scope
    main_path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    before = main_path.read_bytes()
    plans = _plans(owner_scope)
    assert len(plans) == 1
    assert main_path.read_bytes() == before
    plan = plans[0]
    expected = dict(
        zip(foreign._COLUMNS, bank._row(packet, packet["messages"][0]), strict=True)
    )
    assert plan["message"] == expected
    assert own.project_native_owner_notice_plan(
        plan, conversation, [], plan["event"]
    ) == {"message": expected, "proof": plan["proof"]}
    assert (
        datetime.fromisoformat(expected["timestamp_value"]).timestamp()
        == packet["messages"][0]["create_time"]
    )
    assert expected["direction"] == packet["messages"][0]["direction"] == "unknown"
    assert expected["text"] == packet["messages"][0]["text"]
    assert "Source account " not in expected["text"]
    assert "you to a group chat" in expected["text"]
    assert plan["anchors"] == []
    assert "primary_clock_delta_seconds" not in plan["proof"]
    assert (
        json.loads(expected["provenance"])["macos_recovery"]["account_id"]
        == packet["account_id"]
    )
    assert foreign.VERSION == "v1.2.1-codex.0"


@pytest.mark.parametrize("field", ["account_id", "session_id", "peer_id"])
def test_native_owner_scope_cannot_be_rebound_to_a_foreign_owner_or_peer(
    owner_scope, field
):
    _connection, conversation, _packet, _witness = owner_scope
    plan = deepcopy(_plans(owner_scope)[0])
    plan["event"][field] = "wxid_synthetic_unrelated"
    with pytest.raises(ValueError):
        own.project_native_owner_notice_plan(plan, conversation, [], plan["event"])


@pytest.mark.parametrize(
    "field",
    [
        "content_sha256",
        "source_viewer_account_id",
        "decoded_text_sha256",
        "compression_marker",
    ],
)
def test_native_owner_envelope_metadata_cannot_replace_its_exact_source(
    owner_scope, field
):
    _connection, conversation, _packet, _witness = owner_scope
    plan = deepcopy(_plans(owner_scope)[0])
    plan["system_template_witness"][field] = (
        9 if field == "compression_marker" else "tampered"
    )
    with pytest.raises(ValueError):
        own.project_native_owner_notice_plan(plan, conversation, [], plan["event"])


def test_native_owner_plan_requires_live_independent_account_binding(owner_scope):
    connection, _conversation, packet, witness = owner_scope
    anchor = packet["account_binding"]["anchors"][0]["archive_message_id"]
    connection.execute(
        "UPDATE messages SET text=? WHERE id=?", ("Changed actual primary text", anchor)
    )
    with pytest.raises(ValueError):
        own.build_native_owner_notice_plans(
            connection, None, packet, native_system_roles=[witness]
        )


def test_existing_primary_server_is_preserved_without_new_native_duplicate(owner_scope):
    connection, conversation, packet, _witness = owner_scope
    original = dict(
        connection.execute(
            "SELECT * FROM messages WHERE conversation_id=? LIMIT 1",
            (conversation["id"],),
        ).fetchone()
    )
    original.update(
        id="synthetic-existing-system-event",
        provenance=json.dumps({"server_id": packet["messages"][0]["server_id"]}),
    )
    connection.execute(
        "INSERT INTO messages VALUES(" + ",".join("?" for _ in foreign._COLUMNS) + ")",
        tuple(original[key] for key in foreign._COLUMNS),
    )
    connection.commit()
    assert _plans(owner_scope) == []


def test_native_owner_plan_needs_its_retained_envelope_and_no_foreign_anchors(
    owner_scope,
):
    connection, conversation, packet, _witness = owner_scope
    assert own.build_native_owner_notice_plans(connection, None, packet) == []
    plan = _plans(owner_scope)[0]
    with pytest.raises(ValueError):
        own.project_native_owner_notice_plan(
            plan, conversation, [{"id": "foreign-anchor"}], plan["event"]
        )


def test_native_owner_rename_and_type_10002_reuse_closed_parser_without_invitation_guess(
    owner_scope,
):
    connection, _conversation, packet, witness = owner_scope
    raw = RAW.replace(
        "$username$ invited you to a group chat with $others$",
        "$username$ changed the group name to $others$",
    )
    for typ in (10000, 10002):
        packet["messages"] = [native._message(91, typ, raw=raw)]
        message = packet["messages"][0]
        updated = dict(
            witness,
            **{
                key: message[key]
                for key in (
                    "local_id",
                    "server_id",
                    "local_type",
                    "create_time",
                    "content_sha256",
                )
            },
            native_content_base64=base64.b64encode(raw.encode()).decode(),
            decoded_text=raw,
            decoded_text_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        )
        plans = own.build_native_owner_notice_plans(
            connection, None, packet, native_system_roles=[updated]
        )
        assert len(plans) == 1
        assert "changed the group name" in plans[0]["message"]["text"]
        assert plans[0]["message"] == dict(
            zip(foreign._COLUMNS, bank._row(packet, message), strict=True)
        )


def test_native_owner_unknown_decode_backend_fails_closed(owner_scope, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise own.WCDBBackendUnavailableError("Synthetic missing backend")

    monkeypatch.setattr(own, "decode_wcdb_text_compatible", unavailable)
    assert _plans(owner_scope) == []


def test_closed_dispatcher_preserves_native_projection_and_rejects_unknown_strategy(
    owner_scope,
):
    _connection, conversation, _packet, _witness = owner_scope
    plan = _plans(owner_scope)[0]
    assert own.project_notice_plan(plan, conversation, [], plan["event"]) == {
        "message": plan["message"],
        "proof": plan["proof"],
    }
    changed = deepcopy(plan)
    changed["strategy"] = "arbitrary_priority_native_claim"
    with pytest.raises(ValueError):
        own.project_notice_plan(changed, conversation, [], changed["event"])


def test_changed_native_epoch_or_retained_projection_is_rejected(owner_scope):
    _connection, conversation, _packet, _witness = owner_scope
    for field in ("create_time", "message"):
        plan = deepcopy(_plans(owner_scope)[0])
        if field == "create_time":
            plan["event"]["create_time"] += 1
        else:
            plan["message"]["time_sort"] += 1
        with pytest.raises(ValueError):
            own.project_notice_plan(plan, conversation, [], plan["event"])
