"""Preferred native observations retain all source evidence and account boundaries.

Code version: v1.4.2-codex.0
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core import wechat_contact_analysis as analysis
from app.core import wechat_export_merge as export_merge
from app.core.wechat_archive import _identifier
from tests import test_wechat_contact_statement_groups as grouping


CID = grouping.CID
EXPORT_SENDER = "xid-synthetic-local-target"
statement_store = grouping.statement_store


@pytest.fixture
def preferred_native_store(statement_store):
    fixture = statement_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE source_snapshots("
            "id INTEGER PRIMARY KEY,snapshot_id TEXT,source_id TEXT,metadata TEXT)"
        )
        for index, row in enumerate(fixture.rows, 1):
            label = "iPad archive" if index == 1 else "iPhone archive"
            owner = row["account_id"]
            metadata = {
                "source_id": row["source_id"],
                "source_account_id": owner,
                "account_hash": hashlib.md5(owner.encode()).hexdigest(),
                "account_hash_verified": True,
                "source_label": label,
                "source_file": f"Synthetic {label} checkpoint",
            }
            snapshot = connection.execute(
                "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) "
                "VALUES(?,?,?)",
                (
                    "synthetic-native-" + str(index),
                    row["source_id"],
                    json.dumps(metadata),
                ),
            ).lastrowid
            provenance = json.loads(
                connection.execute(
                    "SELECT provenance FROM messages WHERE id=?", (row["id"],)
                ).fetchone()[0]
            )
            provenance["source_snapshot"] = snapshot
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(provenance), row["id"]),
            )
        original = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.rows[0]["id"],)
            ).fetchone()
        )
        owner = fixture.rows[0]["account_id"]
        metadata = {
            "source_id": fixture.rows[0]["source_id"],
            "source_account_id": owner,
            "account_hash": hashlib.md5(owner.encode()).hexdigest(),
            "account_hash_verified": True,
            "source_label": "iPhone archive",
            "source_file": "Synthetic same-owner iPhone checkpoint",
        }
        snapshot = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            (
                "synthetic-native-third",
                fixture.rows[0]["source_id"],
                json.dumps(metadata),
            ),
        ).lastrowid
        copied = dict(original)
        copied["id"] = "0" * 64
        provenance = json.loads(copied["provenance"])
        provenance["source_snapshot"] = snapshot
        copied["provenance"] = json.dumps(provenance)
        columns = ",".join(copied)
        values = ",".join("?" for _ in copied)
        connection.execute(
            f"INSERT INTO messages({columns}) VALUES({values})", tuple(copied.values())
        )
    return SimpleNamespace(
        **vars(fixture), preferred=original["id"], other_same_owner=copied["id"]
    )


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_ipad_is_the_primary_observation_without_deleting_other_sources(
    preferred_native_store, sort
):
    fixture = preferred_native_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID, sort=sort)
    assert page["total_count"] == 1
    assert page["observation_count"] == 3
    (item,) = page["items"]
    assert item["id"] == fixture.preferred
    assert item["source_label"] == "iPad archive"
    assert {origin["id"] for origin in item["observations"]} == {
        fixture.preferred,
        fixture.other_same_owner,
        fixture.rows[1]["id"],
    }
    assert {origin["source_label"] for origin in item["observations"]} == {
        "iPad archive",
        "iPhone archive",
    }
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_native_preference_cannot_merge_conflicting_server_events(
    preferred_native_store,
):
    fixture = preferred_native_store
    with sqlite3.connect(fixture.history) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?",
                (fixture.other_same_owner,),
            ).fetchone()[0]
        )
        provenance["server_id"] = "7210500216981401698"
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(provenance), fixture.other_same_owner),
        )
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 2
    assert page["observation_count"] == 3
    preferred = next(item for item in page["items"] if item["id"] == fixture.preferred)
    assert preferred["source_label"] == "iPad archive"
    assert preferred["observation_count"] == 2
    other = next(
        item for item in page["items"] if item["id"] == fixture.other_same_owner
    )
    assert other["observation_count"] == 1


def _insert_message(connection, row):
    columns = ",".join(row)
    marks = ",".join("?" for _ in row)
    connection.execute(
        f"INSERT INTO messages({columns}) VALUES({marks})", tuple(row.values())
    )


def _change_provenance(fixture, message_id, change):
    with sqlite3.connect(fixture.history) as connection:
        value = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (message_id,)
            ).fetchone()[0]
        )
        change(value)
        connection.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(value), message_id),
        )


@pytest.fixture
def native_html_store(statement_store, monkeypatch):
    """Keep six proven source/account events with independent local locators."""
    # Source-proof fixtures use disposable files; publication tests retain the
    # real APFS no-fallback boundary.
    monkeypatch.setattr(export_merge, "_clonefile", shutil.copyfile)
    fixture = statement_store
    first = fixture.rows[0]
    owner, source = first["account_id"], first["source_id"]
    account_hash = hashlib.md5(owner.encode()).hexdigest()
    digest = hashlib.sha256(b"synthetic-html-precedence-export").hexdigest()
    html_source = "html-" + digest[:24]
    html_conversation = "wechat:" + _identifier(html_source, grouping.TARGET)
    native_ids, html_ids = [], []
    with closing(sqlite3.connect(fixture.history)) as connection, connection:
        connection.row_factory = sqlite3.Row
        template = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (first["id"],)
            ).fetchone()
        )
        connection.execute("UPDATE messages SET sender_id='wxid_other'")
        connection.execute(
            "UPDATE conversations SET kind='direct',peer_id=? WHERE id=?",
            (grouping.TARGET, first["conversation_id"]),
        )
        connection.execute(
            "CREATE TABLE source_snapshots("
            "id INTEGER PRIMARY KEY,snapshot_id TEXT,source_id TEXT,metadata TEXT)"
        )
        native_metadata = {
            "source_id": source,
            "source_account_id": owner,
            "account_hash_verified": True,
            "account_hash": account_hash,
            "source_label": "iPad archive",
            "source_file": "Synthetic iPad checkpoint",
            "identity_evidence": {
                "kind": "native_mmkv_filename_with_directory_md5",
                "source_account_id": owner,
                "evidence_path": "Documents/MMappedKV/mmsetting.archive." + owner,
            },
        }
        native_snapshot = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            ("synthetic-ipad-snapshot", source, json.dumps(native_metadata)),
        ).lastrowid
        html_metadata = {
            "source_id": html_source,
            "source_sha256": digest,
            "source_account_id": owner,
            "source_account_alias": "Original HTML alias",
            "owner_verified": True,
            "owner_evidence": {
                "native_username": owner,
                "outgoing_observations": 6,
                "existing_native_authorities": ["native_account_owner"],
                "basis": "Synthetic same-frame outgoing IDs and independent native owner",
            },
            "source_label": "HTML export",
            "source_file": "Synthetic original HTML.zip",
        }
        html_snapshot = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            (digest, html_source, json.dumps(html_metadata)),
        ).lastrowid
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                html_source,
                "account:" + owner,
                owner,
                "Original HTML alias",
                "Corroborated export ID",
                "HTML export",
                "Synthetic original HTML.zip",
                "",
            ),
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                html_conversation,
                html_source,
                "account:" + owner,
                grouping.TARGET,
                "Synthetic HTML peer",
                "direct",
                6,
                0,
                0,
                "",
                "",
                "",
            ),
        )
        base = datetime.fromisoformat("2020-08-12T13:46:00+08:00")
        for index in range(1, 7):
            stamp = base + timedelta(seconds=index)
            body = f"Synthetic sequence statement {index}"
            body_hash = hashlib.sha256(body.encode()).hexdigest()
            record_id = "native-local-" + str(index)
            native = {
                **template,
                "id": _identifier(source, record_id),
                "ordinal": 100 + index,
                "sender_id": grouping.TARGET,
                "direction": "received",
                "text": body,
                "search_text": body.casefold(),
                "timestamp_value": stamp.isoformat(),
                "timestamp_label": stamp.strftime("%d %b %Y %H:%M:%S"),
                "time_sort": stamp.timestamp(),
                "time_basis": "Native Unix timestamp; rendered +08:00",
                "provenance": json.dumps(
                    {
                        "record_id": record_id,
                        "source_snapshot": native_snapshot,
                        "source_time": None,
                        "message_type": 1,
                        "text_extraction_kind": "plain_text",
                        "server_id": str(7000000000000000000 + index),
                        "unix_timestamp": int(stamp.timestamp()),
                        "locator": {
                            "local_id": index,
                            "row_id": index,
                            "database": "message_4.sqlite",
                            "database_relative_path": f"Documents/{account_hash}/DB/message_4.sqlite",
                            "table": "Chat_"
                            + hashlib.md5(grouping.TARGET.encode()).hexdigest(),
                        },
                    }
                ),
            }
            html = {
                **native,
                "id": _identifier(html_source, "member:local-" + str(index)),
                "conversation_id": html_conversation,
                "ordinal": index,
                "sender_id": EXPORT_SENDER,
                "timestamp_value": stamp.replace(tzinfo=None).isoformat(),
                "time_basis": "Source clock; timezone unknown",
                "provenance": json.dumps(
                    {
                        "record_id": "member:local-" + str(index),
                        "source_snapshot": html_snapshot,
                        "source_time": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                        "text_extraction_kind": "plain_text",
                        "export_msgid_namespace": "MesLocalID",
                        "sender_native_id": grouping.TARGET,
                        "sender_export_id": EXPORT_SENDER,
                        "avatar_path_metadata": "Portrait/" + grouping.TARGET + ".jpg",
                        "primary_text_sha256": body_hash,
                        "source_order": index,
                        "original_timezone": "unknown",
                        "locator": {
                            "export_msgid": str(index),
                            "export_msgid_namespace": "MesLocalID",
                            "export_msgtype": "1",
                            "archive_sha256": digest,
                            "member_path": "Synthetic/peer.html",
                            "message_ordinal": index,
                            "page": 0,
                            "page_message_ordinal": index,
                        },
                    }
                ),
            }
            _insert_message(connection, native)
            _insert_message(connection, html)
            native_ids.append(native["id"])
            html_ids.append(html["id"])
    with closing(sqlite3.connect(fixture.registry)) as connection, connection:
        connection.execute(
            "INSERT INTO sender_bindings VALUES(?,?,?,?,?,?,?)",
            (
                html_source,
                EXPORT_SENDER,
                "",
                "",
                CID,
                "corroborated_export_username",
                "{}",
            ),
        )
    return SimpleNamespace(
        **vars(fixture),
        owner=owner,
        native_source=source,
        html_source=html_source,
        native_snapshot=native_snapshot,
        html_snapshot=html_snapshot,
        html_conversation=html_conversation,
        native_ids=native_ids,
        html_ids=html_ids,
    )


@pytest.mark.parametrize("sort", ["newest", "oldest"])
def test_exact_monotone_source_sequence_prefers_ipad_with_all_html_evidence(
    native_html_store, sort
):
    fixture = native_html_store
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID, sort=sort)
    assert page["total_count"] == 6
    assert page["observation_count"] == 12
    assert page["counts"]["source_accounts"] == 1
    assert page["counts"]["sources"] == 2
    assert {item["id"] for item in page["items"]} == set(fixture.native_ids)
    for item in page["items"]:
        assert item["source_label"] == "iPad archive"
        assert item["observation_count"] == 2
        assert not item["time_conflict"]
        assert not item["time_uncertain"]
        assert item["display_timestamp_value"].endswith("+08:00")
        html = next(
            o for o in item["observations"] if o["source_id"] == fixture.html_source
        )
        assert html["timestamp_value"] and not html["timestamp_value"].endswith(
            "+08:00"
        )
    exported = list(
        analysis.export_contact_analysis_items(
            fixture.store,
            CID,
            sort=sort,
            source_fingerprint=page["source_fingerprint"],
            source_occurrences=True,
        )
    )
    assert len(exported) == 12
    assert {item["id"] for item in exported} == set(
        fixture.native_ids + fixture.html_ids
    )
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize("side", ["html", "native", "all"])
def test_source_sequence_matches_when_device_local_identifiers_differ(
    native_html_store, side
):
    fixture = native_html_store
    if side == "html":
        changes = [(fixture.html_ids[-1], "export_msgid", "206")]
    elif side == "native":
        changes = [(fixture.native_ids[-1], "local_id", 206)]
    else:
        changes = [
            (identifier, "export_msgid", str(200 + index))
            for index, identifier in enumerate(fixture.html_ids, 1)
        ] + [
            (identifier, "local_id", 600 + index)
            for index, identifier in enumerate(fixture.native_ids, 1)
        ]
    for identifier, field, value in changes:
        _change_provenance(
            fixture,
            identifier,
            lambda provenance, field=field, value=value: provenance["locator"].update(
                {field: value}
            ),
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 6
    assert page["observation_count"] == 12
    assert {item["id"] for item in page["items"]} == set(fixture.native_ids)
    assert all(item["source_label"] == "iPad archive" for item in page["items"])
    for item in page["items"]:
        index = fixture.native_ids.index(item["id"])
        assert {origin["id"] for origin in item["observations"]} == {
            fixture.native_ids[index],
            fixture.html_ids[index],
        }
    with sqlite3.connect(fixture.history) as connection:
        for identifier, field, value in changes:
            provenance = json.loads(
                connection.execute(
                    "SELECT provenance FROM messages WHERE id=?", (identifier,)
                ).fetchone()[0]
            )
            assert provenance["locator"][field] == value
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize(
    "change",
    [
        "namespace",
        "body",
        "clock",
        "minute_clock",
        "message_type",
        "direction",
        "explicit_zone_conflict",
        "author_native_id",
        "missing_native_unix",
    ],
)
def test_nearby_but_unproven_html_event_remains_independent(native_html_store, change):
    fixture = native_html_store
    if change in {
        "namespace",
        "message_type",
        "author_native_id",
        "missing_native_unix",
    }:
        target = (
            fixture.native_ids[-1]
            if change == "missing_native_unix"
            else fixture.html_ids[-1]
        )

        def alter(provenance):
            if change == "namespace":
                provenance["export_msgid_namespace"] = "Unknown local identifier"
                provenance["locator"]["export_msgid_namespace"] = (
                    "Unknown local identifier"
                )
            elif change == "message_type":
                provenance["locator"]["export_msgtype"] = "49"
            elif change == "author_native_id":
                provenance["sender_native_id"] = "wxid_unrelated_person"
            else:
                provenance.pop("unix_timestamp")

        _change_provenance(fixture, target, alter)
    else:
        with sqlite3.connect(fixture.history) as connection:
            if change == "body":
                original = connection.execute(
                    "SELECT text,provenance FROM messages WHERE id=?",
                    (fixture.html_ids[-1],),
                ).fetchone()
                text = original[0] + "!"
                provenance = json.loads(original[1])
                provenance["primary_text_sha256"] = hashlib.sha256(
                    text.encode()
                ).hexdigest()
                connection.execute(
                    "UPDATE messages SET text=?,provenance=? WHERE id=?",
                    (text, json.dumps(provenance), fixture.html_ids[-1]),
                )
            elif change == "direction":
                connection.execute(
                    "UPDATE messages SET direction='sent' WHERE id=?",
                    (fixture.html_ids[-1],),
                )
            else:
                value = {
                    "clock": "2020-08-12T13:46:07",
                    "minute_clock": "2020-08-12T13:46",
                    "explicit_zone_conflict": "2020-08-12T13:46:06+00:00",
                }[change]
                if change == "minute_clock":
                    value = "2020-08-12T13:46:00"
                    native_stamp = datetime.fromisoformat(value + "+08:00")
                    native_provenance = json.loads(
                        connection.execute(
                            "SELECT provenance FROM messages WHERE id=?",
                            (fixture.native_ids[-1],),
                        ).fetchone()[0]
                    )
                    native_provenance.update(
                        source_time=native_stamp.isoformat(),
                        unix_timestamp=int(native_stamp.timestamp()),
                    )
                    connection.execute(
                        "UPDATE messages SET timestamp_value=?,provenance=? WHERE id=?",
                        (
                            native_stamp.isoformat(),
                            json.dumps(native_provenance),
                            fixture.native_ids[-1],
                        ),
                    )
                provenance = json.loads(
                    connection.execute(
                        "SELECT provenance FROM messages WHERE id=?",
                        (fixture.html_ids[-1],),
                    ).fetchone()[0]
                )
                provenance["source_time"] = (
                    value if change != "minute_clock" else "2020-08-12 13:46"
                )
                connection.execute(
                    "UPDATE messages SET timestamp_value=?,provenance=? WHERE id=?",
                    (value, json.dumps(provenance), fixture.html_ids[-1]),
                )
                if change == "minute_clock":
                    connection.execute(
                        "UPDATE messages SET quality_flags=?,time_basis=? WHERE id=?",
                        (
                            '["export_timestamp_precision_minutes"]',
                            "Source clock; timezone unknown; precision: minute (seconds unavailable)",
                            fixture.html_ids[-1],
                        ),
                    )
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 7
    assert page["observation_count"] == 12
    assert all(item["observation_count"] in {1, 2} for item in page["items"])
    assert sum(item["observation_count"] == 1 for item in page["items"]) == 2


@pytest.mark.parametrize("change", ["owner", "account", "peer", "unverified"])
def test_html_scope_cannot_be_inferred_from_name_or_account_suffix(
    native_html_store, change
):
    fixture = native_html_store
    with sqlite3.connect(fixture.history) as connection:
        if change == "owner":
            connection.execute(
                "UPDATE partitions SET account_id='wxid_same_suffix_owner' WHERE id=?",
                (fixture.html_source,),
            )
        elif change == "account":
            connection.execute(
                "UPDATE partitions SET account_key='account:wxid_other' WHERE id=?",
                (fixture.html_source,),
            )
        elif change == "peer":
            connection.execute(
                "UPDATE conversations SET peer_id='wxid_other_peer' WHERE id=?",
                (fixture.html_conversation,),
            )
        else:
            connection.execute(
                "UPDATE partitions SET identity_status='Source account unconfirmed' WHERE id=?",
                (fixture.html_source,),
            )
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == page["observation_count"] == 12
    assert all(item["observation_count"] == 1 for item in page["items"])


@pytest.mark.parametrize("change", ["too_few", "reversed"])
def test_local_id_alone_is_insufficient_without_source_sequence(
    native_html_store, change
):
    fixture = native_html_store
    with sqlite3.connect(fixture.history) as connection:
        if change == "too_few":
            for message in fixture.html_ids[2:]:
                connection.execute("DELETE FROM messages WHERE id=?", (message,))
            expected = 8
        else:
            for index, message in enumerate(fixture.html_ids, 1):
                connection.execute(
                    "UPDATE messages SET ordinal=? WHERE id=?", (7 - index, message)
                )
            expected = 12
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == page["observation_count"] == expected


@pytest.mark.parametrize("side", ["html", "native_conflicting_server"])
def test_duplicate_candidate_does_not_discard_a_repeated_occurrence(
    native_html_store, side
):
    fixture = native_html_store
    target = fixture.html_ids[-1] if side == "html" else fixture.native_ids[-1]
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (target,)
            ).fetchone()
        )
        row["id"] = hashlib.sha256((side + target).encode()).hexdigest()
        row["ordinal"] += 50
        if side == "native_conflicting_server":
            provenance = json.loads(row["provenance"])
            provenance["server_id"] = "7999999999999999999"
            row["provenance"] = json.dumps(provenance)
        _insert_message(connection, row)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 8
    assert page["observation_count"] == 13
    assert sum(item["observation_count"] == 1 for item in page["items"]) == 3


def test_short_native_snapshot_cannot_hide_an_ambiguous_server_event(native_html_store):
    fixture = native_html_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.native_ids[-1],)
            ).fetchone()
        )
        metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?",
                (fixture.native_snapshot,),
            ).fetchone()[0]
        )
        metadata.update(
            source_label="iPhone archive",
            source_file="Synthetic independent one-event checkpoint",
        )
        snapshot = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            (
                "synthetic-short-native-snapshot",
                fixture.native_source,
                json.dumps(metadata),
            ),
        ).lastrowid
        row["id"] = hashlib.sha256(b"short-native-conflicting-server-event").hexdigest()
        row["ordinal"] += 50
        provenance = json.loads(row["provenance"])
        provenance.update(
            source_snapshot=snapshot,
            server_id="7999999999999999999",
            record_id="short-native-conflicting-server-event",
        )
        provenance["locator"]["local_id"] = 906
        row["provenance"] = json.dumps(provenance)
        _insert_message(connection, row)
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 8
    assert page["observation_count"] == 13
    independent = {
        item["id"] for item in page["items"] if item["observation_count"] == 1
    }
    assert independent == {
        fixture.native_ids[-1],
        fixture.html_ids[-1],
        row["id"],
    }
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_complete_cache_reproof_preserves_references_and_existing_links(
    native_html_store, monkeypatch
):
    fixture = native_html_store
    for index, identifier in enumerate(fixture.html_ids, 1):
        _change_provenance(
            fixture,
            identifier,
            lambda provenance, index=index: provenance["locator"].update(
                export_msgid=str(200 + index)
            ),
        )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    assert analysis.query_contact_analysis(fixture.store, CID)["status"] == "ready"
    cache = analysis.contact_analysis_cache_path(fixture.store, CID)
    with monkeypatch.context() as previous:
        previous.setattr(analysis, "INDEX_VERSION", "v1.10.0-codex.0")
        old_fingerprint = analysis._sources(fixture.store)[2]
    with closing(sqlite3.connect(cache)) as connection, connection:
        references = connection.execute("SELECT * FROM refs ORDER BY id").fetchall()
        connection.execute("DELETE FROM statement_members")
        connection.execute("DELETE FROM statement_groups")
        groups = {
            reference[0]: _identifier("retained-source-statement", reference[0])
            for reference in references
        }
        groups[fixture.html_ids[0]] = groups[fixture.native_ids[0]]
        for reference in references:
            group = groups[reference[0]]
            representative = (
                fixture.native_ids[0]
                if reference[0] == fixture.html_ids[0]
                else reference[0]
            )
            connection.execute(
                "INSERT OR IGNORE INTO statement_groups VALUES(?,?,?)",
                (group, representative, "retained-existing-conversation"),
            )
            connection.execute(
                "INSERT INTO statement_members VALUES(?,?)", (reference[0], group)
            )
        connection.executemany(
            "UPDATE info SET value=? WHERE key=?",
            [("v1.10.0-codex.0", "version"), (old_fingerprint, "fingerprint")],
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM statement_groups").fetchone()[0]
            == 11
        )

    def no_source_author_scan(*_args, **_kwargs):
        raise AssertionError(
            "A complete retained contact index must not rescan authors."
        )

    for method in (
        "_candidate_batches",
        "_candidate_forwarded_batches",
        "_candidate_native_batches",
        "_group_statements",
    ):
        monkeypatch.setattr(analysis, method, no_source_author_scan)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["status"] == "ready"
    assert page["total_count"] == 6
    assert page["observation_count"] == 12
    assert {item["id"] for item in page["items"]} == set(fixture.native_ids)
    with closing(sqlite3.connect(cache)) as connection, connection:
        assert (
            connection.execute("SELECT * FROM refs ORDER BY id").fetchall()
            == references
        )
        members = dict(
            connection.execute(
                "SELECT reference_id,statement_id FROM statement_members"
            )
        )
        assert len(members) == 12
        assert members[fixture.html_ids[0]] == members[fixture.native_ids[0]]
        assert members[fixture.native_ids[0]] == groups[fixture.native_ids[0]]
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_unique_html_and_native_records_survive_the_union(native_html_store):
    fixture = native_html_store
    extra_ids = set()
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        for side, message_id in (
            ("html", fixture.html_ids[-1]),
            ("native", fixture.native_ids[-1]),
        ):
            row = dict(
                connection.execute(
                    "SELECT * FROM messages WHERE id=?", (message_id,)
                ).fetchone()
            )
            row["id"] = hashlib.sha256((side + "-unique").encode()).hexdigest()
            row["ordinal"] += 100
            row["text"] = f"Synthetic {side}-only text"
            row["search_text"] = row["text"].casefold()
            provenance = json.loads(row["provenance"])
            provenance["record_id"] = side + "-unique"
            if side == "html":
                provenance["locator"]["export_msgid"] = "900"
                provenance["primary_text_sha256"] = hashlib.sha256(
                    row["text"].encode()
                ).hexdigest()
            else:
                provenance["locator"]["local_id"] = 900
                provenance["server_id"] = "7999999999999999999"
            row["provenance"] = json.dumps(provenance)
            _insert_message(connection, row)
            extra_ids.add(row["id"])
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 8
    assert page["observation_count"] == 14
    singles = {item["id"] for item in page["items"] if item["observation_count"] == 1}
    assert singles == extra_ids
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


def test_native_source_is_resolved_from_snapshot_inside_html_container(
    native_html_store,
):
    fixture = native_html_store
    with sqlite3.connect(fixture.history) as connection:
        for message_id in fixture.native_ids:
            connection.execute(
                "UPDATE messages SET conversation_id=? WHERE id=?",
                (fixture.html_conversation, message_id),
            )
    before = fixture.history.read_bytes(), fixture.registry.read_bytes()
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 6
    assert page["observation_count"] == 12
    assert all(item["source_label"] == "iPad archive" for item in page["items"])
    assert all(item["source_id"] == fixture.native_source for item in page["items"])
    assert all(
        item["conversation_id"] == fixture.html_conversation for item in page["items"]
    )
    assert {item["anchor_id"] for item in page["items"]} == {
        "wechat_message_" + message_id for message_id in fixture.native_ids
    }
    assert before == (fixture.history.read_bytes(), fixture.registry.read_bytes())


@pytest.mark.parametrize(
    "invalid", ["owner", "hash", "hash_verified", "boolean_snapshot"]
)
def test_ipad_label_without_valid_snapshot_owner_proof_has_no_priority(
    preferred_native_store, invalid
):
    fixture = preferred_native_store
    with sqlite3.connect(fixture.history) as connection:
        provenance = json.loads(
            connection.execute(
                "SELECT provenance FROM messages WHERE id=?", (fixture.preferred,)
            ).fetchone()[0]
        )
        snapshot = provenance["source_snapshot"]
        if invalid == "boolean_snapshot":
            assert snapshot == 1
            provenance["source_snapshot"] = True
            connection.execute(
                "UPDATE messages SET provenance=? WHERE id=?",
                (json.dumps(provenance), fixture.preferred),
            )
        else:
            metadata = json.loads(
                connection.execute(
                    "SELECT metadata FROM source_snapshots WHERE id=?", (snapshot,)
                ).fetchone()[0]
            )
            key, value = {
                "owner": ("source_account_id", "wxid_other_owner"),
                "hash": ("account_hash", "0" * 32),
                "hash_verified": ("account_hash_verified", False),
            }[invalid]
            metadata[key] = value
            connection.execute(
                "UPDATE source_snapshots SET metadata=? WHERE id=?",
                (json.dumps(metadata), snapshot),
            )
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 1
    assert page["observation_count"] == 3
    assert page["items"][0]["id"] == fixture.other_same_owner
    assert page["items"][0]["source_label"] == "iPhone archive"


def test_raw_native_explicit_clock_conflict_cannot_corroborate_html(native_html_store):
    fixture = native_html_store
    _change_provenance(
        fixture,
        fixture.native_ids[-1],
        lambda provenance: provenance.update(source_time="2020-08-12T13:46:06+00:00"),
    )
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 7
    assert page["observation_count"] == 12
    assert sum(item["observation_count"] == 1 for item in page["items"]) == 2


def test_html_precedence_does_not_hide_native_to_native_clock_conflict(
    native_html_store,
):
    fixture = native_html_store
    with sqlite3.connect(fixture.history) as connection:
        connection.row_factory = sqlite3.Row
        row = dict(
            connection.execute(
                "SELECT * FROM messages WHERE id=?", (fixture.native_ids[-1],)
            ).fetchone()
        )
        original_metadata = json.loads(
            connection.execute(
                "SELECT metadata FROM source_snapshots WHERE id=?",
                (fixture.native_snapshot,),
            ).fetchone()[0]
        )
        original_metadata.update(
            source_label="iPhone archive",
            source_file="Synthetic conflicting iPhone checkpoint",
        )
        snapshot = connection.execute(
            "INSERT INTO source_snapshots(snapshot_id,source_id,metadata) VALUES(?,?,?)",
            (
                "synthetic-conflicting-native-clock",
                fixture.native_source,
                json.dumps(original_metadata),
            ),
        ).lastrowid
        row["id"] = hashlib.sha256(b"synthetic-conflicting-native-clock").hexdigest()
        stamp = datetime.fromisoformat(row["timestamp_value"]) + timedelta(minutes=1)
        row["timestamp_value"] = stamp.isoformat()
        row["time_sort"] = stamp.timestamp()
        provenance = json.loads(row["provenance"])
        provenance.update(
            source_snapshot=snapshot,
            source_time=stamp.isoformat(),
            unix_timestamp=int(stamp.timestamp()),
        )
        row["provenance"] = json.dumps(provenance)
        _insert_message(connection, row)
    page = analysis.query_contact_analysis(fixture.store, CID)
    assert page["total_count"] == 6
    assert page["observation_count"] == 13
    item = next(item for item in page["items"] if item["id"] == fixture.native_ids[-1])
    assert item["source_label"] == "iPad archive"
    assert item["observation_count"] == 3
    assert item["time_conflict"]
    native_clocks = {
        origin["timestamp_value"]
        for origin in item["observations"]
        if origin["source_id"] == fixture.native_source
    }
    assert len(native_clocks) == 2
