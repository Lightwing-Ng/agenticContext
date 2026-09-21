"""Crawl-configuration form parsing for the Cache and Settings routes.

Both surfaces submit overlapping subsets of the same configuration form, so the
bounded field parsers and the merge rule that preserves unsubmitted values live in one
place. Nothing here reads a service or an application instance; it reads the request
form and returns a validated configuration.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

from pathlib import Path

from flask import request

from app.core.foundation import (
    MAX_CHATGPT_SCAN_WAIT_SECONDS,
    MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    MAX_DOWNLOAD_WORKERS,
    MAX_MAX_MEDIA_FILE_SIZE_MIB,
    MIN_CHATGPT_SCAN_WAIT_SECONDS,
    MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
    MIN_MAX_MEDIA_FILE_SIZE_MIB,
    CrawlConfig,
)


def parse_int_field(
    field_name: str,
    fallback: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    """Parse one integer form field while tolerating display separators."""
    raw_value = (request.form.get(field_name, str(fallback)) or str(fallback)).replace(",", "").strip()
    try:
        parsed = max(minimum, int(raw_value or fallback))
    except (TypeError, ValueError):
        parsed = max(minimum, int(fallback))
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed

def parse_float_field(
    field_name: str,
    fallback: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Parse one float form field while tolerating display separators."""
    raw_value = (request.form.get(field_name, str(fallback)) or str(fallback)).replace(",", "").strip()
    parsed = float(raw_value or fallback)
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed

def parse_checkbox_field(field_name: str, fallback: bool, preserve_missing: bool) -> bool:
    """Parse one checkbox while partial cache forms preserve unrelated settings."""
    raw_value = request.form.get(field_name)
    if raw_value is None:
        return fallback if preserve_missing else False
    return raw_value == "on"

def parse_form_config(
    base: CrawlConfig | None = None,
    *,
    preserve_missing_booleans: bool = False,
) -> CrawlConfig:
    source = base or CrawlConfig()
    return CrawlConfig(
        headless=parse_checkbox_field("headless", source.headless, preserve_missing_booleans),
        download_workers=parse_int_field(
            "download_workers",
            source.download_workers,
            maximum=MAX_DOWNLOAD_WORKERS,
        ),
        max_media_file_size_mib=parse_int_field(
            "max_media_file_size_mib",
            source.max_media_file_size_mib,
            minimum=MIN_MAX_MEDIA_FILE_SIZE_MIB,
            maximum=MAX_MAX_MEDIA_FILE_SIZE_MIB,
        ),
        max_media_items=parse_int_field("max_media_items", source.max_media_items),
        max_scroll_rounds=parse_int_field("max_scroll_rounds", source.max_scroll_rounds),
        scroll_pause_seconds=parse_float_field("scroll_pause_seconds", source.scroll_pause_seconds),
        stale_round_limit=parse_int_field("stale_round_limit", source.stale_round_limit),
        x_browser=(request.form.get("x_browser", source.x_browser) or source.x_browser).strip().lower(),
        grok_browser=(request.form.get("grok_browser", source.grok_browser) or source.grok_browser).strip().lower(),
        chatgpt_browser=(request.form.get("chatgpt_browser", source.chatgpt_browser) or source.chatgpt_browser)
        .strip()
        .lower(),
        gemini_browser=(request.form.get("gemini_browser", source.gemini_browser) or source.gemini_browser)
        .strip()
        .lower(),
        claude_browser=(request.form.get("claude_browser", source.claude_browser) or source.claude_browser)
        .strip()
        .lower(),
        zhihu_browser=(request.form.get("zhihu_browser", source.zhihu_browser) or source.zhihu_browser)
        .strip()
        .lower(),
        gemini_max_conversations=parse_int_field(
            "gemini_max_conversations",
            source.gemini_max_conversations,
        ),
        gemini_scroll_pause_seconds=parse_float_field(
            "gemini_scroll_pause_seconds",
            source.gemini_scroll_pause_seconds,
            minimum=0.1,
        ),
        gemini_stale_round_limit=parse_int_field(
            "gemini_stale_round_limit",
            source.gemini_stale_round_limit,
        ),
        chatgpt_project_url=(
            request.form["chatgpt_project_url"].strip()
            if "chatgpt_project_url" in request.form
            else source.chatgpt_project_url
        ),
        chatgpt_project_name=(
            request.form.get("chatgpt_project_name", source.chatgpt_project_name) or source.chatgpt_project_name
        ).strip()
        or source.chatgpt_project_name,
        chatgpt_startup_timeout_seconds=parse_float_field(
            "chatgpt_startup_timeout_seconds",
            source.chatgpt_startup_timeout_seconds,
            minimum=MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
            maximum=MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
        ),
        chatgpt_scan_wait_seconds=parse_float_field(
            "chatgpt_scan_wait_seconds",
            source.chatgpt_scan_wait_seconds,
            minimum=MIN_CHATGPT_SCAN_WAIT_SECONDS,
            maximum=MAX_CHATGPT_SCAN_WAIT_SECONDS,
        ),
        cache_scan_waits={
            key: parse_float_field(
                f"cache_scan_wait_{key}", source.cache_scan_wait(*key.split("_")),
                minimum=0.0, maximum=60.0,
            )
            for key in ("chatgpt_text", "claude_text", "grok_text", "grok_media")
        },
        chatgpt_text_startup_timeout_seconds=parse_float_field(
            "chatgpt_text_startup_timeout_seconds", source.chatgpt_text_startup_timeout_seconds,
            minimum=MIN_CHATGPT_STARTUP_TIMEOUT_SECONDS,
            maximum=MAX_CHATGPT_STARTUP_TIMEOUT_SECONDS,
        ),
        chrome_user_data_dir=Path(
            request.form.get("chrome_user_data_dir", str(source.chrome_user_data_dir)).strip()
        ).expanduser(),
        chrome_profile_directory=request.form.get(
            "chrome_profile_directory", source.chrome_profile_directory
        ).strip()
        or source.chrome_profile_directory,
        account_name_override=request.form.get("account_name_override", source.account_name_override).strip(),
        shadow_backup_enabled=parse_checkbox_field(
            "shadow_backup_enabled",
            source.shadow_backup_enabled,
            preserve_missing_booleans,
        ),
        shadow_backup_auto_sync=parse_checkbox_field(
            "shadow_backup_auto_sync",
            source.shadow_backup_auto_sync,
            preserve_missing_booleans,
        ),
        shadow_backup_mirror_deletions=parse_checkbox_field(
            "shadow_backup_mirror_deletions",
            source.shadow_backup_mirror_deletions,
            preserve_missing_booleans,
        ),
        shadow_backup_destination=Path(
            (
                request.form.get("shadow_backup_destination", str(source.shadow_backup_destination))
                or str(source.shadow_backup_destination)
            ).strip()
        ).expanduser(),
    )


__all__ = [
    "parse_checkbox_field",
    "parse_float_field",
    "parse_form_config",
    "parse_int_field",
]
