"""Known WeChat notice links display as text without rewriting source evidence.

Code version: v1.3.1-codex.0
"""

from __future__ import annotations

import csv
from io import StringIO
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.config import CrawlConfig
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_history_browser import (
    _pdf_message_presentation,
    _system_notice_display_text,
)
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests.test_wechat_archive import make_archive
from tests.test_wechat_reconcile import merge, record, source
from tests.test_wechat_system_profile_templates import _admin_xml, _remark_xml
from tests.wechat_browser_markup import get_verified_browser_page


HREF = "weixin://findfriend/verifycontact"
LABEL = "[Send a friend request]"
PREFIX = "Contact has enabled Friend Confirmation. "
SUFFIX = " to chat."
ANCHOR = f'<a href="{HREF}">{LABEL}</a>'
RAW_NOTICE = PREFIX + ANCHOR + SUFFIX
DISPLAY_NOTICE = PREFIX + LABEL + SUFFIX
STATUS = "${wxid_example@textstatusicon}"
TEMPLATE_ROOM = "123@chatroom"
TEMPLATE_XML = (
    '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
    '<content_template type="tmpl_type_profile"><plain/>'
    "<template>$username$ invited you to a group chat with $others$</template>"
    '<link_list><link name="username" type="link_profile"><memberlist>'
    "<member><username>wxid_example</username><nickname>Inviter</nickname></member>"
    '</memberlist></link><link name="others" type="link_plain">'
    "<plain>Member A, Member B</plain></link></link_list>"
    "</content_template></sysmsgtemplate></sysmsg>"
)
TEMPLATE_RAW = TEMPLATE_ROOM + ":\n" + TEMPLATE_XML
TEMPLATE_DISPLAY = "Inviter invited you to a group chat with Member A, Member B"
REMARK_RAW = TEMPLATE_ROOM + ":\n" + _remark_xml()
REMARK_DISPLAY = '"Source member" changed the group name to ""'
ADMIN_RAW = TEMPLATE_ROOM + ":\n" + _admin_xml()
ADMIN_DISPLAY = 'Group owner "Source owner" has set you as group admin View Details'


@pytest.mark.parametrize(
    "anchor",
    [
        ANCHOR,
        f"<a href='{HREF}'>{LABEL}</a>",
        f'<A HREF="{HREF}">{LABEL}</A>',
        f'<a\thref = "{HREF}" >{LABEL}</a >',
    ],
)
def test_recognized_internal_notice_anchor_preserves_its_label(anchor):
    assert _system_notice_display_text(PREFIX + anchor + SUFFIX) == DISPLAY_NOTICE


def test_known_notice_link_cleanup_preserves_surrounding_literal_text():
    raw = "  " + RAW_NOTICE + "\nTrailing  whitespace  "
    assert _system_notice_display_text(raw) == (
        "  " + DISPLAY_NOTICE + "\nTrailing  whitespace  "
    )


def test_notice_prefix_and_status_decoration_cleanup_compose_with_link_text():
    raw = "System Message: Contact" + STATUS + RAW_NOTICE[len("Contact") :]
    assert _system_notice_display_text(raw) == DISPLAY_NOTICE


@pytest.mark.parametrize(
    "anchor",
    [
        '<a href="https://example.invalid/verify">[Send a friend request]</a>',
        '<a href="javascript:alert(1)">[Send a friend request]</a>',
        '<a href="weixin://findfriend/other">[Send a friend request]</a>',
        f'<a href="{HREF}?unexpected=1">{LABEL}</a>',
        f'<a href="{HREF}">{LABEL}',
        f'<a href="{HREF}>{LABEL}</a>',
        f'<a href="{HREF}"><strong>{LABEL}</strong></a>',
        f'&lt;a href="{HREF}"&gt;{LABEL}&lt;/a&gt;',
        f'<a href="{HREF}" href="https://example.invalid">{LABEL}</a>',
        f'<a href="{HREF}" href="{HREF}">{LABEL}</a>',
        f'<a href="{HREF}"/>{LABEL}</a>',
    ],
)
def test_unknown_escaped_or_malformed_link_markup_remains_literal(anchor):
    raw = PREFIX + anchor + SUFFIX
    assert _system_notice_display_text(raw) == raw


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_pdf_notice_markup_cleanup_keeps_original_source_offsets(ending):
    body = "Human discussion of " + ANCHOR
    original_notice = "System Message: " + RAW_NOTICE
    raw = body + ending + original_notice + ending
    result = _pdf_message_presentation(raw, ("pdf_text_layer",))
    assert result["display_text"] == body
    assert len(result["system_notices"]) == 1
    notice = result["system_notices"][0]
    assert notice["text"] == DISPLAY_NOTICE
    assert raw[notice["source_start"] : notice["source_end"]] == (
        original_notice + ending
    )


