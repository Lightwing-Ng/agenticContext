"""Only newly owned clone descriptors receive document tracking before byte proof.

Code version: v1.0.0-codex.0
"""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_macos_recovery_revalidation as revalidation
from tests import test_wechat_archived_bank_revalidation as archived
from tests import test_wechat_macos_recovery_bank as fixtures
from tests import test_wechat_revalidation_hash_retry as hashes
from tests import test_wechat_sealed_bank_revalidation as sealed


mac_archive = fixtures.mac_archive
bank_archive = fixtures.bank_archive
sealed_case = sealed.sealed_case
archived_case = archived.archived_case
_TRACKED = 0x40


@pytest.fixture
def preparation_case(bank_archive, tmp_path):
    fixture = bank_archive
    fixtures._publish(fixture, packets=[fixture.direct])
    workstation = tmp_path / "private-tracking-workstation"
    workstation.mkdir(mode=0o700)
    directory = workstation / "private-run"
    directory.mkdir(mode=0o700)
    source = directory / "reviewed-source.sqlite3"
    candidate = directory / "new-clone.sqlite3"
    for path in (source, candidate):
        shutil.copyfile(fixture.bank_path, path)
        path.chmod(0o600)
    os.chflags(candidate, 0)
    archive = directory / "protected-preservation.zst"
    receipt = directory / "protected-preservation.receipt.json"
    archive.write_bytes(b"Synthetic protected compressed artifact")
    receipt.write_bytes(b"{}")
    for path in (archive, receipt):
        path.chmod(0o600)
    protected = (source, fixture.history, fixture.bank_path, archive, receipt)
    return SimpleNamespace(
        fixture=fixture, directory=directory, source=source, candidate=candidate,
        archive=archive, receipt=receipt,
        body=source.read_bytes(),
        protected_before={path: (path.read_bytes(), path.stat().st_flags) for path in protected},
    )


def _prepare(case, guard, *, audit=None, path=None, mode=0o600):
    return revalidation._prepare_new_clone_tracking(
        path or case.candidate, case.source, case.fixture.history,
        bank._inputs(case.fixture.history), guard, mode=mode,
        audit=[] if audit is None else audit, protected_paths=(case.archive, case.receipt),
    )


def _protected_unchanged(case):
    for path, (body, flags) in case.protected_before.items():
        assert path.read_bytes() == body
        assert path.stat().st_flags == flags


def _assert_record(record, path, source):
    assert record["phase"] == "new_clone_tracking_preparation"
    assert record["basis"] == "new_owned_clone_document_id_flag_preparation_v1"
    assert record["path"] == str(path)
    assert record["source_path"] == str(source)
    assert record["flag_mask"] == _TRACKED
    assert record["flags_after"] == record["flags_before"] | _TRACKED
    assert type(record["native_called"]) is bool
    before, after = record["before_identity"], record["after_identity"]
    assert set(before) == set(after) == {
        "dev", "ino", "size", "mtime_ns", "ctime_ns", "mode", "uid", "gid", "nlink", "flags",
    }
    assert before["flags"] == record["flags_before"]
    assert after["flags"] == record["flags_after"]
    assert {key: value for key, value in before.items() if key not in {"ctime_ns", "flags"}} == {
        key: value for key, value in after.items() if key not in {"ctime_ns", "flags"}
    }
    assert "file_sha256" not in record


@pytest.mark.parametrize("initial_flags,mode", (
    (0, 0o600), (stat.UF_NODUMP, 0o600), (_TRACKED, 0o600),
    (_TRACKED | stat.UF_NODUMP, 0o600), (0, 0o400),
))
def test_native_preparation_sets_only_tracking_and_preserves_existing_flags(
    preparation_case, monkeypatch, initial_flags, mode
):
    case = preparation_case
    case.candidate.chmod(mode)
    os.chflags(case.candidate, initial_flags)
    native = Mock(wraps=revalidation._native_fchflags)
    monkeypatch.setattr(revalidation, "_native_fchflags", native)
    audit = []
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        record = _prepare(case, guard, audit=audit, mode=mode)
        value, _identity = revalidation._closed_sha(
            case.candidate, case.fixture.history, bank._inputs(case.fixture.history), guard,
            modes={mode}, expected_sha256=hashlib.sha256(case.body).hexdigest(),
        )
    assert audit == [record]
    _assert_record(record, case.candidate, case.source)
    assert record["flags_before"] == initial_flags
    assert native.call_count == int(record["native_called"])
    if initial_flags & _TRACKED == 0:
        native.assert_called_once()
        assert native.call_args.args[1] == initial_flags | _TRACKED
    assert value == hashlib.sha256(case.body).hexdigest()
    assert case.candidate.read_bytes() == case.body
    _protected_unchanged(case)


