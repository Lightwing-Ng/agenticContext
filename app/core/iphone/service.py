"""The Flask-facing service for the USB iPhone runtime.

The web page and the stdio MCP server share one state directory and serialize device
work through its ``operation.lock``. This module owns the page side: it creates the
vendored ``Runtime`` lazily, once per process and only on macOS, whitelists what the
page may ask for, and removes host filesystem paths and signing identifiers from every
result before it leaves the process.

Only the standard library is imported at module import time. The vendored modules
import ``fcntl`` and drive macOS-only tooling, so they load on the first iPhone call.
"""

# Code version: v1.1.2-codex.0

from __future__ import annotations

import base64
from copy import deepcopy
import json
import logging
from collections.abc import Callable
from pathlib import Path
import re
import struct
import sys
import threading
from typing import Any


LOGGER = logging.getLogger(__name__)

ACTION_OPS = ("tap", "swipe", "type_text", "press_button", "launch_app", "observe", "find")
SETUP_ACTIONS = ("discover", "fetch", "configure", "build", "start", "stop")
SETUP_ARGUMENT_KEYS = frozenset({"udid", "team_id", "bundle_id", "job_id"})
SCREEN_ACTIONS = ("pause", "resume")
SCREEN_TOOLBAR_ACTIONS = ("refresh", "home")
MAX_FRAME_CURSOR = 2**53 - 1
MAX_APP_QUERY_LENGTH = 100
# A native capture is one bounded read; the page's action timeout is longer than this.
SCREENSHOT_TIMEOUT_SECONDS = 8
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
UNSUPPORTED_REASON = (
    "iPhone control needs macOS with Xcode, a USB connection, and WebDriverAgent."
)
# Result keys that only ever carry a host filesystem location.
HOST_PATH_KEYS = frozenset(
    {"developer_dir", "log_path", "runtime", "source_dir", "worker_entrypoint"}
)
SIGNING_TEAM_IN_TEXT = re.compile(
    r"(\bDEVELOPMENT_TEAM\s*=\s*[\"']?|\bTeam\s+[\"'])([A-Z0-9]{10})(?![A-Za-z0-9_])"
)


class IPhoneUnavailableError(RuntimeError):
    """The iPhone runtime cannot run here: unsupported platform or unusable state dir."""


class IPhoneOperationError(RuntimeError):
    """A runtime refusal or failure, carried with its stable upstream error code."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details: dict[str, Any] = sanitize_result(dict(details or {}))

    def as_dict(self) -> dict[str, Any]:
        """Return the error body; details never override the code or the message."""
        return {**self.details, "code": self.code, "message": self.message}


def mask_team_id(value: Any) -> Any:
    """Keep only the last four characters of an Apple Team ID."""
    if not isinstance(value, str) or not value:
        return value
    visible = value[-4:] if len(value) > 4 else ""
    return "*" * (len(value) - len(visible)) + visible


def _redact_home(text: str, home: str) -> str:
    """Replace the account's home directory with ``~`` so usernames never leave the host."""
    if not home or home == "/" or home not in text:
        return text
    if text == home:
        return "~"
    return text.replace(home + "/", "~/")


def sanitize_result(value: Any, *, home: str | None = None) -> Any:
    """Return a copy of a runtime result without host paths or full signing identifiers.

    Image descriptors (any mapping that has ``mimeType``) lose their local ``path``;
    path-only keys are dropped; binary locations collapse to whether they were found;
    structured and logged Team IDs are masked; the home directory inside text becomes
    ``~``. Encoded frame data is left untouched.
    """
    if home is None:
        try:
            home = str(Path.home())
        except (KeyError, RuntimeError):
            home = ""
    if isinstance(value, dict):
        is_image = "mimeType" in value
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if key in HOST_PATH_KEYS or (is_image and key == "path"):
                continue
            if key == "team_id":
                result[key] = mask_team_id(item)
            elif key == "binaries" and isinstance(item, dict):
                result[key] = {name: bool(location) for name, location in item.items()}
            elif key == "data" and isinstance(item, str):
                result[key] = item
            else:
                result[key] = sanitize_result(item, home=home)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_result(item, home=home) for item in value]
    if isinstance(value, str):
        return SIGNING_TEAM_IN_TEXT.sub(
            lambda match: match[1] + mask_team_id(match[2]), _redact_home(value, home)
        )
    return value


