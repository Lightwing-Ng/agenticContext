"""Global native resource coverage remains streamed, source-bound, and lossless.

Code version: v1.2.0-codex.0
"""

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from app.core import wechat_native_resource_extension as extension
from app.core.wechat_archive import (
    _SCHEMA as HISTORY_SCHEMA,
    _file_identity,
    _identifier,
    _time_fields,
    wechat_history_partition_fingerprint,
)
from app.core.wechat_resource_index import _SCHEMA as RESOURCE_SCHEMA
from app.core.wechat_native_resource_revalidation import _validate_row
from app.core.wechat_primary_source_receipts import primary_source_database_receipts
from app.core.wechat_resource_catalog_refresh import stage_resource_catalog_refresh


OWNER = "wxid_global_owner"
PEER = "wxid_global_peer"
EPOCH = 1_700_000_000


def _insert(connection, table, values):
    connection.execute(
        f"INSERT INTO {table} VALUES({','.join('?' for _ in values)})", values
    )


def _hash(value):
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest()


def _row(local, server, typ, *, epoch=None, side=1):
    return dict(
        MesLocalID=local,
        MesSvrID=server,
        CreateTime=epoch if epoch is not None else EPOCH + local,
        Type=typ,
        Des=side,
    )


def _message(identifier, cid, proof, *, direction="received"):
    epoch = proof.get("unix_timestamp", EPOCH)
    clock = _time_fields(
        {"sent_at_utc_iso": datetime.fromtimestamp(epoch, UTC).isoformat()}
    )
    return (
        identifier,
        cid,
        proof.get("locator", {}).get("local_id", 1),
        PEER,
        "Same display name",
        direction,
        "Primary text",
        *clock,
        json.dumps(proof),
        "[]",
        "primary text",
    )


@pytest.fixture
def archive(tmp_path, monkeypatch):
    store = tmp_path / "store"
    history = store / "messaging/wechat/history.sqlite3"
    history.parent.mkdir(parents=True)
    with sqlite3.connect(history) as primary:
        primary.executescript(HISTORY_SCHEMA)
        primary.execute(
            "CREATE TABLE source_snapshots(id INTEGER PRIMARY KEY,snapshot_id TEXT,"
            "source_id TEXT,metadata TEXT)"
        )
    monkeypatch.setattr(extension, "_clonefile", shutil.copyfile)
    return SimpleNamespace(store=store, history=history, root=tmp_path, specs=[])


def _source(
    archive, sid, tables, *, owner=OWNER, preserved=False, anchor=True, payloads=None
):
    native = "ios-" + _hash(owner)
    relative = f"Documents/{_hash(owner)}/DB/message_1.sqlite"
    database = archive.root / f"snapshot-{sid}" / "inputs" / relative
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as source:
        for peer, rows in tables.items():
            table = "Chat_" + _hash(peer)
            source.execute(
                f'CREATE TABLE "{table}"(MesLocalID INTEGER PRIMARY KEY,MesSvrID INTEGER,'
                "CreateTime INTEGER,Type INTEGER,Des INTEGER,Message BLOB,WCDB_CT_Message INTEGER)"
            )
            source.executemany(
                f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)',
                [
                    (
                        *row.values(),
                        *(payloads or {}).get(
                            (peer, row["MesLocalID"]), (b"Never select this payload", 0)
                        ),
                    )
                    for row in rows
                ],
            )
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    backup = f"independent-native-backup-{sid}"
    file_id = hashlib.sha1(
        ("AppDomain-com.tencent.xin-" + relative).encode(), usedforsecurity=False
    ).hexdigest()
    metadata = dict(
        source_id=native,
        source_account_id=owner,
        account_hash_verified=True,
        account_hash=_hash(owner),
        snapshot_id=backup,
        identity_evidence=dict(directory_hash=_hash(owner)),
    )
    if preserved:
        metadata.update(
            backup_id=backup,
            input_databases=[dict(relative_path=relative, sha256=digest)],
        )
    with sqlite3.connect(archive.history) as primary:
        if not primary.execute(
            "SELECT 1 FROM partitions WHERE id=?", (native,)
        ).fetchone():
            _insert(
                primary,
                "partitions",
                (
                    native,
                    "account:" + owner,
                    owner,
                    "Same display name",
                    "Verified archive ID",
                    "iPad archive",
                    "Preserved snapshot",
                    "",
                ),
            )
        _insert(
            primary, "source_snapshots", (sid, backup, native, json.dumps(metadata))
        )
        for peer, rows in tables.items():
            cid = "wechat:" + _identifier(native, peer)
            text_rows = [row for row in rows if row["Type"] == 1]
            if not text_rows or not anchor:
                continue
            if not primary.execute(
                "SELECT 1 FROM conversations WHERE id=?", (cid,)
            ).fetchone():
                _insert(
                    primary,
                    "conversations",
                    (
                        cid,
                        native,
                        "account:" + owner,
                        peer,
                        "Same display name",
                        "group" if peer.endswith("@chatroom") else "direct",
                        1,
                        0,
                        0,
                        "",
                        "",
                        "",
                    ),
                )
            raw = text_rows[0]
            proof = dict(
                source_snapshot=sid,
                server_id=str(raw["MesSvrID"]),
                unix_timestamp=raw["CreateTime"],
                message_type=1,
                text_extraction_kind="plain_text",
                locator=dict(
                    database="message_1.sqlite",
                    database_relative_path=relative,
                    database_sha256=digest,
                    file_id=file_id,
                    table="Chat_" + _hash(peer),
                    local_id=raw["MesLocalID"],
                    row_id=raw["MesLocalID"],
                ),
            )
            _insert(primary, "messages", _message(f"anchor-{sid}-{peer}", cid, proof))
    spec = dict(
        source_snapshot_id=sid,
        database_path=database,
        database_relative_path=relative,
    )
    archive.specs.append(spec)
    return SimpleNamespace(
        spec=spec,
        database=database,
        digest=digest,
        relative=relative,
        native=native,
        metadata=metadata,
        backup=backup,
        file_id=file_id,
    )


def _base(archive):
    base = archive.root / "base.sqlite3"
    with sqlite3.connect(base) as catalog, sqlite3.connect(archive.history) as primary:
        catalog.executescript(RESOURCE_SCHEMA)
        catalog.executemany(
            "INSERT INTO archive_info VALUES(?,?)",
            [
                ("schema_version", "1"),
                ("history_file_identity", json.dumps(_file_identity(archive.history))),
                (
                    "history_partitions_sha256",
                    wechat_history_partition_fingerprint(primary),
                ),
                ("messages", "0"),
                (
                    "report",
                    json.dumps(
                        dict(messages=0, conversations=0, kinds={}, accounts=[])
                    ),
                ),
            ],
        )
    return base


def _stage(archive, base=None, *, name="candidate.sqlite3", **kwargs):
    base = base or _base(archive)
    candidate = archive.root / name
    report = extension.stage_native_resource_catalog_extension(
        archive.store, base, candidate, database_specs=archive.specs, **kwargs
    )
    return candidate, report


