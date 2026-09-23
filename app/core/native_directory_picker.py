"""Host-native directory selection for the local Agent interface.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .config import is_windows_host


class NativeDirectoryPickerError(RuntimeError):
    """Raised when the host cannot open its folder selection panel."""


MACOS_FOLDER_SCRIPT = """
on run argv
    set pickerPrompt to item 1 of argv
    set defaultPath to item 2 of argv
    set selectedFolder to choose folder with prompt pickerPrompt default location POSIX file defaultPath
    return POSIX path of selectedFolder
end run
""".strip()


WINDOWS_FOLDER_SCRIPT = """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $env:AGENTIC_CONTEXT_PICKER_PROMPT
$dialog.SelectedPath = $env:AGENTIC_CONTEXT_PICKER_INITIAL_PATH
try {
    if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Out.WriteLine($dialog.SelectedPath)
    }
} finally {
    $dialog.Dispose()
}
""".strip()


def choose_native_directory(initial_path: Path, prompt: str) -> Path | None:
    """Return a chosen host directory, or ``None`` when the user cancels."""
    default_path = initial_path.expanduser()
    while not default_path.is_dir() and default_path.parent != default_path:
        default_path = default_path.parent
    if not default_path.is_dir():
        default_path = Path.home()

    if is_windows_host():
        environment = os.environ.copy()
        environment["AGENTIC_CONTEXT_PICKER_PROMPT"] = prompt
        environment["AGENTIC_CONTEXT_PICKER_INITIAL_PATH"] = str(default_path)
        command = [
            "powershell.exe", "-NoProfile", "-STA",
            "-Command", WINDOWS_FOLDER_SCRIPT,
        ]
    else:
        environment = None
        command = [
            "/usr/bin/osascript", "-e", MACOS_FOLDER_SCRIPT,
            prompt, str(default_path),
        ]

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
    except OSError as exc:
        raise NativeDirectoryPickerError("The system folder picker could not open.") from exc

    if completed.returncode != 0:
        error_text = (completed.stderr or "").strip()
        if not is_windows_host() and ("User canceled" in error_text or "-128" in error_text):
            return None
        raise NativeDirectoryPickerError("The system folder picker failed to open.")

    selected_path = completed.stdout.strip()
    return Path(selected_path).expanduser().resolve(strict=False) if selected_path else None
