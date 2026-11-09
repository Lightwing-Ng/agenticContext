"""Exercise source isolation, exact foreign rows, and reversible publication.

Code version: v1.0.1-codex.0
"""

from contextlib import closing
import hashlib
import json
import shutil
import sqlite3
import sys
import zipfile

import pytest

from app.core import wechat_exchange as codec
from app.core import wechat_exchange_import as exchange
from app.core.wechat_archive import (
    _SCHEMA,
    _file_identity,
    WechatArchiveRepository,
    wechat_archive_path,
    wechat_history_partition_fingerprint,
)
from app.core.wechat_shared_storage import _install as install_shared_storage


SOURCE = "ios-" + hashlib.md5(b"wxid_claimed_owner").hexdigest()
SESSION = "wechat:" + "1" * 64
OTHER_SESSION = "wechat:" + "2" * 64
MESSAGE = "a" * 64


@pytest.mark.parametrize(
    "returncode,stdout,stderr,allowed",
    [
        (1, "", "", True),
        (0, "p1\ncPython\nf3\nar\n", "", True),
        (0, "p1\ncPython\nf3\naw\n", "", False),
        (0, "p1\ncPython\nf3\nau\n", "", False),
        (0, "p1\ncFileProviderd\nf3\nau\n", "", True),
        (0, "p1\ncfileprovider\nf3\naw\n", "", True),
        (0, "p1\ncfileproviderd\nf3\naw\np2\ncPython\nf4\naw\n", "", False),
        (2, "", "", False),
        (1, "", "inspection unavailable", False),
    ],
)
def test_publication_rejects_writers_and_failed_ownership_inspection(
    tmp_path, monkeypatch, returncode, stdout, stderr, allowed
):
    result = exchange.subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )
    monkeypatch.setattr(exchange.subprocess, "run", lambda *args, **kwargs: result)
    if allowed:
        exchange._no_input_writers(tmp_path / "history.sqlite3")
    else:
        with pytest.raises(RuntimeError):
            exchange._no_input_writers(tmp_path / "history.sqlite3")


def _rows(root, table):
    with closing(sqlite3.connect(wechat_archive_path(root))) as connection:
        return connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()


def _seed(
    root,
    *,
    text="  Literal source\n\n* text  ",
    session=SESSION,
    message=MESSAGE,
    shared=False,
):
    path = wechat_archive_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
            (
                SOURCE,
                "account:wxid_claimed_owner",
                "wxid_claimed_owner",
                "Synthetic owner",
                "Verified archive ID",
                "iPhone archive",
                "/donor-only/private/source.sqlite",
                "/donor-only/private/archive.zip",
            ),
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session,
                SOURCE,
                "account:wxid_claimed_owner",
                "wxid_peer",
                "Literal source contact 2️⃣",
                "direct",
                1,
                0.0,
                0.0,
                "",
                "",
                text,
            ),
        )
        proof = '{ "source_snapshot": 1, "source_locator": {"path":"/donor-only/private/do-not-open"}, "authority":"native_account_owner", "text_extraction_kind":"quoted_reply", "message_type":49, "quoted_reference":{"text":"Quoted original"}, "text_components":[{"kind":"text","text":"Exact nested text"}] }'
        values = (
            message,
            session,
            1,
            "wxid_peer",
            "Literal source contact 2️⃣",
            "unknown",
            text,
            0.0,
            "",
            "Unknown time",
            "Source clock; timezone unknown",
            proof,
            '[ "original_flag" ]',
            text.casefold(),
        )
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
        )
        connection.execute(
            "INSERT INTO archive_info VALUES('manifest',?)",
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "messages": 1,
                        "sources": [{"source_id": SOURCE}],
                        "retained": "exact local metadata",
                    }
                ),
            ),
        )
        if shared:
            install_shared_storage(connection)
        connection.commit()
    return path


def _package(tmp_path, *, donor=None, name="package.wechatx", session=SESSION):
    donor = donor or tmp_path / "donor"
    if not wechat_archive_path(donor).exists():
        _seed(donor)
    path = tmp_path / name
    codec.export_conversation(donor, session, path)
    return path


