"""The Agent platform catalog: which providers, models, browsers, and hosts exist.

These tables are data, not behavior. Several entry points read them - the Agent run
loop, the settings validator, the context-package builder, and the Web layer through
the domain facade - so they live in one module instead of being reachable only by
importing the run loop.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

import os
import sys
from typing import Any

from ..agent_model_catalog import LATEST_CHATGPT_MODEL
from ..agent_session_sources import (
    CLAUDE_HOME_URL,
    CLAUDE_HOSTS,
    normalize_agent_conversation_url,
    normalize_agent_project_url,
)
from ..config import is_windows_host

CHATGPT_HOME_URL = "https://chatgpt.com/"

CHATGPT_HOSTS = {"chatgpt.com", "www.chatgpt.com"}

GEMINI_HOME_URL = "https://gemini.google.com/app"

GEMINI_HOSTS = {"gemini.google.com"}

GROK_HOME_URL = "https://grok.com/"

GROK_HOSTS = {"grok.com", "www.grok.com"}

SUPPORTED_BROWSERS = frozenset({"chrome", "edge", "safari"})

SUPPORTED_SAFARI_AGENT_EXECUTION_PLATFORMS = frozenset({"chatgpt", "grok"})

SUPPORTED_SAFARI_WEB_SESSION_PLATFORMS = frozenset(
    {"chatgpt", "grok", "gemini", "claude"}
)

# Compatibility alias for runtime helpers that predate the split between
# read-only source discovery and full Agent execution.
SUPPORTED_SAFARI_AGENT_PLATFORMS = SUPPORTED_SAFARI_AGENT_EXECUTION_PLATFORMS

SUPPORTED_OPERATING_SYSTEMS = frozenset({"macos", "windows"})

SUPPORTED_AGENT_SESSION_MODES = frozenset({"new", "recent", "project_new", "project_session"})

SUPPORTED_AGENT_PLATFORMS = frozenset({"chatgpt", "gemini", "grok", "claude"})

DEFAULT_AGENT_PLATFORM = "chatgpt"

CHATGPT_MODEL_OPTIONS = (
    {
        "key": LATEST_CHATGPT_MODEL,
        "label": "Best available",
        "ui_label": "Best available",
        "remote_labels": (),
        "strength": 200,
    },
    {
        "key": "gpt-5.6-sol",
        "label": "GPT-5.6 Sol",
        "ui_label": "GPT-5.6 Sol",
        "remote_label": "GPT-5.6 Sol",
        "remote_model_labels": ("GPT-5.6 Sol", "5.6 Sol"),
        "remote_labels": ("GPT-5.6 Sol", "5.6 Sol"),
        "strength": 100,
    },
)

GEMINI_MODEL_OPTIONS = (
    {
        "key": "gemini-3.1-pro",
        "label": "Gemini 3.1 Pro",
        "ui_label": "3.1 Pro",
        "remote_labels": ("Gemini 3.1 Pro", "3.1 Pro"),
        "strength": 100,
    },
    {
        "key": "gemini-3.8-flash",
        "label": "Gemini 3.8 Flash",
        "ui_label": "3.8 Flash",
        "remote_labels": ("Gemini 3.8 Flash", "3.8 Flash"),
        "strength": 90,
    },
)

GROK_MODEL_OPTIONS = (
    {
        "key": "grok-build",
        "label": "Build",
        "ui_label": "Build",
        "remote_labels": ("Build",),
        "remote_trigger_labels": ("Build Beta",),
        "strength": 100,
    },
    {
        "key": "grok-auto",
        "label": "Auto",
        "ui_label": "Auto",
        "remote_labels": ("Auto",),
        "strength": 90,
    },
)

CLAUDE_MODEL_OPTIONS = (
    {
        "key": "claude-auto",
        "label": "Auto",
        "ui_label": "Auto",
        "remote_labels": ("Auto",),
        "strength": 100,
    },
)

AGENT_MODEL_OPTIONS_BY_PLATFORM = {
    "chatgpt": CHATGPT_MODEL_OPTIONS,
    "gemini": GEMINI_MODEL_OPTIONS,
    "grok": GROK_MODEL_OPTIONS,
    "claude": CLAUDE_MODEL_OPTIONS,
}

LEGACY_AGENT_MODEL_KEYS = {
    ("grok", "grok-heavy"): "grok-build",
}

def strongest_model_option(options: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    """Return the strongest model from one provider's current option catalog."""
    return max(
        options,
        key=lambda option: (
            int(option.get("strength", 0)),
            str(option.get("key", "")),
        ),
    )

