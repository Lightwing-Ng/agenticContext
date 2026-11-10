"""Keep the fixed SQLite lock-byte exclusion explicit in source receipts.

Code version: v1.0.1-codex.0
"""

from copy import deepcopy
import json
import sqlite3

import pytest

from app.core import wechat_mac_refresh_projection as projection
from app.core import wechat_macos_recovery as legacy
from app.core import wechat_macos_recovery_bank as bank
from tests import test_wechat_mac_refresh as refresh_fixtures
from tests import test_wechat_macos_recovery_account_binding as binding_fixtures
from tests import test_wechat_macos_recovery_bank as bank_fixtures


mac_archive = bank_fixtures.mac_archive
bank_archive = bank_fixtures.bank_archive
source_binding = binding_fixtures.source_binding
refresh_archive = refresh_fixtures.refresh_archive


def _source():
    return {
        "source_id": "macos-synthetic-lock-byte",
        "snapshot_id": "synthetic-committed-snapshot",
        "main_sha256": "a" * 64,
        "wal_sha256": "b" * 64,
        "database_relative_path": "db_storage/message/message_0.db",
        "table": "Msg_synthetic",
        "integrity_check": "ok",
        "authenticated_pages": True,
        "page_count": 10,
        "authenticated_page_count": 10,
    }


def _with_lock_byte(source):
    source.update(
        authenticated_pages=False,
        authenticated_content_pages=True,
        lock_byte_page_count=1,
        page_count=262_145,
        authenticated_page_count=262_144,
    )
    return source


def test_legacy_receipts_keep_their_exact_all_physical_pages_contract():
    source = _source()
    legacy._validate_source(source)
    source["authenticated_page_count"] -= 1
    with pytest.raises(ValueError, match="authenticated content pages"):
        legacy._validate_source(source)


def test_fixed_lock_byte_receipt_counts_only_authenticated_content_pages():
    source = _with_lock_byte(_source())
    legacy._validate_source(source)
    assert source["authenticated_pages"] is False
    assert source["authenticated_page_count"] == source["page_count"] - 1


@pytest.mark.parametrize(
    "change",
    [
        {"lock_byte_page_count": True},
        {"lock_byte_page_count": 0},
        {"lock_byte_page_count": 2},
        {"lock_byte_page_count": -1},
        {"lock_byte_page_count": 1.0},
        {"lock_byte_page_count": "1"},
        {"authenticated_content_pages": False},
        {"authenticated_content_pages": 1},
        {"authenticated_pages": True},
        {"authenticated_pages": 0},
        {"page_count": 262_144, "authenticated_page_count": 262_143},
        {"page_count": True},
        {"authenticated_page_count": True},
        {"authenticated_page_count": 262_145},
        {"authenticated_page_count": 262_143},
        {"integrity_check": "failed"},
        {"reserved_page_count": 1},
    ],
)
def test_reserved_receipt_cannot_degrade_into_general_partial_authentication(change):
    source = _with_lock_byte(_source())
    source.update(change)
    with pytest.raises(ValueError):
        legacy._validate_source(source)


@pytest.mark.parametrize(
    "missing",
    [
        {"lock_byte_page_count"},
        {"authenticated_content_pages"},
        {"lock_byte_page_count", "authenticated_content_pages"},
    ],
)
def test_fixed_lock_byte_exception_requires_both_explicit_proof_fields(missing):
    source = _with_lock_byte(_source())
    for key in missing:
        source.pop(key)
    with pytest.raises(ValueError):
        legacy._validate_source(source)


@pytest.mark.parametrize(
    "old_version",
    ["v1.2.0-codex.0", "v1.2.1-codex.0", "v1.2.2-codex.0", "v1.3.0-codex.0"],
)
def test_new_proof_preserves_prior_bank_rows_and_uses_explicit_writer_version(
    bank_archive, old_version
):
    fixture = bank_archive
    bank_fixtures._publish(fixture, packets=[fixture.group], name="ordinary-source")
    with sqlite3.connect(fixture.bank_path) as connection:
        connection.execute(
            "UPDATE archive_info SET value=? WHERE key='code_version'", (old_version,)
        )
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name!='archive_info' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        prior_rows = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in tables
        }
    original_bank = fixture.bank_path.read_bytes()
    original_history = fixture.history.read_bytes()
    packet = deepcopy(fixture.direct)
    _with_lock_byte(packet["source"])
    candidate, _receipt = bank_fixtures._stage(
        fixture, packets=[packet], name="lock-byte-source"
    )
    with sqlite3.connect(candidate) as connection:
        for table, rows in prior_rows.items():
            assert set(rows) <= set(connection.execute(f"SELECT * FROM {table}"))
        metadata = dict(connection.execute("SELECT key,value FROM archive_info"))
        assert metadata["schema_version"] == "3"
        assert metadata["code_version"] == bank.VERSION == "v1.4.0-codex.0"
        proofs = [
            json.loads(row[0])["source"]
            for row in connection.execute("SELECT proof FROM sources")
        ]
        assert packet["source"] in proofs
        assert fixture.group["source"] in proofs
        bank._validate_bank(connection, fixture.history)
    assert fixture.bank_path.read_bytes() == original_bank
    assert fixture.history.read_bytes() == original_history


def test_globally_bound_source_retains_honest_content_proof(source_binding):
    fixture = source_binding
    packet = deepcopy(fixture.packet)
    _with_lock_byte(packet["source"])
    candidate, _receipt = binding_fixtures._stage(fixture, packet)
    with sqlite3.connect(candidate) as connection:
        stored = json.loads(
            connection.execute("SELECT proof FROM sources").fetchone()[0]
        )
    assert stored["source"] == packet["source"]
    assert stored["account_binding"] == packet["account_binding"]
    assert stored["source"]["authenticated_pages"] is False
    assert stored["source"]["authenticated_content_pages"] is True


def test_refresh_projection_preserves_the_exact_content_proof(refresh_archive):
    root, history, session, records = refresh_archive
    original = history.read_bytes()
    scope = projection.resolve_scope(root, session)
    native = refresh_fixtures._database(scope, records)
    _with_lock_byte(native.source)
    try:
        packet, _counts = projection.prepare_packet(root, scope, native)
        assert packet is not None
        for key, value in native.source.items():
            assert packet["source"][key] == value
        candidate = history.with_name("projected-lock-byte-source.sqlite3")
        bank.stage_mac_recovery_bank(
            root, [packet], candidate, account_ids=[refresh_fixtures.OWNER]
        )
        with sqlite3.connect(candidate) as connection:
            stored = json.loads(
                connection.execute("SELECT proof FROM sources").fetchone()[0]
            )
        assert stored["source"] == packet["source"]
    finally:
        native.close()
    assert history.read_bytes() == original
