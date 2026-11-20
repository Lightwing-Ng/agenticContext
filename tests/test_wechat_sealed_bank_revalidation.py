"""Fresh legacy-seal revalidation preserves sealed banks and revision history.

Code version: v1.0.0-codex.0
"""

from contextlib import closing, ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import wechat_macos_recovery as legacy
from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_macos_recovery_revalidation as revalidation
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_macos_recovery_revalidation as existing
from tests import test_wechat_macos_reparse as repairs


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive


def _ctime_only(path):
    before = bank._identity(path)
    for _attempt in range(3):
        path.chmod(stat.S_IMODE(path.stat().st_mode))
        after = bank._identity(path)
        if before["ctime_ns"] != after["ctime_ns"]:
            break
    assert before["ctime_ns"] != after["ctime_ns"]
    assert {key: value for key, value in before.items() if key != "ctime_ns"} == {
        key: value for key, value in after.items() if key != "ctime_ns"
    }


@pytest.fixture(params=(False, True), ids=("media-absent", "media-present"))
def sealed_case(bank_archive, tmp_path, monkeypatch, request):
    fixture = bank_archive
    media = existing._empty_media(fixture.history) if request.param else None
    staged_legacy = legacy.stage_mac_recovery(fixture.root, fixture.legacy_packet)
    legacy.publish_mac_recovery(fixture.root, staged_legacy)
    legacy_path = fixture.history.with_name("macos_recovery.json")
    old, new = repairs._packets(fixture, "quote")
    fixtures._publish(
        fixture, packets=[fixture.direct, old], name="before-sealed-revision"
    )
    revised, _receipt = fixtures._stage(
        fixture, packets=[new], name="with-sealed-revision"
    )
    bank.publish_mac_recovery_bank(fixture.root, revised, _receipt)
    workstation = tmp_path / "private-workstation"
    workstation.mkdir(mode=0o700)
    directory = workstation / "revalidation"
    directory.mkdir(mode=0o700)
    source = directory / "reviewed-source.sqlite3"
    shutil.copyfile(fixture.bank_path, source)
    source.chmod(0o600)
    with closing(sqlite3.connect(source)) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM archive_info"))
        revisions = connection.execute("SELECT COUNT(*) FROM projection_revisions").fetchone()[0]
        targets = connection.execute(
            "SELECT COUNT(DISTINCT canonical_id) FROM occurrences WHERE canonical_store='existing_timeline'"
        ).fetchone()[0]
    assert metadata["schema_version"] == "4" and revisions == 1 and targets > 0
    before = {
        path: path.read_bytes()
        for path in (fixture.history, fixture.bank_path, legacy_path, source)
    }
    if media is not None:
        before[media] = media.read_bytes()
    _ctime_only(legacy_path)
    clones = Mock(side_effect=shutil.copyfile)
    monkeypatch.setattr(revalidation, "_clonefile", clones)
    return SimpleNamespace(
        fixture=fixture, source=source, legacy_path=legacy_path, directory=directory,
        candidate=directory / "candidate.sqlite3", preserved=directory / "preserved.sqlite3",
        metadata=metadata, source_sha=hashlib.sha256(before[source]).hexdigest(),
        payload_sha=metadata["payload_sha256"], before=before,
        targets=targets, clones=clones, media=media,
    )


def _validate(case, guard, **changes):
    options = {
        "expected_source_sha256": case.source_sha,
        "expected_payload_sha256": case.payload_sha,
        "publication_guard": guard,
        **changes,
    }
    return revalidation.validate_sealed_mac_recovery_bank_revalidation(
        case.fixture.root, case.source, **options
    )


def _stage(case, guard, **changes):
    options = {
        "preserved_path": case.preserved,
        "expected_source_sha256": case.source_sha,
        "expected_payload_sha256": case.payload_sha,
        "publication_guard": guard,
        **changes,
    }
    return revalidation.stage_sealed_mac_recovery_bank_revalidation(
        case.fixture.root, case.source, case.candidate, **options
    )


def _unchanged(case):
    assert all(path.read_bytes() == content for path, content in case.before.items())


