"""Account boundaries, source integrity, and human-chat rendering stay explicit.

Code version: v1.1.0-codex.0
"""

from __future__ import annotations

import gzip
import hashlib
import json
from html import unescape
from pathlib import Path
import re
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.chat_history_browser import query_chat_history
from app.core.config import CrawlConfig
from app.core.local_media_browser import normalize_browser_filters
from app.core.wechat_archive import import_wechat_archive, wechat_archive_path
from app.web.app import create_app
from app.web.cache_sources import CACHE_SOURCE_BY_KEY
from app.web.config_store import SavedConfigStore


def make_archive(root: Path, *, repeated=1) -> Path:
    root.mkdir()
    sources = []
    files = []
    for owner in ("wxid_account_a", "wxid_account_b", None, None):
        source_id = (
            "ios-" + hashlib.md5(owner.encode()).hexdigest()
            if owner
            else f"xlsx-{len(sources)}"
        )
        folder = root / source_id
        folder.mkdir()
        metadata = {
            "source_id": source_id,
            "source_account_id": owner,
            "source_relative_path": "fixture.xlsx",
        }
        if owner:
            metadata.update(
                account_hash_verified=True,
                account_hash=source_id[4:],
                source_account_alias=owner,
            )
        rows = []
        for ordinal in range(repeated):
            for kind in ("direct", "group"):
                row = {
                    "source_id": source_id,
                    "account_partition": source_id,
                    "extraction_record_id": f"same-id-{kind}-{ordinal}",
                    "text": f"你好 {owner or 'unknown'} {kind} <img src=https://invalid.example/track>",
                    "sender_display_name": "Same display name",
                    "sender_id": "wxid_peer",
                    "sent_at_raw": "2021-08-07 18:59:03",
                    "flags": [],
                    "source_locator": {"row": ordinal + 2},
                }
                if owner:
                    row.update(
                        source_account_id=owner,
                        message_type=1,
                        conversation_id=f"peer-{kind}",
                        conversation_display_name="Same title",
                        conversation_kind=kind,
                        sent_at_utc_iso="2021-08-07T10:59:03+00:00",
                        direction="received",
                        server_id="9223372036854775806",
                    )
                    if ordinal % 2:
                        row.update(direction="sent", sender_id=owner)
                else:
                    row["conversation_label"] = "Group Name is not type evidence"
                rows.append(row)
        payload = b"".join(
            (json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows
        )
        encoded = gzip.compress(payload, mtime=0)
        relative = f"{source_id}/messages.jsonl.gz"
        (root / relative).write_bytes(encoded)
        (folder / "source.json").write_text(json.dumps(metadata))
        sources.append(
            {
                "source_id": source_id,
                "source_account_id": owner,
                "text_rows": len(rows),
                "messages_file": relative,
            }
        )
        files.append(
            {
                "stored_relative_path": relative,
                "uncompressed_bytes": len(payload),
                "compressed_sha256": hashlib.sha256(encoded).hexdigest(),
                "uncompressed_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "sources": sources,
                "total_source_separated_text_rows": sum(
                    s["text_rows"] for s in sources
                ),
            }
        )
    )
    (root / "compression_manifest.json").write_text(json.dumps({"files": files}))
    return root


@pytest.fixture
def imported(tmp_path):
    archive = make_archive(tmp_path / "archive")
    store = tmp_path / "store"
    report = import_wechat_archive(archive, store)
    return archive, store, report


def test_same_ids_and_names_never_merge_accounts_or_unknown_sources(imported):
    _, store, report = imported
    assert report["messages"] == 8
    page = query_chat_history(store, source="wechat")
    assert len(page.messaging["accounts"]) == 4
    assert len(page.sessions) == 6
    assert len({s.stable_id for s in page.sessions}) == 6
    groups = query_chat_history(store, source="wechat", chat_kind="group")
    assert len(groups.sessions) == 2
    assert all(s.messaging["kind"] == "group" for s in groups.sessions)
    unknown = query_chat_history(store, source="wechat", chat_kind="unclassified")
    assert len(unknown.sessions) == 2


def test_account_filter_cannot_open_another_accounts_session(imported):
    _, store, _ = imported
    account_a = "account:wxid_account_a"
    page = query_chat_history(store, source="wechat", account=account_a)
    assert len(page.sessions) == 2
    selected = page.sessions[0]
    blocked = query_chat_history(
        store,
        source="wechat",
        account="account:wxid_account_b",
        session=selected.stable_id,
    )
    assert not blocked.items and blocked.current_session is None
    assert not query_chat_history(
        store, source="wechat", account="unknown-account"
    ).sessions
    assert (
        query_chat_history(
            store, source="wechat", account=account_a, query="你好"
        ).conversation_count
        == 2
    )


def test_source_clocks_stay_naive_and_ids_keep_integer_precision(imported):
    _, store, _ = imported
    sessions = query_chat_history(store, source="wechat").sessions
    for session in sessions:
        message = query_chat_history(
            store, source="wechat", session=session.stable_id
        ).items[0]
        assert message.messaging["timestamp_label"] == "7 Aug 2021 18:59:03"
        if session.messaging["account_id"]:
            assert message.messaging["provenance"]["server_id"] == "9223372036854775806"
        else:
            assert message.messaging["time_basis"] == "Source clock; timezone unknown"
            assert message.last_seen_at == "2021-08-07T18:59:03"