@pytest.mark.parametrize("source_kind", ["native", "export_prefixed", "pdf"])
def test_notice_markup_projection_keeps_human_literals_raw_records_and_exports(
    tmp_path, source_kind
):
    archive = make_archive(tmp_path / "archive", repeated=2)
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    session = next(
        item
        for item in query_chat_history(store, source="wechat").sessions
        if (source_kind == "pdf" and item.messaging["partition_id"] == "xlsx-2")
        or (
            source_kind != "pdf"
            and item.messaging["account_key"] == "account:wxid_account_a"
            and item.messaging["kind"] == "direct"
        )
    )
    database = wechat_archive_path(store)
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT id,provenance FROM messages WHERE conversation_id=? ORDER BY ordinal",
            (session.stable_id,),
        ).fetchall()
        identifier, encoded = rows[0]
        provenance = json.loads(encoded)
        if source_kind == "pdf":
            raw = "Human prose\nSystem Message: " + RAW_NOTICE
            flags = ["pdf_text_layer"]
        else:
            raw = "System Message: " if source_kind == "export_prefixed" else ""
            raw += RAW_NOTICE
            flags = ["system_notice"]
            provenance.update(text_extraction_kind="system_text", message_type=10000)
        connection.execute(
            "UPDATE messages SET text=?,quality_flags=?,provenance=? WHERE id=?",
            (raw, json.dumps(flags), json.dumps(provenance), identifier),
        )
        ordinary_id = rows[1][0]
        ordinary_text = "A literal code example: " + RAW_NOTICE
        connection.execute(
            "UPDATE messages SET text=?,sender_label='System',quality_flags=? WHERE id=?",
            (ordinary_text, json.dumps(["system_notice"]), ordinary_id),
        )
        original_rows = {
            row[0]: row
            for row in connection.execute(
                "SELECT id,sender_id,sender_label,direction,provenance,timestamp_value "
                "FROM messages WHERE conversation_id=?",
                (session.stable_id,),
            )
        }
    before = database.read_bytes()
    page = query_chat_history(store, source="wechat", session=session.stable_id)
    message = next(item for item in page.items if item.stable_id == identifier)
    assert message.content_text == raw
    assert message.author_label == original_rows[identifier][2]
    assert message.role == original_rows[identifier][3]
    assert message.messaging["sender_id"] == original_rows[identifier][1]
    assert message.messaging["provenance"] == json.loads(original_rows[identifier][4])
    assert message.last_seen_at == original_rows[identifier][5]
    if source_kind == "pdf":
        assert message.messaging["display_text"] == "Human prose"
        assert message.messaging["system_notices"][0]["text"] == DISPLAY_NOTICE
    else:
        assert message.messaging["is_system_notice"]
        assert message.messaging["display_text"] == DISPLAY_NOTICE
    ordinary = next(item for item in page.items if item.stable_id == ordinary_id)
    assert not ordinary.messaging["is_system_notice"]
    assert ordinary.content_text == ordinary.messaging["display_text"] == ordinary_text
    assert raw in build_chat_history_markdown(page)
    assert ordinary_text in build_chat_history_markdown(page)
    assert len(page.items) == session.message_count
    assert database.read_bytes() == before


