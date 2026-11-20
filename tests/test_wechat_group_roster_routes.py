"""Member snapshot actions resolve canonical scope and expose safe observations.

Code version: v1.5.0-codex.0
"""

from copy import deepcopy
from html import escape, unescape
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from werkzeug.datastructures import MultiDict

from app.core.config import CrawlConfig
from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_group_rosters import WechatGroupRosterRepository
from app.core.wechat_history_browser import query_wechat_history
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests.test_wechat_group_names import ROOM, _record
from tests.test_wechat_group_rosters import EARLY, LATE, snapshot
from tests.test_wechat_reconcile import merge, source


ENDPOINT = "/browser/wechat/group-roster"


@pytest.fixture
def roster_app(tmp_path, monkeypatch):
    store = tmp_path / "store"
    primary = source()
    owner = primary["source_account_id"]
    merge(store, [primary], [
        _record(primary, 10, "A synthetic group message", system=False),
        _record(primary, 11, "A synthetic private message", peer="wxid_private", system=False),
    ])
    overview = query_wechat_history(store, account="account:" + owner)
    group = next(item for item in overview.sessions if item.messaging["kind"] == "group")
    direct = next(item for item in overview.sessions if item.messaging["kind"] == "direct")
    state = {
        "snapshot": snapshot(("wxid_member", "<b>Member 😀</b>"), account=owner, peer=ROOM),
        "available": True,
        "failure": None,
        "calls": [],
    }

    def capture(account_id, peer_id, **_options):
        state["calls"].append((account_id, peer_id))
        if state["failure"]:
            raise state["failure"]
        return deepcopy(state["snapshot"])

    monkeypatch.setattr("app.core.wechat_group_roster_source.capture_group_roster", capture)
    monkeypatch.setattr(
        "app.core.wechat_group_roster_source.roster_source_available",
        lambda _account, **_options: state["available"],
    )
    monkeypatch.setattr(
        SavedConfigStore, "load", lambda *_args, **_kwargs: SavedConfigStore(CrawlConfig()),
    )
    app = create_app(
        store,
        computer_use_settings_path=tmp_path / "agent.json",
        computer_use_runtime_root=tmp_path / "runtime",
        agent_external_operations_enabled=False,
    )
    app.config.update(TESTING=True)
    return SimpleNamespace(
        app=app, client=app.test_client(), store=store, state=state,
        owner=owner, group=group.stable_id, direct=direct.stable_id,
        repository=WechatGroupRosterRepository(store),
    )


def _post(fixture, **options):
    return fixture.client.post(
        ENDPOINT, data={"session_id": fixture.group, "page": "7"}, **options,
    )


def _detail(fixture, **query):
    return fixture.client.get("/browser", query_string={
        "view": "text", "source": "wechat", "account": "account:" + fixture.owner,
        "chat_kind": "group", "session": fixture.group, **query,
    })


def _membership_template(template, **profiles):
    links = "".join(
        f'<link name="{name}" type="link_profile"><memberlist><member>'
        f"<username>{escape(username)}</username><nickname>{escape(label)}</nickname>"
        "</member></memberlist></link>"
        for name, (username, label) in profiles.items()
    )
    return (
        '<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
        '<content_template type="tmpl_type_profile"><plain/>'
        f"<template>{escape(template)}</template><link_list>{links}</link_list>"
        "</content_template></sysmsgtemplate></sysmsg>"
    )


def _membership_history(html):
    table, = re.findall(
        r'<table class="wechat-group-membership-history-table"[^>]*>(.*?)</table>',
        html, re.DOTALL,
    )
    assert re.findall(r'<th scope="col">([^<]+)</th>', table) == [
        "Change date", "Member", "Status",
    ]
    return table


def test_group_snapshot_post_uses_canonical_scope_and_preserves_return_page(roster_app):
    fixture = roster_app
    archive_before = wechat_archive_path(fixture.store).read_bytes()
    response = _post(fixture)
    assert response.status_code == 303
    assert fixture.state["calls"] == [(fixture.owner, ROOM)]
    location = urlsplit(response.headers["Location"])
    query = parse_qs(location.query)
    assert location.path == "/browser"
    assert location.fragment == "wechat_group_roster"
    for key, value in {
        "source": "wechat", "account": "account:" + fixture.owner,
        "chat_kind": "group", "session": fixture.group, "page": "7",
        "roster_result": "saved",
    }.items():
        assert query[key] == [value]
    result = fixture.repository.project(fixture.owner, ROOM)
    assert result["status"] == "ready"
    assert result["snapshot_count"] == 1
    assert result["current"]["member_count"] == 2
    assert result["changes"] == ()
    assert query["roster_snapshot"] == [result["current"]["id"]]
    assert wechat_archive_path(fixture.store).read_bytes() == archive_before


