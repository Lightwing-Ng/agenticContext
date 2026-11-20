"""Keep authenticated relocation bounded to the original archive artifacts.

Code version: v1.1.0-codex.0
"""

from contextlib import closing
import hmac
import os
import shutil
import sqlite3

import pytest

from app.core import wechat_archive_relocation as relocation
from app.core import wechat_macos_recovery_bank as bank
from app.core.wechat_archive import WechatArchiveRepository, _file_identity
from app.core.wechat_browser_snapshot import _cache_storage, _json
from tests import test_wechat_media_event_global_timeline as fixtures


global_media_store = fixtures.global_media_store
placeholder_store = fixtures.placeholder_store


def _proof(history, previous, *, key_override=None, version=1, aliases=None):
    _, key, _ = _cache_storage(history.parent / "browser_cache")
    files = {
        path.name: {"previous": identity, "current": bank._identity(path)}
        for path, identity in previous.items()
    }
    for path, pins in (aliases or {}).items():
        files[path.name]["previous_aliases"] = pins
    encoded = _json({
        "version": version,
        "files": files,
    })
    proof = history.with_name(relocation.FILENAME)
    proof.write_bytes(hmac.digest(key_override or key, relocation.DOMAIN + encoded, "sha256") + encoded)
    proof.chmod(0o600)
    return proof


def _replace_with_exact_copy(path):
    candidate = path.with_suffix(".moved")
    shutil.copy2(path, candidate)
    os.replace(candidate, path)


def _timeline(fixture):
    repository = WechatArchiveRepository(fixture.store)
    with closing(sqlite3.connect(fixture.history.as_uri() + "?mode=ro&immutable=1", uri=True)) as c:
        c.row_factory = sqlite3.Row
        try:
            conversations, messages, _ = repository._timeline_tables(
                c, _file_identity(fixture.history), strict=True,
            )
        except ValueError:
            return None
        return (
            c.execute(f"SELECT COUNT(*) FROM {conversations}").fetchone()[0],
            c.execute(f"SELECT COUNT(*) FROM {messages}").fetchone()[0],
        )


def test_exact_relocation_opens_the_same_timeline_and_revocation_closes_it(global_media_store):
    fixture = global_media_store
    expected = _timeline(fixture)
    assert expected is not None and expected[1] > 0
    paths = (fixture.history, fixture.index)
    original_bytes = {path: path.read_bytes() for path in paths}
    previous = {path: bank._identity(path) for path in paths}
    for path in paths:
        _replace_with_exact_copy(path)
    assert _timeline(fixture) is None
    proof = _proof(fixture.history, previous)
    assert _timeline(fixture) == expected
    assert {path: path.read_bytes() for path in paths} == original_bytes
    proof.rename(proof.with_suffix(".revoked"))
    assert _timeline(fixture) is None


@pytest.mark.parametrize("changed", ["signature", "primary", "sidecar"])
def test_changed_signature_or_artifact_cannot_reuse_relocation(global_media_store, changed):
    fixture = global_media_store
    paths = (fixture.history, fixture.index)
    previous = {path: bank._identity(path) for path in paths}
    _replace_with_exact_copy(fixture.history)
    _proof(fixture.history, previous, key_override=b"wrong signature" if changed == "signature" else None)
    if changed == "primary":
        _replace_with_exact_copy(fixture.history)
    if changed == "sidecar":
        _replace_with_exact_copy(fixture.index)
    assert _timeline(fixture) is None


def test_signed_relocation_admits_distinct_historical_reader_pins(global_media_store):
    fixture = global_media_store
    expected = _timeline(fixture)
    paths = (fixture.history, fixture.index)
    previous = {path: bank._identity(path) for path in paths}
    _replace_with_exact_copy(fixture.history)
    current = bank._identity(fixture.history)
    prior_mount = {**current, "dev": current["dev"] + 1}
    history = {relocation.HISTORY: current}
    prior_mount_history = {relocation.HISTORY: prior_mount}
    reader = fixture.index.name

    _proof(fixture.history, previous)
    assert _timeline(fixture) == expected
    assert not relocation.matches(fixture.history, reader, prior_mount_history, history)

    _proof(fixture.history, previous, version=2, aliases={fixture.history: [prior_mount]})
    assert _timeline(fixture) == expected
    assert relocation.matches(fixture.history, reader, prior_mount_history, history)
    assert relocation.matches_history(fixture.history, prior_mount, current)
    unlisted = {**prior_mount, "dev": current["dev"] + 2}
    assert not relocation.matches_history(fixture.history, unlisted, current)

    _replace_with_exact_copy(fixture.index)
    assert not relocation.matches(fixture.history, reader, prior_mount_history, history)


@pytest.mark.parametrize("problem", ["legacy", "incomplete", "duplicate"])
def test_rejects_aliases_without_a_closed_signed_record(global_media_store, problem):
    fixture = global_media_store
    paths = (fixture.history, fixture.index)
    previous = {path: bank._identity(path) for path in paths}
    _replace_with_exact_copy(fixture.history)
    current = bank._identity(fixture.history)
    prior_mount = {**current, "dev": current["dev"] + 1}
    if problem == "incomplete":
        del prior_mount["ctime_ns"]
    aliases = [prior_mount, prior_mount] if problem == "duplicate" else [prior_mount]
    _proof(
        fixture.history, previous, version=1 if problem == "legacy" else 2,
        aliases={fixture.history: aliases},
    )
    assert _timeline(fixture) is None
