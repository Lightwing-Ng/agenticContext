"""Explicit profile metadata for mocked provider responses. Code version: v1.0.0-codex.1."""

from app.core.agent import browser_profile_cache_identity
from app.core.config import CrawlConfig


def browser_profile_identity(browser: str = "edge") -> str:
    """Match the isolated test server's default saved browser configuration."""
    return browser_profile_cache_identity(browser, CrawlConfig())