def test_get_renders_snapshot_summary_without_capturing_or_listing_members(roster_app):
    fixture = roster_app
    first_view = _detail(fixture)
    assert first_view.status_code == 200
    html = first_view.get_data(as_text=True)
    assert 'id="wechat_group_roster"' in html
    assert "Save member snapshot" not in html
    assert fixture.state["calls"] == []
    assert fixture.repository.project(fixture.owner, ROOM)["status"] == "absent"
    response = _post(fixture)
    saved_view = fixture.client.get(response.headers["Location"])
    assert saved_view.status_code == 200
    html = saved_view.get_data(as_text=True)
    assert "Baseline snapshot saved." in html
    assert f'1 saved snapshot · Latest <time datetime="{EARLY}">' in html
    assert "Members in latest snapshot" not in html
    assert "wechat-group-roster-members" not in html
    assert "Changes since previous snapshot" not in html
    assert "wxid_member" not in html
    assert "db_storage/contact/contact.db" not in html
    assert fixture.state["calls"] == [(fixture.owner, ROOM)]


def test_second_capture_renders_snapshot_evidence_in_the_single_history_table(roster_app):
    fixture = roster_app
    _post(fixture)
    fixture.state["snapshot"] = snapshot(
        ("wxid_new_member", "New member"), account=fixture.owner, peer=ROOM,
        observed_at=LATE,
    )
    response = _post(fixture, headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 303
    html = fixture.client.get(response.headers["Location"]).get_data(as_text=True)
    assert f'2 saved snapshots · Latest <time datetime="{LATE}">' in html
    assert "Members in latest snapshot" not in html
    assert "Changes since previous snapshot" not in html
    assert 'aria-label="Snapshot membership changes"' not in html
    observations = re.findall(
        r'<tr data-wechat-membership-observation="([^"]+)">(.*?)</tr>',
        _membership_history(html), re.DOTALL,
    )
    assert len(observations) == 2
    rows = dict(observations)
    assert rows.keys() == {"newly_listed", "no_longer_listed"}
    joined, left = rows["newly_listed"], rows["no_longer_listed"]
    assert "wxid_new_member" in joined and "Join method unknown" in joined
    assert "wxid_member" in left and "Departure reason unknown" in left
    for row in rows.values():
        assert f'<time datetime="{EARLY}">' in row and f'<time datetime="{LATE}">' in row
    assert "&lt;b&gt;Member 😀&lt;/b&gt;" in left
    assert "<b>Member 😀</b>" not in html
    result = fixture.repository.project(fixture.owner, ROOM)
    assert result["snapshot_count"] == 2
    assert {change["reason"] for change in result["changes"]} == {"unknown"}
    assert fixture.state["calls"] == [(fixture.owner, ROOM)] * 2


def test_membership_history_uses_scoped_notice_identities_and_links_to_evidence(roster_app):
    fixture = roster_app
    primary = source(fixture.owner)
    invited = ("wxid_invited", "Member with a changed nickname")
    qr_guest = ("wxid_qr_guest", "QR guest")
    voluntary = ("wxid_voluntary", "Departed member")
    removed = ("wxid_removed", "Removed member")
    archived = ("wxid_archived", "Earlier roster nickname")
    name_only = ("wxid_name_only", "Shared nickname")
    stale = ("wxid_stale", "Unexplained departure")
    inviter = ("wxid_inviter", "Inviter <b>nickname</b>")
    notices = [
        _record(primary, 18, _membership_template(
            "$username$ left the group chat.", username=(archived[0], "Old notice nickname"),
        )),
        _record(primary, 19, _membership_template(
            "$username$ left the group chat.", username=stale,
        )),
        _record(primary, 22, _membership_template(
            "$username$ invited $names$ to the group chat.",
            username=inviter, names=(invited[0], "Original nickname"),
        )),
        _record(primary, 23, _membership_template(
            "$username$ joined the group chat via the QR Code shared by $sharer$.",
            username=qr_guest, sharer=inviter,
        )),
        _record(primary, 24, _membership_template(
            "$username$ voluntarily left the group chat.", username=voluntary,
        )),
        _record(primary, 25, _membership_template(
            "$username$ removed $names$ from the group chat.",
            username=("wxid_admin", "Admin"), names=removed,
        )),
        _record(primary, 26, '"Shared nickname" left the group chat.'),
    ]
    merge(fixture.store, [primary], notices, snapshot="membership-status-notices")
    fixture.repository.save(snapshot(
        archived, account=fixture.owner, peer=ROOM, observed_at="2026-11-10T17:11:17+08:00",
    ))
    fixture.repository.save(snapshot(
        voluntary, removed, name_only, stale,
        account=fixture.owner, peer=ROOM, observed_at="2026-11-10T17:11:20+08:00",
    ))
    fixture.repository.save(snapshot(
        invited, qr_guest,
        account=fixture.owner, peer=ROOM, observed_at="2026-11-10T17:11:30+08:00",
    ))

    response = _detail(fixture)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    history = _membership_history(html)
    assert re.findall(r'<a[^>]*>([^<]+)</a></div>\s*<div class="wechat-source-label">([^<]+)</div>', history) == [
        (name_only[1], "Unknown"),
        (removed[1], removed[0]),
        (voluntary[1], voluntary[0]),
        (qr_guest[1], qr_guest[0]),
        (invited[1], invited[0]),
        (stale[1], stale[0]),
        (archived[1], archived[0]),
    ]
    observations = re.findall(
        r'<tr data-wechat-membership-observation="no_longer_listed">(.*?)</tr>',
        history, re.DOTALL,
    )
    for username, _ in (name_only, stale):
        row, = [row for row in observations if f">{username}</div>" in row]
        assert "Departure reason unknown" in row
        assert '/browser/wechat/messages/' not in row

    assert "<b>nickname</b>" not in html
    notice_rows = re.findall(r'<tr data-wechat-membership-event="[^"]+">(.*?)</tr>', history, re.DOTALL)
    for member, status in (
        (invited, "Invited by Inviter <b>nickname</b>"),
        (qr_guest, "Joined by QR code"),
        (voluntary, "Left voluntarily"),
        (removed, "Removed by Admin"),
    ):
        row, = [row for row in notice_rows if f">{member[0]}</div>" in row]
        _, _, status_cell = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.DOTALL)
        assert unescape(re.sub(r"<[^>]+>", "", status_cell)).strip() == status
        source_url = re.search(r'href="(/browser/wechat/messages/[^"]+)"', row).group(1)
        source_response = fixture.client.get(source_url)
        assert source_response.status_code == 302
        location = urlsplit(source_response.headers["Location"])
        assert parse_qs(location.query)["session"] == [fixture.group]


