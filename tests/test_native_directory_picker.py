"""Host-native Tunnel folder picker checks. Code version: v1.0.0-codex.0."""

from pathlib import Path
import subprocess
from unittest.mock import patch

from app.core.native_directory_picker import (
    MACOS_FOLDER_SCRIPT,
    WINDOWS_FOLDER_SCRIPT,
    NativeDirectoryPickerError,
    choose_native_directory,
)


def test_macos_picker_uses_one_system_panel_without_finder_activation(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    with (
        patch("app.core.native_directory_picker.is_windows_host", return_value=False),
        patch(
            "app.core.native_directory_picker.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, f"{selected}\n", ""),
        ) as run,
    ):
        result = choose_native_directory(tmp_path, "Choose a folder")

    assert result == selected.resolve()
    assert MACOS_FOLDER_SCRIPT.count("choose folder") == 1
    assert "activate" not in MACOS_FOLDER_SCRIPT.lower()
    assert run.call_count == 1
    assert run.call_args.args[0][:2] == ["/usr/bin/osascript", "-e"]


def test_macos_picker_cancel_does_not_change_selection(tmp_path: Path) -> None:
    with (
        patch("app.core.native_directory_picker.is_windows_host", return_value=False),
        patch(
            "app.core.native_directory_picker.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", "User canceled. (-128)"),
        ),
    ):
        assert choose_native_directory(tmp_path, "Choose a folder") is None


def test_windows_picker_uses_native_shell_dialog_and_cancels_silently(tmp_path: Path) -> None:
    with (
        patch("app.core.native_directory_picker.is_windows_host", return_value=True),
        patch(
            "app.core.native_directory_picker.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run,
    ):
        assert choose_native_directory(tmp_path, "Choose a folder") is None

    assert "System.Windows.Forms.FolderBrowserDialog" in WINDOWS_FOLDER_SCRIPT
    assert "$dialog.AutoUpgradeEnabled = $true" in WINDOWS_FOLDER_SCRIPT
    assert run.call_args.args[0][:3] == ["powershell.exe", "-NoProfile", "-STA"]
    assert run.call_args.kwargs["env"]["AGENTIC_CONTEXT_PICKER_INITIAL_PATH"] == str(tmp_path)


def test_picker_process_failure_is_reported_without_host_details(tmp_path: Path) -> None:
    with (
        patch("app.core.native_directory_picker.is_windows_host", return_value=False),
        patch(
            "app.core.native_directory_picker.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, "", "private host details"),
        ),
    ):
        try:
            choose_native_directory(tmp_path, "Choose a folder")
        except NativeDirectoryPickerError as exc:
            assert "private host details" not in str(exc)
        else:
            raise AssertionError("The failed system picker must raise an error.")
