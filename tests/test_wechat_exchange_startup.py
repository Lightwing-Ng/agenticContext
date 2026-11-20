"""Recover pending exchange publications before application readers are composed.

Code version: v1.0.0-codex.0
"""

from contextlib import closing
import json
import sqlite3

import pytest

from app.core import wechat_exchange_import as exchange
from app.core.wechat_archive import _file_identity
from app.core.wechat_exchange_recovery import PUBLICATION_JOURNAL_NAME
from app.web.app import create_app
import test_wechat_exchange_crash_recovery as publication


def test_application_restart_recovers_a_killed_import_before_browsing(tmp_path):
    package = publication.fixtures._package(tmp_path)
    root, history, sidecars = publication._target(tmp_path)
    publication._interrupt_import(root, package, 1)
    journal = history.parent / PUBLICATION_JOURNAL_NAME
    assert journal.is_file()

    application = create_app(root, agent_external_operations_enabled=False)
    try:
        assert not journal.exists()
        assert exchange.recover_interrupted_import(root) == "absent"
        assert len(publication.fixtures._rows(root, "messages")) == 2
        for path in sidecars:
            with closing(sqlite3.connect(path)) as connection:
                info = dict(connection.execute("SELECT * FROM archive_info"))
                assert json.loads(info["history_file_identity"]) == _file_identity(history)
        response = application.test_client().get("/browser?view=text&source=wechat")
        assert response.status_code == 200
    finally:
        application.extensions["runtime_shutdown"]()


def test_application_rejects_a_drifted_pending_import_without_changing_evidence(tmp_path):
    package = publication.fixtures._package(tmp_path)
    root, history, sidecars = publication._target(tmp_path)
    publication._interrupt_import(root, package, 1)
    sidecars[0].write_bytes(b"Independent changed evidence must be preserved.")
    evidence = {
        path: path.read_bytes()
        for path in (history, *sidecars, history.parent / PUBLICATION_JOURNAL_NAME)
    }

    with pytest.raises(ValueError, match="changed"):
        create_app(root, agent_external_operations_enabled=False)

    assert {path: path.read_bytes() for path in evidence} == evidence