def test_failed_import_preserves_previous_database(imported):
    archive, store, _ = imported
    before = wechat_archive_path(store).read_bytes()
    catalog = json.loads((archive / "catalog.json").read_text())
    first = archive / catalog["sources"][0]["messages_file"]
    first.write_bytes(b"corrupt gzip fixture")
    with pytest.raises(ValueError, match="integrity"):
        import_wechat_archive(archive, store)
    assert wechat_archive_path(store).read_bytes() == before


def test_excluded_sources_are_not_opened_or_reimported(imported):
    archive, store, _ = imported
    old = query_chat_history(store, source="wechat", account="source:xlsx-2").sessions[
        0
    ]
    policy = wechat_archive_path(store).with_name("exclusions.json")
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": [
                    {"source_id": "xlsx-2", "reason": "User-curated compilation"}
                ],
            }
        )
    )
    (archive / "xlsx-2/messages.jsonl.gz").unlink()
    (archive / "xlsx-2/source.json").unlink()
    report = import_wechat_archive(archive, store)
    assert report["messages"] == 6
    assert report["excluded_sources"] == [
        {
            "source_id": "xlsx-2",
            "declared_text_rows": 2,
            "reason": "User-curated compilation",
        }
    ]
    assert not query_chat_history(
        store, source="wechat", account="source:xlsx-2"
    ).sessions
    assert not query_chat_history(store, source="wechat", session=old.stable_id).items
    assert len(query_chat_history(store, source="wechat").messaging["accounts"]) == 3
    assert all("xlsx-2/" not in path for path in report["read_files"])
    assert query_chat_history(store, source="wechat", account="source:xlsx-3").sessions


def test_invalid_exclusion_policy_preserves_previous_database(imported):
    archive, store, _ = imported
    path = wechat_archive_path(store)
    before = path.read_bytes()
    path.with_name("exclusions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": [{"source_id": "../outside"}],
            }
        )
    )
    with pytest.raises(ValueError, match="excluded source"):
        import_wechat_archive(archive, store)
    assert path.read_bytes() == before
    assert not list(wechat_archive_path(store).parent.glob(".wechat-import-*"))


def test_reading_missing_catalog_has_no_write_side_effects(tmp_path):
    assert not query_chat_history(tmp_path / "absent", source="wechat").sessions
    assert not (tmp_path / "absent").exists()
    assert (
        normalize_browser_filters(source="wechat", view="media")["source"] == "chatgpt"
    )
    assert (
        normalize_browser_filters(source="wechat", view="prompts")["source"]
        == "chatgpt"
    )
    assert "wechat" not in CACHE_SOURCE_BY_KEY


def test_local_resources_reuses_filters_escapes_messages_and_exports_origin(imported):
    _, store, _ = imported
    with patch(
        "app.web.app.SavedConfigStore.load",
        return_value=SavedConfigStore(CrawlConfig()),
    ):
        application = create_app(
            store,
            computer_use_settings_path=store / "settings/agent.json",
            computer_use_runtime_root=store / "runtime",
            agent_external_operations_enabled=False,
        )
    application.config.update(TESTING=True)
    client = application.test_client()
    account = "account:wxid_account_a"
    with patch.object(
        application.extensions["local_media_catalog"],
        "snapshot",
        side_effect=AssertionError("WeChat must not enumerate media"),
    ):
        response = client.get(
            "/browser",
            query_string={
                "view": "text",
                "source": "wechat",
                "account": account,
                "chat_kind": "group",
            },
        )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "WeChat account" in html and "Conversation type" in html
    assert 'data-wechat-conversation-kind="group"' in html
    links = [unescape(value) for value in re.findall(r'href="([^"]+)"', html)]
    assert any(
        parse_qs(urlsplit(link).query).get("account") == [account] for link in links
    )
    session = query_chat_history(
        store, source="wechat", account=account, chat_kind="group"
    ).sessions[0]
    response = client.get(
        "/browser",
        query_string={
            "source": "wechat",
            "session": session.stable_id,
            "account": account,
            "chat_kind": "group",
        },
    )
    html = response.get_data(as_text=True)
    assert "wechat-chat--group" in html and "wechat-message--received" in html
    assert "&lt;img src=https://invalid.example/track&gt;" in html
    assert '<img src="https://invalid.example/track"' not in html
    assert "data-prompt-add" not in html
    export = client.get(
        f"/browser/session/{session.stable_id}/export",
        query_string={"source": "wechat", "account": account, "chat_kind": "group"},
    )
    assert export.status_code == 200
    assert "Source account: wxid_account_a" in export.get_data(as_text=True)
    assert "Conversation type: group" in export.get_data(as_text=True)
    assert "Time basis: UTC+08:00" in export.get_data(as_text=True)
    assert "9223372036854775806" in export.get_data(as_text=True)
    assert "Direction: received" in export.get_data(as_text=True)
    assert "Source record:" in export.get_data(as_text=True)
