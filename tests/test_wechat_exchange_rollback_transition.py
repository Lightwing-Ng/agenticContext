"""Reject source drift while building an interrupted exchange rollback.

Code version: v1.0.0-codex.0
"""

import json
import shutil

import pytest

from app.core import wechat_exchange_import as exchange
from app.core import wechat_exchange_recovery as recovery
import test_wechat_exchange_crash_recovery as crash
import test_wechat_exchange_import as fixtures


pytestmark = pytest.mark.slow


def _snapshot(directory):
    return {
        path: path.read_bytes()
        for path in directory.iterdir()
        if path.is_file()
    }


@pytest.mark.parametrize(
    "changed", [
        "history", "sidecar", "candidate", "backup", "retained", "untouched",
        "rollback_stage", "journal", "known_forward",
    ],
)
def test_rollback_transition_preserves_drift_and_all_recovery_evidence(
    tmp_path, monkeypatch, changed,
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
        "history": history,
        "sidecar": sidecars[0],
        "candidate": directory / payload["entries"][1]["candidate"],
        "backup": directory / payload["entries"][1]["backup"],
        "retained": retained,
        "untouched": directory / "source_exclusions.json",
        "journal": journal,
    }
    builder = exchange._rollback_stages
    observed = {}

    def build_then_change(*arguments):
        replacements = builder(*arguments)
        if changed == "known_forward":
            paths["candidate"].replace(paths["sidecar"])
        else:
            if changed == "rollback_stage":
                paths[changed] = replacements[0][history]
            with paths[changed].open("ab") as stream:
                stream.write(
                    b"\n" if changed == "journal"
                    else b"Unknown write after rollback preflight"
                )
        observed.update(_snapshot(directory))
        return replacements

    monkeypatch.setattr(exchange, "_rollback_stages", build_then_change)
    with pytest.raises(ValueError, match="changed"):
        exchange.recover_interrupted_import(root)
    assert observed
    assert _snapshot(directory) == observed
    assert journal.read_bytes() == (
        observed[journal] if changed == "journal" else original_journal
    )
    assert json.loads(journal.read_text())["phase"] == "rollback-pending"
    if changed not in {"journal", "known_forward"}:
        assert paths[changed].read_bytes().endswith(
            b"Unknown write after rollback preflight"
        )
