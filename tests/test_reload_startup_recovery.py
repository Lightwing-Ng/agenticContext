"""Development reload failures wait for source repair without losing the supervisor.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX inherited sockets and process groups are required.",
)

_APP_SOURCE = """
def create_app():
    def application(_environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"Recovered synthetic service"]
    return application
"""

_SERVICE_SOURCE = """
import os
from pathlib import Path
import signal
import sys
import time

import main
import werkzeug.serving

root, marker = map(Path, sys.argv[1:3])


def record(event):
    with marker.open("a", encoding="utf-8") as output:
        output.write(event + "\\n")


if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    make_server = werkzeug.serving.make_server

    def observed_server(*args, **kwargs):
        server = make_server(*args, **kwargs)
        record("listening:" + str(server.socket.getsockname()[1]))
        return server

    werkzeug.serving.make_server = observed_server
    raise SystemExit(main._supervise_reloading_server(
        "127.0.0.1", 0, source_root=root, poll_interval=0.03,
    ))

sys.path.insert(0, str(root))
record("launched")
try:
    import startup_target
    application = startup_target.create_app()
except Exception:
    record("startup-failed")
    raise

server = werkzeug.serving.make_server(
    "127.0.0.1", 0, application, threaded=True,
    fd=int(os.environ["WERKZEUG_SERVER_FD"]),
)


def stop(signum, _frame):
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
record("ready:" + str(server.socket.getsockname()[1]))
try:
    server.serve_forever()
finally:
    time.sleep(0.2)
    server.server_close()
    record("cleaned")
