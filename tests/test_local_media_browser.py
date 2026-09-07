"""Read-only local media browser tests.

Code version: v1.11.2-codex.1
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from zoneinfo import ZoneInfo

import pytest

from app.core.local_media_browser import (
    BrowserDeletionCatalog,
    LocalMediaCatalog,
    LocalMediaItem,
    build_local_store_pagination,
    file_manager_reveal_command,
    file_manager_open_directory_command,
    format_captured_at_label,
    format_captured_at_timestamp_label,
    format_datetime_label,
    local_file_manager_label,
    paginate_chatgpt_sessions,
    paginate_media_items,
    reveal_media_path,
    open_directory_path,
    resolve_local_media_path,
    sort_media_items,
    sort_media_items_absolute,
)
from app.core.resource_persistence import (
    DELETED_MEDIA_FILENAME,
    DELETED_MEDIA_SCHEMA,
    read_parquet_rows,
    write_parquet_rows_atomic,
)


def _write_media(path: Path, content: bytes = b"media") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _item(name: str, captured_at: str = "2026-08-08T00:00:00Z", media_kind: str = "image") -> LocalMediaItem:
    return LocalMediaItem(
        stable_id=f"id-{name}",
        source="x",
        media_kind=media_kind,
        relative_path=f"x/{name}",
        filename=name,
        title=name,
        description="",
        creator="demo",
        source_url="",
        captured_at=captured_at,
        captured_at_label="8 Aug 2026",
        content_bytes=5,
        project_name="",
    )


def test_scans_x_image_and_reads_info_json(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    media_path = root / "x" / "uploader" / "123" / "123.jpg"
    _write_media(media_path, b"jpeg-bytes")
    (media_path.parent / "123.info.json").write_text(
        json.dumps(
            {
                "title": "A cached X image",
                "description": "A useful description",
                "uploader": "Uploader Name",
                "uploader_id": "uploader",
                "timestamp": 1786147200,
                "webpage_url": "https://x.com/uploader/status/123",
                "display_id": "123",
            }
        ),
        encoding="utf-8",
    )

    items = LocalMediaCatalog(root).snapshot(force_refresh=True)

    assert len(items) == 1
    item = items[0]
    assert item.source == "x"
    assert item.media_kind == "image"
    assert item.relative_path == "x/uploader/123/123.jpg"
    assert item.title == "A cached X image"
    assert item.description == "A useful description"
    assert item.creator == "Uploader Name"
    assert item.source_url == "https://x.com/uploader/status/123"
    assert item.content_bytes == len(b"jpeg-bytes")


def test_scans_x_metadata_once_per_media_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "local_store"
    media_directory = root / "x" / "uploader" / "123"
    _write_media(media_directory / "first.jpg")
    _write_media(media_directory / "second.jpg")
    catalog = LocalMediaCatalog(root)
    metadata_loads: list[Path] = []

    def load_metadata(directory: Path) -> dict[str, str]:
        metadata_loads.append(directory)
        return {"title": "Shared X metadata"}

    monkeypatch.setattr(catalog, "_load_x_metadata", load_metadata)

    items = catalog.snapshot(force_refresh=True)

    assert [item.title for item in items] == ["Shared X metadata", "Shared X metadata"]
    assert metadata_loads == [media_directory]


def test_query_returns_stale_snapshot_while_refresh_is_in_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "local_store"
    _write_media(root / "x" / "demo" / "cached.jpg")
    catalog = LocalMediaCatalog(root, ttl_seconds=0)
    catalog.snapshot(force_refresh=True)

    refresh_started = Event()
    release_refresh = Event()
    query_finished = Event()
    original_scan_x = catalog._scan_x
    pages = []
    failures = []

    def blocked_scan_x():
        refresh_started.set()
        if not release_refresh.wait(timeout=2):
            raise TimeoutError("The test refresh was not released.")
        return original_scan_x()

    def run_query() -> None:
        try:
            pages.append(catalog.query())
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            query_finished.set()

    monkeypatch.setattr(catalog, "_scan_x", blocked_scan_x)
    refresh_thread = Thread(target=lambda: catalog.snapshot(force_refresh=True))
    query_thread = Thread(target=run_query)
    refresh_thread.start()

    try:
        assert refresh_started.wait(timeout=1)
        query_thread.start()
        assert query_finished.wait(timeout=1)
        assert not failures
        assert [item.filename for item in pages[0].items] == ["cached.jpg"]
    finally:
        release_refresh.set()
        refresh_thread.join(timeout=2)
        query_thread.join(timeout=2)

    assert not refresh_thread.is_alive()
    assert not query_thread.is_alive()


def test_browser_deletion_keeps_preview_and_blocks_future_source_downloads(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    media_path = root / "x" / "uploader" / "123" / "123.jpg"
    _write_media(media_path, b"jpeg-bytes")
    (media_path.parent / "123.info.json").write_text(
        json.dumps(
            {
                "title": "A removable X image",
                "webpage_url": "https://x.com/uploader/status/123",
                "uploader_id": "uploader",
                "display_id": "123",
            }
        ),
        encoding="utf-8",
    )

    catalog = LocalMediaCatalog(root)
    item = catalog.snapshot(force_refresh=True)[0]
    deleted = catalog.delete(item.stable_id)

    assert deleted.is_deleted
    assert deleted.deleted_at
    assert not media_path.exists()
    assert catalog.deleted_preview_path(item.stable_id).read_bytes() == b"jpeg-bytes"
    assert catalog.resolved_media_path(item.stable_id) == catalog.deleted_preview_path(item.stable_id)
    assert catalog.is_excluded("x", "https://twitter.com/uploader/status/123")
    deleted_snapshot_item = catalog.snapshot(force_refresh=True)[0]
    assert deleted_snapshot_item.is_deleted
    assert deleted_snapshot_item.deleted_at == deleted.deleted_at

    restored = catalog.restore(item.stable_id)

    assert not restored.is_deleted
    assert restored.deleted_at == ""
    assert media_path.read_bytes() == b"jpeg-bytes"
    assert not catalog.is_excluded("x", item.source_url)
    assert catalog.deleted_preview_path(item.stable_id) is None
    assert catalog.resolved_media_path(item.stable_id) == media_path.resolve()


def test_file_manager_reveal_uses_platform_native_commands(tmp_path: Path, monkeypatch) -> None:
    media_path = tmp_path / "asset.png"
    _write_media(media_path)

    assert local_file_manager_label(platform_name="darwin", os_name="posix") == "Finder"
    assert local_file_manager_label(platform_name="win32", os_name="nt") == "File Explorer"
    assert local_file_manager_label(platform_name="linux", os_name="posix") == "file manager"
    assert file_manager_reveal_command(
        media_path,
        platform_name="darwin",
        os_name="posix",
    ) == ["open", "-R", str(media_path.resolve())]
    assert file_manager_reveal_command(
        media_path,
        platform_name="win32",
        os_name="nt",
    ) == ["explorer.exe", f"/select,{media_path.resolve()}"]
    assert file_manager_reveal_command(
        media_path,
        platform_name="linux",
        os_name="posix",
    ) == ["xdg-open", str(tmp_path.resolve())]

    popen_calls = []
    monkeypatch.setattr(
        "app.core.local_media_browser.subprocess.Popen",
        lambda command, **kwargs: popen_calls.append((command, kwargs)),
    )
    reveal_media_path(media_path)

    assert popen_calls[0][0] == file_manager_reveal_command(media_path)
    if os.name == "nt":
        assert popen_calls[0][1]["creationflags"]
    else:
        assert popen_calls[0][1]["start_new_session"] is True


def test_file_manager_open_directory_uses_platform_native_commands(tmp_path: Path, monkeypatch) -> None:
    assert file_manager_open_directory_command(
        tmp_path,
        platform_name="darwin",
        os_name="posix",
    ) == ["open", str(tmp_path.resolve())]
    assert file_manager_open_directory_command(
        tmp_path,
        platform_name="win32",
        os_name="nt",
    ) == ["explorer.exe", str(tmp_path.resolve())]
    assert file_manager_open_directory_command(
        tmp_path,
        platform_name="linux",
        os_name="posix",
    ) == ["xdg-open", str(tmp_path.resolve())]

    popen_calls = []
    monkeypatch.setattr(
        "app.core.local_media_browser.subprocess.Popen",
        lambda command, **kwargs: popen_calls.append((command, kwargs)),
    )
    open_directory_path(tmp_path)

    assert popen_calls[0][0] == file_manager_open_directory_command(tmp_path)


def test_browser_deletion_catalog_normalizes_source_keys(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    deletion_catalog = BrowserDeletionCatalog(root)
    item = _item("asset.jpg")
    media_path = root / item.relative_path
    _write_media(media_path)
    item = replace(
        item,
        source_url="https://x.com/demo/status/asset",
        resource_key="https://x.com/demo/status/asset",
    )

    deletion_catalog.delete(item)

    assert deletion_catalog.is_excluded("x", "https://twitter.com/demo/status/asset/")


def test_scans_x_video_and_supports_double_info_suffix(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    media_path = root / "x" / "uploader" / "456" / "456.mp4"
    _write_media(media_path, b"video-bytes")
    (media_path.parent / "456.info.json.info.json").write_text(
        '{"title": "Cached X video", "upload_date": "20260808"}',
        encoding="utf-8",
    )

    items = LocalMediaCatalog(root).snapshot(force_refresh=True)

    assert len(items) == 1
    assert items[0].media_kind == "video"
    assert items[0].title == "Cached X video"
    assert items[0].captured_at_label == "08/08/2026"


def test_scans_grok_catalog_metadata(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    media_path = root / "media" / "grok" / "img_asset.jpg"
    _write_media(media_path, b"grok-image")
    (root / "media" / "grok" / ".grok_catalog.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "identity": "asset-123/portrait",
                        "relative_path": "img_asset.jpg",
                        "media_kind": "image",
                        "source_url": "https://grok.com/files/asset-123",
                        "first_seen_at": "2026-08-01T00:00:00Z",
                        "last_seen_at": "2026-08-07T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    items = LocalMediaCatalog(root).snapshot(force_refresh=True)

    assert len(items) == 1
    assert items[0].source == "grok"
    assert items[0].title == "portrait"
    assert items[0].creator == "Grok"
    assert items[0].source_url == "https://grok.com/files/asset-123"
    assert items[0].captured_at_label == "07/08/2026"


def test_grok_catalog_damage_falls_back_to_filename_and_mtime(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    media_path = root / "media" / "grok" / "fallback.webp"
    _write_media(media_path, b"fallback")
    os.utime(media_path, (1786147200, 1786147200))
    (root / "media" / "grok" / ".grok_catalog.json").write_text("{not valid", encoding="utf-8")

    items = LocalMediaCatalog(root).snapshot(force_refresh=True)

    assert len(items) == 1
    assert items[0].filename == "fallback.webp"
    assert items[0].title == "fallback.webp"
    assert items[0].captured_at_label == "08/08/2026"


def test_scans_chatgpt_catalog_and_project_name(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    media_path = root / "media" / "chatgpt" / "Demo Project" / "img_file.png"
    _write_media(media_path, b"chatgpt-image")
    (media_path.parent / ".chatgpt_catalog.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "file-123": {
                        "file_id": "file-123",
                        "relative_path": "img_file.png",
                        "conversation_url": "https://chatgpt.com/c/demo",
                        "conversation_title": "Demo Project - Branch · master 0809b",
                        "alt_text": "A project illustration",
                        "prompt_markdown": "**Frame the athlete** in profile.\n\n- Soft light\n- Blue backdrop",
                        "width": 1200,
                        "height": 800,
                        "created_at": "2026-08-07T11:30:00Z",
                        "first_seen_at": "2026-08-02T00:00:00Z",
                        "last_seen_at": "2026-08-08T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    items = LocalMediaCatalog(root).snapshot(force_refresh=True)

    assert len(items) == 1
    assert items[0].source == "chatgpt"
    assert items[0].project_name == "Demo Project"
    assert items[0].title == "A project illustration"
    assert items[0].description == "A project illustration"
    assert items[0].prompt_markdown == "**Frame the athlete** in profile.\n\n- Soft light\n- Blue backdrop"
    assert items[0].source_url == "https://chatgpt.com/c/demo"
    assert items[0].creator == "Demo Project - Branch · master 0809b"
    assert items[0].chatgpt_session_key == "demo"
    assert items[0].chatgpt_branch_key == "demo project:master:0809b"
    assert items[0].captured_at == "2026-08-07T11:30:00Z"
    assert items[0].width == 1200
    assert items[0].height == 800


def test_chatgpt_direct_session_uses_its_recorded_title(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    media_path = root / "media" / "chatgpt" / "Configured Project" / "img_file.png"
    _write_media(media_path, b"chatgpt-image")
    (media_path.parent / ".chatgpt_catalog.json").write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "file-123": {
                        "file_id": "file-123",
                        "relative_path": "img_file.png",
                        "conversation_url": "https://chatgpt.com/c/demo-session",
                        "conversation_title": "A regular session",
                        "first_seen_at": "2026-08-09T07:09:50Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    items = LocalMediaCatalog(root).snapshot(force_refresh=True)

    assert items[0].creator == "A regular session"


def test_legacy_chatgpt_tombstone_hydrates_source_metadata_from_catalog(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    project_dir = root / "media" / "chatgpt" / "demo-project"
    media_path = project_dir / "img_file-123.png"
    _write_media(media_path, b"chatgpt-image")
    catalog_path = project_dir / ".chatgpt_catalog.json"
    catalog_payload = {
        "version": 1,
        "entries": {
            "file-123": {
                "file_id": "file-123",
                "relative_path": media_path.name,
                "conversation_url": "https://chatgpt.com/g/project/c/session-123",
                "conversation_title": "Original session",
                "first_seen_at": "2026-08-01T00:00:00Z",
            }
        },
    }
    catalog_path.write_text(json.dumps(catalog_payload), encoding="utf-8")
    media_catalog = LocalMediaCatalog(root)
    active_item = media_catalog.snapshot(force_refresh=True)[0]
    media_catalog.delete(active_item.stable_id)

    deletion_path = root / DELETED_MEDIA_FILENAME
    deletion_rows = read_parquet_rows(deletion_path)
    assert deletion_rows is not None
    tombstone_row = next(row for row in deletion_rows if row["stable_id"] == active_item.stable_id)
    tombstone_row["chatgpt_session_key"] = ""
    tombstone_row["creator"] = "demo-project"
    write_parquet_rows_atomic(deletion_path, deletion_rows, DELETED_MEDIA_SCHEMA)
    catalog_payload["entries"]["file-123"].update(
        {
            "conversation_title": "Updated session",
            "created_at": "2026-08-10T12:34:00Z",
        }
    )
    catalog_path.write_text(json.dumps(catalog_payload), encoding="utf-8")

    page = LocalMediaCatalog(root).query(source="chatgpt", force_refresh=True)

    assert page.session_count == 1
    assert page.current_session_latest_at == "2026-08-10T12:34:00Z"
    assert len(page.items) == 1
    item = page.items[0]
    assert item.is_deleted
    assert item.creator == "Updated session"
    assert item.captured_at == "2026-08-10T12:34:00Z"
    assert item.captured_at_label == "10/08/2026"
    assert item.chatgpt_session_key == "session-123"


@pytest.mark.parametrize(
    ("sort", "expected"),
    (
        (
            "newest",
            [
                "session-one-branch.png",
                "session-one-current.png",
                "session-one-old.png",
                "session-two-branch.png",
                "session-two-newer.png",
                "session-two-old.png",
            ],
        ),
        (
            "oldest",
            [
                "session-two-old.png",
                "session-two-newer.png",
                "session-two-branch.png",
                "session-one-old.png",
                "session-one-current.png",
                "session-one-branch.png",
            ],
        ),
        (
            "name",
            [
                "session-one-branch.png",
                "session-one-current.png",
                "session-one-old.png",
                "session-two-branch.png",
                "session-two-newer.png",
                "session-two-old.png",
            ],
        ),
    ),
)
def test_chatgpt_branch_families_stay_contiguous_for_every_sort(
    sort: str,
    expected: list[str],
) -> None:
    branch_family = "demo-project:master:0809b"
    other_family = "demo-project:master:0808d"
    items = (
        replace(
            _item("session-one-current.png", "2026-08-06T12:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="session-one",
            chatgpt_branch_key=branch_family,
        ),
        replace(
            _item("session-two-branch.png", "2026-08-06T11:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="session-two-branch",
            chatgpt_branch_key=other_family,
        ),
        replace(
            _item("session-one-branch.png", "2026-08-07T12:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="session-one-branch",
            chatgpt_branch_key=branch_family,
        ),
        replace(
            _item("session-one-old.png", "2026-08-05T12:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="session-one",
            chatgpt_branch_key=branch_family,
        ),
        replace(
            _item("session-two-newer.png", "2026-08-05T18:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="session-two",
            chatgpt_branch_key=other_family,
        ),
        replace(
            _item("session-two-old.png", "2026-08-05T06:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="session-two",
            chatgpt_branch_key=other_family,
        ),
    )

    assert [item.filename for item in sort_media_items(items, sort)] == expected


def test_chatgpt_absolute_sort_ignores_session_grouping() -> None:
    branch_family = "demo-project:master:0810a"
    items = (
        replace(
            _item("first-session-new.png", "2026-08-10T12:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="first-session",
            chatgpt_branch_key=branch_family,
        ),
        replace(
            _item("second-session-middle.png", "2026-08-09T12:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="second-session",
        ),
        replace(
            _item("first-session-old.png", "2026-08-08T12:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="first-session",
            chatgpt_branch_key=branch_family,
        ),
    )

    assert [item.filename for item in sort_media_items_absolute(items, "newest")] == [
        "first-session-new.png",
        "second-session-middle.png",
        "first-session-old.png",
    ]


def test_chatgpt_pagination_uses_one_session_per_page_ordered_by_latest_image() -> None:
    shared_branch = "demo-project:master:0810a"
    items = (
        replace(
            _item("new-session-old.png", "2026-08-01T00:00:00Z"),
            source="chatgpt",
            creator="Newest session",
            project_name="demo-project",
            chatgpt_session_key="new-session",
            chatgpt_branch_key=shared_branch,
        ),
        replace(
            _item("older-session-latest.png", "2026-08-08T12:00:00Z"),
            source="chatgpt",
            creator="Older session",
            project_name="demo-project",
            chatgpt_session_key="older-session",
            chatgpt_branch_key=shared_branch,
        ),
        replace(
            _item("new-session-latest.png", "2026-08-09T12:00:00Z"),
            source="chatgpt",
            creator="Newest session",
            project_name="demo-project",
            source_url="https://chatgpt.com/c/new-session",
            chatgpt_session_key="new-session",
            chatgpt_branch_key=shared_branch,
        ),
        replace(
            _item("older-session-old.png", "2026-08-02T00:00:00Z"),
            source="chatgpt",
            creator="Older session",
            project_name="demo-project",
            chatgpt_session_key="older-session",
            chatgpt_branch_key=shared_branch,
        ),
    )

    first_page = paginate_chatgpt_sessions(items, page=1, sort="oldest")
    second_page = paginate_chatgpt_sessions(items, page=2, sort="newest")
    targeted_page = paginate_chatgpt_sessions(
        items,
        page=1,
        sort="newest",
        target_session_key="older-session",
    )

    assert first_page.pagination_unit == "session"
    assert first_page.session_count == 2
    assert first_page.total_pages == 2
    assert first_page.total_count == 4
    assert first_page.current_session_label == "Newest session"
    assert first_page.current_session_latest_at == "2026-08-09T12:00:00Z"
    assert first_page.current_session_url == "https://chatgpt.com/c/new-session"
    assert [item.filename for item in first_page.items] == [
        "new-session-old.png",
        "new-session-latest.png",
    ]
    assert second_page.current_session_label == "Older session"
    assert [item.filename for item in second_page.items] == [
        "older-session-latest.png",
        "older-session-old.png",
    ]
    assert targeted_page.current_page == 2
    assert targeted_page.current_session_key == "chatgpt:session:demo-project:older-session"
    assert targeted_page.current_session_label == "Older session"


def test_chatgpt_pagination_prefers_explicit_branch_title_over_project_label() -> None:
    items = (
        replace(
            _item("latest.png", "2026-08-09T12:00:00Z"),
            source="chatgpt",
            creator="Studio208cm",
            project_name="Studio208cm",
            source_url="https://chatgpt.com/g/g-p-demo-studio/c/demo-session",
            chatgpt_session_key="demo-session",
        ),
        replace(
            _item("branch.png", "2026-08-08T12:00:00Z"),
            source="chatgpt",
            creator="Branch · master 0809b",
            project_name="Studio208cm",
            source_url="https://chatgpt.com/g/g-p-demo-studio/c/demo-session",
            chatgpt_session_key="demo-session",
            chatgpt_branch_key="studio208cm:master:0809b",
        ),
    )

    page = paginate_chatgpt_sessions(items)

    assert page.current_session_label == "Branch · master 0809b"


def test_chatgpt_pagination_recovers_legacy_tombstone_session_from_url() -> None:
    conversation_url = "https://chatgpt.com/g/project/c/shared-session"
    items = (
        replace(
            _item("active.png", "2026-08-09T00:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            source_url=conversation_url,
            chatgpt_session_key="shared-session",
        ),
        replace(
            _item("deleted.png", "2026-08-08T00:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            source_url=conversation_url,
            chatgpt_session_key="",
            is_deleted=True,
        ),
    )

    page = paginate_chatgpt_sessions(items)

    assert page.session_count == 1
    assert page.total_pages == 1
    assert [item.filename for item in page.items] == ["active.png", "deleted.png"]


def test_catalog_query_uses_session_pagination_only_for_the_chatgpt_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = LocalMediaCatalog(tmp_path / "local_store")
    items = (
        replace(
            _item("chatgpt-one.png", "2026-08-09T00:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="session-one",
        ),
        replace(
            _item("chatgpt-two.png", "2026-08-08T00:00:00Z"),
            source="chatgpt",
            project_name="demo-project",
            chatgpt_session_key="session-two",
        ),
        _item("x-item.png", "2026-08-10T00:00:00Z"),
    )
    monkeypatch.setattr(catalog, "snapshot", lambda force_refresh=False: items)

    chatgpt_page = catalog.query(source="chatgpt", page=1)
    chronological_page = catalog.query(source="chatgpt", page=1, chatgpt_session_view=False)
    all_sources_page = catalog.query(source="all", page=1)

    assert chatgpt_page.pagination_unit == "session"
    assert chatgpt_page.session_count == 2
    assert len(chatgpt_page.items) == 1
    assert chronological_page.pagination_unit == "media"
    assert [item.filename for item in chronological_page.items] == [
        "chatgpt-one.png",
        "chatgpt-two.png",
    ]
    assert all_sources_page.pagination_unit == "media"
    assert all_sources_page.total_count == 3


def test_chatgpt_catalog_missing_falls_back_to_filename(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    media_path = root / "media" / "chatgpt" / "Fallback" / "img_file.avif"
    _write_media(media_path, b"avif-bytes")

    items = LocalMediaCatalog(root).snapshot(force_refresh=True)

    assert len(items) == 1
    assert items[0].project_name == "Fallback"
    assert items[0].title == "img_file.avif"
    assert items[0].media_kind == "image"


def test_formats_captured_timestamp_with_seconds_and_timezone() -> None:
    expected = datetime.fromisoformat("2026-08-09T07:09:50+00:00").astimezone(
        ZoneInfo("Asia/Hong_Kong")
    )
    assert format_captured_at_timestamp_label("2026-08-09T07:09:50Z") == (
        f"{expected.day:02d}/{expected.month:02d}/{expected.year:04d} "
        f"{expected.hour:02d}:{expected.minute:02d}:{expected.second:02d} "
        "(HKT)"
    )


def test_formats_date_only_value_without_time_or_timezone() -> None:
    assert format_datetime_label("2026-08-09") == "09/08/2026"


def test_chatgpt_for_prompts_is_exempt_from_regular_inventory(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    _write_media(root / "media" / "chatgpt" / "forPrompts" / "temporary.png")
    _write_media(root / "media" / "chatgpt" / "RegularProject" / "inventory.png")

    items = LocalMediaCatalog(root).snapshot(force_refresh=True)

    assert [item.relative_path for item in items] == ["media/chatgpt/RegularProject/inventory.png"]


def test_ignores_hidden_partial_state_and_unknown_files(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    visible = root / "x" / "demo" / "visible.png"
    _write_media(visible)
    _write_media(root / "x" / ".hidden" / "hidden.jpg")
    _write_media(root / "x" / "partial.jpg.part")
    _write_media(root / "x" / "download.ytdl")
    _write_media(root / "x" / "state.json")
    _write_media(root / "x" / "unknown.txt")
    _write_media(root / "x" / ".hidden.jpg")

    items = LocalMediaCatalog(root).snapshot(force_refresh=True)

    assert [item.relative_path for item in items] == ["x/demo/visible.png"]


def test_search_is_case_insensitive_and_source_kind_filters_apply(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    _write_media(root / "x" / "demo" / "Summer-Trip.jpg")
    _write_media(root / "media" / "grok" / "winter-video.mp4")
    catalog = LocalMediaCatalog(root)

    search_page = catalog.query(query="SUMMER", force_refresh=True)
    spaced_search_page = catalog.query(query="summer trip")
    source_page = catalog.query(source="grok")
    kind_page = catalog.query(media_kind="video")

    assert [item.filename for item in search_page.items] == ["Summer-Trip.jpg"]
    assert [item.filename for item in spaced_search_page.items] == ["Summer-Trip.jpg"]
    assert [item.filename for item in source_page.items] == ["winter-video.mp4"]
    assert source_page.image_count == 0
    assert source_page.video_count == 1
    assert [item.filename for item in kind_page.items] == ["winter-video.mp4"]


def test_sorting_is_stable_for_equal_timestamps(tmp_path: Path) -> None:
    items = (
        _item("b.jpg"),
        _item("a.jpg"),
    )

    assert [item.filename for item in sort_media_items(items, "newest")] == ["a.jpg", "b.jpg"]
    assert [item.filename for item in sort_media_items(items, "oldest")] == ["a.jpg", "b.jpg"]
    assert [item.filename for item in sort_media_items(items, "name")] == ["a.jpg", "b.jpg"]


@pytest.mark.parametrize(
    ("sort", "expected"),
    (
        ("newest", ["resource-n.jpg", "resource-1.jpg", "resource-2.jpg", "resource-3.jpg"]),
        ("oldest", ["resource-2.jpg", "resource-1.jpg", "resource-n.jpg", "resource-3.jpg"]),
        ("name", ["resource-1.jpg", "resource-2.jpg", "resource-n.jpg", "resource-3.jpg"]),
    ),
)
def test_sorting_keeps_deleted_media_at_the_end_for_every_sort(
    sort: str,
    expected: list[str],
) -> None:
    items = (
        _item("resource-1.jpg", "2026-08-03T00:00:00Z"),
        _item("resource-2.jpg", "2026-08-02T00:00:00Z"),
        replace(
            _item("resource-3.jpg", "2026-08-01T00:00:00Z"),
            is_deleted=True,
            deleted_at="2026-08-10T00:00:00Z",
        ),
        _item("resource-n.jpg", "2026-08-04T00:00:00Z"),
    )

    assert [item.filename for item in sort_media_items(items, sort)] == expected


def test_page_invalid_and_out_of_range_values_are_normalized() -> None:
    items = tuple(_item(f"{index:02d}.jpg") for index in range(25))

    negative = paginate_media_items(items, page="-3")
    too_large = paginate_media_items(items, page="999")
    malformed = paginate_media_items(items, page="not-a-page")

    assert negative.current_page == 1
    assert malformed.current_page == 1
    assert too_large.current_page == 2
    assert too_large.total_pages == 2
    assert len(too_large.items) == 1


def test_pagination_matches_investment_table_five_page_chunks() -> None:
    first_chunk = build_local_store_pagination(total_pages=12, current_page=1)
    middle_chunk = build_local_store_pagination(total_pages=12, current_page=6)

    assert [(item.kind, item.page, item.is_active) for item in first_chunk] == [
        ("page", 1, True),
        ("page", 2, False),
        ("page", 3, False),
        ("page", 4, False),
        ("page", 5, False),
        ("ellipsis", 0, False),
        ("page", 12, False),
        ("next", 6, False),
    ]
    assert [(item.kind, item.page, item.is_active) for item in middle_chunk] == [
        ("previous", 5, False),
        ("page", 1, False),
        ("ellipsis", 0, False),
        ("page", 6, True),
        ("page", 7, False),
        ("page", 8, False),
        ("page", 9, False),
        ("page", 10, False),
        ("ellipsis", 0, False),
        ("page", 12, False),
        ("next", 11, False),
    ]


def test_pagination_ellipses_expose_five_page_ranges_and_merge_short_tail() -> None:
    pagination = build_local_store_pagination(total_pages=457, current_page=53)
    leading, trailing = [item for item in pagination if item.kind == "ellipsis"]

    assert leading.position == "leading"
    assert leading.ranges == tuple((start, start + 4) for start in range(1, 51, 5))
    assert trailing.position == "trailing"
    assert trailing.ranges[0] == (56, 60)
    assert trailing.ranges[-2:] == ((446, 450), (451, 457))
    assert len(trailing.ranges) == 80


def test_pagination_omits_controls_for_a_single_page() -> None:
    assert build_local_store_pagination(total_pages=1, current_page=1) == ()


def test_pagination_range_picker_preserves_a_short_only_or_merged_tail() -> None:
    short_tail = build_local_store_pagination(total_pages=7, current_page=1)
    merged_tail = build_local_store_pagination(total_pages=12, current_page=1)

    assert next(item for item in short_tail if item.kind == "ellipsis").ranges == ((6, 7),)
    assert next(item for item in merged_tail if item.kind == "ellipsis").ranges == ((6, 12),)


def test_date_format_uses_zero_padded_numeric_parts() -> None:
    assert format_captured_at_label("20260808") == "08/08/2026"


def test_stable_id_is_unchanged_across_two_scans(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    _write_media(root / "x" / "demo" / "same.jpg")
    catalog = LocalMediaCatalog(root, ttl_seconds=0)

    first = catalog.snapshot(force_refresh=True)[0]
    second = catalog.snapshot(force_refresh=True)[0]

    assert first.stable_id == second.stable_id
    assert first.stable_id.startswith("media-")


def test_external_symlink_is_rejected_and_not_scanned(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    outside = tmp_path / "outside.jpg"
    _write_media(outside, b"outside")
    link = root / "x" / "outside.jpg"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    catalog = LocalMediaCatalog(root)

    assert resolve_local_media_path(root, "x/outside.jpg") is None
    assert catalog.snapshot(force_refresh=True) == ()


def test_parent_path_traversal_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "local_store"
    _write_media(root / "x" / "safe.jpg")

    assert resolve_local_media_path(root, "../x/safe.jpg") is None
    assert resolve_local_media_path(root, "%2e%2e/x/safe.jpg") is None
    assert resolve_local_media_path(root, "%252e%252e/x/safe.jpg") is None


def test_x_gallery_hides_only_same_directory_video_covers(tmp_path: Path) -> None:
    video = tmp_path / "x/creator/123/123.mp4"
    cover = video.with_suffix(".jpg")
    standalone = video.with_name("123-photo.jpg")
    other_directory = tmp_path / "x/another/123.jpg"
    for path in (video, cover, standalone, other_directory):
        _write_media(path)
    catalog = LocalMediaCatalog(tmp_path)
    assert len(catalog.snapshot()) == 4
    page = catalog.query(source="x")
    assert page.total_count == 3
    assert page.video_count == 1
    assert page.image_count == 2
    assert {item.relative_path for item in page.items} == {
        path.relative_to(tmp_path).as_posix() for path in (video, standalone, other_directory)
    }
    cover_item = next(item for item in catalog.snapshot() if item.relative_path == cover.relative_to(tmp_path).as_posix())
    assert catalog.query(media_id=cover_item.stable_id).items == (cover_item,)
    assert cover.read_bytes() == b"media"
