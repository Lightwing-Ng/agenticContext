"""Contact integrity is checked once per unchanged published registry.

Code version: v1.0.0-codex.0
"""

from contextlib import closing
import os
import sqlite3

import pytest

from app.core.wechat_browser_read import WechatBrowserReadService
from app.core.wechat_contacts import initialize_contact_registry
from tests import test_wechat_session_csv
from tests.test_wechat_resource_timeline import _session


session_app = test_wechat_session_csv.session_app


def _write_registry(path, *, schema="2"):
    with closing(sqlite3.connect(path)) as connection, connection:
        initialize_contact_registry(connection)
        connection.execute(
            "UPDATE contact_registry_info SET value=? WHERE key='schema_version'", (schema,)
        )


def _track_checks(monkeypatch, registry, after_check=lambda: None):
    checks = []
    connect = sqlite3.connect

    class TracedConnection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            cursor = super().execute(statement, parameters)
            if statement == "PRAGMA quick_check":
                checks.append(statement)
                after_check()
            return cursor

    def traced(database, *args, **kwargs):
        if database == registry.absolute().as_uri() + "?mode=ro&immutable=1":
            kwargs["factory"] = TracedConnection
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", traced)
    return checks


def test_contact_integrity_reused_across_pages_and_rechecked_after_replacement(
    session_app, monkeypatch,
):
    app, root = session_app
    service = app.extensions["wechat_browser_read"]
    session = _session(root, kind="group")["id"]
    contacts = root / "messaging/wechat/contacts.sqlite3"
    _write_registry(contacts)
    checks = _track_checks(monkeypatch, contacts)
    try:
        service._validate({"session": session, "page": 1})
        service._validate({"session": session, "page": 2})
        assert len(checks) == 1

        replacement = contacts.with_suffix(".replacement")
        _write_registry(replacement, schema="unsupported")
        replacement.replace(contacts)
        with pytest.raises(ValueError, match="unsupported schema"):
            service._validate({"session": session, "page": 1})
        assert len(checks) == 2

        _write_registry(replacement)
        replacement.replace(contacts)
        service._validate({"session": session, "page": 1})
        assert len(checks) == 3
    finally:
        app.extensions["runtime_shutdown"]()


def test_cached_contact_integrity_cannot_accept_pending_writes(tmp_path, monkeypatch):
    contacts = tmp_path / "contacts.sqlite3"
    _write_registry(contacts)
    checks = _track_checks(monkeypatch, contacts)
    service = WechatBrowserReadService(tmp_path)
    service._validate_contacts(contacts)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = contacts.with_name(contacts.name + suffix)
        sidecar.touch()
        try:
            with pytest.raises(ValueError, match="published local file"):
                service._validate_contacts(contacts)
        finally:
            sidecar.unlink()
    assert len(checks) == 1


def test_contact_drift_during_validation_does_not_publish_proof(tmp_path, monkeypatch):
    contacts = tmp_path / "contacts.sqlite3"
    _write_registry(contacts)

    def change_mtime_once():
        if len(checks) == 1:
            current = contacts.stat()
            os.utime(contacts, ns=(current.st_atime_ns, current.st_mtime_ns - 1_000_000_000))

    checks = _track_checks(monkeypatch, contacts, change_mtime_once)
    service = WechatBrowserReadService(tmp_path)
    with pytest.raises(ValueError, match="changed during validation"):
        service._validate_contacts(contacts)
    service._validate_contacts(contacts)
    service._validate_contacts(contacts)
    assert len(checks) == 2