@pytest.fixture(autouse=True)
def _synthetic_clone_backend(monkeypatch):
    if sys.platform != "darwin":
        monkeypatch.setattr(exchange, "_clonefile", shutil.copyfile)


def _import(root, path, **options):
    return exchange.import_package(root, path, minimum_free_bytes=0, **options)


def test_preview_is_exact_and_does_not_publish_or_touch_local_evidence(tmp_path):
    package = _package(tmp_path)
    target = tmp_path / "target"
    path = _seed(target, text="Local evidence must survive", shared=True)
    before = path.read_bytes()
    original = {
        name: _rows(target, name)
        for name in ("partitions", "conversations", "messages")
    }
    report = _import(target, package, apply=False)
    assert report["inserted_messages"] == 1 and report["added_partitions"] == 1
    assert report["changed"] and not report["published"]
    assert path.read_bytes() == before
    assert original == {name: _rows(target, name) for name in original}
    assert not list(path.parent.glob(".wechat-exchange-*"))


def test_import_is_source_scoped_preserves_exact_fields_and_replays_zero(tmp_path):
    package = _package(tmp_path)
    target = tmp_path / "target"
    _seed(target, text="Local protected text")
    local = {
        name: _rows(target, name)
        for name in ("partitions", "conversations", "messages")
    }
    imported = _import(target, package)
    assert imported["published"] and imported["inserted_messages"] == 1
    assert imported["messages_before"] == 1 and imported["messages_after"] == 2
    sources = _rows(target, "partitions")
    foreign = next(row for row in sources if row[0].startswith("xchg-"))
    assert foreign[1] == "source:" + foreign[0]
    assert foreign[2] is None and foreign[4] == "Imported origin claim"
    assert foreign[6:] == ("", "")
    with closing(sqlite3.connect(wechat_archive_path(target))) as connection:
        for table, rows in local.items():
            for row in rows:
                assert (
                    connection.execute(
                        f'SELECT * FROM "{table}" WHERE id=?', (row[0],)
                    ).fetchone()
                    == row
                )
        stored = connection.execute(
            "SELECT row_payload FROM exchange_original_rows WHERE table_name='history.messages'"
        ).fetchone()[0]
        exact = exchange.decode_preserved_row(stored)
        donor = _rows(tmp_path / "donor", "messages")[0]
        assert tuple(exact.values()) == donor
        row = connection.execute(
            "SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.partition_id=?",
            (foreign[0],),
        ).fetchone()
        assert row[5] == "unknown" and row[6] == donor[6]
        assert row[8:11] == donor[8:11]
        proof = json.loads(row[11])
        assert "source_snapshot" not in proof and "source_locator" not in proof
        assert proof["exchange"]["original_provenance_raw"] == donor[11]
        assert proof["exchange"]["local_resources_available"] is False
        assert proof["text_components"][0]["text"] == "Exact nested text"
        assert proof["quoted_reference"]["text"] == "Quoted original"
    before = wechat_archive_path(target).read_bytes()
    again = _import(target, package)
    assert (
        again["replayed"] and not again["changed"] and again["inserted_messages"] == 0
    )
    assert wechat_archive_path(target).read_bytes() == before


def test_same_origin_changed_record_preserves_both_variants_and_local_names(tmp_path):
    donor = tmp_path / "donor"
    _seed(donor, text="Original foreign record")
    first = _package(tmp_path, donor=donor, name="first.wechatx")
    target = tmp_path / "target"
    _import(target, first)
    with closing(sqlite3.connect(wechat_archive_path(target))) as connection:
        connection.execute("UPDATE partitions SET account_label='My local label'")
        connection.execute("UPDATE conversations SET title='My local remark'")
        connection.commit()
    with closing(sqlite3.connect(wechat_archive_path(donor))) as connection:
        connection.execute(
            "UPDATE messages SET text='Conflicting new record',search_text='conflicting new record'"
        )
        connection.commit()
    second = _package(tmp_path, donor=donor, name="second.wechatx")
    result = _import(target, second)
    assert result["inserted_messages"] == 1 and result["conflict_variants"] >= 1
    assert {row[6] for row in _rows(target, "messages")} == {
        "Original foreign record",
        "Conflicting new record",
    }
    assert _rows(target, "partitions")[0][3] == "My local label"
    assert _rows(target, "conversations")[0][4] == "My local remark"
    with closing(sqlite3.connect(wechat_archive_path(target))) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM exchange_original_rows WHERE table_name='history.messages'"
            ).fetchone()[0]
            == 2
        )