def _logical_contents(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        tables = [row[1] for row in schema if row[0] == "table" and row[1] != "archive_info"]
        data = {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1,2').fetchall() for table in tables}
        metadata = dict(connection.execute("SELECT key,value FROM archive_info"))
    return schema, data, metadata


def _rewrite_legacy(case, mutate):
    before = case.legacy_path.stat()
    packet, _digest = legacy._read(case.legacy_path)
    mutate(packet)
    value = {**packet, "seal_sha256": hashlib.sha256(legacy._json(packet).encode()).hexdigest()}
    raw = (legacy._json(value) + "\n").encode()
    assert len(raw) == before.st_size
    case.legacy_path.write_bytes(raw)
    os.utime(case.legacy_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert case.legacy_path.stat().st_mtime_ns == before.st_mtime_ns
    assert legacy._read(case.legacy_path)[0] == packet
    case.before[case.legacy_path] = raw


def _after_input_update(monkeypatch, case, action):
    connect = sqlite3.connect
    invoked = []

    class Observed(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql.strip().upper().startswith("UPDATE ARCHIVE_INFO SET VALUE="):
                main = next(row[2] for row in super().execute("PRAGMA database_list") if row[1] == "main")
                if Path(main).resolve() == case.candidate.resolve():
                    assert self.in_transaction
                    invoked.append(True)
                    action(self)
            return result

    def observed(*args, **kwargs):
        kwargs["factory"] = Observed
        return connect(*args, **kwargs)

    monkeypatch.setattr(revalidation.sqlite3, "connect", observed)
    return invoked


def test_fresh_sealed_validation_proves_revision_and_dedup_semantics_without_clones(sealed_case):
    case = sealed_case
    paths = set(case.directory.iterdir())
    with closing(sqlite3.connect(case.source)) as connection:
        with pytest.raises(ValueError):
            bank._validate_bank(connection, case.fixture.history)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        report = _validate(case, guard)
    assert report["historical_byte_equivalence"] == "not_established"
    assert report["changed_input"] == "macos_recovery.json"
    assert report["changed_fields"] == ["ctime_ns"]
    assert report["source_bank_sha256"] == case.source_sha
    assert report["source_payload_sha256"] == case.payload_sha
    assert report["source_metadata"] == case.metadata
    assert report["existing_timeline_targets"] == case.targets
    assert report["current_legacy_messages"] == len(case.fixture.legacy_packet["messages"])
    assert report["current_media_events"] == 0 and report["published"] is False
    assert set(case.directory.iterdir()) == paths
    case.clones.assert_not_called()
    _unchanged(case)


def test_owner_local_readable_source_mode_is_preserved(sealed_case):
    case = sealed_case
    case.source.chmod(0o644)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        assert _validate(case, guard)["source_payload_sha256"] == case.payload_sha
    assert stat.S_IMODE(case.source.stat().st_mode) == 0o644
    _unchanged(case)


def test_two_outputs_change_only_input_metadata_and_publish_through_normal_cas(sealed_case, monkeypatch):
    case = sealed_case
    validator = Mock(wraps=revalidation.validate_sealed_mac_recovery_bank_revalidation)
    monkeypatch.setattr(revalidation, "validate_sealed_mac_recovery_bank_revalidation", validator)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        receipt = _stage(case, guard)
        assert validator.call_count == 1
        assert [tuple(call.args) for call in case.clones.call_args_list] == [
            (case.source, case.preserved), (case.source, case.candidate),
        ]
        assert case.preserved.read_bytes() == case.before[case.source]
        assert stat.S_IMODE(case.preserved.stat().st_mode) == 0o400
        assert stat.S_IMODE(case.candidate.stat().st_mode) == 0o600
        source_schema, source_rows, source_info = _logical_contents(case.source)
        schema, rows, info = _logical_contents(case.candidate)
        assert schema == source_schema and rows == source_rows
        assert {key: value for key, value in info.items() if key != "input_identities"} == {
            key: value for key, value in source_info.items() if key != "input_identities"
        }
        assert json.loads(info["input_identities"]) == {
            key: value for key, value in bank._inputs(case.fixture.history).items()
            if key != case.fixture.bank_path.name
        }
        with closing(sqlite3.connect(case.candidate)) as connection:
            bank._validate_bank(connection, case.fixture.history)
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        _unchanged(case)
        bank.publish_mac_recovery_bank(
            case.fixture.root, case.candidate, receipt, publication_guard=guard
        )
    assert not case.candidate.exists()
    assert case.source.read_bytes() == case.preserved.read_bytes() == case.before[case.source]
    assert case.fixture.history.read_bytes() == case.before[case.fixture.history]
    assert case.legacy_path.read_bytes() == case.before[case.legacy_path]


@pytest.mark.parametrize("defect", ("source_sha", "payload_sha", "history", "no_drift"))
def test_bad_receipts_or_unsupported_input_drift_fail_before_cloning(sealed_case, defect):
    case = sealed_case
    options = {}
    if defect == "source_sha":
        options["expected_source_sha256"] = "0" * 64
    elif defect == "payload_sha":
        options["expected_payload_sha256"] = "0" * 64
    elif defect == "history":
        _ctime_only(case.fixture.history)
    else:
        current = bank._inputs(case.fixture.history)
        with closing(sqlite3.connect(case.source)) as connection:
            connection.execute("UPDATE archive_info SET value=? WHERE key='input_identities'", (
                bank._json({key: value for key, value in current.items() if key != case.fixture.bank_path.name}),
            ))
            connection.commit()
        case.before[case.source] = case.source.read_bytes()
        case.source_sha = hashlib.sha256(case.before[case.source]).hexdigest()
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard, **options)
    case.clones.assert_not_called()
    assert not case.candidate.exists() and not case.preserved.exists()
    _unchanged(case)


@pytest.mark.parametrize("guard_kind", ("missing", "forged", "expired"))
def test_sealed_revalidation_requires_an_active_publication_capability(sealed_case, guard_kind):
    case = sealed_case
    if guard_kind == "expired":
        with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
            pass
    else:
        guard = None if guard_kind == "missing" else SimpleNamespace()
    with pytest.raises(ValueError):
        _stage(case, guard)
    case.clones.assert_not_called()
    _unchanged(case)


@pytest.mark.parametrize("defect", ("dedup_body", "authentication_header"))
def test_resealed_same_size_legacy_changes_are_freshly_checked_against_semantics(sealed_case, defect):
    case = sealed_case

    def change(packet):
        if defect == "authentication_header":
            packet["source"]["authenticated_page_count"] += 1
        else:
            message = packet["messages"][-1]
            replacement = "X" + message["text"][1:]
            message["text"] = replacement
            message["native_text"]["text"] = replacement
            message["native_text"]["text_components"][0]["text"] = replacement

    _rewrite_legacy(case, change)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard)
    case.clones.assert_not_called()
    assert not case.candidate.exists() and not case.preserved.exists()
    _unchanged(case)


@pytest.mark.parametrize("defect", ("symlink", "hardlink", "mode", "parent", "sidecar", "reader"))
def test_sealed_source_requires_closed_private_independent_resident_file(sealed_case, defect):
    case = sealed_case
    if defect == "symlink":
        actual = case.source.with_name("preserved-source.sqlite3")
        case.source.rename(actual)
        case.source.symlink_to(actual)
    elif defect == "hardlink":
        case.source.with_name("alias-source.sqlite3").hardlink_to(case.source)
    elif defect == "mode":
        case.source.chmod(0o666)
    elif defect == "parent":
        case.source.parent.chmod(0o755)
    elif defect == "sidecar":
        case.source.with_name(case.source.name + "-wal").touch()
    with ExitStack() as stack:
        if defect == "reader":
            reader = stack.enter_context(closing(sqlite3.connect(case.source)))
            reader.execute("SELECT COUNT(*) FROM messages").fetchone()
        with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
            with pytest.raises(ValueError):
                _stage(case, guard)
    case.clones.assert_not_called()
    assert not case.candidate.exists() and not case.preserved.exists()
    _unchanged(case)


def test_insufficient_two_output_budget_never_clones_or_uses_a_fallback(sealed_case, monkeypatch):
    case = sealed_case
    required = 2 * case.source.stat().st_size + 16 * 1024**2
    monkeypatch.setattr(revalidation.shutil, "disk_usage", lambda _path: SimpleNamespace(free=required - 1))
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard)
    case.clones.assert_not_called()
    assert not case.candidate.exists() and not case.preserved.exists()
    _unchanged(case)


def test_clone_failure_preserves_source_and_the_completed_readonly_clone(sealed_case, monkeypatch):
    case = sealed_case
    targets = []

    def clone(source, target):
        targets.append(target)
        if len(targets) == 2:
            raise OSError("Synthetic clone refusal; no fallback")
        shutil.copyfile(source, target)

    monkeypatch.setattr(revalidation, "_clonefile", clone)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(OSError):
            _stage(case, guard)
    assert targets == [case.preserved, case.candidate]
    assert case.preserved.read_bytes() == case.before[case.source]
    assert stat.S_IMODE(case.preserved.stat().st_mode) == 0o400
    assert not case.candidate.exists()
    _unchanged(case)


def test_metadata_validation_failure_rolls_back_the_candidate_input_pin(sealed_case, monkeypatch):
    case = sealed_case
    invoked = _after_input_update(
        monkeypatch, case,
        lambda connection: connection.execute("INSERT INTO archive_info VALUES('unexpected','Synthetic uncommitted metadata')"),
    )
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard)
    assert invoked == [True]
    assert _logical_contents(case.candidate) == _logical_contents(case.source)
    assert case.preserved.read_bytes() == case.before[case.source]
    _unchanged(case)


