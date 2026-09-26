"""Persistent AppleScript transport checks. Code version: v1.0.0-codex.0."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import time

import pytest

from app.core import macos_applescript


pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="The macOS worker uses selectable POSIX pipes."
)

FAKE_WORKER_SOURCE = r"""
import json
import os
import sys
import time

for line in sys.stdin.buffer:
    request = json.loads(line)
    source = request["source"]
    response = {
        "id": request["id"], "returncode": 0, "stdout": source, "stderr": ""
    }
    if source == "delay":
        time.sleep(60)
    elif source == "error":
        response.update(returncode=1, stdout="", stderr="Execution failed (-1712)")
    elif source == "pid":
        response["stdout"] = str(os.getpid())
    elif source == "wrong-id":
        response["id"] += 1
    elif source == "boolean-id":
        response["id"] = True
    elif source == "boolean-returncode":
        response["returncode"] = False
    elif source == "invalid-returncode":
        response["returncode"] = 2
    elif source == "missing-stdout":
        del response["stdout"]
    elif source == "invalid-stderr":
        response["stderr"] = None
    elif source == "eof":
        sys.exit(0)
    elif source == "truncated":
        os.write(1, b'{"id":')
        sys.exit(0)
    elif source == "malformed-json":
        os.write(1, b'not-json\n')
        continue
    elif source == "non-object":
        os.write(1, b'[]\n')
        continue
    elif source == "oversized":
        os.write(1, b'x' * 2048)
        continue
    encoded = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
    if source == "extra-line":
        encoded += b'{}\n'
    elif source == "extra-tail":
        encoded += b'x'
    offset = 0
    while offset < len(encoded):
        # Exercise incomplete reads, including UTF-8 code points across chunks.
        length = 7 if len(encoded) > 65536 else len(encoded)
        offset += os.write(1, encoded[offset:offset + length])