def test_streams_more_than_100000_primary_rows_without_reading_message(
    archive, monkeypatch
):
    source = _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47)]})
    cid = "wechat:" + _identifier(source.native, PEER)
    with sqlite3.connect(archive.history) as primary:
        primary.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_message(f"filler-{index}", cid, {}) for index in range(100_001)),
        )
    readonly = extension._readonly
    columns = []

    @contextmanager
    def guarded(path, *args, **kwargs):
        with readonly(path, *args, **kwargs) as connection:
            if Path(path) == source.database:

                def authorize(action, table, column, *_):
                    if action == sqlite3.SQLITE_READ:
                        columns.append(column)
                        if column == "Message":
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                connection.set_authorizer(authorize)
            yield connection

    monkeypatch.setattr(extension, "_readonly", guarded)
    candidate, report = _stage(archive, batch_size=17)
    assert report["primary_observations_checked"] == 100_002
    assert report["added"] == 1
    assert report["message_payload_columns_read"] == 0
    assert "MesLocalID" in columns and "Message" not in columns
    with sqlite3.connect(candidate) as result:
        assert result.execute(
            "SELECT resource_rows,native_rows FROM native_resource_coverage"
        ).fetchone() == (1, 2)


def test_alternate_snapshots_keep_all_occurrences_and_repeat_idempotently(archive):
    _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47, epoch=EPOCH + 2)]})
    _source(archive, 2, {PEER: [_row(10, 110, 1), _row(20, 202, 47, epoch=EPOCH + 2)]})
    base = _base(archive)
    before = {path: path.read_bytes() for path in (archive.history, base)}
    candidate, report = _stage(archive, base)
    assert report["added"] == 1
    with sqlite3.connect(candidate) as result:
        assert result.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert (
            result.execute("SELECT COUNT(*) FROM native_resource_sources").fetchone()[0]
            == 2
        )
        assert (
            result.execute(
                "SELECT COUNT(*) FROM native_resource_occurrences"
            ).fetchone()[0]
            == 2
        )
        assert (
            result.execute(
                "SELECT COUNT(DISTINCT canonical_id) FROM native_resource_occurrences"
            ).fetchone()[0]
            == 1
        )
        original_rows = result.execute("SELECT * FROM messages").fetchall()
    again, second = _stage(archive, candidate, name="second.sqlite3")
    assert second["added"] == 0
    assert second["repeated_occurrences"] == 2
    with sqlite3.connect(again) as result:
        assert result.execute("SELECT * FROM messages").fetchall() == original_rows
        assert (
            result.execute(
                "SELECT COUNT(*) FROM native_resource_occurrences"
            ).fetchone()[0]
            == 2
        )
    assert all(path.read_bytes() == value for path, value in before.items())


def test_negative_zero_and_missing_server_ids_require_exact_source_rows(archive):
    _source(
        archive,
        1,
        {
            PEER: [
                _row(1, 101, 1),
                _row(2, -123, 3, epoch=EPOCH + 2),
                _row(3, -123, 3, epoch=EPOCH + 2),
                _row(4, 0, 47, epoch=EPOCH + 2),
                _row(5, None, 47, epoch=EPOCH + 2),
            ]
        },
    )
    candidate, report = _stage(archive)
    assert report["added"] == 4
    with sqlite3.connect(candidate) as result:
        proofs = [
            json.loads(row[0])
            for row in result.execute("SELECT provenance FROM messages")
        ]
        assert all(proof["server_id"] is None for proof in proofs)
        raw = {proof["recorded_native_metadata"]["MesSvrID"] for proof in proofs}
        assert raw == {"-123", "0", None}
    again, second = _stage(archive, candidate, name="repeat.sqlite3")
    assert second["added"] == 0
    with sqlite3.connect(again) as result:
        assert result.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4


def test_same_display_name_and_server_id_do_not_merge_accounts(archive):
    for sid, owner in ((1, OWNER), (2, "wxid_other_global_owner")):
        _source(archive, sid, {PEER: [_row(1, 101, 1), _row(2, 202, 3)]}, owner=owner)
    candidate, report = _stage(archive)
    assert report["added"] == 2
    with sqlite3.connect(candidate) as result:
        assert (
            result.execute(
                "SELECT COUNT(DISTINCT account_key) FROM conversations"
            ).fetchone()[0]
            == 2
        )
        assert (
            result.execute(
                "SELECT COUNT(DISTINCT account_id) FROM native_resource_occurrences"
            ).fetchone()[0]
            == 2
        )


def test_resource_only_zero_and_unresolved_peer_tables_have_coverage(archive):
    other, unknown, empty = "wxid_resource_only", "wxid_unknown_peer", "wxid_empty_peer"
    source = _source(
        archive,
        1,
        {
            PEER: [_row(1, 101, 1)],
            other: [_row(2, 202, 43)],
            unknown: [_row(3, 303, 47)],
            empty: [],
        },
    )
    source.spec["peer_ids"] = [other, empty]
    candidate, report = _stage(archive)
    assert report["tables_checked"] == 4
    assert report["unknown_peer_tables"] == 1
    assert report["added"] == 2
    with sqlite3.connect(candidate) as result:
        coverage = result.execute(
            "SELECT peer_id,status,native_rows,resource_rows FROM native_resource_coverage"
        ).fetchall()
        assert (other, "covered", 1, 1) in coverage
        assert (empty, "covered", 0, 0) in coverage
        assert ("", "unresolved_peer_hash", 1, 1) in coverage
        assert result.execute(
            "SELECT kind FROM conversations WHERE peer_id='' "
        ).fetchone() == ("unclassified",)


def test_conflicting_exact_native_locator_rejects_and_removes_candidate(archive):
    source = _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47)]})
    with sqlite3.connect(archive.history) as primary:
        proof = json.loads(
            primary.execute("SELECT provenance FROM messages LIMIT 1").fetchone()[0]
        )
        proof.update(server_id="202", unix_timestamp=EPOCH + 999, message_type=47)
        proof["locator"].update(local_id=2, row_id=2)
        _insert(
            primary,
            "messages",
            _message(
                "conflicting-source-row",
                "wechat:" + _identifier(source.native, PEER),
                proof,
            ),
        )
    base = _base(archive)
    before = (
        archive.history.read_bytes(),
        base.read_bytes(),
        source.database.read_bytes(),
    )
    with pytest.raises(ValueError, match="conflicting metadata"):
        _stage(archive, base)
    assert not (archive.root / "candidate.sqlite3").exists()
    assert before == (
        archive.history.read_bytes(),
        base.read_bytes(),
        source.database.read_bytes(),
    )


