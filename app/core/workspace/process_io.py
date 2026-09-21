"""Bounded process launch and output capture for workspace commands."""

# Code version: v1.0.0-claude.0

from __future__ import annotations

import os
from pathlib import Path
from queue import Empty, Full, Queue
import signal
import subprocess
from threading import Event, Thread
import time
from typing import Any, Callable

from ..config import is_windows_host
from .executables import _trusted_windows_taskkill


_SUBPROCESS_POPEN_TYPE = subprocess.Popen


_STREAM_READ_FAILED = object()


MAX_ACTION_OUTPUT_CHARS = 48_000


RUN_OUTPUT_QUEUE_SIZE = 4


SEARCH_STDOUT_QUEUE_SIZE = 4


def _process_group_options() -> dict[str, Any]:
    """Return subprocess options that isolate a task on the current host."""
    if is_windows_host():
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creation_flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": creation_flags} if creation_flags else {}
    return {"start_new_session": True}


def _stop_process(process: subprocess.Popen[Any], *, timeout: float = 3) -> None:
    """Stop one isolated task process and every surviving descendant."""
    bounded_timeout = max(0.05, float(timeout))
    if is_windows_host():
        if process.poll() is None:
            try:
                process.send_signal(
                    getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
                )
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=bounded_timeout)
            except (OSError, subprocess.TimeoutExpired):
                pass
        taskkill = _trusted_windows_taskkill()
        if taskkill is not None:
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=bounded_timeout,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=bounded_timeout)
        except (OSError, subprocess.TimeoutExpired):
            return
        return

    deadline = time.monotonic() + bounded_timeout
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            process.wait(timeout=0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    except OSError:
        return
    try:
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        pass
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            break
        time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        return
    try:
        process.wait(timeout=bounded_timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _queue_text_lines(
    stream: Any,
    output: Queue[Any],
    discard: Event,
) -> None:
    """Drain a text stream into a bounded queue without retaining excess output."""
    terminal_event: object | None = None
    try:
        for line in stream:
            while not discard.is_set():
                try:
                    output.put(line, timeout=0.05)
                    break
                except Full:
                    continue
            if discard.is_set():
                break
    except (OSError, UnicodeError, ValueError):
        terminal_event = _STREAM_READ_FAILED
    finally:
        while not discard.is_set():
            try:
                output.put(terminal_event, timeout=0.05)
                break
            except Full:
                continue


def _queue_text_chunks(
    stream: Any,
    output: Queue[Any],
    discard: Event,
) -> None:
    """Drain fixed-size text chunks into a bounded queue."""
    terminal_event: object | None = None
    try:
        while not discard.is_set():
            chunk = stream.read(4_096)
            if not chunk:
                break
            while not discard.is_set():
                try:
                    output.put(chunk, timeout=0.05)
                    break
                except Full:
                    continue
    except (OSError, UnicodeError, ValueError):
        terminal_event = _STREAM_READ_FAILED
    finally:
        while not discard.is_set():
            try:
                output.put(terminal_event, timeout=0.05)
                break
            except Full:
                continue


def _bounded_verification_process_output(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: int,
    should_stop: Callable[[], bool],
) -> tuple[str, int, bool, bool, bool]:
    """Drain one verification process with fixed memory, time, and Stop bounds."""
    if process.stdout is None:
        _stop_process(process, timeout=1)
        raise RuntimeError("Verification could not open a bounded output stream.")

    chunks: list[str] = []
    retained_characters = 0
    truncated = False
    stopped = False
    timed_out = False
    stream_failed = False
    stream_done = False
    loop_completed = False
    discard_output: Event | None = None
    reader: Thread | None = None
    deadline = time.monotonic() + timeout_seconds
    try:
        output_queue: Queue[Any] = Queue(maxsize=RUN_OUTPUT_QUEUE_SIZE)
        discard_output = Event()
        reader = Thread(
            target=_queue_text_chunks,
            args=(process.stdout, output_queue, discard_output),
            daemon=True,
        )
        reader.start()
        while True:
            if should_stop():
                stopped = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if stream_done:
                if process.poll() is not None:
                    break
                time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
                continue
            try:
                remaining_time = max(0.001, deadline - time.monotonic())
                value = output_queue.get(timeout=min(0.05, remaining_time))
            except Empty:
                if not reader.is_alive() and output_queue.empty():
                    stream_done = True
                continue
            if value is _STREAM_READ_FAILED:
                stream_failed = True
                break
            if value is None:
                stream_done = True
                continue
            if not isinstance(value, str):
                stream_failed = True
                break
            remaining_characters = MAX_ACTION_OUTPUT_CHARS - retained_characters
            if remaining_characters > 0:
                retained = value[:remaining_characters]
                chunks.append(retained)
                retained_characters += len(retained)
            if len(value) > remaining_characters:
                truncated = True
        loop_completed = True
    finally:
        if stopped or timed_out or stream_failed or not loop_completed:
            if discard_output is not None:
                discard_output.set()
            _stop_process(process, timeout=1)
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            if discard_output is not None:
                discard_output.set()
            _stop_process(process, timeout=1)
        if isinstance(process, _SUBPROCESS_POPEN_TYPE):
            _stop_process(process, timeout=0.25)
        if discard_output is not None:
            discard_output.set()
        if reader is not None and reader.ident is not None:
            reader.join(timeout=1)
        if reader is None or not reader.is_alive():
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass

    if stream_failed:
        raise RuntimeError("Verification output could not be read safely.")
    output = "".join(chunks)
    if truncated:
        marker = f"\n[output truncated at {MAX_ACTION_OUTPUT_CHARS:,} characters]"
        output = output[: max(0, MAX_ACTION_OUTPUT_CHARS - len(marker))] + marker
    returncode = process.returncode if isinstance(process.returncode, int) else -1
    return output, returncode, truncated, stopped, timed_out


def _bounded_devnull_process(
    command: list[str],
    *,
    workspace: Path,
    timeout_seconds: float,
    should_stop: Callable[[], bool],
    process_changed: Callable[[subprocess.Popen[str] | None], None],
) -> tuple[int, bool, bool]:
    """Run one no-output check with bounded time, Stop, and process cleanup."""
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_process_group_options(),
        )
    except OSError as exc:
        raise RuntimeError("Verification process could not be started.") from exc

    stopped = False
    timed_out = False
    loop_completed = False
    deadline = time.monotonic() + timeout_seconds
    try:
        process_changed(process)
        while process.poll() is None:
            if should_stop():
                stopped = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
        loop_completed = True
    finally:
        if stopped or timed_out or not loop_completed:
            _stop_process(process, timeout=1)
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            _stop_process(process, timeout=1)
        if isinstance(process, _SUBPROCESS_POPEN_TYPE):
            _stop_process(process, timeout=0.25)
        process_changed(None)

    returncode = process.returncode if isinstance(process.returncode, int) else -1
    return returncode, stopped, timed_out


def _discard_text_stream(stream: Any) -> None:
    """Drain subprocess diagnostics without exposing or retaining path-bearing text."""
    try:
        for _chunk in iter(lambda: stream.read(4_096), ""):
            pass
    except (OSError, ValueError):
        return


def _truncate_text(value: str, maximum: int) -> str:
    text = str(value or "")
    if len(text) <= maximum:
        return text
    omitted = len(text) - maximum
    return text[:maximum] + f"\n[truncated {omitted:,} characters]"
