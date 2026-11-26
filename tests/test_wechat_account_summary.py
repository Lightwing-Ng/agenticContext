"""Account lists consume only a verified shard of the current publication.

Code version: v1.3.1-codex.0
"""

from contextlib import closing
import sqlite3
from threading import Event
import time

from bs4 import BeautifulSoup
import pytest

from app.core.storage import wechat_summary_read as summary
from app.core.storage.wechat_summary_worker import WechatSummaryWorker
from app.core.wechat_archive import WechatArchiveRepository, _file_identity, _identifier
from app.core.wechat_contacts import contact_registry_path, initialize_contact_registry
from app.core.wechat_history_browser import query_wechat_history
from app.web.wechat_read_routes import (
    WECHAT_DIRECT_READ_EXTENSION, WECHAT_READ_EXTENSION, WECHAT_SUMMARY_EXTENSION,
)
from tests import test_wechat_browser_snapshot as snapshot_tests
from tests import test_wechat_session_csv as session_tests
from tests.test_wechat_context_links import context_link_fixture
from tests.test_wechat_context_links import synthetic_export_clone as synthetic_export_clone


event_store = snapshot_tests.event_store
snapshot_store = snapshot_tests.snapshot_store
session_app = session_tests.session_app
_ACCOUNT = "account:wxid_event_owner"


@pytest.mark.parametrize("sort", ("newest", "oldest", "name"))
def test_scope_matches_verified_list_without_opening_the_archive(snapshot_store, monkeypatch, sort):
    repository = snapshot_store.repository
    identity = _file_identity(repository.path)
    pin = summary.snapshot._pin

    def separate_snapshot_identity(repository, history_identity):
        # Model a Windows snapshot's independent change-time token.
        current = pin(repository, history_identity)
        history = current["inputs"][str(repository.path.resolve())]
        history["ctime_ns"] += 1
        return current

    monkeypatch.setattr(summary.snapshot, "_pin", separate_snapshot_identity)
    assert snapshot_tests._load(snapshot_store)[0] is not None
    proof_connection, proof = snapshot_tests.fixtures._run(snapshot_store)
    proof_connection.close()
    assert proof["count_delta"] == 3
    expected = query_wechat_history(
        snapshot_store.store, account=_ACCOUNT, chat_kind="direct", sort=sort, page=1,
    )
    assert summary.publish_account_summaries(repository, identity)

    opened = []
    read = summary._read

    def tracked(directory, filename, key):
        opened.append(filename)
        return read(directory, filename, key)

    monkeypatch.setattr(summary, "_read", tracked)
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: pytest.fail("Opened SQLite"))
    result = summary.read_account_summary(snapshot_store.store, account=_ACCOUNT, sort=sort, page=1)
    assert result == expected
    assert result.conversation_count == 1
    assert result.messaging["message_count"] == 3
    assert opened == [summary.MANIFEST, summary._account_filename(_ACCOUNT)]


@pytest.mark.parametrize("sort", ("newest", "name"))
def test_all_chats_preserve_account_scope_kinds_and_verified_titles(snapshot_store, monkeypatch, sort):
    repository = snapshot_store.repository
    selected_partition = snapshot_tests.fixtures.NATIVE
    other_account = "account:wxid_other"
    group_id = "wechat:" + "a" * 64
    group_notice = 'You changed the group name to "A renamed group"'
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            ("other", other_account, "wxid_other", "Other", "Archived", "Other source", "Other.zip", ""),
        )
        connection.executemany(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                (group_id, selected_partition, _ACCOUNT, "fixture@chatroom", "Zulu group", "group",
                 1, 6, 6, "", "", group_notice),
                ("wechat:" + "b" * 64, selected_partition, _ACCOUNT, "gh_fixture", "Official title", "direct",
                 2, 4, 4, "", "", "Official preview"),
                ("wechat:" + "c" * 64, selected_partition, _ACCOUNT, None, "Unclassified title", "unclassified",
                 2, 0, 0, "", "", "Unclassified preview"),
                ("wechat:" + "d" * 64, "other", other_account, "other@chatroom", "Foreign group", "group",
                 20, 10, 10, "", "", "Other account preview"),
            ),
        )
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "rename-notice", group_id, 0, None, "", "incoming", group_notice, 6,
                "2026-01-02T03:04:05+00:00", "", "UTC+00:00",
                '{"text_extraction_kind":"system_text","message_type":10000}',
                '["system_notice"]', group_notice.lower(),
            ),
        )
    assert snapshot_tests._load(snapshot_store)[0] is not None
    assert summary.publish_account_summaries(repository, _file_identity(repository.path))

    scopes = ("all", "direct", "group", "unclassified")
    expected = {
        kind: query_wechat_history(
            snapshot_store.store, account=_ACCOUNT, chat_kind=kind, sort=sort, page=1,
        )
        for kind in scopes
    }
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: pytest.fail("Opened SQLite"))
    for kind in scopes:
        page = summary.read_account_summary(
            snapshot_store.store, account=_ACCOUNT, chat_kind=kind, sort=sort, page=1,
        )
        assert page == expected[kind]
        assert {session.messaging["account_key"] for session in page.sessions} == {_ACCOUNT}
    assert {
        (session.conversation_title, session.messaging["conversation_kind_label"])
        for session in expected["all"].sessions
    } == {
        ("A renamed group", "Group chat"),
        ("Official title", "Official account"),
        ("Synthetic conversation", "Private chat"),
        ("Unclassified title", "Unclassified chat"),
    }