def test_preservation_receipt_can_cover_shard_without_primary_anchor(archive):
    source = _source(archive, 1, {PEER: [_row(2, 202, 49)]}, anchor=False)
    root = source.database.parents[4]
    report_path, sources_path = root / "preservation-report.json", root / "sources.json"
    report_path.write_text(
        json.dumps(
            dict(
                snapshot_id=source.backup,
                selected_files=[
                    dict(
                        domain="AppDomain-com.tencent.xin",
                        relative_path=source.relative,
                        sha256=source.digest,
                        snapshot_sha256=source.digest,
                        source_sha256=source.digest,
                        bytes=source.database.stat().st_size,
                        file_id=source.file_id,
                        input_relative_path="inputs/" + source.relative,
                    )
                ],
            )
        )
    )
    sources_path.write_text(json.dumps([source.metadata]))
    source.spec.update(
        preservation_report_path=report_path, sources_path=sources_path, peer_ids=[PEER]
    )
    candidate, report = _stage(archive)
    assert report["added"] == 1
    with sqlite3.connect(candidate) as result:
        assert result.execute(
            "SELECT receipt_basis FROM native_resource_sources"
        ).fetchone() == ("preservation_manifest",)


def test_unreceipted_and_changed_databases_are_not_imported(archive):
    source = _source(archive, 1, {PEER: [_row(2, 202, 3)]}, anchor=False)
    base = _base(archive)
    with pytest.raises(ValueError, match="no independent"):
        _stage(archive, base)
    assert not (archive.root / "candidate.sqlite3").exists()
    with sqlite3.connect(archive.history) as primary:
        metadata = source.metadata | dict(
            backup_id=source.backup,
            input_databases=[dict(relative_path=source.relative, sha256="0" * 64)],
        )
        primary.execute(
            "UPDATE source_snapshots SET metadata=?", (json.dumps(metadata),)
        )
    base.unlink()
    base = _base(archive)
    with pytest.raises(ValueError, match="full original SHA-256"):
        _stage(archive, base)


@pytest.mark.parametrize("typ", [42, 48, 66, 67])
def test_contact_and_location_metadata_retain_generic_placeholders(archive, typ):
    _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, typ)]})
    candidate, report = _stage(archive)
    assert report["added"] == 1
    with sqlite3.connect(candidate) as result:
        text, raw = result.execute("SELECT text,provenance FROM messages").fetchone()
        assert text == "[Other message]"
        assert json.loads(raw)["message_type"] == typ
        assert json.loads(raw)["resource_reference"]["kind"] == "unknown"


def test_legacy_member_prefix_is_verified_against_original_account_hash(archive):
    source = _source(archive, 1, {PEER: [_row(2, 202, 3)]}, anchor=False)
    metadata = source.metadata | dict(
        backup_id=source.backup,
        input_databases=[
            dict(
                member_path="9.6聊天记录/" + source.relative,
                sha256=source.digest,
                bytes=source.database.stat().st_size,
            )
        ],
    )
    with sqlite3.connect(archive.history) as primary:
        primary.execute(
            "UPDATE source_snapshots SET metadata=?", (json.dumps(metadata),)
        )
    source.spec["peer_ids"] = [PEER]
    candidate, report = _stage(archive)
    assert report["added"] == 1
    with sqlite3.connect(candidate) as result:
        proof = json.loads(
            result.execute("SELECT proof FROM native_resource_sources").fetchone()[0]
        )
        assert proof["member_path"] == "9.6聊天记录/" + source.relative


def test_declared_multisha_versions_preserve_backup_folders_and_clock_differences(
    archive,
):
    first = _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47)]})
    second = _source(
        archive,
        2,
        {
            PEER: [
                _row(10, 110, 1),
                _row(20, 202, 47, epoch=EPOCH + 2),
                _row(21, 202, 47, epoch=EPOCH + 99),
            ]
        },
    )
    occurrences = []
    for source, folder in (
        (first, "backup-a"),
        (first, "backup-b"),
        (second, "backup-c"),
    ):
        occurrences.append(
            dict(
                account_hash=_hash(OWNER),
                backup_date=folder,
                backup_folder=folder,
                bytes=source.database.stat().st_size,
                container_id="original-container",
                file_id=source.file_id,
                kind="DB/message_1.sqlite",
                manifest_sha256="a" * 64,
                object="objects/" + source.digest,
                relative_path=source.relative,
                sha256=source.digest,
                source_identity=[1, 2, 3],
                source_path=str(source.database),
            )
        )
    metadata = first.metadata | dict(
        native_occurrences=occurrences,
        native_restore_version="v1.1.1",
        source_inventory_sha256="b" * 64,
    )
    metadata.pop("snapshot_id")
    with sqlite3.connect(archive.history) as primary:
        primary.execute(
            "UPDATE source_snapshots SET metadata=? WHERE id=1", (json.dumps(metadata),)
        )
        primary.execute("DELETE FROM source_snapshots WHERE id=2")
        raw = primary.execute(
            "SELECT provenance FROM messages WHERE id=?", ("anchor-2-" + PEER,)
        ).fetchone()[0]
        proof = json.loads(raw) | dict(source_snapshot=1)
        primary.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), "anchor-2-" + PEER),
        )
    second.spec["source_snapshot_id"] = 1
    candidate, report = _stage(archive)
    assert report["added"] == 2
    with sqlite3.connect(candidate) as result:
        assert (
            result.execute("SELECT COUNT(*) FROM native_resource_sources").fetchone()[0]
            == 2
        )
        assert (
            result.execute(
                "SELECT COUNT(*) FROM native_resource_occurrences"
            ).fetchone()[0]
            == 3
        )
        assert (
            result.execute("SELECT COUNT(*) FROM native_resource_coverage").fetchone()[
                0
            ]
            == 2
        )
        retained = [
            json.loads(row[0])["native_occurrences"]
            for row in result.execute("SELECT proof FROM native_resource_sources")
        ]
        assert sorted(len(value) for value in retained) == [1, 2]
        clocks = sorted(
            json.loads(row[0])["unix_timestamp"]
            for row in result.execute("SELECT provenance FROM messages")
        )
        assert clocks == [EPOCH + 2, EPOCH + 99]


def test_undeclared_same_snapshot_database_versions_reject(archive):
    _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 3)]})
    second = _source(archive, 2, {PEER: [_row(10, 110, 1), _row(20, 220, 3)]})
    with sqlite3.connect(archive.history) as primary:
        primary.execute("DELETE FROM source_snapshots WHERE id=2")
        raw = primary.execute(
            "SELECT provenance FROM messages WHERE id=?", ("anchor-2-" + PEER,)
        ).fetchone()[0]
        proof = json.loads(raw) | dict(source_snapshot=1)
        primary.execute(
            "UPDATE messages SET provenance=? WHERE id=?",
            (json.dumps(proof), "anchor-2-" + PEER),
        )
    second.spec["source_snapshot_id"] = 1
    with pytest.raises(ValueError, match="undeclared source versions"):
        _stage(archive)


