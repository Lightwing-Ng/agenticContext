"""Verify Mac refresh comparison reuse and bounded native row projection.

Code version: v1.0.0-codex.0
"""

from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing, nullcontext
from dataclasses import replace
from multiprocessing import get_context

import pytest

from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_mac_refresh_source import RefreshError
from app.core.wechat_secondary_completions import FILENAME
from tests import test_wechat_mac_refresh as fixtures
from tests.test_wechat_mac_refresh import _database


mac_archive = fixtures.mac_archive
refresh_archive = fixtures.refresh_archive


def test_conversation_comparison_reuses_indexes_across_authenticated_shards(
    refresh_archive, monkeypatch,
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    comparison = projection.ConversationComparison(history, scope, bank._inputs(history))
    scans = []
    scan = projection._archive_comparison_indexes

    def counted_scan(*args):
        scans.append(scope)
        return scan(*args)

    monkeypatch.setattr(projection, "_archive_comparison_indexes", counted_scan)
    empty = _database(scope, records, include_target=False)
    try:
        assert projection.prepare_packet(root, scope, empty, comparison=comparison) == (
            None, {"total": 0, "existing": 0, "excluded": 0},
        )
        assert not scans
    finally:
        empty.close()

    results = []
    for _ in range(2):
        database = _database(scope, records)
        try:
            results.append(projection.prepare_packet(root, scope, database, comparison=comparison))
        finally:
            database.close()
    assert scans == [scope]
    assert results[0] == results[1]
    assert results[0][1] == {"total": 4, "existing": 2, "excluded": 0}
    with pytest.raises(RefreshError, match="another conversation"):
        comparison.require_current(history, replace(scope, account_id="wxid_other"))


def test_conversation_comparison_rejects_overlay_publication_during_projection(
    refresh_archive, monkeypatch,
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    baseline = bank._inputs(history)
    comparison = projection.ConversationComparison(history, scope, baseline)
    new_overlay = history.with_name(FILENAME)
    ordered = projection._ordered_native_projection

    def publish_overlay_after_projection(*args, **kwargs):
        yield from ordered(*args, **kwargs)
        new_overlay.write_bytes(b"Synthetic replacement catalog")

    with closing(_database(scope, records)) as database:
        projection.prepare_packet(root, scope, database, comparison=comparison)
        monkeypatch.setattr(
            projection, "_ordered_native_projection", publish_overlay_after_projection,
        )
        with pytest.raises(RefreshError, match="archive changed during refresh"):
            projection.prepare_packet(root, scope, database, comparison=comparison)
    assert bank._inputs(history) == baseline


@pytest.mark.parametrize("broken", [False, True])
def test_scoped_parallel_projection_preserves_packet_order_and_exclusions(refresh_archive, monkeypatch, broken):
    root, _history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    monkeypatch.setattr(projection, "_PROJECTION_BATCH_ROWS", 2)

    class UnavailablePool:
        def submit(self, *_args):
            raise BrokenProcessPool("Synthetic unavailable projection worker.")

    def packet(executor=None):
        with closing(_database(scope, records)) as database:
            table = projection._table(scope.peer_id)
            database.connection.execute(
                f'UPDATE "{table}" SET WCDB_CT_message_content=9 '
                f'WHERE local_id=(SELECT max(local_id) FROM "{table}")'
            )
            excluded = []
            result, counts = projection.prepare_packet(
                root, scope, database, projection_executor=executor, on_excluded=excluded.append,
            )
            return bank._json(result), counts, excluded

    expected = packet()
    if not broken:
        def unexpected_serial_decode(*_args):
            pytest.fail("The runnable synthetic batches must be decoded by the child processes.")

        monkeypatch.setattr(projection, "_project_native_row", unexpected_serial_decode)
    context = nullcontext(UnavailablePool()) if broken else ProcessPoolExecutor(
        max_workers=2, mp_context=get_context("spawn"),
    )
    with context as executor:
        assert packet(executor) == expected
    assert expected[1] == {"total": 4, "existing": 2, "excluded": 1}
    assert expected[2][0]["reason"] == "row_wcdb_decode_failure"


def test_projection_ipc_cost_rejects_large_or_non_scalar_native_fields(monkeypatch):
    monkeypatch.setattr(projection, "_PROJECTION_BATCH_BYTES", 2_048)
    ordinary = {
        "local_id": 1, "server_id": 2, "local_type": 1, "create_time": 3,
        "real_sender_id": 4, "compression_marker": 0, "message_content": "small",
    }
    blob_identifier = {**ordinary, "real_sender_id": b"a" * 5_000}
    names = {4: {"rowid": 4, "user_name": "a" * 5_000, "is_session": 0}}
    assert list(projection._projection_batches([ordinary], {})) == [
        ([ordinary], [projection.MAX_TEXT_BYTES]),
    ]
    assert list(projection._projection_batches([blob_identifier, ordinary], names)) == [
        ([blob_identifier], None), ([ordinary], None),
    ]
    compressed = {**ordinary, "message_content": b"Synthetic frame", "compression_marker": 4}
    monkeypatch.setattr(projection, "get_wcdb_frame_size", lambda _value: 64)
    assert list(projection._projection_batches([compressed], {})) == [([compressed], [64])]
    monkeypatch.setattr(projection, "get_wcdb_frame_size", lambda _value: None)
    assert list(projection._projection_batches([compressed], {})) == [([compressed], None)]


@pytest.mark.parametrize(("cores", "memory", "expected"), [
    (18, 64 * 1024**3, 16),
    (10, 32 * 1024**3, 8),
    (18, 512 * 1024**2, 4),
    (2, 4 * 1024**3, 1),
])
def test_native_projection_pool_scales_with_cpu_and_memory(monkeypatch, cores, memory, expected):
    resources = projection.discover_compute_resources(logical_cpu_count=cores, memory_bytes=memory)
    monkeypatch.setattr(projection, "discover_compute_resources", lambda: resources)

    class Executor:
        def __init__(self, *, max_workers, mp_context):
            self.workers = max_workers
            assert mp_context.get_start_method() == "spawn"

        def shutdown(self):
            pass

        def terminate_workers(self):
            pytest.fail("A clean exit should close the worker pool normally.")

    monkeypatch.setattr(projection, "ProcessPoolExecutor", Executor)
    with projection.native_projection_executor() as (executor, in_flight):
        assert in_flight == expected
        assert (executor.workers if executor else 1) == expected
