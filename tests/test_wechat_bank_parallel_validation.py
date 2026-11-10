"""Published shadow proofs use bounded processes without changing sealed evidence.

Code version: v1.3.0-codex.0
"""

from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import closing, contextmanager
from multiprocessing import get_context
import sqlite3
from threading import Event

import pytest

from app.core import wechat_macos_recovery_bank as bank
from tests import test_wechat_macos_bank_v4_reader as revisions
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_macos_recovery_bank_upgrades as upgrades
from tests import test_wechat_macos_retained_clock_reader as clocks


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
app_stubs = upgrades.app_stubs
retained_clock = clocks.retained_clock
retained_v4 = revisions.retained_v4


def _read_only(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def test_concurrent_cold_readers_share_one_validation(bank_archive, monkeypatch):
    fixture = bank_archive
    fixtures._publish(fixture)
    entered, release, follower_waiting = Event(), Event(), Event()
    calls = []
    original = bank._validate_bank

    class ObservedFuture(Future):
        def result(self, *args, **kwargs):
            follower_waiting.set()
            return super().result(*args, **kwargs)

    def validate(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def attach():
        with closing(_read_only(fixture.history)) as connection:
            return bank._attach(connection, fixture.history)

    monkeypatch.setattr(bank, "Future", ObservedFuture)
    monkeypatch.setattr(bank, "_validate_bank", validate)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(attach)
        try:
            assert entered.wait(5)
            second = executor.submit(attach)
            assert follower_waiting.wait(5)
        finally:
            release.set()
        assert first.result() == second.result()
    assert calls == [1]
    assert not bank._VALIDATING


def test_bank_payload_keeps_the_original_sequential_digest(bank_archive, monkeypatch):
    fixture = bank_archive
    writer_version = bank.VERSION
    fixtures._publish(fixture)
    before = fixture.bank_path.read_bytes()

    def unexpected_pool(**kwargs):
        raise AssertionError("The ordered checksum has no independent worker stages.")

    monkeypatch.setattr(bank, "ProcessPoolExecutor", unexpected_pool)
    with closing(_read_only(fixture.history)) as connection:
        metadata = bank._attach(connection, fixture.history)
    with closing(_read_only(fixture.bank_path)) as connection:
        assert bank._rows_digest(connection) == metadata["payload_sha256"]
    assert bank.VERSION == writer_version == metadata["code_version"]
    assert fixture.bank_path.read_bytes() == before


def test_failed_bank_proof_releases_pending_readers_without_caching(bank_archive, monkeypatch):
    fixture = bank_archive
    fixtures._publish(fixture)
    calls = []

    def invalid(*args, **kwargs):
        calls.append(1)
        raise ValueError("Synthetic invalid bank proof.")

    monkeypatch.setattr(bank, "_validate_bank", invalid)
    monkeypatch.setattr(bank, "_VALIDATED", {})
    for _attempt in range(2):
        with closing(_read_only(fixture.history)) as connection:
            with pytest.raises(ValueError, match="invalid bank proof"):
                bank._attach(connection, fixture.history)
        assert not bank._VALIDATING
        assert not bank._VALIDATED
    assert calls == [1, 1]


@contextmanager
def _shadow_connection(fixture, *, session=None):
    with closing(_read_only(fixture.history)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "ATTACH DATABASE ? AS macos_recovery_bank",
            (fixture.fixture.bank_path.resolve().as_uri() + "?mode=ro&immutable=1",),
        )
        connection.execute(
            "CREATE TEMP TABLE macos_recovery_bank_scopes(id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        if session is None:
            connection.execute(
                "INSERT INTO temp.macos_recovery_bank_scopes "
                "SELECT id FROM macos_recovery_bank.conversations"
            )
        else:
            connection.execute("INSERT INTO temp.macos_recovery_bank_scopes VALUES(?)", (session,))
        yield connection


def _enable_shadow_parallel(monkeypatch):
    monkeypatch.setattr(bank, "_MIN_PARALLEL_SHADOW_ROWS", 0)
    monkeypatch.setattr(bank, "resolve_cpu_process_workers", lambda **kwargs: 3)
    monkeypatch.setattr(bank, "_SHADOW_VALIDATED", {})
    monkeypatch.setattr(bank, "_SHADOW_VALIDATING", {})


def test_real_spawn_shadow_ranges_match_complete_sequential_proofs(app_stubs, monkeypatch):
    fixture = app_stubs
    upgrades._publish(fixture)
    bank_bytes, catalog_bytes = fixture.fixture.bank_path.read_bytes(), fixture.catalog.read_bytes()
    with _shadow_connection(fixture) as connection:
        bank._validate_shadow_scope(connection, fixture.history, bank._inputs(fixture.history))
    _enable_shadow_parallel(monkeypatch)
    with _shadow_connection(fixture) as connection:
        bank._validate_shadow_sources(connection, fixture.history)
    assert fixture.fixture.bank_path.read_bytes() == bank_bytes
    assert fixture.catalog.read_bytes() == catalog_bytes
    assert bank._SHADOW_VALIDATED


def test_retained_clock_proofs_survive_parallel_shadow_checks(retained_clock, monkeypatch):
    fixture, _connection, _row, upgrade = retained_clock
    before = fixture.fixture.bank_path.read_bytes(), fixture.catalog.read_bytes()
    _enable_shadow_parallel(monkeypatch)
    with closing(_read_only(fixture.history)) as connection:
        assert bank._attach(connection, fixture.history)["schema_version"] == "3"
    with _shadow_connection(fixture) as connection:
        bank._validate_shadow_sources(connection, fixture.history)
    assert upgrade["clock_observations"]["native_minus_catalog_seconds"] == 1
    assert (fixture.fixture.bank_path.read_bytes(), fixture.catalog.read_bytes()) == before


def test_schema4_proofs_remain_checked_once_for_shared_bank_reads(retained_v4, monkeypatch):
    fixture, _connection, _prior, _following, _source, _reason = retained_v4
    before = fixture.bank_path.read_bytes()
    original = bank.schema4_reader.validate
    calls = []

    def validate(connection):
        calls.append(1)
        return original(connection)

    monkeypatch.setattr(bank.schema4_reader, "validate", validate)
    monkeypatch.setattr(bank, "_VALIDATED", {})
    for _attempt in range(2):
        with closing(_read_only(fixture.history)) as connection:
            assert bank._attach(connection, fixture.history)["schema_version"] == "4"
    assert calls == [1]
    assert fixture.bank_path.read_bytes() == before


def test_shadow_ranges_cover_rowid_gaps_without_promoting_a_subset(app_stubs, monkeypatch):
    fixture = app_stubs
    upgrades._publish(fixture)
    with sqlite3.connect(fixture.fixture.bank_path) as connection:
        connection.execute("UPDATE shadow_replacements SET rowid=rowid*101")
    _enable_shadow_parallel(monkeypatch)
    session = fixture.fixture.group_session
    with _shadow_connection(fixture, session=session) as connection:
        expected = connection.execute(
            "SELECT COUNT(*) FROM macos_recovery_bank.shadow_replacements WHERE conversation_id=?",
            (session,),
        ).fetchone()[0]
        assert expected > 0
        count = bank._parallel_shadow_count(
            connection, fixture.history, fixture.catalog.name, expected,
            bank._identity(fixture.catalog), bank._inputs(fixture.history),
        )
        assert count == expected
        bank._validate_shadow_sources(connection, fixture.history, conversation_id=session)
    assert all(key[-1] == session for key in bank._SHADOW_VALIDATED)


@pytest.mark.parametrize("change", ["missing", "altered"])
def test_real_shadow_worker_rejects_missing_or_changed_original_rows(app_stubs, change):
    fixture = app_stubs
    upgrades._publish(fixture)
    with sqlite3.connect(fixture.fixture.bank_path) as connection:
        first, last, expected = connection.execute(
            "SELECT MIN(rowid),MAX(rowid),COUNT(*) FROM shadow_replacements"
        ).fetchone()
        scopes = tuple(row[0] for row in connection.execute("SELECT id FROM conversations"))
    with sqlite3.connect(fixture.catalog) as connection:
        if change == "missing":
            connection.execute("DELETE FROM messages WHERE id=(SELECT id FROM messages LIMIT 1)")
        else:
            connection.execute("UPDATE messages SET text='Changed original catalog row'")
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as executor:
        future = executor.submit(
            bank._validate_shadow_job, str(fixture.history), fixture.catalog.name, scopes,
            (first, last), bank._identity(fixture.fixture.bank_path), bank._identity(fixture.catalog),
            bank._inputs(fixture.history),
        )
        if change == "missing":
            assert future.result() != expected
        else:
            with pytest.raises(ValueError, match="differs from its exact shadow receipt"):
                future.result()


def test_shadow_parent_rejects_incomplete_worker_membership(app_stubs, monkeypatch):
    fixture = app_stubs
    upgrades._publish(fixture)
    monkeypatch.setattr(bank, "_parallel_shadow_count", lambda *args: 0)
    monkeypatch.setattr(bank, "_SHADOW_VALIDATED", {})
    with _shadow_connection(fixture) as connection:
        with pytest.raises(ValueError, match="membership changed during validation"):
            bank._validate_shadow_sources(connection, fixture.history)
    assert not bank._SHADOW_VALIDATED
    assert not bank._SHADOW_VALIDATING


def test_shadow_worker_rejects_catalog_identity_change(app_stubs):
    fixture = app_stubs
    upgrades._publish(fixture)
    inputs = bank._inputs(fixture.history)
    bank_identity = bank._identity(fixture.fixture.bank_path)
    catalog_identity = bank._identity(fixture.catalog)
    fixture.catalog.touch()
    with pytest.raises(ValueError, match="input changed during worker validation"):
        bank._validate_shadow_job(
            str(fixture.history), fixture.catalog.name, (), (1, 10),
            bank_identity, catalog_identity, inputs,
        )


@pytest.mark.parametrize("failure", [BrokenProcessPool, ValueError])
def test_shadow_process_transport_fallback_preserves_proof_failures(app_stubs, monkeypatch, failure):
    fixture = app_stubs
    upgrades._publish(fixture)
    _enable_shadow_parallel(monkeypatch)

    class FailedPool:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, *args):
            future = Future()
            future.set_exception(failure("Synthetic worker failure."))
            return future

    monkeypatch.setattr(bank, "ProcessPoolExecutor", FailedPool)
    with _shadow_connection(fixture) as connection:
        if failure is BrokenProcessPool:
            bank._validate_shadow_sources(connection, fixture.history)
            assert bank._SHADOW_VALIDATED
        else:
            with pytest.raises(ValueError, match="Synthetic worker failure"):
                bank._validate_shadow_sources(connection, fixture.history)
            assert not bank._SHADOW_VALIDATED
        assert not bank._SHADOW_VALIDATING


def test_scoped_shadow_reader_reuses_pending_global_validation(app_stubs, monkeypatch):
    fixture = app_stubs
    upgrades._publish(fixture)
    entered, release, follower_waiting = Event(), Event(), Event()
    calls = []
    original = bank._validate_shadow_scope

    class ObservedFuture(Future):
        def result(self, *args, **kwargs):
            follower_waiting.set()
            return super().result(*args, **kwargs)

    def validate(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def validate_scope(session=None):
        with _shadow_connection(fixture, session=session) as connection:
            bank._validate_shadow_sources(connection, fixture.history, conversation_id=session)

    monkeypatch.setattr(bank, "Future", ObservedFuture)
    monkeypatch.setattr(bank, "_validate_shadow_scope", validate)
    monkeypatch.setattr(bank, "_SHADOW_VALIDATED", {})
    monkeypatch.setattr(bank, "_SHADOW_VALIDATING", {})
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(validate_scope)
        try:
            assert entered.wait(5)
            second = executor.submit(validate_scope, fixture.fixture.direct_session)
            assert follower_waiting.wait(5)
        finally:
            release.set()
        first.result()
        second.result()
    assert calls == [1]
    assert not bank._SHADOW_VALIDATING
