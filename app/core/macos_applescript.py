"""Execute Safari Apple Events through one process-local, UI-prohibited worker.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import atexit
import json
import os
import selectors
import subprocess
import time
from threading import RLock


# Only the fixed bridge appears in argv; scripts and results stay in anonymous pipes.
WORKER_SOURCE = r"""
ObjC.import('Foundation');
ObjC.import('AppKit');
const application = $.NSApplication.sharedApplication;
if (!application.setActivationPolicy(2)) throw new Error('Background policy refused');
const input = $.NSFileHandle.fileHandleWithStandardInput;
const output = $.NSFileHandle.fileHandleWithStandardOutput;
const reset = $.NSAppleScript.alloc.initWithSource('set AppleScript\'s text item delimiters to ""');
let pending = '';
function resultText(result) {
    if (Number(result.descriptorType) === 0x74797065
        && Number(result.typeCodeValue) === 0x6d736e67) return 'missing value';
    if (Number(result.descriptorType) === 0x6e756c6c) return '';
    if (Number(result.descriptorType) === 0x6c697374) {
        const values = [];
        for (let index = 1; index <= Number(result.numberOfItems); index++) {
            values.push(resultText(result.descriptorAtIndex(index)));
        }
        return values.join(', ');
    }
    const text = ObjC.unwrap(result.stringValue);
    if (typeof text !== 'string') throw new Error('Unsupported AppleScript result type');
    return text;
}
function respond(value) {
    const text = $(JSON.stringify(value) + '\n');
    output.writeData(text.dataUsingEncoding($.NSUTF8StringEncoding));
}
while (true) {
    const chunk = input.availableData;
    if (Number(chunk.length) === 0) break;
    pending += ObjC.unwrap($.NSString.alloc.initWithDataEncoding(chunk, $.NSUTF8StringEncoding));
    let boundary;
    while ((boundary = pending.indexOf('\n')) !== -1) {
        const line = pending.slice(0, boundary);
        pending = pending.slice(boundary + 1);
        const request = JSON.parse(line);
        let response = {id: request.id, returncode: 0, stdout: '', stderr: ''};
        try {
            reset.executeAndReturnError(null);
            const script = $.NSAppleScript.alloc.initWithSource($(request.source));
            const error = $();
            const result = script.executeAndReturnError(error);
            const details = ObjC.deepUnwrap(error);
            if (details) {
                response.returncode = 1;
                response.stderr = String(details.NSAppleScriptErrorMessage || 'AppleScript failed')
                    + ' (' + String(details.NSAppleScriptErrorNumber || 0) + ')';
            } else {
                response.stdout = resultText(result);
            }
        } catch (error) {
            response.returncode = 1;
            response.stderr = String(error);
        }
        respond(response);
    }
}
""".strip()
WORKER_COMMAND = ("/usr/bin/osascript", "-l", "JavaScript", "-e", WORKER_SOURCE)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class AppleScriptWorker:
    """Serialize framed calls without launching an application for each Safari read."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._request_id = 0

    def _start(self) -> subprocess.Popen[bytes]:
        if self._process is None or self._process.poll() is not None:
            self.close()
            self._process = subprocess.Popen(
                WORKER_COMMAND,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            assert self._process.stdin is not None
            assert self._process.stdout is not None
            os.set_blocking(self._process.stdin.fileno(), False)
            os.set_blocking(self._process.stdout.fileno(), False)
        return self._process

    def run(self, source: str, *, timeout: float) -> subprocess.CompletedProcess[str]:
        """Execute once; discard a failed transport without replaying its request."""
        with self._lock:
            try:
                process = self._start()
                self._request_id += 1
                request = json.dumps(
                    {"id": self._request_id, "source": source}, ensure_ascii=True,
                ).encode("ascii") + b"\n"
                raw = self._exchange(process, request, timeout)
                response = json.loads(raw)
                if (
                    not isinstance(response, dict)
                    or type(response.get("id")) is not int
                    or response["id"] != self._request_id
                    or type(response.get("returncode")) is not int
                    or response["returncode"] not in (0, 1)
                    or not isinstance(response.get("stdout"), str)
                    or not isinstance(response.get("stderr"), str)
                ):
                    raise ValueError("Invalid worker response")
                return subprocess.CompletedProcess(
                    [WORKER_COMMAND[0]], response["returncode"],
                    response["stdout"], response["stderr"],
                )
            except subprocess.TimeoutExpired:
                self.close()
                raise
            except (OSError, ValueError) as exc:
                self.close()
                raise RuntimeError("Safari AppleScript worker transport failed.") from exc

    def _exchange(
        self, process: subprocess.Popen[bytes], request: bytes, timeout: float,
    ) -> bytes:
        """Bound both pipe writes and reads by the same deadline."""
        assert process.stdin is not None and process.stdout is not None
        deadline = time.monotonic() + timeout
        pending = memoryview(request)
        response = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(WORKER_COMMAND[0], timeout)
                for key, _events in selector.select(remaining):
                    if key.fileobj is process.stdin:
                        try:
                            written = os.write(key.fd, pending[:65_536])
                        except BlockingIOError:
                            continue
                        pending = pending[written:]
                        if not pending:
                            selector.unregister(process.stdin)
                    else:
                        try:
                            chunk = os.read(key.fd, 65_536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            raise OSError("AppleScript worker closed its output")
                        response.extend(chunk)
                        if len(response) > MAX_RESPONSE_BYTES:
                            raise ValueError("AppleScript response exceeded its limit")
                        if b"\n" in response:
                            if pending or not response.endswith(b"\n") or response.count(b"\n") != 1:
                                raise ValueError("Invalid AppleScript response framing")
                            return bytes(response)

    def close(self) -> None:
        """Reap only this worker; never close Terminal or a browser window."""
        with self._lock:
            process, self._process = self._process, None
            if process is None:
                return
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


_WORKER = AppleScriptWorker()
atexit.register(_WORKER.close)


def execute_applescript(source: str, *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Reuse one worker for all Safari contexts in the current application process."""
    return _WORKER.run(source, timeout=timeout)
