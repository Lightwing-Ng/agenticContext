"""Application entrypoint for agenticContext."""

# Code version: v1.6.0-codex.0

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


def _stop_runtime_services(app: Any) -> None:
    """Stop browser-owning services before the host process exits."""
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
    for key in ("jury_service", "agent_session_pool"):
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


def _supervise_reloading_server(host: str, port: int) -> int:
    """Hold the listening socket and restart the serving child after source changes.

    Werkzeug's own supervisor kills the child shortly after an interrupt or termination,
    which would cut browser cleanup short. This one waits for the child to finish.
    """
    from werkzeug.serving import make_server

    server = make_server(host, port, _reload_placeholder, threaded=True)
    server.socket.set_inheritable(True)
    server.log_startup()
    environment = {
        **os.environ,
        "WERKZEUG_RUN_MAIN": "true",
        "WERKZEUG_SERVER_FD": str(server.fileno()),
    }
    child: subprocess.Popen[bytes] | None = None
    stopping = False

    def forward_termination(signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_termination)
    # A terminal interrupt already reaches the child; forwarding it would abort its cleanup.
    signal.signal(signal.SIGINT, lambda _signum, _frame: None)
    status = 0
    try:
        while not stopping:
            child = subprocess.Popen([sys.executable, *sys.argv], env=environment, close_fds=False)
            if stopping:
                # Termination arrived while this child was starting.
                child.send_signal(signal.SIGTERM)
            status = child.wait()
            if status != RELOAD_EXIT_STATUS:
                break
    finally:
        server.server_close()
    if stopping and status == RELOAD_EXIT_STATUS:
        return 0
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
    _start_web_console()


if __name__ == "__main__":
    main()
