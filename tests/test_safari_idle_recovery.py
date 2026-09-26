"""Fail-closed recovery for verified task-owned Safari idle windows."""

# Code version: v1.1.0-codex.0

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import call, patch

import pytest

from app.core.safari_automation import (
    SAFARI_CONTEXT_LEASE_VERSION,
    SafariContext,
    SafariPage,
    _safari_window_inert_state,
    _safari_window_is_inert_ghost,
)


@pytest.mark.parametrize("raw,expected", (
    ("exists:true|tab_count:0|visible:false|miniaturized:false|has_document:false", True),
    ("exists:true|tab_count:1|visible:false|miniaturized:false|has_document:false", False),
    ("exists:true|tab_count:0|visible:true|miniaturized:false|has_document:false", False),
    ("exists:true|tab_count:0|visible:false|miniaturized:true|has_document:false", False),
    ("exists:true|tab_count:0|visible:false|miniaturized:false|has_document:true", False),
    ("exists:true|tab_count:0|visible:unknown|miniaturized:false|has_document:false", False),
    ("exists:false", False),
    ("unexpected response", False),
))
def test_inert_window_guard_requires_all_fixed_properties(raw: str, expected: bool) -> None:
    with patch("app.core.safari_automation.run_applescript", return_value=raw):
        assert _safari_window_is_inert_ghost(456) is expected


def test_inert_window_diagnostics_report_missing_and_unverified_states() -> None:
    with patch("app.core.safari_automation.run_applescript", return_value="exists:false"):
        missing = _safari_window_inert_state(456)
    assert missing == {
        "exists": False, "tab_count": None, "visible": None,
        "miniaturized": None, "has_document": None, "read_error": False,
    }
    with patch("app.core.safari_automation.run_applescript", side_effect=RuntimeError("private diagnostic")):
        unverified = _safari_window_inert_state(456)
    assert unverified["read_error"] is True
    assert unverified["exists"] is None
    assert "private diagnostic" not in str(unverified)


@pytest.mark.parametrize("close_succeeds,remains", ((True, False), (False, False), (False, True), (True, True)))
def test_safari_failed_idle_shell_requires_proven_disappearance(close_succeeds: bool, remains: bool) -> None:
    context = SafariContext("https://x.com/home")
    context._context_lock_handle = object()
    context._durable_lease_started = True
    context._creation_baseline_inventory = {123: 4}
    payload = {
        "version": SAFARI_CONTEXT_LEASE_VERSION,
        "state": "idle",
        "window_id": 456,
        "owner_pid": 123,
        "ownership_token": "a" * 32,
        "baseline_windows": [{"window_id": 123, "tab_count": 4}],
        "safari_process_identity": "same-process",
    }
    after = {123: 4, 456: 0} if remains else {123: 4}
    with patch.object(context, "_read_context_lease_state", return_value=payload), patch(
        "app.core.safari_automation._safari_process_identity", return_value="same-process"
    ), patch(
        "app.core.safari_automation._safari_window_inventory", side_effect=[{123: 4, 456: 0}, after]
    ), patch(
        "app.core.safari_automation._close_safari_window_id", return_value=close_succeeds
    ) as close, patch(
        "app.core.safari_automation._safari_window_is_inert_ghost", return_value=False
    ), patch.object(context, "_clear_context_lease_state") as clear:
        assert context._retire_unaddressable_idle_window(456) is (not remains)

    close.assert_called_once_with(456, require_empty=True)
    assert clear.call_count == int(not remains)


