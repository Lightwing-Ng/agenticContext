"""Canonical local-console navigation helpers.

Code version: v1.1.1-codex.1
"""

from __future__ import annotations

from app.core.agent import SUPPORTED_AGENT_PLATFORMS, SUPPORTED_BROWSERS


def is_supported_agent_selection(browser: str | None, platform: str | None) -> bool:
    """Return whether one browser/provider pair has a supported Agent route."""
    selected_browser = str(browser or "").strip().lower()
    selected_platform = str(platform or "").strip().lower()
    return (
        selected_browser in SUPPORTED_BROWSERS
        and selected_platform in SUPPORTED_AGENT_PLATFORMS
        and not (selected_browser == "safari" and selected_platform != "chatgpt")
    )


def build_agent_path(browser: str, platform: str) -> str:
    """Return the canonical path for one Agent browser/provider selection."""
    selected_browser = str(browser or "").strip().lower()
    selected_platform = str(platform or "").strip().lower()
    if not is_supported_agent_selection(selected_browser, selected_platform):
        raise ValueError("Unsupported Agent browser/provider selection.")
    return f"/agent/{selected_browser}/{selected_platform}"