def test_membership_history_shows_departures_from_older_and_other_account_snapshots(roster_app):
    fixture = roster_app
    other = "wxid_other_roster_source"
    for account, username, name in (
        (fixture.owner, "wxid_current_departure", "Selected account departure"),
        (other, "wxid_other_departure", "Other account departure"),
    ):
        fixture.repository.save(snapshot(
            ("wxid_staying", "Staying"), (username, name), account=account, peer=ROOM,
            observed_at=EARLY,
        ))
        for stamp in (LATE, "2026-11-18T11:00:00+08:00"):
            fixture.repository.save(snapshot(
                ("wxid_staying", "Staying"), account=account, peer=ROOM, observed_at=stamp,
            ))

    response = _detail(fixture)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Changes since previous snapshot" not in html
    inferred = re.findall(
        r'<tr data-wechat-membership-observation="no_longer_listed">(.*?)</tr>',
        _membership_history(html), re.DOTALL,
    )
    assert len(inferred) == 2
    assert any("wxid_current_departure" in item for item in inferred)
    assert any("wxid_other_departure" in item and other in item for item in inferred)
    for item in inferred:
        assert "18 Nov 2026 09:00:00" in item and "18 Nov 2026 10:00:00" in item
        assert "No longer listed" in item and "Departure reason unknown" in item
        assert "Left voluntarily" not in item and "Removed" not in item


