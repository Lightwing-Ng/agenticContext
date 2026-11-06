"""Explicit native metadata refresh preserves every verified conversation scope.

Code version: v1.2.1-codex.0
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from datetime import UTC, datetime
import csv
import errno
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import sys
from types import SimpleNamespace

from flask import Flask, render_template
import pytest

from app.core.chat_history_browser import query_chat_history
from app.core import wechat_native_resource_revalidation as refresh
from app.core.owner_only_files import owner_only_problem
from app.core.wechat_archive import WechatArchiveRepository, _identifier, _time_fields
from app.core.wechat_mac_resources import WechatMacResourceResolver
from app.core.wechat_session_export import WechatSessionExport
from app.web.presentation import format_wechat_source_clock
from app.web.wechat_session_csv import session_csv_chunks
from tests import test_wechat_native_resource_revalidation as fixtures


KINDS = (
    ("image", 3, "[Image]"),
    ("file", 49, "[File]"),
    ("audio", 34, "[Voice message]"),
    ("video", 43, "[Video]"),
    ("sticker", 47, "[Sticker]"),
)
native_metadata = fixtures.native_metadata


def _resource(template, conversation, owner, backup, database, local, kind, typ, label):
    """Retain a complete synthetic native locator without creating its payload."""
    row = list(template)
    proof = deepcopy(json.loads(row[11]))
    relative = "Documents/" + conversation["partition_id"][4:] + "/DB/message_2.sqlite"
    table = (
        "Chat_"
        + hashlib.md5(
            conversation["peer_id"].encode(), usedforsecurity=False
        ).hexdigest()
    )
    identifier = _identifier(
        "wechat-resource-reference-v1",
        owner,
        backup,
        "AppDomain-com.tencent.xin/" + relative,
        table,
        str(local),
    )
    epoch = 1650000000 + local
    proof.update(
        record_id=identifier,
        backup_id=backup,
        source_sha256=database,
        source_relative_path="AppDomain-com.tencent.xin/" + relative,
        server_id=str(10000 + local),
        unix_timestamp=epoch,
        message_type=typ,
        locator={
            "database": "message_2.sqlite",
            "table": table,
            "local_id": local,
            "row_id": local,
            "text_origin": "native_resource_metadata",
        },
        resource_reference={
            "account_id": owner,
            "peer_id": conversation["peer_id"],
            "unix_timestamp": epoch,
            "kind": kind,
            "label": label,
        },
    )
    row[:7] = (
        identifier,
        conversation["id"],
        local,
        conversation["peer_id"],
        "Recorded sender",
        "received",
        label,
    )
    row[7:11] = _time_fields(
        {"sent_at_utc_iso": datetime.fromtimestamp(epoch, UTC).isoformat()}
    )
    row[11] = json.dumps(proof)
    row[13] = label.casefold()
    return tuple(row)


@pytest.fixture
def two_native_scopes(native_metadata, monkeypatch):
    if sys.platform != "darwin":
        # Only the synthetic fixture replaces the production APFS-only clone backend.
        monkeypatch.setattr("app.core.wechat_export_merge._clonefile", shutil.copyfile)
    fixture = native_metadata
    owner = "wxid_account_b"
    backup, database = "c" * 64, "d" * 64
    with closing(sqlite3.connect(fixture.history)) as primary, primary:
        primary.row_factory = sqlite3.Row
        second = dict(
            primary.execute(
                "SELECT * FROM conversations WHERE account_key=? AND kind='direct'",
                ("account:" + owner,),
            ).fetchone()
        )
        relative = "Documents/" + second["partition_id"][4:] + "/DB/message_2.sqlite"
        metadata = {
            "source_id": second["partition_id"],
            "source_account_id": owner,
            "account_hash": second["partition_id"][4:],
            "account_hash_verified": True,
            "backup_id": backup,
            "input_databases": [{"path": relative, "sha256": database}],
        }
        primary.execute(
            "INSERT INTO source_snapshots VALUES(2,?,?,?)",
            (backup, second["partition_id"], json.dumps(metadata)),
        )
    template = fixture.resources[1]
    first_rows = [fixture.resources[1], fixture.resources[2]] + [
        _resource(
            template,
            fixture.conversation,
            "wxid_account_a",
            "a" * 64,
            "b" * 64,
            local,
            kind,
            typ,
            label,
        )
        for local, (kind, typ, label) in enumerate(KINDS[2:], 4)
    ]
    second_rows = [
        _resource(template, second, owner, backup, database, local, kind, typ, label)
        for local, (kind, typ, label) in enumerate(KINDS, 11)
    ]
    with closing(sqlite3.connect(fixture.original)) as source, source:
        source.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", first_rows[2:]
        )
        source.execute(
            "UPDATE conversations SET message_count=6,last_sort=?,last_time=?,latest_text=? WHERE id=?",
            (
                first_rows[-1][7],
                first_rows[-1][8],
                first_rows[-1][6],
                fixture.conversation["id"],
            ),
        )
        source.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", second_rows
        )
        source.execute(
            "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                second[key]
                for key in (
                    "id",
                    "partition_id",
                    "account_key",
                    "peer_id",
                    "title",
                    "kind",
                )
            )
            + (
                5,
                second_rows[0][7],
                second_rows[-1][7],
                second_rows[0][8],
                second_rows[-1][8],
                second_rows[-1][6],
            ),
        )
    home = fixture.store.parent / "empty-mac-home"
    home.mkdir()
    monkeypatch.setattr(
        "app.core.wechat_history_browser.WechatMacResourceResolver",
        lambda root: WechatMacResourceResolver(root, home=home),
    )
    return SimpleNamespace(
        **vars(fixture), second=second, first_rows=first_rows, second_rows=second_rows
    )


def _publish(fixture, selected, name):
    candidate = fixture.original.with_name(name + ".sqlite3")
    report = refresh.stage_extended_native_resource_revalidation(
        fixture.store, candidate, conversation_ids=selected
    )
    rollback = fixture.store.parent / (name + "-rollback.sqlite3")
    refresh.publish_native_resource_revalidation(
        fixture.store, report, rollback_path=rollback
    )


class _ResourceBubbles(HTMLParser):
    def __init__(self):
        super().__init__()
        self.article = None
        self.resources = {}
        self.payload_tags = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "article":
            self.article = values.get("id")
        if "data-wechat-resource-kind" in values:
            self.resources[self.article] = values["data-wechat-resource-kind"]
        if tag in {"img", "audio", "video", "source", "iframe", "object"}:
            self.payload_tags.append(tag)

    def handle_endtag(self, tag):
        if tag == "article":
            self.article = None


def test_explicit_refresh_adds_a_second_scope_without_losing_first_bubbles_or_source_rows(
    two_native_scopes, monkeypatch
):
    fixture = two_native_scopes
    before = fixture.history.read_bytes(), fixture.original.read_bytes()
    path_open, connect = Path.open, sqlite3.connect

    def no_payload_open(path, *args, **kwargs):
        assert path.suffix.casefold() not in {
            ".jpg",
            ".jpeg",
            ".png",
            ".mp4",
            ".mp3",
            ".dat",
            ".zip",
            ".gz",
        }
        return path_open(path, *args, **kwargs)

    def no_backup_open(path, *args, **kwargs):
        assert "/DB/message_2.sqlite" not in str(path).replace("\\", "/")
        return connect(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_payload_open)
    monkeypatch.setattr(sqlite3, "connect", no_backup_open)
    repository = WechatArchiveRepository(fixture.store)
    assert (
        repository.query(session=fixture.conversation["id"])["resource_index_status"]
        == "stale"
    )
    assert repository.query(session=fixture.second["id"])["count"] == 1
    _publish(fixture, [fixture.conversation["id"]], "first-verified-scope")
    first_publication = fixture.history.with_name(
        "resource_revalidated.sqlite3"
    ).read_bytes()
    assert repository.query(session=fixture.conversation["id"])["count"] == 6
    # A normal visit stays read-only and cannot silently publish another account.
    assert (
        repository.query(session=fixture.second["id"])["resource_index_status"]
        == "stale"
    )
    assert (
        fixture.history.with_name("resource_revalidated.sqlite3").read_bytes()
        == first_publication
    )
    _publish(fixture, [fixture.second["id"]], "second-verified-scope")

    for conversation, resources in (
        (fixture.conversation, fixture.first_rows),
        (fixture.second, fixture.second_rows),
    ):
        pages = [
            repository.query(
                session=conversation["id"],
                account=conversation["account_key"],
                page=page,
                page_size=2,
            )
            for page in (1, 2, 3)
        ]
        assert {page["resource_index_status"] for page in pages} == {"ready"}
        assert {page["count"] for page in pages} == {6}
        rows = [row for page in pages for row in page["messages"]]
        assert len(rows) == len({row["id"] for row in rows}) == 6
        assert {row[0] for row in resources} <= {row["id"] for row in rows}
        assert (
            repository.query(session=conversation["id"], query="[Sticker]")["count"]
            == 1
        )
        page = query_chat_history(
            fixture.store,
            source="wechat",
            session=conversation["id"],
            account=conversation["account_key"],
        )
        resource_items = [item for item in page.items if item.messaging.get("resource")]
        assert {item.messaging["resource"]["kind"] for item in resource_items} == {
            kind for kind, _, _ in KINDS
        }
        assert all(
            item.messaging["resource"]["status"] == "missing" for item in resource_items
        )
        assert all(
            not item.messaging["resource"]["can_reveal"] for item in resource_items
        )
        app = Flask(
            __name__,
            template_folder=str(Path(__file__).parents[1] / "app/web/templates"),
        )
        app.jinja_env.filters["wechat_source_clock"] = format_wechat_source_clock
        with app.test_request_context("/"):
            rendered = render_template(
                "_wechat_browser.html",
                text_page=page,
                filters=SimpleNamespace(
                    account=conversation["account_key"],
                    chat_kind="direct",
                    sort="newest",
                    session_page=1,
                ),
                url_for=lambda *_args, **_kwargs: "/synthetic",
            )
        parsed = _ResourceBubbles()
        parsed.feed(rendered)
        assert set(parsed.resources.values()) == {kind for kind, _, _ in KINDS}
        assert set(parsed.resources) == {
            "wechat_message_" + item.stable_id for item in resource_items
        }
        assert parsed.payload_tags == []
        export = WechatSessionExport(
            fixture.store, conversation["id"], account=conversation["account_key"]
        )
        exported = list(
            csv.DictReader(
                io.StringIO("".join(session_csv_chunks(export)).lstrip("\ufeff"))
            )
        )
        assert len(exported) == 6
        assert {label for _, _, label in KINDS} <= {
            row["Statement"] for row in exported
        }

    blocked = repository.query(
        session=fixture.second["id"], account=fixture.conversation["account_key"]
    )
    assert blocked["current"] is None and blocked["messages"] == []
    with pytest.raises(ValueError, match="scope"):
        WechatSessionExport(
            fixture.store,
            fixture.second["id"],
            account=fixture.conversation["account_key"],
        )
    with closing(sqlite3.connect(fixture.history)) as source, source:
        outside = source.execute(
            "SELECT id FROM conversations WHERE account_key=? AND kind='group'",
            (fixture.conversation["account_key"],),
        ).fetchone()[0]
    assert (
        refresh.select_resource_index(fixture.history, conversation_id=outside)
        == fixture.original
    )
    assert repository.query(session=outside)["resource_index_status"] == "stale"
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == before


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
def test_directory_sync_bypasses_only_native_windows_and_propagates_posix_errors(
    tmp_path, monkeypatch, platform_name
):
    events = []

    class PlatformOs:
        name = platform_name
        O_RDONLY = os.O_RDONLY

        def open(self, path, flags):
            assert path == tmp_path and flags == os.O_RDONLY
            events.append("open")
            return 123

        def fsync(self, descriptor):
            assert descriptor == 123
            events.append("fsync")
            raise OSError(errno.EIO, "Synthetic directory flush failure")

        def close(self, descriptor):
            assert descriptor == 123
            events.append("close")

    monkeypatch.setattr(refresh, "os", PlatformOs())
    if platform_name == "nt":
        refresh._sync_directory(tmp_path)
        assert events == []
    else:
        with pytest.raises(OSError, match="Synthetic directory flush failure"):
            refresh._sync_directory(tmp_path)
        assert events == ["open", "fsync", "close"]


def test_windows_publication_flushes_writable_rollback_and_preserves_exact_sources(
    two_native_scopes, monkeypatch
):
    fixture = two_native_scopes
    original_sources = fixture.history.read_bytes(), fixture.original.read_bytes()
    _publish(fixture, [fixture.conversation["id"]], "windows-first-scope")
    fallback = fixture.history.with_name("resource_revalidated.sqlite3")
    prior_fallback = fallback.read_bytes()
    candidate = fixture.original.with_name("windows-second-scope.sqlite3")
    report = refresh.stage_extended_native_resource_revalidation(
        fixture.store, candidate, conversation_ids=[fixture.second["id"]]
    )
    rollback = fixture.store.parent / "windows-native-rollback.sqlite3"
    open_file = Path.open
    handles, flushed = [], []

    def track(path, *args, **kwargs):
        handle = open_file(path, *args, **kwargs)
        if path == rollback:
            handles.append(handle)
        return handle

    class WindowsOs:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)

        def fsync(self, descriptor):
            handle = next(
                item for item in handles
                if not item.closed and item.fileno() == descriptor
            )
            assert handle.writable()
            flushed.append(descriptor)
            return os.fsync(descriptor)

    monkeypatch.setattr(Path, "open", track)
    monkeypatch.setattr(refresh, "os", WindowsOs())
    result = refresh.publish_native_resource_revalidation(
        fixture.store, report, rollback_path=rollback
    )
    assert result["published"] is True
    assert flushed and all(handle.closed for handle in handles)
    assert rollback.read_bytes() == prior_fallback
    assert owner_only_problem(rollback) == owner_only_problem(fallback) == ""
    if os.name == "posix":
        assert rollback.stat().st_mode & 0o777 == 0o600
        assert fallback.stat().st_mode & 0o777 == 0o600
    assert (fixture.history.read_bytes(), fixture.original.read_bytes()) == original_sources


def test_rollback_flush_error_keeps_published_fallback_and_sources_unchanged(
    two_native_scopes, monkeypatch
):
    fixture = two_native_scopes
    _publish(fixture, [fixture.conversation["id"]], "before-failed-rollback")
    fallback = fixture.history.with_name("resource_revalidated.sqlite3")
    preserved = (
        fixture.history.read_bytes(), fixture.original.read_bytes(), fallback.read_bytes()
    )
    candidate = fixture.original.with_name("unpublished-second-scope.sqlite3")
    report = refresh.stage_extended_native_resource_revalidation(
        fixture.store, candidate, conversation_ids=[fixture.second["id"]]
    )
    candidate_bytes = candidate.read_bytes()
    rollback = fixture.store.parent / "failed-flush-rollback.sqlite3"
    open_file, fsync = Path.open, os.fsync
    handles, failures = [], []

    def track(path, *args, **kwargs):
        handle = open_file(path, *args, **kwargs)
        if path == rollback:
            handles.append(handle)
        return handle

    def fail(descriptor):
        if any(
            not handle.closed and handle.fileno() == descriptor for handle in handles
        ):
            failures.append(descriptor)
            raise OSError(errno.EIO, "Synthetic rollback flush failure")
        return fsync(descriptor)

    monkeypatch.setattr(Path, "open", track)
    monkeypatch.setattr(refresh.os, "fsync", fail)
    with pytest.raises(OSError, match="Synthetic rollback flush failure"):
        refresh.publish_native_resource_revalidation(
            fixture.store, report, rollback_path=rollback
        )
    assert len(failures) == 1 and all(handle.closed for handle in handles)
    assert candidate.read_bytes() == candidate_bytes
    assert rollback.read_bytes() == preserved[2]
    assert (
        fixture.history.read_bytes(), fixture.original.read_bytes(), fallback.read_bytes()
    ) == preserved


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
def test_publication_syncs_directories_only_where_the_host_supports_them(tmp_path, monkeypatch, platform_name):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    native = SimpleNamespace(
        name=platform_name,
        O_RDONLY=0,
        open=MagicMock(return_value=73),
        fsync=MagicMock(),
        close=MagicMock(),
    )
    monkeypatch.setattr(refresh, "os", native)
    refresh._sync_directory(tmp_path)
    if platform_name == "nt":
        native.open.assert_not_called()
        native.fsync.assert_not_called()
        native.close.assert_not_called()
    else:
        native.open.assert_called_once_with(tmp_path, native.O_RDONLY)
        native.fsync.assert_called_once_with(73)
        native.close.assert_called_once_with(73)
