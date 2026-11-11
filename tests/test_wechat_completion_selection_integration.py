"""Readers and compaction share bounded selection without widening proof caches.

Code version: v1.0.0-codex.0
"""

from contextlib import closing
import os
import sqlite3

import pytest

from app.core import wechat_completion_selection as selection
from app.core import wechat_secondary_completions as common
from app.core.wechat_archive import WechatArchiveRepository, _file_identity
from tests import test_wechat_secondary_completions as fixtures


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
source_binding = fixtures.source_binding
witness = fixtures.witness
completion_fixture = fixtures.completion_fixture


@pytest.mark.parametrize("scope", ["global", "selected", "unrelated"])
def test_actual_reader_keeps_global_scoped_rows_and_uses_only_bounded_probes(
    completion_fixture, monkeypatch, scope
):
    fixture = completion_fixture
    fixtures._publish(fixture, fixtures._stage(fixture))
    target = common.completion_path(fixture["root"])
    identifier = fixture["conversation"]["id"] if scope == "selected" else None
    if scope == "unrelated":
        identifier = "unrelated-conversation"
    plans = []
    original = selection.BoundedActiveSelection

    class RecordingSelection(original):
        def _replacement_query(self, count):
            query = super()._replacement_query(count)
            plans.extend(row[3] for row in self.connection.execute(
                "EXPLAIN QUERY PLAN " + query, ["candidate"] * count
            ))
            return query

    monkeypatch.setattr(selection, "BoundedActiveSelection", RecordingSelection)
    repository = WechatArchiveRepository(fixture["root"])
    with closing(common._readonly(fixture["history"])) as connection:
        conversations, upstream, status = repository._timeline_tables(
            connection, _file_identity(fixture["history"]),
            include_secondary=False, strict=True,
        )
        assert status in {"ready", "absent"}
        connection.execute("ATTACH DATABASE ? AS expected_completion", (str(target),))
        expected_active = [row[0] for row in connection.execute(
            "SELECT e.id FROM expected_completion.entries e WHERE "
            + common._active_predicate(upstream)
            + (" AND e.conversation_id=?" if identifier else "") + " ORDER BY e.id",
            (identifier,) if identifier else (),
        )]
        source_rows = [dict(row) for row in connection.execute("SELECT * FROM " + upstream)]
        projected = [common._decoded_message(row) for row in connection.execute(
            "SELECT * FROM expected_completion.messages ORDER BY id"
        ) if row["id"] in expected_active]
        expected = [row for row in source_rows if row["id"] not in expected_active] + projected
        for _repeat in range(2):
            _, final, attached = common.attach_secondary_message_completions(
                connection, fixture["history"], conversations, upstream,
                conversation_id=identifier,
            )
            assert attached == "ready"
            assert [row[0] for row in connection.execute(
                "SELECT id FROM secondary_completion_active ORDER BY id"
            )] == expected_active
            actual = [dict(row) for row in connection.execute("SELECT * FROM " + final)]
            assert sorted(actual, key=lambda row: row["id"]) == sorted(expected, key=lambda row: row["id"])
            assert not connection.execute(
                "SELECT 1 FROM temp.sqlite_master WHERE name LIKE 'completion_selection_%'"
            ).fetchone()
    if scope != "unrelated":
        assert any("SEARCH " in line and "id=?" in line for line in plans), plans
    assert not any("AUTOMATIC COVERING INDEX" in line for line in plans), plans


def test_compaction_reuses_only_in_process_full_source_validation(completion_fixture, monkeypatch):
    fixture = completion_fixture
    stage = fixtures._stage(fixture)
    calls = []
    validate = common._validate_entries

    def observe(*args, **kwargs):
        calls.append(kwargs.get("conversation_id"))
        return validate(*args, **kwargs)

    monkeypatch.setattr(common, "_validate_entries", observe)
    source, pins = fixture["candidate"], common._pins(fixture["history"])
    with common._LOCK:
        common._CACHE[common._validation_cache_key(
            source, common._identity(source), pins, fixture["conversation"]["id"]
        )] = True
    first = common.compact_secondary_message_completions(
        fixture["root"], source, candidate_sha256=stage["candidate_sha256"], dry_run=True
    )
    second = common.compact_secondary_message_completions(
        fixture["root"], source, candidate_sha256=stage["candidate_sha256"], dry_run=True
    )
    assert calls == [None]
    assert not first["source_validation_cache_reused"]
    assert second["source_validation_cache_reused"]
    with common._LOCK:
        common._CACHE.clear()
    third = common.compact_secondary_message_completions(
        fixture["root"], source, candidate_sha256=stage["candidate_sha256"], dry_run=True
    )
    assert calls == [None, None] and not third["source_validation_cache_reused"]


@pytest.mark.parametrize("drift", ["source", "pins"])
def test_a_cached_compaction_rechecks_source_identity_and_pins_before_reuse(
    completion_fixture, monkeypatch, drift
):
    fixture = completion_fixture
    stage = fixtures._stage(fixture)
    common.compact_secondary_message_completions(
        fixture["root"], fixture["candidate"], candidate_sha256=stage["candidate_sha256"], dry_run=True
    )
    monkeypatch.setattr(common, "_validate_entries", lambda *_args, **_kwargs: pytest.fail("Stale cache was used"))

    def change(value):
        if value["phase"] != "validating_source_entries":
            return
        if drift == "source":
            state = fixture["candidate"].stat()
            os.utime(fixture["candidate"], ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000))
        else:
            with closing(sqlite3.connect(fixture["catalog"])) as catalog:
                catalog.execute("INSERT INTO archive_info VALUES('drift','retained')")
                catalog.commit()

    with pytest.raises(ValueError, match="changed before proof validation"):
        common.compact_secondary_message_completions(
            fixture["root"], fixture["candidate"], candidate_sha256=stage["candidate_sha256"],
            dry_run=True, progress=change,
        )
