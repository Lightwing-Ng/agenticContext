"""Durable local-resource boundary for catalogs, history, and backups."""

# Code version: v1.8.1-codex.0

from ..chat_history_browser import (
    ChatHistoryPage,
    attach_media_references,
    build_chat_history_markdown,
    format_chat_message_timestamp_label,
    query_chat_history,
)
from ..wechat_archive import import_wechat_archive, wechat_archive_path
from ..wechat_resources import resolve_resource_for_reveal
from ..local_media_browser import (
    DISPLAY_TIMEZONE,
    LocalMediaCatalog,
    build_local_store_pagination,
    format_captured_at_label,
    format_captured_at_timestamp_label,
    format_datetime_label,
    file_manager_open_directory_command,
    local_file_manager_label,
    media_route_relative_path,
    normalize_browser_filters,
    open_directory_path,
    reveal_media_path,
    resolve_browser_media_path,
    resolve_local_media_path,
)
from ..prompt_store import (
    PromptPage,
    PromptStore,
    SavedPrompt,
    prompt_content_key,
    prompt_pointer_key,
)
from ..shadow_backup import (
    SettingsDirectoryBrowserError,
    SettingsDirectoryListing,
    ShadowBackupError,
    ShadowBackupService,
    browse_settings_directory,
)

_LAZY_EXPORTS = {
    "wechat_browser_read": frozenset({"WechatBrowserReadService"}),
    "wechat_contact_analysis": frozenset({
        "ContactAnalysisSourceChanged", "ContactAnalysisUnavailable", "query_contact_analysis",
        "contact_analysis_export_context", "export_contact_analysis_items",
        "message_source_location",
    }),
    "wechat_contacts": frozenset({"classify_wechat_identifier"}),
    "wechat_display_names": frozenset({"WechatDisplayNames"}),
    "wechat_exchange": frozenset({"export_conversation", "read_package"}),
    "wechat_exchange_import": frozenset({"archive_state", "import_package", "preview_package"}),
    "wechat_global_guard": frozenset({"GlobalRefreshBusy"}),
    "wechat_group_roster_source": frozenset({"capture_group_roster", "roster_source_available"}),
    "wechat_group_rosters": frozenset({"WechatGroupRosterRepository"}),
    "wechat_history_browser": frozenset({"query_wechat_history"}),
    "wechat_html_export": frozenset({"normalize_html_clock"}),
    "wechat_mac_refresh": frozenset({"WechatMacRefreshService"}),
    "wechat_mac_refresh_source": frozenset({"RefreshNotice"}),
    "wechat_session_export": frozenset({"WechatSessionExport", "WechatSessionSourceChanged"}),
    "storage.wechat_summary_read": frozenset({
        "ACCOUNT_SUMMARY_KINDS", "account_summary_pin", "read_account_summary",
    }),
    "storage.wechat_summary_worker": frozenset({"WechatSummaryWorker"}),
    "youtube_cache": frozenset({
        "YouTubeCacheError", "YouTubeCacheProgress", "YouTubeCacheResult",
        "download_youtube_media", "parse_youtube_video_id", "read_cached_youtube_media",
        "recent_cached_youtube_media", "youtube_cache_directory", "youtube_cache_path",
    }),
}
# Facade names that differ from the member they expose.
_LAZY_ALIASES = {
    "recover_wechat_exchange_import": ("wechat_exchange_import", "recover_interrupted_import"),
    "resolve_wechat_refresh_scope": ("wechat_mac_refresh_projection", "resolve_scope"),
    "WECHAT_REFRESH_BUSY_MESSAGE": ("wechat_global_guard", "BUSY_MESSAGE"),
    "WECHAT_REFRESH_MAX_COUNTER": ("wechat_mac_refresh_status", "MAX_COUNTER"),
    "safe_wechat_refresh_progress": ("wechat_mac_refresh_status", "safe_progress"),
}


def __getattr__(name: str):
    """Keep optional archive workflows behind the local-resource facade."""
    from importlib import import_module

    if name in _LAZY_ALIASES:
        module, member = _LAZY_ALIASES[name]
        return getattr(import_module(f"..{module}", __package__), member)
    for module, names in _LAZY_EXPORTS.items():
        if name in names:
            return getattr(import_module(f"..{module}", __package__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ChatHistoryPage",
    "LocalMediaCatalog",
    "PromptPage",
    "PromptStore",
    "SavedPrompt",
    "SettingsDirectoryBrowserError",
    "SettingsDirectoryListing",
    "ShadowBackupError",
    "ShadowBackupService",
    "attach_media_references",
    "browse_settings_directory",
    "build_chat_history_markdown",
    "build_local_store_pagination",
    "DISPLAY_TIMEZONE",
    "file_manager_open_directory_command",
    "format_captured_at_label",
    "format_datetime_label",
    "format_captured_at_timestamp_label",
    "format_chat_message_timestamp_label",
    "local_file_manager_label",
    "media_route_relative_path",
    "normalize_browser_filters",
    "open_directory_path",
    "prompt_content_key",
    "prompt_pointer_key",
    "query_chat_history",
    "import_wechat_archive",
    "wechat_archive_path",
    "resolve_browser_media_path",
    "resolve_local_media_path",
    "resolve_resource_for_reveal",
    "reveal_media_path",
    *(name for names in _LAZY_EXPORTS.values() for name in sorted(names)),
    *_LAZY_ALIASES,
]