def test_group_summary_folds_verified_html_supplement_before_count_and_pagination(
    tmp_path, monkeypatch, synthetic_export_clone,
):
    fixture = context_link_fixture(tmp_path)
    repository = WechatArchiveRepository(tmp_path)
    with closing(sqlite3.connect(contact_registry_path(tmp_path))) as connection:
        initialize_contact_registry(connection)
        connection.commit()
    account = "account:wxid_owner_a"
    other_group = "wechat:" + _identifier("separate HTML room")
    with closing(sqlite3.connect(repository.path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        base_id = connection.execute(
            "SELECT conversation_id FROM messages WHERE id=?", (fixture["target_id"],),
        ).fetchone()[0]
        supplement = dict(connection.execute(
            "SELECT * FROM messages WHERE id=?", (fixture["source_id"],),
        ).fetchone())
        supplement_id = supplement["conversation_id"]
        supplement.update(
            id=_identifier("supplement-only occurrence"), ordinal=supplement["ordinal"] + 1,
            provenance="{}",
        )
        connection.execute(
            "INSERT INTO messages VALUES(" + ",".join("?" for _ in supplement) + ")",
            tuple(supplement.values()),
        )
        connection.execute("UPDATE conversations SET last_sort=100 WHERE id=?", (base_id,))
        connection.execute(
            "UPDATE conversations SET last_sort=300,message_count=2 WHERE id=?", (supplement_id,),
        )
        connection.execute(
            "INSERT INTO conversations SELECT ?,partition_id,account_key,'different@chatroom',"
            "title,kind,0,200,200,'','','' FROM conversations WHERE id=?", (other_group, base_id),
        )
    identity = _file_identity(repository.path)
    with closing(sqlite3.connect(repository.path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        assert summary.snapshot.load_conversation_snapshot(repository, connection, identity)
    assert summary.publish_account_summaries(repository, identity)
    monkeypatch.setattr(summary, "CHAT_HISTORY_PAGE_SIZE", 1)
    for sort, first_id in (("newest", base_id), ("oldest", other_group)):
        pages = []
        for page in (1, 2):
            scope = dict(account=account, chat_kind="group", sort=sort, page=page)
            expected = query_wechat_history(tmp_path, **scope, page_size=1)
            actual = summary.read_account_summary(tmp_path, **scope)
            assert actual == expected
            assert actual.total_count == actual.total_pages == 2
            assert actual.messaging["message_count"] == 2
            pages.append(actual.sessions[0])
        assert pages[0].stable_id == first_id
        base = next(session for session in pages if session.stable_id == base_id)
        assert base.message_count == 2
        assert base.messaging["html_supplement"]["id"] == supplement_id
        assert base.messaging["html_supplement"]["additional_count"] == 1


def test_corrupt_shard_or_changed_source_cannot_serve_as_current(snapshot_store):
    repository = snapshot_store.repository
    identity = _file_identity(repository.path)
    assert snapshot_tests._load(snapshot_store)[0] is not None
    assert summary.publish_account_summaries(repository, identity)
    directory = repository.path.with_name("browser_cache")
    path = directory / summary._account_filename(_ACCOUNT)
    path.write_bytes(path.read_bytes() + b"tampered")

    def read():
        return summary.read_account_summary(snapshot_store.store, account=_ACCOUNT, sort="newest", page=1)

    assert read() is None
    assert summary.publish_account_summaries(repository, identity)
    assert read() is not None
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE conversations SET title='Published replacement'")
    assert read() is None
    assert not summary.publish_account_summaries(repository, _file_identity(repository.path))
    assert snapshot_tests._load(snapshot_store)[0] is not None
    assert summary.publish_account_summaries(repository, _file_identity(repository.path))
    assert read().sessions[0].conversation_title == "Published replacement"


def test_previous_shard_retains_preview_rules_and_hides_message_previews(snapshot_store, monkeypatch):
    repository = snapshot_store.repository
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE conversations SET latest_text='A saved message preview'")
    identity = _file_identity(repository.path)
    assert snapshot_tests._load(snapshot_store)[0] is not None
    assert summary.publish_account_summaries(repository, identity)
    scope = dict(account=_ACCOUNT, sort="newest", page=1)
    assert summary.read_account_summary(snapshot_store.store, **scope).sessions[0].latest_message
    assert summary.read_account_summary(snapshot_store.store, **scope, previous=True) is None

    monkeypatch.setattr(summary.snapshot, "_code_fingerprint", lambda: "reader-code-v2")
    assert summary.read_account_summary(snapshot_store.store, **scope) is None
    preview = summary.read_account_summary(snapshot_store.store, **scope, previous=True)
    assert preview.messaging["message_count"] == 0
    assert preview.sessions[0].latest_message == ""

    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE conversations SET title='Published replacement'")
    assert summary.read_account_summary(snapshot_store.store, **scope, previous=True) is None


def test_worker_republishes_a_verified_shard_after_archive_changes(snapshot_store):
    root = snapshot_store.store
    # Give the worker's own connection the real source path, without preparing a
    # snapshot in the test or invoking any browser request.
    worker = WechatSummaryWorker(root, interval_seconds=0.02)

    def wait_for_title(title):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            page = summary.read_account_summary(root, account=_ACCOUNT, sort="newest", page=1)
            if page is not None and page.sessions[0].conversation_title == title:
                return
            assert worker.wait_for_publication(timeout=0.03) != "failed"
        pytest.fail("The worker did not publish the expected summary.")

    worker.start()
    try:
        wait_for_title("Synthetic conversation")
        with sqlite3.connect(snapshot_store.repository.path) as connection:
            connection.execute("UPDATE conversations SET title='Background replacement'")
        wait_for_title("Background replacement")
    finally:
        worker.shutdown()


@pytest.mark.parametrize(
    ("chat_kind", "expected_kinds"),
    (("direct", {"direct"}), ("group", {"group"}), ("all", {"direct", "group"})),
)
def test_http_miss_waits_for_worker_and_never_falls_back_to_global_read(
    session_app, monkeypatch, chat_kind, expected_kinds,
):
    app, root = session_app
    worker = app.extensions[WECHAT_SUMMARY_EXTENSION]
    repository = WechatArchiveRepository(root)
    with closing(sqlite3.connect(contact_registry_path(root))) as connection:
        initialize_contact_registry(connection)
        connection.commit()
    release = Event()
    scoped = app.extensions[WECHAT_DIRECT_READ_EXTENSION]

    def wait_for_publication():
        return "ready" if release.wait(2) else "running"

    monkeypatch.setattr(type(worker), "active", property(lambda _: True))
    monkeypatch.setattr(worker, "wait_for_publication", wait_for_publication)
    monkeypatch.setattr(
        app.extensions[WECHAT_READ_EXTENSION], "acquire",
        lambda *_args, **_kwargs: pytest.fail("Used the global read service"),
    )
    account = "account:wxid_account_a"
    url = (
        f"/browser?view=text&session_view=1&source=wechat&account={account}"
        f"&chat_kind={chat_kind}&sort=newest&q="
    )
    client = app.test_client()
    try:
        pending = BeautifulSoup(client.get(url).data, "html.parser").select_one("[data-wechat-read-pending]")
        assert pending["data-state"] == "running"
        assert client.get(pending["data-status-url"]).json["state"] == "running"
        with closing(sqlite3.connect(repository.path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            assert summary.snapshot.load_conversation_snapshot(
                repository, connection, _file_identity(repository.path),
            ) is not None
        assert summary.publish_account_summaries(repository, _file_identity(repository.path))
        release.set()
        deadline = time.monotonic() + 3
        while scoped.status(pending["data-task-id"])["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert client.get(pending["data-status-url"]).json["state"] == "succeeded"
        html = BeautifulSoup(client.get(url).data, "html.parser")
        assert html.select_one("[data-wechat-read-pending]") is None
        rows = html.select("tr[data-chat-session-id]")
        assert rows and {row["data-wechat-account"] for row in rows} == {account}
        assert {row["data-wechat-conversation-kind"] for row in rows} == expected_kinds
    finally:
        release.set()
        app.extensions["runtime_shutdown"]()


@pytest.mark.parametrize("preview", ("signed", "tampered", "wrong-pin"))
def test_failed_account_view_keeps_only_the_authenticated_preview(session_app, monkeypatch, preview):
    app, root = session_app
    worker = app.extensions[WECHAT_SUMMARY_EXTENSION]
    repository = WechatArchiveRepository(root)
    contacts = contact_registry_path(root)
    with closing(sqlite3.connect(contacts)) as connection, connection:
        initialize_contact_registry(connection)
    identity = _file_identity(repository.path)
    with closing(sqlite3.connect(repository.path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        assert summary.snapshot.load_conversation_snapshot(repository, connection, identity)
    assert summary.publish_account_summaries(repository, identity)

    account = "account:wxid_account_a"
    if preview == "tampered":
        shard = repository.path.with_name("browser_cache") / summary._account_filename(account)
        shard.write_bytes(shard.read_bytes() + b"tampered")
    elif preview == "wrong-pin":
        with closing(sqlite3.connect(contacts)) as connection, connection:
            connection.execute("CREATE TABLE replaced_registry_fixture (value TEXT)")
    monkeypatch.setattr(summary.snapshot, "_code_fingerprint", lambda: "reader-update")

    release = Event()

    def reject_publication():
        assert release.wait(4)
        return "failed"

    monkeypatch.setattr(type(worker), "active", property(lambda _: True))
    monkeypatch.setattr(worker, "wait_for_publication", reject_publication)
    client = app.test_client()
    parameters = {
        "view": "text", "source": "wechat", "session_view": "1", "account": account, "chat_kind": "all",
    }

    def saved_view(state):
        response = client.get("/browser", query_string=parameters)
        assert response.status_code == 200
        html = BeautifulSoup(response.data, "html.parser")
        panel = html.select_one("[data-wechat-read-pending]")
        assert panel["data-state"] == state
        rows = html.select("tr[data-chat-session-id]")
        assert bool(rows) is (preview == "signed")
        assert panel.has_attr("data-wechat-read-account-preview") is (preview == "signed")
        assert html.select_one("[data-chat-message-id], .browser-metric-grid [data-numeric-display-value]") is None
        if rows:
            assert {row["data-wechat-account"] for row in rows} == {account}
            assert "last verified snapshot" in html.select_one("[data-wechat-read-preview]").get_text()
        if state == "failed":
            assert "Saved records could not be checked" in panel.select_one("[data-wechat-read-copy]").get_text()
            assert not panel.select_one("[data-wechat-read-retry]").has_attr("hidden")
        return panel, [row["data-chat-session-id"] for row in rows]

    def wait_until_failed(status_url):
        deadline = time.monotonic() + 3
        while (status := client.get(status_url).json["state"]) == "running":
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert status == "failed"

    try:
        pending, previous = saved_view("running")
        release.set()
        wait_until_failed(pending["data-status-url"])
        failed, after_reload = saved_view("failed")
        assert after_reload == previous
        retried = client.post(failed["data-retry-url"]).json
        assert retried["task_id"] != failed["data-task-id"]
        wait_until_failed(retried["status_url"])
        _, after_retry = saved_view("failed")
        assert after_retry == previous
    finally:
        release.set()
        app.extensions["runtime_shutdown"]()
