"""Kill a real publisher across coordinated WeChat replacement boundaries.

Code version: v1.0.0-codex.0
"""

from contextlib import closing, contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import traceback

import pytest

from app.core import wechat_exchange_import as exchange
from app.core.wechat_archive import _file_identity, wechat_archive_path
import test_wechat_exchange_import as fixtures


pytestmark = pytest.mark.slow


def _publisher(root, package, boundary, notifications, release, rollback):
    try:
        directory = wechat_archive_path(root).parent
        targets = {
            directory / name
            for name in ("history.sqlite3", *exchange._SIDECARS)
        }
        original_replace = exchange.os.replace
        completed = 0
        if rollback:
            def failed_readback(*arguments):
                raise ValueError("Synthetic published readback failure")

            exchange._readback = failed_readback
            if boundary == "pending":
                original_builder = exchange._rollback_stages

                def pause_rollback_setup(*arguments):
                    notifications.send(boundary)
                    release.wait()
                    return original_builder(*arguments)

                exchange._rollback_stages = pause_rollback_setup

        def replace(source, destination):
            nonlocal completed
            selected = Path(destination) in targets and (
                not rollback or Path(source).name.startswith(".wechat-exchange-rollback-stage-")
            )
            if selected:
                if completed == boundary:
                    notifications.send(boundary)
                    release.wait()
                result = original_replace(source, destination)
                completed += 1
                if completed == boundary:
                    notifications.send(boundary)
                    release.wait()
                return result
            return original_replace(source, destination)

        # Pause after real filesystem replacements; no synthetic exception unwinds them.
        exchange.os.replace = replace
        exchange.import_package(root, package, minimum_free_bytes=0)
        notifications.send("Publication unexpectedly completed before its boundary.")
    except BaseException:
        notifications.send(traceback.format_exc())
        raise
    finally:
        notifications.close()


@contextmanager
def _paused_import(root, package, boundary, *, rollback=False):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    release = context.Event()
    publisher = context.Process(
        target=_publisher,
        args=(str(root), str(package), boundary, sender, release, rollback),
    )
    publisher.start()
    sender.close()
    try:
        assert receiver.poll(30), "The publisher did not reach its replacement boundary."
        assert receiver.recv() == boundary
        yield publisher
    finally:
        if publisher.is_alive():
            publisher.kill()
            publisher.join(10)
        receiver.close()
        publisher.close()


def _interrupt_import(root, package, boundary, *, rollback=False):
    with _paused_import(root, package, boundary, rollback=rollback) as publisher:
        publisher.kill()
        publisher.join(10)
        assert not publisher.is_alive() and publisher.exitcode != 0


def _target(tmp_path):
    root = tmp_path / "target"
    history = fixtures._seed(root, text="Retained local body")
    sidecars = [
        fixtures._sidecar(root, "resource_references.sqlite3"),
        fixtures._sidecar(root, "resource_revalidated.sqlite3", supplemental=True),
        fixtures._sidecar(root, "media_events.sqlite3"),
    ]
    return root, history, sidecars


@pytest.mark.parametrize("boundary", [0, 1, 2, 4])
def test_reentry_completes_killed_multi_file_publication(tmp_path, boundary):
    package = fixtures._package(tmp_path)
    root, history, sidecars = _target(tmp_path)
    original = fixtures._rows(root, "messages")
    _interrupt_import(root, package, boundary)

    result = exchange.import_package(root, package, minimum_free_bytes=0)
    assert result["published"] or result["replayed"]
    messages = fixtures._rows(root, "messages")
    assert len(messages) == 2 and all(row in messages for row in original)
    for path in [history, *sidecars]:
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            if path != history:
                info = dict(connection.execute("SELECT * FROM archive_info"))
                assert json.loads(info["history_file_identity"]) == _file_identity(history)
                assert connection.execute("SELECT * FROM protected_resources").fetchall() == [
                    ("original", b"untouched\x00resource evidence")
                ]
                if path.name == "resource_revalidated.sqlite3":
                    assert json.loads(info["canonical_file_identity"]) == _file_identity(
                        sidecars[0]
                    )
    before = {path: path.read_bytes() for path in [history, *sidecars]}
    assert exchange.import_package(root, package, minimum_free_bytes=0)["replayed"]
    assert {path: path.read_bytes() for path in before} == before


