"""Viewer names project across native identities without rewriting source evidence.

Code version: v1.5.3-codex.0
"""

from __future__ import annotations

from collections import Counter
import csv
import hashlib
from html import unescape
from io import StringIO
import json
import re
import sqlite3
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

from app.core.chat_history_browser import (
    build_chat_history_markdown,
    query_chat_history,
)
from app.core.config import CrawlConfig
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.core.wechat_contacts import (
    canonical_contact_id,
    contact_observation,
    contact_registry_path,
    initialize_contact_registry,
)
from app.core.wechat_display_names import display_names_path
from app.web.app import create_app
from app.web.config_store import SavedConfigStore
from tests import test_sidebar_e2e
from tests.test_wechat_archive import make_archive


OWNER_A = "wxid_account_a"
OWNER_B = "wxid_account_b"
OTHER = "wxid_peer"
ALIAS = "Local <viewer> & 昵称"
RAW_SPEAKER = "Synthetic source phone label"
RAW_ACCOUNT = "Synthetic source account · ount_a"
NATIVE_OWNER_NAME = "Synthetic source account"
HTML_ONLY_OWNER = "wxid_html_only_owner"
HTML_ONLY_ACCOUNT = "Synthetic HTML owner · _owner"
HTML_VARIANT_TEXT = "A completion keeps a separate parent observation."
disposable_browser = test_sidebar_e2e.disposable_browser


def _source(owner):
    return "ios-" + hashlib.md5(owner.encode()).hexdigest()


def _session(store, owner):
    return next(
        session
        for session in query_chat_history(
            store, source="wechat", account=f"account:{owner}"
        ).sessions
        if session.messaging["kind"] == "direct"
    )


def _write_preference(store, contact_id, name=ALIAS):
    path = display_names_path(store)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "contacts": {contact_id: name} if name is not None else {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _set_hidden_sources(store, sources):
    path = display_names_path(store)
    preferences = json.loads(path.read_text(encoding="utf-8"))
    preferences["hidden_account_sources"] = sources
    path.write_text(json.dumps(preferences, ensure_ascii=False), encoding="utf-8")
    return path


def _query(fixture, owner):
    session = fixture.sessions[owner]
    return query_chat_history(
        fixture.store,
        source="wechat",
        account=f"account:{owner}",
        session=session.stable_id,
    )


@pytest.fixture
def viewer_archive(tmp_path):
    archive = make_archive(tmp_path / "archive", repeated=3)
    store = tmp_path / "store"
    import_wechat_archive(archive, store)
    sessions = {owner: _session(store, owner) for owner in (OWNER_A, OWNER_B)}
    export_sessions = {}
    history = wechat_archive_path(store)
    with sqlite3.connect(history) as connection:
        connection.execute(
            "UPDATE partitions SET account_label=? WHERE id=?",
            (RAW_ACCOUNT, _source(OWNER_A)),
        )
        connection.execute(
            "UPDATE messages SET sender_label=? WHERE sender_id=?",
            (RAW_SPEAKER, OWNER_A),
        )
        b_messages = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal",
                (sessions[OWNER_B].stable_id,),
            )
        ]
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=?,direction='received' WHERE id=?",
            (OWNER_A, RAW_SPEAKER, b_messages[0]),
        )
        connection.execute(
            "UPDATE messages SET sender_label=? WHERE id=?",
            (RAW_SPEAKER, b_messages[-1]),
        )
        unknown = connection.execute(
            "SELECT m.id,c.id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "WHERE c.partition_id='xlsx-2' ORDER BY m.ordinal LIMIT 1"
        ).fetchone()
        connection.execute(
            "UPDATE messages SET sender_id=?,sender_label=? WHERE id=?",
            (OWNER_A, RAW_SPEAKER, unknown[0]),
        )
        native_message, native_provenance = connection.execute(
            "SELECT id,provenance FROM messages WHERE conversation_id=? AND sender_id=?",
            (sessions[OWNER_A].stable_id, OWNER_A),
        ).fetchone()
        for ordinal, prefix in enumerate(("A", "Z"), 1):
            source = f"html-viewer-{ordinal}"
            session_id = "wechat:" + hashlib.sha256(source.encode()).hexdigest()
            message_id = (
                "wechat:" + hashlib.sha256((source + "-message").encode()).hexdigest()
            )
            raw_account = f"{prefix} HTML source account · ount_a"
            connection.execute(
                "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
                (
                    source,
                    f"account:{OWNER_A}",
                    OWNER_A,
                    raw_account,
                    "Corroborated export ID",
                    "HTML export",
                    f"synthetic-{ordinal}.html",
                    "",
                ),
            )
            connection.execute(
                "INSERT INTO conversations SELECT ?,?,account_key,peer_id,?,kind,1,"
                "first_sort,last_sort,first_time,last_time,latest_text "
                "FROM conversations WHERE id=?",
                (
                    session_id,
                    source,
                    f"HTML conversation {ordinal}",
                    sessions[OWNER_A].stable_id,
                ),
            )
            provenance = {**json.loads(native_provenance), "source_id": source}
            connection.execute(
                "INSERT INTO messages SELECT ?,?,ordinal,sender_id,sender_label,direction,"
                "text,time_sort,timestamp_value,timestamp_label,time_basis,?,quality_flags,search_text "
                "FROM messages WHERE id=?",
                (message_id, session_id, json.dumps(provenance), native_message),
            )
            export_sessions[session_id] = {
                "partition_id": source,
                "message_id": message_id,
                "raw_account": raw_account,
            }
    registry = contact_registry_path(store)
    contact_id = canonical_contact_id(OWNER_A, authority="native_account_owner")
    other_id = canonical_contact_id(OTHER, authority="native_contact_username")
    with sqlite3.connect(registry) as connection:
        initialize_contact_registry(connection)
        for identifier, username in ((contact_id, OWNER_A), (other_id, OTHER)):
            connection.execute(
                "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
                (identifier, "wechat", "", username, "wechat_account", "verified"),
            )
            connection.execute(
                "INSERT INTO contact_identifiers VALUES(?,?,?,?,?)",
                (
                    identifier,
                    "native_username",
                    username,
                    "",
                    "native_contact_username",
                ),
            )
        for source in (_source(OWNER_A), _source(OWNER_B)):
            for identifier, sender in ((contact_id, OWNER_A), (other_id, OTHER)):
                connection.execute(
                    "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
                    (source, sender, "", "", identifier, "native_message_sender", "{}"),
                )
        for owner, remark in (
            (OWNER_A, "Account A remark"),
            (OWNER_B, "Account B remark"),
        ):
            observation = contact_observation(
                contact_id,
                source_id=_source(owner),
                owner_account_id=owner,
                kind="remark",
                value=remark,
            )
            columns = tuple(observation)
            connection.execute(
                "INSERT INTO contact_observations ("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(observation[column] for column in columns),
            )
    _write_preference(store, contact_id)
    return SimpleNamespace(
        store=store,
        sessions=sessions,
        export_sessions=export_sessions,
        contact_id=contact_id,
        other_id=other_id,
        history=history,
        registry=registry,
        unknown_session=unknown[1],
        unknown_message=unknown[0],
        sandbox=tmp_path,
    )