def _sidecar(root, name, *, supplemental=False):
    path = wechat_archive_path(root).with_name(name)
    with closing(sqlite3.connect(wechat_archive_path(root))) as source:
        fingerprint = wechat_history_partition_fingerprint(source)
    info = {
        "schema_version": "1",
        "history_file_identity": json.dumps(
            _file_identity(wechat_archive_path(root)),
            sort_keys=True,
            separators=(",", ":"),
        ),
        "history_partitions_sha256": fingerprint,
        "sealed_proof": "keep exactly",
    }
    if supplemental:
        info.update(
            supplemental_metadata="1",
            canonical_file_identity=json.dumps(
                _file_identity(path.with_name("resource_references.sqlite3")),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            "CREATE TABLE archive_info(key TEXT PRIMARY KEY,value TEXT NOT NULL);CREATE TABLE protected_resources(id TEXT PRIMARY KEY,body BLOB NOT NULL);"
        )
        connection.executemany("INSERT INTO archive_info VALUES(?,?)", info.items())
        connection.execute(
            "INSERT INTO protected_resources VALUES('original',?)",
            (b"untouched\x00resource evidence",),
        )
        connection.commit()
    return path


def test_publication_repins_all_current_resource_sidecars_without_changing_rows(
    tmp_path,
):
    package = _package(tmp_path)
    target = tmp_path / "target"
    _seed(target)
    paths = [
        _sidecar(target, "resource_references.sqlite3"),
        _sidecar(target, "resource_revalidated.sqlite3", supplemental=True),
        _sidecar(target, "media_events.sqlite3"),
    ]
    result = _import(target, package)
    assert result["published"] and len(result["sidecars"]) == 3
    history = wechat_archive_path(target)
    with closing(sqlite3.connect(history)) as connection:
        fingerprint = wechat_history_partition_fingerprint(connection)
    for path in paths:
        with closing(sqlite3.connect(path)) as connection:
            info = dict(connection.execute("SELECT * FROM archive_info"))
            assert json.loads(info["history_file_identity"]) == _file_identity(history)
            assert info["history_partitions_sha256"] == fingerprint
            assert info["sealed_proof"] == "keep exactly"
            assert connection.execute(
                "SELECT * FROM protected_resources"
            ).fetchall() == [("original", b"untouched\x00resource evidence")]
            if path.name == "resource_revalidated.sqlite3":
                assert json.loads(info["canonical_file_identity"]) == _file_identity(
                    paths[0]
                )


def test_failed_published_readback_rolls_back_every_original_and_resource_pin(
    tmp_path, monkeypatch
):
    package = _package(tmp_path)
    target = tmp_path / "target"
    _seed(target, text="Retained baseline")
    paths = [
        _sidecar(target, "resource_references.sqlite3"),
        _sidecar(target, "resource_revalidated.sqlite3", supplemental=True),
        _sidecar(target, "media_events.sqlite3"),
    ]
    originals = {
        name: _rows(target, name)
        for name in ("messages", "partitions", "conversations", "archive_info")
    }

    def fail(*args):
        raise ValueError("Synthetic readback failure")

    monkeypatch.setattr(exchange, "_readback", fail)
    with pytest.raises(ValueError, match="Synthetic readback failure"):
        _import(target, package)
    assert {name: _rows(target, name) for name in originals} == originals
    for path in paths:
        with closing(sqlite3.connect(path)) as connection:
            info = dict(connection.execute("SELECT * FROM archive_info"))
            assert json.loads(info["history_file_identity"]) == _file_identity(
                wechat_archive_path(target)
            )
            assert connection.execute(
                "SELECT * FROM protected_resources"
            ).fetchall() == [("original", b"untouched\x00resource evidence")]
            if path.name == "resource_revalidated.sqlite3":
                assert json.loads(info["canonical_file_identity"]) == _file_identity(
                    paths[0]
                )
    assert not list(wechat_archive_path(target).parent.glob(".wechat-exchange-*"))


def test_changed_target_after_preview_is_rejected_before_staging(tmp_path):
    package = _package(tmp_path)
    target = tmp_path / "target"
    path = _seed(target)
    preview = _import(target, package, apply=False)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE partitions SET account_label='Changed after preview'"
        )
        connection.commit()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="changed after preview"):
        _import(target, package, expected_state=preview["expected_state"])
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["traversal", "hash", "link"])
def test_untrusted_package_never_writes_existing_archive(tmp_path, damage):
    package = _package(tmp_path)
    with zipfile.ZipFile(package) as source:
        members = [(item, source.read(item.filename)) for item in source.infolist()]
    broken = tmp_path / "broken.wechatx"
    with zipfile.ZipFile(broken, "w") as archive:
        for item, payload in members:
            if damage == "hash" and item.filename != "manifest.json":
                payload = payload + b"changed"
            archive.writestr(item, payload)
        if damage == "traversal":
            archive.writestr("../outside", b"must never extract")
        if damage == "link":
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            archive.writestr(link, "/private/forbidden")
    target = tmp_path / "target"
    path = _seed(target)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        _import(target, broken)
    assert path.read_bytes() == before
    assert not (tmp_path / "outside").exists()
    assert not list(path.parent.glob(".wechat-exchange-*"))


