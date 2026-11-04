"""Application entrypoint for agenticContext."""

# Code version: v1.7.0-codex.0

from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any


LOGGER = logging.getLogger(__name__)
# Flask debug mode: templates reload in place and Python source changes restart the server.
DEBUG = True
# Werkzeug's serving child requests a restart with this exit status.
RELOAD_EXIT_STATUS = 3
# Tests, tooling, and runtime data never change the running service.
RELOAD_EXCLUDED_DIRECTORIES = ("tests", "scripts", "local_store", "logs")
RECOVERY_EXCLUDED_DIRECTORIES = frozenset(
    (*RELOAD_EXCLUDED_DIRECTORIES, ".git", ".venv", "venv", "__pycache__", "node_modules")
)


def _configure_bytecode_cache() -> None:
    """Keep optional bytecode reads outside a cloud-backed source checkout."""
    if sys.pycache_prefix is not None:
        return
    if sys.platform == "darwin":
        cache_root = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        cache_root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Cache"
    else:
        cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    sys.pycache_prefix = str(cache_root / "agenticContext" / "pycache")


def _stop_runtime_services(app: Any) -> None:
    """Cancel derived work and stop browser owners before the host process exits."""
    # Executor exit hooks run before atexit, so cancel an already-loaded worker here.
    contact_analysis = sys.modules.get("app.core.wechat_contact_analysis")
    cancel_contact_jobs = getattr(contact_analysis, "cancel_contact_analysis_jobs", None)
    if callable(cancel_contact_jobs):
        try:
            cancel_contact_jobs()
        except Exception as exc:
            LOGGER.error("Could not cancel contact analysis during shutdown: %s", exc)
    extensions = getattr(app, "extensions", {})
    runtime_shutdown = (
        extensions.get("runtime_shutdown")
        if isinstance(extensions, dict)
        else None
    )
    if callable(runtime_shutdown):
        try:
            runtime_shutdown()
        except Exception as exc:
            LOGGER.error("Could not stop browser services during shutdown: %s", exc)
        return
    for key in ("jury_service", "agent_session_pool", "iphone_service"):
        service = extensions.get(key) if isinstance(extensions, dict) else None
        stop_at_exit = getattr(service, "stop_at_exit", None)
        if not callable(stop_at_exit):
            continue
        try:
            stop_at_exit()
        except Exception as exc:
            LOGGER.error("Could not stop %s during service shutdown: %s", key, exc)


def _install_shutdown_signal_handlers(app: Any) -> None:
    """Route normal interrupt and termination signals through browser cleanup."""
    shutdown_started = False

    def handle_shutdown(signum: int, _frame: Any) -> None:
        nonlocal shutdown_started
        if shutdown_started:
            raise SystemExit(128 + signum)
        shutdown_started = True
        _stop_runtime_services(app)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)


def _enable_chatgpt_tunnel(app: Any, port: int) -> None:
    """Let the saved ChatGPT Tunnel reach this service's loopback MCP endpoint."""
    tunnel_runtime = app.extensions.get("tunnel_runtime")
    if tunnel_runtime is None:
        return
    try:
        tunnel_runtime.enable(f"http://127.0.0.1:{port}/mcp")
    except Exception as exc:
        LOGGER.error("Could not start the ChatGPT Tunnel: %s", exc)


def _reload_placeholder(_environ: Any, start_response: Any) -> list[bytes]:
    """Own the supervisor's listening socket; only the serving child accepts requests."""
    start_response("503 Service Unavailable", [("Content-Type", "text/plain; charset=utf-8")])
    return [b"Restarting"]


def _reload_source_snapshot(roots: tuple[Path, ...]) -> dict[str, tuple[int, ...]]:
    """Watch Python source metadata without reading runtime data or following links."""
    snapshot = {}

    def remember(path: Path) -> None:
        try:
            if path.is_symlink() or not path.is_file():
                return
            state = path.stat()
        except OSError:
            return
        snapshot[str(path)] = (
            state.st_dev,
            state.st_ino,
            state.st_size,
            state.st_mtime_ns,
            state.st_ctime_ns,
        )

    for root in roots:
        if root.is_symlink():
            continue
        if root.suffix == ".py":
            remember(root)
            continue
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = [
                name
                for name in directories
                if name not in RECOVERY_EXCLUDED_DIRECTORIES
                and not (Path(directory) / name).is_symlink()
            ]
            for name in filenames:
                if name.endswith(".py"):
                    remember(Path(directory) / name)
    return snapshot


