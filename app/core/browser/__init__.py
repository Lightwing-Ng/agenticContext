"""Browser and transport boundary for authenticated source sessions."""

# Code version: v1.1.0-codex.1

from .x_session import X_READY_SELECTORS, detect_account_handle, extract_account_handle_from_urlish

_SESSION_EXPORTS = frozenset(
    {
        "BrowserDescriptor",
        "browser_descriptors",
        "build_browser_options",
        "open_zhihu_browser_for_login",
        "probe_browser_session",
    }
)


def __getattr__(name: str):
    """Load browser-session implementations only after the package is initialized."""
    if name in _SESSION_EXPORTS:
        from .. import browser_sessions

        return getattr(browser_sessions, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "BrowserDescriptor",
    "X_READY_SELECTORS",
    "browser_descriptors",
    "build_browser_options",
    "detect_account_handle",
    "extract_account_handle_from_urlish",
    "open_zhihu_browser_for_login",
    "probe_browser_session",
]