def test_preparation_uses_a_readonly_nofollow_descriptor_and_closes_it(preparation_case, monkeypatch):
    case = preparation_case
    original = os.open
    opened = []

    def observe(path, flags, *args, **kwargs):
        descriptor = original(path, flags, *args, **kwargs)
        if Path(path) == case.candidate:
            opened.append((descriptor, flags))
        return descriptor

    monkeypatch.setattr(revalidation.os, "open", observe)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        _prepare(case, guard)
    assert len(opened) == 1
    descriptor, flags = opened[0]
    assert flags & os.O_NOFOLLOW
    assert flags & os.O_ACCMODE == os.O_RDONLY
    with pytest.raises(OSError):
        os.fstat(descriptor)
    _protected_unchanged(case)


def test_path_swap_cannot_redirect_descriptor_flags_to_a_protected_source(preparation_case, monkeypatch):
    case = preparation_case
    native = revalidation._native_fchflags
    held = case.directory / "held-original-clone.sqlite3"
    descriptors = []

    def swap(descriptor, flags):
        descriptors.append(descriptor)
        assert os.fstat(descriptor).st_ino == case.candidate.stat().st_ino
        case.candidate.rename(held)
        case.candidate.symlink_to(case.source)
        native(descriptor, flags)

    setter = Mock(side_effect=swap)
    monkeypatch.setattr(revalidation, "_native_fchflags", setter)
    audit = []
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _prepare(case, guard, audit=audit)
    setter.assert_called_once()
    assert held.stat().st_flags & _TRACKED
    assert audit == []
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    _protected_unchanged(case)


@pytest.mark.parametrize("target", ("source", "history", "bank", "archive", "receipt"))
def test_protected_named_paths_never_reach_native_flag_mutation(preparation_case, monkeypatch, target):
    case = preparation_case
    paths = {
        "source": case.source, "history": case.fixture.history,
        "bank": case.fixture.bank_path, "archive": case.archive, "receipt": case.receipt,
    }
    native = Mock(side_effect=AssertionError("Protected native flag operation"))
    monkeypatch.setattr(revalidation, "_native_fchflags", native)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _prepare(case, guard, path=paths[target])
    native.assert_not_called()
    _protected_unchanged(case)


@pytest.mark.parametrize("change", ("uid", "mode", "link", "flags", "size", "mtime"))
def test_nontracking_metadata_drift_rejects_preparation(preparation_case, monkeypatch, change):
    case = preparation_case
    native = revalidation._native_fchflags
    original_lstat = Path.lstat
    changed_owner = []

    class OwnerView:
        def __init__(self, value):
            self.value = value

        def __getattr__(self, name):
            return self.value.st_uid + 1 if name == "st_uid" else getattr(self.value, name)

    def lstat(path, *args, **kwargs):
        value = original_lstat(path, *args, **kwargs)
        return OwnerView(value) if path == case.candidate and changed_owner else value

    monkeypatch.setattr(Path, "lstat", lstat)

    def mutate(descriptor, flags):
        native(descriptor, flags)
        before = case.candidate.stat()
        if change == "uid":
            changed_owner.append(True)
        elif change == "mode":
            case.candidate.chmod(0o400)
        elif change == "link":
            case.candidate.with_name("new-hardlink.sqlite3").hardlink_to(case.candidate)
        elif change == "flags":
            os.chflags(case.candidate, flags | stat.UF_NODUMP)
        elif change == "size":
            with case.candidate.open("ab") as stream:
                stream.write(b"X")
        else:
            os.utime(case.candidate, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))

    setter = Mock(side_effect=mutate)
    monkeypatch.setattr(revalidation, "_native_fchflags", setter)
    audit = []
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _prepare(case, guard, audit=audit)
    setter.assert_called_once()
    assert audit == []
    _protected_unchanged(case)


@pytest.mark.parametrize("obstacle", ("reader", "sidecar", "symlink", "source_alias", "public_parent"))
def test_preparation_preserves_private_closed_unaliased_file_gates(preparation_case, monkeypatch, obstacle):
    case = preparation_case
    native = Mock(side_effect=AssertionError("Unverified native flag operation"))
    monkeypatch.setattr(revalidation, "_native_fchflags", native)
    with ExitStack() as stack:
        if obstacle == "reader":
            stack.enter_context(case.candidate.open("rb"))
        elif obstacle == "sidecar":
            Path(str(case.candidate) + "-wal").write_bytes(b"Synthetic WAL")
        elif obstacle == "public_parent":
            case.directory.chmod(0o755)
        else:
            case.candidate.unlink()
            if obstacle == "symlink":
                case.candidate.symlink_to(case.source)
            else:
                case.candidate.hardlink_to(case.source)
        with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
            with pytest.raises(ValueError):
                _prepare(case, guard)
    native.assert_not_called()
    _protected_unchanged(case)


