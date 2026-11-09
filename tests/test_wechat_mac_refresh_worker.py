"""Compatible workers keep secret data out of argv and cooperate with cancellation.

Code version: v1.0.0-codex.0
"""

from io import BytesIO, StringIO
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from app.core import wechat_mac_refresh as refresh
from app.core import wechat_mac_refresh_worker as worker
from app.core.wechat_mac_refresh_source import RefreshError


SESSION = "wechat:" + "a" * 64
SUCCESS = {
    "state": "succeeded",
    "message": "Chat history refreshed.",
    "added": 2,
    "total": 504,
    "partial": False,
    "checked": 100,
}


class RecordingPipe(BytesIO):
    retained = b""

    def close(self):
        self.retained = self.getvalue()
        super().close()


class Process:
    def __init__(self, *, output=None, running=False, cancelled=None):
        self.stdin = RecordingPipe()
        self.stdout = BytesIO(
            json.dumps(SUCCESS).encode() if output is None else output
        )
        self.returncode = None if running else 0
        self.cancelled = cancelled
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.cancelled is not None:
            self.cancelled.set()
        if self.returncode is None and (
            self.stdin.closed or b"cancel" in self.stdin.getvalue()
        ):
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        assert self.stdin.closed or b"cancel" in self.stdin.getvalue()
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_worker_command_contains_only_server_scope_and_returns_safe_status(
    tmp_path, monkeypatch
):
    captured = {}
    process = Process()
    monkeypatch.setattr(
        worker, "_compatible_python", lambda *_args: "/controlled/python3"
    )

    def launch(arguments, **kwargs):
        captured.update(argv=arguments, options=kwargs)
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    assert worker.run_compatible_worker(tmp_path, SESSION, Event()) == SUCCESS
    assert captured["argv"] == [
        "/controlled/python3",
        "-B",
        "-m",
        "app.core.wechat_mac_refresh_worker",
        "--local-store",
        str(tmp_path),
        "--session",
        SESSION,
    ]
    assert captured["options"]["stderr"] == worker.subprocess.DEVNULL
    assert captured["options"]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert captured["options"]["cwd"] == Path(worker.__file__).resolve().parents[2]
    assert process.stdin.closed and process.stdout.closed


def test_cancelled_owned_worker_gets_cooperative_cleanup_and_cannot_report_success(
    tmp_path, monkeypatch
):
    cancelled = Event()
    process = Process(running=True, cancelled=cancelled)
    monkeypatch.setattr(
        worker, "_compatible_python", lambda *_args: "/controlled/python3"
    )
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(RefreshError, match="interrupted"):
        worker.run_compatible_worker(tmp_path, SESSION, cancelled)
    assert b"cancel" in process.stdin.retained
    assert not process.terminated and not process.killed


def test_reader_thread_allocation_failure_cancels_and_reaps_the_owned_child(
    tmp_path, monkeypatch
):
    process = Process(running=True)
    monkeypatch.setattr(
        worker, "_compatible_python", lambda *_args: "/controlled/python3"
    )
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: process)

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("Synthetic reader allocation failure")

        def is_alive(self):
            return False

    monkeypatch.setattr(worker, "Thread", BrokenThread)
    with pytest.raises(RuntimeError, match="allocation"):
        worker.run_compatible_worker(tmp_path, SESSION, Event())
    assert process.returncode == 0 and process.stdin.closed and process.stdout.closed
    assert b"cancel" in process.stdin.retained
    assert not process.terminated


@pytest.mark.parametrize(
    "value",
    [
        {**SUCCESS, "message": "Synthetic private path must not pass through"},
        {**SUCCESS, "partial": "false"},
        {**SUCCESS, "added": -1},
        {**SUCCESS, "total": True},
        {**SUCCESS, "state": "running"},
    ],
)
def test_worker_status_contract_rejects_unsafe_fields(value):
    with pytest.raises(RefreshError):
        worker._safe_result(json.dumps(value))


def test_worker_output_is_bounded(tmp_path, monkeypatch):
    process = Process(output=b"x" * (worker.MAX_OUTPUT_BYTES + 1))
    monkeypatch.setattr(
        worker, "_compatible_python", lambda *_args: "/controlled/python3"
    )
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(RefreshError, match="interrupted"):
        worker.run_compatible_worker(tmp_path, SESSION, Event())


def test_interpreter_probe_tries_older_path_then_a_compatible_platform_candidate(
    tmp_path, monkeypatch
):
    first, second = tmp_path / "older-python3", tmp_path / "newer-python3"
    for path in (first, second):
        path.write_text("synthetic executable, never run")
        path.chmod(0o700)
    monkeypatch.delenv("AGENTIC_CONTEXT_WECHAT_PYTHON", raising=False)
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        worker.shutil,
        "which",
        lambda value: (
            str(first)
            if value == "python3"
            else str(second)
            if value == "/usr/local/bin/python3"
            else None
        ),
    )
    probes = []

    def probe(argv, **kwargs):
        probes.append(argv)
        assert "sys.version_info[:2] < (3, 14)" in argv[-1]
        assert "import compression.zstd" in argv[-1]
        assert "import WechatMacRefreshService" in argv[-1]
        assert kwargs["stdout"] == kwargs["stderr"] == worker.subprocess.DEVNULL
        return SimpleNamespace(returncode=0 if argv[0] == str(second) else 1)

    monkeypatch.setattr(worker.subprocess, "run", probe)
    assert worker._compatible_python(tmp_path, {}) == str(second)
    assert [call[0] for call in probes] == [str(first), str(second)]


def test_explicit_unsupported_interpreter_does_not_silently_fallback(
    tmp_path, monkeypatch
):
    executable = tmp_path / "explicit-python"
    executable.write_text("synthetic executable, never run")
    executable.chmod(0o700)
    monkeypatch.setenv("AGENTIC_CONTEXT_WECHAT_PYTHON", str(executable))
    calls = []
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda argv, **_kwargs: calls.append(argv) or SimpleNamespace(returncode=1),
    )
    with pytest.raises(RefreshError, match="interpreter"):
        worker._compatible_python(tmp_path, {})
    assert len(calls) == 1 and calls[0][0] == str(executable)


def test_child_stdin_eof_cancels_instead_of_leaving_an_orphan(
    tmp_path, monkeypatch, capsys
):
    stopped = Event()

    class Service:
        def __init__(self, root):
            assert root == str(tmp_path)

        def start(self, session):
            assert session == SESSION and stopped.wait(2)
            return {"task_id": "synthetic-child-task"}

        def status(self, token):
            assert token == "synthetic-child-task"
            return {
                "state": "failed",
                "message": worker._FAILED_MESSAGE,
                "added": 0,
                "total": 0,
                "partial": True,
                "checked": 0,
            }

        def shutdown(self, **_kwargs):
            stopped.set()

    monkeypatch.setattr(refresh, "WechatMacRefreshService", Service)
    monkeypatch.setattr(
        worker,
        "sys",
        SimpleNamespace(
            version_info=(3, 14),
            stdin=StringIO(""),
            argv=["worker", "--local-store", str(tmp_path), "--session", SESSION],
        ),
    )
    assert worker._main() == 0
    assert stopped.is_set()
    assert json.loads(capsys.readouterr().out)["state"] == "failed"