def test_preserved_export_does_not_include_unrelated_chat_from_same_origin(tmp_path):
    donor = tmp_path / "donor"
    _seed(donor, text="Selected private conversation")
    first = _package(tmp_path, donor=donor, name="selected.wechatx")
    with closing(sqlite3.connect(wechat_archive_path(donor))) as connection:
        conversation = list(
            connection.execute("SELECT * FROM conversations").fetchone()
        )
        conversation[0], conversation[3], conversation[4] = (
            OTHER_SESSION,
            "wxid_other_peer",
            "Unrelated private conversation",
        )
        connection.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", conversation
        )
        message = list(connection.execute("SELECT * FROM messages").fetchone())
        message[0], message[1], message[6] = (
            "b" * 64,
            OTHER_SESSION,
            "Secret unrelated text",
        )
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", message
        )
        connection.commit()
    second = _package(
        tmp_path, donor=donor, name="unrelated.wechatx", session=OTHER_SESSION
    )
    target = tmp_path / "target"
    _import(target, first)
    _import(target, second)
    with closing(sqlite3.connect(wechat_archive_path(target))) as connection:
        selected = connection.execute(
            "SELECT id,partition_id FROM conversations WHERE title!='Unrelated private conversation'"
        ).fetchone()
        rows = list(
            exchange.preserved_rows(
                connection, [selected[1]], conversation_ids=[selected[0]]
            )
        )
        originals = [
            exchange.decode_preserved_row(row["row_payload"])
            for row in rows
            if row["table_name"] == "history.messages"
        ]
        assert [row["text"] for row in originals] == ["Selected private conversation"]
        manifests = list(
            exchange.preserved_manifests(
                connection, [selected[1]], conversation_ids=[selected[0]]
            )
        )
        assert (
            len(manifests) == 1
            and manifests[0]["package_id"] == codec.read_package(first).package_id
        )


def test_typed_preservation_round_trips_blob_large_int_and_unknown_values():
    value = {
        "id": 2**63 - 1,
        "null_value": None,
        "real_value": -0.0,
        "blob": b"\xff\x00\xfe",
        "text": " \n2️⃣\n ",
    }
    import zlib

    payload = zlib.compress(exchange._encoded_row(value, value))
    decoded = exchange.decode_preserved_row(payload)
    assert decoded == value and decoded["real_value"].hex() == "-0x0.0p+0"
    with pytest.raises(ValueError):
        exchange.decode_preserved_row(payload + b"trailing untrusted frame")


