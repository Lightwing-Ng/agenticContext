"""Keep the global conversation list browsable during a completion refresh.

Code version: v1.0.0-codex.0
"""

from contextlib import closing
import sqlite3
from threading import Event

import pytest

from app.core.wechat_archive import WechatArchiveRepository, _file_identity
from app.core import wechat_browser_snapshot
from app.core.wechat_contacts import initialize_contact_registry
from app.core.wechat_secondary_completions import FILENAME
from tests import test_wechat_session_csv
from tests.wechat_browser_markup import parse_browser_markup


session_app = test_wechat_session_csv.session_app


@pytest.mark.parametrize("change", ("completion", "reader"))
def test_pending_all_accounts_use_authenticated_summaries(session_app, monkeypatch, change):
    app, root = session_app
    repository = WechatArchiveRepository(root)
    contacts = repository.path.with_name("contacts.sqlite3")
    with closing(sqlite3.connect(contacts)) as connection, connection:
        initialize_contact_registry(connection)
    with closing(sqlite3.connect(repository.path)) as connection:
        assert wechat_browser_snapshot.load_conversation_snapshot(
            repository, connection, _file_identity(repository.path)
        ) is not None
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
        document = parse_browser_markup(response.data)
        pending = document.find(".//*[@data-wechat-read-pending]")
        assert pending is not None and pending.get("data-state") == "running"
        preview = document.find(".//*[@data-wechat-read-preview]")
        assert preview is not None
        rows = preview.findall(".//tr[@data-chat-session-id]")
        assert len(rows) == 6
        assert {row.get("data-wechat-account") for row in rows} == {
            "account:wxid_account_a", "account:wxid_account_b", "source:xlsx-2", "source:xlsx-3",
        }
        assert all("".join(row.findall("td")[-1].itertext()).strip() == "—" for row in rows)
        assert not document.findall(".//*[@data-chat-message-id]")
    finally:
        release.set()
        app.extensions["runtime_shutdown"]()