def test_viewer_name_follows_canonical_identity_across_sources_without_data_changes(
    viewer_archive,
):
    fixture = viewer_archive
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    pages = [_query(fixture, owner) for owner in (OWNER_A, OWNER_B)]
    projected = [
        message
        for page in pages
        for message in page.items
        if message.messaging["sender_id"] == OWNER_A
    ]
    assert len(projected) == 2
    assert {message.messaging["contact"]["id"] for message in projected} == {
        fixture.contact_id
    }
    assert {message.messaging["viewer_display_name"] for message in projected} == {
        ALIAS
    }
    assert {message.author_label for message in projected} == {RAW_SPEAKER}
    assert {
        message.messaging["contact"]["source_display_name"] for message in projected
    } == {RAW_SPEAKER}
    assert {
        message.messaging["contact"]["preferred_name"] for message in projected
    } == {"Account A remark", "Account B remark"}
    for message in projected:
        assert message.messaging["contact"]["viewer_display_name"] == ALIAS
        assert message.messaging["sender_id"] == OWNER_A
    other = next(
        message for message in pages[1].items if message.messaging["sender_id"] == OTHER
    )
    assert other.author_label == RAW_SPEAKER
    assert other.messaging["contact"]["id"] == fixture.other_id
    assert not other.messaging.get("viewer_display_name")
    unknown = query_chat_history(
        fixture.store, source="wechat", session=fixture.unknown_session
    )
    unverified = next(
        message
        for message in unknown.items
        if message.stable_id == fixture.unknown_message
    )
    assert unverified.messaging["sender_id"] == OWNER_A
    assert unverified.author_label == RAW_SPEAKER
    assert unverified.messaging["contact"]["identity_status"] == "unresolved"
    assert not unverified.messaging.get("viewer_display_name")
    assert pages[0].current_session.messaging["account_label"] == RAW_ACCOUNT
    assert (
        pages[0].current_session.messaging["viewer_account_label"]
        == ALIAS + " · ount_a"
    )
    option = next(
        account
        for account in pages[0].messaging["account_options"]
        if account["key"] == f"account:{OWNER_A}"
    )
    assert option["name"] == RAW_ACCOUNT
    assert option["viewer_name"] == ALIAS + " · ount_a"
    assert option["partition_id"] == _source(OWNER_A)
    sources = [
        account
        for account in pages[0].messaging["accounts"]
        if account["key"] == f"account:{OWNER_A}"
    ]
    assert len(sources) == 3
    assert {source["viewer_name"] for source in sources} == {ALIAS + " · ount_a"}
    assert {source["name"] for source in sources} == {
        RAW_ACCOUNT,
        *(row["raw_account"] for row in fixture.export_sessions.values()),
    }
    assert len(pages[0].messaging["accounts"]) == 6
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_viewer_preference_changes_and_clear_are_visible_on_the_next_query(
    viewer_archive,
):
    fixture = viewer_archive
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())

    def self_message():
        return next(
            message
            for message in _query(fixture, OWNER_A).items
            if message.messaging["sender_id"] == OWNER_A
        )

    first = self_message()
    assert first.messaging["viewer_display_name"] == ALIAS
    _write_preference(fixture.store, fixture.contact_id, "Revised viewer")
    assert self_message().messaging["viewer_display_name"] == "Revised viewer"
    revised = _query(fixture, OWNER_A)
    assert {
        row["viewer_name"]
        for row in revised.messaging["accounts"]
        if row["key"] == f"account:{OWNER_A}"
    } == {"Revised viewer · ount_a"}
    assert first.messaging["viewer_display_name"] == ALIAS
    _write_preference(fixture.store, fixture.contact_id, None)
    assert self_message().messaging["viewer_display_name"] == NATIVE_OWNER_NAME
    assert (
        self_message().messaging["contact"]["viewer_display_name_source"]
        == "verified_native_account_alias"
    )
    cleared = _query(fixture, OWNER_A)
    raw_options = [
        row
        for row in cleared.messaging["account_options"]
        if row["key"] == f"account:{OWNER_A}"
    ]
    assert len(raw_options) == 1
    assert raw_options[0]["partition_id"] == _source(OWNER_A)
    assert raw_options[0]["name"] == RAW_ACCOUNT
    assert not any(row["viewer_name"] for row in raw_options)
    assert (
        sum(row["key"] == f"account:{OWNER_A}" for row in cleared.messaging["accounts"])
        == 3
    )
    display_names_path(fixture.store).unlink()
    assert self_message().author_label == RAW_SPEAKER
    assert self_message().messaging["viewer_display_name"] == NATIVE_OWNER_NAME
    assert not _query(fixture, OWNER_A).current_session.messaging[
        "viewer_account_label"
    ]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_native_owner_alias_unifies_raw_names_across_pages_search_and_export(
    viewer_archive,
):
    fixture = viewer_archive
    session = fixture.sessions[OWNER_A].stable_id
    raw_names = (OWNER_A, "Old source nickname", RAW_ACCOUNT)
    _write_preference(fixture.store, fixture.contact_id, None)
    with sqlite3.connect(fixture.history) as connection:
        identifiers = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY ordinal",
                (session,),
            )
        ]
        assert len(identifiers) == len(raw_names)
        for index, (identifier, label) in enumerate(zip(identifiers, raw_names)):
            text = f"Native owner sample {index}"
            connection.execute(
                "UPDATE messages SET sender_id=?,sender_label=?,direction='sent',"
                "text=?,search_text=? WHERE id=?",
                (OWNER_A, label, text, text.casefold(), identifier),
            )
    before = (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        display_names_path(fixture.store).read_bytes(),
    )
    detail = _query(fixture, OWNER_A)
    assert len(detail.items) == 3
    assert {row.author_label for row in detail.items} == set(raw_names)
    assert {row.messaging["viewer_display_name"] for row in detail.items} == {
        NATIVE_OWNER_NAME
    }
    assert {row.messaging["contact"]["id"] for row in detail.items} == {
        fixture.contact_id
    }
    for page in range(1, 4):
        paged = query_chat_history(
            fixture.store,
            source="wechat",
            account=f"account:{OWNER_A}",
            session=session,
            page=page,
            page_size=1,
        )
        assert len(paged.items) == 1
        assert paged.items[0].messaging["viewer_display_name"] == NATIVE_OWNER_NAME
    searched = query_chat_history(
        fixture.store,
        source="wechat",
        account=f"account:{OWNER_A}",
        session=session,
        query="Native owner sample 2",
    )
    assert len(searched.items) == 1
    assert searched.items[0].messaging["viewer_display_name"] == NATIVE_OWNER_NAME
    exported = build_chat_history_markdown(detail)
    for label in raw_names:
        assert f'- Source display name: "{label}"' in exported
    client = _application(fixture).test_client()
    html = client.get(
        "/browser",
        query_string={"source": "wechat", "session": session},
    ).get_data(as_text=True)
    names = re.findall(r'class="wechat-speaker[^\"]*"[^>]*>([^<]*)<', html)
    assert names == [NATIVE_OWNER_NAME] * 3
    assert before == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        display_names_path(fixture.store).read_bytes(),
    )


