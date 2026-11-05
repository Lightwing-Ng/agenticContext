"""Durable local-resource boundary for catalogs, history, and backups."""

# Code version: v1.6.0-codex.0

from ..chat_history_browser import (
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
    "wechat_contact_analysis": frozenset({
        "ContactAnalysisSourceChanged", "query_contact_analysis",
        "contact_analysis_export_context", "export_contact_analysis_items",
        "message_source_location",
    }),
    "wechat_display_names": frozenset({"WechatDisplayNames"}),
    "wechat_html_export": frozenset({"normalize_html_clock"}),
    "wechat_session_export": frozenset({"WechatSessionExport", "WechatSessionSourceChanged"}),
}


def __getattr__(name: str):
    """Keep optional history projections behind the local-resource facade."""
    from importlib import import_module

    for module, names in _LAZY_EXPORTS.items():
        if name in names:
            return getattr(import_module(f"..{module}", __package__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
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
]
