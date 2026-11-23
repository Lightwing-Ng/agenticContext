"""Known exact bytes permit bounded closed-hash ctime retries before derived writes.

Code version: v1.1.2-codex.0
"""

from contextlib import ExitStack
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import wechat_macos_recovery_bank as bank
from app.core import wechat_macos_recovery_revalidation as revalidation
from app.core.owner_only_files import ensure_owner_only, ensure_owner_only_directory
from tests import test_wechat_archived_bank_revalidation as archived
from tests import test_wechat_sealed_bank_revalidation as sealed


mac_archive = sealed.mac_archive
bank_archive = sealed.bank_archive
sealed_case = sealed.sealed_case
archived_case = archived.archived_case
clone_document_flags = sealed.clone_document_flags


@pytest.fixture
def hash_case(bank_archive, tmp_path, clone_document_flags):
    workstation = tmp_path / "private-hash-workstation"
    workstation.mkdir(mode=0o700)
    ensure_owner_only_directory(workstation)
    directory = workstation / "private-run"
    directory.mkdir(mode=0o700)
    ensure_owner_only_directory(directory)
    path = directory / "synthetic-artifact.bin"
    body = bytes(range(256)) * 256
    path.write_bytes(body)
    ensure_owner_only(path)
    return SimpleNamespace(
        fixture=bank_archive, path=path, body=body,
        sha=hashlib.sha256(body).hexdigest(), history_before=bank_archive.history.read_bytes(),
    )


class _ObservedRead:
    def __init__(self, stream, record, action, attempt):
        self.stream, self.record, self.action, self.attempt = stream, record, action, attempt
        self.changed = False

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *exception):
        return self.stream.__exit__(*exception)

    def read(self, size=-1):
        block = self.stream.read(size)
        self.record["bytes"] += len(block)
        if block and not self.changed:
            self.changed = True
            self.action(self.attempt)
        return block


def _during_real_reads(monkeypatch, path, action):
    original = os.fdopen
    passes = []

    def fdopen(descriptor, *args, **kwargs):
        stream = original(descriptor, *args, **kwargs)
        opened = os.fstat(stream.fileno())
        try:
            target = path.stat()
        except FileNotFoundError:
            return stream
        if (opened.st_dev, opened.st_ino) != (target.st_dev, target.st_ino):
            return stream
        record = {"bytes": 0}
        passes.append(record)
        return _ObservedRead(stream, record, action, len(passes))

    monkeypatch.setattr(revalidation.os, "fdopen", fdopen)
    return passes


def _hash(case, guard, **options):
    return revalidation._closed_sha(
        case.path, case.fixture.history, bank._inputs(case.fixture.history), guard,
        modes={0o600}, **options,
    )


def _assert_audit(records, expected_sha, *, phase=None):
    assert records
    for record in records:
        assert record["basis"] == "candidate_ctime_only_exact_file_sha256_v1"
        assert record["file_sha256"] == expected_sha
        if phase is not None:
            assert record["phase"] == phase
        before, after = record["before_identity"], record["after_identity"]
        assert before["ctime_ns"] != after["ctime_ns"]
        assert {key: value for key, value in before.items() if key != "ctime_ns"} == {
            key: value for key, value in after.items() if key != "ctime_ns"
        }


@pytest.mark.parametrize("unstable_passes", (1, 2))
@pytest.mark.parametrize("mode", (0o600, 0o400))
def test_known_hash_retries_complete_passes_until_a_stable_identical_read(
    hash_case, monkeypatch, unstable_passes, mode
):
    case = hash_case
    case.path.chmod(mode)
    passes = _during_real_reads(
        monkeypatch, case.path,
        lambda attempt: sealed._ctime_only(case.path) if attempt <= unstable_passes else None,
    )
    audit = []
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        value, identity = revalidation._closed_sha(
            case.path, case.fixture.history, bank._inputs(case.fixture.history), guard,
            modes={mode}, expected_sha256=case.sha, audit=audit, phase="synthetic_known_hash",
        )
    assert value == case.sha
    assert identity == bank._identity(case.path)
    assert len(passes) == unstable_passes + 1
    assert all(item["bytes"] == len(case.body) for item in passes)
    assert revalidation._source_mode(case.path.stat()) == mode
    _assert_audit(audit, case.sha, phase="synthetic_known_hash")
    assert case.path.read_bytes() == case.body


def test_three_unstable_complete_passes_fail_without_a_fourth(hash_case, monkeypatch):
    case = hash_case
    passes = _during_real_reads(monkeypatch, case.path, lambda _attempt: sealed._ctime_only(case.path))
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _hash(case, guard, expected_sha256=case.sha)
    assert len(passes) == 3
    assert all(item["bytes"] == len(case.body) for item in passes)


def test_unknown_hash_stays_strict_and_never_retries_ctime(hash_case, monkeypatch):
    case = hash_case
    passes = _during_real_reads(monkeypatch, case.path, lambda _attempt: sealed._ctime_only(case.path))
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _hash(case, guard)
    assert len(passes) == 1


