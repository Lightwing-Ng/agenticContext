"""Native refresh projection keeps exact archive and source evidence.

Code version: v1.2.1-codex.0
"""

from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing, nullcontext
from dataclasses import replace
import hashlib
from multiprocessing import get_context

import pytest

from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_mac_refresh_source import RefreshError
from tests import test_wechat_mac_refresh as refresh_fixtures
from tests.test_wechat_mac_refresh import _database


mac_archive = refresh_fixtures.mac_archive
refresh_archive = refresh_fixtures.refresh_archive
OWNER = "wxid_synthetic_card_owner"
SENDER = "wxid_synthetic_card_sender"


def _project(raw, *, kind="direct", marker=0):
    return projection._project_message(
        {
            "local_id": 94,
            "local_type": (5 << 32) | 49,
            "server_id": 594,
            "create_time": 1700000094,
            "real_sender_id": 3,
            "message_content": raw,
            "compression_marker": marker,
        },
        OWNER,
        kind,
        {3: {"rowid": 3, "user_name": SENDER, "is_session": 1}},
    )


@pytest.mark.parametrize("kind,prefix", [("direct", ""), ("group", SENDER + ":\n")])
def test_app_card_projection_preserves_title_publisher_and_original_body_hash(kind, prefix):
    raw = (
        prefix
        + "<msg><appmsg><type>5</type><title>Literal &amp; source title</title>"
        "<sourcedisplayname>Original publisher</sourcedisplayname>"
        "<sourceusername>gh_synthetic_original</sourceusername>"
        "<url>https://example.invalid/private-article</url>"
        "<aeskey>opaque-secret-key</aeskey></appmsg>"
        "<appinfo><appname></appname></appinfo></msg>"
    )
    message = _project(raw, kind=kind)
    assert message["text"] == "[App message]"
    assert message["resource"] == {
        "kind": "app",
        "label": "[App message]",
        "native_time": 1700000094,
        "app_subtype": "5",
        "safe_title": "Literal & source title",
        "publisher_name": "Original publisher",
        "publisher_name_source": "appmsg/sourcedisplayname",
        "app_metadata_version": "1",
    }
    assert message["content_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert message["sender_id"] == SENDER
    assert "native_text" not in message


def test_third_party_app_keeps_app_name_without_inventing_a_publisher():
    message = _project(
        "<msg><appmsg><type>5</type><title>Literal third-party title</title>"
        "<url>https://example.invalid/unknown-publisher</url></appmsg>"
        "<appinfo><appname>Source application</appname></appinfo></msg>"
    )
    resource = message["resource"]
    assert resource["safe_title"] == "Literal third-party title"
    assert resource["app_name"] == "Source application"
    assert resource["app_name_source"] == "appinfo/appname"
    assert "publisher_name" not in resource


@pytest.mark.parametrize(
    "payload",
    [
        "<msg><appmsg><type>5</type><url>https://example.invalid/only-source</url></appmsg></msg>",
        "<msg><appmsg><type>5</type><title><b>Structured title</b></title>"
        "<sourcedisplayname><b>Nested publisher</b></sourcedisplayname></appmsg></msg>",
        "<msg><appmsg><type>5</type><title>Broken",
    ],
)
def test_unavailable_or_ambiguous_labels_keep_a_generic_card(payload):
    message = _project(payload)
    assert message["resource"] == {
        "kind": "app",
        "label": "[App message]",
        "native_time": 1700000094,
        "app_subtype": "5",
    }


def test_conversation_comparison_reuses_indexes_across_authenticated_shards(
    refresh_archive, monkeypatch,
):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    comparison = projection.ConversationComparison(history, scope, bank._inputs(history))
    scans = []
    timeline = projection.WechatArchiveRepository._timeline_tables

    def counted_timeline(*args, **kwargs):
        scans.append(kwargs.get("conversation_id"))
        return timeline(*args, **kwargs)

    monkeypatch.setattr(projection.WechatArchiveRepository, "_timeline_tables", counted_timeline)
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
    assert scans == [session]
    assert results[0] == results[1]
    assert results[0][1] == {"total": 4, "existing": 2, "excluded": 0}
    with pytest.raises(RefreshError, match="another conversation"):
        comparison.require_current(history, replace(scope, account_id="wxid_other"))


def test_conversation_comparison_rejects_an_overlay_published_during_reuse(refresh_archive):
    root, history, session, records = refresh_archive
    scope = projection.resolve_scope(root, session)
    baseline = bank._inputs(history)
    comparison = projection.ConversationComparison(history, scope, baseline)
    details = history.with_name("resource_details.json")

    def publish_detail_registry(_):
        details.write_text("{}", encoding="utf-8")
        details.chmod(0o600)

    database = _database(scope, records)
    try:
        projection.prepare_packet(root, scope, database, comparison=comparison)
        with pytest.raises(RefreshError, match="archive changed during refresh"):
            projection.prepare_packet(
                root, scope, database, comparison=comparison, on_counts=publish_detail_registry,
            )
        assert bank._inputs(history) == baseline
    finally:
        database.close()


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
