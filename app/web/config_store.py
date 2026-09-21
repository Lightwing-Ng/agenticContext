"""The one mutable crawl configuration shared by the Web route modules.

``create_app`` used to keep this as a ``nonlocal`` binding, which meant every route
that reads or replaces it had to live inside the factory. This holder makes the same
single value explicit and passable, so route modules can share it without a second
copy and without a service registry.
"""

# Code version: v1.0.0-claude.0

from __future__ import annotations

from dataclasses import dataclass

from app.core.foundation import CrawlConfig, load_saved_config, save_config


@dataclass(slots=True)
class SavedConfigStore:
    """Hold and persist the current crawl configuration for one application instance."""

    config: CrawlConfig

    @classmethod
    def load(cls) -> SavedConfigStore:
        """Open the store from the persisted configuration for a new application."""
        return cls(load_saved_config())

    def replace(self, config: CrawlConfig) -> CrawlConfig:
        """Adopt one validated configuration and persist it."""
        self.config = config
        save_config(self.config)
        return self.config


__all__ = ["SavedConfigStore"]
