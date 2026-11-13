"""Cold pages remain responsive while exact verified results are built once.

Code version: v1.0.3-codex.0
"""

from threading import Event
import time

import pytest

from app.core.chat_history_browser import ChatHistoryPage
from app.core.wechat_browser_read import WechatBrowserReadService
from tests import test_wechat_session_csv
from tests import test_wechat_conversation_events
from tests.wechat_browser_markup import parse_browser_markup


session_app = test_wechat_session_csv.session_app
event_store = test_wechat_conversation_events.event_store


def _page(status="ready", **metadata):
    return ChatHistoryPage(
        items=(), total_count=7, conversation_count=2, current_page=1, total_pages=1,
        messaging={"resource_index_status": status, **metadata},
    )


def _done(service, token):
    deadline = time.monotonic() + 3
    while service.status(token)["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.002)
    return service.status(token)


def test_blocked_singleflight_and_status_never_reenter_loader_or_pin(tmp_path):
    entered, release = Event(), Event()
    calls = []
    expected = _page(authority=object())

    def load(parameters):
        calls.append(parameters)
        entered.set()
        assert release.wait(3)
        return expected

    service = WechatBrowserReadService(tmp_path, loader=load, pin=lambda: "unchanged", validator=lambda _value: None)
    try:
        started = time.monotonic()
        page, first = service.acquire({"session": "selected"})
        assert page is None and time.monotonic() - started < 0.3
        assert entered.wait(1)
        assert service.acquire({"session": "selected"}, wait=False)[1] == first
        service._pin_override = lambda: (_ for _ in ()).throw(AssertionError("status attempted filesystem access"))
        started = time.monotonic()
        assert service.status(first["task_id"])["state"] == "running"
        assert time.monotonic() - started < 0.1
        service._pin_override = lambda: "unchanged"
        release.set()
        assert _done(service, first["task_id"])["state"] == "succeeded"
        result, _job = service.acquire({"session": "selected"})
        assert result is expected
        assert result.messaging["authority"] is expected.messaging["authority"]
        assert len(calls) == 1
    finally:
        release.set()
        service.shutdown()


def test_query_keys_separate_sessions_search_and_accounts(tmp_path):
    release = Event()
    calls = []
    service = WechatBrowserReadService(
        tmp_path, loader=lambda parameters: release.wait(3) and calls.append(parameters) or _page(),
        pin=lambda: "fixed", validator=lambda _value: None,
    )
    try:
        jobs = [service.acquire(parameters, wait=False)[1]["task_id"] for parameters in (
            {"session": "a"}, {"session": "b"}, {"query": "exact literal"}, {"account": "other"},
        )]
        assert len(set(jobs)) == 4
        release.set()
        for token in jobs:
            assert _done(service, token)["state"] == "succeeded"
        assert len(calls) == 4
    finally:
        release.set()
        service.shutdown()


@pytest.mark.parametrize("when", ("during", "after"))
def test_input_drift_never_delivers_old_result(tmp_path, when):
    release = Event()
    pin = ["first"]
    pages = []

    def load(_parameters):
        assert release.wait(3)
        pages.append(_page(marker=len(pages)))
        return pages[-1]

    service = WechatBrowserReadService(tmp_path, loader=load, pin=lambda: pin[0], validator=lambda _value: None)
    try:
        _, job = service.acquire({"session": "selected"}, wait=False)
        if when == "during":
            pin[0] = "second"
        release.set()
        finished = _done(service, job["task_id"])
        if when == "after":
            assert finished["state"] == "succeeded"
            pin[0] = "second"
        else:
            assert finished["state"] == "failed"
        first_result = pages[0] if pages else None
        page, fresh = service.acquire({"session": "selected"}, wait=False)
        assert page is None and fresh["task_id"] != job["task_id"]
        assert _done(service, fresh["task_id"])["state"] == "succeeded"
        result, _ = service.acquire({"session": "selected"})
        assert result is not first_result
    finally:
        release.set()
        service.shutdown()


@pytest.mark.parametrize("status", ("invalid", "stale", "unavailable"))
def test_unverified_fallback_is_not_ready(tmp_path, status):
    service = WechatBrowserReadService(tmp_path, loader=lambda _: _page(status), pin=lambda: "fixed", validator=lambda _: None)
    try:
        _, job = service.acquire({}, wait=False)
        assert _done(service, job["task_id"])["state"] == "failed"
        assert service.acquire({})[0] is None
    finally:
        service.shutdown()


def test_optional_unavailable_overlay_preserves_verified_source_page(tmp_path):
    expected = _page(event_projection={"status": "unavailable", "count_delta": 0})
    validated = []
    service = WechatBrowserReadService(tmp_path, loader=lambda _: expected,
        pin=lambda: "fixed", validator=lambda _: validated.append(True))
    try:
        _, job = service.acquire({}, wait=False)
        assert _done(service, job["task_id"])["state"] == "succeeded"
        page, _ = service.acquire({})
        assert validated == [True] and page is expected
        assert page.messaging["event_projection"]["status"] == "unavailable"
    finally:
        service.shutdown()


def test_strict_validator_failure_does_not_call_query_and_retry_is_explicit(tmp_path):
    calls = []

    def invalid(_):
        raise ValueError("private details must not escape")

    service = WechatBrowserReadService(tmp_path, loader=lambda _: calls.append(1) or _page(), pin=lambda: "fixed", validator=invalid)
    try:
        _, first = service.acquire({}, wait=False)
        failed = _done(service, first["task_id"])
        assert failed["state"] == "failed" and "private details" not in failed["message"]
        assert not calls
        service.validator = lambda _: None
        retried = service.retry(first["task_id"])
        assert retried["task_id"] != first["task_id"]
        assert _done(service, retried["task_id"])["state"] == "succeeded"
        assert len(calls) == 1
    finally:
        service.shutdown()


def test_shutdown_stops_queued_work_without_waiting_for_blocked_validation(tmp_path):
    entered, release = Event(), Event()
    calls = []

    def validate(parameters):
        calls.append(parameters)
        entered.set()
        assert release.wait(3)

    service = WechatBrowserReadService(tmp_path, loader=lambda _: _page(), pin=lambda: "fixed", validator=validate)
    _, first = service.acquire({"session": "a"}, wait=False)
    assert entered.wait(1)
    _, second = service.acquire({"session": "b"}, wait=False)
    started = time.monotonic()
    service.shutdown(timeout=0.01)
    assert time.monotonic() - started < 0.2
    assert service.status(second["task_id"])["state"] == "failed"
    with pytest.raises(RuntimeError, match="shutting down"):
        service.acquire({})
    release.set()
    assert _done(service, first["task_id"])["state"] == "failed"
    assert calls == [{"session": "a"}]


@pytest.mark.parametrize("detail", (False, True))
def test_real_route_returns_pending_shell_and_safe_poll_while_loader_blocked(session_app, monkeypatch, detail):
    from app.web import local_resource_routes as routes
    from tests.test_wechat_resource_timeline import _session

    app, source_root = session_app
    entered, release = Event(), Event()
    original = routes.query_chat_history

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return original(*args, **kwargs)

    monkeypatch.setattr(routes, "query_chat_history", blocked)
    service = app.extensions["wechat_browser_read"]
    try:
        started = time.monotonic()
        address = "/browser?view=text&source=wechat&session_view=1"
        if detail:
            address += "&session=" + _session(source_root, kind="group")["id"]
        response = app.test_client().get(address)
        assert response.status_code == 200 and time.monotonic() - started < 1
        assert entered.wait(1)
        assert b"data-wechat-read-pending" in response.data
        assert b"No WeChat conversations" not in response.data
        assert b"No cached text found" not in response.data
        assert b"Cached text totals" not in response.data
        assert b"data-wechat-conversation-refresh" not in response.data
        if detail:
            assert b'data-browser-search-global-scope="false"' in response.data
            assert b'data-browser-search-scope="session"' in response.data
        job = next(iter(service._tasks.values()))
        status = app.test_client().get("/api/browser/wechat/read/" + job["task_id"])
        assert status.status_code == 200 and status.json["state"] == "running"
        assert set(status.json) == {"task_id", "generation", "state", "message", "status_url", "retry_url"}
        assert status.cache_control.no_store
        release.set()
        assert _done(service, job["task_id"])["state"] == "succeeded"
        ready = app.test_client().get(address)
        assert ready.status_code == 200 and b"data-wechat-read-pending" not in ready.data
        assert b"WeChat conversations" in ready.data if not detail else b"data-chat-message-id" in ready.data
    finally:
        release.set()
        app.extensions["runtime_shutdown"]()


def test_pending_account_shows_previous_conversations_until_current_page_is_ready(
    session_app, monkeypatch,
):
    from app.core import wechat_browser_snapshot

    app, _ = session_app
    service = app.extensions["wechat_browser_read"]
    entered, release = Event(), Event()
    current_loader = service.loader

    def blocked_loader(parameters):
        entered.set()
        assert release.wait(4)
        return current_loader(parameters)

    monkeypatch.setattr(service, "loader", blocked_loader)
    monkeypatch.setattr(service, "validator", lambda _: None)
    monkeypatch.setattr(
        wechat_browser_snapshot,
        "load_previous_conversation_snapshot",
        lambda *_: ("main.conversations", "absent", None),
    )
    account = "account:wxid_account_a"
    parameters = {
        "view": "text", "source": "wechat", "session_view": "1", "account": account,
    }
    client = app.test_client()
    try:
        response = client.get("/browser", query_string=parameters)
        assert response.status_code == 200
        assert entered.wait(1)
        pending = parse_browser_markup(response.data)
        shell = pending.find(".//*[@data-wechat-read-pending]")
        assert shell.get("data-state") == "running"
        assert client.get(shell.get("data-status-url")).json["state"] == "running"
        copy = shell.find(".//*[@data-wechat-read-copy]")
        assert not "".join(copy.itertext()).strip()
        preview = pending.find(".//*[@data-wechat-read-preview]")
        assert "Message counts are from the last verified snapshot" not in "".join(preview.itertext())
        rows = preview.findall(".//tr[@data-chat-session-id]")
        assert len(rows) == 2
        assert {row.get("data-wechat-account") for row in rows} == {account}
        assert all("".join(row.findall("td")[-1].itertext()).strip() == "—" for row in rows)
        assert not pending.findall(".//*[@data-chat-message-id]")

        release.set()
        assert _done(service, shell.get("data-task-id"))["state"] == "succeeded"
        response = client.get("/browser", query_string=parameters)
        assert response.status_code == 200
        ready = parse_browser_markup(response.data)
        assert ready.find(".//*[@data-wechat-read-pending]") is None
        assert ready.find(".//*[@data-wechat-read-preview]") is None
        current_rows = ready.findall(".//tr[@data-chat-session-id]")
        assert [row.get("data-chat-session-id") for row in current_rows] == [
            row.get("data-chat-session-id") for row in rows
        ]
        assert all("".join(row.findall("td")[-1].itertext()).strip() == "215" for row in current_rows)
    finally:
        release.set()
        app.extensions["runtime_shutdown"]()


def test_absent_optional_identity_sidecar_keeps_verified_raw_session(session_app):
    from tests.test_wechat_resource_timeline import _session

    app, root = session_app
    session = _session(root, kind="group")
    assert not (root / "messaging/wechat/contacts.sqlite3").exists()
    service = app.extensions["wechat_browser_read"]
    parameters = {"session": session["id"]}
    try:
        _page_value, job = service.acquire(parameters, wait=False)
        assert _done(service, job["task_id"])["state"] == "succeeded"
        result, _ = service.acquire(parameters)
        assert result.items
        assert result.messaging["resource_index_status"] in {"ready", "absent"}
        assert result.messaging["event_projection"]["status"] == "unavailable"
        assert not (root / "messaging/wechat/contacts.sqlite3").exists()
    finally:
        app.extensions["runtime_shutdown"]()


@pytest.mark.parametrize("broken_link", (False, True))
def test_present_corrupt_identity_sidecar_cannot_become_ready(session_app, broken_link):
    app, root = session_app
    contacts = root / "messaging/wechat/contacts.sqlite3"
    if broken_link:
        contacts.symlink_to(contacts.with_name("missing-synthetic-registry"))
    else:
        contacts.write_bytes(b"invalid synthetic identity database")
    service = app.extensions["wechat_browser_read"]
    try:
        if broken_link:
            with pytest.raises(ValueError, match="symbolic link"):
                service.acquire({}, wait=False)
        else:
            _, job = service.acquire({}, wait=False)
            assert _done(service, job["task_id"])["state"] == "failed"
            assert service.acquire({})[0] is None
    finally:
        app.extensions["runtime_shutdown"]()


def test_disappeared_required_archive_is_not_reported_as_empty(session_app):
    app, root = session_app
    history = root / "messaging/wechat/history.sqlite3"
    history.rename(history.with_suffix(".synthetic-unavailable"))
    try:
        response = app.test_client().get("/browser?view=text&source=wechat&session_view=1")
        assert response.status_code == 200
        assert b"data-wechat-read-pending" in response.data
        assert b'data-state="failed"' in response.data
        assert b"No cached text found" not in response.data
        assert b"No WeChat conversations" not in response.data
    finally:
        app.extensions["runtime_shutdown"]()


def test_generated_cache_files_do_not_invalidate_source_pin_but_preferences_do(session_app):
    app, root = session_app
    service = app.extensions["wechat_browser_read"]
    base = root / "messaging/wechat"
    try:
        before = service._pin()
        for name in ("browser_cache", "conversation_events"):
            directory = base / name
            directory.mkdir(exist_ok=True)
            (directory / "synthetic.json").write_text("{}")
        assert service._pin() == before
        (base / "display_names.json").write_text("{}")
        assert service._pin() != before
    finally:
        app.extensions["runtime_shutdown"]()


def test_signed_summary_reuse_does_not_promote_authority_to_message_views(event_store, monkeypatch):
    from app.core import wechat_browser_snapshot as snapshots
    from app.core.wechat_archive import WechatArchiveRepository

    calls = []
    original = WechatArchiveRepository._timeline_tables

    def strict_timeline(repository, *args, **options):
        calls.append(options)
        assert options.get("strict") is True
        return original(repository, *args, **options)

    monkeypatch.setattr(WechatArchiveRepository, "_timeline_tables", strict_timeline)
    service = WechatBrowserReadService(event_store.repository.path.parent.parent.parent)
    try:
        service._validate({})
        assert len(calls) == 1
        snapshots._SNAPSHOTS.clear()
        service._validate({})
        assert len(calls) == 1
        service._validate({"session": test_wechat_conversation_events.SESSION})
        assert len(calls) == 2
        assert calls[-1]["conversation_id"] == test_wechat_conversation_events.SESSION
        service._validate({"query": "literal search"})
        assert len(calls) == 3
    finally:
        service.shutdown()


def test_result_memory_budget_fails_closed_without_retaining_page(tmp_path, monkeypatch):
    from app.core import wechat_browser_read as reads

    monkeypatch.setattr(reads, "MAX_RESULT_BYTES", 256)
    service = WechatBrowserReadService(tmp_path, loader=lambda _: _page(payload="synthetic" * 1000),
                                       pin=lambda: "fixed", validator=lambda _: None)
    try:
        _, job = service.acquire({}, wait=False)
        assert _done(service, job["task_id"])["state"] == "failed"
        assert service._result_bytes == 0
        assert service.acquire({})[0] is None
    finally:
        service.shutdown()


def test_pending_queue_is_bounded_and_does_not_start_parallel_workers(tmp_path, monkeypatch):
    from app.core import wechat_browser_read as reads

    monkeypatch.setattr(reads, "MAX_TASKS", 2)
    entered, release = Event(), Event()
    calls = []

    def load(parameters):
        calls.append(parameters)
        entered.set()
        assert release.wait(3)
        return _page()

    service = WechatBrowserReadService(tmp_path, loader=load, pin=lambda: "fixed", validator=lambda _: None)
    try:
        service.acquire({"session": "one"}, wait=False)
        assert entered.wait(1)
        service.acquire({"session": "two"}, wait=False)
        with pytest.raises(RuntimeError, match="Several saved views"):
            service.acquire({"session": "three"}, wait=False)
        assert calls == [{"session": "one"}]
    finally:
        release.set()
        service.shutdown()


def test_memory_eviction_never_advertises_an_undeliverable_ready_result(tmp_path, monkeypatch):
    from app.core import wechat_browser_read as reads

    size = reads._result_size(_page())
    monkeypatch.setattr(reads, "MAX_RESULT_BYTES", size + 16)
    service = WechatBrowserReadService(tmp_path, loader=lambda _: _page(), pin=lambda: "fixed", validator=lambda _: None)
    try:
        _, first = service.acquire({"session": "one"}, wait=False)
        assert _done(service, first["task_id"])["state"] == "succeeded"
        _, second = service.acquire({"session": "two"}, wait=False)
        assert _done(service, second["task_id"])["state"] == "succeeded"
        assert service.status(first["task_id"])["state"] == "failed"
        assert "expired" in service.status(first["task_id"])["message"]
        assert service._result_bytes <= reads.MAX_RESULT_BYTES
    finally:
        service.shutdown()


def test_status_expires_a_ready_result_without_filesystem_or_database_access(tmp_path, monkeypatch):
    from app.core import wechat_browser_read as reads

    service = WechatBrowserReadService(tmp_path, loader=lambda _: _page(), pin=lambda: "fixed", validator=lambda _: None)
    try:
        _, job = service.acquire({}, wait=False)
        assert _done(service, job["task_id"])["state"] == "succeeded"
        monkeypatch.setattr(reads, "RESULT_SECONDS", 0)
        service._pin_override = lambda: (_ for _ in ()).throw(AssertionError("status attempted I/O"))
        assert service.status(job["task_id"])["state"] == "failed"
        assert service._result_bytes == 0
    finally:
        service.shutdown()


def test_retry_does_not_consume_a_result_that_completes_before_post_returns(tmp_path, monkeypatch):
    calls = []
    expected = _page()

    def initially_invalid(_):
        raise ValueError("synthetic first attempt")

    service = WechatBrowserReadService(tmp_path, loader=lambda _: calls.append(1) or expected,
                                       pin=lambda: "fixed", validator=initially_invalid)
    try:
        _, failed = service.acquire({}, wait=False)
        assert _done(service, failed["task_id"])["state"] == "failed"
        service.validator = lambda _: None
        acquire = service.acquire

        def eagerly_finished(parameters, **options):
            options["wait"] = True
            return acquire(parameters, **options)

        monkeypatch.setattr(service, "acquire", eagerly_finished)
        retried = service.retry(failed["task_id"])
        assert retried["state"] == "succeeded"
        page, _ = acquire({})
        assert page is expected and calls == [1]
    finally:
        service.shutdown()


def test_source_change_during_ready_proof_inventory_cannot_transfer_old_page(tmp_path, monkeypatch):
    pin = ["before"]
    first = _page(marker="original")
    service = WechatBrowserReadService(tmp_path, loader=lambda _: first, pin=lambda: pin[0], validator=lambda _: None)
    try:
        _, job = service.acquire({}, wait=False)
        assert _done(service, job["task_id"])["state"] == "succeeded"
        changed = []

        def publication_during_proofs():
            if not changed:
                pin[0] = "published"
                changed.append(True)
            return ()

        monkeypatch.setattr(service, "_proofs", publication_during_proofs)
        result, replacement = service.acquire({}, wait=False)
        assert result is None
        assert replacement["task_id"] != job["task_id"]
        assert service.status(job["task_id"])["state"] == "failed"
    finally:
        service.shutdown()