def _application(fixture):
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        app = create_app(
            fixture.store,
            computer_use_settings_path=fixture.sandbox / "agent.json",
            computer_use_runtime_root=fixture.sandbox / "runtime",
            agent_external_operations_enabled=False,
        )
    app.config.update(TESTING=True)
    return app


def _assert_session_csv(fixture, encoded):
    reader = csv.DictReader(StringIO(encoded.removeprefix("\ufeff")))
    assert reader.fieldnames == ["Time", "Speaker ID", "Statement"]
    rows = list(reader)
    session = fixture.sessions[OWNER_A]
    with sqlite3.connect(fixture.history) as connection:
        original = connection.execute(
            "SELECT sender_id,sender_label,text FROM messages WHERE conversation_id=?",
            (session.stable_id,),
        ).fetchall()
    assert len(rows) == len(original) == session.message_count
    assert {row["Time"] for row in rows} == {"2021-08-07 18:59:03 UTC+08:00"}
    expected = Counter(
        (f"{ALIAS if sender_id == OWNER_A else label} ({sender_id})", body)
        for sender_id, label, body in original
    )
    assert Counter((row["Speaker ID"], row["Statement"]) for row in rows) == expected
    assert any(row["Speaker ID"] == f"{ALIAS} ({OWNER_A})" for row in rows)