@pytest.mark.parametrize("headers,environ", [
    ({}, {"REMOTE_ADDR": "192.168.1.9"}),
    ({"Origin": "https://untrusted.invalid"}, {}),
    ({"Origin": "https://localhost"}, {}),
    ({"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"}, {}),
])
def test_remote_or_cross_origin_capture_is_rejected_before_source_access(roster_app, headers, environ):
    fixture = roster_app
    response = _post(fixture, headers=headers, environ_overrides=environ)
    assert response.status_code == 403
    assert fixture.state["calls"] == []
    assert not fixture.repository.path.exists()


@pytest.mark.parametrize("payload", [
    {},
    {"session_id": "not-a-session"},
    {"session_id": "wechat:" + "a" * 64, "account_id": "wxid_other"},
    {"session_id": "wechat:" + "a" * 64, "peer_id": "other@chatroom"},
    {"session_id": "wechat:" + "a" * 64, "page": "../1"},
    {"session_id": "wechat:" + "a" * 64, "page": "1.5"},
    MultiDict([("session_id", "wechat:" + "a" * 64), ("session_id", "wechat:" + "b" * 64)]),
    MultiDict([("session_id", "wechat:" + "a" * 64), ("page", "1"), ("page", "2")]),
])
def test_untrusted_payload_cannot_supply_an_account_or_group(roster_app, payload):
    fixture = roster_app
    response = fixture.client.post(ENDPOINT, data=payload)
    assert response.status_code == 400
    assert fixture.state["calls"] == []
    assert not fixture.repository.path.exists()


def test_private_or_missing_conversation_cannot_capture_a_group_roster(roster_app):
    fixture = roster_app
    private = fixture.client.post(ENDPOINT, data={"session_id": fixture.direct})
    assert private.status_code == 400
    missing = fixture.client.post(ENDPOINT, data={"session_id": "wechat:" + "f" * 64})
    assert missing.status_code == 404
    assert fixture.state["calls"] == []


def test_unavailable_source_leaves_no_snapshot_and_reports_safe_feedback(roster_app):
    fixture = roster_app
    fixture.state["available"] = False
    response = _post(fixture)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["Location"]).query)["roster_result"] == ["unavailable"]
    assert fixture.state["calls"] == []
    assert not fixture.repository.path.exists()
    html = fixture.client.get(response.headers["Location"]).get_data(as_text=True)
    assert "A verified local member source is unavailable for this account." in html


def test_failed_capture_preserves_saved_bytes_and_hides_native_error_details(roster_app):
    fixture = roster_app
    _post(fixture)
    before = fixture.repository.path.read_bytes()
    fixture.state["failure"] = RuntimeError("Native /private/contact.db key=synthetic-secret")
    response = _post(fixture)
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["Location"]).query)
    assert query["roster_result"] == ["failed"]
    assert fixture.repository.path.read_bytes() == before
    html = fixture.client.get(response.headers["Location"]).get_data(as_text=True)
    assert "The member snapshot could not be verified or saved." in html
    assert "/private/contact.db" not in html
    assert "synthetic-secret" not in html
    assert fixture.repository.project(fixture.owner, ROOM)["snapshot_count"] == 1


@pytest.mark.parametrize("mismatch", ["account", "room", "not_a_snapshot"])
def test_capture_result_cannot_escape_the_server_resolved_scope(roster_app, mismatch):
    fixture = roster_app
    _post(fixture)
    before = fixture.repository.path.read_bytes()
    if mismatch == "account":
        fixture.state["snapshot"] = snapshot(account="wxid_another_owner", peer=ROOM, observed_at=LATE)
    elif mismatch == "room":
        fixture.state["snapshot"] = snapshot(account=fixture.owner, peer="987654321@chatroom", observed_at=LATE)
    else:
        fixture.state["snapshot"] = None
    response = _post(fixture)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["Location"]).query)["roster_result"] == ["failed"]
    assert fixture.repository.path.read_bytes() == before
    assert fixture.repository.project(fixture.owner, ROOM)["snapshot_count"] == 1


def test_untrusted_success_query_cannot_claim_that_a_snapshot_was_saved(roster_app):
    fixture = roster_app
    _post(fixture)
    html = _detail(fixture, roster_result="saved", roster_snapshot="f" * 64).get_data(as_text=True)
    assert "Member snapshot saved. Differences describe observations" not in html
    assert fixture.state["calls"] == [(fixture.owner, ROOM)]
    assert fixture.repository.project(fixture.owner, ROOM)["current"]["observed_at"] == EARLY