@pytest.mark.parametrize("server", [0, -123])
def test_same_original_backup_in_two_snapshot_rows_keeps_one_unidentified_event(
    archive, server
):
    rows = {PEER: [_row(1, 101, 1), _row(2, server, 47)]}
    first = _source(archive, 1, rows)
    second = _source(archive, 2, rows)
    assert first.digest == second.digest
    metadata = second.metadata | dict(snapshot_id=first.backup)
    with sqlite3.connect(archive.history) as primary:
        primary.execute(
            "UPDATE source_snapshots SET snapshot_id=?,metadata=? WHERE id=2",
            (first.backup, json.dumps(metadata)),
        )
    candidate, report = _stage(archive)
    assert report["added"] == 1
    with sqlite3.connect(candidate) as result:
        assert result.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert (
            result.execute(
                "SELECT COUNT(*) FROM native_resource_occurrences"
            ).fetchone()[0]
            == 2
        )
        assert (
            result.execute(
                "SELECT COUNT(DISTINCT source_snapshot_id) FROM native_resource_occurrences"
            ).fetchone()[0]
            == 2
        )
        proof = json.loads(
            result.execute("SELECT provenance FROM messages").fetchone()[0]
        )
        assert proof["record_id_scheme"] == "wechat-resource-reference-v2"
    _, again = _stage(archive, candidate, name="repeat-backup.sqlite3")
    assert again["added"] == 0


def test_conflicting_exact_negative_ids_are_not_normalized_into_one_event(archive):
    source = _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, -123, 47)]})
    with sqlite3.connect(archive.history) as primary:
        proof = json.loads(
            primary.execute("SELECT provenance FROM messages LIMIT 1").fetchone()[0]
        )
        proof.update(server_id="-456", unix_timestamp=EPOCH + 2, message_type=47)
        proof["locator"].update(local_id=2, row_id=2)
        _insert(
            primary,
            "messages",
            _message(
                "conflicting-negative-id",
                "wechat:" + _identifier(source.native, PEER),
                proof,
            ),
        )
    with pytest.raises(ValueError, match="conflicting metadata"):
        _stage(archive)


def test_v2_projection_requires_independent_digest_and_supported_scheme(archive):
    source = _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47)]})
    candidate, _ = _stage(archive)
    with (
        sqlite3.connect(candidate) as result,
        sqlite3.connect(archive.history) as primary,
    ):
        result.row_factory = primary.row_factory = sqlite3.Row
        row = dict(result.execute("SELECT * FROM messages").fetchone())
        conversation = dict(result.execute("SELECT * FROM conversations").fetchone())
        receipt = primary_source_database_receipts(primary, conversation, 1)
        assert _validate_row(
            row, conversation, OWNER, receipt["receipts"], receipt["snapshots"]
        )
        proof = json.loads(row["provenance"])
        assert proof["record_id_scheme"] == "wechat-resource-reference-v2"
        assert proof["source_sha256"] == source.digest
        unproven = {
            **row,
            "provenance": json.dumps(proof | dict(source_sha256="0" * 64)),
        }
        with pytest.raises(ValueError, match="preserved database receipt"):
            _validate_row(
                unproven, conversation, OWNER, receipt["receipts"], receipt["snapshots"]
            )
        unsupported = {
            **row,
            "provenance": json.dumps(proof | dict(record_id_scheme="unproven-v3")),
        }
        with pytest.raises(ValueError, match="unsupported record identifier scheme"):
            _validate_row(
                unsupported,
                conversation,
                OWNER,
                receipt["receipts"],
                receipt["snapshots"],
            )


def test_legacy_catalog_refresh_preserves_extended_memberships_by_rejecting(archive):
    _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47)]})
    candidate, _ = _stage(archive)
    canonical = archive.history.with_name("resource_references.sqlite3")
    shutil.copyfile(candidate, canonical)
    before = canonical.read_bytes()
    target = archive.root / "lossy-refresh.sqlite3"
    with pytest.raises(ValueError, match="memberships and coverage"):
        stage_resource_catalog_refresh(archive.store, target)
    assert not target.exists()
    assert canonical.read_bytes() == before


def test_missing_backup_frame_is_not_accepted_without_restoration_receipts(archive):
    source = _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47)]})
    metadata = dict(source.metadata)
    metadata.pop("snapshot_id")
    with sqlite3.connect(archive.history) as primary:
        primary.execute(
            "UPDATE source_snapshots SET metadata=?", (json.dumps(metadata),)
        )
    with pytest.raises(ValueError, match="independent native account evidence"):
        _stage(archive)


def test_unknown_peer_v2_resource_requires_its_exact_unclassified_hash_bucket(archive):
    unknown = "wxid_unresolved_native_peer"
    source = _source(
        archive,
        1,
        {
            PEER: [_row(1, 101, 1)],
            unknown: [_row(2, 202, 47)],
        },
    )
    candidate, _ = _stage(archive)
    with (
        sqlite3.connect(candidate) as result,
        sqlite3.connect(archive.history) as primary,
    ):
        result.row_factory = primary.row_factory = sqlite3.Row
        row = dict(result.execute("SELECT * FROM messages").fetchone())
        conversation = dict(result.execute("SELECT * FROM conversations").fetchone())
        anchor = dict(primary.execute("SELECT * FROM conversations").fetchone())
        receipt = primary_source_database_receipts(primary, anchor, 1)
        assert conversation["peer_id"] == ""
        assert conversation["kind"] == "unclassified"
        proof = json.loads(row["provenance"])
        assert proof["unresolved_peer_hash"] == _hash(unknown)
        assert conversation["id"] == "wechat:" + _identifier(
            source.native, _hash(unknown)
        )
        assert _validate_row(
            row, conversation, OWNER, receipt["receipts"], receipt["snapshots"]
        )
        wrong = {
            **row,
            "provenance": json.dumps(proof | dict(unresolved_peer_hash="0" * 32)),
        }
        with pytest.raises(ValueError):
            _validate_row(
                wrong, conversation, OWNER, receipt["receipts"], receipt["snapshots"]
            )
        with pytest.raises(ValueError):
            _validate_row(
                row,
                conversation | dict(kind="direct"),
                OWNER,
                receipt["receipts"],
                receipt["snapshots"],
            )


@pytest.mark.parametrize(
    "invalid", [None, "wrong-owner", "traversal", "missing-sha", "wrong-container"]
)
def test_legacy_string_identity_requires_exact_archive_and_account_receipts(
    archive, invalid
):
    source = _source(archive, 1, {PEER: [_row(2, 202, 3)]}, anchor=False)
    member = "9.6聊天记录/Documents/" + _hash(OWNER) + "/mmsetting.archive"
    metadata = source.metadata | dict(
        backup_id=source.backup,
        container_kind="legacy_ios",
        identity_evidence="NSKeyedArchiver UsrName with exact MD5 account-directory agreement; aliases do not establish identity",
        identity_member_path=member,
        identity_sha256="a" * 64,
        archive_sha256="b" * 64,
        input_databases=[
            dict(
                member_path="9.6聊天记录/" + source.relative,
                sha256=source.digest,
                bytes=source.database.stat().st_size,
            )
        ],
    )
    metadata.pop("snapshot_id")
    if invalid == "wrong-owner":
        metadata["identity_member_path"] = member.replace(_hash(OWNER), "0" * 32)
    elif invalid == "traversal":
        metadata["identity_member_path"] = "../" + member
    elif invalid == "missing-sha":
        metadata.pop("identity_sha256")
    elif invalid == "wrong-container":
        metadata["container_kind"] = "unknown"
    with sqlite3.connect(archive.history) as primary:
        primary.execute(
            "UPDATE source_snapshots SET metadata=?", (json.dumps(metadata),)
        )
    source.spec["peer_ids"] = [PEER]
    if invalid:
        with pytest.raises(ValueError, match="independent native account evidence"):
            _stage(archive)
        assert not (archive.root / "candidate.sqlite3").exists()
    else:
        candidate, report = _stage(archive)
        assert report["added"] == 1
        with sqlite3.connect(candidate) as result:
            assert result.execute(
                "SELECT receipt_basis FROM native_resource_sources"
            ).fetchone() == ("preservation_manifest",)


