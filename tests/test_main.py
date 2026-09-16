"""Focused regression tests for the application entrypoint.

Code version: v1.4.0-codex.1
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import types
import unittest
from unittest.mock import Mock, patch

import main


class MainEntrypointTests(unittest.TestCase):
    """Validate runtime-agnostic startup and the normal Flask configuration."""

    def test_start_web_console_preserves_existing_flask_configuration(self) -> None:
        """The supported-runtime startup uses the established app settings."""
        configure_logging = Mock()
        create_app = Mock()
        app = Mock()
        create_app.return_value = app
        runtime_modules = {
            "app.core.config": types.SimpleNamespace(DEFAULT_HOST="0.0.0.0", DEFAULT_PORT=8666),
            "app.core.logging_setup": types.SimpleNamespace(configure_logging=configure_logging),
            "app.core.version": types.SimpleNamespace(APP_VERSION="v1.4.0"),
            "app.web.app": types.SimpleNamespace(create_app=create_app),
        }

        with patch.dict(sys.modules, runtime_modules), patch.object(
            main,
            "_install_shutdown_signal_handlers",
        ) as install_shutdown_handlers:
            main._start_web_console()

        configure_logging.assert_called_once_with("v1.4.0")
        create_app.assert_called_once_with()
        install_shutdown_handlers.assert_called_once_with(app)
        app.run.assert_called_once_with(host="0.0.0.0", port=8666, debug=False, threaded=True)

    def test_sigterm_stops_browser_services_before_process_exit(self) -> None:
        """Normal service termination cleans Safari owners before exiting."""
        events = []
        jury_service = Mock()
        jury_service.stop_at_exit.side_effect = lambda: events.append("jury")
        agent_session_pool = Mock()
        agent_session_pool.stop_at_exit.side_effect = lambda: events.append("agent")
        app = types.SimpleNamespace(
            extensions={
                "jury_service": jury_service,
                "agent_session_pool": agent_session_pool,
            }
        )
        handlers = {}

        with patch.object(
            main.signal,
            "signal",
            side_effect=lambda signum, handler: handlers.__setitem__(signum, handler),
        ):
            main._install_shutdown_signal_handlers(app)

        with self.assertRaises(SystemExit) as raised:
            handlers[main.signal.SIGTERM](main.signal.SIGTERM, None)

        self.assertEqual(raised.exception.code, 128 + main.signal.SIGTERM)
        self.assertEqual(events, ["jury", "agent"])

    def test_sigterm_prefers_the_shared_once_only_runtime_shutdown(self) -> None:
        """Signal cleanup uses the same callback later invoked by atexit."""
        runtime_shutdown = Mock()
        jury_service = Mock()
        agent_session_pool = Mock()
        app = types.SimpleNamespace(
            extensions={
                "runtime_shutdown": runtime_shutdown,
                "jury_service": jury_service,
                "agent_session_pool": agent_session_pool,
            }
        )
        handlers = {}

        with patch.object(
            main.signal,
            "signal",
            side_effect=lambda signum, handler: handlers.__setitem__(signum, handler),
        ):
            main._install_shutdown_signal_handlers(app)

        with self.assertRaises(SystemExit):
            handlers[main.signal.SIGTERM](main.signal.SIGTERM, None)

        runtime_shutdown.assert_called_once_with()
        jury_service.stop_at_exit.assert_not_called()
        agent_session_pool.stop_at_exit.assert_not_called()

    def test_sigint_stops_browser_services_and_exits_with_interrupt_status(self) -> None:
        """Keyboard interruption cannot bypass the shared browser cleanup."""
        runtime_shutdown = Mock()
        app = types.SimpleNamespace(extensions={"runtime_shutdown": runtime_shutdown})
        handlers = {}

        with patch.object(
            main.signal,
            "signal",
            side_effect=lambda signum, handler: handlers.__setitem__(signum, handler),
        ):
            main._install_shutdown_signal_handlers(app)

        with self.assertRaises(SystemExit) as raised:
            handlers[main.signal.SIGINT](main.signal.SIGINT, None)

        self.assertEqual(raised.exception.code, 128 + main.signal.SIGINT)
        runtime_shutdown.assert_called_once_with()

    @unittest.skipUnless(os.name == "posix", "POSIX signal delivery is required.")
    def test_real_sigterm_runs_browser_cleanup_before_subprocess_exit(self) -> None:
        """The runtime entrypoint handles an actual service termination signal."""
        with TemporaryDirectory() as temporary_directory:
            marker = Path(temporary_directory) / "shutdown-events.txt"
            script = f"""
import signal
import types
from pathlib import Path
import main

marker = Path({str(marker)!r})

class Service:
    def __init__(self, label):
        self.label = label

    def stop_at_exit(self):
        with marker.open("a", encoding="utf-8") as handle:
            handle.write(self.label + "\\n")

app = types.SimpleNamespace(
    extensions={{
        "jury_service": Service("jury"),
        "agent_session_pool": Service("agent"),
    }}
)
main._install_shutdown_signal_handlers(app)
marker.write_text("ready\\n", encoding="utf-8")
while True:
    signal.pause()
"""
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "Signal test subprocess did not become ready.")
                os.kill(process.pid, signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(
                    process.returncode,
                    128 + signal.SIGTERM,
                    f"stdout={stdout!r} stderr={stderr!r}",
                )
                self.assertEqual(
                    marker.read_text(encoding="utf-8").splitlines(),
                    ["ready", "jury", "agent"],
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    @unittest.skipUnless(os.name == "posix", "POSIX signal delivery is required.")
    def test_real_sigint_runs_browser_cleanup_and_exits_with_status_130(self) -> None:
        """An actual keyboard interrupt cannot be swallowed by the Web server."""
        with TemporaryDirectory() as temporary_directory:
            marker = Path(temporary_directory) / "shutdown-events.txt"
            script = f"""
import signal
import types
from pathlib import Path
import main

marker = Path({str(marker)!r})

class Service:
    def stop_at_exit(self):
        with marker.open("a", encoding="utf-8") as handle:
            handle.write("cleaned\\n")

app = types.SimpleNamespace(extensions={{"jury_service": Service()}})
main._install_shutdown_signal_handlers(app)
marker.write_text("ready\\n", encoding="utf-8")
while True:
    signal.pause()
"""
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "Signal test subprocess did not become ready.")
                os.kill(process.pid, signal.SIGINT)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(
                    process.returncode,
                    128 + signal.SIGINT,
                    f"stdout={stdout!r} stderr={stderr!r}",
                )
                self.assertEqual(
                    marker.read_text(encoding="utf-8").splitlines(),
                    ["ready", "cleaned"],
                )
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_main_starts_web_console_with_python_314(self) -> None:
        """The previous Python 3.14 runtime invokes the normal startup path."""
        with (
            patch.object(main, "_start_web_console") as start_web_console,
        ):
            main.main()

        start_web_console.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