def test_imported_literal_text_is_searchable_through_shared_storage_query(tmp_path):
    package = _package(tmp_path)
    target = tmp_path / "target"
    _seed(target, text="Protected original", shared=True)
    _import(target, package)
    with closing(sqlite3.connect(wechat_archive_path(target))) as connection:
        session, account = connection.execute(
            "SELECT c.id,c.account_key FROM conversations c JOIN partitions p ON p.id=c.partition_id WHERE p.id LIKE 'xchg-%'"
        ).fetchone()
    result = WechatArchiveRepository(target).query(
        account=account, session=session, query="Literal"
    )
    assert result["count"] == 1
    assert result["messages"][0]["text"] == "  Literal source\n\n* text  "


def test_reexported_package_retains_original_origin_rows_without_identity_promotion(
    tmp_path,
):
    first = _package(tmp_path)
    middle, target = tmp_path / "middle", tmp_path / "target"
    _import(middle, first)
    with closing(sqlite3.connect(wechat_archive_path(middle))) as connection:
        session = connection.execute("SELECT id FROM conversations").fetchone()[0]
    second = _package(
        tmp_path, donor=middle, name="reexported.wechatx", session=session
    )
    result = _import(target, second)
    assert result["published"]
    original = codec.read_package(first)
    with closing(sqlite3.connect(wechat_archive_path(target))) as connection:
        rows = connection.execute(
            "SELECT row_payload FROM exchange_original_rows WHERE origin_id=? AND table_name='history.messages'",
            (original.manifest["origin_id"],),
        ).fetchall()
        assert len(rows) == 1
        assert exchange.decode_preserved_row(rows[0][0]) == next(
            original.iter_rows("history.messages")
        )
        assert all(
            row[0] is None and row[1] == "Imported origin claim"
            for row in connection.execute(
                "SELECT account_id,identity_status FROM partitions"
            )
        )


def test_unchanged_imports_reexport_without_wrappers_across_multiple_generations(
    tmp_path,
):
    first = _package(tmp_path)
    baseline = codec.read_package(first)
    expected = {
        name: (row["row_count"], row["rows_sha256"])
        for name, row in baseline.tables.items()
    }
    package = first
    for generation in range(3):
        store = tmp_path / f"generation-{generation}"
        _import(store, package)
        with closing(sqlite3.connect(wechat_archive_path(store))) as connection:
            session = connection.execute("SELECT id FROM conversations").fetchone()[0]
            source = exchange.fast_forward_source(connection, session)
            assert source is not None
            assert source.selected_original_session_id == SESSION
        package = tmp_path / f"generation-{generation}.wechatx"
        codec.export_conversation(store, session, package)
        next_package = codec.read_package(package)
        assert next_package.manifest["origin_id"] == baseline.manifest["origin_id"]
        assert not any(name.startswith("preserved.") for name in next_package.tables)
        assert {
            name: (row["row_count"], row["rows_sha256"])
            for name, row in next_package.tables.items()
        } == expected


@pytest.mark.parametrize("change", ["title", "message", "added_message", "label"])
def test_fast_forward_refuses_local_edits_without_dropping_them(tmp_path, change):
    package = _package(tmp_path)
    store = tmp_path / "target"
    _import(store, package)
    with closing(sqlite3.connect(wechat_archive_path(store))) as connection:
        session = connection.execute("SELECT id FROM conversations").fetchone()[0]
        if change == "title":
            connection.execute("UPDATE conversations SET title='My local title'")
        elif change == "message":
            connection.execute("UPDATE messages SET text='My local text'")
        elif change == "label":
            connection.execute("UPDATE partitions SET account_label='My local label'")
        else:
            row = list(connection.execute("SELECT * FROM messages").fetchone())
            row[0] = "e" * 64
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
            )
        connection.commit()
        assert exchange.fast_forward_source(connection, session) is None


