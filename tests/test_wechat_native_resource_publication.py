"""Reviewed native metadata publication refuses races and retains rollback bytes.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from contextlib import closing
import json
import shutil
import sqlite3

import pytest

from app.core import wechat_export_merge
from app.core import wechat_native_resource_revalidation as refresh
from app.core.job_lock import CACHE_TASK_LOCK_NAME, CacheTaskLock
from tests import test_wechat_native_resource_revalidation as fixtures


native_metadata = fixtures.native_metadata


@pytest.fixture
def staged_extension(native_metadata, monkeypatch):
    fixture = native_metadata
    monkeypatch.setattr(wechat_export_merge, "_clonefile", shutil.copyfile)
    initial = fixture.original.with_name("initial.sqlite3")
    refresh.stage_native_resource_revalidation(
        fixture.store, initial, conversation_ids=[fixture.conversation["id"]]
    )
    fallback = fixture.history.with_name("resource_revalidated.sqlite3")
    initial.replace(fallback)
    # The publication format is unchanged for legacy, fully sealed scopes.
    with closing(sqlite3.connect(fallback)) as source, source:
        source.execute(
            "UPDATE archive_info SET value='v1.0.1-codex.0' WHERE key='proof_version'"
        )
    before = (
        fixture.history.read_bytes(),
        fixture.original.read_bytes(),
        fallback.read_bytes(),
    )
    candidate = fallback.with_name("extended.sqlite3")
    report = refresh.stage_extended_native_resource_revalidation(
        fixture.store, candidate, conversation_ids=[fixture.conversation["id"]]
    )
    assert report["preserved_conversations"] == [fixture.conversation["id"]]
    return fixture, fallback, candidate, report, before


def test_explicit_publication_preserves_legacy_coverage_and_exact_rollback(
    staged_extension,
):
    fixture, fallback, candidate, report, before = staged_extension
    rollback = fixture.store.parent / "rollback.sqlite3"
    result = refresh.publish_native_resource_revalidation(
        fixture.store, report, rollback_path=rollback
    )
    assert result["published"] and result["rollback"] == str(rollback)
    assert rollback.read_bytes() == before[2]
    assert fixture.history.read_bytes() == before[0]
    assert fixture.original.read_bytes() == before[1]
    assert not candidate.exists()
    assert (
        refresh.select_resource_index(
            fixture.history, conversation_id=fixture.conversation["id"]
        )
        == fallback
    )


@pytest.mark.parametrize("changed", ["candidate", "history", "canonical", "fallback"])
def test_publication_refuses_changed_review_inputs(staged_extension, changed):
    fixture, fallback, candidate, report, _before = staged_extension
    target = {
        "candidate": candidate,
        "history": fixture.history,
        "canonical": fixture.original,
        "fallback": fallback,
    }[changed]
    with closing(sqlite3.connect(target)) as source, source:
        source.execute("INSERT INTO archive_info VALUES('later-change','retained')")
    before = fallback.read_bytes()
    with pytest.raises(ValueError, match="changed"):
        refresh.publish_native_resource_revalidation(
            fixture.store,
            report,
            rollback_path=fixture.store.parent / "refused-rollback.sqlite3",
        )
    assert fallback.read_bytes() == before
    assert candidate.exists()


def test_publication_never_overwrites_an_existing_rollback(staged_extension):
    fixture, fallback, candidate, report, before = staged_extension
    rollback = fixture.store.parent / "keep-rollback.sqlite3"
    rollback.write_bytes(b"Keep this independent evidence.")
    with pytest.raises(ValueError, match="rollback"):
        refresh.publish_native_resource_revalidation(
            fixture.store, report, rollback_path=rollback
        )
    assert rollback.read_bytes() == b"Keep this independent evidence."
    assert fallback.read_bytes() == before[2] and candidate.exists()


def test_publication_refuses_an_active_cache_writer(staged_extension):
    fixture, fallback, candidate, report, before = staged_extension
    lock = CacheTaskLock(fixture.store / CACHE_TASK_LOCK_NAME)
    assert lock.acquire("test-owned-writer")
    try:
        with pytest.raises(RuntimeError, match="active cache task"):
            refresh.publish_native_resource_revalidation(
                fixture.store,
                report,
                rollback_path=fixture.store.parent / "busy-rollback.sqlite3",
            )
    finally:
        lock.release()
    assert fallback.read_bytes() == before[2] and candidate.exists()


def test_failed_post_swap_readback_restores_exact_previous_bytes(
    staged_extension, monkeypatch
):
    fixture, fallback, candidate, report, before = staged_extension
    original = refresh._verified_coverage

    def fail_after_swap(history, path, **options):
        if path == fallback:
            raise ValueError("Injected post-publication readback failure.")
        return original(history, path, **options)

    monkeypatch.setattr(refresh, "_verified_coverage", fail_after_swap)
    rollback = fixture.store.parent / "readback-rollback.sqlite3"
    with pytest.raises(ValueError, match="readback failure"):
        refresh.publish_native_resource_revalidation(
            fixture.store, report, rollback_path=rollback
        )
    assert fallback.read_bytes() == before[2]
    assert rollback.read_bytes() == before[2]
    assert fixture.history.read_bytes() == before[0]
    assert fixture.original.read_bytes() == before[1]


def test_corrupt_prior_coverage_is_not_silently_discarded(native_metadata):
    fixture = native_metadata
    initial = fixture.original.with_name("initial.sqlite3")
    refresh.stage_native_resource_revalidation(
        fixture.store, initial, conversation_ids=[fixture.conversation["id"]]
    )
    fallback = fixture.history.with_name("resource_revalidated.sqlite3")
    initial.replace(fallback)
    with closing(sqlite3.connect(fallback)) as source, source:
        source.execute("UPDATE messages SET text='not the preserved source'")
    before = fallback.read_bytes()
    with pytest.raises(ValueError, match="sealed"):
        refresh.stage_extended_native_resource_revalidation(
            fixture.store,
            fallback.with_name("refused.sqlite3"),
            conversation_ids=[fixture.conversation["id"]],
        )
    assert fallback.read_bytes() == before


def test_stale_prior_pin_requires_fresh_reproof_without_losing_scope(native_metadata):
    fixture = native_metadata
    initial = fixture.original.with_name("initial.sqlite3")
    refresh.stage_native_resource_revalidation(
        fixture.store, initial, conversation_ids=[fixture.conversation["id"]]
    )
    fallback = fixture.history.with_name("resource_revalidated.sqlite3")
    initial.replace(fallback)
    with closing(sqlite3.connect(fixture.history)) as history, history:
        history.execute(
            "INSERT INTO archive_info VALUES('new-primary-revision','retained')"
        )
    candidate = fallback.with_name("reproved.sqlite3")
    report = refresh.stage_extended_native_resource_revalidation(
        fixture.store, candidate, conversation_ids=[fixture.conversation["id"]]
    )
    assert report["preserved_conversations"] == [fixture.conversation["id"]]
    with closing(sqlite3.connect(candidate)) as source, source:
        info = dict(source.execute("SELECT * FROM archive_info"))
    assert json.loads(info["history_file_identity"]) == refresh._resident(
        fixture.history
    )