def test_active_publication_is_busy_and_is_not_recovered(tmp_path):
    package = fixtures._package(tmp_path)
    root, history, sidecars = _target(tmp_path)
    with _paused_import(root, package, 1):
        before = {path: path.read_bytes() for path in [history, *sidecars]}
        assert exchange.recover_interrupted_import(root) == "busy"
        assert {path: path.read_bytes() for path in before} == before
    assert exchange.recover_interrupted_import(root) == "recovered"
    assert exchange.recover_interrupted_import(root) == "absent"


def test_recovery_can_itself_be_interrupted_and_resumed(tmp_path):
    package = fixtures._package(tmp_path)
    root, history, sidecars = _target(tmp_path)
    _interrupt_import(root, package, 1)
    _interrupt_import(root, package, 1)
    assert exchange.recover_interrupted_import(root) == "recovered"
    for path in sidecars:
        assert json.loads(exchange._info(path)["history_file_identity"]) == _file_identity(history)
    assert exchange.import_package(root, package, minimum_free_bytes=0)["replayed"]


@pytest.mark.parametrize("boundary", ["pending", 0, 1, 2, 4])
def test_killed_rollback_resumes_to_original_rows_and_valid_resource_pins(tmp_path, boundary):
    package = fixtures._package(tmp_path)
    root, history, sidecars = _target(tmp_path)
    original = {
        table: fixtures._rows(root, table)
        for table in ("messages", "partitions", "conversations", "archive_info")
    }
    _interrupt_import(root, package, boundary, rollback=True)
    assert exchange.recover_interrupted_import(root) == "recovered"
    assert {table: fixtures._rows(root, table) for table in original} == original
    for path in sidecars:
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            info = dict(connection.execute("SELECT * FROM archive_info"))
            assert json.loads(info["history_file_identity"]) == _file_identity(history)
            assert connection.execute("SELECT * FROM protected_resources").fetchall() == [
                ("original", b"untouched\x00resource evidence")
            ]
            if path.name == "resource_revalidated.sqlite3":
                assert json.loads(info["canonical_file_identity"]) == _file_identity(sidecars[0])
    assert exchange.recover_interrupted_import(root) == "absent"


@pytest.mark.parametrize("boundary", [0, 1])
def test_killed_initial_publication_recovers_without_an_original_database(tmp_path, boundary):
    package = fixtures._package(tmp_path)
    root = tmp_path / "empty"
    _interrupt_import(root, package, boundary)
    assert exchange.recover_interrupted_import(root) == "recovered"
    assert len(fixtures._rows(root, "messages")) == 1
    assert exchange.import_package(root, package, minimum_free_bytes=0)["replayed"]


def test_killed_initial_rollback_restores_database_absence(tmp_path):
    package = fixtures._package(tmp_path)
    root = tmp_path / "empty"
    _interrupt_import(root, package, "pending", rollback=True)
    assert exchange.recover_interrupted_import(root) == "recovered"
    assert not wechat_archive_path(root).exists()
    assert exchange.recover_interrupted_import(root) == "absent"


def test_preparation_failure_does_not_discard_another_publications_journal(tmp_path, monkeypatch):
    package = fixtures._package(tmp_path)
    root, history, sidecars = _target(tmp_path)
    journal = history.with_name(exchange.PUBLICATION_JOURNAL_NAME)
    owned_elsewhere = b"Journal owned by a separately interrupted publisher"
    before = {path: path.read_bytes() for path in [history, *sidecars]}

    def another_publication(*arguments):
        journal.write_bytes(owned_elsewhere)
        raise ValueError("An earlier exchange publication requires recovery.")

    monkeypatch.setattr(exchange, "prepare_publication", another_publication)
    with pytest.raises(ValueError, match="earlier exchange publication"):
        exchange.import_package(root, package, minimum_free_bytes=0)
    assert journal.read_bytes() == owned_elsewhere
    assert {path: path.read_bytes() for path in before} == before