@pytest.mark.parametrize("change", ("occupied", "baseline", "process", "lease", "unlocked"))
def test_safari_failed_idle_shell_does_not_close_unverified_window(change: str) -> None:
    context = SafariContext("https://x.com/home")
    context._context_lock_handle = None if change == "unlocked" else object()
    context._durable_lease_started = True
    context._creation_baseline_inventory = {123: 4}
    payload = {
        "version": SAFARI_CONTEXT_LEASE_VERSION,
        "state": "owned" if change == "lease" else "idle",
        "window_id": 456,
        "owner_pid": 123,
        "ownership_token": "a" * 32,
        "baseline_windows": [{"window_id": 123, "tab_count": 4}],
        "safari_process_identity": "old-process" if change == "process" else "same-process",
    }
    if change == "baseline":
        payload["baseline_windows"].append({"window_id": 456, "tab_count": 0})
    with patch.object(context, "_read_context_lease_state", return_value=payload), patch(
        "app.core.safari_automation._safari_process_identity", return_value="same-process"
    ), patch(
        "app.core.safari_automation._safari_window_inventory", return_value={123: 4, 456: int(change == "occupied")}
    ), patch("app.core.safari_automation._close_safari_window_id") as close, patch.object(
        context, "_clear_context_lease_state"
    ) as clear:
        assert context._retire_unaddressable_idle_window(456) is False

    close.assert_not_called()
    clear.assert_not_called()


@pytest.mark.parametrize("retired", (True, False))
def test_safari_idle_shell_recovery_creates_one_replacement_only_after_retirement(retired: bool) -> None:
    context = SafariContext("https://x.com/home")
    context._adopted_window_id = 456
    context._adopted_window_was_empty = True
    with patch("app.core.safari_automation._safari_window_inventory", return_value={123: 4, 456: 0}), patch.object(
        context, "_create_tab", side_effect=RuntimeError("Safari idle task tab did not become addressable. (-1719)")
    ), patch.object(context, "_retire_unaddressable_idle_window", return_value=retired) as retire, patch.object(
        context, "_begin_context_window_creation"
    ) as begin, patch.object(context, "_create_window", return_value="789") as create, patch.object(
        context, "_mark_context_window_owned"
    ), patch.object(SafariPage, "goto"):
        if retired:
            assert context._create_page("https://x.com/home").window_id == 789
        else:
            with pytest.raises(RuntimeError, match="did not become addressable"):
                context._create_page("https://x.com/home")

    retire.assert_called_once_with(456)
    assert create.call_count == int(retired)
    assert begin.call_count == int(retired)


@pytest.mark.parametrize("error", (
    "Safari idle task window gained an extra tab.",
    "Safari automation timed out after 20 seconds.",
    "Safari invalid tab index (-1719).",
))
def test_safari_only_exact_readiness_failure_can_retire_an_idle_window(error: str) -> None:
    context = SafariContext("https://x.com/home")
    context._adopted_window_id = 456
    context._adopted_window_was_empty = True
    with patch("app.core.safari_automation._safari_window_inventory", return_value={123: 4, 456: 0}), patch.object(
        context, "_create_tab", side_effect=RuntimeError(error)
    ), patch.object(context, "_retire_unaddressable_idle_window") as retire, patch.object(
        context, "_create_window"
    ) as create, pytest.raises(RuntimeError):
        context._create_page("https://x.com/home")

    retire.assert_not_called()
    create.assert_not_called()


def test_safari_verified_inert_ghost_is_protected_before_replacement_document(tmp_path: Path) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    state_path.write_text(json.dumps({
        "version": SAFARI_CONTEXT_LEASE_VERSION,
        "state": "idle",
        "window_id": 456,
        "owner_pid": 123,
        "ownership_token": "a" * 32,
        "baseline_windows": [{"window_id": 123, "tab_count": 4}],
        "safari_process_identity": "same-process",
    }))
    context = SafariContext("https://x.com/home", lock_blocking=False)

    def create_document(_url):
        creating = json.loads(state_path.read_text())
        assert creating["state"] == "creating"
        assert creating["baseline_windows"] == [
            {"window_id": 123, "tab_count": 4},
            {"window_id": 456, "tab_count": 0},
        ]
        return "789"

    with patch("app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH", lock_path), patch(
        "app.core.safari_automation._safari_process_identity", return_value="same-process"
    ), patch("app.core.safari_automation._safari_window_inventory", return_value={123: 4, 456: 0}), patch(
        "app.core.safari_automation._close_safari_window_id", return_value=False
    ) as close, patch("app.core.safari_automation._safari_window_is_inert_ghost", return_value=True), patch.object(
        context, "_create_tab", side_effect=RuntimeError("Safari idle task tab did not become addressable.")
    ), patch.object(context, "_create_window", side_effect=create_document), patch.object(
        context, "_clear_context_lease_state"
    ) as clear, patch.object(SafariPage, "goto"):
        context._acquire_context_lock()
        try:
            assert context._create_page("https://x.com/home").window_id == 789
            owned = json.loads(state_path.read_text())
            assert owned["state"] == "owned"
            assert owned["window_id"] == 789
            assert {"window_id": 456, "tab_count": 0} in owned["baseline_windows"]
        finally:
            context._release_context_lock()

    close.assert_called_once_with(456, require_empty=True)
    clear.assert_not_called()


