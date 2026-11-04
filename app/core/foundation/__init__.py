"""Stable foundation boundary for runtime configuration, identity, and task state."""

# Code version: v1.4.1-codex.0

from ..brand import PRODUCT_NAME
from ..cache_task_coordinator import CacheTaskCoordinator, CacheTaskIdentity
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
    is_macos_host,
    is_windows_host,
    load_saved_config,
    normalize_download_workers,
    save_config,
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
    "DEFAULT_HOST",
    "DEFAULT_PORT",
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
    "get_log_file_path",
    "is_macos_host",
    "is_windows_host",
    "load_saved_config",
    "normalize_download_workers",
    "save_config",
    "utc_now",
]
