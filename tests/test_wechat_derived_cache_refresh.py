"""A retirement bridge preserves every surviving accepted contact reference.

Code version: v1.0.3-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import shutil
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_export_merge
from app.core.wechat_derived_cache_refresh import refresh_retired_contact_cache, refresh_retired_native_candidates
from app.core.wechat_derived_redundancy import file_identity, plan_derived_redundancy
from tests import test_wechat_derived_redundancy as fixtures


AUTHORS, CONTACTS = fixtures.AUTHORS, fixtures.CONTACTS
redundancy_store = fixtures.redundancy_store


@pytest.fixture(autouse=True)
def fixture_clone_backend(monkeypatch):
    if sys.platform != "darwin":
        monkeypatch.setattr(wechat_export_merge, "_clonefile", shutil.copyfile)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _inputs(fixture):
    return hashlib.sha256(_json({str(path.resolve()): analysis._identity(path) for path in (fixture.history, fixture.registry)}).encode()).hexdigest()


@pytest.fixture
def retired_cache(redundancy_store):
    fixture = redundancy_store
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.execute("UPDATE messages SET sender_id=? WHERE sender_id=?", (AUTHORS[0], AUTHORS[1]))
        connection.execute("UPDATE messages SET sender_id='legacy-0' WHERE sender_id='legacy-1'")
    plan = plan_derived_redundancy(fixture.store, contact_id=CONTACTS[0])
    assert len(plan["matches"]) == 3
    fingerprint = analysis._source_state(fixture.store)[2]
    cache = analysis.contact_analysis_cache_path(fixture.store, CONTACTS[0])
    cache.parent.mkdir()
    info = {
        "version": analysis.INDEX_VERSION,
        "native_embedded_version": analysis.NATIVE_EMBEDDED_VERSION,
        "fingerprint": fingerprint,
        "source_inputs": _inputs(fixture),
        "unchanged_rule": "Synthetic unchanged proof",
    }
    with (
        closing(analysis._readonly(fixture.history)) as history,
        closing(sqlite3.connect(cache)) as output,
        output,
    ):
        output.executescript(analysis._REFERENCE_SCHEMA)
        for row in history.execute(analysis._ROW_SELECT):
            proof = json.loads(row["provenance"])
            output.execute("INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                row["id"], row["id"], "[]", row["id"], row["conversation_id"], row["source_id"],
                row["account_key"], row["time_sort"], row["ordinal"], 1,
                row["timestamp_value"], row["timestamp_label"], row["time_basis"], proof.get("source_time") or "",
            ))
            output.execute("INSERT INTO statement_groups VALUES(?,?,?)", (row["id"], row["id"], row["conversation_id"]))
            output.execute("INSERT INTO statement_members VALUES(?,?)", (row["id"], row["id"]))
        output.executemany("INSERT INTO info VALUES(?,?)", info.items())
    before = {
        "source_fingerprint": fingerprint, "source_inputs": info["source_inputs"],
        "contact_cache_info": info, "contact_cache_identity": file_identity(cache),
    }
    with closing(sqlite3.connect(fixture.history)) as history, history:
        history.execute("CREATE TABLE derived_message_aliases(old_id TEXT PRIMARY KEY,native_id TEXT,source_id TEXT,metadata TEXT,proof TEXT)")
        for match in plan["matches"]:
            history.execute("INSERT INTO derived_message_aliases VALUES(?,?,?,?,?)", (
                match["derived_id"], match["native_id"], match["evidence"]["derived_source_id"],
                _json({"original_sha256": match["derived_sha256"]}), _json(match["evidence"]),
            ))
            history.execute("DELETE FROM messages WHERE id=?", (match["derived_id"],))
    state = analysis._source_state(fixture.store)
    before.update(expected_source_fingerprint_after=state[2], expected_source_inputs_after=_inputs(fixture), expected_source_checkpoint_after=state[3])
    return SimpleNamespace(**vars(fixture), plan=plan, before=before, cache=cache)


def _refresh(fixture):
    return refresh_retired_contact_cache(fixture.store, CONTACTS[0], plan=fixture.plan, before=fixture.before)


def _capture_cache(fixture):
    fixture.before["contact_cache_identity"] = file_identity(fixture.cache)


def test_exact_retirement_bridge_keeps_all_native_and_other_references(retired_cache):
    fixture = retired_cache
    sources = fixture.history.read_bytes(), fixture.registry.read_bytes()
    with closing(sqlite3.connect(fixture.cache)) as cache, cache:
        native_before = cache.execute("SELECT * FROM refs WHERE source_id LIKE 'ios-%' ORDER BY id").fetchall()
    report = _refresh(fixture)
    assert report["published"]
    assert report["removed_references"] == 3
    assert report["references_before"] == 9 and report["references_after"] == 6
    assert report["groups_before"] == 9 and report["groups_after"] == 6
    with closing(sqlite3.connect(fixture.cache)) as cache, cache:
        assert cache.execute("SELECT * FROM refs WHERE source_id LIKE 'ios-%' ORDER BY id").fetchall() == native_before
        info = dict(cache.execute("SELECT * FROM info"))
        assert info["fingerprint"] == fixture.before["expected_source_fingerprint_after"]
        assert info["source_inputs"] == fixture.before["expected_source_inputs_after"]
        assert info["unchanged_rule"] == "Synthetic unchanged proof"
    assert (fixture.history.read_bytes(), fixture.registry.read_bytes()) == sources


@pytest.mark.parametrize("pin", ["source_fingerprint", "expected_source_fingerprint_after", "expected_source_inputs_after", "expected_source_checkpoint_after"])
def test_stale_or_missing_source_pins_cannot_rebind_a_cache(retired_cache, pin):
    fixture = retired_cache
    before = fixture.cache.read_bytes()
    fixture.before[pin] = "0" * 64
    with pytest.raises(ValueError):
        _refresh(fixture)
    assert fixture.cache.read_bytes() == before


def test_changed_cache_cannot_be_overwritten(retired_cache):
    fixture = retired_cache
    with closing(sqlite3.connect(fixture.cache)) as cache, cache:
        cache.execute("UPDATE info SET value='Newer cache' WHERE key='unchanged_rule'")
    before = fixture.cache.read_bytes()
    with pytest.raises(ValueError, match="pre-publication cache"):
        _refresh(fixture)
    assert fixture.cache.read_bytes() == before


def test_alias_receipt_must_match_the_exact_retirement_plan(retired_cache):
    fixture = retired_cache
    with closing(sqlite3.connect(fixture.history)) as history, history:
        history.execute("UPDATE derived_message_aliases SET metadata='{}'")
    state = analysis._source_state(fixture.store)
    fixture.before.update(expected_source_fingerprint_after=state[2], expected_source_inputs_after=_inputs(fixture), expected_source_checkpoint_after=state[3])
    before = fixture.cache.read_bytes()
    with pytest.raises(ValueError, match="Published aliases"):
        _refresh(fixture)
    assert fixture.cache.read_bytes() == before


@pytest.mark.parametrize("failure", ["missing-native", "nested"])
def test_missing_native_or_nested_reference_requires_a_full_rebuild(retired_cache, failure):
    fixture = retired_cache
    match = fixture.plan["matches"][0]
    with closing(sqlite3.connect(fixture.cache)) as cache, cache:
        if failure == "missing-native":
            cache.execute("DELETE FROM refs WHERE id=?", (match["native_id"],))
        else:
            cache.execute("UPDATE refs SET component_path='[1]' WHERE id=?", (match["derived_id"],))
    _capture_cache(fixture)
    before = fixture.cache.read_bytes()
    with pytest.raises(ValueError, match="native counterpart|top-level|reference membership"):
        _refresh(fixture)
    assert fixture.cache.read_bytes() == before


@pytest.mark.parametrize("has_dependency", [False, True])
def test_other_evidence_remains_identical_or_refuses_dependency_changes(retired_cache, has_dependency):
    fixture = retired_cache
    value = fixture.plan["matches"][0]["derived_id"] if has_dependency else "unrelated proof"
    with closing(sqlite3.connect(fixture.cache)) as cache, cache:
        cache.execute("CREATE TABLE supplemental_evidence(key TEXT,value TEXT)")
        cache.execute("INSERT INTO supplemental_evidence VALUES('proof',?)", (_json({"anchor": value}),))
    _capture_cache(fixture)
    before = fixture.cache.read_bytes()
    if has_dependency:
        with pytest.raises(ValueError, match="evidence dependencies"):
            _refresh(fixture)
        assert fixture.cache.read_bytes() == before
    else:
        _refresh(fixture)
        with closing(sqlite3.connect(fixture.cache)) as cache, cache:
            assert cache.execute("SELECT value FROM supplemental_evidence").fetchone()[0] == _json({"anchor": value})


def test_removed_representative_uses_an_already_accepted_native_member(retired_cache):
    fixture = retired_cache
    match = fixture.plan["matches"][0]
    with closing(sqlite3.connect(fixture.cache)) as cache, cache:
        cache.execute("UPDATE statement_members SET statement_id=? WHERE reference_id=?", (match["derived_id"], match["native_id"]))
        cache.execute("DELETE FROM statement_groups WHERE id=?", (match["native_id"],))
    _capture_cache(fixture)
    _refresh(fixture)
    with closing(sqlite3.connect(fixture.cache)) as cache, cache:
        assert cache.execute("SELECT reference_id FROM statement_groups WHERE id=?", (match["derived_id"],)).fetchone()[0] == match["native_id"]


def test_refresh_does_not_select_any_source_message_body(retired_cache, monkeypatch):
    fixture = retired_cache
    import app.core.wechat_derived_cache_refresh as refresh

    original = refresh._readonly

    def bounded(path):
        connection = original(path)
        if path == fixture.history:
            def authorize(operation, table, column, _database, _trigger):
                if operation == sqlite3.SQLITE_READ and table == "messages" and column in {"text", "provenance", "search_text"}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(refresh, "_readonly", bounded)
    assert _refresh(fixture)["removed_references"] == 3


def _hint_fingerprint(fixture, *, previous):
    from app.core.wechat_contact_embedded import CANDIDATE_INDEX_VERSION, _identity

    state = fixture.plan["history_identity"]
    identity = tuple(state[key] for key in ("dev", "inode", "size", "mtime_ns", "ctime_ns")) if previous else _identity(fixture.history)
    return hashlib.sha256(json.dumps([CANDIDATE_INDEX_VERSION, str(fixture.history.resolve()), identity]).encode()).hexdigest()


def _native_hints(fixture, *, fingerprint=None, retired_parent=False):
    path = fixture.cache.parent / "native-author-candidates.sqlite3"
    with closing(sqlite3.connect(path)) as hints, hints:
        hints.executescript(
            "CREATE TABLE candidates(author TEXT,parent_id TEXT,PRIMARY KEY(author,parent_id)) WITHOUT ROWID;"
            "CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID;"
        )
        hints.executemany("INSERT INTO info VALUES(?,?)", [
            ("fingerprint", fingerprint or _hint_fingerprint(fixture, previous=True)),
            ("invalid", "0"), ("other_rule", "Unchanged native hints"),
        ])
        hints.executemany("INSERT INTO candidates VALUES(?,?)", ((AUTHORS[0], key) for key in fixture.native_ids))
        if retired_parent:
            hints.execute("INSERT INTO candidates VALUES(?,?)", (AUTHORS[0], fixture.derived_ids[0]))
    return path


def test_native_hint_bridge_preserves_all_candidates_and_other_metadata(retired_cache):
    fixture = retired_cache
    path = _native_hints(fixture)
    with closing(sqlite3.connect(path)) as hints, hints:
        before = hints.execute("SELECT * FROM candidates ORDER BY author,parent_id").fetchall()
    source_bytes = fixture.history.read_bytes(), fixture.registry.read_bytes()
    report = refresh_retired_native_candidates(fixture.store, fixture.plan)
    assert report["published"] and report["candidates"] == 6
    assert report["checked_retired_parents"] == 3
    with closing(sqlite3.connect(path)) as hints, hints:
        assert hints.execute("SELECT * FROM candidates ORDER BY author,parent_id").fetchall() == before
        assert dict(hints.execute("SELECT * FROM info")) == {
            "fingerprint": _hint_fingerprint(fixture, previous=False),
            "invalid": "0", "other_rule": "Unchanged native hints",
        }
    assert (fixture.history.read_bytes(), fixture.registry.read_bytes()) == source_bytes


@pytest.mark.parametrize("state", ["already_current", "different_source_fingerprint"])
def test_native_hint_bridge_leaves_other_fingerprints_untouched(retired_cache, monkeypatch, state):
    fixture = retired_cache
    fingerprint = _hint_fingerprint(fixture, previous=False) if state == "already_current" else "0" * 64
    path = _native_hints(fixture, fingerprint=fingerprint)
    before = path.read_bytes()
    import app.core.wechat_derived_cache_refresh as refresh

    def no_alias_read(*_args):
        raise AssertionError("An unrelated hint cache must be left alone.")

    monkeypatch.setattr(refresh, "_aliases", no_alias_read)
    report = refresh_retired_native_candidates(fixture.store, fixture.plan)
    assert not report["published"] and report["reason"] == state
    assert path.read_bytes() == before


def test_native_hint_bridge_refuses_a_retired_parent_dependency(retired_cache):
    fixture = retired_cache
    path = _native_hints(fixture, retired_parent=True)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="still a native author candidate"):
        refresh_retired_native_candidates(fixture.store, fixture.plan)
    assert path.read_bytes() == before


def test_native_hint_retired_parent_probe_scans_hints_once_and_uses_temp_primary_key(retired_cache, monkeypatch):
    fixture = retired_cache
    path = _native_hints(fixture)
    import app.core.wechat_derived_cache_refresh as refresh

    original = refresh._readonly
    queries = []

    def observed(selected):
        connection = original(selected)
        if selected == path:
            connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(refresh, "_readonly", observed)
    refresh_retired_native_candidates(fixture.store, fixture.plan)
    probes = [query for query in queries if "CROSS JOIN temp.retired_ids" in query]
    assert len(probes) == 1
    assert "main.candidates c" in probes[0]


def test_native_hint_bridge_never_selects_source_bodies(retired_cache, monkeypatch):
    fixture = retired_cache
    _native_hints(fixture)
    import app.core.wechat_derived_cache_refresh as refresh

    original = refresh._readonly

    def bounded(path):
        connection = original(path)
        if path == fixture.history:
            def authorize(operation, table, column, _database, _trigger):
                if operation == sqlite3.SQLITE_READ and table == "messages" and column in {"text", "provenance", "search_text"}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(refresh, "_readonly", bounded)
    assert refresh_retired_native_candidates(fixture.store, fixture.plan)["published"]


def test_native_hint_cache_source_drift_does_not_publish(retired_cache, monkeypatch):
    fixture = retired_cache
    path = _native_hints(fixture)
    before = path.read_bytes()
    from app.core import wechat_export_merge

    original = wechat_export_merge._clonefile

    def drift(source, destination):
        original(source, destination)
        with closing(sqlite3.connect(fixture.history)) as history, history:
            history.execute("INSERT INTO archive_info VALUES('synthetic-source-drift','changed')")

    monkeypatch.setattr(wechat_export_merge, "_clonefile", drift)
    with pytest.raises(ValueError, match="changed before bridge publication"):
        refresh_retired_native_candidates(fixture.store, fixture.plan)
    assert path.read_bytes() == before


def test_missing_native_hints_are_not_created_by_a_bridge(retired_cache):
    fixture = retired_cache
    report = refresh_retired_native_candidates(fixture.store, fixture.plan)
    assert not report["published"] and report["reason"] == "cache_missing"
    assert not (fixture.cache.parent / "native-author-candidates.sqlite3").exists()


def test_cloud_only_native_hints_are_not_opened_or_materialized(retired_cache, monkeypatch):
    fixture = retired_cache
    path = _native_hints(fixture)
    import app.core.wechat_derived_cache_refresh as refresh

    original = refresh.file_identity

    def cold(selected):
        if selected == path:
            raise ValueError("Synthetic cloud-only hint file.")
        return original(selected)

    def unopened(*_args):
        raise AssertionError("Cloud-only hints must not be opened.")

    monkeypatch.setattr(refresh, "file_identity", cold)
    monkeypatch.setattr(refresh, "_readonly", unopened)
    report = refresh_retired_native_candidates(fixture.store, fixture.plan)
    assert not report["published"] and report["reason"] == "cache_unavailable"


def test_native_hint_bridge_rejects_changed_registry_identity(retired_cache):
    fixture = retired_cache
    path = _native_hints(fixture)
    before = path.read_bytes()
    with closing(sqlite3.connect(fixture.registry)) as registry, registry:
        registry.execute("INSERT INTO contact_registry_info VALUES('synthetic-drift','changed')")
    with pytest.raises(ValueError, match="contact registry changed"):
        refresh_retired_native_candidates(fixture.store, fixture.plan)
    assert path.read_bytes() == before
