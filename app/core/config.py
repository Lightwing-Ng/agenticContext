"""Configuration helpers."""

# Code version: v1.19.1-codex.1

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT_ENV = "AGENTIC_CONTEXT_RUNTIME_ROOT"
SETTINGS_PATH_ENV = "AGENTIC_CONTEXT_SETTINGS_PATH"
LEGACY_RUNTIME_ROOT_ENV = "CACHELIKES_RUNTIME_ROOT"
LEGACY_SETTINGS_PATH_ENV = "CACHELIKES_SETTINGS_PATH"
SETTINGS_DIRECTORY_NAME = "agenticContext"
LEGACY_SETTINGS_DIRECTORY_NAME = "CacheLikesFromTwitter"


def is_windows_host() -> bool:
    """Return whether the current Python process runs on Windows."""
    return os.name == "nt" or sys.platform.startswith("win")


def is_macos_host() -> bool:
    """Return whether the current Python process runs on macOS."""
    return sys.platform == "darwin"


def _windows_app_data_root(variable_name: str, fallback_name: str) -> Path:
    """Resolve one Windows application-data root without requiring a shell."""
    configured = os.environ.get(variable_name, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "AppData" / fallback_name


def _configured_environment_value(primary_name: str, legacy_name: str) -> str:
    """Read the current environment name before its legacy compatibility alias."""
    return (
        os.environ.get(primary_name, "").strip()
        or os.environ.get(legacy_name, "").strip()
    )


def default_chrome_user_data_dir() -> Path:
    """Return the platform-native Chrome user-data directory."""
    if is_windows_host():
        return _windows_app_data_root("LOCALAPPDATA", "Local") / "Google/Chrome/User Data"
    if is_macos_host():
        return Path.home() / "Library/Application Support/Google/Chrome"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "google-chrome"


def default_edge_user_data_dir() -> Path:
    """Return the platform-native Microsoft Edge user-data directory."""
    if is_windows_host():
        return _windows_app_data_root("LOCALAPPDATA", "Local") / "Microsoft/Edge/User Data"
    if is_macos_host():
        return Path.home() / "Library/Application Support/Microsoft Edge"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "microsoft-edge"


def normalize_host_browser(value: object, fallback: str) -> str:
    """Keep persisted browser selections valid on the current host."""
    selected = str(value or fallback).strip().lower() or fallback
    if is_windows_host() and selected == "safari":
        return fallback
    return selected


def resolve_runtime_root() -> Path:
    """Return the optional runtime root used by isolated test processes."""
    configured_root = _configured_environment_value(
        RUNTIME_ROOT_ENV,
        LEGACY_RUNTIME_ROOT_ENV,
    )
    if not configured_root:
        return PROJECT_ROOT
    return Path(configured_root).expanduser().resolve(strict=False)


def runtime_root_is_overridden() -> bool:
    """Return whether a caller explicitly redirected the runtime root."""
    return bool(
        _configured_environment_value(
            RUNTIME_ROOT_ENV,
            LEGACY_RUNTIME_ROOT_ENV,
        )
    )


RUNTIME_ROOT = resolve_runtime_root()
LOCAL_STORE_ROOT = RUNTIME_ROOT / "local_store"
BETA_STORE_ROOT = LOCAL_STORE_ROOT / "beta"
MEDIA_STORE_DIRNAME = "media"
MEDIA_STORE_ROOT = LOCAL_STORE_ROOT / MEDIA_STORE_DIRNAME
X_LOCAL_STORE_DIRNAME = "x"
LOGS_ROOT = RUNTIME_ROOT / "logs"
LEGACY_SETTINGS_PATH = RUNTIME_ROOT / ".cachelikes-settings.json"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8666
DEFAULT_CHROME_USER_DATA_DIR = default_chrome_user_data_dir()
# Safari is opt-in: its authenticated Apple Events path owns a real Safari
# window, while Edge provides the non-disruptive default on macOS and Windows.
DEFAULT_GEMINI_BROWSER = "edge"
DEFAULT_CHROME_PROFILE_DIRECTORY = "Default"
DEFAULT_SHADOW_BACKUP_DESTINATION = (
    Path.home() / "AICaches"
)
DEFAULT_DOWNLOAD_WORKERS = 4
MIN_DOWNLOAD_WORKERS = 1
MAX_DOWNLOAD_WORKERS = 8
DEFAULT_MAX_MEDIA_FILE_SIZE_MIB = 50
MIN_MAX_MEDIA_FILE_SIZE_MIB = 1
MAX_MAX_MEDIA_FILE_SIZE_MIB = 10_240
DEFAULT_CHATGPT_PROJECT_URL = "https://chatgpt.com/g/g-p-demo-project/project"
DEFAULT_CHATGPT_PROJECT_NAME = "demo-project"
DEFAULT_CHATGPT_STARTUP_TIMEOUT_SECONDS = 30.0
DEFAULT_CHATGPT_SCAN_WAIT_SECONDS = 0.5
DEFAULT_GEMINI_MAX_CONVERSATIONS = 1_000
DEFAULT_GEMINI_SCROLL_PAUSE_SECONDS = 0.75
DEFAULT_GEMINI_STALE_ROUND_LIMIT = 5
MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS = 1.0
MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS = 600.0
MIN_CHATGPT_SCAN_WAIT_SECONDS = 0.1
MAX_CHATGPT_SCAN_WAIT_SECONDS = 5.0


def normalize_download_workers(value: object, fallback: int = DEFAULT_DOWNLOAD_WORKERS) -> int:
    """Keep the legacy shared worker setting inside the application hard limit."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(fallback)
    return min(MAX_DOWNLOAD_WORKERS, max(MIN_DOWNLOAD_WORKERS, parsed))


def default_settings_path() -> Path:
    """Store local settings outside the Git worktree to avoid accidental commits."""
    configured_path = _configured_environment_value(
        SETTINGS_PATH_ENV,
        LEGACY_SETTINGS_PATH_ENV,
    )
    if configured_path:
        return Path(configured_path).expanduser().resolve(strict=False)
    if is_windows_host():
        return (
            _windows_app_data_root("APPDATA", "Roaming")
            / f"{SETTINGS_DIRECTORY_NAME}/settings.json"
        )
    if is_macos_host():
        return Path.home() / f"Library/Application Support/{SETTINGS_DIRECTORY_NAME}/settings.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / f"{SETTINGS_DIRECTORY_NAME}/settings.json"


def legacy_default_settings_path() -> Path:
    """Return the pre-agenticContext settings path for a one-way read fallback."""
    if is_windows_host():
        return (
            _windows_app_data_root("APPDATA", "Roaming")
            / f"{LEGACY_SETTINGS_DIRECTORY_NAME}/settings.json"
        )
    if is_macos_host():
        return Path.home() / f"Library/Application Support/{LEGACY_SETTINGS_DIRECTORY_NAME}/settings.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / f"{LEGACY_SETTINGS_DIRECTORY_NAME}/settings.json"


# Backward-compatible import-time snapshot. Runtime read/write helpers resolve the
# current environment again so tests and isolated processes can redirect settings safely.
SETTINGS_PATH = default_settings_path()


@dataclass(slots=True)
class CrawlConfig:
    """Runtime configuration for a single cache job."""

    headless: bool = False
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS
    max_media_items: int = 10
    max_scroll_rounds: int = 200
    scroll_pause_seconds: float = 1.2
    stale_round_limit: int = 8
    x_browser: str = "chrome"
    grok_browser: str = "edge"
    chatgpt_browser: str = "edge"
    gemini_browser: str = DEFAULT_GEMINI_BROWSER
    claude_browser: str = "edge"
    zhihu_browser: str = "edge"
    gemini_max_conversations: int = DEFAULT_GEMINI_MAX_CONVERSATIONS
    gemini_scroll_pause_seconds: float = DEFAULT_GEMINI_SCROLL_PAUSE_SECONDS
    gemini_stale_round_limit: int = DEFAULT_GEMINI_STALE_ROUND_LIMIT
    chatgpt_project_url: str = DEFAULT_CHATGPT_PROJECT_URL
    chatgpt_project_name: str = DEFAULT_CHATGPT_PROJECT_NAME
    chatgpt_startup_timeout_seconds: float = DEFAULT_CHATGPT_STARTUP_TIMEOUT_SECONDS
    chatgpt_scan_wait_seconds: float = DEFAULT_CHATGPT_SCAN_WAIT_SECONDS
    cache_scan_waits: dict[str, float] = field(default_factory=dict)
    chatgpt_text_startup_timeout_seconds: float = DEFAULT_CHATGPT_STARTUP_TIMEOUT_SECONDS
    chrome_user_data_dir: Path = DEFAULT_CHROME_USER_DATA_DIR
    chrome_profile_directory: str = DEFAULT_CHROME_PROFILE_DIRECTORY
    account_name_override: str = ""
    shadow_backup_enabled: bool = True
    shadow_backup_auto_sync: bool = True
    shadow_backup_mirror_deletions: bool = False
    shadow_backup_destination: Path = DEFAULT_SHADOW_BACKUP_DESTINATION
    max_media_file_size_mib: int = DEFAULT_MAX_MEDIA_FILE_SIZE_MIB

    def __post_init__(self) -> None:
        """Normalize concurrency even when a caller constructs config directly."""
        self.download_workers = normalize_download_workers(self.download_workers)

    def cache_scan_wait(self, provider: str, mode: str) -> float:
        """Read an independent scan interval without changing legacy defaults."""
        defaults = {"chatgpt_text": 0.0, "claude_text": 0.0, "grok_text": 0.0, "grok_media": 0.8}
        key = f"{provider}_{mode}"
        fallback = defaults.get(key, 0.0)
        return _clamp_float_setting(self.cache_scan_waits.get(key, fallback), fallback, 0.0, 60.0)

    @property
    def max_media_file_size_bytes(self) -> int:
        """Return the universal media-file limit in bytes."""
        return self.max_media_file_size_mib * 1024 * 1024

    def sanitized_account_name(self, fallback: str) -> str:
        raw_name = self.account_name_override.strip() or fallback.strip() or "unknown_account"
        safe_name = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in raw_name)
        return safe_name.strip("._") or "unknown_account"


def load_saved_config(settings_path: Path | None = None) -> CrawlConfig:
    """Load persisted crawler settings, or defaults when none exist."""
    resolved_settings_path = settings_path if settings_path is not None else default_settings_path()
    candidate_paths = [resolved_settings_path]
    if resolved_settings_path == default_settings_path():
        for fallback_path in (legacy_default_settings_path(), LEGACY_SETTINGS_PATH):
            if fallback_path not in candidate_paths:
                candidate_paths.append(fallback_path)

    payload: dict[str, object] | None = None
    for candidate_path in candidate_paths:
        if not candidate_path.exists():
            continue

        try:
            payload = json.loads(candidate_path.read_text())
            break
        except (OSError, json.JSONDecodeError):
            continue

    if payload is None:
        return CrawlConfig()

    defaults = CrawlConfig()
    return CrawlConfig(
        headless=bool(payload.get("headless", defaults.headless)),
        download_workers=normalize_download_workers(
            payload.get("download_workers", defaults.download_workers),
            defaults.download_workers,
        ),
        max_media_file_size_mib=_clamp_int_setting(
            payload.get("max_media_file_size_mib", defaults.max_media_file_size_mib),
            defaults.max_media_file_size_mib,
            MIN_MAX_MEDIA_FILE_SIZE_MIB,
            MAX_MAX_MEDIA_FILE_SIZE_MIB,
        ),
        max_media_items=int(payload.get("max_media_items", defaults.max_media_items)),
        max_scroll_rounds=int(payload.get("max_scroll_rounds", defaults.max_scroll_rounds)),
        scroll_pause_seconds=float(payload.get("scroll_pause_seconds", defaults.scroll_pause_seconds)),
        stale_round_limit=int(payload.get("stale_round_limit", defaults.stale_round_limit)),
        x_browser=normalize_host_browser(payload.get("x_browser", defaults.x_browser), defaults.x_browser),
        grok_browser=normalize_host_browser(payload.get("grok_browser", defaults.grok_browser), defaults.grok_browser),
        chatgpt_browser=normalize_host_browser(
            payload.get("chatgpt_browser", defaults.chatgpt_browser),
            defaults.chatgpt_browser,
        ),
        gemini_browser=normalize_host_browser(
            payload.get("gemini_browser", defaults.gemini_browser),
            defaults.gemini_browser,
        ),
        claude_browser=normalize_host_browser(
            payload.get("claude_browser", defaults.claude_browser),
            defaults.claude_browser,
        ),
        zhihu_browser=normalize_host_browser(
            payload.get("zhihu_browser", defaults.zhihu_browser),
            defaults.zhihu_browser,
        ),
        gemini_max_conversations=max(
            1,
            int(payload.get("gemini_max_conversations", defaults.gemini_max_conversations)),
        ),
        gemini_scroll_pause_seconds=max(
            0.1,
            float(payload.get("gemini_scroll_pause_seconds", defaults.gemini_scroll_pause_seconds)),
        ),
        gemini_stale_round_limit=max(
            1,
            int(payload.get("gemini_stale_round_limit", defaults.gemini_stale_round_limit)),
        ),
        chatgpt_project_url=str(
            payload.get("chatgpt_project_url", defaults.chatgpt_project_url)
        ).strip(),
        chatgpt_project_name=str(payload.get("chatgpt_project_name", defaults.chatgpt_project_name)).strip()
        or defaults.chatgpt_project_name,
        chatgpt_startup_timeout_seconds=_clamp_float_setting(
            payload.get("chatgpt_startup_timeout_seconds", defaults.chatgpt_startup_timeout_seconds),
            defaults.chatgpt_startup_timeout_seconds,
            MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
            MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
        ),
        chatgpt_scan_wait_seconds=_clamp_float_setting(
            payload.get("chatgpt_scan_wait_seconds", defaults.chatgpt_scan_wait_seconds),
            defaults.chatgpt_scan_wait_seconds,
            MIN_CHATGPT_SCAN_WAIT_SECONDS,
            MAX_CHATGPT_SCAN_WAIT_SECONDS,
        ),
        cache_scan_waits={
            key: _clamp_float_setting(value, 0.0, 0.0, 60.0)
            for key, value in (payload.get("cache_scan_waits", {}) or {}).items()
            if key in {"chatgpt_text", "claude_text", "grok_text", "grok_media"}
        } if isinstance(payload.get("cache_scan_waits", {}), dict) else {},
        chatgpt_text_startup_timeout_seconds=_clamp_float_setting(
            payload.get("chatgpt_text_startup_timeout_seconds",
                        payload.get("chatgpt_startup_timeout_seconds", defaults.chatgpt_startup_timeout_seconds)),
            defaults.chatgpt_startup_timeout_seconds, MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
            MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
        ),
        chrome_user_data_dir=Path(payload.get("chrome_user_data_dir", str(defaults.chrome_user_data_dir))).expanduser(),
        chrome_profile_directory=str(
            payload.get("chrome_profile_directory", defaults.chrome_profile_directory)
        ).strip()
        or defaults.chrome_profile_directory,
        account_name_override=str(payload.get("account_name_override", defaults.account_name_override)).strip(),
        shadow_backup_enabled=bool(payload.get("shadow_backup_enabled", defaults.shadow_backup_enabled)),
        shadow_backup_auto_sync=bool(payload.get("shadow_backup_auto_sync", defaults.shadow_backup_auto_sync)),
        shadow_backup_mirror_deletions=bool(
            payload.get("shadow_backup_mirror_deletions", defaults.shadow_backup_mirror_deletions)
        ),
        shadow_backup_destination=Path(
            str(payload.get("shadow_backup_destination", defaults.shadow_backup_destination)).strip()
            or str(defaults.shadow_backup_destination)
        ).expanduser(),
    )


def _clamp_float_setting(value: object, fallback: float, minimum: float, maximum: float) -> float:
    """Parse one persisted float setting and keep it within its supported range."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    if not math.isfinite(parsed):
        parsed = fallback
    return min(max(parsed, minimum), maximum)


def _clamp_int_setting(value: object, fallback: int, minimum: int, maximum: int) -> int:
    """Parse one persisted integer setting and keep it within its supported range."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return min(max(parsed, minimum), maximum)


def save_config(config: CrawlConfig, settings_path: Path | None = None) -> None:
    """Persist crawler settings for future app restarts."""
    resolved_settings_path = settings_path if settings_path is not None else default_settings_path()
    payload = asdict(config)
    payload["download_workers"] = normalize_download_workers(payload.get("download_workers"))
    payload["x_browser"] = config.x_browser
    payload["grok_browser"] = config.grok_browser
    payload["chatgpt_browser"] = config.chatgpt_browser
    payload["gemini_browser"] = config.gemini_browser
    payload["claude_browser"] = config.claude_browser
    payload["zhihu_browser"] = config.zhihu_browser
    payload["chatgpt_project_url"] = config.chatgpt_project_url
    payload["chatgpt_project_name"] = config.chatgpt_project_name
    payload["chrome_user_data_dir"] = str(config.chrome_user_data_dir)
    payload["shadow_backup_destination"] = str(config.shadow_backup_destination)
    resolved_settings_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_settings_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