def sanitize_tunnel_result(value: Any) -> Any:
    """Keep native recovery evidence while directing local-only setup to its UI."""
    def rewrite(item: Any) -> Any:
        if isinstance(item, list):
            return [rewrite(child) for child in item]
        if not isinstance(item, dict):
            return item
        result = {key: rewrite(child) for key, child in item.items()}
        recovery = result.get("recovery")
        if not isinstance(recovery, dict):
            return result
        if isinstance(recovery.get("next_step"), str):
            recovery["next_step"] = recovery["next_step"].replace(
                "Inspect the image content, not its base64 text; it arrives as MCP image "
                "content, or open the local file at observation.image.path.",
                "Inspect the attached MCP image content, not its base64 text. "
                "Local image paths are not exposed through the Tunnel.",
            )
        next_arguments = recovery.get("next_arguments")
        local_only = (
            recovery.get("next_tool") in {"pua_setup", "pua_doctor"}
            or recovery.get("status_tool") in {"pua_setup", "pua_doctor"}
            or (
                recovery.get("next_tool") == "pua_ready"
                and isinstance(next_arguments, dict)
                and next_arguments.get("recover") is True
            )
        )
        if local_only:
            for key in ("next_tool", "next_arguments", "status_tool", "status_arguments", "next_steps"):
                recovery.pop(key, None)
            recovery["requires_local_setup"] = True
            recovery["next_step"] = (
                "Ask the user to inspect setup or finish recovery on this Mac's local iPhone "
                "page. After the user confirms completion, call pua_ready with screenshot=false "
                "and recover=false. Do not replay the previous phone action."
            )
            if result.get("state") in {"recovering", "recovery_required"}:
                result["message"] = (
                    "The iPhone connection needs local setup or recovery. Use the local iPhone "
                    "page before checking READY again; phone tasks require ready=true."
                )
            elif isinstance(result.get("message"), str):
                result["message"] = result["message"].replace(
                    "Run pua_doctor and inspect setup status.",
                    "Inspect setup status on this Mac's local iPhone page.",
                )
        return result

    return rewrite(sanitize_result(value))


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width and height from a complete PNG header, or ``None`` for anything else."""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if 0 < width <= 20000 and 0 < height <= 20000 else None


def _is_wda_error(error: BaseException) -> bool:
    """Recognize the vendored ``WDAError`` without importing the vendored modules."""
    return any(cls.__name__ == "WDAError" for cls in type(error).__mro__)


def _default_runtime_factory(state_dir: Path | None) -> Any:
    """Create the vendored runtime; imported here so ``fcntl`` loads only on demand."""
    from .mcp_server import Runtime

    return Runtime(state_dir=state_dir)


def _default_wda_client_factory(base_url: str, timeout: float) -> Any:
    """Create a short-lived loopback WebDriverAgent client for one native capture."""
    from .wda_client import WDAClient

    return WDAClient(base_url, timeout=timeout)


def _invalid_argument(message: str, **details: Any) -> IPhoneOperationError:
    return IPhoneOperationError("invalid_argument", message, {"action_executed": False, **details})


class IPhoneService:
    """One lazily created iPhone runtime per process, shared by every page request."""

    def __init__(
        self,
        state_dir: Path | None = None,
        *,
        platform: str | None = None,
        runtime_factory: Callable[[Path | None], Any] | None = None,
        wda_client_factory: Callable[[str, float], Any] | None = None,
        external_operations_enabled: Callable[[], bool] | None = None,
    ) -> None:
        # Construction never touches the device, starts a process, or creates directories.
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self.platform = platform or sys.platform
        self.supported = self.platform == "darwin"
        self._runtime_factory = runtime_factory or _default_runtime_factory
        self._wda_client_factory = wda_client_factory or _default_wda_client_factory
        self._runtime_lock = threading.Lock()
        self._runtime: Any = None
        self._in_flight_lock = threading.Lock()
        self._in_flight = 0
        self._external_operations_enabled = external_operations_enabled or (lambda: True)
        self._tunnel_lock = threading.Lock()
        self._tunnel_enabled = False

    def tunnel_status(self) -> dict[str, bool]:
        """Read the local, process-scoped grant without creating the phone runtime."""
        with self._tunnel_lock:
            available = self.supported and bool(self._external_operations_enabled())
            return {"available": available, "enabled": available and self._tunnel_enabled}

    def set_tunnel_enabled(self, enabled: bool) -> dict[str, bool]:
        """Grant or revoke new ChatGPT phone calls until this application exits."""
        if not isinstance(enabled, bool):
            raise _invalid_argument("Send enabled as a boolean.")
        self._require_supported()
        with self._tunnel_lock:
            if enabled and not self._external_operations_enabled():
                raise IPhoneOperationError("operations_disabled", "External operations are disabled.")
            self._tunnel_enabled = enabled
        return self.tunnel_status()

    def tunnel_tools(self) -> list[dict[str, Any]]:
        """Publish phone schemas only under an explicit local grant; never start WDA."""
        if not self.tunnel_status()["enabled"]:
            return []
        from .mcp_server import LISTED_TOOLS

        tools = deepcopy([
            tool for tool in LISTED_TOOLS
            if tool["name"] not in {"pua_setup", "pua_doctor"}
        ])
        for tool in tools:
            if tool["name"] == "pua_ready":
                tool["inputSchema"]["examples"] = [{"recover": False, "screenshot": False}]
                tool["inputSchema"]["properties"]["recover"] = {
                    "type": "boolean", "enum": [False], "default": False,
                    "description": "Recovery is local-only. Use the iPhone page to restart WDA.",
                }
                tool["description"] = (
                    "Verify the phone connection before any task. Recovery and setup are managed "
                    "on the local iPhone page. Pass recover=false; this tool never starts WDA."
                )
        return tools

    def tunnel_instructions(self) -> str:
        """Retain phone operating rules without advertising local-only setup tools."""
        if not self.tunnel_status()["enabled"]:
            return ""
        from .mcp_server import INSTRUCTIONS

        operations = INSTRUCTIONS[INSTRUCTIONS.index("Results are one compact JSON text."):]
        operations = operations.replace(
            "If the image content is unavailable, open the local file at image.path or error.observation.image.path.",
            "If image content is unavailable, request a new screenshot; host image paths are not exposed.",
        )
        return (
            "The user explicitly enabled iPhone access through this ChatGPT Tunnel. "
            "The pua_ tools operate the connected phone, independently of project file access. "
            "First call pua_ready(screenshot=false, recover=false); only ready=true permits tasks. "
            "If unavailable or recovering, ask the user to finish setup or recovery on the local "
            "iPhone page, then check READY again. Setup, signing, install, start, and stop are "
            "local-only; pua_setup and pua_doctor are not available through the Tunnel. "
            "Access can be revoked on the iPhone page and resets off after an application restart. "
            + operations
        )

    def call_tunnel_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Admit one phone call and preserve native MCP image blocks, without host paths."""
        available = {tool["name"] for tool in self.tunnel_tools()}
        if name not in available:
            raise IPhoneOperationError(
                "iphone_access_disabled", "Enable ChatGPT Tunnel access on this Mac's iPhone page."
            )
        if not isinstance(arguments, dict):
            raise _invalid_argument("Tool arguments must be an object.")
        arguments = dict(arguments)
        if name == "pua_ready":
            if arguments.get("recover", False) is not False:
                raise _invalid_argument("Use the local iPhone page for runtime recovery.")
            arguments["recover"] = False
        runtime = self._ensure_runtime()
        with self._tunnel_lock:
            if not self._tunnel_enabled or not self._external_operations_enabled():
                raise IPhoneOperationError("iphone_access_disabled", "iPhone Tunnel access was revoked.")
        from .mcp_server import tool_result

        result = self._guarded(lambda: tool_result(runtime, {"name": name, "arguments": arguments}))
        for block in result.get("content", []):
            if block.get("type") == "text":
                block["text"] = json.dumps(
                    sanitize_tunnel_result(json.loads(block["text"])), ensure_ascii=False, separators=(",", ":")
                )
        if "structuredContent" in result:
            result["structuredContent"] = sanitize_tunnel_result(result["structuredContent"])
        return result

    # Runtime lifecycle
    def started(self) -> bool:
        """Return whether this process has already created the runtime."""
        return self._runtime is not None

    def _require_supported(self) -> None:
        if not self.supported:
            raise IPhoneUnavailableError(UNSUPPORTED_REASON)

    def _ensure_runtime(self) -> Any:
        """Create the runtime on first use; a failed creation is retried on the next call."""
        self._require_supported()
        runtime = self._runtime
        if runtime is not None:
            return runtime
        with self._runtime_lock:
            if self._runtime is None:
                try:
                    self._runtime = self._runtime_factory(self._state_dir)
                except (ValueError, OSError) as exc:
                    raise IPhoneUnavailableError(str(exc)) from exc
            return self._runtime

    def stop_at_exit(self) -> None:
        """Close the preview child and HTTP client; leave the shared WDA start job running."""
        runtime = self._runtime
        if runtime is None:
            return
        try:
            runtime.close()
        except Exception as exc:  # Shutdown must continue for every other service.
            LOGGER.warning("Could not close the iPhone runtime: %s", type(exc).__name__)

    # Error translation
    @staticmethod
    def _translate(error: Exception) -> Exception:
        if isinstance(error, (IPhoneOperationError, IPhoneUnavailableError)):
            return error
        if _is_wda_error(error):
            details = getattr(error, "details", None)
            return IPhoneOperationError(
                getattr(error, "code", "internal_error"),
                str(error),
                {
                    "uncertain": bool(getattr(error, "uncertain", False)),
                    **(details if isinstance(details, dict) else {}),
                },
            )
        LOGGER.error("The iPhone runtime failed: %s", type(error).__name__)
        return IPhoneOperationError(
            "internal_error",
            "The local iPhone runtime failed. Check the setup status or the service log.",
            {"uncertain": True},
        )

    def _guarded(self, operation: Callable[[], Any], *, track: bool = True) -> Any:
        """Run one runtime operation, counting it as busy and translating its errors."""
        if track:
            with self._in_flight_lock:
                self._in_flight += 1
        try:
            return operation()
        except Exception as exc:
            raise self._translate(exc) from exc
        finally:
            if track:
                with self._in_flight_lock:
                    self._in_flight -= 1

    def _call(self, name: str, arguments: dict[str, Any], *, track: bool = True) -> dict[str, Any]:
        runtime = self._ensure_runtime()
        return sanitize_result(self._guarded(lambda: runtime.call(name, arguments), track=track))

    # Read-only state
    def status(self) -> dict[str, Any]:
        """Return setup, preview, and activity state without taking the operation lock."""
        if not self.supported:
            return {"supported": False, "started": False, "reason": UNSUPPORTED_REASON}
        runtime = self._ensure_runtime()

        def read() -> dict[str, Any]:
            screen = runtime.screen
            return {
                "setup": runtime.setup_manager.setup(action="status"),
                "preview": screen.pause_status(),
                "screen_busy": bool(screen.start().get("busy")),
            }

        snapshot = self._guarded(read, track=False)
        with self._in_flight_lock:
            busy = self._in_flight > 0
        return {
            "supported": True,
            "started": self.started(),
            "state_dir": str(runtime.state_dir),
            "setup": sanitize_result(snapshot["setup"]),
            "preview": sanitize_result(snapshot["preview"]),
            "busy": busy or snapshot["screen_busy"],
        }

    def doctor(self) -> dict[str, Any]:
        """Diagnose this Mac and the phone directly, outside the shared operation lock."""
        runtime = self._ensure_runtime()
        return sanitize_result(self._guarded(runtime.setup_manager.doctor))

    # Setup and connection
    def setup(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one whitelisted setup step through the runtime so its side effects apply."""
        self._require_supported()
        if action not in SETUP_ACTIONS:
            raise _invalid_argument(
                "Choose a supported setup action.", allowed_actions=list(SETUP_ACTIONS)
            )
        if not isinstance(arguments, dict):
            raise _invalid_argument("Setup arguments must be an object.")
        unknown = sorted(str(key) for key in set(arguments) - SETUP_ARGUMENT_KEYS)
        if unknown:
            raise _invalid_argument(
                "Unknown setup fields: " + ", ".join(unknown) + ".",
                unknown_fields=unknown,
                allowed_fields=sorted(SETUP_ARGUMENT_KEYS),
            )
        call_arguments = {"action": action, **arguments}
        if action == "start":
            # The page polls status, so a start never holds the request open.
            call_arguments["wait_seconds"] = 0
        return self._call("pua_setup", call_arguments)

    def ready(self) -> dict[str, Any]:
        """Verify the whole phone channel, allowing one bounded owned recovery."""
        self._require_supported()
        return self._call("pua_ready", {"screenshot": False, "recover": True})

    # Live preview
    def frame(self, after_seq: int, last_event_id: int) -> dict[str, Any]:
        """Return the cached live preview frame and action cursor events."""
        self._require_supported()
        for name, value in (("after_seq", after_seq), ("last_event_id", last_event_id)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_FRAME_CURSOR:
                raise _invalid_argument(f"{name} must be an integer between 0 and {MAX_FRAME_CURSOR}.")
        return self._call(
            "pua_screen_frame",
            {"after_seq": after_seq, "last_event_id": last_event_id},
            track=False,
        )

    def screen(self, action: str) -> dict[str, Any]:
        """Pause the preview before user authentication, or resume it afterward."""
        self._require_supported()
        if action not in SCREEN_ACTIONS:
            raise _invalid_argument("Choose pause or resume.", allowed_actions=list(SCREEN_ACTIONS))
        return self._call("pua_screen", {"action": action}, track=False)

    def screen_action(self, action: str) -> dict[str, Any]:
        """Run one preview toolbar action the user pressed: refresh or Home."""
        self._require_supported()
        if action not in SCREEN_TOOLBAR_ACTIONS:
            raise _invalid_argument(
                "Choose refresh or home.", allowed_actions=list(SCREEN_TOOLBAR_ACTIONS)
            )
        return self._call("pua_screen_action", {"action": action}, track=action == "home")

    def screenshot_png(self) -> bytes:
        """Return one native PNG capture, refused while the preview is paused."""
        runtime = self._ensure_runtime()

        def capture() -> bytes:
            if runtime.screen.paused():
                raise IPhoneOperationError(
                    "preview_paused",
                    "The preview is paused while the user authenticates on the iPhone. "
                    "No capture was made.",
                    {"action_executed": False},
                )
            client = self._wda_client_factory(runtime.base_url, SCREENSHOT_TIMEOUT_SECONDS)
            try:
                response = client.request("GET", "/screenshot")
            finally:
                client.close()
            encoded = response.get("value") if isinstance(response, dict) else None
            try:
                data = base64.b64decode(encoded, validate=True)
            except (TypeError, ValueError) as exc:
                raise IPhoneOperationError(
                    "invalid_response", "WebDriverAgent returned an invalid screenshot encoding."
                ) from exc
            if png_dimensions(data[:32]) is None:
                raise IPhoneOperationError(
                    "invalid_response", "WebDriverAgent returned a screenshot that is not a PNG."
                )
            return data

        return self._guarded(capture)

    # Phone actions
    def act(self, op: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one whitelisted phone action; the runtime validates its closed schema."""
        self._require_supported()
        if op not in ACTION_OPS:
            raise _invalid_argument("Choose a supported iPhone action.", allowed_ops=list(ACTION_OPS))
        if not isinstance(arguments, dict):
            raise _invalid_argument("Action arguments must be an object.")
        return self._call(f"pua_{op}", arguments)

    def apps(self, query: str) -> dict[str, Any]:
        """Resolve an app name to bundle IDs: installed apps, the catalog, then Apple."""
        self._require_supported()
        text = query.strip() if isinstance(query, str) else ""
        if not 1 <= len(text) <= MAX_APP_QUERY_LENGTH:
            raise _invalid_argument(f"Enter an app name of 1 to {MAX_APP_QUERY_LENGTH} characters.")
        return self._call("pua_apps", {"query": text, "source": "auto", "limit": 10})


__all__ = [
    "ACTION_OPS",
    "SETUP_ACTIONS",
    "IPhoneOperationError",
    "IPhoneService",
    "IPhoneUnavailableError",
    "mask_team_id",
    "png_dimensions",
    "sanitize_result",
]
