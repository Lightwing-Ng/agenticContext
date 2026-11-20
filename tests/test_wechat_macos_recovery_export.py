"""Streaming Mac recovery exports pin every active source overlay.

Code version: v1.0.0-codex.0
"""

import csv
import io

import pytest

from app.core import wechat_macos_recovery as recovery
from app.core.wechat_archive import WechatArchiveRepository
from app.core.wechat_session_export import (
    WechatSessionExport,
    WechatSessionSourceChanged,
)
from app.web.wechat_session_csv import session_csv_chunks
from tests import test_wechat_macos_recovery as recovery_fixtures


mac_archive = recovery_fixtures.mac_archive


def _publish(fixture):
    root, history, session, packet = fixture
    recovery.publish_mac_recovery(root, recovery.stage_mac_recovery(root, packet))
    return root, history, session, packet


def test_streaming_export_contains_all_five_recovered_events_with_mac_source(
    mac_archive,
):
    root, history, session, packet = _publish(mac_archive)
    original = history.read_bytes()
    export = WechatSessionExport(root, session, write_event_cache=False)
    assert history.with_name("macos_recovery.json") in export.sources
    assert export.conversation["message_count"] == 7
    items = list(export.items())
    recovered = [item for item in items if recovery.validated_recovery_receipt(item)]
    assert len(recovered) == 5
    for item in recovered:
        assert item["source_label"] == "WeChat for Mac local database"
        assert item["source_file"] == (
            "WeChat for Mac local database: "
            + packet["source"]["database_relative_path"]
        )
        assert item["source_snapshot_id"] == packet["source"]["snapshot_id"]
        assert item["backup_id"] == ""
    export = WechatSessionExport(root, session, write_event_cache=False)
    text = "".join(session_csv_chunks(export))
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    assert len(rows) == 7
    assert sum(row["Statement"] == "[Image]" for row in rows) == 3
    assert sum(row["Statement"] == "[Video]" for row in rows) == 1
    assert sum(row["Statement"] == "Reply kept exactly" for row in rows) == 1
    assert history.read_bytes() == original


def test_sidecar_drift_stops_stream_and_preserves_archive(mac_archive):
    root, history, session, _packet = _publish(mac_archive)
    original = history.read_bytes()
    export = WechatSessionExport(root, session, write_event_cache=False)
    path = history.with_name("macos_recovery.json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(WechatSessionSourceChanged):
        next(export.items())
    assert export.connection is None
    assert history.read_bytes() == original


@pytest.mark.parametrize("invalidate", [False, True])
@pytest.mark.parametrize("before_projection", [False, True])
def test_sidecar_race_during_projection_refuses_export(
    mac_archive, monkeypatch, invalidate, before_projection
):
    root, history, session, _packet = _publish(mac_archive)
    original_timeline = WechatArchiveRepository._timeline_tables

    def racing_projection(repository, *args, **kwargs):
        path = history.with_name("macos_recovery.json")
        replacement = b"{}" if invalidate else path.read_bytes() + b" "
        if before_projection:
            path.write_bytes(replacement)
        result = original_timeline(repository, *args, **kwargs)
        if not before_projection:
            path.write_bytes(replacement)
        return result

    monkeypatch.setattr(WechatArchiveRepository, "_timeline_tables", racing_projection)
    with pytest.raises(WechatSessionSourceChanged):
        WechatSessionExport(root, session, write_event_cache=False)


@pytest.mark.parametrize(
    "changed_name",
    ["resource_references.sqlite3", "media_placeholders.json", "media_events.sqlite3"],
)
def test_final_mac_view_pins_every_active_underlying_overlay(
    mac_archive, monkeypatch, changed_name
):
    root, history, session, _packet = _publish(mac_archive)
    paths = {
        name: history.with_name(name)
        for name in (
            "resource_references.sqlite3",
            "media_placeholders.json",
            "media_events.sqlite3",
        )
    }
    for path in paths.values():
        path.write_bytes(b"synthetic source identity only")
    monkeypatch.setattr(
        "app.core.wechat_native_resource_revalidation.select_resource_index",
        lambda *_args, **_kwargs: paths["resource_references.sqlite3"],
    )

    def chained_projection(repository, connection, _identity, **kwargs):
        # Real overlay validation has independent suites; this boundary fake
        # isolates nested dependency pinning while retaining the real Mac overlay.
        previous = "main.messages"
        for name in (
            "timeline_messages",
            "placeholder_timeline_messages",
            "media_event_timeline_messages",
        ):
            connection.execute(f"CREATE TEMP VIEW {name} AS SELECT * FROM {previous}")
            previous = name
        return recovery.attach_mac_recovery(
            connection,
            repository.path,
            "main.conversations",
            previous,
            conversation_id=kwargs.get("conversation_id"),
        )[:2] + ("absent",)

    monkeypatch.setattr(WechatArchiveRepository, "_timeline_tables", chained_projection)
    export = WechatSessionExport(root, session, write_event_cache=False)
    assert set(paths.values()).issubset(export.sources)
    assert history.with_name("macos_recovery.json") in export.sources
    paths[changed_name].write_bytes(b"changed source identity")
    with pytest.raises(WechatSessionSourceChanged):
        next(export.items())
    assert export.connection is None