def default_model_for_platform(platform: str) -> str:
    """Return the strongest supported model for one Web Agent platform."""
    options = tuple(AGENT_MODEL_OPTIONS_BY_PLATFORM.get(platform, ()))
    if not options:
        raise ValueError(f"No Web Agent models are configured for {platform}.")
    return str(strongest_model_option(options)["key"])

DEFAULT_CHATGPT_MODEL = default_model_for_platform("chatgpt")

SUPPORTED_CHATGPT_MODELS = frozenset(option["key"] for option in CHATGPT_MODEL_OPTIONS)

AGENT_PLATFORM_OPTIONS = (
    {
        "key": "chatgpt",
        "label": "ChatGPT",
        "icon_filename": "images/ChatGPT-Logo.svg",
        "home_url": CHATGPT_HOME_URL,
        "hosts": CHATGPT_HOSTS,
    },
    {
        "key": "gemini",
        "label": "Gemini",
        "icon_filename": "images/Google_Gemini_logo_2025_symbol.svg",
        "home_url": GEMINI_HOME_URL,
        "hosts": GEMINI_HOSTS,
    },
    {
        "key": "grok",
        "label": "Grok",
        "icon_filename": "images/grok.svg",
        "home_url": GROK_HOME_URL,
        "hosts": GROK_HOSTS,
    },
    {
        "key": "claude",
        "label": "Claude",
        "icon_filename": "images/claude.svg",
        "home_url": CLAUDE_HOME_URL,
        "hosts": CLAUDE_HOSTS,
    },
)

AGENT_PLATFORM_BY_KEY = {option["key"]: option for option in AGENT_PLATFORM_OPTIONS}

AGENT_MODEL_OPTIONS = CHATGPT_MODEL_OPTIONS

OPERATING_SYSTEM_OPTIONS = (
    {
        "key": "macos",
        "label": "macOS",
        "icon_filename": "images/finder.svg",
        "available": True,
    },
    {
        "key": "windows",
        "label": "Windows",
        "icon_filename": "images/MSFT.svg",
        "available": True,
    },
)

BROWSER_OPTIONS = (
    {"key": "safari", "label": "Safari", "icon_filename": "images/browser.safari.png"},
    {"key": "edge", "label": "Edge", "icon_filename": "images/browser.edge.png"},
    {"key": "chrome", "label": "Chrome", "icon_filename": "images/browser.chrome.png"},
)

def detect_host_operating_system() -> str:
    """Return the Agent operating-system key detected from this host."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win") or os.name == "nt":
        return "windows"
    return "macos"

DEFAULT_OPERATING_SYSTEM = "windows" if is_windows_host() else "macos"

def _platform_model_options(platform: str) -> tuple[dict[str, Any], ...]:
    """Return the supported remote model choices for one web platform."""
    return tuple(AGENT_MODEL_OPTIONS_BY_PLATFORM.get(platform, ()))

def _platform_home_url(platform: str) -> str:
    """Return the official home URL for one supported web platform."""
    option = AGENT_PLATFORM_BY_KEY.get(platform)
    if option is None:
        raise ValueError("Choose ChatGPT, Gemini, Grok, or Claude for the Web Agent.")
    return str(option["home_url"])

def _platform_hosts(platform: str) -> set[str]:
    """Return the official HTTPS hosts accepted for one web platform."""
    option = AGENT_PLATFORM_BY_KEY.get(platform)
    if option is None:
        raise ValueError("Choose ChatGPT, Gemini, Grok, or Claude for the Web Agent.")
    return set(option["hosts"])

def _normalize_web_agent_target(platform: str, target_url: str = "") -> str:
    """Normalize one safe target without allowing cross-site browser navigation."""
    normalized_target_url = (
        normalize_agent_conversation_url(platform, target_url)
        or normalize_agent_project_url(platform, target_url)
    )
    if normalized_target_url:
        return normalized_target_url
    return _platform_home_url(platform)
