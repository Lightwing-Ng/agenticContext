"""Application entrypoint for agenticContext."""

# Code version: v1.4.0-codex.1

from __future__ import annotations

import logging
import signal
from typing import Any


LOGGER = logging.getLogger(__name__)


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


def _start_web_console() -> None:
    """Start the local web console with the resolved host Python runtime."""
    from app.core.config import DEFAULT_HOST, DEFAULT_PORT
    from app.core.logging_setup import configure_logging
    from app.core.version import APP_VERSION
    from app.web.app import create_app

    configure_logging(APP_VERSION)
    app = create_app()
    _install_shutdown_signal_handlers(app)
    app.run(host=DEFAULT_HOST, port=DEFAULT_PORT, debug=False, threaded=True)


def main() -> None:
    """Start the local web console."""
    _start_web_console()


if __name__ == "__main__":
    main()
