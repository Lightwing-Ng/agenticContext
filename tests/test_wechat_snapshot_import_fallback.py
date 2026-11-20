"""An unavailable optional overview cache retains complete source validation.

Code version: v1.0.0-codex.0
"""

import builtins
from types import SimpleNamespace

import pytest

from tests import test_wechat_conversation_events as fixtures


event_store = fixtures.event_store
_MODULE = "app.core.wechat_browser_snapshot"


@pytest.mark.parametrize("error_type", (ModuleNotFoundError, ImportError))
def test_unavailable_snapshot_module_or_export_uses_full_query(
    event_store, monkeypatch, error_type
):
    repository = event_store.repository
    original_import = builtins.__import__
    original_timeline = repository._timeline_tables
    validated = []

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "wechat_browser_snapshot" and level == 1:
            raise error_type("The optional cache is unavailable.", name=_MODULE)
        return original_import(name, globals, locals, fromlist, level)

    def timeline(*args, **kwargs):
        validated.append(True)
        return original_timeline(*args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    monkeypatch.setattr(repository, "_timeline_tables", timeline)
    result = repository._query_uncached()

    assert validated == [True]
    assert result["message_count"] == 6
    assert result["conversation_count"] == 1
    assert result["resource_index_status"] == "absent"


def test_unrelated_import_failure_is_not_hidden(event_store, monkeypatch):
    original_import = builtins.__import__

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "wechat_browser_snapshot" and level == 1:
            raise ModuleNotFoundError("A different dependency is unavailable.", name="different_dependency")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(ModuleNotFoundError, match="different dependency"):
        event_store.repository._query_uncached()


def test_snapshot_validation_errors_remain_visible(event_store, monkeypatch):
    original_import = builtins.__import__

    def invalid_snapshot(*args, **kwargs):
        raise ValueError("The validated source changed.")

    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "wechat_browser_snapshot" and level == 1:
            return SimpleNamespace(load_conversation_snapshot=invalid_snapshot)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(ValueError, match="validated source changed"):
        event_store.repository._query_uncached()