@pytest.mark.parametrize("ctime_race", (False, True))
def test_a_wrong_first_pass_digest_never_authorizes_a_retry(hash_case, monkeypatch, ctime_race):
    case = hash_case
    before = case.path.stat()
    changed = bytes([case.body[0] ^ 1]) + case.body[1:]
    case.path.write_bytes(changed)
    os.utime(case.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    passes = _during_real_reads(
        monkeypatch, case.path,
        lambda _attempt: sealed._ctime_only(case.path) if ctime_race else None,
    )
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _hash(case, guard, expected_sha256=case.sha)
    assert len(passes) == 1
    assert passes[0]["bytes"] == len(case.body)


@pytest.mark.parametrize("change", ("mode", "mtime", "inode", "link", "size", "owner", "flags"))
def test_nonctime_storage_changes_never_receive_a_retry(hash_case, monkeypatch, change, clone_document_flags):
    case = hash_case
    original_lstat = Path.lstat
    simulated_owner = []
    initial_flags = revalidation._hash_storage(case.path.stat(), path=case.path)["flags"]

    class OwnerView:
        def __init__(self, value):
            self.value = value

        def __getattr__(self, name):
            return self.value.st_uid + 1 if name == "st_uid" else getattr(self.value, name)

    def lstat(path, *args, **kwargs):
        value = original_lstat(path, *args, **kwargs)
        return OwnerView(value) if path == case.path and simulated_owner else value

    monkeypatch.setattr(Path, "lstat", lstat)
    replace_denied_while_open = os.name == "nt" and change == "inode"

    def mutate(_attempt):
        before = case.path.stat()
        if change == "mode":
            case.path.chmod(0o400)
        elif change == "mtime":
            os.utime(case.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        elif change == "inode":
            replacement = case.path.with_name("new-synthetic-inode.bin")
            replacement.write_bytes(case.body)
            replacement.chmod(0o600)
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(replacement, case.path)
        elif change == "link":
            case.path.with_name("synthetic-hardlink.bin").hardlink_to(case.path)
        elif change == "size":
            with case.path.open("ab") as stream:
                stream.write(b"X")
        elif change == "owner":
            simulated_owner.append(True)
        else:
            clone_document_flags(case.path, initial_flags ^ stat.UF_NODUMP)

    passes = _during_real_reads(monkeypatch, case.path, mutate)
    try:
        with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
            with pytest.raises(PermissionError if replace_denied_while_open else ValueError) as failure:
                revalidation._closed_sha(
                    case.path, case.fixture.history, bank._inputs(case.fixture.history), guard,
                    modes={0o400, 0o600}, expected_sha256=case.sha,
                )
        assert len(passes) == 1
        if replace_denied_while_open:
            # The open Windows CRT handle prevents the attempted pathname replacement.
            assert failure.value.winerror == 5
            assert case.path.read_bytes() == case.body
    finally:
        if change == "flags":
            clone_document_flags(case.path, initial_flags)


def test_canonical_input_drift_is_rejected_without_a_storage_retry(hash_case, monkeypatch):
    case = hash_case
    passes = _during_real_reads(
        monkeypatch, case.path, lambda _attempt: sealed._ctime_only(case.fixture.history),
    )
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            _hash(case, guard, expected_sha256=case.sha)
    assert len(passes) == 1
    assert case.fixture.history.read_bytes() == case.history_before


@pytest.mark.parametrize("obstacle", ("reader", "sidecar"))
def test_known_hash_keeps_existing_handle_and_sidecar_gates(hash_case, obstacle):
    case = hash_case
    with ExitStack() as stack:
        if obstacle == "reader":
            stack.enter_context(case.path.open("r+b" if os.name == "nt" else "rb"))
        else:
            case.path.with_name(case.path.name + "-wal").write_bytes(b"Synthetic sidecar")
        with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
            with pytest.raises(ValueError):
                _hash(case, guard, expected_sha256=case.sha)


def _records(value):
    if isinstance(value, dict):
        if {"phase", "basis", "before_identity", "after_identity", "file_sha256"} <= value.keys():
            yield value
        for child in value.values():
            yield from _records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _records(child)


def test_preflight_callback_gets_one_isolated_complete_proof_before_any_clone(archived_case):
    case = archived_case
    received = []

    def review(evidence):
        assert not case.candidate.exists()
        case.clones.assert_not_called()
        assert evidence["base_message_admission"]["status"] == "verified"
        assert evidence["source_bank_sha256"] == case.source_sha
        received.append(deepcopy(evidence))
        evidence["base_message_admission"]["scope_counts"].clear()
        evidence["source_metadata"]["payload_sha256"] = "0" * 64
        evidence["account_ids"].append("foreign-synthetic-owner")
        evidence["source_storage_rebindings"].append({"synthetic": True})
        evidence.clear()

    callback = Mock(side_effect=review)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        receipt = archived._stage(case, guard, on_preflight=callback)
    callback.assert_called_once()
    assert receipt["revalidation"]["base_message_admission"] == received[0]["base_message_admission"]
    assert receipt["revalidation"]["source_metadata"] == received[0]["source_metadata"]
    assert receipt["revalidation"]["account_ids"] == received[0]["account_ids"]
    assert {"synthetic": True} not in receipt["revalidation"]["source_storage_rebindings"]
    archived._unchanged(case)


@pytest.mark.parametrize("failure", ("exception", "input", "guard"))
def test_preflight_callback_failure_stops_before_outputs_or_clone(archived_case, failure):
    case = archived_case
    paths = set(case.directory.iterdir())
    lease = bank.mac_recovery_publication_guard(case.fixture.root)
    guard = lease.__enter__()
    released = []

    def stop(_evidence):
        if failure == "exception":
            raise RuntimeError("Synthetic preflight rejection")
        if failure == "input":
            sealed._ctime_only(case.fixture.history)
        else:
            lease.__exit__(None, None, None)
            released.append(True)

    callback = Mock(side_effect=stop)
    try:
        error = RuntimeError if failure == "exception" else ValueError
        with pytest.raises(error):
            archived._stage(case, guard, on_preflight=callback)
    finally:
        if not released:
            lease.__exit__(None, None, None)
    callback.assert_called_once()
    case.clones.assert_not_called()
    assert set(case.directory.iterdir()) == paths
    archived._unchanged(case)


@pytest.mark.parametrize("change", ("bytes", "inode", "mtime"))
def test_callback_cannot_change_the_proved_source_before_clone(archived_case, change):
    case = archived_case
    paths = set(case.directory.iterdir())

    def mutate(evidence):
        assert evidence["base_message_admission"]["status"] == "verified"
        before = case.source.stat()
        if change == "bytes":
            altered = bytearray(case.before[case.source])
            altered[-1] ^= 1
            case.source.write_bytes(altered)
            os.utime(case.source, ns=(before.st_atime_ns, before.st_mtime_ns))
            case.before[case.source] = bytes(altered)
        elif change == "inode":
            replacement = case.source.with_name("replacement-source.sqlite3")
            replacement.write_bytes(case.before[case.source])
            replacement.chmod(stat.S_IMODE(before.st_mode))
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(replacement, case.source)
        else:
            os.utime(case.source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))

    callback = Mock(side_effect=mutate)
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        with pytest.raises(ValueError):
            archived._stage(case, guard, on_preflight=callback)
    callback.assert_called_once()
    case.clones.assert_not_called()
    assert set(case.directory.iterdir()) == paths
    archived._unchanged(case)


def test_callback_source_ctime_only_change_requires_audited_exact_bytes_before_clone(archived_case):
    case = archived_case
    callback = Mock(side_effect=lambda _evidence: sealed._ctime_only(case.source))
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        receipt = archived._stage(case, guard, on_preflight=callback)
    callback.assert_called_once()
    case.clones.assert_called_once_with(case.source, case.candidate)
    _assert_audit(receipt["revalidation"]["source_storage_rebindings"], case.source_sha)
    archived._unchanged(case)


@pytest.mark.parametrize("target", ("source", "candidate"))
def test_archived_known_hash_retry_is_audited_without_repeating_preflight(archived_case, monkeypatch, target):
    case = archived_case
    path = case.source if target == "source" else case.candidate
    passes = _during_real_reads(
        monkeypatch, path, lambda attempt: sealed._ctime_only(path) if attempt == 1 else None,
    )
    callback = Mock()
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        receipt = archived._stage(case, guard, on_preflight=callback)
    callback.assert_called_once()
    assert len(passes) >= 2
    records = [record for record in _records(receipt) if record["before_identity"]["ino"] == path.stat().st_ino]
    _assert_audit(records, case.source_sha)
    assert receipt["candidate_sha256"] == hashlib.sha256(case.candidate.read_bytes()).hexdigest()
    archived._unchanged(case)


def test_preserved_readonly_clone_hash_can_retry_and_returns_its_proof(sealed_case, monkeypatch):
    case = sealed_case
    passes = _during_real_reads(
        monkeypatch, case.preserved,
        lambda attempt: sealed._ctime_only(case.preserved) if attempt == 1 else None,
    )
    with bank.mac_recovery_publication_guard(case.fixture.root) as guard:
        receipt = sealed._stage(case, guard)
    assert len(passes) >= 2
    assert receipt["preserved_sha256"] == case.source_sha
    assert revalidation._source_mode(case.preserved.stat()) == 0o400
    records = [record for record in _records(receipt) if record["before_identity"]["ino"] == case.preserved.stat().st_ino]
    _assert_audit(records, case.source_sha)
    sealed._unchanged(case)