def test_native_failure_has_no_pathname_fallback_or_success_audit(preparation_case, monkeypatch):
    case = preparation_case
    descriptors = []

    def unavailable(descriptor, _flags):
        descriptors.append(descriptor)
        raise ValueError("Synthetic unavailable native operation")

    setter = Mock(side_effect=unavailable)
    monkeypatch.setattr(revalidation, "_native_fchflags", setter)
    audit = []
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _prepare(case, guard, audit=audit)
    setter.assert_called_once()
    assert case.candidate.stat().st_flags == 0
    assert audit == []
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    _protected_unchanged(case)


def test_canonical_input_change_during_native_preparation_rejects_evidence(preparation_case, monkeypatch):
    case = preparation_case
    native = revalidation._native_fchflags

    def drift(descriptor, flags):
        native(descriptor, flags)
        sealed._ctime_only(case.fixture.history)

    monkeypatch.setattr(revalidation, "_native_fchflags", drift)
    audit = []
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _prepare(case, guard, audit=audit)
    assert audit == []
    _protected_unchanged(case)


def test_tracking_flag_changes_during_known_hash_still_fail_without_retry(preparation_case, monkeypatch):
    case = preparation_case
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        _prepare(case, guard)
        passes = hashes._during_real_reads(
            monkeypatch, case.candidate,
            lambda _attempt: os.chflags(case.candidate, case.candidate.stat().st_flags & ~_TRACKED),
        )
        with pytest.raises(ValueError):
            revalidation._closed_sha(
                case.candidate, case.fixture.history, bank._inputs(case.fixture.history), guard,
                modes={0o600}, expected_sha256=hashlib.sha256(case.body).hexdigest(),
            )
    assert len(passes) == 1
    _protected_unchanged(case)


def test_preparation_never_substitutes_for_the_complete_known_hash(archived_case, monkeypatch):
    case = archived_case
    prepare, closed_sha = revalidation._prepare_new_clone_tracking, revalidation._closed_sha
    events = []

    def malformed_clone(source, target):
        shutil.copyfile(source, target)
        before = target.stat()
        body = bytearray(target.read_bytes())
        body[-1] ^= 1
        target.write_bytes(body)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    def preparation(path, *args, **kwargs):
        result = prepare(path, *args, **kwargs)
        events.append("preparation")
        return result

    def full_hash(path, *args, **kwargs):
        if path == case.candidate:
            assert path.stat().st_flags & _TRACKED
            assert kwargs["expected_sha256"] == case.source_sha
            events.append("known_hash")
        return closed_sha(path, *args, **kwargs)

    case.clones.side_effect = malformed_clone
    monkeypatch.setattr(revalidation, "_prepare_new_clone_tracking", preparation)
    monkeypatch.setattr(revalidation, "_closed_sha", full_hash)
    update = Mock(side_effect=AssertionError("Unverified clone reached metadata write"))
    monkeypatch.setattr(revalidation, "_update_controlled_candidate", update)
    flags = {path: path.stat().st_flags for path in (case.source, case.fixture.bank_path, case.archive, case.archive_receipt)}
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            archived._stage(case, guard)
    assert events == ["preparation", "known_hash"]
    update.assert_not_called()
    assert {path: path.stat().st_flags for path in flags} == flags
    archived._unchanged(case)


def test_archived_candidate_preparation_is_a_separate_returned_audit(archived_case):
    case = archived_case
    protected = (case.source, case.fixture.history, case.fixture.bank_path, case.archive, case.archive_receipt)
    flags = {path: path.stat().st_flags for path in protected}
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        receipt = archived._stage(case, guard)
    records = receipt["revalidation"]["clone_tracking_preparations"]
    assert len(records) == 1
    _assert_record(records[0], case.candidate, case.source)
    assert case.candidate.stat().st_flags & _TRACKED
    assert receipt["candidate_sha256"] == hashlib.sha256(case.candidate.read_bytes()).hexdigest()
    assert {path: path.stat().st_flags for path in flags} == flags
    archived._unchanged(case)


def test_preserved_and_candidate_preparations_are_both_returned_without_source_changes(sealed_case):
    case = sealed_case
    protected = (case.source, case.fixture.history, case.fixture.bank_path, case.legacy_path)
    flags = {path: path.stat().st_flags for path in protected}
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        receipt = sealed._stage(case, guard)
    records = receipt["revalidation"]["clone_tracking_preparations"]
    assert {record["path"] for record in records} == {str(case.candidate), str(case.preserved)}
    assert len(records) == 2
    for record in records:
        _assert_record(record, Path(record["path"]), case.source)
    assert case.preserved.stat().st_flags & _TRACKED
    assert stat.S_IMODE(case.preserved.stat().st_mode) == 0o400
    assert receipt["preserved_sha256"] == case.source_sha
    assert case.preserved.read_bytes() == case.before[case.source]
    assert {path: path.stat().st_flags for path in flags} == flags
    sealed._unchanged(case)