def _supervise_reloading_server(
    host: str,
    port: int,
    *,
    source_root: Path | None = None,
    poll_interval: float = 0.25,
) -> int:
    """Hold the listening socket and restart the serving child after source changes.

    Wait for browser cleanup before restarting. A failed startup waits for an
    actual source change while the supervisor serves a plain unavailable response.
    """
    from werkzeug.serving import make_server

    if not 0 < poll_interval <= 5:
        raise ValueError("The recovery poll interval must be between zero and five seconds.")
    project_root = Path(__file__).resolve().parent
    watched_roots = (
        (Path(source_root),)
        if source_root is not None
        else (project_root / "main.py", project_root / "app")
    )
    server = make_server(host, port, _reload_placeholder, threaded=True)
    server.timeout = poll_interval
    server.socket.set_inheritable(True)
    server.log_startup()
    environment = {
        **os.environ,
        "WERKZEUG_RUN_MAIN": "true",
        "WERKZEUG_SERVER_FD": str(server.fileno()),
    }
    child: subprocess.Popen[bytes] | None = None
    stopping = False
    stop_signal: int | None = None

    def forward_termination(signum: int, _frame: Any) -> None:
        nonlocal stopping, stop_signal
        stopping = True
        stop_signal = signum
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    def stop_after_interrupt(signum: int, _frame: Any) -> None:
        nonlocal stopping, stop_signal
        stopping = True
        stop_signal = signum

    signal.signal(signal.SIGTERM, forward_termination)
    # A terminal interrupt already reaches the child; forwarding it would abort its cleanup.
    signal.signal(signal.SIGINT, stop_after_interrupt)
    status = 0
    try:
        while not stopping:
            source_snapshot = _reload_source_snapshot(watched_roots)
            child = subprocess.Popen([sys.executable, *sys.argv], env=environment, close_fds=False)
            if stopping:
                # Termination arrived while this child was starting.
                child.send_signal(signal.SIGTERM)
            status = child.wait()
            if stopping or status in {
                0,
                -signal.SIGINT,
                -signal.SIGTERM,
                128 + signal.SIGINT,
                128 + signal.SIGTERM,
            }:
                break
            if status == RELOAD_EXIT_STATUS:
                continue
            LOGGER.error(
                "The serving process exited with status %s. Waiting for a Python source "
                "change before restarting; requests return 503 meanwhile.",
                status,
            )
            while not stopping and _reload_source_snapshot(watched_roots) == source_snapshot:
                server.handle_request()
    finally:
        server.server_close()
    if stop_signal is not None:
        return 128 + stop_signal
    return status if status >= 0 else 128 - status


def _start_web_console() -> None:
    """Start the local web console with the resolved host Python runtime."""
    from app.core.config import DEFAULT_HOST, DEFAULT_PORT
    from werkzeug.serving import is_running_from_reloader

    if DEBUG and not is_running_from_reloader():
        # The supervisor must not build services or start the Tunnel a second time.
        raise SystemExit(_supervise_reloading_server(DEFAULT_HOST, DEFAULT_PORT))

    from app.core.logging_setup import configure_logging
    from app.core.version import APP_VERSION
    from app.web.app import create_app

    configure_logging(APP_VERSION)
    app = create_app()
    _install_shutdown_signal_handlers(app)
    _enable_chatgpt_tunnel(app, DEFAULT_PORT)
    project_root = Path(__file__).resolve().parent
    try:
        app.run(
            host=DEFAULT_HOST,
            port=DEFAULT_PORT,
            debug=DEBUG,
            # The interactive debugger must not sit in front of the LAN session gate.
            use_debugger=DEBUG and DEFAULT_HOST == "127.0.0.1",
            threaded=True,
            exclude_patterns=[
                str(project_root / directory / "*") for directory in RELOAD_EXCLUDED_DIRECTORIES
            ],
        )
    finally:
        # A reload exits through Werkzeug instead of the shutdown signal handlers.
        _stop_runtime_services(app)


def main() -> None:
    """Start the local web console."""
    _configure_bytecode_cache()
    _start_web_console()


if __name__ == "__main__":
    main()