"""


@pytest.fixture
def fake_worker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        macos_applescript,
        "WORKER_COMMAND",
        (sys.executable, "-u", "-c", FAKE_WORKER_SOURCE),
    )
    worker = macos_applescript.AppleScriptWorker()
    try:
        yield worker
    finally:
        worker.close()


def test_worker_reuses_one_process_and_keeps_scripts_out_of_argv(fake_worker) -> None:
    first = fake_worker.run("pid", timeout=5)
    process = fake_worker._process
    secret_source = 'private text "quoted"\nwith a second line'
    response = fake_worker.run(secret_source, timeout=5)
    last = fake_worker.run("pid", timeout=5)

    assert first.stdout == last.stdout == str(process.pid)
    assert fake_worker._process is process
    assert response.returncode == 0
    assert response.stdout == secret_source
    assert response.stderr == ""
    assert secret_source not in " ".join(process.args)


def test_worker_preserves_large_unicode_and_multiline_payloads(fake_worker) -> None:
    source = 'Snow: 雪; emoji: 😀; quoted: "x"; slash: \\\n' * 5000

    response = fake_worker.run(source, timeout=10)

    assert response.stdout == source
    assert response.returncode == 0


def test_worker_serializes_concurrent_requests_without_mixing_replies(fake_worker) -> None:
    sources = [f"request {index}: 雪😀\nnext line" for index in range(24)]
    with ThreadPoolExecutor(max_workers=6) as executor:
        responses = list(executor.map(lambda source: fake_worker.run(source, timeout=5), sources))

    assert [response.stdout for response in responses] == sources
    assert fake_worker._request_id == len(sources)


def test_script_error_preserves_error_number_and_worker(fake_worker) -> None:
    fake_worker.run("pid", timeout=5)
    process = fake_worker._process

    response = fake_worker.run("error", timeout=5)

    assert response.returncode == 1
    assert response.stdout == ""
    assert "-1712" in response.stderr
    assert fake_worker._process is process
    assert fake_worker.run("recovered", timeout=5).stdout == "recovered"
    assert fake_worker._process is process


@pytest.mark.parametrize(
    "source",
    [
        "wrong-id", "boolean-id", "boolean-returncode", "invalid-returncode",
        "missing-stdout", "invalid-stderr", "eof", "truncated", "malformed-json",
        "non-object", "extra-line", "extra-tail", "oversized",
    ],
)
def test_invalid_transport_is_reaped_without_replaying_and_next_call_recovers(
    fake_worker, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    fake_worker.run("pid", timeout=5)
    process = fake_worker._process
    monkeypatch.setattr(macos_applescript, "MAX_RESPONSE_BYTES", 1024)

    with pytest.raises(RuntimeError, match="worker transport failed"):
        fake_worker.run(source, timeout=5)

    assert fake_worker._process is None
    assert process.poll() is not None
    assert process.stdin.closed
    assert process.stdout.closed
    assert fake_worker._request_id == 2
    assert fake_worker.run("recovered", timeout=5).stdout == "recovered"
    assert fake_worker._process is not process


def test_read_timeout_reaps_worker_without_replaying_and_next_call_recovers(fake_worker) -> None:
    fake_worker.run("pid", timeout=5)
    process = fake_worker._process
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        fake_worker.run("delay", timeout=0.1)

    assert time.monotonic() - started < 3
    assert process.poll() is not None
    assert fake_worker._process is None
    assert fake_worker._request_id == 2
    assert fake_worker.run("recovered", timeout=5).stdout == "recovered"
    assert fake_worker._process is not process


def test_write_timeout_bounds_worker_that_never_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        macos_applescript,
        "WORKER_COMMAND",
        (sys.executable, "-u", "-c", "import time; time.sleep(60)"),
    )
    worker = macos_applescript.AppleScriptWorker()
    process = worker._start()
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            worker.run("x" * 4_000_000, timeout=0.1)
        assert time.monotonic() - started < 3
        assert process.poll() is not None
        assert worker._process is None
        assert worker._request_id == 1
    finally:
        worker.close()


def test_close_is_idempotent_and_reaps_only_owned_worker(fake_worker) -> None:
    fake_worker.run("pid", timeout=5)
    process = fake_worker._process

    fake_worker.close()
    fake_worker.close()

    assert fake_worker._process is None
    assert process.poll() is not None
    assert process.stdin.closed
    assert process.stdout.closed


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires the macOS AppleScript runtime.")
def test_native_worker_reuses_process_and_preserves_scalar_unicode_and_large_results() -> None:
    worker = macos_applescript.AppleScriptWorker()
    try:
        assert worker.run('return "ready"', timeout=10).stdout == "ready"
        process = worker._process
        cases = [
            ("return 123", "123"),
            ("return true", "true"),
            ("return false", "false"),
            ("return missing value", "missing value"),
            ('return ""', ""),
            ('return "雪😀" & linefeed & "next line"', "雪😀\nnext line"),
        ]
        for source, expected in cases:
            result = worker.run(source, timeout=10)
            assert result.returncode == 0, result.stderr
            assert result.stdout == expected
            assert worker._process is process
        large = "雪😀" * 50_000
        result = worker.run("return " + json.dumps(large, ensure_ascii=False), timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout == large
        assert worker._process is process
    finally:
        worker.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires the macOS AppleScript runtime.")
def test_native_worker_preserves_error_number_and_resets_script_state() -> None:
    worker = macos_applescript.AppleScriptWorker()
    try:
        changed = worker.run(
            'set AppleScript\'s text item delimiters to "changed"\nreturn "ready"',
            timeout=10,
        )
        assert changed.returncode == 0, changed.stderr
        process = worker._process
        result = worker.run('return {"a", "b"} as text', timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "ab"
        error = worker.run('error "failure marker" number -1712', timeout=10)
        assert error.returncode == 1
        assert "failure marker" in error.stderr
        assert "-1712" in error.stderr
        assert worker.run('return "recovered"', timeout=10).stdout == "recovered"
        assert worker._process is process
    finally:
        worker.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires the macOS AppKit runtime.")
def test_native_worker_remains_prohibited_from_dock_and_activation() -> None:
    worker = macos_applescript.AppleScriptWorker()
    try:
        result = worker.run(
            'use framework "AppKit"\n'
            "return current application's NSApplication's sharedApplication()'s "
            "activationPolicy() as integer",
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "2"
    finally:
        worker.close()
