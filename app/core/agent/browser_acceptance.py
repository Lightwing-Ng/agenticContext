"""Safe local browser acceptance for Agent workspaces.

Code version: v1.0.0-codex.1
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import sys
from threading import Thread
import time
from typing import Any, Callable, ContextManager
from urllib.parse import urlsplit


_PREVIEW_BOOT_TIMEOUT_SECONDS = 5.0
_MAX_BROWSER_ERRORS = 20
_MAX_BROWSER_ERROR_CHARS = 500
_MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024
_MAX_TRACE_BYTES = 8 * 1024 * 1024
_VIEWPORTS = (
    ("desktop", 1440, 900),
    ("narrow", 390, 844),
)
_PROJECT_CONFIG_NAME = ".agenticContext-browser-acceptance.json"
_PROJECT_CONFIG_MAX_BYTES = 64 * 1024
_PREVIEW_SERVER_SCRIPT = "\n".join(
    (
        "import http.server",
        "import json",
        "from pathlib import Path",
        "import sys",
        "from urllib.parse import unquote, urlsplit",
        "root = Path(sys.argv[1]).resolve()",
        "requested_port = int(sys.argv[2])",
        "blocked_parts = {'.computer-use-agent', '.git', '.aws', '.ssh', '.venv', 'local_store', 'logs', 'node_modules', 'venv'}",
        "blocked_names = {'.env', '.git-credentials', '.netrc', '.npmrc', '.pypirc', 'cookies', 'cookies.json', 'credential', 'credentials', 'credentials.json', 'id_dsa', 'id_ed25519', 'id_ecdsa', 'id_rsa', 'secret', 'secrets', 'secrets.json'}",
        "blocked_suffixes = {'.key', '.pem', '.p12', '.pfx'}",
        "class Handler(http.server.SimpleHTTPRequestHandler):",
        "    def __init__(self, *args, **kwargs):",
        "        super().__init__(*args, directory=str(root), **kwargs)",
        "    def _allowed(self):",
        "        relative = Path(unquote(urlsplit(self.path).path).lstrip('/'))",
        "        try:",
        "            candidate = (root / relative).resolve(strict=False)",
        "            clean = candidate.relative_to(root)",
        "        except (OSError, ValueError):",
        "            return False",
        "        current = root",
        "        for part in clean.parts:",
        "            lowered = part.casefold()",
        "            if lowered.startswith('.') or lowered in blocked_parts or lowered in blocked_names or Path(lowered).suffix in blocked_suffixes:",
        "                return False",
        "            current = current / part",
        "            if current.is_symlink():",
        "                return False",
        "        return True",
        "    def list_directory(self, path):",
        "        self.send_error(404)",
        "        return None",
        "    def do_GET(self):",
        "        if not self._allowed():",
        "            self.send_error(404)",
        "            return",
        "        super().do_GET()",
        "    def do_HEAD(self):",
        "        if not self._allowed():",
        "            self.send_error(404)",
        "            return",
        "        super().do_HEAD()",
        "    def log_message(self, format, *args):",
        "        return",
        "class Server(http.server.ThreadingHTTPServer):",
        "    daemon_threads = True",
        "try:",
        "    server = Server(('127.0.0.1', requested_port), Handler)",
        "except OSError:",
        "    print(json.dumps({'error': 'Preview port is unavailable.'}), flush=True)",
        "    raise SystemExit(2)",
        "print(json.dumps({'port': int(server.server_address[1])}), flush=True)",
        "server.serve_forever(poll_interval=0.1)",
    )
)


@dataclass(frozen=True, slots=True)
class BrowserAcceptanceRequest:
    """Declarative local-browser acceptance inputs."""

    root: Path
    target: str
    port: int
    expected_text: tuple[str, ...]
    expected_selectors: tuple[str, ...]
    protected_ports: frozenset[int]
    timeout_seconds: float
    artifact_root: Path


def _bounded_error(value: Any) -> str:
    """Return a content-free fingerprint for one bounded browser diagnostic."""
    text = str(value or "").strip()[:_MAX_BROWSER_ERROR_CHARS]
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"sha256:{digest};length:{len(text)}"


def load_project_protected_ports(workspace_root: Path) -> frozenset[int]:
    """Load additive protected ports from one small project-owned configuration file."""
    config_path = workspace_root / _PROJECT_CONFIG_NAME
    if not config_path.exists():
        return frozenset()
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError("Browser acceptance protected-port configuration must be a regular file.")
    if config_path.stat().st_size > _PROJECT_CONFIG_MAX_BYTES:
        raise ValueError("Browser acceptance protected-port configuration is too large.")
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Browser acceptance protected-port configuration is invalid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) - {"schema_version", "protected_ports"}:
        raise ValueError("Browser acceptance protected-port configuration has unsupported fields.")
    if payload.get("schema_version") != 1:
        raise ValueError("Browser acceptance protected-port configuration schema_version must be 1.")
    ports = payload.get("protected_ports", [])
    if not isinstance(ports, list) or len(ports) > 64:
        raise ValueError("Browser acceptance protected_ports must be an array of at most 64 ports.")
    normalized: set[int] = set()
    for port in ports:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Browser acceptance protected_ports contains an invalid port.")
        normalized.add(port)
    return frozenset(normalized)


def _loopback_host(host: str | None) -> bool:
    return str(host or "").strip().lower() in {"127.0.0.1", "localhost", "::1"}


def validate_browser_acceptance_target(target: str, owned_port: int) -> str:
    """Return one owned local URL and reject external, credentialed, and file targets."""
    raw = str(target or "/").strip() or "/"
    if raw.startswith("//"):
        raise ValueError("Browser acceptance targets must not use scheme-relative URLs.")
    parsed = (
        urlsplit(f"http://127.0.0.1:{owned_port}{raw}")
        if raw.startswith("/")
        else urlsplit(raw)
    )
    if parsed.scheme.lower() != "http":
        raise ValueError("Browser acceptance targets must use local HTTP only.")
    if parsed.username or parsed.password:
        raise ValueError("Browser acceptance targets must not contain credentials.")
    if not _loopback_host(parsed.hostname):
        raise ValueError("Browser acceptance targets must stay on loopback.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Browser acceptance target port is invalid.") from exc
    if port != owned_port:
        raise ValueError("Browser acceptance targets must use the owned preview port.")
    return parsed.geturl()


def validate_browser_request_url(url: str, owned_port: int) -> bool:
    """Return whether one request can stay inside the clean local-browser boundary."""
    parsed = urlsplit(str(url or ""))
    scheme = parsed.scheme.lower()
    if scheme in {"about", "blob", "data"}:
        return True
    if scheme not in {"http", "https"}:
        return False
    if parsed.username or parsed.password or not _loopback_host(parsed.hostname):
        return False
    try:
        return parsed.port == owned_port
    except ValueError:
        return False


def _preview_boot_record(
    process: subprocess.Popen[str],
    should_stop: Callable[[], bool],
) -> dict[str, Any]:
    """Read one bounded startup receipt without blocking Stop forever."""
    queue: Queue[str] = Queue(maxsize=1)

    def read_line() -> None:
        stream = process.stdout
        line = stream.readline() if stream is not None else ""
        try:
            queue.put_nowait(line)
        except Exception:
            return

    Thread(
        target=read_line,
        daemon=True,
        name="browser-acceptance-preview-boot",
    ).start()
    deadline = time.monotonic() + _PREVIEW_BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if should_stop():
            raise RuntimeError("Stop requested.")
        try:
            line = queue.get(timeout=0.05)
        except Empty:
            if process.poll() is not None:
                raise RuntimeError(
                    "The local preview process exited before reporting its port."
                )
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "The local preview process returned an invalid startup receipt."
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                "The local preview process returned an invalid startup receipt."
            )
        return payload
    raise RuntimeError("The local preview process did not become ready in time.")


def _artifact_reference(artifact_root: Path, path: Path) -> str:
    return path.relative_to(artifact_root).as_posix()


def _retain_bounded_artifact(path: Path, maximum_bytes: int) -> bool:
    """Retain only bounded evidence artifacts; oversize files are task-owned cleanup."""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= maximum_bytes:
        return True
    try:
        path.unlink()
    except OSError:
        pass
    return False


def run_browser_acceptance(
    request: BrowserAcceptanceRequest,
    *,
    playwright_context: Callable[[], ContextManager[Any]],
    process_group_options: Callable[[], dict[str, Any]],
    stop_process: Callable[[subprocess.Popen[Any]], None],
    process_changed: Callable[[subprocess.Popen[str] | None], None],
    should_stop: Callable[[], bool],
) -> dict[str, Any]:
    """Serve workspace files and run clean Chromium checks at two fixed viewports."""
    if request.port and request.port in request.protected_ports:
        raise ValueError("Browser acceptance cannot use a protected application port.")
    if request.port and not 1024 <= request.port <= 65535:
        raise ValueError(
            "Browser acceptance ports must be between 1024 and 65535, or 0 for automatic selection."
        )
    if not request.root.is_dir():
        raise ValueError("Browser acceptance root must be a workspace directory.")

    request.artifact_root.mkdir(parents=True, exist_ok=True)
    run_id = f"browser-acceptance-{time.time_ns():x}"
    run_root = request.artifact_root / run_id
    run_root.mkdir(parents=False, exist_ok=False)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-u",
                "-c",
                _PREVIEW_SERVER_SCRIPT,
                str(request.root),
                str(request.port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(request.root),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **process_group_options(),
        )
    except OSError:
        shutil.rmtree(run_root, ignore_errors=True)
        raise
    browser = None
    context = None
    completed = False
    process_registered = False
    started = time.monotonic()
    try:
        process_changed(process)
        process_registered = True
        receipt = _preview_boot_record(process, should_stop)
        if receipt.get("error"):
            raise RuntimeError(str(receipt["error"])[:500])
        owned_port = int(receipt.get("port") or 0)
        if owned_port <= 0 or owned_port in request.protected_ports:
            raise RuntimeError("The local preview selected an invalid or protected port.")
        target_url = validate_browser_acceptance_target(request.target, owned_port)

        with playwright_context() as playwright:
            launch_options: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-domain-reliability",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    "--proxy-server=http://127.0.0.1:9",
                    "--proxy-bypass-list=127.0.0.1;localhost",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            }
            executable = Path(str(playwright.chromium.executable_path or ""))
            if not executable.is_file():
                launch_options["channel"] = "chrome"
            browser = playwright.chromium.launch(**launch_options)
            results: list[dict[str, Any]] = []
            for label, width, height in _VIEWPORTS:
                if should_stop():
                    raise RuntimeError("Stop requested.")
                if time.monotonic() - started > request.timeout_seconds:
                    raise RuntimeError("Browser acceptance timed out.")
                console_errors: list[str] = []
                page_errors: list[str] = []
                blocked_requests: list[str] = []
                context = browser.new_context(viewport={"width": width, "height": height})
                context.tracing.start(screenshots=False, snapshots=False, sources=False)
                page = context.new_page()

                def route_request(route: Any) -> None:
                    request_url = str(route.request.url or "")
                    if validate_browser_request_url(request_url, owned_port):
                        route.continue_()
                        return
                    if len(blocked_requests) < _MAX_BROWSER_ERRORS:
                        parsed = urlsplit(request_url)
                        host = str(parsed.hostname or "")
                        try:
                            port = parsed.port
                        except ValueError:
                            port = None
                        authority = f"{host}:{port}" if port is not None else host
                        blocked_requests.append(
                            _bounded_error(f"{parsed.scheme}://{authority}")
                        )
                    route.abort()

                context.route("**/*", route_request)
                page.on(
                    "console",
                    lambda message: console_errors.append(_bounded_error(message.text))
                    if message.type == "error"
                    and len(console_errors) < _MAX_BROWSER_ERRORS
                    else None,
                )
                page.on(
                    "pageerror",
                    lambda error: page_errors.append(_bounded_error(error))
                    if len(page_errors) < _MAX_BROWSER_ERRORS
                    else None,
                )
                response = page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=min(15_000, max(1_000, int(request.timeout_seconds * 1_000))),
                )
                http_status = int(response.status) if response is not None else 0
                assertions: list[dict[str, Any]] = []
                for index, expected in enumerate(request.expected_text):
                    assertions.append(
                        {
                            "kind": "text",
                            "index": index,
                            "ok": bool(
                                page.evaluate(
                                    "expected => Boolean(document.body?.innerText.includes(expected))",
                                    expected,
                                )
                            ),
                        }
                    )
                for index, selector in enumerate(request.expected_selectors):
                    assertions.append(
                        {
                            "kind": "selector",
                            "index": index,
                            "ok": page.locator(selector).count() > 0,
                        }
                    )
                screenshot_path = run_root / f"{label}.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                screenshot_ref = (
                    _artifact_reference(request.artifact_root, screenshot_path)
                    if _retain_bounded_artifact(
                        screenshot_path,
                        _MAX_SCREENSHOT_BYTES,
                    )
                    else None
                )
                trace_path = run_root / f"{label}-trace.zip"
                context.tracing.stop(path=str(trace_path))
                trace_ref = (
                    _artifact_reference(request.artifact_root, trace_path)
                    if _retain_bounded_artifact(trace_path, _MAX_TRACE_BYTES)
                    else None
                )
                viewport_ok = (
                    200 <= http_status < 400
                    and all(item["ok"] for item in assertions)
                    and not console_errors
                    and not page_errors
                    and not blocked_requests
                )
                results.append(
                    {
                        "viewport": {
                            "name": label,
                            "width": width,
                            "height": height,
                        },
                        "ok": viewport_ok,
                        "http_status": http_status,
                        "assertions": assertions,
                        "console_errors": console_errors,
                        "page_errors": page_errors,
                        "blocked_requests": blocked_requests,
                        "screenshot_path": screenshot_ref,
                        "trace_path": trace_ref,
                    }
                )
                context.close()
                context = None

            ok = all(result["ok"] for result in results)
            completed = True
            return {
                "ok": ok,
                "action": "browser_acceptance",
                "preview": {
                    "host": "127.0.0.1",
                    "port": owned_port,
                },
                "viewports": results,
                "artifact_run": run_id,
                "error": ""
                if ok
                else "Browser acceptance assertions or local-only request policy failed.",
            }
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        try:
            if process_registered:
                process_changed(None)
        except Exception:
            pass
        finally:
            try:
                stop_process(process)
            finally:
                if not completed:
                    shutil.rmtree(run_root, ignore_errors=True)
