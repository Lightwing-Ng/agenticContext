"""Group membership logs retain original system evidence and source navigation.

Code version: v1.2.0-codex.0
"""

from contextlib import closing
from datetime import datetime
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core import wechat_group_membership as membership
from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_completion_json_codec import encode_json_cell
from app.core.wechat_history_browser import query_wechat_history
from app.web.app import create_app
from tests.test_wechat_group_names import OTHER_ROOM, ROOM, _record
from tests.test_wechat_reconcile import merge, source


KEY = "group_membership_history"
INVITATION_JOIN = '"Inviter" invited "<b>New member</b>" to the group chat'
CHINESE_JOIN = '“甲”邀请“乙”加入了群聊'
DEPARTURE = '"Departing member" left the group chat'
REMOVAL = '"Admin" removed "Member" from the group chat'
SELF_DEPARTURE = "You've left the group chat"
QR_JOIN = '"New member" joined the group chat via the QR Code shared by "Member".'
GROUP_INVITE = "A Member invited New Member, Other Member to the group chat."
SEARCH_MARKER = "Unique transcript search marker"


def _template_notice(plain):
    return (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template>'
        f"<plain>{plain}</plain>"
        "</content_template></sysmsgtemplate></sysmsg>"
    )


def _session(store, account, *, kind="group", peer=ROOM):
    page = query_wechat_history(store, account=account, chat_kind=kind)
    return next(item for item in page.sessions if item.messaging["peer_id"] == peer)


def test_group_membership_follows_timed_native_notices_across_transcript_pages(tmp_path):
    primary = source()
    other = source("wxid_account_b")
    untimed = _record(primary, 20, '"Unknown time" left the group chat')
    untimed["timeline_timestamp_usable"] = False
    merge(tmp_path, [primary, other], [
        _record(primary, 10, INVITATION_JOIN),
        _record(primary, 11, ROOM + ":\n" + _template_notice(CHINESE_JOIN)),
        _record(primary, 12, DEPARTURE),
        _record(primary, 13, REMOVAL),
        _record(primary, 14, '"Pasted notice" left the group chat', system=False),
        _record(primary, 15, '"Inviter" invited you to join the group chat'),
        _record(
            primary, 16,
            '<sysmsg type="revokemsg"><revokemsg><replacemsg>'
            '"Revoked notice" left the group chat'
            "</replacemsg></revokemsg></sysmsg>",
        ),
        _record(
            primary, 17, OTHER_ROOM + ":\n" + _template_notice(CHINESE_JOIN),
        ),
        _record(primary, 18, REMOVAL, peer="wxid_peer"),
        _record(primary, 19, DEPARTURE, peer=OTHER_ROOM, system=False),
        untimed,
        _record(primary, 21, SEARCH_MARKER, system=False),
        _record(other, 22, '"Other account member" left the group chat'),
        _record(primary, 23, SELF_DEPARTURE),
        _record(primary, 24, QR_JOIN),
        _record(primary, 25, _template_notice(GROUP_INVITE)),
        _record(primary, 26, 'You have been removed from the group admins by group owner "A"'),
    ])
    path = wechat_archive_path(tmp_path)
    original = path.read_bytes()
    account = "account:" + primary["source_account_id"]
    session = _session(tmp_path, account)
    assert KEY not in session.messaging
    detail = query_wechat_history(
        tmp_path, account=account, session=session.stable_id, query=SEARCH_MARKER,
        page_size=1, persist_event_cache=False,
    )
    assert [item.content_text for item in detail.items] == [SEARCH_MARKER]
    history = detail.current_session.messaging[KEY]
    assert history["total"] == 7
    assert [(event["kind"], event["text"]) for event in history["events"]] == [
        ("joined", GROUP_INVITE),
        ("joined", QR_JOIN),
        ("left", SELF_DEPARTURE),
        ("removed", REMOVAL),
        ("left", DEPARTURE),
        ("joined", CHINESE_JOIN),
        ("joined", INVITATION_JOIN),
    ]
    newest, *_, oldest = history["events"]
    assert newest["timestamp_value"] == "2026-11-10T17:11:25+08:00"
    assert oldest["timestamp_label"] == "10 Nov 2026 17:11:10"
    assert len({event["message_id"] for event in history["events"]}) == 7

    other_room = _session(tmp_path, account, peer=OTHER_ROOM)
    empty = query_wechat_history(tmp_path, account=account, session=other_room.stable_id)
    assert empty.current_session.messaging[KEY]["total"] == 0
    assert empty.current_session.messaging[KEY]["events"] == ()
    direct = _session(tmp_path, account, kind="direct", peer="wxid_peer")
    direct_detail = query_wechat_history(tmp_path, account=account, session=direct.stable_id)
    assert KEY not in direct_detail.current_session.messaging
    other_account = "account:" + other["source_account_id"]
    other_session = _session(tmp_path, other_account)
    other_detail = query_wechat_history(tmp_path, account=other_account, session=other_session.stable_id)
    assert [event["text"] for event in other_detail.current_session.messaging[KEY]["events"]] == [
        '"Other account member" left the group chat'
    ]
    assert path.read_bytes() == original