def test_viewer_names_render_as_text_and_exports_keep_source_evidence(viewer_archive):
    fixture = viewer_archive
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    client = _application(fixture).test_client()
    session = fixture.sessions[OWNER_A]
    query = {
        "view": "text",
        "source": "wechat",
        "account": f"account:{OWNER_A}",
        "chat_kind": "direct",
        "session": session.stable_id,
    }
    response = client.get("/browser", query_string=query)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Local &lt;viewer&gt; &amp; 昵称" in html
    assert "<viewer>" not in html
    assert "<dt>Source display name</dt><dd>" + RAW_SPEAKER + "</dd>" in html
    assert f'value="account:{OWNER_A}" selected' in html
    assert fixture.contact_id in html
    links = [unescape(value) for value in re.findall(r'href="([^"]+)"', html)]
    assert 'aria-label="Export conversation as CSV"' in html
    export_url = next(link for link in links if "/export.csv?" in link)
    assert urlsplit(export_url).path == (
        f"/browser/wechat/session/{session.stable_id}/export.csv"
    )
    assert parse_qs(urlsplit(export_url).query)["account"] == [f"account:{OWNER_A}"]
    assert parse_qs(urlsplit(export_url).query)["chat_kind"] == ["direct"]
    exported_csv = client.get(export_url)
    assert exported_csv.status_code == 200
    assert exported_csv.headers["Content-Type"].startswith("text/csv")
    _assert_session_csv(fixture, exported_csv.get_data(as_text=True))
    exported = client.get(
        f"/browser/session/{session.stable_id}/export",
        query_string={
            "source": "wechat",
            "account": f"account:{OWNER_A}",
            "chat_kind": "direct",
        },
    )
    assert exported.status_code == 200
    assert exported.mimetype == "text/markdown"
    markdown = exported.get_data(as_text=True)
    assert f"- Source account: {RAW_ACCOUNT}" in markdown
    assert f'- Viewer account label: "{ALIAS} · ount_a"' in markdown
    assert f'- Source display name: "{RAW_SPEAKER}"' in markdown
    assert f"- Sender ID: {OWNER_A}" in markdown
    assert f"- Contact ID: {fixture.contact_id}" in markdown
    assert f". {ALIAS} · " in markdown
    assert build_chat_history_markdown(_query(fixture, OWNER_A)) == markdown
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_corroborated_sources_share_only_viewer_labels_and_keep_export_provenance(
    viewer_archive,
):
    fixture = viewer_archive
    preference = display_names_path(fixture.store)
    before = (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )
    account = f"account:{OWNER_A}"
    index = query_chat_history(fixture.store, source="wechat", account=account)
    assert len(index.sessions) == 4
    assert len({session.stable_id for session in index.sessions}) == 4
    assert {session.messaging["partition_id"] for session in index.sessions} == {
        _source(OWNER_A),
        *(row["partition_id"] for row in fixture.export_sessions.values()),
    }
    assert {
        session.messaging["viewer_account_label"] for session in index.sessions
    } == {ALIAS + " · ount_a"}
    assert sum(row["key"] == account for row in index.messaging["accounts"]) == 3
    assert sum(row["key"] == account for row in index.messaging["account_options"]) == 1
    client = _application(fixture).test_client()
    response = client.get("/browser", query_string={"view": "text", "source": "wechat"})
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert html.count(f'<option value="{account}"') == 1
    assert re.search(r"Account sources</span>\s*<strong>6</strong>", html)
    for session_id, origin in fixture.export_sessions.items():
        page = query_chat_history(
            fixture.store, source="wechat", account=account, session=session_id
        )
        context = page.current_session.messaging
        assert page.current_session.stable_id == session_id
        assert context["account_key"] == account
        assert context["account_id"] == OWNER_A
        assert context["partition_id"] == origin["partition_id"]
        assert context["identity_status"] == "Corroborated export ID"
        assert context["account_label"] == origin["raw_account"]
        assert context["viewer_account_label"] == ALIAS + " · ount_a"
        assert page.items[0].stable_id == origin["message_id"]
        assert page.items[0].author_label == RAW_SPEAKER
        assert page.items[0].messaging["sender_id"] == OWNER_A
        assert (
            page.items[0].messaging["sender_identifier_namespace"]
            == "unverified_export_field"
        )
        assert not page.items[0].messaging.get("viewer_display_name")
        detail = client.get(
            "/browser",
            query_string={
                "source": "wechat",
                "account": account,
                "session": session_id,
            },
        )
        assert detail.status_code == 200
        assert "Local &lt;viewer&gt; &amp; 昵称 · ount_a" in detail.get_data(
            as_text=True
        )
        exported = client.get(
            f"/browser/session/{session_id}/export",
            query_string={"source": "wechat", "account": account},
        )
        assert exported.status_code == 200
        markdown = exported.get_data(as_text=True)
        assert f"- Source account: {origin['raw_account']}" in markdown
        assert f'- Viewer account label: "{ALIAS} · ount_a"' in markdown
        assert f"- Original export identifier: {OWNER_A}" in markdown
        assert f'- Source display name: "{RAW_SPEAKER}"' in markdown
    assert before == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )


@pytest.mark.parametrize("preference_state", ["missing", "empty"])
def test_trusted_accounts_group_without_aliases_and_keep_all_source_records(
    viewer_archive, preference_state
):
    fixture = viewer_archive
    preference = display_names_path(fixture.store)
    if preference_state == "missing":
        preference.unlink()
    else:
        _write_preference(fixture.store, fixture.contact_id, None)
    with sqlite3.connect(fixture.history) as connection:
        # Identical raw names under different verified owners never establish identity.
        connection.execute(
            "UPDATE partitions SET account_label=? WHERE id=?",
            (RAW_ACCOUNT, _source(OWNER_B)),
        )
        expected_sessions = set(
            connection.execute(
                "SELECT id,partition_id,account_key,message_count FROM conversations"
            )
        )
        expected_messages = connection.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]
    before = (fixture.history.read_bytes(), fixture.registry.read_bytes())
    preference_before = preference.read_bytes() if preference.exists() else None
    page = query_chat_history(fixture.store, source="wechat")
    assert {
        (
            row.stable_id,
            row.messaging["partition_id"],
            row.messaging["account_key"],
            row.message_count,
        )
        for row in page.sessions
    } == expected_sessions
    assert sum(row.message_count for row in page.sessions) == expected_messages
    assert page.messaging["message_count"] == expected_messages
    assert len(page.messaging["accounts"]) == 6
    account = f"account:{OWNER_A}"
    sources = [row for row in page.messaging["accounts"] if row["key"] == account]
    assert len(sources) == 3
    assert len({row["partition_id"] for row in sources}) == 3
    assert {row["name"] for row in sources} == {
        RAW_ACCOUNT,
        *(row["raw_account"] for row in fixture.export_sessions.values()),
    }
    options = [
        row for row in page.messaging["account_options"] if row["key"] == account
    ]
    assert len(options) == 1
    assert options[0]["partition_id"] == _source(OWNER_A)
    assert options[0]["name"] == RAW_ACCOUNT
    assert not options[0]["viewer_name"]
    same_names = [
        row for row in page.messaging["account_options"] if row["name"] == RAW_ACCOUNT
    ]
    assert {row["key"] for row in same_names} == {account, f"account:{OWNER_B}"}
    client = _application(fixture).test_client()
    rendered = client.get("/browser", query_string={"view": "text", "source": "wechat"})
    assert rendered.status_code == 200
    html = rendered.get_data(as_text=True)
    assert re.findall(rf'<option value="{account}"[^>]*>(.*?)</option>', html) == [
        RAW_ACCOUNT
    ]
    assert re.search(r"Account sources</span>\s*<strong>6</strong>", html)
    for session_id, origin in fixture.export_sessions.items():
        detail = query_chat_history(
            fixture.store, source="wechat", account=account, session=session_id
        )
        assert (
            detail.current_session.messaging["account_label"] == origin["raw_account"]
        )
        assert not detail.current_session.messaging["viewer_account_label"]
        assert detail.items[0].stable_id == origin["message_id"]
        exported = client.get(
            f"/browser/session/{session_id}/export",
            query_string={"source": "wechat", "account": account},
        )
        assert exported.status_code == 200
        markdown = exported.get_data(as_text=True)
        assert f"- Source account: {origin['raw_account']}" in markdown
        assert f"- Source account ID: {OWNER_A}" in markdown
        assert f"- Original export identifier: {OWNER_A}" in markdown
        assert "- Viewer account label:" not in markdown
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
    assert (
        preference.read_bytes() if preference.exists() else None
    ) == preference_before