def test_old_catalog_unknown_server_can_match_exact_fresh_negative_metadata(archive):
    source = _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, -123, 47)]})
    base = _base(archive)
    with sqlite3.connect(archive.history) as primary, sqlite3.connect(base) as catalog:
        primary.row_factory = sqlite3.Row
        conversation = dict(primary.execute("SELECT * FROM conversations").fetchone())
        raw_metadata = primary.execute(
            "SELECT metadata FROM source_snapshots WHERE id=1"
        ).fetchone()[0]
        evidence = dict(
            account_id=OWNER,
            peer_id=PEER,
            source_snapshot_id=1,
            source_metadata_sha256=hashlib.sha256(raw_metadata.encode()).hexdigest(),
            source_database_sha256=source.digest,
        )
        row, proof = extension._project(
            evidence,
            _row(2, -123, 47),
            dict(
                owner=OWNER,
                backup_id=source.backup,
                basis="primary_source_database_receipt",
            ),
            source.relative,
            "Chat_" + _hash(PEER),
            conversation,
        )
        proof.pop("recorded_native_metadata")
        assert proof["server_id"] is None
        row = (*row[:11], json.dumps(proof), *row[12:])
        _insert(catalog, "messages", row)
        _insert(
            catalog,
            "conversations",
            (
                *(
                    conversation[key]
                    for key in (
                        "id",
                        "partition_id",
                        "account_key",
                        "peer_id",
                        "title",
                        "kind",
                    )
                ),
                1,
                row[7],
                row[7],
                row[8],
                row[8],
                row[6],
            ),
        )
        report = dict(
            messages=1,
            conversations=1,
            kinds=dict(sticker=1),
            accounts=[dict(account_id=OWNER, messages=1)],
        )
        catalog.execute(
            "UPDATE archive_info SET value=? WHERE key='report'", (json.dumps(report),)
        )
        catalog.execute("UPDATE archive_info SET value='1' WHERE key='messages'")
    candidate, report = _stage(archive, base)
    assert report["added"] == 0
    with sqlite3.connect(candidate) as result:
        assert result.execute("SELECT * FROM messages").fetchall() == [row]
        occurrence = json.loads(
            result.execute(
                "SELECT native_metadata FROM native_resource_occurrences"
            ).fetchone()[0]
        )
        assert occurrence["MesSvrID"] == "-123"


@pytest.mark.parametrize("kind", ["brand", "chatbot"])
@pytest.mark.parametrize(
    "invalid", [None, "wrong-owner", "missing-sha", "message-db", "unreceipted"]
)
def test_brand_observations_are_audited_outside_native_message_database_scope(
    archive, invalid, kind
):
    source = _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47)]})
    brand_peer = "gh_brand_account"
    suffix = "Brand/BrandMsg.db" if kind == "brand" else "ChatBot/msg.sqlite3"
    path = "Documents/" + _hash(OWNER) + "/" + suffix
    locator = dict(
        container_kind=kind,
        database=path,
        relative_path=path,
        input_sha256="a" * 64,
        file_id=hashlib.sha1(
            ("AppDomain-com.tencent.xin-" + path).encode(), usedforsecurity=False
        ).hexdigest(),
        table="Chat_" + _hash(brand_peer),
        local_id=1,
        row_id=1,
        text_origin="native_wcdb_message_cell",
    )
    if invalid == "wrong-owner":
        locator["relative_path"] = path.replace(_hash(OWNER), "0" * 32)
        locator["database"] = locator["relative_path"]
    elif invalid == "missing-sha":
        locator.pop("input_sha256")
    elif invalid == "message-db":
        locator["database"] = locator["relative_path"] = source.relative
    cid = "wechat:" + _identifier(source.native, brand_peer)
    proof = dict(
        source_snapshot=1,
        server_id="301",
        unix_timestamp=EPOCH + 10,
        message_type=1,
        text_extraction_kind="plain_text",
        locator=locator,
    )
    with sqlite3.connect(archive.history) as primary:
        metadata = source.metadata | dict(
            input_databases=[]
            if invalid == "unreceipted"
            else [
                dict(
                    relative_path=path,
                    sha256="a" * 64,
                    file_id=locator["file_id"],
                    bytes=100,
                )
            ]
        )
        primary.execute(
            "UPDATE source_snapshots SET metadata=?", (json.dumps(metadata),)
        )
        _insert(
            primary,
            "conversations",
            (
                cid,
                source.native,
                "account:" + OWNER,
                brand_peer,
                "Brand account",
                "direct",
                1,
                0,
                0,
                "",
                "",
                "",
            ),
        )
        _insert(primary, "messages", _message("brand-primary", cid, proof))
    if invalid:
        with pytest.raises(ValueError, match="auxiliary source"):
            _stage(archive)
        assert not (archive.root / "candidate.sqlite3").exists()
    else:
        candidate, report = _stage(archive)
        assert report["outside_scope_" + kind + "_observations"] == 1
        assert report["primary_observations_checked"] == 2
        assert report["added"] == 1
        with sqlite3.connect(candidate) as result:
            assert result.execute(
                "SELECT resource_rows FROM native_resource_coverage"
            ).fetchone() == (1,)
            assert (
                result.execute(
                    "SELECT COUNT(*) FROM native_resource_sources"
                ).fetchone()[0]
                == 1
            )


