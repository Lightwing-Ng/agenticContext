"""Supplemental native metadata requires exact independent source receipts.

Code version: v1.0.1-codex.0
"""

from datetime import UTC, datetime
import hashlib
import json
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_native_media_supplement as supplement
from app.core.wechat_archive import (
    WechatArchiveRepository,
    _SCHEMA,
    _file_identity,
    _identifier,
    _time_fields,
    wechat_history_partition_fingerprint,
)
from app.core.wechat_native_resource_revalidation import _validate_row
from app.core.wechat_primary_source_receipts import primary_source_database_receipts


OWNER = "wxid_supplement_owner"
PEER = "wxid_supplement_peer"
BACKUP = "independently-preserved-ipad"


def _insert(connection, table, values):
    connection.execute(
        f"INSERT INTO {table} VALUES({','.join('?' for _ in values)})", values
    )


@pytest.fixture(params=["native", "html"])
def media_source(tmp_path, monkeypatch, request):
    owner_hash = hashlib.md5(OWNER.encode(), usedforsecurity=False).hexdigest()
    native = "ios-" + owner_hash
    table = "Chat_" + hashlib.md5(PEER.encode(), usedforsecurity=False).hexdigest()
    relative = f"Documents/{owner_hash}/DB/message_1.sqlite"
    database = tmp_path / "inputs" / relative
    database.parent.mkdir(parents=True)
    rows = [
        dict(
            MesLocalID=local,
            MesSvrID=str(server),
            CreateTime=1700000000 + epoch,
            Des=0,
            Type=typ,
            Status=status,
            ImgStatus=1,
        )
        for local, server, epoch, typ, status in (
            (1, 101, 1, 1, 2),
            (2, 202, 2, 47, 2),
            (3, 303, 3, 49, 2),
            (4, 0, 4, 47, 5),
            (5, 0, 4, 47, 5),
            (6, 606, 6, 50, 2),
            (7, 707, 7, 3, 2),
            (8, 808, 8, 49, 2),
        )
    ]
    with sqlite3.connect(database) as source:
        source.execute(
            f'CREATE TABLE "{table}"(MesLocalID INTEGER PRIMARY KEY,MesSvrID INTEGER,CreateTime INTEGER,Des INTEGER,Type INTEGER,Status INTEGER,ImgStatus INTEGER,Message BLOB)'
        )
        source.executemany(
            f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?,?)',
            [(*row.values(), b"Never decode this payload") for row in rows],
        )
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    file_id = hashlib.sha1(
        ("AppDomain-com.tencent.xin-" + relative).encode(), usedforsecurity=False
    ).hexdigest()
    store = tmp_path / "store"
    history = store / "messaging/wechat/history.sqlite3"
    history.parent.mkdir(parents=True)
    cid = "wechat:" + _identifier(native, PEER)
    html = "html-" + "a" * 24
    anchor_cid = (
        cid if request.param == "native" else "wechat:" + _identifier(html, PEER)
    )
    metadata = dict(
        source_id=native,
        source_account_id=OWNER,
        account_hash=owner_hash,
        account_hash_verified=True,
        snapshot_id=BACKUP,
        identity_evidence=dict(directory_hash=owner_hash),
    )
    raw_metadata = json.dumps(metadata)
    metadata_digest = hashlib.sha256(raw_metadata.encode()).hexdigest()
    conversation = (
        cid,
        native,
        "account:" + OWNER,
        PEER,
        "Native source",
        "direct",
        0,
        0,
        0,
        "",
        "",
        "",
    )
    with sqlite3.connect(history) as primary:
        primary.executescript(_SCHEMA)
        primary.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,source_id TEXT,metadata TEXT)"
        )
        _insert(
            primary,
            "partitions",
            (
                native,
                "account:" + OWNER,
                OWNER,
                "Native owner",
                "Verified archive ID",
                "iPad archive",
                "Preserved snapshot",
                "",
            ),
        )
        _insert(primary, "conversations", conversation)
        if request.param == "html":
            _insert(
                primary,
                "partitions",
                (
                    html,
                    "account:" + OWNER,
                    OWNER,
                    "HTML owner",
                    "Corroborated export ID",
                    "HTML export",
                    "Recorded source.zip",
                    "",
                ),
            )
            _insert(
                primary,
                "conversations",
                (
                    anchor_cid,
                    html,
                    "account:" + OWNER,
                    PEER,
                    "HTML container",
                    "direct",
                    2,
                    0,
                    0,
                    "",
                    "",
                    "",
                ),
            )
        _insert(primary, "source_snapshots", (1, BACKUP, native, raw_metadata))
        for raw in (rows[0], rows[2]):
            local, epoch = raw["MesLocalID"], raw["CreateTime"]
            proof = dict(
                source_snapshot=1,
                record_id=f"primary-{local}",
                server_id=raw["MesSvrID"],
                unix_timestamp=epoch,
                message_type=raw["Type"],
                text_extraction_kind="plain_text",
                locator=dict(
                    database="message_1.sqlite",
                    database_relative_path=relative,
                    database_sha256=digest,
                    file_id=file_id,
                    table=table,
                    local_id=local,
                    row_id=local,
                ),
            )
            clock = _time_fields(
                dict(sent_at_utc_iso=datetime.fromtimestamp(epoch, UTC).isoformat())
            )
            _insert(
                primary,
                "messages",
                (
                    f"primary-{local}",
                    anchor_cid,
                    local,
                    OWNER,
                    OWNER,
                    "sent",
                    "Independent primary text",
                    *clock,
                    json.dumps(proof),
                    "[]",
                    "independent primary text",
                ),
            )
    evidence = dict(
        version="v1.0.0-codex.0",
        scope="exact_readonly_media_metadata_only",
        source_database=str(database),
        source_database_sha256=digest,
        source_database_bytes=database.stat().st_size,
        source_snapshot_id=1,
        snapshot_id=BACKUP,
        source_id=native,
        account_id=OWNER,
        peer_id=PEER,
        source_metadata_sha256=metadata_digest,
        database_relative_path=relative,
        database_file_id=file_id,
        table=table,
        message_payload_column_read=False,
        media_files_read=0,
        rows=rows[1:],
    )
    evidence_path = tmp_path / "native-media-evidence.json"
    evidence_path.write_text(json.dumps(evidence))
    base = tmp_path / "base-catalog.sqlite3"
    base_row, base_proof = supplement._project(
        evidence,
        rows[6],
        dict(
            owner=OWNER,
            backup_id="older-independent-backup",
            basis="preservation_manifest",
        ),
        relative,
        table,
        dict(
            zip(
                ("id", "partition_id", "account_key", "peer_id", "title", "kind"),
                conversation[:6],
            )
        ),
    )
    with sqlite3.connect(base) as catalog, sqlite3.connect(history) as primary:
        catalog.executescript(_SCHEMA)
        _insert(catalog, "messages", base_row)
        _insert(
            catalog,
            "conversations",
            (
                *conversation[:6],
                1,
                base_row[7],
                base_row[7],
                base_row[8],
                base_row[8],
                base_row[6],
            ),
        )
        catalog.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("history_file_identity", json.dumps(_file_identity(history))),
                (
                    "history_partitions_sha256",
                    wechat_history_partition_fingerprint(primary),
                ),
                ("messages", "1"),
                (
                    "report",
                    json.dumps(
                        dict(
                            messages=1,
                            conversations=1,
                            kinds=dict(image=1),
                            accounts=[dict(account_id=OWNER, messages=1)],
                        )
                    ),
                ),
            ],
        )
    monkeypatch.setattr(supplement, "_clonefile", shutil.copyfile)
    return SimpleNamespace(
        store=store,
        history=history,
        base=base,
        evidence=evidence,
        evidence_path=evidence_path,
        database=database,
        table=table,
        conversation=cid,
        anchor_conversation=anchor_cid,
        root=tmp_path,
    )