def test_decoded_known_link_label_is_escaped_by_the_actual_notice_template(tmp_path):
    archive = make_archive(tmp_path / "archive")
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    database = wechat_archive_path(store)
    label = "&lt;script&gt;window.noticeExecuted = true&lt;/script&gt; &amp; label"
    raw = PREFIX + f'<a href="{HREF}">{label}</a>' + SUFFIX
    display = PREFIX + "<script>window.noticeExecuted = true</script> & label" + SUFFIX
    assert _system_notice_display_text(raw) == display
    with sqlite3.connect(database) as connection:
        identifier, session, encoded = connection.execute(
            "SELECT m.id,m.conversation_id,m.provenance FROM messages m "
            "JOIN conversations c ON c.id=m.conversation_id "
            "WHERE c.account_key='account:wxid_account_a' AND c.kind='direct' "
            "ORDER BY m.ordinal LIMIT 1"
        ).fetchone()
        provenance = json.loads(encoded)
        provenance.update(text_extraction_kind="system_text", message_type=10000)
        connection.execute(
            "UPDATE messages SET text=?,provenance=? WHERE id=?",
            (raw, json.dumps(provenance), identifier),
        )
    before = database.read_bytes()
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            store,
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    response = get_verified_browser_page(
        app.test_client(),
        "/browser",
        query_string={"view": "text", "source": "wechat", "session": session},
    )
    assert response.status_code == 200
    rendered = response.get_data(as_text=True)
    assert (
        "&lt;script&gt;window.noticeExecuted = true&lt;/script&gt; &amp; label"
        in rendered
    )
    assert "<script>window.noticeExecuted = true</script>" not in rendered
    assert f'<a href="{HREF}">' not in rendered
    assert database.read_bytes() == before


def _system_template_fixture(tmp_path, *, notice_text=TEMPLATE_RAW):
    store = tmp_path / "store"
    metadata = source()
    observations = [
        record(
            metadata,
            "template-notice",
            peer=TEMPLATE_ROOM,
            text=notice_text,
            message_type=10000,
            text_extraction_kind="system_text",
            sender_id="",
            sender_display_name="System",
            direction="unknown",
            flags=["system_notice"],
        ),
        record(
            metadata,
            "ordinary-xml-example",
            peer=TEMPLATE_ROOM,
            text=notice_text,
            server_id="9223372036854775805",
        ),
        record(
            metadata,
            "wrong-room-notice",
            peer=TEMPLATE_ROOM,
            text="999@chatroom:\n" + TEMPLATE_XML,
            message_type=10002,
            text_extraction_kind="system_text",
            sender_id="",
            sender_display_name="System",
            direction="unknown",
            server_id="9223372036854775804",
            flags=["system_notice"],
        ),
    ]
    merge(store, [metadata], observations)
    page = query_chat_history(store, source="wechat")
    session = page.sessions[0].stable_id
    database = wechat_archive_path(store)
    with sqlite3.connect(database) as connection:
        identifiers = {
            json.loads(encoded)["record_id"]: identifier
            for identifier, encoded in connection.execute(
                "SELECT id,provenance FROM messages"
            )
        }
    preserved = database.read_bytes()
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            store,
            computer_use_settings_path=tmp_path / "agent.json",
            computer_use_runtime_root=tmp_path / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    return SimpleNamespace(
        app=app,
        store=store,
        database=database,
        preserved=preserved,
        session=session,
        identifiers=identifiers,
        observations=observations,
    )


@pytest.mark.parametrize("prefix", ["", TEMPLATE_ROOM + ":\n"])
def test_known_system_template_projects_only_readable_display_fields(prefix):
    assert (
        _system_notice_display_text(prefix + TEMPLATE_XML, peer_id=TEMPLATE_ROOM)
        == TEMPLATE_DISPLAY
    )


def test_system_template_from_another_room_is_not_partially_rendered():
    raw = "999@chatroom:\n" + TEMPLATE_XML
    assert _system_notice_display_text(raw, peer_id=TEMPLATE_ROOM) == raw


