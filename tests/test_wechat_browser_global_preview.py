"""Keep conversation navigation browsable while saved views are verified.

Code version: v1.0.4-codex.0
"""

from contextlib import closing
import sqlite3
from threading import Event

import pytest
from bs4 import BeautifulSoup

from app.core.wechat_archive import WechatArchiveRepository, _file_identity
from app.core import wechat_browser_snapshot
from app.core.wechat_contacts import initialize_contact_registry
from app.core.wechat_secondary_completions import FILENAME
from tests import test_wechat_session_csv


session_app = test_wechat_session_csv.session_app


@pytest.mark.parametrize("change", ("completion", "reader"))
def test_pending_all_accounts_use_authenticated_summaries(session_app, monkeypatch, change):
    app, root = session_app
    repository = WechatArchiveRepository(root)
    contacts = repository.path.with_name("contacts.sqlite3")
    with closing(sqlite3.connect(contacts)) as connection, connection:
        initialize_contact_registry(connection)
    with closing(sqlite3.connect(repository.path)) as connection:
        snapshot = wechat_browser_snapshot.load_conversation_snapshot(
            repository, connection, _file_identity(repository.path)
        )
        assert snapshot is not None
        expected_counts = dict(connection.execute(
            f"SELECT id,message_count FROM {snapshot[0]}"
        ))
    if change == "completion":
        with closing(sqlite3.connect(repository.path.with_name(FILENAME))) as connection, connection:
            connection.execute("CREATE TABLE completion_fixture (value TEXT)")
    else:
        monkeypatch.setattr(wechat_browser_snapshot, "_code_fingerprint", lambda: "reader-update")

    entered, release = Event(), Event()
    service = app.extensions["wechat_browser_read"]
    monkeypatch.setattr(service, "loader", lambda _: None)

    def hold_validation(_parameters):
        entered.set()
        assert release.wait(4)

    monkeypatch.setattr(service, "validator", hold_validation)
    try:
        response = app.test_client().get("/browser", query_string={
            "view": "text", "source": "wechat", "session_view": "1",
        })
        assert response.status_code == 200 and entered.wait(1)
        document = BeautifulSoup(response.data, "html.parser")
        pending = document.select_one("[data-wechat-read-pending]")
        assert pending["data-state"] == "running"
        preview = document.select_one("[data-wechat-read-preview]")
        assert preview is not None
        rows = preview.select("tr[data-chat-session-id]")
        assert len(rows) == 6
        assert {row["data-wechat-account"] for row in rows} == {
            "account:wxid_account_a", "account:wxid_account_b", "source:xlsx-2", "source:xlsx-3",
        }
        assert "last verified snapshot" in preview.get_text(" ", strip=True)
        assert {
            row["data-chat-session-id"]: row.select_one("td:last-child").get_text(strip=True)
            for row in rows
        } == {key: f"{value:,}" for key, value in expected_counts.items()}
        assert not document.select("[data-chat-message-id]")
    finally:
        release.set()
        app.extensions["runtime_shutdown"]()


@pytest.mark.parametrize("snapshot_available", (True, False))
def test_pending_account_distinguishes_empty_snapshot_from_missing_snapshot(
    session_app, monkeypatch, snapshot_available,
):
    app, root = session_app
    repository = WechatArchiveRepository(root)
    contacts = repository.path.with_name("contacts.sqlite3")
    with closing(sqlite3.connect(contacts)) as connection, connection:
        initialize_contact_registry(connection)
    if snapshot_available:
        with closing(sqlite3.connect(repository.path)) as connection:
            assert wechat_browser_snapshot.load_conversation_snapshot(
                repository, connection, _file_identity(repository.path)
            ) is not None
    monkeypatch.setattr(wechat_browser_snapshot, "_code_fingerprint", lambda: "reader-update")

    entered, release = Event(), Event()
    service = app.extensions["wechat_browser_read"]
    monkeypatch.setattr(service, "loader", lambda _: None)

    def hold_validation(_parameters):
        entered.set()
        assert release.wait(4)

    monkeypatch.setattr(service, "validator", hold_validation)
    try:
        response = app.test_client().get("/browser", query_string={
            "view": "text", "source": "wechat", "session_view": "1",
            "account": "source:xlsx-2", "chat_kind": "group",
        })
        assert response.status_code == 200 and entered.wait(1)
        document = BeautifulSoup(response.data, "html.parser")
        assert document.select_one("[data-wechat-read-pending]")["data-state"] == "running"
        preview = document.select_one("[data-wechat-read-preview]")
        assert (preview is not None) is snapshot_available
        assert bool(document.select_one(".wechat-read-skeleton")) is not snapshot_available
        if preview is not None:
            assert "No WeChat conversations matched these filters in the last verified snapshot." in preview.get_text(" ", strip=True)
            assert not preview.select("[data-chat-session-id]")
    finally:
        release.set()
        app.extensions["runtime_shutdown"]()