def _stage(fixture):
    candidate = fixture.root / "supplement-candidate.sqlite3"
    report = supplement.stage_native_media_supplement(
        fixture.store, fixture.base, candidate, evidence_paths=[fixture.evidence_path]
    )
    return candidate, report


def test_exact_metadata_supplement_keeps_zero_server_rows_distinct_and_sources_unchanged(
    media_source, monkeypatch
):
    fixture = media_source
    before = {
        path: path.read_bytes()
        for path in (fixture.history, fixture.base, fixture.database)
    }
    original_connect = sqlite3.connect
    database_uri = fixture.database.resolve().as_uri()
    columns_read = []

    def guarded_connect(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        if str(database).partition("?")[0] == database_uri:

            def authorize(action, table, column, *_):
                if action == sqlite3.SQLITE_READ:
                    assert column != "Message"
                    columns_read.append(column)
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    candidate, report = _stage(fixture)
    assert report["added"] == 5 and report["published"] is False
    assert report["sources"][0]["primary_duplicates"] == 1
    assert report["sources"][0]["catalog_duplicates"] == 1
    assert report["sources"][0]["kinds"] == dict(sticker=3, unknown=1, app=1)
    assert (
        report["message_payload_columns_read"]
        == report["media_payload_files_read"]
        == 0
    )
    assert "MesLocalID" in columns_read
    with sqlite3.connect(candidate) as output:
        output.row_factory = sqlite3.Row
        rows = output.execute("SELECT * FROM messages ORDER BY ordinal").fetchall()
        assert len(rows) == 6 and {row["conversation_id"] for row in rows} == {
            fixture.conversation
        }
        zero = [
            json.loads(row["provenance"]) for row in rows if row["ordinal"] in {4, 5}
        ]
        assert len(zero) == 2 and all(proof["server_id"] is None for proof in zero)
        assert all(proof["recorded_native_metadata"]["Status"] == 5 for proof in zero)
        assert (
            next(row["text"] for row in rows if row["ordinal"] == 6)
            == "[Other message]"
        )
        assert (
            next(row["text"] for row in rows if row["ordinal"] == 8) == "[App message]"
        )
        assert (
            output.execute("SELECT message_count FROM conversations").fetchone()[0] == 6
        )
        info = dict(output.execute("SELECT key,value FROM archive_info"))
        assert info["messages"] == "6"
        assert json.loads(info["report"])["kinds"] == dict(
            image=1, sticker=3, unknown=1, app=1
        )
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize(
    "damage",
    [
        "row",
        "database_sha256",
        "source_metadata_sha256",
        "account_id",
        "peer_id",
        "table",
        "source_snapshot_id",
        "source_database_bytes",
        "message_payload_column_read",
    ],
)
def test_tampered_source_or_metadata_evidence_never_creates_candidate(
    media_source, damage
):
    fixture = media_source
    evidence = fixture.evidence
    if damage == "row":
        evidence["rows"][0]["Type"] = 3
    elif damage == "database_sha256":
        evidence["source_database_sha256"] = "0" * 64
    elif damage == "source_metadata_sha256":
        evidence[damage] = "0" * 64
    elif damage in {"account_id", "peer_id"}:
        evidence[damage] = "wxid_other_person"
    elif damage == "table":
        evidence[damage] = "Chat_" + "0" * 32
    elif damage == "source_snapshot_id":
        evidence[damage] = 999
    elif damage == "source_database_bytes":
        evidence[damage] += 1
    else:
        evidence[damage] = True
    fixture.evidence_path.write_text(json.dumps(evidence))
    before = fixture.history.read_bytes(), fixture.base.read_bytes()
    with pytest.raises(ValueError):
        _stage(fixture)
    assert not (fixture.root / "supplement-candidate.sqlite3").exists()
    assert before == (fixture.history.read_bytes(), fixture.base.read_bytes())


def test_full_database_hash_rejects_changed_payload_even_when_metadata_is_identical(
    media_source,
):
    fixture = media_source
    with sqlite3.connect(fixture.database) as source:
        source.execute(
            f'UPDATE "{fixture.table}" SET Message=? WHERE MesLocalID=2',
            (b"A different preserved source",),
        )
    with pytest.raises(ValueError, match="full-file receipt"):
        _stage(fixture)
    assert not (fixture.root / "supplement-candidate.sqlite3").exists()


def test_staged_rows_satisfy_shared_validator_and_native_timeline_scope(media_source):
    fixture = media_source
    if fixture.anchor_conversation != fixture.conversation:
        with sqlite3.connect(fixture.history) as primary:
            primary.execute(
                "DELETE FROM conversations WHERE id=?", (fixture.conversation,)
            )
        with sqlite3.connect(fixture.base) as catalog:
            catalog.execute(
                "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
                (json.dumps(_file_identity(fixture.history)),),
            )
    candidate, _ = _stage(fixture)
    with (
        sqlite3.connect(fixture.history) as primary,
        sqlite3.connect(candidate) as catalog,
    ):
        primary.row_factory = catalog.row_factory = sqlite3.Row
        anchor = dict(
            primary.execute(
                "SELECT * FROM conversations WHERE id=?", (fixture.anchor_conversation,)
            ).fetchone()
        )
        receipt = primary_source_database_receipts(primary, anchor, 1)
        native = dict(
            catalog.execute(
                "SELECT * FROM conversations WHERE id=?", (fixture.conversation,)
            ).fetchone()
        )
        for row in catalog.execute("SELECT * FROM messages"):
            proof = json.loads(row["provenance"])
            if proof["source_checksum_basis"] == "primary_source_database_receipt":
                _validate_row(
                    row, native, OWNER, receipt["receipts"], receipt["snapshots"]
                )
    canonical = fixture.history.with_name("resource_references.sqlite3")
    shutil.copyfile(candidate, canonical)
    repository = WechatArchiveRepository(fixture.store)
    page = repository.query(session=fixture.conversation)
    assert page["resource_index_status"] == "ready"
    assert page["count"] == (
        8 if fixture.anchor_conversation == fixture.conversation else 6
    )
    if fixture.anchor_conversation != fixture.conversation:
        assert repository.query(session=fixture.anchor_conversation)["count"] == 2
        assert page["current"]["title"] == "HTML container"


@pytest.mark.parametrize("location", ["primary", "catalog"])
def test_identical_positive_event_in_another_account_does_not_deduplicate(
    media_source, location
):
    fixture = media_source
    foreign = "wxid_other_supplement_owner"
    partition = (
        "ios-" + hashlib.md5(foreign.encode(), usedforsecurity=False).hexdigest()
    )
    cid = "wechat:" + _identifier(partition, PEER)
    conversation = dict(
        id=cid,
        partition_id=partition,
        account_key="account:" + foreign,
        peer_id=PEER,
        title="Another source account",
        kind="direct",
    )
    relative = f"Documents/{partition[4:]}/DB/message_1.sqlite"
    row, proof = supplement._project(
        fixture.evidence,
        fixture.evidence["rows"][0],
        dict(owner=foreign, backup_id="foreign-backup", basis="preservation_manifest"),
        relative,
        fixture.table,
        conversation,
    )
    with sqlite3.connect(fixture.history) as primary:
        _insert(
            primary,
            "partitions",
            (
                partition,
                "account:" + foreign,
                foreign,
                foreign,
                "Verified archive ID",
                "iPad archive",
                "Other native evidence",
                "",
            ),
        )
        _insert(primary, "conversations", (*conversation.values(), 0, 0, 0, "", "", ""))
        if location == "primary":
            proof.update(source_snapshot=2, text_extraction_kind="plain_text")
            row = (*row[:11], json.dumps(proof), *row[12:])
            _insert(primary, "source_snapshots", (2, "foreign-backup", partition, "{}"))
            _insert(primary, "messages", row)
    with (
        sqlite3.connect(fixture.base) as catalog,
        sqlite3.connect(fixture.history) as primary,
    ):
        catalog.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture.history)),),
        )
        catalog.execute(
            "UPDATE archive_info SET value=? WHERE key='history_partitions_sha256'",
            (wechat_history_partition_fingerprint(primary),),
        )
        if location == "catalog":
            _insert(catalog, "messages", row)
            _insert(
                catalog,
                "conversations",
                (*conversation.values(), 1, row[7], row[7], row[8], row[8], row[6]),
            )
            report = dict(
                messages=2,
                conversations=2,
                kinds=dict(image=1, sticker=1),
                accounts=[
                    dict(account_id=OWNER, messages=1),
                    dict(account_id=foreign, messages=1),
                ],
            )
            catalog.execute("UPDATE archive_info SET value='2' WHERE key='messages'")
            catalog.execute(
                "UPDATE archive_info SET value=? WHERE key='report'",
                (json.dumps(report),),
            )
    candidate, report = _stage(fixture)
    assert report["added"] == 5
    with sqlite3.connect(candidate) as catalog:
        assert (
            catalog.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                (fixture.conversation,),
            ).fetchone()[0]
            == 6
        )


@pytest.mark.parametrize("field", ["server", "epoch", "type", "direction"])
def test_exact_native_row_conflicts_are_rejected_instead_of_deduplicated(
    media_source, field
):
    fixture = media_source
    with sqlite3.connect(fixture.history) as primary:
        raw, direction = primary.execute(
            "SELECT provenance,direction FROM messages WHERE id='primary-3'"
        ).fetchone()
        proof = json.loads(raw)
        if field == "server":
            proof["server_id"] = "999999"
        elif field == "epoch":
            proof["unix_timestamp"] += 1
        elif field == "type":
            proof["message_type"] = 3
        else:
            direction = "received"
        primary.execute(
            "UPDATE messages SET provenance=?,direction=? WHERE id='primary-3'",
            (json.dumps(proof), direction),
        )
    with sqlite3.connect(fixture.base) as catalog:
        catalog.execute(
            "UPDATE archive_info SET value=? WHERE key='history_file_identity'",
            (json.dumps(_file_identity(fixture.history)),),
        )
    with pytest.raises(ValueError, match="conflicts with retained metadata"):
        _stage(fixture)
    assert not (fixture.root / "supplement-candidate.sqlite3").exists()