def test_group_native_direction_keys_preserve_uncertain_sender_presentation(archive):
    group, safe_group = "100000001@chatroom", "100000002@chatroom"
    source = _source(
        archive,
        1,
        {
            group: [
                _row(1, 101, 1, side=0),
                _row(2, 202, 49, side=0),
                _row(3, 303, 47, side=0),
                _row(4, 404, 3, side=1),
            ],
            safe_group: [_row(1, 111, 1), _row(2, 222, 47, side=0)],
        },
    )
    cid = "wechat:" + _identifier(source.native, group)
    with sqlite3.connect(archive.history) as primary:
        raw = primary.execute(
            "SELECT provenance FROM messages WHERE conversation_id=?", (cid,)
        ).fetchone()[0]
        proof = json.loads(raw)
        proof["locator"].update(
            native_direction=0,
            native_sender_prefix="wxid_unresolved_group_member",
            native_sender_prefix_matches_owner=False,
            sender_prefix_identity_status="unresolved",
        )
        primary.execute(
            "UPDATE messages SET direction='unknown',provenance=? WHERE conversation_id=?",
            (json.dumps(proof), cid),
        )
        proof.update(server_id="202", unix_timestamp=EPOCH + 2, message_type=49)
        proof["locator"].update(local_id=2, row_id=2)
        original = list(
            _message("existing-uncertain-contact", cid, proof, direction="unknown")
        )
        original[3:5] = ["existing-contact-object", "Existing contact"]
        _insert(primary, "messages", original)
    before = archive.history.read_bytes()
    candidate, report = _stage(archive)
    assert report["added"] == 3
    assert report["primary_duplicates"] == 1
    assert report["unresolved_des0_scopes"] == 1
    assert report["presentation_unknown_resources_added"] == 1
    assert archive.history.read_bytes() == before
    with sqlite3.connect(candidate) as result:
        result.row_factory = sqlite3.Row
        rows = result.execute("SELECT * FROM messages").fetchall()
        assert sorted(row["direction"] for row in rows) == [
            "received",
            "sent",
            "unknown",
        ]
        uncertain = next(row for row in rows if row["direction"] == "unknown")
        assert uncertain["sender_id"] is None
        assert uncertain["sender_label"] == "Unknown sender"
        assert "native_des0_sender_unresolved" in json.loads(uncertain["quality_flags"])
        proof = json.loads(uncertain["provenance"])
        assert proof["recorded_native_metadata"]["Des"] == 0
        assert extension._native_direction(proof, uncertain["direction"]) == "sent"
        scope = result.execute(
            "SELECT * FROM native_resource_direction_scopes WHERE id=?",
            (proof["direction_scope_id"],),
        ).fetchone()
        assert scope["database_sha256"] == source.digest
        assert scope["chat_table"] == "Chat_" + _hash(group)
        assert (
            proof["presentation_direction_basis"]
            == "unresolved_native_des0_sender_prefix"
        )
        assert (
            result.execute(
                "SELECT canonical_id FROM native_resource_occurrences WHERE local_id=2 AND chat_table=?",
                ("Chat_" + _hash(group),),
            ).fetchone()[0]
            == "existing-uncertain-contact"
        )
        expected_rows = [tuple(row) for row in rows]
    repeated, second = _stage(archive, candidate, name="group-repeat.sqlite3")
    assert second["added"] == 0
    with sqlite3.connect(repeated) as result:
        assert result.execute("SELECT * FROM messages").fetchall() == expected_rows
        assert (
            result.execute(
                "SELECT COUNT(*) FROM native_resource_occurrences"
            ).fetchone()[0]
            == 4
        )


def test_conflicting_original_native_direction_receipts_remain_rejected(archive):
    _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47)]})
    with sqlite3.connect(archive.history) as primary:
        proof = json.loads(
            primary.execute("SELECT provenance FROM messages LIMIT 1").fetchone()[0]
        )
        proof.update(recorded_native_metadata=dict(Des=1))
        proof["locator"]["native_direction"] = 0
        primary.execute("UPDATE messages SET provenance=?", (json.dumps(proof),))
    with pytest.raises(ValueError, match="direction receipts disagree"):
        _stage(archive)



def test_selected_resource_only_table_uses_outside_primary_database_receipt(archive):
    selected = "wxid_selected_resource_only"
    source = _source(
        archive, 1,
        {PEER: [_row(1, 101, 1), _row(2, 202, 47)], selected: [_row(3, 303, 3)]},
    )
    table = "Chat_" + _hash(selected)
    source.spec.update(chat_tables=[table], peer_ids=[selected])
    candidate, report = _stage(archive)
    assert report["added"] == report["occurrences_added"] == 1
    assert report["tables_checked"] == report["outside_scope_tables"] == 1
    assert report["primary_observations_outside_table_scope"] == 1
    assert report["seed_primary_rows"] == report["seed_event_keys"] == 0
    assert report["selected_table_scopes"][0]["chat_tables"] == [table]
    with sqlite3.connect(candidate) as result:
        assert result.execute(
            "SELECT receipt_basis FROM native_resource_sources"
        ).fetchone() == ("primary_source_database_receipt",)
        assert result.execute(
            "SELECT chat_table,resource_rows,native_rows FROM native_resource_coverage"
        ).fetchall() == [(table, 1, 1)]
    repeated, replay = _stage(archive, candidate, name="selected-repeat.sqlite3")
    assert replay["added"] == replay["occurrences_added"] == 0
    assert replay["repeated_occurrences"] == 1
    assert replay["seed_event_keys"] == replay["seed_locator_keys"] == 1
    with sqlite3.connect(repeated) as result:
        assert result.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_table_batches_equal_full_coverage_and_keep_cross_snapshot_membership(archive):
    other = "wxid_other_table"
    for sid in (1, 2):
        _source(
            archive, sid,
            {
                PEER: [_row(1, 101, 1), _row(2, 202, 47)],
                other: [_row(3, 303, 1), _row(4, 404, 3)],
            },
        )
    base = _base(archive)
    full, full_report = _stage(archive, base, name="full.sqlite3")
    for spec in archive.specs:
        spec["chat_tables"] = ["Chat_" + _hash(PEER)]
    first, first_report = _stage(archive, base, name="first-batch.sqlite3")
    assert first_report["added"] == 1
    assert first_report["occurrences_added"] == 2
    for spec in archive.specs:
        spec["chat_tables"] = ["Chat_" + _hash(other)]
    final, final_report = _stage(archive, first, name="last-batch.sqlite3")
    assert final_report["added"] == 1
    assert final_report["occurrences_added"] == 2
    assert final_report["catalog_rows_outside_table_scope"] == 1
    assert final_report["retained_occurrences_outside_table_scope"] == 2
    assert final_report["seed_event_keys"] == final_report["seed_locator_keys"] == 0
    with sqlite3.connect(full) as expected, sqlite3.connect(final) as actual:
        for table in (
            "messages", "conversations", "native_resource_occurrences",
            "native_resource_sources", "native_resource_coverage",
        ):
            assert sorted(actual.execute(f"SELECT * FROM {table}")) == sorted(
                expected.execute(f"SELECT * FROM {table}")
            )
    _, replay = _stage(archive, final, name="batch-replay.sqlite3")
    assert replay["added"] == replay["occurrences_added"] == 0
    assert replay["repeated_occurrences"] == 2
    assert full_report["added"] == 2


@pytest.mark.parametrize(
    "selection", [[], "Chat_bad", ["Chat_bad"], ["Chat_" + _hash(PEER)] * 2,
                  ["Chat_" + _hash("wxid_absent")], None],
)
def test_rejects_invalid_or_missing_table_allowlists_without_changes(archive, selection):
    source = _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47)]})
    source.spec["chat_tables"] = selection
    base = _base(archive)
    before = [path.read_bytes() for path in (archive.history, base, source.database)]
    with pytest.raises(ValueError, match="Chat table"):
        _stage(archive, base)
    assert not (archive.root / "candidate.sqlite3").exists()
    assert before == [path.read_bytes() for path in (archive.history, base, source.database)]