@pytest.mark.parametrize("completion_change", ("unchanged", "published", "replaced", "removed", "registry"))
def test_pending_direct_account_page_checks_changed_inputs_before_showing_navigation(
    session_app, monkeypatch, completion_change,
):
    app, root = session_app
    repository = WechatArchiveRepository(root)
    contacts = repository.path.with_name("contacts.sqlite3")
    with closing(sqlite3.connect(contacts)) as connection, connection:
        initialize_contact_registry(connection)
    bank = repository.path.with_name("macos_recovery.sqlite3")
    bank.write_bytes(b"synthetic previous bank")
    completion = repository.path.with_name(FILENAME)
    if completion_change in {"replaced", "removed", "registry"}:
        completion.write_bytes(b"synthetic previous completion")
    directory, key, _identity = wechat_browser_snapshot._storage(repository)
    pin = wechat_browser_snapshot._pin(repository, _file_identity(repository.path))
    with closing(sqlite3.connect(repository.path)) as connection:
        rows = [list(row) for row in connection.execute("SELECT * FROM conversations")]
    hidden_body = "Synthetic prior message body for the navigation preview."
    for index, row in enumerate(rows):
        row[6] = 1_000 + index
        row[-1] = hidden_body
    payload = {
        "pin": pin, "rows": rows, "resource_status": "absent", "resource_index_accounts": None,
    }
    assert wechat_browser_snapshot._write_snapshot(directory, key, payload, lambda: True)
    replacement = bank.with_suffix(".candidate")
    replacement.write_bytes(b"synthetic replacement bank")
    replacement.replace(bank)
    if completion_change in {"published", "replaced", "registry"}:
        completion.write_bytes(b"synthetic new completion")
    elif completion_change == "removed":
        completion.unlink()
    if completion_change == "registry":
        with closing(sqlite3.connect(contacts)) as connection, connection:
            connection.execute("CREATE TABLE changed_registry (value TEXT)")
    monkeypatch.setattr(wechat_browser_snapshot, "_code_fingerprint", lambda: "reader-update")

    entered, release = Event(), Event()
    service = app.extensions["wechat_browser_read"]
    monkeypatch.setattr(service, "loader", lambda _: None)

    def hold_validation(_parameters):
        entered.set()
        assert release.wait(4)

    monkeypatch.setattr(service, "validator", hold_validation)
    account = "account:wxid_account_a"
    expected_counts = {
        row[0]: f"{row[6]:,}" for row in rows if row[2] == account and row[5] == "direct"
    }
    assert expected_counts
    try:
        response = app.test_client().get("/browser", query_string={
            "view": "text", "source": "wechat", "account": account, "chat_kind": "direct",
            "session_view": "1", "page": "1", "sort": "newest", "q": "",
        })
        assert response.status_code == 200 and entered.wait(1)
        document = BeautifulSoup(response.data, "html.parser")
        pending = document.select_one("[data-wechat-read-pending]")
        assert pending is not None and pending.get("data-state") == "running"
        preview = document.select_one("[data-wechat-read-preview]")
        if completion_change in {"removed", "registry"}:
            assert preview is None
            return
        assert preview is not None
        visible = preview.select("tr[data-chat-session-id]")
        assert "last verified snapshot" in preview.get_text(" ", strip=True)
        assert {
            row["data-chat-session-id"]: row.select_one("td:last-child").get_text(strip=True)
            for row in visible
        } == expected_counts
        assert {row.get("data-wechat-account") for row in visible} == {account}
        assert {row.get("data-wechat-conversation-kind") for row in visible} == {"direct"}
        assert not document.select("[data-chat-message-id]")
        assert hidden_body.encode() not in response.data
    finally:
        release.set()
        app.extensions["runtime_shutdown"]()