def test_current_input_drift_before_commit_preserves_all_copied_payloads(sealed_case, monkeypatch):
    case = sealed_case
    invoked = _after_input_update(
        monkeypatch, case, lambda _connection: _ctime_only(case.legacy_path)
    )
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _stage(case, guard)
    assert invoked == [True]
    assert _logical_contents(case.candidate) == _logical_contents(case.source)
    assert case.preserved.read_bytes() == case.before[case.source]
    _unchanged(case)


def test_post_commit_full_validation_failure_keeps_original_and_preserved_exact(
    sealed_case, monkeypatch
):
    case = sealed_case
    validate = bank._validate_bank
    refused = []

    def fail_candidate(connection, *args, **kwargs):
        path = next(row[2] for row in connection.execute("PRAGMA database_list") if row[1] == "main")
        if Path(path).resolve() == case.candidate.resolve():
            refused.append(True)
            assert not connection.in_transaction
            raise ValueError("Synthetic immutable verification refusal")
        return validate(connection, *args, **kwargs)

    monkeypatch.setattr(bank, "_validate_bank", fail_candidate)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError, match="Synthetic immutable verification refusal"):
            _stage(case, guard)
    assert refused == [True]
    schema, rows, metadata = _logical_contents(case.candidate)
    old_schema, old_rows, old_metadata = _logical_contents(case.source)
    assert schema == old_schema and rows == old_rows
    assert metadata["input_identities"] != old_metadata["input_identities"]
    assert case.preserved.read_bytes() == case.before[case.source]
    _unchanged(case)