@pytest.mark.parametrize(
    "notice_text,display",
    [
        (TEMPLATE_RAW, TEMPLATE_DISPLAY),
        (REMARK_RAW, REMARK_DISPLAY),
        (ADMIN_RAW, ADMIN_DISPLAY),
    ],
)
def test_system_template_projection_preserves_native_records_and_raw_exports(
    tmp_path, notice_text, display
):
    fixture = _system_template_fixture(tmp_path, notice_text=notice_text)
    original_observations = json.dumps(fixture.observations, sort_keys=True)
    page = query_chat_history(
        fixture.store,
        source="wechat",
        account="account:wxid_account_a",
        session=fixture.session,
    )
    messages = {item.stable_id: item for item in page.items}
    notice = messages[fixture.identifiers["template-notice"]]
    assert notice.messaging["is_system_notice"]
    assert notice.messaging["display_text"] == display
    assert notice.content_text == notice_text
    ordinary = messages[fixture.identifiers["ordinary-xml-example"]]
    assert not ordinary.messaging["is_system_notice"]
    assert ordinary.content_text == ordinary.messaging["display_text"] == notice_text
    wrong_room = messages[fixture.identifiers["wrong-room-notice"]]
    assert wrong_room.messaging["is_system_notice"]
    assert wrong_room.messaging["display_text"] == wrong_room.content_text
    assert wrong_room.content_text == "999@chatroom:\n" + TEMPLATE_XML
    assert len(page.items) == 3
    markdown = build_chat_history_markdown(page)
    assert markdown.count(notice_text) == 2
    assert wrong_room.content_text in markdown
    with fixture.app.test_client() as client:
        response = client.get(
            f"/browser/wechat/session/{fixture.session}/export.csv",
            query_string={"account": "account:wxid_account_a", "chat_kind": "group"},
        )
    assert response.status_code == 200
    reader = csv.DictReader(StringIO(response.data.decode("utf-8-sig"), newline=""))
    assert reader.fieldnames == ["Time", "Speaker ID", "Statement"]
    exported = [row["Statement"] for row in reader]
    assert sorted(exported) == sorted(
        observation["text"] for observation in fixture.observations
    )
    assert json.dumps(fixture.observations, sort_keys=True) == original_observations
    assert fixture.database.read_bytes() == fixture.preserved


def test_system_template_member_label_is_escaped_by_the_actual_notice_template(
    tmp_path,
):
    label = "<script>window.systemTemplateExecuted = true</script>"
    raw = TEMPLATE_RAW.replace(
        "<nickname>Inviter</nickname>",
        "<nickname>&lt;script&gt;window.systemTemplateExecuted = true&lt;/script&gt;</nickname>",
    )
    fixture = _system_template_fixture(tmp_path, notice_text=raw)
    with fixture.app.test_client() as client:
        response = get_verified_browser_page(
            client,
            "/browser",
            query_string={
                "view": "text",
                "source": "wechat",
                "account": "account:wxid_account_a",
                "session": fixture.session,
            },
        )
    assert response.status_code == 200
    rendered = response.get_data(as_text=True)
    assert (
        "&lt;script&gt;window.systemTemplateExecuted = true&lt;/script&gt; "
        "invited you to a group chat with Member A, Member B" in rendered
    )
    assert label not in rendered
    assert fixture.database.read_bytes() == fixture.preserved


@pytest.mark.parametrize("field", ["owner", "action"])
def test_admin_notice_profile_and_action_title_remain_inert_in_the_actual_template(
    tmp_path, field
):
    label = '<script>window.adminNoticeExecuted = true</script> & literal'
    xml = _admin_xml(
        nickname=label if field == "owner" else "Source owner",
        title=label if field == "action" else "View Details",
    )
    raw = TEMPLATE_ROOM + ":\n" + xml
    fixture = _system_template_fixture(tmp_path, notice_text=raw)
    with fixture.app.test_client() as client:
        response = get_verified_browser_page(
            client,
            "/browser",
            query_string={
                "view": "text",
                "source": "wechat",
                "account": "account:wxid_account_a",
                "session": fixture.session,
            },
        )
    assert response.status_code == 200
    rendered = response.get_data(as_text=True)
    assert "&lt;script&gt;window.adminNoticeExecuted = true&lt;/script&gt; &amp; literal" in rendered
    assert label not in rendered
    assert '<a href="javascript:' not in rendered
    assert fixture.database.read_bytes() == fixture.preserved