def test_mixed_table_specs_union_only_actual_selected_tables(archive):
    other, outside = "wxid_mixed_other", "wxid_not_selected"
    first = _source(
        archive, 1,
        {PEER: [_row(1, 101, 1), _row(2, 202, 47)], outside: [_row(3, 303, 3)]},
    )
    _source(archive, 2, {other: [_row(1, 111, 1), _row(2, 222, 43)]})
    first.spec["chat_tables"] = ["Chat_" + _hash(PEER)]
    candidate, report = _stage(archive)
    assert report["tables_checked"] == 2
    assert report["outside_scope_tables"] == 1
    assert report["added"] == report["occurrences_added"] == 2
    with sqlite3.connect(candidate) as result:
        assert {row[0] for row in result.execute(
            "SELECT chat_table FROM native_resource_coverage"
        )} == {"Chat_" + _hash(PEER), "Chat_" + _hash(other)}


@pytest.mark.parametrize("invalid_kind", ["virtual", "missing_column"])
def test_selected_tables_must_be_ordinary_with_complete_metadata(archive, invalid_kind):
    source = _source(archive, 1, {PEER: [_row(1, 101, 1)]})
    table = "Chat_" + _hash("wxid_invalid_table")
    with sqlite3.connect(source.database) as connection:
        if invalid_kind == "virtual":
            connection.execute(
                f'CREATE VIRTUAL TABLE "{table}" USING fts5('
                'MesLocalID,MesSvrID,CreateTime,Type,Des)'
            )
        else:
            connection.execute(
                f'CREATE TABLE "{table}"(MesLocalID,MesSvrID,CreateTime,Type)'
            )
    source.spec["chat_tables"] = [table]
    with pytest.raises(ValueError, match="ordinary metadata column contract"):
        _stage(archive)
    assert not (archive.root / "candidate.sqlite3").exists()


def test_disjoint_table_lists_do_not_allow_duplicate_physical_specs(archive):
    other = "wxid_duplicate_other"
    source = _source(
        archive, 1,
        {PEER: [_row(1, 101, 1)], other: [_row(2, 202, 47)]},
    )
    source.spec["chat_tables"] = ["Chat_" + _hash(PEER)]
    archive.specs.append({**source.spec, "chat_tables": ["Chat_" + _hash(other)]})
    with pytest.raises(ValueError, match="physical native source database only once"):
        _stage(archive)
    assert not (archive.root / "candidate.sqlite3").exists()


def test_later_snapshot_batch_reuses_prior_same_peer_catalog_event(archive):
    first = _source(archive, 1, {PEER: [_row(1, 101, 1), _row(2, 202, 47)]})
    second = _source(archive, 2, {PEER: [_row(10, 110, 1), _row(20, 202, 47, epoch=EPOCH + 2)]})
    table = "Chat_" + _hash(PEER)
    first.spec["chat_tables"] = second.spec["chat_tables"] = [table]
    archive.specs = [first.spec]
    base = _base(archive)
    initial, _ = _stage(archive, base, name="snapshot-first.sqlite3")
    archive.specs = [second.spec]
    final, report = _stage(archive, initial, name="snapshot-next.sqlite3")
    assert report["added"] == 0
    assert report["occurrences_added"] == 1
    assert report["catalog_duplicates"] == 1
    assert report["seed_event_keys"] == 1
    with sqlite3.connect(final) as result:
        assert result.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert result.execute(
            "SELECT COUNT(*) FROM native_resource_occurrences"
        ).fetchone()[0] == 2


def test_outside_group_observation_does_not_add_direction_scopes(archive):
    group = "123456789@chatroom"
    source = _source(
        archive, 1,
        {PEER: [_row(1, 101, 1), _row(2, 202, 47)], group: [_row(1, 303, 1, side=0)]},
    )
    cid = "wechat:" + _identifier(source.native, group)
    with sqlite3.connect(archive.history) as primary:
        proof = json.loads(primary.execute(
            "SELECT provenance FROM messages WHERE conversation_id=?", (cid,)
        ).fetchone()[0])
        proof["locator"].update(
            native_direction=0,
            native_sender_prefix="wxid_unresolved_member",
            native_sender_prefix_matches_owner=False,
            sender_prefix_identity_status="unresolved",
        )
        primary.execute(
            "UPDATE messages SET direction='unknown',provenance=? WHERE conversation_id=?",
            (json.dumps(proof), cid),
        )
    source.spec["chat_tables"] = ["Chat_" + _hash(PEER)]
    candidate, report = _stage(archive)
    assert report["primary_observations_outside_table_scope"] == 1
    assert report["unresolved_des0_scopes"] == 0
    with sqlite3.connect(candidate) as result:
        assert result.execute(
            "SELECT COUNT(*) FROM native_resource_direction_scopes"
        ).fetchone()[0] == 0


@pytest.fixture
def sender_dictionary():
    backend = pytest.importorskip("compression.zstd")
    samples = [
        f"wxid_sample_{i}:\n<msg><videomsg repeated='metadata {i % 7}'/></msg>".encode()
        for i in range(200)
    ]
    trained = backend.train_dict(samples, 1024)
    data = bytearray(trained.dict_content)
    data[4:8] = (5).to_bytes(4, "little")
    dictionary = backend.ZstdDict(bytes(data))

    def compress(sender):
        return backend.compress(
            (sender + ":\n<msg><videomsg/></msg>").encode(), zstd_dict=dictionary
        )

    return SimpleNamespace(content=bytes(data), compress=compress)


def test_enabled_sender_resolution_handles_mixed_payloads_and_exact_replay(
    archive, sender_dictionary
):
    group = "123456789@chatroom"
    compressed = sender_dictionary.compress("wxid_compressed_author")
    source = _source(
        archive,
        1,
        {group: [_row(1, 101, 1), *[_row(i, 200 + i, 43) for i in range(2, 6)]]},
        payloads={
            (group, 2): (b"wxid_plain_author:\n<msg><videomsg/></msg>", 0),
            (group, 3): (compressed, 2),
            (group, 4): (b"\xff", 0),
            (group, 5): (b"quoted wxid_other_author:\n<msg/>", 0),
        },
    )
    base = _base(archive)
    original = {
        path: path.read_bytes() for path in (archive.history, base, source.database)
    }
    candidate, report = _stage(archive, base, resolve_sender_prefixes=True)
    assert report["native_sender_statuses"] == {
        "resolved": 1,
        "missing_dictionary": 1,
        "invalid_payload": 1,
        "prefix_unavailable": 1,
    }
    assert report["message_payload_columns_read"] == 4
    assert report["media_payload_files_read"] == 0
    with sqlite3.connect(candidate) as result:
        result.row_factory = sqlite3.Row
        rows = result.execute("SELECT * FROM messages ORDER BY ordinal").fetchall()
        assert [row["sender_id"] for row in rows] == [
            "wxid_plain_author", None, None, None
        ]
        proof = json.loads(rows[1]["provenance"])
        resolution = proof["native_resource_sender_resolution"]
        assert resolution["status"] == "missing_dictionary"
        assert resolution["dictionary_id"] == 5
        assert resolution["basis"] == "native_compression_frame_dictionary_requirement"
        assert resolution["native_message_sha256"] == hashlib.sha256(compressed).hexdigest()
        assert set(proof["recorded_native_metadata"]) == set(extension._FIELDS)
        assert all(
            set(json.loads(row[0])) == set(extension._FIELDS)
            for row in result.execute("SELECT native_metadata FROM native_resource_occurrences")
        )
        retained = {
            table: result.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in (
                "messages", "conversations", "native_resource_occurrences",
                "native_resource_sources", "native_resource_coverage",
            )
        }
    repeated, replay = _stage(
        archive, candidate, name="sender-replay.sqlite3", resolve_sender_prefixes=True
    )
    assert replay["added"] == replay["occurrences_added"] == 0
    assert replay["message_payload_columns_read"] == 0
    assert replay["native_sender_statuses"] == {}
    with sqlite3.connect(repeated) as result:
        result.row_factory = sqlite3.Row
        for table, before in retained.items():
            assert result.execute(f'SELECT * FROM "{table}"').fetchall() == before
    assert all(path.read_bytes() == value for path, value in original.items())


