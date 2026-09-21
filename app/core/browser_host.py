"""Host-window helpers shared by browser launch and Agent orchestration.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import logging
import subprocess
import sys


LOGGER = logging.getLogger(__name__)


def _capture_macos_frontmost_application() -> str:
    """Return the current macOS frontmost app without activating a browser."""
    if sys.platform != "darwin":
        return ""
    command = [
        "/usr/bin/osascript",
        "-e",
        'tell application "System Events"',
        "-e",
        "try",
        "-e",
        "return name of first application process whose frontmost is true",
        "-e",
        "end try",
        "-e",
        "end tell",
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        LOGGER.debug("Could not capture the macOS frontmost application: %s", result.stderr)
        return ""
    return str(result.stdout or "").strip()


def _restore_macos_frontmost_application_after_task_stage(
    previous_application: str,
    browser_application: str,
) -> None:
    """Restore a prior macOS app only if the new task browser took focus."""
    if sys.platform != "darwin":
        return
    previous = str(previous_application or "").strip()
    browser = str(browser_application or "").strip()
    if not previous or not browser or previous == browser:
        return
    command = [
        "/usr/bin/osascript",
        "-e",
        "on run argv",
        "-e",
        "set previousFrontmostProcessName to item 1 of argv",
        "-e",
        "set taskBrowserProcessName to item 2 of argv",
        "-e",
        'tell application "System Events"',
        "-e",
        "try",
        "-e",
        "set currentFrontmostProcessName to name of first application process whose frontmost is true",
        "-e",
        "if currentFrontmostProcessName is taskBrowserProcessName then",
        "-e",
        "set frontmost of process previousFrontmostProcessName to true",
        "-e",
        "end if",
        "-e",
        "end try",
        "-e",
        "end tell",
        "-e",
        "end run",
        previous,
        browser,
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if result.returncode != 0:
        LOGGER.debug("Could not restore the macOS frontmost application: %s", result.stderr)


__all__ = [
    "_capture_macos_frontmost_application",
    "_restore_macos_frontmost_application_after_task_stage",
]