def test_native_export_still_works_after_foreign_packages_are_imported(tmp_path):
    package = _package(tmp_path)
    target = tmp_path / "target"
    _seed(target, text="Native selected conversation")
    _import(target, package)
    native = _package(tmp_path, donor=target, name="native-after-import.wechatx")
    verified = codec.read_package(native)
    assert not list(verified.iter_rows("preserved.original_rows"))
    assert [row["text"] for row in verified.iter_rows("history.messages")] == [
        "Native selected conversation"
    ]


def test_nested_preserved_row_cannot_claim_a_different_original_key(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(exchange, "fast_forward_source", lambda *args, **kwargs: None)
    first = _package(tmp_path)
    middle = tmp_path / "middle"
    _import(middle, first)
    with closing(sqlite3.connect(wechat_archive_path(middle))) as connection:
        session = connection.execute("SELECT id FROM conversations").fetchone()[0]
    original_rows = exchange.preserved_rows

    def changed_key(connection, partitions, *, conversation_ids):
        for row in original_rows(
            connection, partitions, conversation_ids=conversation_ids
        ):
            yield (
                {**row, "original_key": "a false key"}
                if row["table_name"] == "history.messages"
                else row
            )

    monkeypatch.setattr(exchange, "preserved_rows", changed_key)
    forged = _package(
        tmp_path, donor=middle, name="forged-key.wechatx", session=session
    )
    target = tmp_path / "target"
    path = _seed(target, text="Protected local evidence")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="exact origin schema and original key"):
        _import(target, forged)
    assert path.read_bytes() == before


def test_disk_budget_uses_expanded_rows_and_refuses_before_writing_original(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    package = _package(tmp_path)
    target = tmp_path / "target"
    path = _seed(target)
    before = path.read_bytes()
    monkeypatch.setattr(
        exchange.shutil, "disk_usage", lambda path: SimpleNamespace(free=16 * 1024**2)
    )
    with pytest.raises(OSError, match="disk-space budget"):
        _import(target, package)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".wechat-exchange-*"))


def test_active_maintenance_owner_refuses_import_without_writes(tmp_path):
    from app.core.job_lock import (
        CACHE_TASK_LOCK_NAME,
        CacheMaintenanceLock,
        CacheTaskLock,
    )

    package = _package(tmp_path)
    target = tmp_path / "target"
    path = _seed(target)
    before = path.read_bytes()
    lock = CacheMaintenanceLock(CacheTaskLock(target / CACHE_TASK_LOCK_NAME))
    assert lock.acquire("Synthetic conflicting owner")
    try:
        with pytest.raises(RuntimeError, match="maintenance boundary"):
            _import(target, package)
    finally:
        lock.release()
    assert path.read_bytes() == before


def test_canonical_exclusion_policy_is_honored_and_preview_pins_it(tmp_path):
    package = _package(tmp_path)
    target = tmp_path / "target"
    path = _seed(target)
    preview = _import(target, package, apply=False)
    path.with_name("exclusions.json").write_text(
        json.dumps({"schema_version": 1, "sources": [{"source_id": SOURCE}]})
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="changed after preview"):
        _import(target, package, expected_state=preview["expected_state"])
    with pytest.raises(ValueError, match="explicitly excluded source"):
        _import(target, package)
    assert path.read_bytes() == before


