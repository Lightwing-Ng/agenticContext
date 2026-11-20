"""Rollback staging preserves both present and absent trusted input states.

Code version: v1.0.0-codex.0
"""

import json
import os
import shutil

import pytest

from app.core import wechat_exchange_import as exchange
from app.core import wechat_exchange_recovery as recovery
from app.core.wechat_archive import wechat_archive_path
import test_wechat_exchange_crash_recovery as crash
import test_wechat_exchange_import as fixtures
from test_wechat_exchange_rollback_transition import _snapshot


pytestmark = pytest.mark.slow


@pytest.mark.parametrize("missing_role", ["candidate", "canonical"])
@pytest.mark.parametrize("created_bytes", ["unknown", "published"])
def test_rollback_refuses_previously_absent_inputs_created_during_staging(
    tmp_path, monkeypatch, missing_role, created_bytes,
):
    package = fixtures._package(tmp_path)
    if missing_role == "canonical":
        root = tmp_path / "empty-target"
        history = wechat_archive_path(root)
        crash._interrupt_import(root, package, 0)
        assert not os.path.lexists(history)
    else:
        root, history, _ = crash._target(tmp_path)
        crash._interrupt_import(root, package, 1)
    directory = history.parent
    recovery.mark_rollback_pending(directory)
    journal = directory / recovery.PUBLICATION_JOURNAL_NAME
    original_journal = journal.read_bytes()
    payload = json.loads(original_journal)
    forward_candidate = directory / payload["entries"][0]["candidate"]
    if missing_role == "canonical":
        missing = history
        published_bytes = forward_candidate.read_bytes()
    else:
        missing = forward_candidate
        published_bytes = history.read_bytes()
    assert not os.path.lexists(missing)
    content = published_bytes if created_bytes == "published" else b"Independent new input"
    builder = exchange._rollback_stages
    observed = {}

    def build_then_create(*arguments):
        stages = builder(*arguments)
        missing.write_bytes(content)
        observed.update(_snapshot(directory))
        return stages

    monkeypatch.setattr(exchange, "_rollback_stages", build_then_create)
    with pytest.raises(ValueError, match="changed"):
        exchange.recover_interrupted_import(root)
    assert observed and _snapshot(directory) == observed
    assert missing.read_bytes() == content
    assert journal.read_bytes() == original_journal
    assert json.loads(journal.read_text())["phase"] == "rollback-pending"


@pytest.mark.parametrize(
    "removed_role", ["canonical", "candidate", "backup", "retained", "stage", "journal"],
)
def test_rollback_refuses_present_inputs_removed_during_staging(
    tmp_path, monkeypatch, removed_role,
):
    package = fixtures._package(tmp_path)
    root, history, sidecars = crash._target(tmp_path)
    crash._interrupt_import(root, package, 1)
    directory = history.parent
    recovery.mark_rollback_pending(directory)
    journal = directory / recovery.PUBLICATION_JOURNAL_NAME
    payload = json.loads(journal.read_text())
    retained = directory / ".wechat-exchange-rollback-retained.sqlite3"
    shutil.copyfile(history, retained)
    payload["retained"].append({"name": retained.name, "pin": recovery._pin(retained)})
    journal.write_text(json.dumps(payload))
    original_journal = journal.read_bytes()
    paths = {
        "canonical": sidecars[0],
        "candidate": directory / payload["entries"][1]["candidate"],
        "backup": directory / payload["entries"][1]["backup"],
        "retained": retained, "journal": journal,
    }
    builder = exchange._rollback_stages
    observed = {}

    def build_then_remove(*arguments):
        stages = builder(*arguments)
        if removed_role == "stage":
            paths["stage"] = stages[0][history]
        paths[removed_role].unlink()
        observed.update(_snapshot(directory))
        return stages

    monkeypatch.setattr(exchange, "_rollback_stages", build_then_remove)
    expected_error = FileNotFoundError if removed_role == "stage" else ValueError
    with pytest.raises(expected_error):
        exchange.recover_interrupted_import(root)
    assert observed and _snapshot(directory) == observed
    assert not os.path.lexists(paths[removed_role])
    if removed_role != "journal":
        assert journal.read_bytes() == original_journal
        assert json.loads(journal.read_text())["phase"] == "rollback-pending"
