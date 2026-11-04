"""Build lightweight Local resources navigation around nested detail pages.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from contextlib import closing
import sqlite3
import stat
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from app.core.local_media_browser import normalize_browser_filters
from app.core.wechat_archive import wechat_archive_path
from app.core.wechat_display_names import WechatDisplayNames
from app.web.cache_sources import CACHE_SOURCE_VIEWS, LOCAL_RESOURCE_TEXT_SOURCE_VIEWS


def local_resource_source_context():
    """Use one source registry selection for the index and nested detail pages."""
    return {
        "local_resource_media_sources": tuple(
            source
            for source in CACHE_SOURCE_VIEWS
            if source.key in {"x", "grok", "chatgpt", "claude", "gemini"}
        ),
        "local_resource_text_sources": tuple(
            source
            for source in LOCAL_RESOURCE_TEXT_SOURCE_VIEWS
            if source.key
            in {
                "x",
                "grok",
                "grok_build",
                "chatgpt",
                "copilot",
                "codex",
                "claude",
                "claude_code",
                "gemini",
                "zhihu",
                "wechat",
            }
            or source.key.endswith("_openclaw")
        ),
    }


def _navigation_identity(path):
    state = path.stat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(state.st_mode)
        or getattr(state, "st_flags", 0) & 0x40000000
        or any(
            path.with_name(path.name + suffix).exists()
            for suffix in ("-wal", "-shm", "-journal")
        )
    ):
        raise ValueError("Navigation metadata is not a stable local input.")
    return state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns


def _wechat_account_navigation(local_store_root):
    """Read partition labels only; never query messages or optional media indices."""
    path = wechat_archive_path(local_store_root)
    try:
        initial = _navigation_identity(path)
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            accounts = [
                dict(row)
                for row in connection.execute(
                    "SELECT account_key AS key,account_label AS name,identity_status,"
                    "account_id,id AS partition_id FROM partitions "
                    "ORDER BY account_label COLLATE NOCASE,id"
                )
            ]
        if _navigation_identity(path) != initial:
            return (), ()
        return WechatDisplayNames(local_store_root).project_accounts(accounts)
    except (OSError, ValueError, sqlite3.DatabaseError):
        return (), ()


def wechat_contact_sidebar_context(local_store_root, return_url):
    """Retain the sanitized browser origin independently of contact-result filters."""
    values = parse_qs(urlsplit(return_url).query, keep_blank_values=True)

    def first(name):
        return values.get(name, [None])[0]

    filters = normalize_browser_filters(
        view="text",
        source="wechat",
        media_kind=first("kind"),
        query=first("q"),
        sort=first("sort"),
        page=first("page"),
        session=first("session"),
        session_view=first("session_view"),
        session_page=first("session_page"),
        account=first("account"),
        chat_kind=first("chat_kind"),
    )
    accounts, options = _wechat_account_navigation(local_store_root)
    return {
        **local_resource_source_context(),
        "filters": filters,
        "text_page": SimpleNamespace(
            messaging={"accounts": accounts, "account_options": options},
            project_options=(),
            answerer_options=(),
            session_detail=False,
        ),
    }
