"""Serialize synthetic bank publishers without discarding another session's data.

Code version: v1.0.1-codex.0
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import multiprocessing
import os
from pathlib import Path
import sqlite3
import stat
from threading import Event
from unittest.mock import patch

import pytest

from app.core import wechat_macos_recovery_bank as bank
from tests import test_wechat_macos_recovery_bank as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


def _publish_before_replace(root, candidate, receipt, entered, release, results):
    """Pause a real child-process publisher inside its final replacement window."""
    original_replace = bank.os.replace

    def replace(source, target):
        entered.set()
        if not release.wait(15):
            raise TimeoutError("The synthetic publication gate was not released.")
        return original_replace(source, target)

    try:
        with patch.object(bank.os, "replace", side_effect=replace):
            bank.publish_mac_recovery_bank(root, candidate, receipt)
    except Exception as error:
        results.put((type(error).__name__, str(error)))
    else:
        results.put(("published", ""))


def test_concurrent_publisher_cannot_replace_another_sessions_new_bank(bank_archive):
    fixture = bank_archive
    history_before = fixture.history.read_bytes()
    stale_candidate, stale_receipt = fixtures._stage(
        fixture, packets=[fixture.direct], name="stale-session-a"
    )
    winning_candidate, winning_receipt = fixtures._stage(
        fixture, packets=[fixture.group], name="winning-session-b"
    )
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    results = context.Queue()
    publisher = context.Process(
        target=_publish_before_replace,
        args=(
            str(fixture.root),
            str(winning_candidate),
            winning_receipt,
            entered,
            release,
            results,
        ),
    )
    publisher.start()
    try:
        assert entered.wait(15), "The child publisher did not reach its final replace."
        with pytest.raises(ValueError, match="Another process is publishing"):
            bank.publish_mac_recovery_bank(fixture.root, stale_candidate, stale_receipt)
        assert stale_candidate.exists() and winning_candidate.exists()
        assert not fixture.bank_path.exists()
        release.set()
        publisher.join(15)
        assert not publisher.is_alive() and publisher.exitcode == 0
        assert results.get(timeout=5) == ("published", "")

        published = fixture.bank_path.read_bytes()
        assert (
            hashlib.sha256(published).hexdigest() == winning_receipt["candidate_sha256"]
        )
        with pytest.raises(ValueError, match="source identities changed"):
            bank.publish_mac_recovery_bank(fixture.root, stale_candidate, stale_receipt)
        assert fixture.bank_path.read_bytes() == published
        assert fixture.history.read_bytes() == history_before
        with sqlite3.connect(fixture.bank_path) as connection:
            scopes = [
                row[0] for row in connection.execute("SELECT id FROM conversations")
            ]
        assert scopes == [fixture.group_session]
        assert stale_candidate.exists()
        if os.name != "nt":
            lock = fixture.history.with_name(bank._PUBLICATION_LOCK_NAME)
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    finally:
        release.set()
        publisher.join(5)
        if publisher.is_alive():
            publisher.terminate()
            publisher.join(5)
        results.close()
        results.join_thread()
        publisher.close()


def test_rejected_candidate_releases_publication_lock(bank_archive):
    fixture = bank_archive
    candidate, receipt = fixtures._stage(fixture)
    changed = deepcopy(receipt)
    changed["candidate_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="candidate differs"):
        bank.publish_mac_recovery_bank(fixture.root, candidate, changed)
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    assert fixture.bank_path.is_file() and not candidate.exists()


def test_cancellation_during_publication_hash_preserves_existing_bank(bank_archive):
    fixture = bank_archive
    fixtures._publish(fixture, name="before-refresh-cancellation")
    before = fixture.bank_path.read_bytes()
    group = deepcopy(fixture.group)
    group["messages"].append(
        fixtures._message(57, 1, raw="Synthetic refresh cancellation candidate.")
    )
    candidate, receipt = fixtures._stage(
        fixture, packets=[group], name="cancelled-refresh-candidate"
    )
    entered, release, cancelled, callback_called = (Event() for _ in range(4))
    original_hash = bank._file_sha

    def gated_hash(path):
        if Path(path) == candidate:
            entered.set()
            if not release.wait(15):
                raise TimeoutError(
                    "The synthetic publication hash gate was not released."
                )
        return original_hash(path)

    def check_running():
        callback_called.set()
        if cancelled.is_set():
            raise RuntimeError("The synthetic refresh was cancelled.")

    with (
        patch.object(bank, "_file_sha", side_effect=gated_hash),
        patch.object(bank.os, "replace", wraps=bank.os.replace) as replace,
        ThreadPoolExecutor(max_workers=1) as worker,
    ):
        result = worker.submit(
            bank.publish_mac_recovery_bank,
            fixture.root,
            candidate,
            receipt,
            before_replace=check_running,
        )
        try:
            assert entered.wait(15), (
                "The publisher did not enter its hash verification."
            )
            cancelled.set()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="synthetic refresh was cancelled"):
            result.result(timeout=15)
        assert callback_called.is_set()
        replace.assert_not_called()
    assert fixture.bank_path.read_bytes() == before
    assert candidate.exists()
    # A cancelled final callback releases both locks so a later explicit retry can publish.
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)


def test_publication_refuses_a_redirected_lock_without_touching_target(bank_archive):
    fixture = bank_archive
    candidate, receipt = fixtures._stage(fixture)
    preserved = fixture.history.with_name("synthetic-lock-destination.txt")
    preserved.write_text("Preserve this unrelated synthetic file.", encoding="ascii")
    lock = fixture.history.with_name(bank._PUBLICATION_LOCK_NAME)
    lock.symlink_to(preserved)
    before = preserved.read_bytes()
    with pytest.raises(ValueError, match="publication lock"):
        bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    assert preserved.read_bytes() == before
    assert candidate.exists() and not fixture.bank_path.exists()


def test_lock_patch_keeps_existing_schema_three_bank_and_other_scope(bank_archive):
    fixture = bank_archive
    first, first_receipt = fixtures._stage(
        fixture, packets=[fixture.group], name="old-schema-three"
    )
    bank.publish_mac_recovery_bank(fixture.root, first, first_receipt)
    with sqlite3.connect(fixture.bank_path) as connection:
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='code_version'",
            ("v1.2.0-codex.0",),
        )
        before = connection.execute(
            "SELECT id,text,provenance FROM messages WHERE conversation_id=? ORDER BY id",
            (fixture.group_session,),
        ).fetchall()
    candidate, receipt = fixtures._stage(
        fixture, packets=[fixture.direct], name="merged-schema-three"
    )
    bank.publish_mac_recovery_bank(fixture.root, candidate, receipt)
    with sqlite3.connect(fixture.bank_path) as connection:
        assert (
            connection.execute(
                "SELECT id,text,provenance FROM messages WHERE conversation_id=? ORDER BY id",
                (fixture.group_session,),
            ).fetchall()
            == before
        )
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone() == (
            2,
        )
