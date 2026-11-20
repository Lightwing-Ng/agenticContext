"""Compatible workers keep secret data out of argv and cooperate with cancellation.

Code version: v1.1.2-codex.0
"""

from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import stat
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from app.core import wechat_mac_refresh as refresh
from app.core import wechat_mac_refresh_worker as worker
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_mac_refresh_source import RefreshError, RefreshIssue, RefreshNotice


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


def test_worker_status_transports_a_known_failure_reason():
    failure = {
        **SUCCESS,
        "state": "failed",
        "message": RefreshIssue.DISK_SPACE.value,
        "partial": True,
    }
    assert worker._safe_result(json.dumps(failure)) == failure


@pytest.mark.parametrize(
    "mode,flags,size,expected",
    [
        (stat.S_IFREG, 0, worker.LARGE_BANK_BYTES, worker.MAX_LARGE_BANK_WORKER_SECONDS),
        (stat.S_IFREG, 0, worker.LARGE_BANK_BYTES - 1, worker.MAX_WORKER_SECONDS),
        (stat.S_IFLNK, 0, worker.LARGE_BANK_BYTES, worker.MAX_WORKER_SECONDS),
        (stat.S_IFREG, 0x40000000, worker.LARGE_BANK_BYTES, worker.MAX_WORKER_SECONDS),
    ],
)
def test_worker_budget_uses_only_a_large_resident_regular_bank(
    tmp_path, monkeypatch, mode, flags, size, expected,
):
    info = SimpleNamespace(st_mode=mode, st_flags=flags, st_size=size)
    monkeypatch.setattr(
        bank, "mac_recovery_bank_path", lambda _root: SimpleNamespace(lstat=lambda: info),
    )
    assert worker._worker_budget_seconds(tmp_path) == expected


def test_committed_reparse_count_survives_the_terminal_contract():
    assert worker._safe_result(json.dumps({**SUCCESS, "reparsed": 3})) == {
        **SUCCESS,
        "reparsed": 3,
    }


def test_worker_output_is_bounded(tmp_path, monkeypatch):
    process = Process(output=b"x" * (worker.MAX_OUTPUT_BYTES + 1))
    monkeypatch.setattr(
        worker, "_compatible_python", lambda *_args: "/controlled/python3"
    )
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(RefreshNotice) as error:
        worker.run_compatible_worker(tmp_path, SESSION, Event())
    assert error.value.issue == RefreshIssue.WORKER_UNAVAILABLE


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


def test_progress_reaches_parent_before_child_finishes(tmp_path, monkeypatch):
    """Use a real OS pipe so buffered whole-output reads cannot pass this test."""
    read_fd, write_fd = os.pipe()
    process = Process(running=True)
    process.stdout = os.fdopen(read_fd, "rb")
    progress_seen, release, returned = Event(), Event(), Event()
    progress = {
        "phase": "reading",
        "parts_total": 7,
        "part_current": 2,
        "parts_completed": 1,
        "checked": 1200,
        "candidates": 4,
    }
    observed, results, failures = [], [], []

    def produce():
        with os.fdopen(write_fd, "wb") as pipe:
            pipe.write(
                json.dumps({"type": "progress", "status": progress}).encode() + b"\n"
            )
            pipe.flush()
            assert release.wait(5)
            pipe.write(json.dumps(SUCCESS).encode() + b"\n")
            pipe.flush()
        process.returncode = 0

    def receive(value):
        observed.append(value)
        progress_seen.set()

    def run():
        try:
            results.append(
                worker.run_compatible_worker(
                    tmp_path, SESSION, Event(), on_progress=receive
                )
            )
        except BaseException as error:
            failures.append(error)
        finally:
            returned.set()

    monkeypatch.setattr(
        worker, "_compatible_python", lambda *_args: "/controlled/python3"
    )
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: process)
    producer, parent = Thread(target=produce), Thread(target=run)
    producer.start()
    parent.start()
    try:
        assert progress_seen.wait(3)
        assert observed == [progress]
        assert not returned.is_set() and process.returncode is None
    finally:
        release.set()
        producer.join(5)
        parent.join(5)
    assert returned.is_set() and not failures and results == [SUCCESS]
    assert process.stdin.closed and process.stdout.closed