def test_group_membership_disclosure_escapes_names_and_opens_the_exact_source(tmp_path):
    store = tmp_path / "store"
    primary = source()
    merge(store, [primary], [
        _record(primary, 10, INVITATION_JOIN),
        _record(primary, 11, SEARCH_MARKER, system=False),
    ])
    account = "account:" + primary["source_account_id"]
    session = _session(store, account)
    detail = query_wechat_history(store, account=account, session=session.stable_id)
    event, = detail.current_session.messaging[KEY]["events"]
    app = create_app(
        store,
        computer_use_settings_path=tmp_path / "agent.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.get("/browser", query_string={
        "view": "text", "source": "wechat", "session_view": "1", "chat_kind": "group",
        "account": account, "session": session.stable_id, "q": SEARCH_MARKER,
    })
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Group membership history" in html
    assert "&lt;b&gt;New member&lt;/b&gt;" in html
    assert "<b>New member</b>" not in html
    source_url = "/browser/wechat/messages/" + event["message_id"]
    assert f'href="{source_url}"' in html
    redirect = client.get(source_url)
    assert redirect.status_code == 302
    location = urlsplit(redirect.headers["Location"])
    query = parse_qs(location.query)
    assert location.path == "/browser"
    assert location.fragment == "wechat_message_" + event["message_id"]
    assert query["account"] == [account]
    assert query["session"] == [session.stable_id]


@pytest.mark.parametrize("copy_hash, original_ids, expected", [
    ("a" * 64, ("native",), ("native", "earlier")),
    ("b" * 64, ("native",), ("secondary", "native", "earlier")),
    ("a" * 64, ("native", "other-native"), ("secondary", "other-native", "native", "earlier")),
])
def test_membership_folds_only_the_proven_secondary_invitation_copy(
    monkeypatch, copy_hash, original_ids, expected
):
    owner, observer, session = "wxid_archive_owner", "wxid_other_viewer", "selected-room"
    strategy = "secondary_group_invitation_v1"
    epoch = 1793983105
    stamp = "2026-11-07T00:38:25+08:00"
    text = "Inviter invited New Member to the group chat"
    conversation = {
        "id": session, "peer_id": ROOM, "kind": "group", "account_id": owner,
        "account_key": "account:" + owner, "identity_status": "Verified archive ID",
    }
    common = {
        "text_extraction_kind": "system_text", "message_type": 10000,
        "system_message_subtype": "sysmsgtemplate", "unix_timestamp": epoch,
    }
    foreign = {
        "strategy": strategy, "target_conversation_id": session, "peer_id": ROOM,
        "source_viewer_account_id": observer, "server_id": "1000",
    }
    plan = {
        "proof": foreign,
        "event": {
            "content_sha256": copy_hash, "create_time": epoch, "local_type": 10000,
            "account_id": observer, "peer_id": ROOM, "server_id": "1000",
        },
    }
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ':memory:' AS secondary_completions")
        connection.execute(
            "CREATE TABLE secondary_completions.entries (id TEXT PRIMARY KEY, "
            "conversation_id TEXT, operation TEXT, strategy TEXT, plan BLOB)"
        )
        connection.execute(
            "INSERT INTO secondary_completions.entries VALUES (?, ?, ?, ?, ?)",
            ("secondary", session, "append", strategy, encode_json_cell(json.dumps(plan))),
        )
        connection.execute(
            "CREATE TABLE messages (id TEXT, conversation_id TEXT, ordinal INT, text TEXT, "
            "time_sort INT, timestamp_value TEXT, timestamp_label TEXT, provenance TEXT, quality_flags TEXT)"
        )
        rows = [
            ("earlier", session, 0, '"Earlier" left the group chat', epoch - 1,
             "2026-11-07T00:38:24+08:00", "Earlier", json.dumps(common), '["system_notice"]'),
        ]
        for index, row_id in enumerate(original_ids, start=1):
            server = str(index + 1000)
            native = {
                "content_sha256": "a" * 64, "create_time": epoch, "local_type": 10000,
                "account_id": owner, "peer_id": ROOM, "session_id": session,
                "conversation_kind": "group", "authenticated_pages": True,
                "integrity_check": "ok", "server_id": server,
            }
            proof = {**common, "server_id": server, "macos_recovery": native}
            rows.append((row_id, session, index, text, epoch, stamp, stamp, json.dumps(proof),
                         '["system_notice"]'))
        proof = {**common, "server_id": "1000", "secondary_source_invitation": foreign}
        rows.append(("secondary", session, 9, text, epoch, stamp, stamp, json.dumps(proof),
                     '["system_notice","secondary_source_invitation"]'))
        connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        monkeypatch.setattr(membership, "_MAX_EVENTS", 2)
        result = membership.group_membership_history(connection, "main.messages", conversation)

    assert result["total"] == len(expected)
    assert tuple(event["message_id"] for event in result["events"]) == expected[:2]
    assert result["events"][0].get("duplicate_message_ids", ()) == (
        ("secondary",) if expected[0] == "native" else ()
    )