def test_only_frozen_canonical_timeline_creates_bubbles_and_retains_forwarded_clocks(
    tmp_path, monkeypatch
):
    donor = tmp_path / "donor"
    _seed(donor)
    with closing(sqlite3.connect(wechat_archive_path(donor))) as connection:
        row = list(connection.execute("SELECT * FROM messages").fetchone())
        row[0], row[2], row[7] = "b" * 64, 2, 1.0
        connection.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
        )
        connection.execute("UPDATE conversations SET message_count=2")
        connection.commit()

    def frozen(collector, root, family, stack):
        row = next(
            row for row in collector.rows("history.messages") if row["id"] == MESSAGE
        )
        leaf = {
            "id": "c" * 64,
            "component_path": [1, 0],
            "sender_id": "foreign-sender",
            "sender_label": "Literal child label",
            "viewer_display_name": "Foreign child preference",
            "text": "Forwarded child source",
            "timestamp_value": "2026-11-07T00:12:03+00:00",
            "timestamp_label": "7 Nov 2026 08:12:03",
            "time_basis": "Proven source-local clock",
            "source_clock": "2026-11-07 08:12:03",
            "provenance": {"source_locator": {"path": "/donor-only/receipt"}},
        }
        metadata = {
            "schema_version": 1,
            "authority": "frozen_external_presentation",
            "source_message_id": row["id"],
            "source_provenance": row["provenance"],
            "forwarded_items": [leaf],
            "event_members": [{"id": MESSAGE}, {"id": "b" * 64}],
        }
        collector.add(
            "exchange.presentations",
            [
                {
                    "message_id": row["id"],
                    "viewer_display_name": "Foreign viewing preference",
                    "metadata_json": codec._json(metadata),
                }
            ],
            [
                (key, "TEXT")
                for key in ("message_id", "viewer_display_name", "metadata_json")
            ],
            ["message_id"],
        )
        return [{**row, "origin_table": "history.messages"}], {
            "snapshot": "synthetic canonical event"
        }

    monkeypatch.setattr(codec, "_validated_timeline", frozen)
    package = _package(tmp_path, donor=donor)
    target = tmp_path / "target"
    result = _import(target, package)
    assert result["inserted_messages"] == 1
    with closing(sqlite3.connect(wechat_archive_path(target))) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM exchange_original_rows WHERE table_name='history.messages'"
            ).fetchone()[0]
            == 2
        )
        row = connection.execute(
            "SELECT sender_label,provenance FROM messages"
        ).fetchone()
        assert row[0] == "Foreign viewing preference"
        projection = json.loads(row[1])["exchange"]["frozen_presentation"]
        assert (
            projection["forwarded_items"][0]["timestamp_label"] == "7 Nov 2026 08:12:03"
        )
        assert projection["forwarded_items"][0]["component_path"] == [1, 0]
        assert len(projection["event_members"]) == 2


@pytest.mark.parametrize(
    "field,value",
    [("conversation_id", OTHER_SESSION), ("text", "Forged same-scope original text")],
)
def test_nested_preserved_message_remains_bound_to_original_manifest(
    tmp_path, monkeypatch, field, value
):
    monkeypatch.setattr(exchange, "fast_forward_source", lambda *args, **kwargs: None)
    import zlib

    first = _package(tmp_path)
    middle = tmp_path / "middle"
    _import(middle, first)
    with closing(sqlite3.connect(wechat_archive_path(middle))) as connection:
        session = connection.execute("SELECT id FROM conversations").fetchone()[0]
    original_rows, original_memberships = (
        exchange.preserved_rows,
        exchange.preserved_memberships,
    )
    changed_hashes = {}

    def changed_scope(connection, partitions, *, conversation_ids):
        for row in original_rows(
            connection, partitions, conversation_ids=conversation_ids
        ):
            if row["table_name"] == "history.messages":
                decoded = exchange.decode_preserved_row(row["row_payload"])
                decoded[field] = value
                encoded = exchange._encoded_row(decoded, decoded)
                changed = hashlib.sha256(encoded).hexdigest()
                changed_hashes[row["row_sha256"]] = changed
                yield {
                    **row,
                    "row_sha256": changed,
                    "row_payload": zlib.compress(encoded),
                }
            else:
                yield row

    def changed_membership(connection, partitions, *, conversation_ids):
        for row in original_memberships(
            connection, partitions, conversation_ids=conversation_ids
        ):
            yield {
                **row,
                "row_sha256": changed_hashes.get(row["row_sha256"], row["row_sha256"]),
            }

    monkeypatch.setattr(exchange, "preserved_rows", changed_scope)
    monkeypatch.setattr(exchange, "preserved_memberships", changed_membership)
    forged = _package(
        tmp_path, donor=middle, name="forged-scope.wechatx", session=session
    )
    target = tmp_path / "target"
    path = _seed(target, text="Protected local original")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        _import(target, forged)
    assert path.read_bytes() == before