@pytest.mark.parametrize(
    ("mutation", "option_count"),
    [
        ("missing_native_anchor", 2),
        ("unconfirmed_export", 2),
        ("wrong_export_owner", 2),
    ],
)
def test_account_options_do_not_group_sources_without_exact_owner_proof(
    viewer_archive, mutation, option_count
):
    fixture = viewer_archive
    _write_preference(fixture.store, fixture.contact_id, None)
    source = next(iter(fixture.export_sessions.values()))["partition_id"]
    with sqlite3.connect(fixture.history) as connection:
        if mutation == "missing_native_anchor":
            connection.execute(
                "UPDATE partitions SET identity_status='Source account unconfirmed' WHERE id=?",
                (_source(OWNER_A),),
            )
        elif mutation == "unconfirmed_export":
            connection.execute(
                "UPDATE partitions SET identity_status='Source account unconfirmed' WHERE id=?",
                (source,),
            )
        else:
            connection.execute(
                "UPDATE partitions SET account_id=? WHERE id=?", (OWNER_B, source)
            )
    preference = display_names_path(fixture.store)
    before = (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )
    page = query_chat_history(fixture.store, source="wechat")
    account = f"account:{OWNER_A}"
    assert sum(row["key"] == account for row in page.messaging["accounts"]) == 3
    assert (
        sum(row["key"] == account for row in page.messaging["account_options"])
        == option_count
    )
    assert not any(row["viewer_name"] for row in page.messaging["accounts"])
    assert before == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )


def _prepare_html_only_owner(fixture):
    """Keep both export partitions and a completion variant under one exact owner."""
    exports = list(fixture.export_sessions.items())
    with sqlite3.connect(fixture.history) as connection:
        for session_id, origin in exports:
            connection.execute(
                "UPDATE partitions SET account_key=?,account_id=?,account_label=? WHERE id=?",
                (
                    f"account:{HTML_ONLY_OWNER}",
                    HTML_ONLY_OWNER,
                    HTML_ONLY_ACCOUNT,
                    origin["partition_id"],
                ),
            )
            connection.execute(
                "UPDATE conversations SET account_key=? WHERE id=?",
                (f"account:{HTML_ONLY_OWNER}", session_id),
            )
            origin["raw_account"] = HTML_ONLY_ACCOUNT
            origin["source_file"] = connection.execute(
                "SELECT source_file FROM partitions WHERE id=?",
                (origin["partition_id"],),
            ).fetchone()[0]
        session_id, completion = exports[-1]
        fixture.html_variant_id = hashlib.sha256(b"html-completion-variant").hexdigest()
        connection.execute(
            "INSERT INTO messages SELECT ?,conversation_id,ordinal+1,sender_id,sender_label,"
            "direction,?,time_sort,timestamp_value,timestamp_label,time_basis,provenance,"
            "quality_flags,? FROM messages WHERE id=?",
            (
                fixture.html_variant_id,
                HTML_VARIANT_TEXT,
                HTML_VARIANT_TEXT,
                completion["message_id"],
            ),
        )
        connection.execute(
            "UPDATE conversations SET message_count=message_count+1 WHERE id=?",
            (session_id,),
        )
    path = display_names_path(fixture.store)
    preferences = json.loads(path.read_text(encoding="utf-8"))
    identifier = canonical_contact_id(
        HTML_ONLY_OWNER, authority="corroborated_export_username"
    )
    preferences["contacts"][identifier] = "A native-only preference must stay unused"
    path.write_text(json.dumps(preferences, ensure_ascii=False), encoding="utf-8")
    return fixture


def test_corroborated_only_options_keep_sources_variants_and_exports(viewer_archive):
    fixture = _prepare_html_only_owner(viewer_archive)
    preference = display_names_path(fixture.store)
    before = (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )
    account = f"account:{HTML_ONLY_OWNER}"
    page = query_chat_history(fixture.store, source="wechat", account=account)
    assert len(page.messaging["accounts"]) == 6
    sources = [row for row in page.messaging["accounts"] if row["key"] == account]
    assert len(sources) == 2
    assert {row["partition_id"] for row in sources} == {
        origin["partition_id"] for origin in fixture.export_sessions.values()
    }
    options = [
        row for row in page.messaging["account_options"] if row["key"] == account
    ]
    assert len(options) == 1
    assert options[0]["partition_id"] == sources[0]["partition_id"] == "html-viewer-1"
    assert options[0]["name"] == HTML_ONLY_ACCOUNT
    assert options[0]["identity_status"] == "Corroborated export ID"
    assert not options[0]["viewer_name"]
    assert not any(row["viewer_name"] for row in sources)
    assert len(page.sessions) == 2
    assert (
        sum(row.message_count for row in page.sessions)
        == page.messaging["message_count"]
        == 3
    )
    assert {session.stable_id for session in page.sessions} == set(
        fixture.export_sessions
    )
    client = _application(fixture).test_client()
    rendered = client.get("/browser", query_string={"view": "text", "source": "wechat"})
    html = rendered.get_data(as_text=True)
    assert rendered.status_code == 200
    assert re.findall(rf'<option value="{account}"[^>]*>(.*?)</option>', html) == [
        HTML_ONLY_ACCOUNT
    ]
    assert re.search(r"Account sources</span>\s*<strong>6</strong>", html)
    message_ids = set()
    for session_id, origin in fixture.export_sessions.items():
        detail = query_chat_history(
            fixture.store, source="wechat", account=account, session=session_id
        )
        context = detail.current_session.messaging
        assert context["account_id"] == HTML_ONLY_OWNER
        assert context["account_key"] == account
        assert context["account_label"] == HTML_ONLY_ACCOUNT
        assert context["partition_id"] == origin["partition_id"]
        assert context["identity_status"] == "Corroborated export ID"
        assert not context["viewer_account_label"]
        assert all(row.author_label == RAW_SPEAKER for row in detail.items)
        assert all(
            row.messaging["sender_identifier_namespace"] == "unverified_export_field"
            for row in detail.items
        )
        message_ids.update(row.stable_id for row in detail.items)
        exported = client.get(
            f"/browser/session/{session_id}/export",
            query_string={"source": "wechat", "account": account},
        )
        assert exported.status_code == 200
        markdown = exported.get_data(as_text=True)
        assert f"- Source account: {HTML_ONLY_ACCOUNT}" in markdown
        assert f"- Source account ID: {HTML_ONLY_OWNER}" in markdown
        assert f"- Archive: HTML export / {origin['source_file']}" in markdown
        assert f"- Original export identifier: {OWNER_A}" in markdown
        assert "- Viewer account label:" not in markdown
        if origin["partition_id"] == "html-viewer-2":
            assert HTML_VARIANT_TEXT in markdown
    assert message_ids == {
        fixture.html_variant_id,
        *(origin["message_id"] for origin in fixture.export_sessions.values()),
    }
    assert before == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )


def test_hidden_account_option_preserves_old_links_search_exports_and_raw_records(
    viewer_archive,
):
    fixture = viewer_archive
    source = "xlsx-2"
    account = "source:" + source
    baseline = query_chat_history(fixture.store, source="wechat")
    baseline_detail = query_chat_history(
        fixture.store, source="wechat", account=account, session=fixture.unknown_session
    )
    expected_export = build_chat_history_markdown(baseline_detail)
    preference = _set_hidden_sources(fixture.store, [source])
    contacts = json.loads(preference.read_text(encoding="utf-8"))["contacts"]
    before = (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )
    page = query_chat_history(fixture.store, source="wechat")
    assert page.messaging["accounts"] == baseline.messaging["accounts"]
    assert page.messaging["message_count"] == baseline.messaging["message_count"]
    assert {row.stable_id for row in page.sessions} == {
        row.stable_id for row in baseline.sessions
    }
    assert not any(row["key"] == account for row in page.messaging["account_options"])
    searched = query_chat_history(fixture.store, source="wechat", query="你好")
    assert fixture.unknown_session in {row.stable_id for row in searched.sessions}
    selected = query_chat_history(fixture.store, source="wechat", account=account)
    assert (
        selected.messaging["message_count"]
        == baseline_detail.current_session.message_count
    )
    assert [row.stable_id for row in selected.sessions] == [fixture.unknown_session]
    detail = query_chat_history(
        fixture.store, source="wechat", account=account, session=fixture.unknown_session
    )
    assert detail.current_session.messaging["account_id"] is None
    assert (
        detail.current_session.messaging["identity_status"]
        == "Source account unconfirmed"
    )
    assert detail.current_session.messaging["partition_id"] == source
    assert [(row.stable_id, row.content_text) for row in detail.items] == [
        (row.stable_id, row.content_text) for row in baseline_detail.items
    ]
    assert build_chat_history_markdown(detail) == expected_export
    client = _application(fixture).test_client()
    normal_html = client.get(
        "/browser", query_string={"view": "text", "source": "wechat"}
    ).get_data(as_text=True)
    assert f'<option value="{account}"' not in normal_html
    assert re.search(r"Account sources</span>\s*<strong>6</strong>", normal_html)
    for extra in ({}, {"session": fixture.unknown_session}):
        response = client.get(
            "/browser",
            query_string={
                "view": "text",
                "source": "wechat",
                "account": account,
                **extra,
            },
        )
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        option = re.search(rf'<option value="{account}"([^>]*)>', html)
        assert option is not None
        assert "hidden" in option[1] and "selected" in option[1]
        assert "disabled" not in option[1]
        assert html.count(f'<option value="{account}"') == 1
        assert "Unavailable source account" not in html
        assert re.search(r"Account sources</span>\s*<strong>1</strong>", html)
    exported = client.get(
        f"/browser/session/{fixture.unknown_session}/export",
        query_string={"source": "wechat", "account": account},
    )
    assert exported.status_code == 200
    assert exported.get_data(as_text=True) == expected_export
    unavailable = client.get(
        "/browser",
        query_string={"view": "text", "source": "wechat", "account": "source:missing"},
    ).get_data(as_text=True)
    assert (
        '<option value="source:missing" selected>Unavailable source account</option>'
        in unavailable
    )
    assert before == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )
    assert not fixture.history.with_name("exclusions.json").exists()
    _set_hidden_sources(fixture.store, [])
    restored = query_chat_history(fixture.store, source="wechat")
    assert (
        restored.messaging["account_options"] == baseline.messaging["account_options"]
    )
    assert json.loads(preference.read_text(encoding="utf-8"))["contacts"] == contacts
    assert before[:2] == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("hidden_source", [_source(OWNER_A), "html-viewer-1"])
def test_hiding_one_partition_keeps_a_shared_account_selectable(
    viewer_archive, hidden_source
):
    fixture = viewer_archive
    preference = _set_hidden_sources(fixture.store, [hidden_source])
    before = (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )
    account = f"account:{OWNER_A}"
    page = query_chat_history(fixture.store, source="wechat", account=account)
    assert sum(row["key"] == account for row in page.messaging["accounts"]) == 3
    options = [
        row for row in page.messaging["account_options"] if row["key"] == account
    ]
    assert len(options) == 1
    assert options[0]["partition_id"] != hidden_source
    assert options[0]["viewer_name"] == ALIAS + " · ount_a"
    assert len(page.sessions) == 4
    html = (
        _application(fixture)
        .test_client()
        .get(
            "/browser",
            query_string={"view": "text", "source": "wechat", "account": account},
        )
        .get_data(as_text=True)
    )
    options = re.findall(rf'<option value="{account}"([^>]*)>', html)
    assert len(options) == 1
    assert "selected" in options[0]
    assert "hidden" not in options[0] and "disabled" not in options[0]
    assert re.search(r"Account sources</span>\s*<strong>3</strong>", html)
    assert before == (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )


@pytest.fixture
def viewer_server(viewer_archive, monkeypatch):
    home = viewer_archive.sandbox / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    app = _application(viewer_archive)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            base_url=f"http://127.0.0.1:{server.server_port}", fixture=viewer_archive
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def html_only_server(viewer_server):
    _prepare_html_only_owner(viewer_server.fixture)
    return viewer_server


@pytest.mark.parametrize(("width", "color_scheme"), [(1280, "light"), (390, "dark")])
def test_browser_hidden_source_preserves_active_value_and_skips_menu_navigation(
    disposable_browser, viewer_server, width, color_scheme
):
    fixture = viewer_server.fixture
    account = "source:xlsx-2"
    preference = _set_hidden_sources(fixture.store, ["xlsx-2"])
    before = (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    page = context.new_page()
    try:
        page.goto(
            viewer_server.base_url + "/browser?view=text&source=wechat",
            wait_until="networkidle",
        )
        native = page.locator("#browser_wechat_account")
        expect(native.locator(f'option[value="{account}"]')).to_have_count(0)
        page.goto(
            viewer_server.base_url
            + "/browser?"
            + urlencode({"view": "text", "source": "wechat", "account": account}),
            wait_until="networkidle",
        )
        expect(native).to_have_value(account)
        current = native.locator(f'option[value="{account}"]')
        assert current.evaluate(
            "option => option.hidden && option.selected && !option.disabled"
        )
        assert (
            native.evaluate("select => new FormData(select.form).get('account')")
            == account
        )
        if (
            width < 600
            and page.locator("#sidebar_toggle").get_attribute("aria-expanded") != "true"
        ):
            page.locator("#sidebar_toggle").click()
        shell = native.locator("xpath=..")
        trigger = shell.locator("[data-browser-filter-select-trigger]")
        trigger.press("Home")
        hidden = shell.locator(f'[data-browser-filter-select-option="{account}"]')
        expect(hidden).to_be_hidden()
        assert (
            page.evaluate("document.activeElement.dataset.browserFilterSelectOption")
            != account
        )
        page.keyboard.press("End")
        assert (
            page.evaluate("document.activeElement.dataset.browserFilterSelectOption")
            != account
        )
        page.keyboard.press("Escape")
        expect(native).to_have_value(account)
        native.evaluate("select => window.SHARED_SELECT_AUTO.refresh(select)")
        trigger.click()
        expect(hidden).to_be_hidden()
        page.keyboard.press("Escape")
        page.get_by_role(
            "button", name=re.compile(r"^Filter by conversation type:")
        ).click()
        page.get_by_role("option", name="Unclassified", exact=True).click()
        expect(page).to_have_url(re.compile(r"account=(?:source%3A|source:)xlsx-2"))
        page.wait_for_load_state("networkidle")
        assert parse_qs(urlsplit(page.url).query)["account"] == [account]
        expect(native).to_have_value(account)
        assert native.locator(f'option[value="{account}"]').evaluate(
            "option => option.hidden && option.selected && !option.disabled"
        )
        assert (
            native.evaluate("select => new FormData(select.form).get('account')")
            == account
        )
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
        assert before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
            preference.read_bytes(),
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_browser_corroborated_only_account_select_keeps_completion_sources(
    disposable_browser, html_only_server, width, color_scheme
):
    fixture = html_only_server.fixture
    preference = display_names_path(fixture.store)
    before = (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )
    account = f"account:{HTML_ONLY_OWNER}"
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(
            html_only_server.base_url
            + "/browser?"
            + urlencode({"view": "text", "source": "wechat"}),
            wait_until="networkidle",
        )
        options = page.locator(f'#browser_wechat_account option[value="{account}"]')
        expect(options).to_have_count(1)
        expect(options).to_have_text(HTML_ONLY_ACCOUNT)
        source_total = (
            page.locator(".browser-text-metric-grid .metric-card")
            .filter(has=page.get_by_text("Account sources", exact=True))
            .locator("strong")
        )
        expect(source_total).to_have_text("6")
        if (
            width < 600
            and page.locator("#sidebar_toggle").get_attribute("aria-expanded") != "true"
        ):
            page.locator("#sidebar_toggle").click()
        page.get_by_role(
            "button", name=re.compile(r"^Filter by WeChat account:")
        ).click()
        page.get_by_role("option", name=HTML_ONLY_ACCOUNT, exact=True).click()
        expect(page).to_have_url(
            re.compile(r"account=(?:account%3A|account:)wxid_html_only_owner")
        )
        page.wait_for_load_state("networkidle")
        expect(page.locator("#browser_wechat_account")).to_have_value(account)
        expect(source_total).to_have_text("2")
        if (
            width < 600
            and page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true"
        ):
            page.locator("#sidebar_toggle").click()
        expect(page.locator(".browser-session-table tbody tr")).to_have_count(2)
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
        for session_id, origin in fixture.export_sessions.items():
            page.goto(
                html_only_server.base_url
                + "/browser?"
                + urlencode(
                    {"source": "wechat", "account": account, "session": session_id}
                ),
                wait_until="networkidle",
            )
            expect(page.locator(".wechat-conversation-labels")).to_have_text(
                "Private chat"
            )
            expect(page.locator(".wechat-conversation-labels > *")).to_have_count(1)
            source_account = page.locator(".wechat-provenance dt").filter(
                has_text=re.compile(r"^Source account$")
            )
            assert source_account.count() > 0
            assert (
                page.get_by_text(
                    "A native-only preference must stay unused", exact=False
                ).count()
                == 0
            )
            expect(
                page.locator(f'#browser_wechat_account option[value="{account}"]')
            ).to_have_count(1)
            expected_messages = 2 if origin["partition_id"] == "html-viewer-2" else 1
            expect(page.locator("article.wechat-message")).to_have_count(
                expected_messages
            )
            if expected_messages == 2:
                expect(
                    page.locator(
                        f"#wechat_message_{fixture.html_variant_id} .wechat-message-text"
                    )
                ).to_have_text(HTML_VARIANT_TEXT)
            assert (
                page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
            )
        assert errors == []
        assert before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
            preference.read_bytes(),
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_browser_viewer_names_keep_native_filters_and_compact_source_details(
    disposable_browser, viewer_server, width, color_scheme
):
    fixture = viewer_server.fixture
    preference = display_names_path(fixture.store)
    before = (
        fixture.history.read_bytes(),
        fixture.registry.read_bytes(),
        preference.read_bytes(),
    )
    context = disposable_browser.new_context(
        viewport={"width": width, "height": 900},
        reduced_motion="reduce",
        color_scheme=color_scheme,
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(
            viewer_server.base_url
            + "/browser?"
            + urlencode({"view": "text", "source": "wechat", "chat_kind": "direct"}),
            wait_until="networkidle",
        )
        expect(
            page.locator(f'#browser_wechat_account option[value="account:{OWNER_A}"]')
        ).to_have_count(1)
        source_total = (
            page.locator(".browser-text-metric-grid .metric-card")
            .filter(has=page.get_by_text("Account sources", exact=True))
            .locator("strong")
        )
        expect(source_total).to_have_text("6")
        if (
            width < 600
            and page.locator("#sidebar_toggle").get_attribute("aria-expanded") != "true"
        ):
            page.locator("#sidebar_toggle").click()
        page.get_by_role(
            "button", name=re.compile(r"^Filter by WeChat account:")
        ).click()
        page.get_by_role("option", name=ALIAS + " · ount_a", exact=True).click()
        expect(page).to_have_url(
            re.compile(r"account=(?:account%3A|account:)wxid_account_a")
        )
        page.wait_for_load_state("networkidle")
        assert parse_qs(urlsplit(page.url).query)["account"] == [f"account:{OWNER_A}"]
        assert "Local" not in page.url
        expect(page.locator("#browser_wechat_account")).to_have_value(
            f"account:{OWNER_A}"
        )
        if (
            width < 600
            and page.locator("#sidebar_toggle").get_attribute("aria-expanded") == "true"
        ):
            page.locator("#sidebar_toggle").click()
        page.get_by_role("link", name="Same title", exact=True).click()
        page.evaluate("document.fonts.ready")
        assert parse_qs(urlsplit(page.url).query)["account"] == [f"account:{OWNER_A}"]
        expect(page.locator(".wechat-conversation-labels")).to_have_text("Private chat")
        expect(page.locator(".wechat-conversation-labels > *")).to_have_count(1)
        target = page.locator(".wechat-message--sent")
        expect(target).to_have_count(1)
        expect(target.locator(".wechat-speaker")).to_have_text(ALIAS)
        assert page.locator("viewer").count() == 0
        assert page.locator('img[src*="invalid.example"]').count() == 0
        details = target.locator("details.wechat-provenance")
        assert details.get_attribute("open") is None
        current_url = page.url
        # Contact names navigate; the native summary still opens source details.
        details.locator("summary").press("Enter")
        raw_name = details.locator("dt").filter(
            has_text=re.compile(r"^Source display name$")
        )
        expect(raw_name.locator("xpath=following-sibling::dd[1]")).to_have_text(
            RAW_SPEAKER
        )
        sender = details.locator("dt").filter(has_text=re.compile(r"^Sender ID$"))
        expect(sender.locator("xpath=following-sibling::dd[1]")).to_have_text(OWNER_A)
        assert page.url == current_url
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
        assert page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches") == (
            color_scheme == "dark"
        )
        export_link = page.get_by_role(
            "link", name="Export conversation as CSV", exact=True
        ).get_attribute("href")
        assert parse_qs(urlsplit(export_link).query)["account"] == [
            f"account:{OWNER_A}"
        ]
        assert parse_qs(urlsplit(export_link).query)["chat_kind"] == ["direct"]
        assert urlsplit(export_link).path == (
            f"/browser/wechat/session/{fixture.sessions[OWNER_A].stable_id}/export.csv"
        )
        exported_csv = page.request.get(viewer_server.base_url + export_link)
        assert exported_csv.status == 200
        assert exported_csv.headers["content-type"].startswith("text/csv")
        _assert_session_csv(fixture, exported_csv.text())
        markdown_url = (
            f"/browser/session/{fixture.sessions[OWNER_A].stable_id}/export?"
            + urlencode(
                {
                    "source": "wechat",
                    "account": f"account:{OWNER_A}",
                    "chat_kind": "direct",
                }
            )
        )
        exported = page.request.get(viewer_server.base_url + markdown_url)
        assert exported.status == 200
        assert exported.headers["content-type"].startswith("text/markdown")
        markdown = exported.text()
        assert f"- Source account: {RAW_ACCOUNT}" in markdown
        assert f'- Viewer account label: "{ALIAS} · ount_a"' in markdown
        assert f'- Source display name: "{RAW_SPEAKER}"' in markdown
        assert f"- Sender ID: {OWNER_A}" in markdown
        assert f"- Contact ID: {fixture.contact_id}" in markdown
        export_session = next(iter(fixture.export_sessions))
        page.goto(
            viewer_server.base_url
            + "/browser?"
            + urlencode(
                {
                    "view": "text",
                    "source": "wechat",
                    "account": f"account:{OWNER_A}",
                    "session": export_session,
                }
            ),
            wait_until="networkidle",
        )
        expect(page.locator(".wechat-conversation-labels")).to_have_text("Private chat")
        expect(page.locator(".wechat-conversation-labels > *")).to_have_count(1)
        assert (
            page.locator(".wechat-provenance dt")
            .filter(has_text=re.compile(r"^Source account$"))
            .count()
            > 0
        )
        expect(
            page.locator(f'#browser_wechat_account option[value="account:{OWNER_A}"]')
        ).to_have_count(1)
        assert parse_qs(urlsplit(page.url).query)["account"] == [f"account:{OWNER_A}"]
        assert page.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1
        assert errors == []
        assert before == (
            fixture.history.read_bytes(),
            fixture.registry.read_bytes(),
            preference.read_bytes(),
        )
    finally:
        context.close()