def test_journal_directory_sync_failure_preserves_resumable_stages(tmp_path, monkeypatch):
    from app.core import wechat_exchange_recovery as recovery

    package = fixtures._package(tmp_path)
    root, history, sidecars = _target(tmp_path)
    journal = history.with_name(exchange.PUBLICATION_JOURNAL_NAME)
    before = {path: path.read_bytes() for path in [history, *sidecars]}

    def failed_sync(*arguments):
        raise OSError("Synthetic journal directory sync failure")

    with monkeypatch.context() as context:
        context.setattr(recovery, "_sync_directory", failed_sync)
        with pytest.raises(OSError, match="directory sync failure"):
            exchange.import_package(root, package, minimum_free_bytes=0)
    assert journal.is_file()
    assert {path: path.read_bytes() for path in before} == before
    assert exchange.recover_interrupted_import(root) == "recovered"
    assert len(fixtures._rows(root, "messages")) == 2


@pytest.mark.parametrize("changed", ["target", "candidate", "backup", "untouched"])
def test_recovery_refuses_unknown_drift_before_any_replacement(tmp_path, changed):
    package = fixtures._package(tmp_path)
    root, history, sidecars = _target(tmp_path)
    _interrupt_import(root, package, 1)
    journal = history.with_name(exchange.PUBLICATION_JOURNAL_NAME)
    payload = json.loads(journal.read_text())
    if changed == "target":
        path = history
    elif changed == "untouched":
        path = history.with_name("source_exclusions.json")
    else:
        entry = payload["entries"][1]
        path = history.with_name(entry[changed])
    with path.open("ab") as stream:
        stream.write(b"Unknown concurrent mutation")
    retained = [journal, history, *sidecars, path]
    before = {item: item.read_bytes() for item in retained}
    with pytest.raises(ValueError, match="changed"):
        exchange.recover_interrupted_import(root)
    assert {item: item.read_bytes() for item in before} == before


@pytest.mark.parametrize("field", ["target", "candidate", "backup"])
def test_recovery_rejects_noncanonical_path_mappings(tmp_path, field):
    package = fixtures._package(tmp_path)
    root, history, sidecars = _target(tmp_path)
    _interrupt_import(root, package, 1)
    journal = history.with_name(exchange.PUBLICATION_JOURNAL_NAME)
    payload = json.loads(journal.read_text())
    payload["entries"][1][field] = "../outside.sqlite3"
    journal.write_text(json.dumps(payload))
    before = {path: path.read_bytes() for path in [history, *sidecars]}
    with pytest.raises(ValueError, match="canonical|mapping"):
        exchange.recover_interrupted_import(root)
    assert journal.exists()
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("link", ["symbolic", "hard"])
def test_recovery_refuses_linked_candidates(tmp_path, link):
    package = fixtures._package(tmp_path)
    root, history, sidecars = _target(tmp_path)
    _interrupt_import(root, package, 1)
    journal = history.with_name(exchange.PUBLICATION_JOURNAL_NAME)
    payload = json.loads(journal.read_text())
    candidate = history.with_name(payload["entries"][1]["candidate"])
    other = tmp_path / "retained-source.sqlite3"
    if link == "symbolic":
        candidate.rename(other)
        candidate.symlink_to(other)
    else:
        os.link(candidate, other)
    before = {path: path.read_bytes() for path in [history, *sidecars, other]}
    with pytest.raises(ValueError, match="without links"):
        exchange.recover_interrupted_import(root)
    assert journal.exists()
    assert {path: path.read_bytes() for path in before} == before
