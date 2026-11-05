"""Native preference survives local source-order anomalies across public views.

Code version: v1.0.1-codex.0
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core.chat_history_browser import query_chat_history
from app.core.wechat_archive import _identifier
from app.core.wechat_session_export import WechatSessionExport
from tests import test_wechat_contact_statement_groups as grouping
from tests import test_wechat_conversation_event_browser as browser_cases
from tests import test_wechat_conversation_events as event_cases
from tests import test_wechat_native_html_precedence as source_cases


statement_store = grouping.statement_store
native_html_store = source_cases.native_html_store
event_store = event_cases.event_store
conversation_store = browser_cases.conversation_store


def _insert(connection, row):
    columns = ",".join(row)
    marks = ",".join("?" for _ in row)
    connection.execute(
        f"INSERT INTO messages({columns}) VALUES({marks})", tuple(row.values())
    )


def _update_clock(connection, identifier, stamp, *, native):
    provenance = json.loads(
        connection.execute(
            "SELECT provenance FROM messages WHERE id=?", (identifier,)
        ).fetchone()[0]
    )
    clock = stamp if native else stamp.replace(tzinfo=None)
    provenance["source_time"] = None if native else clock.isoformat(sep=" ")
    if native:
        provenance["unix_timestamp"] = int(stamp.timestamp())
    connection.execute(
        "UPDATE messages SET timestamp_value=?,timestamp_label=?,time_sort=?,"
        "provenance=? WHERE id=?",
        (
            clock.isoformat(),
            f"{stamp.day} {stamp:%b %Y %H:%M:%S}",
            stamp.timestamp(),
            json.dumps(provenance),
            identifier,
        ),
    )


def _late_permutation(connection, pairs, *, same_clock):
    """Keep independent local orders; only the final two native positions reverse."""
    left, right = pairs[-2:]
    orders = [
        connection.execute(
            "SELECT ordinal FROM messages WHERE id=?", (pair[1],)
        ).fetchone()[0]
        for pair in (left, right)
    ]
    connection.executemany(
        "UPDATE messages SET ordinal=? WHERE id=?",
        [(orders[1], left[1]), (orders[0], right[1])],
    )
    if same_clock:
        stamp = datetime.fromisoformat(
            connection.execute(
                "SELECT timestamp_value FROM messages WHERE id=?", (left[1],)
            ).fetchone()[0]
        )
        for html, native in (left, right):
            _update_clock(connection, html, stamp, native=False)
            _update_clock(connection, native, stamp, native=True)


def _html_only(connection, template_id):
    row = dict(
        connection.execute(
            "SELECT * FROM messages WHERE id=?", (template_id,)
        ).fetchone()
    )
    identifier = hashlib.sha256(b"preserved-html-only-global-note").hexdigest()
    text = "Only the independent HTML export retained this statement."
    provenance = json.loads(row["provenance"])
    provenance.update(
        record_id="html-only-global-note",
        primary_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        source_order=900,
    )
    provenance["locator"].update(export_msgid="900", message_ordinal=900)
    row.update(
        id=identifier,
        ordinal=900,
        text=text,
        search_text=text.casefold(),
        provenance=json.dumps(provenance),
    )
    _insert(connection, row)
    return identifier


@pytest.mark.parametrize("same_clock", [True, False])
@pytest.mark.parametrize("sort", ["oldest", "newest"])
def test_contact_and_default_export_keep_native_clean_pairs_and_html_only_tail(
    native_html_store, same_clock, sort
):
    fixture = native_html_store
    pairs = list(zip(fixture.html_ids, fixture.native_ids, strict=True))
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        _late_permutation(connection, pairs, same_clock=same_clock)
        html_only = _html_only(connection, fixture.html_ids[-1])
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, grouping.CID, sort=sort)
    assert page["status"] == "ready"
    assert page["observation_count"] == 13
    rows = {item["id"]: item for item in page["items"]}
    clean = pairs if same_clock else pairs[:4]
    for html, native in clean:
        assert native in rows and html not in rows
        assert rows[native]["source_label"] == "iPad archive"
        assert {item["id"] for item in rows[native]["observations"]} == {html, native}
    assert html_only in rows
    if same_clock:
        assert page["total_count"] == 7
    preferred = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            grouping.CID,
            sort=sort,
            source_fingerprint=page["source_fingerprint"],
        )
    )
    original = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            grouping.CID,
            sort=sort,
            source_fingerprint=page["source_fingerprint"],
            source_occurrences=True,
        )
    )
    assert {item["id"] for item in preferred} == set(rows)
    assert {item["id"] for item in original} == {
        *fixture.native_ids,
        *fixture.html_ids,
        html_only,
    }
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.fixture
def six_event_store(conversation_store):
    fixture = conversation_store
    pairs = list(fixture.ids)
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        templates = [
            dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (value,)
                ).fetchone()
            )
            for value in fixture.ids[0]
        ]
        base = datetime.fromisoformat("2020-01-01T08:00:00+08:00")
        for index in range(3, 6):
            sender = event_cases.PEER if index % 2 == 0 else event_cases.OWNER
            body = f"Private event body {index}; preserve  two spaces"
            pair = []
            for native, template in enumerate(templates):
                row = dict(template)
                provenance = json.loads(row["provenance"])
                record = ("native-" if native else "html-record-") + str(index)
                identifier = (
                    _identifier(event_cases.NATIVE, record)
                    if native
                    else hashlib.sha256(("html-" + str(index)).encode()).hexdigest()
                )
                provenance["record_id"] = record
                provenance["locator"].update(
                    {"local_id": 100 + index}
                    if native
                    else {"export_msgid": str(index + 1)}
                )
                if native:
                    provenance["server_id"] = "92233720368547758" + str(index)
                else:
                    provenance.update(
                        source_order=index + 1,
                        primary_text_sha256=hashlib.sha256(body.encode()).hexdigest(),
                    )
                row.update(
                    id=identifier,
                    ordinal=index + (101 if native else 1),
                    sender_id=sender if native else "alias-" + sender,
                    direction="received" if sender == event_cases.PEER else "sent",
                    text=body,
                    search_text=body.casefold(),
                    provenance=json.dumps(provenance),
                )
                _insert(connection, row)
                _update_clock(
                    connection,
                    identifier,
                    base + timedelta(seconds=index),
                    native=bool(native),
                )
                pair.append(identifier)
            pairs.append(tuple(pair))
    return SimpleNamespace(**vars(fixture), pairs=pairs)


@pytest.mark.parametrize("same_clock", [True, False])
def test_conversation_and_session_export_use_native_pairs_after_local_order_anomaly(
    six_event_store, same_clock
):
    fixture = six_event_store
    with sqlite3.connect(fixture.repository.path) as connection:
        connection.row_factory = sqlite3.Row
        _late_permutation(connection, fixture.pairs, same_clock=same_clock)
        html_only = _html_only(connection, fixture.pairs[0][0])
        connection.execute(
            "UPDATE conversations SET message_count=13 WHERE id=?",
            (event_cases.SESSION,),
        )
    before = fixture.repository.path.read_bytes(), fixture.registry.read_bytes()
    page = query_chat_history(
        fixture.store, source="wechat", session=event_cases.SESSION, page_size=50
    )
    visible = {item.stable_id: item for item in page.items}
    clean = fixture.pairs if same_clock else fixture.pairs[:4]
    for html, native in clean:
        assert native in visible and html not in visible
        assert visible[native].messaging["source_label"] == "iPad archive"
        assert len(visible[native].messaging["source_observations"]) == 2
    assert html_only in visible
    if same_clock:
        assert page.total_count == page.current_session.message_count == 7
    exported = list(WechatSessionExport(fixture.store, event_cases.SESSION).items())
    assert [item["id"] for item in exported] == [item.stable_id for item in page.items]
    assert before == (
        fixture.repository.path.read_bytes(),
        fixture.registry.read_bytes(),
    )


@pytest.mark.parametrize(
    "mutation", ["duplicate_html", "conflicting_server", "unverified_owner"]
)
def test_equal_clock_order_tolerance_does_not_relax_independent_identity_guards(
    native_html_store, mutation
):
    fixture = native_html_store
    pairs = list(zip(fixture.html_ids, fixture.native_ids, strict=True))
    extra = None
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        _late_permutation(connection, pairs, same_clock=True)
        if mutation == "unverified_owner":
            row = connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?",
                (fixture.html_snapshot,),
            ).fetchone()
            metadata = json.loads(row[0])
            metadata["owner_verified"] = False
            connection.execute(
                "UPDATE source_snapshots SET metadata=? WHERE id=?",
                (json.dumps(metadata), fixture.html_snapshot),
            )
        else:
            source_id = (
                fixture.html_ids[0]
                if mutation == "duplicate_html"
                else fixture.native_ids[0]
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (source_id,)
                ).fetchone()
            )
            extra = hashlib.sha256((mutation + source_id).encode()).hexdigest()
            row.update(id=extra, ordinal=950)
            if mutation == "conflicting_server":
                provenance = json.loads(row["provenance"])
                provenance["server_id"] = "7999999999999999999"
                row["provenance"] = json.dumps(provenance)
            _insert(connection, row)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, grouping.CID)
    visible = {item["id"] for item in page["items"]}
    assert fixture.html_ids[0] in visible and fixture.native_ids[0] in visible
    if extra is not None:
        assert extra in visible
        assert page["observation_count"] == 13
    else:
        assert page["total_count"] == page["observation_count"] == 12
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_complete_previous_cache_replays_native_preference_without_rescanning_authors(
    native_html_store, monkeypatch
):
    fixture = native_html_store
    pairs = list(zip(fixture.html_ids, fixture.native_ids, strict=True))
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        _late_permutation(connection, pairs, same_clock=True)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    with monkeypatch.context() as previous:
        previous.setattr(analysis, "INDEX_VERSION", "v1.14.0-codex.0")
        previous.setattr(analysis, "_apply_source_precedence", lambda _output: None)
        old = analysis.query_contact_analysis(fixture.store, grouping.CID)
        assert old["status"] == "ready"
        assert old["total_count"] == old["observation_count"] == 12
    cache = analysis.contact_analysis_cache_path(fixture.store, grouping.CID)
    with sqlite3.connect(cache) as connection:
        retained_refs = connection.execute("SELECT * FROM refs ORDER BY id").fetchall()

    def no_author_rescan(*_args, **_kwargs):
        raise AssertionError("A complete prior reference cache must be replayed.")

    for name in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, name, no_author_rescan)
    page = analysis.query_contact_analysis(fixture.store, grouping.CID)
    assert page["status"] == "ready"
    assert page["total_count"] == 6
    assert page["observation_count"] == 12
    assert page["source_fingerprint"] != old["source_fingerprint"]
    assert {item["id"] for item in page["items"]} == set(fixture.native_ids)
    assert all(item["source_label"] == "iPad archive" for item in page["items"])
    with sqlite3.connect(cache) as connection:
        assert (
            connection.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == retained_refs
        )
        info = dict(connection.execute("SELECT key,value FROM info"))
        assert info["version"] == analysis.INDEX_VERSION
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())