@pytest.mark.parametrize("variation, expected_subjects", [
    ("exact", ("wxid_retained_invitee",)),
    ("digest mismatch", ()),
    ("source mismatch", ()),
    ("receipt mismatch", ()),
    ("conflicting subjects", ()),
    ("canonical subjects", ("wxid_canonical_invitee",)),
])
def test_membership_reuses_subjects_only_from_exact_retained_occurrences(variation, expected_subjects):
    owner, session, message_id = "wxid_archive_owner", "selected-room", "1" * 64
    stamp = "2026-11-07T00:38:25+08:00"
    epoch = int(datetime.fromisoformat(stamp).timestamp())
    text = "Inviter invited New Member to the group chat"
    scope = {"account_id": owner, "peer_id": ROOM, "session_id": session, "conversation_kind": "group"}
    conversation = {
        "id": session, "peer_id": ROOM, "kind": "group", "account_id": owner,
        "account_key": "account:" + owner, "identity_status": "Verified archive ID",
    }
    source = {
        "source_id": "retained-native-source", "database_relative_path": "db_storage/message/message_0.db",
        "table": "Msg_fixture", "authenticated_pages": True, "integrity_check": "ok",
    }
    coordinates = {
        "local_id": 17, "server_id": "1000", "local_type": 10000,
        "create_time": epoch, "content_sha256": "a" * 64, "real_sender_id": 0,
    }
    proof = {
        "text_extraction_kind": "system_text", "message_type": 10000,
        "system_message_subtype": "sysmsgtemplate", "unix_timestamp": epoch,
        "server_id": coordinates["server_id"], "macos_recovery": {**scope, **source, **coordinates},
    }
    fields = {
        "kind": "joined", "method": "invited", "actor_label": "Inviter",
        "subject_ids": ["wxid_retained_invitee"], "viewer_subject": False,
    }
    if variation == "canonical subjects":
        proof["system_membership"] = {**fields, "subject_ids": ["wxid_canonical_invitee"]}
    packet_source = dict(source)
    if variation == "source mismatch":
        packet_source["database_relative_path"] = "db_storage/message/message_1.db"
    occurrence_digest = "b" * 64 if variation == "digest mismatch" else coordinates["content_sha256"]
    candidates = [None, fields]
    if variation == "conflicting subjects":
        candidates.append({**fields, "subject_ids": ["wxid_conflicting_invitee"]})

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ':memory:' AS macos_recovery_bank")
        connection.executescript("""
            CREATE TABLE macos_recovery_bank.sources (
                id TEXT PRIMARY KEY, account_id TEXT, peer_id TEXT, source_id TEXT,
                snapshot_id TEXT, proof TEXT
            );
            CREATE TABLE macos_recovery_bank.occurrences (
                id TEXT PRIMARY KEY, source_id TEXT, conversation_id TEXT, local_id INTEGER,
                server_id TEXT, content_sha256 TEXT, canonical_id TEXT, canonical_store TEXT,
                signature TEXT, proof TEXT, UNIQUE(source_id, conversation_id, local_id)
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, conversation_id TEXT, ordinal INTEGER, text TEXT,
                time_sort INTEGER, timestamp_value TEXT, timestamp_label TEXT,
                provenance TEXT, quality_flags TEXT
            );
        """)
        connection.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            message_id, session, 1, text, epoch, stamp, "7 Nov 2026 00:38:25",
            json.dumps(proof), '["system_notice"]',
        ))
        for index, candidate in enumerate(candidates):
            source_key = f"snapshot-source-{index}"
            receipt_id = "retained-native-source" if index == 0 else "reissued-native-source"
            receipt_source = {**packet_source, "source_id": receipt_id}
            if variation == "receipt mismatch":
                receipt_source["source_id"] = "unrelated-native-source"
            connection.execute("INSERT INTO macos_recovery_bank.sources VALUES (?, ?, ?, ?, ?, ?)", (
                source_key, owner, ROOM, receipt_id, f"snapshot-{index}",
                encode_json_cell(json.dumps({**scope, "source": receipt_source})),
            ))
            projection = {"text": text}
            if candidate is not None:
                projection["system_membership"] = candidate
            message = {**coordinates, "text": text, "native_text": projection}
            connection.execute("INSERT INTO macos_recovery_bank.occurrences VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                f"occurrence-{index}", source_key, session, coordinates["local_id"],
                coordinates["server_id"], occurrence_digest, message_id, "macos_bank", "fixture",
                encode_json_cell(json.dumps(message)),
            ))
        history = membership.group_membership_history(connection, "main.messages", conversation)
        stored = connection.execute("SELECT provenance FROM messages WHERE id=?", (message_id,)).fetchone()

    event, = history["events"]
    assert history["total"] == 1
    assert (event["message_id"], event["method"], event["text"]) == (message_id, "invited", text)
    assert event["subject_ids"] == expected_subjects
    assert event["subject_label"] == "New Member"
    assert json.loads(stored[0]) == proof
