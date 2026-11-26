"""Exercise durable web refresh status without reading native WeChat data.

Code version: v1.0.2-codex.0
"""

from hashlib import sha256
from io import BytesIO
from threading import Event
from types import SimpleNamespace

from flask import Blueprint, Flask
import pytest

from app.core import wechat_mac_refresh as refresh
from app.core import wechat_refresh_receipts as receipts
from app.core.owner_only_files import owner_only_directory_problem, owner_only_problem
from app.web.wechat_refresh_routes import WECHAT_REFRESH_EXTENSION, register_wechat_refresh_routes


_ACCOUNT = "synthetic_refresh_scope"


def _service(root, monkeypatch, *, durable=True):
    service = refresh.WechatMacRefreshService(root, platform="darwin", durable_status=durable)
    monkeypatch.setattr(service, "account_available", lambda owner: owner == _ACCOUNT)
    return service


def _client(service):
    app = Flask(__name__)
    app.extensions[WECHAT_REFRESH_EXTENSION] = service
    blueprint = Blueprint("local_resources", __name__)
    register_wechat_refresh_routes(blueprint)
    app.register_blueprint(blueprint)
    return app.test_client()


@pytest.mark.parametrize("layout", ("current", "legacy", "empty"))
def test_live_receipt_reads_only_beyond_windows_lock_byte(tmp_path, monkeypatch, layout):
    class MandatoryLockReader(BytesIO):
        def read(self, size=-1):
            if self.tell() == 0:
                raise OSError("Synthetic Windows byte-range lock violation")
            return super().read(size)

    def locked(*_args, **_kwargs):
        raise BlockingIOError("Synthetic existing lease")

    token = "x" * 32
    store = receipts.WechatRefreshReceipts(tmp_path / "receipts")
    key = sha256(token.encode()).hexdigest().encode()
    with store.claim(token) as lease:
        lease.seek(0)
        marker = lease.read()
        assert marker == b"\x00" + key
        if layout == "legacy":
            marker = key
        elif layout == "empty":
            marker = b""
        store.write(token, {
            "state": "running", "phase": "preparing", "added": 0, "total": 0,
            "message": "Refreshing chat history.",
        }, publication_possible=False)
        monkeypatch.setattr(store, "_open_active", lambda: MandatoryLockReader(marker))
        monkeypatch.setattr(receipts, "lock_file", locked)
        status = store.read(token)
        assert status["state"] == ("interrupted" if layout == "empty" else "running")


@pytest.mark.parametrize(
    ("phase", "publication_possible", "unknown"),
    [
        ("preparing", False, False),
        ("preparing", True, True),
        ("members", True, True),
        ("saving", False, True),
        ("verifying", False, False),
    ],
)
def test_restarted_route_reports_orphaned_receipts_without_rerunning(
    tmp_path, monkeypatch, phase, publication_possible, unknown,
):
    class DeferredThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(refresh, "Thread", DeferredThread)
    original = _service(tmp_path, monkeypatch)
    restarted = _service(tmp_path, monkeypatch)
    original_client = _client(original)
    restarted_client = _client(restarted)
    started = original_client.post("/api/browser/wechat/refresh/account", json={"account_id": _ACCOUNT})
    assert started.status_code == 202
    status_url = started.json["status_url"]
    token = status_url.rsplit("/", 1)[-1]
    lease = original._tasks[token]["lease"]
    try:
        receipt = original._receipts.directory / (sha256(token.encode()).hexdigest() + ".json")
        assert not owner_only_problem(receipt)
        assert not owner_only_directory_problem(receipt.parent)
        assert _ACCOUNT not in receipt.read_text() and token not in receipt.read_text()
        assert restarted_client.get(status_url).json["state"] == "running"
        assert restarted_client.post(
            "/api/browser/wechat/refresh/account", json={"account_id": _ACCOUNT},
        ).status_code == 409
        original._update(token, phase=phase, publication_possible=publication_possible)
    finally:
        lease.close()

    status = restarted_client.get(status_url)
    assert status.status_code == 200
    assert status.cache_control.no_store
    assert status.json["state"] == "interrupted"
    assert status.json["phase"] == phase
    assert status.json["publication_unknown"] is unknown
    assert status.json["partial"] is True
    assert ("check for updates" in status.json["message"]) is unknown
    assert not restarted._tasks
    assert restarted_client.get("/api/browser/wechat/refresh/" + "x" * 32).status_code == 404


