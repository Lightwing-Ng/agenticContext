"""Preserved native metadata staging keeps source scope, clocks, and raw data.

Code version: v1.3.0-codex.0
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.wechat_archive import (
    _SCHEMA,
    _file_identity,
    _identifier,
    _time_fields,
    wechat_archive_path,
)
from app.core.wechat_native_resource_revalidation import (
    _content_digest,
    _verified_coverage,
)
from app.core.wechat_native_resource_receipts import WITNESS_SCHEMA, _witness
from app.core import wechat_native_resource_scope as scope


OWNER = "wxid_scope_owner"
ACCOUNT_HASH = hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest()
SOURCE = "ios-" + ACCOUNT_HASH
SNAPSHOT = "ipad-pro-synthetic-snapshot"
RELATIVE = f"Documents/{ACCOUNT_HASH}/DB/message_1.sqlite"
FILE_ID = hashlib.sha1(
    ("AppDomain-com.tencent.xin-" + RELATIVE).encode(), usedforsecurity=False
).hexdigest()
BASE_TIME = 1700000000


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _times(epoch):
    return _time_fields(
        {"sent_at_utc_iso": datetime.fromtimestamp(epoch, UTC).isoformat()}
    )


@pytest.fixture
def preserved_native_scope(tmp_path):
    workstation = tmp_path / "preserved"
    database = workstation / "inputs" / RELATIVE
    database.parent.mkdir(parents=True)
    peers = ["123@chatroom", "456@chatroom"]
    tables = [
        "Chat_" + hashlib.md5(peer.encode(), usedforsecurity=False).hexdigest()
        for peer in peers
    ]
    raw_rows = [
        (1, 10001, BASE_TIME + 1, 1, 1, "Source anchor A", 0),
        (2, 10002, BASE_TIME + 2, 1, 1, "Source anchor B", 0),
        (
            3,
            10003,
            BASE_TIME + 3,
            49,
            1,
            "<msg><appmsg><type>5</type><title>Decoded card</title></appmsg></msg>",
            0,
        ),
        (
            4,
            10004,
            BASE_TIME + 4,
            3,
            1,
            b"Opaque image metadata must not become text",
            0,
        ),
        (5, 10005, BASE_TIME + 5, 47, 0, b"Opaque sticker metadata", 0),
        (
            6,
            10006,
            BASE_TIME + 6,
            49,
            1,
            "wxid_source_author:\n<msg><appmsg><type>6</type><title>source-plan.txt</title><appattach><fileext>txt</fileext><totallen>128</totallen></appattach></appmsg></msg>",
            0,
        ),
        (
            7,
            10007,
            BASE_TIME + 7,
            49,
            1,
            "<msg><appmsg><type>5</type><title>Card source label</title></appmsg></msg>",
            0,
        ),
        (8, 10008, BASE_TIME + 8, 34, 1, b"Opaque voice metadata", 0),
        (9, 10009, BASE_TIME + 9, 43, 1, b"Opaque video metadata", 0),
    ]
    with sqlite3.connect(database) as native:
        for table in tables:
            native.execute(
                f'CREATE TABLE "{table}" (MesLocalID INTEGER,MesSvrID INTEGER,'
                "CreateTime INTEGER,Type INTEGER,Des INTEGER,Message BLOB,WCDB_CT_Message INTEGER)"
            )
            native.executemany(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)', raw_rows)
    digest = _sha(database)
    metadata = {
        "source_id": SOURCE,
        "source_account_id": OWNER,
        "account_hash": ACCOUNT_HASH,
        "account_hash_verified": True,
        "text_only": True,
        "snapshot_id": SNAPSHOT,
        "source_inventory_file": "sources.json",
        "identity_evidence": {
            "kind": "native_login_info_with_directory_md5",
            "directory_hash": ACCOUNT_HASH,
            "directory_hash_algorithm": "MD5-UTF8",
            "login_info_sha256": hashlib.sha256(b"Synthetic login info").hexdigest(),
        },
    }
    sources = workstation / "sources.json"
    sources.write_text(_json([metadata]))
    receipt = workstation / "preservation-report-v1.0.0.json"
    receipt_object = {
        "snapshot_id": SNAPSHOT,
        "sources_sha256": _sha(sources),
        "status": "complete",
        "all_snapshot_hashes_equal_source_hashes": True,
        "selected_files": [
            {
                "domain": "AppDomain-com.tencent.xin",
                "relative_path": RELATIVE,
                "input_relative_path": "inputs/" + RELATIVE,
                "bytes": database.stat().st_size,
                "sha256": digest,
                "snapshot_sha256": digest,
                "source_sha256": digest,
                "file_id": FILE_ID,
                "copy_method": "APFSclone",
            }
        ],
    }
    receipt.write_text(_json(receipt_object))
    store = tmp_path / "store"
    history = wechat_archive_path(store)
    history.parent.mkdir(parents=True)
    conversations = []
    primary_ids = {}
    with sqlite3.connect(history) as primary:
        primary.executescript(_SCHEMA)
        primary.execute(
            "CREATE TABLE source_snapshots (id INTEGER PRIMARY KEY,snapshot_id TEXT NOT NULL,source_id TEXT NOT NULL,metadata TEXT NOT NULL)"
        )
        primary.execute(
            "INSERT INTO source_snapshots VALUES(26,?,?,?)",
            (SNAPSHOT, SOURCE, _json(metadata)),
        )
        primary.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                SOURCE,
                "account:" + OWNER,
                OWNER,
                "Synthetic owner",
                "Verified archive ID",
                "iPad archive",
                "Synthetic preservation",
                "",
            ),
        )
        for peer, table in zip(peers, tables, strict=True):
            cid = "wechat:" + _identifier(SOURCE, peer)
            conversations.append(cid)
            primary.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    SOURCE,
                    "account:" + OWNER,
                    peer,
                    "Synthetic room",
                    "group",
                    3,
                    _times(BASE_TIME + 1)[0],
                    _times(BASE_TIME + 3)[0],
                    _times(BASE_TIME + 1)[1],
                    _times(BASE_TIME + 3)[1],
                    "Decoded card",
                ),
            )
            for local, server, epoch, typ, des, _payload, _marker in raw_rows[:3]:
                mid = _identifier("primary", peer, str(local))
                primary_ids[(cid, local)] = mid
                body = {1: "Source anchor A", 2: "Source anchor B", 3: "Decoded card"}[
                    local
                ]
                proof = {
                    "record_id": mid,
                    "source_snapshot": 26,
                    "message_type": typ,
                    "text_extraction_kind": "plain_text" if typ == 1 else "text_appmsg",
                    "server_id": str(server),
                    "unix_timestamp": epoch,
                    "locator": {
                        "database": "message_1.sqlite",
                        "database_relative_path": RELATIVE,
                        "database_sha256": digest,
                        "file_id": FILE_ID,
                        "native_direction": des,
                        "table": table,
                        "local_id": local,
                        "row_id": local,
                    },
                }
                primary.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        mid,
                        cid,
                        local,
                        "wxid_source_author",
                        "Source author",
                        "received",
                        body,
                        *_times(epoch),
                        _json(proof),
                        "[]",
                        body.lower(),
                    ),
                )
        primary.execute(
            "INSERT INTO archive_info VALUES('manifest',?)", (_json({"messages": 6}),)
        )
    return SimpleNamespace(
        store=store,
        history=history,
        database=database,
        receipt=receipt,
        receipt_object=receipt_object,
        sources=sources,
        metadata=metadata,
        tables=tables,
        conversations=conversations,
        primary_ids=primary_ids,
        raw_rows=raw_rows,
        digest=digest,
        tmp_path=tmp_path,
    )


def _stage(fixture, *, number=0, candidate=None):
    output = candidate or fixture.tmp_path / f"scope-{number}.sqlite3"
    report = scope.stage_preserved_native_resource_scope(
        fixture.store,
        output,
        conversation_id=fixture.conversations[number],
        source_snapshot_id=26,
        database_path=fixture.database,
        preservation_report_path=fixture.receipt,
        sources_path=fixture.sources,
    )
    return output, report


def _read(path, sql):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql)]


def _reseal_synthetic_database(fixture):
    digest = _sha(fixture.database)
    value = json.loads(fixture.receipt.read_text())
    value["selected_files"][0].update(
        sha256=digest,
        snapshot_sha256=digest,
        source_sha256=digest,
        bytes=fixture.database.stat().st_size,
    )
    fixture.receipt.write_text(_json(value))
    with sqlite3.connect(fixture.history) as primary:
        for mid, encoded in primary.execute("SELECT id,provenance FROM messages"):
            proof = json.loads(encoded)
            proof["locator"]["database_sha256"] = digest
            primary.execute(
                "UPDATE messages SET provenance=? WHERE id=?", (_json(proof), mid)
            )
    fixture.digest = digest


def _witnessed_prior(fixture, *, name="witnessed-prior", metadata_override=None):
    """Build a prior whose witnesses pass the unmodified production verifier."""
    canonical = fixture.history.with_name("resource_references.sqlite3")
    if not canonical.exists():
        with sqlite3.connect(canonical) as connection:
            connection.executescript(_SCHEMA)
    candidate, _ = _stage(
        fixture, number=1, candidate=fixture.tmp_path / f"{name}.sqlite3"
    )
    with sqlite3.connect(fixture.history) as primary:
        primary.row_factory = sqlite3.Row
        metadata = primary.execute(
            "SELECT metadata FROM source_snapshots WHERE id=26"
        ).fetchone()[0]
        receipt = {
            "source_snapshot_id": 26,
            "source_id": SOURCE,
            "snapshot_id": SNAPSHOT,
            "metadata_sha256": hashlib.sha256(metadata.encode()).hexdigest(),
            "database_relative_path": RELATIVE,
            "database_sha256": fixture.digest,
            "database_bytes": fixture.database.stat().st_size,
            "file_id": FILE_ID,
            "manifest_sha256": _sha(fixture.receipt),
            "manifest_path": str(fixture.receipt),
            "original_source_inventory_file": "sources.json",
        }
        extra = {
            "supplemental_metadata": "1",
            "canonical_file_identity": _json(_file_identity(canonical)),
            "supplement_source_receipts": _json([receipt]),
        }
        if metadata_override:
            extra.update(metadata_override)
        retained_receipt = json.loads(extra["supplement_source_receipts"])[0]
        conversation = primary.execute(
            "SELECT * FROM conversations WHERE id=?", (fixture.conversations[1],)
        ).fetchone()
        native_row = primary.execute(
            "SELECT id,provenance,direction FROM messages WHERE id=?",
            (fixture.primary_ids[(fixture.conversations[1], 1)],),
        ).fetchone()
        witness = _witness(primary, conversation, retained_receipt, native_row)
    with sqlite3.connect(candidate) as connection:
        connection.executescript(WITNESS_SCHEMA)
        connection.execute(
            "INSERT INTO revalidation_native_witnesses VALUES(?,?,?,?,?,?,?,?)",
            witness,
        )
        connection.executemany(
            "INSERT OR REPLACE INTO archive_info VALUES(?,?)", extra.items()
        )
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='content_sha256'",
            (_content_digest(connection),),
        )
    assert _verified_coverage(fixture.history, candidate) == (fixture.conversations[1],)
    return candidate, witness, extra


def test_staged_scope_keeps_typed_metadata_exact_clocks_and_original_inputs(
    preserved_native_scope,
):
    f = preserved_native_scope
    before = {p: p.read_bytes() for p in (f.history, f.database, f.receipt, f.sources)}
    candidate, report = _stage(f)
    rows = _read(candidate, "SELECT * FROM messages ORDER BY ordinal")
    assert report["messages"] == len(rows) == 6
    assert not report["published"]
    assert len(report["excluded_existing_events"]) == 1
    assert report["kinds"] == {
        "image": 1,
        "sticker": 1,
        "file": 1,
        "app": 1,
        "audio": 1,
        "video": 1,
    }
    assert {row["ordinal"] for row in rows} == set(range(4, 10))
    for row in rows:
        proof = json.loads(row["provenance"])
        assert proof["unix_timestamp"] == BASE_TIME + row["ordinal"]
        assert tuple(
            row[key]
            for key in ("time_sort", "timestamp_value", "timestamp_label", "time_basis")
        ) == _times(BASE_TIME + row["ordinal"])
        assert proof["resource_reference"]["account_id"] == OWNER
        assert proof["resource_reference"]["peer_id"] == "123@chatroom"
    file_row = next(
        row
        for row in rows
        if json.loads(row["provenance"])["resource_reference"]["kind"] == "file"
    )
    file_reference = json.loads(file_row["provenance"])["resource_reference"]
    assert file_reference["filename"] == "source-plan.txt"
    assert file_reference["file_extension"] == "txt"
    assert file_reference["byte_size"] == 128
    candidate_bytes = candidate.read_bytes()
    assert b"Opaque image metadata must not become text" not in candidate_bytes
    assert b"Opaque sticker metadata" not in candidate_bytes
    assert b"Opaque voice metadata" not in candidate_bytes
    assert b"Opaque video metadata" not in candidate_bytes
    assert {p: p.read_bytes() for p in before} == before
    assert not f.history.with_name("resource_revalidated.sqlite3").exists()
    info = {
        row["key"]: row["value"]
        for row in _read(candidate, "SELECT * FROM archive_info")
    }
    with sqlite3.connect(candidate) as connection:
        assert info["content_sha256"] == _content_digest(connection)


@pytest.mark.parametrize(
    "defect",
    [
        "db_sha",
        "bytes",
        "domain",
        "relative",
        "input_relative",
        "duplicate",
        "sources_digest",
    ],
)
def test_source_receipt_mismatch_never_creates_a_candidate(
    preserved_native_scope, defect
):
    f = preserved_native_scope
    value = json.loads(f.receipt.read_text())
    entry = value["selected_files"][0]
    if defect == "db_sha":
        entry.update(sha256="f" * 64, source_sha256="f" * 64, snapshot_sha256="f" * 64)
    elif defect == "bytes":
        entry["bytes"] += 1
    elif defect == "domain":
        entry["domain"] = "AppDomain-other"
    elif defect == "relative":
        entry["relative_path"] = "Documents/other/DB/message_1.sqlite"
    elif defect == "input_relative":
        entry["input_relative_path"] = "../outside.sqlite"
    elif defect == "duplicate":
        value["selected_files"].append(dict(entry))
    else:
        value["sources_sha256"] = "f" * 64
    f.receipt.write_text(_json(value))
    before = f.history.read_bytes(), f.database.read_bytes()
    with pytest.raises(ValueError):
        _stage(f)
    assert not (f.tmp_path / "scope-0.sqlite3").exists()
    assert (f.history.read_bytes(), f.database.read_bytes()) == before


@pytest.mark.parametrize("defect", ["owner", "hash", "verified", "duplicate"])
def test_declared_source_identity_must_agree_with_retained_account(
    preserved_native_scope, defect
):
    f = preserved_native_scope
    values = json.loads(f.sources.read_text())
    if defect == "owner":
        values[0]["source_account_id"] = "wxid_other"
    elif defect == "hash":
        values[0]["account_hash"] = "f" * 32
    elif defect == "verified":
        values[0]["account_hash_verified"] = False
    else:
        values.append(dict(values[0]))
    f.sources.write_text(_json(values))
    receipt = json.loads(f.receipt.read_text())
    receipt["sources_sha256"] = _sha(f.sources)
    f.receipt.write_text(_json(receipt))
    with pytest.raises(ValueError):
        _stage(f)
    assert not (f.tmp_path / "scope-0.sqlite3").exists()


def test_current_snapshot_allows_only_added_cross_validation(preserved_native_scope):
    f = preserved_native_scope
    metadata = dict(f.metadata, cross_validation={"observed": "Current readback"})
    with sqlite3.connect(f.history) as primary:
        primary.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=26", (_json(metadata),)
        )
    candidate, report = _stage(f)
    assert report["messages"] == 6
    assert len(_read(candidate, "SELECT * FROM messages")) == 6


def test_undeclared_current_source_field_refuses_projection(preserved_native_scope):
    f = preserved_native_scope
    metadata = dict(f.metadata, unexpected_binding="Not preserved")
    with sqlite3.connect(f.history) as primary:
        primary.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=26", (_json(metadata),)
        )
    with pytest.raises(ValueError):
        _stage(f)
    assert not (f.tmp_path / "scope-0.sqlite3").exists()


def test_conflicting_primary_source_time_refuses_projection(preserved_native_scope):
    f = preserved_native_scope
    with sqlite3.connect(f.history) as primary:
        primary.execute(
            "UPDATE messages SET timestamp_value='2099-01-01T00:00:00+08:00' WHERE id=?",
            (f.primary_ids[(f.conversations[0], 1)],),
        )
    with pytest.raises(ValueError):
        _stage(f)


@pytest.mark.parametrize("defect", ["server", "epoch", "direction", "type"])
def test_primary_anchor_must_match_the_exact_native_event(
    preserved_native_scope, defect
):
    f = preserved_native_scope
    mid = f.primary_ids[(f.conversations[0], 1)]
    with sqlite3.connect(f.history) as primary:
        proof = json.loads(
            primary.execute(
                "SELECT provenance FROM messages WHERE id=?", (mid,)
            ).fetchone()[0]
        )
        if defect == "server":
            proof["server_id"] = "999999"
        elif defect == "epoch":
            proof["unix_timestamp"] += 1
        elif defect == "type":
            proof["message_type"] = 49
        else:
            primary.execute("UPDATE messages SET direction='sent' WHERE id=?", (mid,))
        primary.execute(
            "UPDATE messages SET provenance=? WHERE id=?", (_json(proof), mid)
        )
    with pytest.raises(ValueError):
        _stage(f)
    assert not (f.tmp_path / "scope-0.sqlite3").exists()


def test_staging_requires_multiple_original_clock_anchors(preserved_native_scope):
    f = preserved_native_scope
    with sqlite3.connect(f.history) as primary:
        primary.execute(
            "DELETE FROM messages WHERE conversation_id=? AND ordinal IN (1,2)",
            (f.conversations[0],),
        )
    with pytest.raises(ValueError):
        _stage(f)


def test_ambiguous_native_local_row_refuses_projection(preserved_native_scope):
    f = preserved_native_scope
    with sqlite3.connect(f.database) as native:
        native.execute(
            f'INSERT INTO "{f.tables[0]}" VALUES(?,?,?,?,?,?,?)',
            (4, 10004, BASE_TIME + 4, 3, 1, b"Conflicting duplicate source row", 0),
        )
    _reseal_synthetic_database(f)
    with pytest.raises(ValueError):
        _stage(f)
    assert not (f.tmp_path / "scope-0.sqlite3").exists()


def test_same_clock_metadata_with_distinct_native_rows_stays_distinct(
    preserved_native_scope,
):
    f = preserved_native_scope
    with sqlite3.connect(f.database) as native:
        native.execute(
            f'INSERT INTO "{f.tables[0]}" VALUES(?,?,?,?,?,?,?)',
            (10, 10010, BASE_TIME + 4, 3, 1, b"Distinct native occurrence", 0),
        )
    _reseal_synthetic_database(f)
    candidate, report = _stage(f)
    rows = _read(candidate, "SELECT * FROM messages ORDER BY ordinal")
    images = [
        row
        for row in rows
        if json.loads(row["provenance"])["resource_reference"]["kind"] == "image"
    ]
    assert report["messages"] == 7
    assert len(images) == 2
    assert {row["ordinal"] for row in images} == {4, 10}
    assert images[0]["time_sort"] == images[1]["time_sort"]


def test_unsupported_native_direction_stays_unknown(preserved_native_scope):
    f = preserved_native_scope
    with sqlite3.connect(f.database) as native:
        native.execute(f'UPDATE "{f.tables[0]}" SET Des=2 WHERE MesLocalID=4')
    _reseal_synthetic_database(f)
    candidate, _ = _stage(f)
    row = _read(candidate, "SELECT * FROM messages WHERE ordinal=4")[0]
    assert row["direction"] == "unknown"
    assert row["sender_id"] is None
    assert row["sender_label"] == "Unknown sender"
    assert json.loads(row["provenance"])["locator"]["native_direction"] == 2


def test_invalid_utf8_sender_prefix_is_not_repaired_into_identity(
    preserved_native_scope,
):
    f = preserved_native_scope
    with sqlite3.connect(f.database) as native:
        native.execute(
            f'UPDATE "{f.tables[0]}" SET Message=? WHERE MesLocalID=4',
            (b"wxid_spoof_\xff:\nNot an asserted sender",),
        )
    _reseal_synthetic_database(f)
    candidate, _ = _stage(f)
    row = _read(candidate, "SELECT * FROM messages WHERE ordinal=4")[0]
    assert row["sender_id"] is None
    assert row["sender_label"] == "Unknown sender"


def test_sender_prefix_survives_an_unreadable_truncated_multibyte_body(
    preserved_native_scope,
):
    f = preserved_native_scope
    body = "汉".encode() * 300
    with sqlite3.connect(f.database) as native:
        native.execute(
            f'UPDATE "{f.tables[0]}" SET Message=? WHERE MesLocalID=4',
            (b"wxid_source_author:\n" + body,),
        )
    _reseal_synthetic_database(f)
    candidate, _ = _stage(f)
    row = _read(candidate, "SELECT * FROM messages WHERE ordinal=4")[0]
    assert row["sender_id"] == "wxid_source_author"
    assert row["sender_label"] == "wxid_source_author"
    assert "汉".encode() not in candidate.read_bytes()


@pytest.mark.parametrize("app_info_location", ["root", "appmsg"])
def test_app_card_keeps_literal_title_without_active_or_secret_metadata(
    preserved_native_scope, app_info_location
):
    f = preserved_native_scope
    app_info = "<appinfo><appname>Source application</appname></appinfo>"
    payload = (
        "<msg><appmsg><type>5</type><title>Source &amp; label</title>"
        "<sourcedisplayname>Literal publisher</sourcedisplayname>"
        "<url>https://example.invalid/private-source</url>"
        "<aeskey>secret-aes-value</aeskey><cdnurl>secret-cdn-value</cdnurl>"
        + (app_info if app_info_location == "appmsg" else "")
        + "</appmsg>"
        + (app_info if app_info_location == "root" else "")
        + "</msg>"
    )
    with sqlite3.connect(f.database) as native:
        native.execute(
            f'UPDATE "{f.tables[0]}" SET Message=? WHERE MesLocalID=7', (payload,)
        )
    _reseal_synthetic_database(f)
    candidate, _ = _stage(f)
    row = _read(candidate, "SELECT * FROM messages WHERE ordinal=7")[0]
    reference = json.loads(row["provenance"])["resource_reference"]
    assert reference["safe_title"] == "Source & label"
    assert reference["app_name"] == "Source application"
    assert reference["app_name_source"] == (
        "appmsg/appinfo/appname" if app_info_location == "appmsg" else "appinfo/appname"
    )
    assert reference["publisher_name"] == "Literal publisher"
    assert reference["publisher_name_source"] == "appmsg/sourcedisplayname"
    assert reference["kind"] == "app"
    content = candidate.read_bytes()
    for private in (
        b"https://example.invalid/private-source",
        b"secret-aes-value",
        b"secret-cdn-value",
    ):
        assert private not in content


def test_scoped_app_metadata_requires_a_bounded_supported_zstd_frame(
    preserved_native_scope,
):
    zstd = pytest.importorskip("compression.zstd")
    f = preserved_native_scope
    payload = (
        b"wxid_compressed_author:\n<msg><appmsg><type>51</type>"
        b"<finderFeed><desc>Original description</desc>"
        b"<nickname>Original creator</nickname></finderFeed></appmsg></msg>"
    )
    frame = zstd.compress(payload)
    dictionary = zstd.train_dict(
        [payload + f" Sample {number}".encode() for number in range(200)], 1024
    )
    with sqlite3.connect(f.database) as native:
        native.executemany(
            f'UPDATE "{f.tables[0]}" SET Message=?,WCDB_CT_Message=?,Type=?,Des=1 '
            "WHERE MesLocalID=?",
            [
                (frame, 4, 43, 4),
                (frame + b"trailing", 4, 49, 5),
                (zstd.compress(payload, zstd_dict=dictionary), 2, 49, 6),
                (frame, 4, 49, 7),
                (frame, 6, 49, 8),
                (zstd.compress(payload + b" " * (256 * 1024)), 5, 49, 9),
            ],
        )
    _reseal_synthetic_database(f)

    candidate, report = _stage(f)

    assert report["media_payload_files_read"] == 0
    rows = {row["ordinal"]: row for row in _read(candidate, "SELECT * FROM messages")}
    assert rows[7]["sender_id"] == "wxid_compressed_author"
    proof = json.loads(rows[7]["provenance"])
    reference = proof["resource_reference"]
    assert proof["native_appmsg_subtype"] == reference["app_subtype"] == "51"
    assert reference["channels_description"] == "Original description"
    assert reference["channels_nickname"] == "Original creator"
    for ordinal in (4, 5, 6, 8, 9):
        assert rows[ordinal]["sender_id"] is None
        proof = json.loads(rows[ordinal]["provenance"])
        reference = proof["resource_reference"]
        assert reference["kind"] == ("video" if ordinal == 4 else "app")
        assert "native_appmsg_subtype" not in proof
        assert {"app_subtype", "channels_description", "channels_nickname"}.isdisjoint(
            reference
        )


def test_conflicting_app_names_do_not_choose_a_location(preserved_native_scope):
    f = preserved_native_scope
    payload = (
        "<msg><appmsg><type>5</type><title>Unambiguous title</title>"
        "<appinfo><appname>First source</appname></appinfo></appmsg>"
        "<appinfo><appname>Second source</appname></appinfo></msg>"
    )
    with sqlite3.connect(f.database) as native:
        native.execute(
            f'UPDATE "{f.tables[0]}" SET Message=? WHERE MesLocalID=7', (payload,)
        )
    _reseal_synthetic_database(f)
    candidate, _ = _stage(f)
    row = _read(candidate, "SELECT * FROM messages WHERE ordinal=7")[0]
    reference = json.loads(row["provenance"])["resource_reference"]
    assert reference["safe_title"] == "Unambiguous title"
    assert not reference.get("app_name")


@pytest.mark.parametrize(
    "publisher",
    [
        "<sourcedisplayname>First</sourcedisplayname>"
        "<sourcedisplayname>Second</sourcedisplayname>",
        "<sourcedisplayname><b>Nested publisher</b></sourcedisplayname>",
        "<sourcedisplayname> </sourcedisplayname>",
        "<url>https://example.invalid/publisher-name</url>",
        "<refermsg><sourcedisplayname>Quoted publisher</sourcedisplayname></refermsg>",
    ],
)
def test_app_publisher_requires_one_direct_literal_source_field(publisher):
    labels = scope._app_labels(
        "<msg><appmsg><type>5</type><title>Literal title</title>"
        + publisher
        + "</appmsg><appinfo><appname>Third-party application</appname></appinfo></msg>",
        False,
        False,
    )
    assert labels["safe_title"] == "Literal title"
    assert labels["app_name"] == "Third-party application"
    assert labels["app_name_source"] == "appinfo/appname"
    assert "publisher_name" not in labels
    assert "publisher_name_source" not in labels


@pytest.mark.parametrize("separator", [":\n", ":\r\n"])
def test_scoped_group_app_labels_keep_both_native_line_separators(separator):
    labels = scope._app_labels(
        "wxid_source_author"
        + separator
        + "<msg><appmsg><type>5</type><title>Source title</title>"
        "<sourcedisplayname>Source publisher</sourcedisplayname></appmsg></msg>",
        True,
        True,
    )
    assert labels["safe_title"] == "Source title"
    assert labels["publisher_name"] == "Source publisher"
    assert labels["publisher_name_source"] == "appmsg/sourcedisplayname"


@pytest.mark.parametrize("separator", [b":\n", b":\r\n"])
def test_bounded_binary_sender_prefix_does_not_decode_an_unread_tail(separator):
    with sqlite3.connect(":memory:") as native:
        native.execute(
            "CREATE TABLE messages(MesLocalID INTEGER,Message BLOB,WCDB_CT_Message INTEGER)"
        )
        native.execute(
            "INSERT INTO messages VALUES(1,?,0)",
            (b"wxid_source_author" + separator + b"\xe4",),
        )
        assert scope._sender_prefix(native, "messages", 1, "WCDB_CT_Message") == (
            "wxid_source_author"
        )


@pytest.mark.parametrize(
    "title",
    ["<title>First</title><title>Second</title>", "<title><b>Nested</b></title>"],
)
def test_ambiguous_or_structured_app_title_is_not_flattened(
    preserved_native_scope, title
):
    f = preserved_native_scope
    payload = (
        f"<msg><appmsg><type>5</type>{title}</appmsg>"
        "<appinfo><appname>Source application</appname></appinfo></msg>"
    )
    with sqlite3.connect(f.database) as native:
        native.execute(
            f'UPDATE "{f.tables[0]}" SET Message=? WHERE MesLocalID=7', (payload,)
        )
    _reseal_synthetic_database(f)
    candidate, _ = _stage(f)
    row = _read(candidate, "SELECT * FROM messages WHERE ordinal=7")[0]
    reference = json.loads(row["provenance"])["resource_reference"]
    assert not reference.get("safe_title")
    assert reference["app_name"] == "Source application"
    assert reference["kind"] == "app"


def test_malformed_app_metadata_preserves_generic_source_occurrence(
    preserved_native_scope,
):
    f = preserved_native_scope
    with sqlite3.connect(f.database) as native:
        native.execute(
            f'UPDATE "{f.tables[0]}" SET Message=? WHERE MesLocalID=7',
            ("<msg><appmsg><type>5</type><title>Broken",),
        )
    _reseal_synthetic_database(f)
    candidate, _ = _stage(f)
    row = _read(candidate, "SELECT * FROM messages WHERE ordinal=7")[0]
    reference = json.loads(row["provenance"])["resource_reference"]
    assert not reference.get("safe_title")
    assert not reference.get("app_name")
    assert reference["kind"] == "app"


def test_existing_candidate_is_retained_exactly(preserved_native_scope):
    f = preserved_native_scope
    candidate = f.tmp_path / "keep.sqlite3"
    candidate.write_bytes(b"Keep this previous candidate")
    with pytest.raises(ValueError):
        _stage(f, candidate=candidate)
    assert candidate.read_bytes() == b"Keep this previous candidate"


def test_cloud_only_database_is_refused_before_source_open(
    preserved_native_scope, monkeypatch
):
    f = preserved_native_scope
    original_lstat, original_connect = Path.lstat, sqlite3.connect

    def cloud(path):
        state = original_lstat(path)
        if path == f.database:
            return SimpleNamespace(
                **{
                    key: getattr(state, key)
                    for key in dir(state)
                    if key.startswith("st_") and key != "st_flags"
                },
                st_flags=0x40000000,
            )
        return state

    def no_database_open(path, *args, **kwargs):
        assert str(f.database) not in str(path)
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", cloud)
    monkeypatch.setattr(sqlite3, "connect", no_database_open)
    with pytest.raises(ValueError):
        _stage(f)
    assert not (f.tmp_path / "scope-0.sqlite3").exists()


def test_merging_disjoint_verified_scopes_preserves_all_metadata(
    preserved_native_scope,
):
    f = preserved_native_scope
    first, _ = _stage(f)
    second, _ = _stage(f, number=1)
    before = first.read_bytes(), second.read_bytes(), f.history.read_bytes()
    output = f.tmp_path / "merged.sqlite3"
    report = scope.merge_native_resource_scopes(f.store, [first, second], output)
    rows = _read(output, "SELECT * FROM messages")
    assert report["messages"] == len(rows) == 12
    assert {row["conversation_id"] for row in rows} == set(f.conversations)
    assert not report["published"]
    assert (first.read_bytes(), second.read_bytes(), f.history.read_bytes()) == before
    assert {row["id"] for row in rows} == {
        row["id"] for row in _read(first, "SELECT * FROM messages")
    } | {row["id"] for row in _read(second, "SELECT * FROM messages")}


def test_merging_rejects_modified_candidate_content(preserved_native_scope):
    f = preserved_native_scope
    candidate, _ = _stage(f)
    with sqlite3.connect(candidate) as connection:
        connection.execute("UPDATE messages SET text='Forged metadata copy'")
    output = f.tmp_path / "refused.sqlite3"
    with pytest.raises(ValueError):
        scope.merge_native_resource_scopes(f.store, [candidate], output)
    assert not output.exists()


def test_merging_matching_native_occurrences_does_not_duplicate_storage(
    preserved_native_scope,
):
    f = preserved_native_scope
    first, _ = _stage(f)
    second, _ = _stage(f, candidate=f.tmp_path / "same-scope.sqlite3")
    output = f.tmp_path / "merged-same-scope.sqlite3"
    report = scope.merge_native_resource_scopes(f.store, [first, second], output)
    rows = _read(output, "SELECT * FROM messages")
    assert report["messages"] == len(rows) == 6
    assert {row["id"] for row in rows} == {
        row["id"] for row in _read(first, "SELECT * FROM messages")
    }


def test_merging_preserves_native_witnesses_and_sealed_supplement_metadata(
    preserved_native_scope,
):
    f = preserved_native_scope
    prior, _witness_value, extra = _witnessed_prior(f)
    prior_witnesses = _read(prior, "SELECT * FROM revalidation_native_witnesses")
    current, current_report = _stage(f)
    before = {path: path.read_bytes() for path in (prior, current, f.history)}
    output = f.tmp_path / "merged-witnessed.sqlite3"
    report = scope.merge_native_resource_scopes(f.store, [prior, current], output)
    assert report["messages"] == 12
    assert (
        _read(output, "SELECT * FROM revalidation_native_witnesses") == prior_witnesses
    )
    info = {
        row["key"]: row["value"] for row in _read(output, "SELECT * FROM archive_info")
    }
    assert {key: info[key] for key in extra} == extra
    assert json.loads(info["supplement_source_receipts"]) == json.loads(
        extra["supplement_source_receipts"]
    )
    new_receipts = current_report["source_receipts"]
    assert new_receipts
    assert all(value in json.loads(info["source_receipts"]) for value in new_receipts)
    with sqlite3.connect(output) as connection:
        assert info["content_sha256"] == _content_digest(connection)
    assert set(_verified_coverage(f.history, output)) == set(f.conversations)
    assert {path: path.read_bytes() for path in before} == before


def test_conflicting_sealed_supplement_flags_refuse_merge(preserved_native_scope):
    f = preserved_native_scope
    first, _, _ = _witnessed_prior(f)
    second, _, _ = _witnessed_prior(
        f, name="different-flag", metadata_override={"supplemental_metadata": "0"}
    )
    before = first.read_bytes(), second.read_bytes(), f.history.read_bytes()
    output = f.tmp_path / "refused-metadata-conflict.sqlite3"
    with pytest.raises(ValueError):
        scope.merge_native_resource_scopes(f.store, [first, second], output)
    assert not output.exists()
    assert (first.read_bytes(), second.read_bytes(), f.history.read_bytes()) == before


def test_conflicting_witness_receipts_refuse_merge(preserved_native_scope):
    f = preserved_native_scope
    first, first_witness, extra = _witnessed_prior(f)
    receipts = json.loads(extra["supplement_source_receipts"])
    receipts[0]["manifest_sha256"] = "f" * 64
    second, second_witness, _ = _witnessed_prior(
        f,
        name="different-manifest",
        metadata_override={"supplement_source_receipts": _json(receipts)},
    )
    assert first_witness[0] == second_witness[0]
    assert first_witness != second_witness
    output = f.tmp_path / "refused-witness-conflict.sqlite3"
    with pytest.raises(ValueError):
        scope.merge_native_resource_scopes(f.store, [first, second], output)
    assert not output.exists()


@pytest.mark.parametrize("server", [True, 1.0, -1, 2**63, "01", "1e3", "bad"])
def test_primary_server_id_requires_an_exact_positive_native_identifier(
    preserved_native_scope, server
):
    f = preserved_native_scope
    mid = f.primary_ids[(f.conversations[0], 1)]
    with sqlite3.connect(f.history) as primary:
        proof = json.loads(
            primary.execute(
                "SELECT provenance FROM messages WHERE id=?", (mid,)
            ).fetchone()[0]
        )
        proof["server_id"] = server
        primary.execute(
            "UPDATE messages SET provenance=? WHERE id=?", (_json(proof), mid)
        )
    with pytest.raises(ValueError):
        _stage(f)
    assert not (f.tmp_path / "scope-0.sqlite3").exists()


@pytest.mark.parametrize("server", [1.5, -1, str(2**63), "not-an-id", b"10004"])
def test_malformed_native_server_id_does_not_become_missing_metadata(
    preserved_native_scope, server
):
    f = preserved_native_scope
    with sqlite3.connect(f.database) as native:
        native.execute(
            f'UPDATE "{f.tables[0]}" SET MesSvrID=? WHERE MesLocalID=4', (server,)
        )
    _reseal_synthetic_database(f)
    with pytest.raises(ValueError):
        _stage(f)
    assert not (f.tmp_path / "scope-0.sqlite3").exists()


@pytest.mark.parametrize("server", [None, 0])
def test_explicit_absent_native_server_id_preserves_occurrence(
    preserved_native_scope, server
):
    f = preserved_native_scope
    with sqlite3.connect(f.database) as native:
        native.execute(
            f'UPDATE "{f.tables[0]}" SET MesSvrID=? WHERE MesLocalID=4', (server,)
        )
    _reseal_synthetic_database(f)
    candidate, report = _stage(f)
    assert report["messages"] == 6
    row = _read(candidate, "SELECT * FROM messages WHERE ordinal=4")[0]
    proof = json.loads(row["provenance"])
    assert proof["server_id"] is None
    assert proof["locator"]["local_id"] == 4
    assert row["timestamp_value"] == _times(BASE_TIME + 4)[1]
