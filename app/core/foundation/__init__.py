"""Stable foundation boundary for runtime configuration, identity, and task state."""

# Code version: v1.2.1-codex.1

from ..brand import PRODUCT_NAME
from ..config import (
    BETA_STORE_ROOT,
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
from ..state import TaskState, build_initial_snapshot, utc_now
from ..version import APP_VERSION

__all__ = [
    "APP_VERSION",
    "BETA_STORE_ROOT",
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
    "PRODUCT_NAME",
    "CrawlConfig",
    "CacheTaskLock",
    "SHARED_CACHE_TASK_LOCK",
    "TaskState",
    "build_initial_snapshot",
    "configure_logging",
    "get_log_file_path",
    "is_macos_host",
    "is_windows_host",
    "load_saved_config",
    "normalize_download_workers",
    "save_config",
    "utc_now",
]