def test_completed_receipt_preserves_verified_outcome_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "sys", SimpleNamespace(version_info=(3, 14)))
    service = _service(tmp_path, monkeypatch)

    def finish(token, _owner):
        service._update(token, phase="saving")
        service._update(
            token, state="succeeded", phase="complete", added=7, upgraded=3, reparsed=2,
            message="Chat history refreshed.",
        )

    monkeypatch.setattr(service, "_run_account", finish)
    result = _client(service).post("/api/browser/wechat/refresh/account", json={"account_id": _ACCOUNT})
    assert result.status_code == 202
    service.shutdown()
    restarted = _client(_service(tmp_path, monkeypatch))
    status = restarted.get(result.json["status_url"])
    assert status.status_code == 200
    assert status.json["state"] == "succeeded"
    assert status.json["phase"] == "complete"
    assert (status.json["added"], status.json["upgraded"], status.json["reparsed"]) == (7, 3, 2)
    assert "publication_unknown" not in status.json


def test_live_worker_refuses_to_publish_without_durable_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "sys", SimpleNamespace(version_info=(3, 14)))
    service = _service(tmp_path, monkeypatch)
    write = service._receipts.write

    def refuse_checkpoint(token, snapshot, *, publication_possible):
        if publication_possible:
            raise OSError("Synthetic receipt write failure")
        write(token, snapshot, publication_possible=publication_possible)

    def finish(token, _owner):
        service._update(token, phase="saving")
        pytest.fail("Publication must wait for a durable checkpoint.")

    monkeypatch.setattr(service._receipts, "write", refuse_checkpoint)
    monkeypatch.setattr(service, "_run_account", finish)
    started = service.start_account(_ACCOUNT)
    service.shutdown()
    status = _service(tmp_path, monkeypatch).status(started["task_id"])
    assert status["state"] == "failed"
    assert status["phase"] == "preparing"


def test_graceful_shutdown_preserves_interrupted_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "sys", SimpleNamespace(version_info=(3, 14)))
    service = _service(tmp_path, monkeypatch)
    saving = Event()

    def wait_at_publication(token, _owner):
        service._update(token, phase="saving")
        saving.set()
        if service._shutdown.wait(timeout=5):
            service._check_running()

    monkeypatch.setattr(service, "_run_account", wait_at_publication)
    started = service.start_account(_ACCOUNT)
    try:
        assert saving.wait(timeout=5)
    finally:
        service.shutdown()
    status = _service(tmp_path, monkeypatch).status(started["task_id"])
    assert status["state"] == "interrupted"
    assert status["phase"] == "saving"
    assert status["publication_unknown"] is True


def test_worker_instances_keep_status_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "sys", SimpleNamespace(version_info=(3, 14)))
    service = _service(tmp_path, monkeypatch, durable=False)

    def finish(token, _owner):
        service._update(token, state="succeeded", phase="complete")

    monkeypatch.setattr(service, "_run_account", finish)
    started = service.start_account(_ACCOUNT)
    service.shutdown()
    assert service.status(started["task_id"])["state"] == "succeeded"
    assert _service(tmp_path, monkeypatch).status(started["task_id"]) is None
    assert list(tmp_path.iterdir()) == []



def test_publication_failure_logs_only_bounded_diagnostics(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(refresh, "sys", SimpleNamespace(version_info=(3, 14)))
    service = _service(tmp_path, monkeypatch)

    def fail(token, _owner):
        service._update(token, phase="saving")
        raise OSError(5, "private-source-error-text", "/private/source-database")

    monkeypatch.setattr(service, "_run_account", fail)
    started = service.start_account(_ACCOUNT)
    service.shutdown()

    result = service.status(started["task_id"])
    assert result["message"] == refresh.FAILED_MESSAGE
    assert "filesystem, code=5, step=app.core.wechat_mac_refresh:" in caplog.text
    assert "private-source-error-text" not in caplog.text
    assert "/private/source-database" not in caplog.text
