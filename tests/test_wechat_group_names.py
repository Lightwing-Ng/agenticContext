"""Archived group renames follow the original conversation across saved views.

Code version: v1.0.0-codex.0
"""

from datetime import datetime

from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_history_browser import query_wechat_history
from tests.test_wechat_reconcile import merge, record, source


ROOM = "123456@chatroom"
OTHER_ROOM = "789012@chatroom"
CURRENT_TITLE = 'Zulu <b> 👩‍👩‍👦'
ARCHIVED_TITLE = "Alpha import name"


def _record(
    metadata, number, text, *, peer=ROOM, title=ARCHIVED_TITLE, system=True,
    second=None,
):
    second = number if second is None else second
    stamp = f"2026-11-10T09:11:{second:02d}+00:00"
    return record(
        metadata,
        f"occurrence-{number}",
        peer=peer,
        conversation_display_name=title,
        text=text,
        message_type=10000 if system else 1,
        text_extraction_kind="system_text" if system else "plain_text",
        sender_id="" if system else "wxid_peer",
        sender_display_name="System Message" if system else "Member",
        direction="unknown" if system else "received",
        flags=["system_notice"] if system else [],
        server_id=str(500 + number),
        sent_at_utc_iso=stamp,
        unix_timestamp=int(datetime.fromisoformat(stamp).timestamp()),
    )


def test_group_names_follow_timed_notices_in_list_sort_search_and_detail(tmp_path):
    primary = source()
    other = source("wxid_account_b")
    xml = (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template>'
        '<plain>"Member" changed the group name to "Zulu &lt;b&gt; 👩‍👩‍👦"</plain>'
        "</content_template></sysmsgtemplate></sysmsg>"
    )
    old_notice = 'You changed the group name to "First observed name"'
    merge(tmp_path, [primary, other], [
        _record(primary, 10, old_notice),
        _record(primary, 11, old_notice, second=10),
        _record(primary, 12, '「成員」已將群組名稱變更為「中期名稱」'),
        _record(primary, 13, ROOM + ":\n" + xml),
        _record(primary, 14, OTHER_ROOM + ":\n" + xml.replace("Zulu", "Wrong room")),
        _record(primary, 15, 'You changed the group name to "Member’s copy"', system=False),
        _record(
            primary, 16,
            '<sysmsg type="revokemsg"><revokemsg><replacemsg>'
            'You changed the group name to "Revoked copy"'
            "</replacemsg></revokemsg></sysmsg>",
        ),
        _record(primary, 17, "Other group message", peer=OTHER_ROOM, title="Middle group", system=False),
        _record(primary, 18, 'You changed the group name to "Peer text"', peer="wxid_peer"),
        _record(other, 19, 'You changed the group name to "Other account observation"'),
    ])
    path = wechat_archive_path(tmp_path)
    original = path.read_bytes()
    account = "account:" + primary["source_account_id"]
    page = query_wechat_history(tmp_path, account=account, chat_kind="group", sort="name", page_size=1)
    assert page.sessions[0].conversation_title == "Middle group"
    page = query_wechat_history(tmp_path, account=account, chat_kind="group", sort="name", page_size=1, page=2)
    session = page.sessions[0]
    assert session.conversation_title == CURRENT_TITLE
    search = query_wechat_history(tmp_path, account=account, query="First observed name", sort="name")
    assert [(item.stable_id, item.conversation_title) for item in search.sessions] == [
        (session.stable_id, CURRENT_TITLE)
    ]
    detail = query_wechat_history(tmp_path, account=account, session=session.stable_id, page_size=1)
    assert detail.current_session.conversation_title == CURRENT_TITLE
    assert detail.current_session.stable_id == session.stable_id
    assert len(detail.items) == 1
    history = detail.current_session.messaging["group_name_history"]
    assert [change["title"] for change in history["changes"]] == [
        CURRENT_TITLE, "中期名稱", "First observed name",
    ]
    assert history["changes"][0]["timestamp_value"] == "2026-11-10T17:11:13+08:00"
    same_peer = query_wechat_history(tmp_path, account="account:wxid_account_b", chat_kind="group")
    assert same_peer.sessions[0].conversation_title == "Other account observation"
    assert same_peer.sessions[0].stable_id != session.stable_id
    direct = query_wechat_history(tmp_path, account=account, chat_kind="direct")
    assert direct.sessions[0].conversation_title == ARCHIVED_TITLE
    assert path.read_bytes() == original


def test_tied_rename_notices_preserve_both_observations_without_choosing_a_title(tmp_path):
    metadata = source()
    merge(tmp_path, [metadata], [
        _record(metadata, 10, 'You changed the group name to "First name"'),
        _record(metadata, 11, 'You changed the group name to "Other name"', second=10),
    ])
    overview = query_wechat_history(tmp_path, chat_kind="group")
    session = overview.sessions[0]
    assert session.conversation_title == ARCHIVED_TITLE
    detail = query_wechat_history(tmp_path, session=session.stable_id)
    assert detail.current_session.conversation_title == ARCHIVED_TITLE
    history = detail.current_session.messaging["group_name_history"]
    assert history["title"] == ""
    assert {item["title"] for item in history["changes"]} == {"First name", "Other name"}