def test_child_emits_changed_safe_progress_frames_and_member_snapshot_result(
    tmp_path, monkeypatch, capsys
):
    stopped = Event()
    states = iter(
        [
            {"state": "running", "phase": "members"},
            {
                "state": "running",
                "phase": "reading",
                "parts_total": 2,
                "part_current": 1,
                "checked": 0,
                "database_path": "/private/source",
            },
            {
                "state": "running",
                "phase": "comparing",
                "parts_total": 2,
                "part_current": 1,
                "checked": 100,
                "raw_key": "must-not-escape",
            },
            {**SUCCESS, "phase": "complete", "member_snapshot": "saved"},
        ]
    )

    class ParentInput:
        def __iter__(self):
            assert stopped.wait(3)
            return iter(())

    class Service:
        def __init__(self, root):
            assert root == str(tmp_path)

        def start(self, session):
            assert session == SESSION and not stopped.is_set()
            return {"task_id": "synthetic-child"}

        def status(self, token):
            assert token == "synthetic-child"
            return next(states)

        def shutdown(self, **_kwargs):
            stopped.set()

    monkeypatch.setattr(refresh, "WechatMacRefreshService", Service)
    monkeypatch.setattr(
        worker,
        "sys",
        SimpleNamespace(
            version_info=(3, 14),
            stdin=ParentInput(),
            argv=["worker", "--local-store", str(tmp_path), "--session", SESSION],
        ),
    )
    assert worker._main() == 0 and stopped.is_set()
    output = capsys.readouterr().out
    frames = [json.loads(line) for line in output.splitlines()]
    assert [frame["status"]["phase"] for frame in frames[:-1]] == [
        "members",
        "reading",
        "comparing",
    ]
    assert all(frame["type"] == "progress" for frame in frames[:-1])
    assert frames[-1] == {**SUCCESS, "phase": "complete", "member_snapshot": "saved"}
    assert "private/source" not in output and "must-not-escape" not in output


@pytest.mark.parametrize(
    "status",
    [
        {"phase": "/private/key-path"},
        {"phase": "reading", "checked": True},
        {"phase": "reading", "checked": -1},
        {"phase": "reading", "checked": 2**53},
        {"phase": "reading", "parts_total": 2, "part_current": 3},
        {"phase": "reading", "partial": "false"},
        {"phase": "members", "member_snapshot": "/private/contact-source"},
        {"phase": "reading", "database_path": "/private/key-path"},
    ],
)
def test_unsafe_progress_is_never_delivered_and_cancels_owned_worker(
    tmp_path, monkeypatch, status
):
    process = Process(
        output=json.dumps({"type": "progress", "status": status}).encode() + b"\n",
        running=True,
    )
    observed = []
    monkeypatch.setattr(
        worker, "_compatible_python", lambda *_args: "/controlled/python3"
    )
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(RefreshNotice) as caught:
        worker.run_compatible_worker(
            tmp_path, SESSION, Event(), on_progress=observed.append
        )
    assert caught.value.issue == RefreshIssue.WORKER_UNAVAILABLE
    assert not observed and "private/key-path" not in str(caught.value)
    assert b"cancel" in process.stdin.retained and process.stdout.closed


@pytest.mark.parametrize(
    "output",
    [
        json.dumps(SUCCESS).encode() + b"\n" + json.dumps(SUCCESS).encode() + b"\n",
        json.dumps(SUCCESS).encode()
        + b"\n"
        + b'{"type":"progress","status":{"phase":"reading"}}\n',
        b'{"type":"progress","status":{"phase":"reading"}}\n',
        b'{"type":"progress","status":{"phase":"reading"},"extra":"private"}\n',
        b"x" * (worker.MAX_FRAME_BYTES + 1),
    ],
)
def test_worker_stream_requires_one_final_result_after_bounded_frames(
    tmp_path, monkeypatch, output
):
    process = Process(output=output)
    monkeypatch.setattr(
        worker, "_compatible_python", lambda *_args: "/controlled/python3"
    )
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(RefreshNotice) as error:
        worker.run_compatible_worker(tmp_path, SESSION, Event())
    assert error.value.issue == RefreshIssue.WORKER_UNAVAILABLE


def test_progress_flood_has_a_cumulative_limit(tmp_path, monkeypatch):
    line = b'{"type":"progress","status":{"phase":"reading"}}\n'
    process = Process(output=line * (worker.MAX_OUTPUT_BYTES // len(line) + 1))
    monkeypatch.setattr(
        worker, "_compatible_python", lambda *_args: "/controlled/python3"
    )
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(RefreshNotice) as error:
        worker.run_compatible_worker(tmp_path, SESSION, Event())
    assert error.value.issue == RefreshIssue.WORKER_UNAVAILABLE


def test_progress_callback_failure_and_deadline_reap_owned_worker(
    tmp_path, monkeypatch
):
    output = b'{"type":"progress","status":{"phase":"reading"}}\n'
    monkeypatch.setattr(
        worker, "_compatible_python", lambda *_args: "/controlled/python3"
    )

    def fail_callback(_value):
        raise RuntimeError("private")

    for callback, deadline, issue in (
        (fail_callback, 600, RefreshIssue.WORKER_UNAVAILABLE),
        (None, -1, RefreshIssue.WORKER_TIMEOUT),
    ):
        process = Process(output=output, running=True)
        monkeypatch.setattr(
            worker.subprocess, "Popen", lambda *_args, **_kwargs: process
        )
        monkeypatch.setattr(worker, "MAX_WORKER_SECONDS", deadline)
        with pytest.raises(RefreshNotice) as error:
            worker.run_compatible_worker(
                tmp_path, SESSION, Event(), on_progress=callback
            )
        assert error.value.issue == issue
        assert b"cancel" in process.stdin.retained and process.stdout.closed
