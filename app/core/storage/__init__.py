"""Durable local-resource boundary for catalogs, history, and backups."""

# Code version: v1.6.1-codex.0

from ..chat_history_browser import (
    attach_media_references,
    build_chat_history_markdown,
    format_chat_message_timestamp_label,
    query_chat_history,
)
from ..wechat_archive import import_wechat_archive, wechat_archive_path
from ..wechat_contact_analysis import (
    ContactAnalysisSourceChanged,
)
from ..wechat_display_names import WechatDisplayNames
from ..wechat_resources import resolve_resource_for_reveal
from ..wechat_session_export import WechatSessionExport, WechatSessionSourceChanged
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


def query_contact_analysis(local_store_root, contact_id, **options):
    """Read contact statements through the implementation's current request handler."""
    from .. import wechat_contact_analysis

    return wechat_contact_analysis.query_contact_analysis(
        local_store_root, contact_id, **options
    )


def contact_analysis_export_context(local_store_root, contact_id):
    """Resolve the current verified contact export context through the storage boundary."""
    from .. import wechat_contact_analysis

    return wechat_contact_analysis.contact_analysis_export_context(
        local_store_root, contact_id
    )


def export_contact_analysis_items(local_store_root, contact_id, **options):
    """Return the implementation's live export iterator, including its close contract."""
    from .. import wechat_contact_analysis

    return wechat_contact_analysis.export_contact_analysis_items(
        local_store_root, contact_id, **options
    )


def message_source_location(local_store_root, message_id, **options):
    """Resolve a source occurrence without freezing a private implementation alias."""
    from .. import wechat_contact_analysis

    return wechat_contact_analysis.message_source_location(
        local_store_root, message_id, **options
    )


__all__ = [
    "ContactAnalysisSourceChanged",
    "LocalMediaCatalog",
    "PromptPage",
    "PromptStore",
    "SavedPrompt",
    "SettingsDirectoryBrowserError",
    "SettingsDirectoryListing",
    "ShadowBackupError",
    "ShadowBackupService",
    "WechatDisplayNames",
    "WechatSessionExport",
    "WechatSessionSourceChanged",
    "attach_media_references",
    "browse_settings_directory",
    "build_chat_history_markdown",
    "build_local_store_pagination",
    "contact_analysis_export_context",
    "DISPLAY_TIMEZONE",
    "file_manager_open_directory_command",
    "export_contact_analysis_items",
    "format_captured_at_label",
    "format_datetime_label",
    "format_captured_at_timestamp_label",
    "format_chat_message_timestamp_label",
    "local_file_manager_label",
    "media_route_relative_path",
    "message_source_location",
    "normalize_browser_filters",
    "open_directory_path",
    "prompt_content_key",
    "prompt_pointer_key",
    "query_chat_history",
    "query_contact_analysis",
    "import_wechat_archive",
    "wechat_archive_path",
    "resolve_browser_media_path",
    "resolve_local_media_path",
    "resolve_resource_for_reveal",
    "reveal_media_path",
]
