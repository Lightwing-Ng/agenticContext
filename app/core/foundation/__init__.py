"""Stable foundation boundary for runtime configuration, identity, and task state."""

# Code version: v1.5.2-codex.0

from ..brand import PRODUCT_NAME
from ..cache_task_coordinator import CacheTaskBusyError, CacheTaskCoordinator, CacheTaskIdentity
from ..config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    LOCAL_STORE_ROOT,
    MAX_CHATGPT_SCAN_WAIT_SECONDS,
    MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    MAX_DOWNLOAD_WORKERS,
    MAX_MAX_MEDIA_FILE_SIZE_MIB,
    MIN_CHATGPT_SCAN_WAIT_SECONDS,
    MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    MIN_MAX_MEDIA_FILE_SIZE_MIB,
    MIN_DOWNLOAD_WORKERS,
    CrawlConfig,
    default_settings_path,
    is_macos_host,
    is_windows_host,
    load_saved_config,
    normalize_copilot_entry_url,
    normalize_download_workers,
    save_config,
)
from ..date_display import (
    COMPACT_FORMATS,
    DEFAULT_FULL_STYLE,
    EXAMPLE_DATE,
    FULL_FORMATS,
    DateDisplay,
    format_date,
    format_datetime,
    format_time,
)
from ..logging_setup import configure_logging, get_log_file_path
from ..job_lock import CacheTaskLock, SHARED_CACHE_TASK_LOCK
from ..state import (
    TaskSnapshot,
    TaskState,
    build_initial_snapshot,
    build_x_text_snapshot,
    utc_now,
)
from ..version import APP_VERSION

_NATIVE_DIRECTORY_EXPORTS = frozenset(
    {"NativeDirectoryPickerError", "choose_native_directory"}
)


def __getattr__(name: str):
    """Load host-native picker support only when a caller needs it."""
    if name in _NATIVE_DIRECTORY_EXPORTS:
        from .. import native_directory_picker

        return getattr(native_directory_picker, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "APP_VERSION",
    "COMPACT_FORMATS",
    "DEFAULT_FULL_STYLE",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "EXAMPLE_DATE",
    "FULL_FORMATS",
    "LOCAL_STORE_ROOT",
    "MAX_CHATGPT_SCAN_WAIT_SECONDS",
    "MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS",
    "MAX_DOWNLOAD_WORKERS",
    "MAX_MAX_MEDIA_FILE_SIZE_MIB",
    "MIN_CHATGPT_SCAN_WAIT_SECONDS",
    "MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS",
    "MIN_MAX_MEDIA_FILE_SIZE_MIB",
    "MIN_DOWNLOAD_WORKERS",
    "NativeDirectoryPickerError",
    "PRODUCT_NAME",
    "CrawlConfig",
    "DateDisplay",
    "CacheTaskBusyError",
    "CacheTaskCoordinator",
    "CacheTaskIdentity",
    "CacheTaskLock",
    "SHARED_CACHE_TASK_LOCK",
    "TaskSnapshot",
    "TaskState",
    "build_initial_snapshot",
    "build_x_text_snapshot",
    "choose_native_directory",
    "configure_logging",
    "default_settings_path",
    "format_date",
    "format_datetime",
    "format_time",
    "get_log_file_path",
    "is_macos_host",
    "is_windows_host",
    "load_saved_config",
    "normalize_copilot_entry_url",
    "normalize_download_workers",
    "save_config",
    "utc_now",
]