@pytest.mark.parametrize("inert_ghost", (True, False))
def test_safari_stale_owned_window_records_idle_before_recovering_empty_shell(
    tmp_path: Path,
    inert_ghost: bool,
) -> None:
    lock_path = tmp_path / "safari-context.lock"
    state_path = tmp_path / "safari-context.lock.state"
    original_baseline = [{"window_id": 123, "tab_count": 4}]
    state_path.write_text(json.dumps({
        "version": SAFARI_CONTEXT_LEASE_VERSION,
        "state": "owned",
        "window_id": 456,
        "owner_pid": os.getpid() + 1,
        "ownership_token": "a" * 32,
        "baseline_windows": original_baseline,
        "safari_process_identity": "same-process",
    }))
    context = SafariContext("https://chatgpt.com/", lock_blocking=False)
    inventory_reads = 0

    def read_inventory():
        nonlocal inventory_reads
        inventory_reads += 1
        return {123: 4, 456: 1 if inventory_reads == 1 else 0}

    def fail_idle_tab(window_id, _url, *, expect_empty_window):
        idle = json.loads(state_path.read_text())
        assert idle["state"] == "idle"
        assert idle["window_id"] == window_id == 456
        assert idle["baseline_windows"] == original_baseline
        assert idle["owner_pid"] == os.getpid()
        assert idle["ownership_token"] == context._ownership_token
        assert expect_empty_window is True
        raise RuntimeError("Safari idle task tab did not become addressable. (-1719)")

    def create_document(_url):
        creating = json.loads(state_path.read_text())
        assert creating["state"] == "creating"
        assert creating["baseline_windows"] == [
            *original_baseline,
            {"window_id": 456, "tab_count": 0},
        ]
        return "789"

    with patch("app.core.safari_automation.SAFARI_CONTEXT_LOCK_PATH", lock_path), patch(
        "app.core.safari_automation._safari_process_identity", return_value="same-process"
    ), patch("app.core.safari_automation._safari_pid_is_alive", return_value=False), patch(
        "app.core.safari_automation._safari_window_inventory", side_effect=read_inventory
    ), patch("app.core.safari_automation._close_safari_window_id", return_value=False) as close, patch(
        "app.core.safari_automation._safari_window_is_inert_ghost", return_value=inert_ghost
    ), patch.object(context, "_create_tab", side_effect=fail_idle_tab) as create_tab, patch.object(
        context, "_create_window", side_effect=create_document
    ) as create, patch.object(context, "_clear_context_lease_state") as clear, patch.object(
        SafariPage, "goto"
    ):
        context._acquire_context_lock()
        try:
            if inert_ghost:
                assert context._create_page("https://chatgpt.com/").window_id == 789
                owned = json.loads(state_path.read_text())
                assert owned["state"] == "owned"
                assert owned["window_id"] == 789
                assert owned["baseline_windows"] == [
                    *original_baseline,
                    {"window_id": 456, "tab_count": 0},
                ]
            else:
                with pytest.raises(RuntimeError, match="did not become addressable"):
                    context._create_page("https://chatgpt.com/")
                idle = json.loads(state_path.read_text())
                assert idle["state"] == "idle"
                assert idle["baseline_windows"] == original_baseline
        finally:
            context._release_context_lock()

    assert close.call_args_list == [call(456), call(456, require_empty=True)]
    create_tab.assert_called_once_with(456, "https://chatgpt.com/", expect_empty_window=True)
    assert create.call_count == int(inert_ghost)
    clear.assert_not_called()
