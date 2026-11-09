"""Delegate a Python 3.13 host refresh to a controlled compatible local worker.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event, Thread
import time

from .wechat_mac_refresh_source import RefreshError


MAX_OUTPUT_BYTES = 16 * 1024
MAX_WORKER_SECONDS = 600
CANCEL_GRACE_SECONDS = 5
_FAILED_MESSAGE = "Chat history could not be refreshed safely. Try again shortly."
_SAFE_MESSAGES = {
    _FAILED_MESSAGE,
    "Chat history refreshed.",
    "Available chat history refreshed; some sources or messages remain unavailable.",
}


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
    raise RefreshError("A compatible local refresh interpreter is unavailable.")


def _safe_result(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("state") not in {"succeeded", "failed"}:
        raise RefreshError("The local refresh worker returned an invalid result.")
    fields = {
        field: value.get(field)
        for field in ("state", "message", "added", "total", "partial", "checked")
    }
    if (
        fields["message"] not in _SAFE_MESSAGES
        or type(fields["partial"]) is not bool
        or any(
            type(fields[field]) is not int or fields[field] < 0
            for field in ("added", "total", "checked")
        )
    ):
        raise RefreshError("The local refresh worker returned unsafe result fields.")
    return fields


def run_compatible_worker(local_store_root, session_id, cancelled):
    """Transport only server-owned scope in argv and a bounded safe final status out."""
    project_root = Path(__file__).resolve().parents[2]
    environment = _worker_environment(project_root)
    executable = _compatible_python(project_root, environment)
    if cancelled.is_set():
        raise RefreshError("The local refresh was cancelled before its worker started.")
    process = subprocess.Popen(
        [
            executable,
            "-B",
            "-m",
            "app.core.wechat_mac_refresh_worker",
            "--local-store",
            str(Path(local_store_root).resolve()),
            "--session",
            session_id,
        ],
        cwd=project_root,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    reader = None
    try:
        output = bytearray()
        overflow = Event()

        def drain():
            while chunk := process.stdout.read(4096):
                if len(output) + len(chunk) > MAX_OUTPUT_BYTES:
                    overflow.set()
                elif not overflow.is_set():
                    output.extend(chunk)

        reader = Thread(target=drain, daemon=True, name="wechat-refresh-result")
        reader.start()
        started = time.monotonic()
        cancel_started = None
        interrupted = False
        while process.poll() is None:
            now = time.monotonic()
            if cancel_started is None and (
                cancelled.is_set()
                or overflow.is_set()
                or now - started > MAX_WORKER_SECONDS
            ):
                interrupted = True
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
        if (
            interrupted
            or cancelled.is_set()
            or overflow.is_set()
            or process.returncode != 0
            or reader.is_alive()
        ):
            raise RefreshError(
                "The compatible refresh worker was interrupted or unavailable."
            )
        return _safe_result(bytes(output))
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
    parser.add_argument("--session", required=True)
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
        token = service.start(arguments.session)["task_id"]
        while True:
            result = service.status(token)
            if result is None:
                raise RefreshError("The local refresh worker lost its task state.")
            if result["state"] != "running":
                print(json.dumps(_safe_result(json.dumps(result))), flush=True)
                return 0
            time.sleep(0.05)
    except Exception:
        print(json.dumps(failed), flush=True)
        return 1
    finally:
        if service is not None:
            service.shutdown(wait=True, timeout=CANCEL_GRACE_SECONDS)


if __name__ == "__main__":
    raise SystemExit(_main())