"""


def _events(runtime):
    return (
        runtime.marker.read_text(encoding="utf-8").splitlines()
        if runtime.marker.exists()
        else []
    )


def _diagnostic(runtime):
    return runtime.log.read_text(encoding="utf-8", errors="replace")[-4000:]


def _wait_for(runtime, predicate, description, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = _events(runtime)
        if predicate(events):
            return events
        assert runtime.process.poll() is None, _diagnostic(runtime)
        time.sleep(0.01)
    pytest.fail(
        f"Timed out waiting for {description}: {_events(runtime)!r}\n{_diagnostic(runtime)}"
    )


def _assert_waiting(runtime):
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        assert runtime.process.poll() is None, _diagnostic(runtime)
        assert _events(runtime).count("launched") == 1, _events(runtime)
        time.sleep(0.01)


def _port(events, prefix):
    return int(
        next(event.split(":", 1)[1] for event in events if event.startswith(prefix))
    )


@contextmanager
def _service(tmp_path, failure):
    root = tmp_path / "watched"
    root.mkdir()
    target = root / "startup_target.py"
    repair = target
    if failure == "import_error":
        target.write_text(
            "from startup_dependency import create_app\n", encoding="utf-8"
        )
        repair = root / "startup_dependency.py"
        repair.write_text("UNFINISHED = True\n", encoding="utf-8")
    elif failure == "syntax_error":
        target.write_text("def create_app(:\n", encoding="utf-8")
    elif failure == "factory_error":
        target.write_text(
            "def create_app():\n    raise RuntimeError('Synthetic startup failure')\n",
            encoding="utf-8",
        )
    elif failure == "normal_exit":
        target.write_text(
            "def create_app():\n    raise SystemExit(0)\n", encoding="utf-8"
        )
    elif failure == "blocked_error":
        target.write_text(
            "def create_app():\n"
            "    import __main__\n"
            "    from pathlib import Path\n"
            "    import time\n"
            "    __main__.record('startup-blocked')\n"
            "    while not Path(__file__).with_suffix('.release').exists():\n"
            "        time.sleep(0.01)\n"
            "    raise RuntimeError('Synthetic delayed startup failure')\n",
            encoding="utf-8",
        )
    else:
        assert failure == "missing_module"
    script = tmp_path / "recovery_service.py"
    script.write_text(_SERVICE_SOURCE, encoding="utf-8")
    marker = tmp_path / "events.txt"
    log = tmp_path / "service.log"
    project = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": str(project),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    environment.pop("WERKZEUG_RUN_MAIN", None)
    environment.pop("WERKZEUG_SERVER_FD", None)
    with log.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, str(script), str(root), str(marker)],
            cwd=tmp_path,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        runtime = SimpleNamespace(
            process=process, root=root, repair=repair, marker=marker, log=log
        )
        try:
            yield runtime
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            # Remove only descendants belonging to this disposable process group.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(
    "failure", ["import_error", "syntax_error", "factory_error", "missing_module"]
)
def test_startup_failure_waits_for_real_source_fix_then_recovers_same_socket(
    tmp_path, failure
):
    with _service(tmp_path, failure) as runtime:
        events = _wait_for(
            runtime,
            lambda events: "startup-failed" in events,
            "initial startup failure",
        )
        listening_port = _port(events, "listening:")
        _assert_waiting(runtime)
        ignored = runtime.root / "tests"
        ignored.mkdir()
        (ignored / "test_unrelated.py").write_text("CHANGED = True\n", encoding="utf-8")
        _assert_waiting(runtime)
        with pytest.raises(HTTPError) as unavailable:
            urlopen(f"http://127.0.0.1:{listening_port}/", timeout=3)
        assert unavailable.value.code == 503
        assert unavailable.value.read() == b"Restarting"
        replacement = runtime.root / "replacement.tmp"
        replacement.write_text(_APP_SOURCE, encoding="utf-8")
        replacement.replace(runtime.repair)
        events = _wait_for(
            runtime,
            lambda events: any(event.startswith("ready:") for event in events),
            "recovered service",
        )
        assert _port(events, "ready:") == listening_port
        with urlopen(f"http://127.0.0.1:{listening_port}/", timeout=3) as response:
            assert response.status == 200
            assert response.read() == b"Recovered synthetic service"
        runtime.process.send_signal(signal.SIGTERM)
        assert runtime.process.wait(timeout=5) == 128 + signal.SIGTERM
        events = _events(runtime)
        assert events.count("launched") == 2
        assert events.count("startup-failed") == 1
        assert events[-1] == "cleaned"


@pytest.mark.parametrize(
    "signum,whole_group", [(signal.SIGTERM, False), (signal.SIGINT, True)]
)
def test_manual_stop_while_waiting_for_source_fix_exits_without_respawning(
    tmp_path, signum, whole_group
):
    with _service(tmp_path, "import_error") as runtime:
        _wait_for(
            runtime,
            lambda events: "startup-failed" in events,
            "initial startup failure",
        )
        _assert_waiting(runtime)
        if whole_group:
            os.killpg(runtime.process.pid, signum)
        else:
            runtime.process.send_signal(signum)
        assert runtime.process.wait(timeout=3) == 128 + signum
        assert _events(runtime).count("launched") == 1
        assert not any(event.startswith("ready:") for event in _events(runtime))


def test_explicit_success_exit_still_closes_the_reload_supervisor(tmp_path):
    with _service(tmp_path, "normal_exit") as runtime:
        assert runtime.process.wait(timeout=5) == 0
        assert _events(runtime).count("launched") == 1
        assert "startup-failed" not in _events(runtime)


def test_source_fix_before_failed_child_exit_is_not_lost(tmp_path):
    with _service(tmp_path, "blocked_error") as runtime:
        _wait_for(
            runtime, lambda events: "startup-blocked" in events, "blocked startup"
        )
        replacement = runtime.root / "replacement.tmp"
        replacement.write_text(_APP_SOURCE, encoding="utf-8")
        replacement.replace(runtime.repair)
        runtime.repair.with_suffix(".release").touch()
        events = _wait_for(
            runtime,
            lambda events: any(event.startswith("ready:") for event in events),
            "source fix made before child exit",
        )
        assert events.count("launched") == 2
        assert events.count("startup-failed") == 1
        assert _port(events, "ready:") == _port(events, "listening:")
        runtime.process.send_signal(signal.SIGTERM)
        assert runtime.process.wait(timeout=5) == 128 + signal.SIGTERM
        assert _events(runtime)[-1] == "cleaned"
