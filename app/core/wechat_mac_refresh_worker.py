"""Delegate a Python 3.13 host refresh to a controlled compatible local worker.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from threading import Event, Thread
import time

from .wechat_mac_refresh_source import RefreshError, RefreshIssue, RefreshNotice
from .wechat_mac_refresh_status import (
    FAILED_MESSAGE as _FAILED_MESSAGE,
    MAX_COUNTER,
    PROGRESS_FIELDS,
    SAFE_FAILURE_MESSAGES,
    safe_progress,
)


MAX_OUTPUT_BYTES = 256 * 1024
MAX_FRAME_BYTES = 4 * 1024
MAX_WORKER_SECONDS = 600
LARGE_BANK_BYTES = 4 * 1024**3
MAX_LARGE_BANK_WORKER_SECONDS = 2_700
MAX_ACCOUNT_WORKER_SECONDS = 5_400
CANCEL_GRACE_SECONDS = 5
# Keep worst-case account progress under the existing byte budget for 90 minutes.
ACCOUNT_PROGRESS_INTERVAL_SECONDS = 15
_SAFE_MESSAGES = {
    *(issue.value for issue in RefreshIssue),
    *SAFE_FAILURE_MESSAGES,
    "Chat history refreshed.",
    "Available chat history refreshed; some sources or messages remain unavailable.",
}


class AccountRefreshDeadlineExceeded(RefreshError):
    """The account worker reached its bounded wall-time limit."""


def _worker_environment(project_root):
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(project_root)
    return environment


def _compatible_python(project_root, environment):
    configured = os.environ.get("AGENTIC_CONTEXT_WECHAT_PYTHON")
    candidates = [configured] if configured else [shutil.which("python3")]
    if not configured and sys.platform == "darwin":
        candidates.extend(
            (
                "/usr/local/bin/python3",
                "/opt/homebrew/bin/python3",
                "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
            )
        )
    checked = set()
    for candidate in candidates:
        if not candidate:
            continue
        found = shutil.which(candidate) or candidate
        try:
            executable = str(Path(found).expanduser().resolve(strict=True))
            if executable in checked:
                continue
            checked.add(executable)
            if not os.access(executable, os.X_OK):
                continue
            probe = subprocess.run(
                [
                    executable,
                    "-B",
                    "-c",
                    "import sys; sys.exit(1) if sys.version_info[:2] < (3, 14) else None; import compression.zstd; "
                    "from app.core.wechat_mac_refresh import WechatMacRefreshService",
                ],
                cwd=project_root,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if probe.returncode == 0:
                return executable
        except (OSError, subprocess.SubprocessError):
            continue
    raise RefreshNotice(RefreshIssue.INTERPRETER_UNAVAILABLE)


def _safe_result(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeError):
        raise RefreshError(
            "The local refresh worker returned an invalid result."
        ) from None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("state"), str)
        or value["state"] not in {"succeeded", "failed"}
    ):
        raise RefreshError("The local refresh worker returned an invalid result.")
    fields = {
        field: value.get(field)
        for field in ("state", "message", "added", "total", "partial", "checked")
    }
    if (
        not isinstance(fields["message"], str)
        or fields["message"] not in _SAFE_MESSAGES
        or type(fields["partial"]) is not bool
        or any(
            type(fields[field]) is not int or not 0 <= fields[field] <= MAX_COUNTER
            for field in ("added", "total", "checked")
        )
    ):
        raise RefreshError("The local refresh worker returned unsafe result fields.")
    try:
        fields.update(safe_progress(value))
    except ValueError:
        raise RefreshError(
            "The local refresh worker returned unsafe progress fields."
        ) from None
    return fields


def _worker_budget_seconds(local_store_root):
    """Budget for full verification of a large resident recovery bank."""
    from .wechat_macos_recovery_bank import mac_recovery_bank_path

    try:
        info = mac_recovery_bank_path(local_store_root).lstat()
    except OSError:
        return MAX_WORKER_SECONDS
    if (
        stat.S_ISREG(info.st_mode)
        and not getattr(info, "st_flags", 0) & getattr(stat, "UF_DATALESS", 0x40000000)
        and info.st_size >= LARGE_BANK_BYTES
    ):
        return MAX_LARGE_BANK_WORKER_SECONDS
    return MAX_WORKER_SECONDS


def run_compatible_worker(local_store_root, scope_id, cancelled, *, on_progress=None, account=False):
    """Stream bounded safe progress while keeping scope and cleanup server-owned."""
    project_root = Path(__file__).resolve().parents[2]
    environment = _worker_environment(project_root)
    executable = _compatible_python(project_root, environment)
    if cancelled.is_set():
        raise RefreshNotice(RefreshIssue.INTERRUPTED)
    budget_seconds = (
        MAX_ACCOUNT_WORKER_SECONDS if account else _worker_budget_seconds(local_store_root)
    )
    process = subprocess.Popen(
        [
            executable,
            "-B",
            "-m",
            "app.core.wechat_mac_refresh_worker",
            "--local-store",
            str(Path(local_store_root).resolve()),
            "--account" if account else "--session",
            scope_id,
        ],
        cwd=project_root,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    reader = None
    try:
        overflow = Event()
        invalid = Event()
        results = []

        def drain():
            size = 0
            try:
                while line := process.stdout.readline(MAX_FRAME_BYTES + 1):
                    size += len(line)
                    if len(line) > MAX_FRAME_BYTES or size > MAX_OUTPUT_BYTES:
                        overflow.set()
                        return
                    if results:
                        raise ValueError(
                            "A worker emitted data after its terminal result."
                        )
                    value = json.loads(line)
                    if isinstance(value, dict) and value.get("type") == "progress":
                        if set(value) != {"type", "status"}:
                            raise ValueError("Unsupported worker frame fields.")
                        status = value["status"]
                        if (
                            not isinstance(status, dict)
                            or set(status) - PROGRESS_FIELDS
                        ):
                            raise ValueError("Unsupported worker progress fields.")
                        progress = safe_progress(status, require_phase=True)
                        if on_progress is not None:
                            on_progress(progress)
                    else:
                        results.append(_safe_result(line))
            except Exception:
                # Neither malformed child output nor callback exceptions may expose
                # source strings or leave an owned worker running indefinitely.
                invalid.set()

        reader = Thread(target=drain, daemon=True, name="wechat-refresh-result")
        reader.start()
        started = time.monotonic()
        cancel_started = None
        deadline_exceeded = False
        while process.poll() is None:
            now = time.monotonic()
            if cancel_started is None and (
                cancelled.is_set()
                or overflow.is_set()
                or invalid.is_set()
                or now - started > budget_seconds
            ):
                deadline_exceeded = not (
                    cancelled.is_set() or overflow.is_set() or invalid.is_set()
                )
                cancel_started = now
                _request_cancel(process)
            if (
                cancel_started is not None
                and now - cancel_started >= CANCEL_GRACE_SECONDS
            ):
                _stop_owned_worker(process)
                break
            time.sleep(0.05)
        reader.join(timeout=2)
        if cancelled.is_set():
            raise RefreshNotice(RefreshIssue.INTERRUPTED)
        if account and deadline_exceeded:
            raise AccountRefreshDeadlineExceeded("The account refresh reached its time limit.")
        if overflow.is_set() or invalid.is_set() or reader.is_alive():
            raise RefreshNotice(RefreshIssue.WORKER_UNAVAILABLE)
        if cancel_started is not None:
            raise RefreshNotice(RefreshIssue.WORKER_TIMEOUT)
        if process.returncode != 0 or len(results) != 1:
            raise RefreshNotice(RefreshIssue.WORKER_UNAVAILABLE)
        return results[0]
    finally:
        # Every post-Popen failure, including thread allocation failure, closes the
        # parent transport and waits for cooperative candidate cleanup first.
        if process.poll() is None:
            _stop_owned_worker(process)
        if reader is not None and reader.is_alive():
            reader.join(timeout=2)
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _request_cancel(process):
    try:
        process.stdin.write(b"cancel\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass


def _stop_owned_worker(process):
    _request_cancel(process)
    try:
        process.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        process.wait(timeout=CANCEL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def _main():
    parser = argparse.ArgumentParser(
        description="Run one server-scoped local WeChat refresh."
    )
    parser.add_argument("--local-store", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--session")
    scope.add_argument("--account")
    arguments = parser.parse_args(sys.argv[1:])
    failed = {
        "state": "failed",
        "message": _FAILED_MESSAGE,
        "added": 0,
        "total": 0,
        "checked": 0,
        "partial": True,
    }
    service = None
    try:
        if (
            sys.version_info[:2] < (3, 14)
            or not Path(arguments.local_store).is_absolute()
        ):
            raise RefreshError("The local refresh worker is unsupported.")
        from .wechat_mac_refresh import WechatMacRefreshService

        service = WechatMacRefreshService(arguments.local_store)

        def watch_parent():
            try:
                for line in sys.stdin:
                    if line.strip() == "cancel":
                        break
            finally:
                # EOF means the owning parent has exited or closed the transport.
                service.shutdown(wait=False)

        watcher = Thread(target=watch_parent, daemon=True, name="wechat-refresh-parent")
        watcher.start()
        token = (
            service.start(arguments.session) if arguments.account is None
            else service.start_account(arguments.account)
        )["task_id"]
        previous = None
        last_progress = -float("inf")
        while True:
            result = service.status(token)
            if result is None:
                raise RefreshError("The local refresh worker lost its task state.")
            if result["state"] != "running":
                print(json.dumps(_safe_result(json.dumps(result))), flush=True)
                return 0
            progress = safe_progress(result, require_phase=True)
            now = time.monotonic()
            if progress != previous and (
                arguments.account is None or now - last_progress >= ACCOUNT_PROGRESS_INTERVAL_SECONDS
            ):
                print(json.dumps({"type": "progress", "status": progress}), flush=True)
                previous = progress
                last_progress = now
            time.sleep(0.05)
    except RefreshNotice as exc:
        print(json.dumps({**failed, "message": exc.issue.value}), flush=True)
        return 0
    except Exception:
        print(json.dumps(failed), flush=True)
        return 1
    finally:
        if service is not None:
            service.shutdown(wait=True, timeout=CANCEL_GRACE_SECONDS)


if __name__ == "__main__":
    raise SystemExit(_main())
