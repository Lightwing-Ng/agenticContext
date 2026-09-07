"""Flush pytest failure evidence before the session summary. Code version: v1.0.0-codex.1."""

from __future__ import annotations

import sys

import pytest


class ImmediateFailureReporter:
    """Observe completed reports without changing collection, execution, or outcomes."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config

    def _write_failure(self, report: pytest.TestReport | pytest.CollectReport) -> None:
        if not report.failed:
            return
        phase = getattr(report, "when", "collect")
        heading = f"[pytest-failure] nodeid={report.nodeid} phase={phase}"
        terminal = self.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line("")
            terminal.write_line(heading)
            terminal.write_line(report.longreprtext)
            terminal.flush()
        else:
            # Preserve diagnostics when a caller explicitly disables pytest's terminal plugin.
            stream = sys.__stderr__ or sys.stderr
            stream.write(f"\n{heading}\n{report.longreprtext}\n")
            stream.flush()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self._write_failure(report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        self._write_failure(report)