def test_enabled_sender_resolution_uses_exact_dictionary(archive, sender_dictionary):
    group, sender = "123456789@chatroom", "wxid_compressed_author"
    _source(
        archive, 1, {group: [_row(1, 101, 1), _row(2, 202, 43)]},
        payloads={(group, 2): (sender_dictionary.compress(sender), 2)},
    )
    candidate, report = _stage(
        archive, resolve_sender_prefixes=True,
        sender_dictionaries={5: sender_dictionary.content},
    )
    assert report["native_sender_statuses"] == {"resolved": 1}
    with sqlite3.connect(candidate) as result:
        author, raw = result.execute("SELECT sender_id,provenance FROM messages").fetchone()
    resolution = json.loads(raw)["native_resource_sender_resolution"]
    assert author == sender
    assert resolution["dictionary_sha256"] == hashlib.sha256(sender_dictionary.content).hexdigest()


def test_enabled_sender_resolution_keeps_accounts_separate(archive):
    group = "123456789@chatroom"
    for sid, owner, sender in (
        (1, OWNER, "wxid_first_author"),
        (2, "wxid_other_global_owner", "wxid_second_author"),
    ):
        _source(
            archive, sid, {group: [_row(1, 101, 1), _row(2, 202, 43)]}, owner=owner,
            payloads={(group, 2): ((sender + ":\n<msg/>").encode(), 0)},
        )
    candidate, report = _stage(archive, resolve_sender_prefixes=True)
    assert report["added"] == 2
    with sqlite3.connect(candidate) as result:
        assert result.execute(
            "SELECT c.account_key,m.sender_id FROM messages m JOIN conversations c "
            "ON c.id=m.conversation_id ORDER BY c.account_key"
        ).fetchall() == sorted([
            ("account:" + OWNER, "wxid_first_author"),
            ("account:wxid_other_global_owner", "wxid_second_author"),
        ])


@pytest.mark.parametrize("field,value", [("MesSvrID", "999"), ("Des", 0)])
def test_enabled_sender_resolution_rejects_wrong_exact_row(
    archive, monkeypatch, field, value
):
    group = "123456789@chatroom"
    _source(
        archive, 1, {group: [_row(1, 101, 1), _row(2, 202, 43)]},
        payloads={(group, 2): (b"wxid_author:\n<msg/>", 0)},
    )
    resolve = extension.resolve_source_bound_resource_sender

    def mismatched(row, proof, native, **kwargs):
        return resolve(row, proof, native | {field: value}, **kwargs)

    monkeypatch.setattr(extension, "resolve_source_bound_resource_sender", mismatched)
    with pytest.raises(ValueError, match="recorded event metadata"):
        _stage(archive, resolve_sender_prefixes=True)
    assert not (archive.root / "candidate.sqlite3").exists()


def test_sender_resolution_does_not_decode_direct_or_sent_resources(archive):
    group = "123456789@chatroom"
    source = _source(
        archive, 1,
        {group: [_row(1, 101, 1), _row(2, 202, 43, side=0)], PEER: [_row(2, 202, 43)]},
        payloads={
            (group, 2): (b"wxid_other:\n<msg/>", 0),
            (PEER, 2): (b"wxid_other:\n<msg/>", 0),
        },
    )
    source.spec["peer_ids"] = [PEER]
    candidate, report = _stage(archive, resolve_sender_prefixes=True)
    assert report["message_payload_columns_read"] == 0
    assert report["native_sender_statuses"] == {}
    with sqlite3.connect(candidate) as result:
        assert {row[0] for row in result.execute("SELECT sender_id FROM messages")} == {OWNER, PEER}


def test_metadata_only_group_resources_preserve_default_contract(archive):
    group = "123456789@chatroom"
    _source(
        archive, 1, {group: [_row(1, 101, 1), _row(2, 202, 43)]},
        payloads={(group, 2): (b"wxid_author:\n<msg/>", 0)},
    )
    candidate, report = _stage(archive)
    assert report["message_payload_columns_read"] == 0
    assert report["resolve_sender_prefixes"] is False
    with sqlite3.connect(candidate) as result:
        sender, raw = result.execute("SELECT sender_id,provenance FROM messages").fetchone()
    assert sender is None
    assert "native_resource_sender_resolution" not in json.loads(raw)


@pytest.mark.parametrize("server", [-7, 0, None])
def test_enabled_sender_resolution_preserves_exact_nonpositive_or_absent_server(
    archive, server
):
    group = "123456789@chatroom"
    _source(
        archive, 1, {group: [_row(1, 101, 1), _row(2, server, 43)]},
        payloads={(group, 2): (b"wxid_exact_author:\n<msg/>", 0)},
    )
    candidate, report = _stage(archive, resolve_sender_prefixes=True)
    assert report["native_sender_statuses"] == {"resolved": 1}
    with sqlite3.connect(candidate) as result:
        sender, raw = result.execute("SELECT sender_id,provenance FROM messages").fetchone()
    proof = json.loads(raw)
    expected = None if server is None else str(server)
    assert sender == "wxid_exact_author"
    assert proof["server_id"] is None
    assert proof["recorded_native_metadata"]["MesSvrID"] == expected
    assert proof["native_resource_sender_resolution"]["server_id"] == expected


def test_enabled_sender_resolution_rejects_none_to_zero_raw_metadata_change(
    archive, monkeypatch
):
    group = "123456789@chatroom"
    _source(
        archive, 1, {group: [_row(1, 101, 1), _row(2, None, 43)]},
        payloads={(group, 2): (b"wxid_author:\n<msg/>", 0)},
    )
    resolve = extension.resolve_source_bound_resource_sender

    def mismatched(row, proof, native, **kwargs):
        return resolve(row, proof, native | {"MesSvrID": "0"}, **kwargs)

    monkeypatch.setattr(extension, "resolve_source_bound_resource_sender", mismatched)
    with pytest.raises(ValueError, match="recorded event metadata"):
        _stage(archive, resolve_sender_prefixes=True)
    assert not (archive.root / "candidate.sqlite3").exists()
