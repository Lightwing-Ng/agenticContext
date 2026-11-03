"""Read-only WeChat Tunnel tools stay inside the configured archive.

Code version: v1.0.0-codex.0
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.core.tunnel_mcp import TunnelMcpService, tunnel_tool_definitions
from app.core.tunnel_projects import ProjectRegistry
from app.core.wechat_archive import _SCHEMA as HISTORY_SCHEMA
from app.core.wechat_contact_analysis import (
    INDEX_VERSION,
    _REFERENCE_SCHEMA,
    _sources,
    contact_analysis_cache_path,
)
from app.core.wechat_contacts import canonical_contact_id, initialize_contact_registry
from app.core.wechat_mcp_archive import archive_order_key


def _registry(path: Path, projects: list[dict]) -> ProjectRegistry:
    path.write_text(json.dumps({"schema_version": 1, "projects": projects}))
    return ProjectRegistry(path)


class _Settings:
    def __init__(self, workspace_path: str) -> None:
        self.workspace_path = workspace_path


def _service(tmp_path: Path, store: Path | None = None) -> TunnelMcpService:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "AGENTS.md").write_text("# Rules\n")
    (project / "app.py").write_text('print("hi")\n')
    registry = _registry(
        tmp_path / "tunnel-projects.json",
        [{"id": "main", "root": str(project), "writable": True}],
    )
    return TunnelMcpService(
        lambda: _Settings(str(project)),
        registry=registry,
        runtime_root=tmp_path / "runtime",
        local_store_root=store,
    )


def _rpc(service: TunnelMcpService, method: str, params: dict | None = None, *, provider: str = "chatgpt"):
    status, body = service.handle(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        {},
        provider=provider,
    )
    assert status == 200
    return body


def _call(service: TunnelMcpService, name: str, arguments: dict | None = None, *, provider: str = "chatgpt"):
    body = _rpc(
        service,
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        provider=provider,
    )
    if "error" in body:
        return {"isError": True, "content": [{"type": "text", "text": body["error"]["message"]}]}, body["error"]
    result = body["result"]
    return result, result.get("structuredContent", {})


def _message_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _fingerprint(path: Path) -> tuple[int, int]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    stat = path.stat()
    return digest, stat.st_mtime_ns, stat.st_size


def build_wechat_store(root: Path) -> dict:
    """Publish a tiny read-only archive with two same-name contacts and one index."""
    wechat = root / "messaging" / "wechat"
    wechat.mkdir(parents=True)
    ada = canonical_contact_id("wxid_ada", authority="native_contact_username")
    bey = canonical_contact_id("wxid_bey", authority="native_contact_username")
    scoped = "wechat:contact:" + hashlib.sha256(b"scoped-ada").hexdigest()
    conversation = "wechat:" + hashlib.sha256(b"conversation").hexdigest()
    messages = {
        "one": ("2020-01-01T00:00:00+08:00", "one", "wxid_ada", "Ada"),
        "two": ("2020-01-02T00:00:00+08:00", "two", "wxid_ada", "Ada"),
        "side": ("2020-01-02T01:00:00+08:00", "side", "wxid_bey", "Bey"),
        "three": ("2020-01-03T00:00:00+08:00", "three", "wxid_ada", "Ada"),
        "undated": ("", "undated", "wxid_ada", "Ada"),
    }
    history = wechat / "history.sqlite3"
    connection = sqlite3.connect(history)
    connection.executescript(HISTORY_SCHEMA)
    connection.execute(
        "INSERT INTO partitions VALUES(?,?,?,?,?,?,?,?)",
        (
            "src-test",
            "source:src-test",
            "",
            "Test archive",
            "Source account unconfirmed",
            "Excel export",
            "ada.xlsx",
            "messages.jsonl.gz",
        ),
    )
    ids = {label: _message_id(label) for label in messages}
    rows = []
    for ordinal, label in enumerate(("one", "two", "side", "three", "undated"), start=1):
        timestamp, text, sender, sender_label = messages[label]
        known = bool(timestamp)
        rows.append(
            (
                ids[label],
                conversation,
                ordinal,
                sender,
                sender_label,
                "received",
                text,
                archive_order_key(timestamp) if known else 0,
                timestamp,
                timestamp or "Unknown time",
                "UTC+08:00" if known else "Unavailable source time",
                json.dumps(
                    {"record_id": label, "locator": {"table": "Chat_test"}, "server_id": ordinal}
                ),
                json.dumps(["imported"] if label != "undated" else ["missing_time"]),
                text.casefold(),
            )
        )
    connection.executemany(
        "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    connection.execute(
        "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            conversation,
            "src-test",
            "source:src-test",
            "",
            "Ada",
            "direct",
            len(rows),
            rows[0][7],
            rows[3][7],
            rows[0][8],
            rows[3][8],
            "three",
        ),
    )
    connection.execute(
        "INSERT INTO archive_info VALUES('manifest',?)",
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "messages": len(rows),
                    "sources": [{"source_id": "src-test", "messages": len(rows)}],
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    contacts = wechat / "contacts.sqlite3"
    registry = sqlite3.connect(contacts)
    initialize_contact_registry(registry)
    for contact_id, username, status, kind in (
        (ada, "wxid_ada", "verified", "wechat_account"),
        (bey, "wxid_bey", "verified", "wechat_account"),
        (scoped, "ada-export", "source_scoped", "source_scoped"),
    ):
        registry.execute(
            "INSERT INTO contacts VALUES(?,?,?,?,?,?)",
            (contact_id, "wechat", "", username, kind, status),
        )
        registry.execute(
            "INSERT INTO contact_observations VALUES(?,?,?,?,?,?,?,?,?)",
            (
                hashlib.sha256(contact_id.encode()).hexdigest(),
                contact_id,
                "src-test",
                "",
                "",
                "",
                "remark",
                "Ada",
                "{}",
            ),
        )
    registry.execute(
        "INSERT INTO contact_identifiers VALUES(?,?,?,?,?)",
        (ada, "wechat", "wxid_ada", "", "native_contact_username"),
    )
    registry.commit()
    registry.close()
    (wechat / "display_names.json").write_text(
        json.dumps({"schema_version": 1, "contacts": {ada: "测试别名"}})
    )

    fingerprint = _sources(root)[2]
    cache = contact_analysis_cache_path(root, ada)
    cache.parent.mkdir()
    indexed = sqlite3.connect(cache)
    indexed.executescript(_REFERENCE_SCHEMA)
    indexed.execute("INSERT INTO info VALUES('fingerprint',?)", (fingerprint,))
    indexed.execute("INSERT INTO info VALUES('version',?)", (INDEX_VERSION,))
    for ordinal, label in enumerate(("one", "two", "three", "undated"), start=1):
        timestamp = messages[label][0]
        known = bool(timestamp)
        indexed.execute(
            "INSERT INTO refs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ids[label],
                ids[label],
                "[]",
                ids[label],
                conversation,
                "src-test",
                "source:src-test",
                archive_order_key(timestamp) if known else 0,
                ordinal,
                1 if known else 0,
                timestamp,
                timestamp or "Unknown time",
                "UTC+08:00" if known else "Unavailable source time",
                timestamp,
            ),
        )
    indexed.commit()
    indexed.close()
    return {
        "root": root,
        "ada_id": ada,
        "bey_id": bey,
        "scoped_id": scoped,
        "conversation_id": conversation,
        "message_ids": ids,
        "history": history,
        "contacts": contacts,
    }


def test_tools_are_registered_read_only_for_chatgpt_only(tmp_path: Path) -> None:
    service = _service(tmp_path)
    listed = _rpc(service, "tools/list")["result"]["tools"]
    gemini = _rpc(service, "tools/list", provider="gemini")["result"]["tools"]
    names = [tool["name"] for tool in listed]
    for name in (
        "wechat_archive_status",
        "wechat_find_contacts",
        "wechat_search_messages",
        "wechat_get_message_context",
    ):
        tool = next(item for item in listed if item["name"] == name)
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert "path" not in tool["inputSchema"]["properties"]
        assert name in names
        assert name not in {item["name"] for item in gemini}
    assert [tool["name"] for tool in tunnel_tool_definitions() if tool["name"].startswith("wechat_")] == [
        "wechat_archive_status",
        "wechat_find_contacts",
        "wechat_search_messages",
        "wechat_get_message_context",
    ]
    refused, _ = _call(service, "wechat_archive_status", provider="gemini")
    assert refused["isError"] is True
    assert "Unknown tool" in refused["content"][0]["text"]


def test_same_display_name_stays_unmerged_and_messages_page(tmp_path: Path) -> None:
    store = build_wechat_store(tmp_path / "store")
    before = (_fingerprint(store["history"]), _fingerprint(store["contacts"]))
    service = _service(tmp_path, store["root"])
    _status, status = _call(service, "wechat_archive_status")
    assert status["ok"] is True
    assert status["message_count"] == 5
    assert status["sources"][0]["source_id"] == "src-test"

    _found, found = _call(service, "wechat_find_contacts", {"query": "Ada"})
    assert found["ok"] is True
    assert [item["contact_id"] for item in found["contacts"]] == [
        *sorted((store["ada_id"], store["bey_id"])),
        store["scoped_id"],
    ]
    matches = {item["contact_id"]: item["identity_match"] for item in found["contacts"]}
    assert matches == {
        store["ada_id"]: "verified",
        store["bey_id"]: "verified",
        store["scoped_id"]: "source_scoped",
    }
    ada = next(item for item in found["contacts"] if item["contact_id"] == store["ada_id"])
    assert ada["conversation_count"] == 1
    assert ada["evidence"][0]["kind"] == "remark"
    assert any(
        item["authority"] == "native_contact_username" for item in ada["identifier_authorities"]
    )

    alias, alias_body = _call(service, "wechat_find_contacts", {"query": "测试别名"})
    assert alias["isError"] is False
    assert [item["contact_id"] for item in alias_body["contacts"]] == [store["ada_id"]]
    assert alias_body["contacts"][0]["evidence"][0]["authority"] == "local_display_preference"

    unverified, unverified_body = _call(
        service,
        "wechat_search_messages",
        {"contact_id": store["scoped_id"]},
    )
    assert unverified["isError"] is True
    assert unverified_body["code"] == "identity_unverified"
    assert "messages" not in unverified_body

    first, first_body = _call(
        service,
        "wechat_search_messages",
        {"contact_id": store["ada_id"], "sort": "newest", "page_size": 1, "page": 1},
    )
    second, second_body = _call(
        service,
        "wechat_search_messages",
        {"contact_id": store["ada_id"], "sort": "newest", "page_size": 1, "page": 2},
    )
    assert first["isError"] is False and second["isError"] is False
    assert first_body["total_count"] == 4
    assert first_body["messages"][0]["text"] == "three"
    assert second_body["messages"][0]["text"] == "two"
    assert first_body["messages"][0]["quality_flags"] == ["imported"]
    assert first_body["messages"][0]["provenance"]["record_id"] == "three"
    assert first_body["messages"][0]["time_basis"] == "UTC+08:00"
    assert first_body["messages"][0]["sender_id"] == "wxid_ada"

    bounded, bounded_body = _call(
        service,
        "wechat_search_messages",
        {
            "contact_id": store["ada_id"],
            "query": "two",
            "not_before": "2020-01-02T00:00:00+08:00",
            "not_after": "2020-01-02T12:00:00+08:00",
            "sort": "oldest",
        },
    )
    assert bounded["isError"] is False
    assert [item["text"] for item in bounded_body["messages"]] == ["two"]
    assert bounded_body["unknown_time_excluded"] == 1

    context, context_body = _call(
        service,
        "wechat_get_message_context",
        {"message_id": store["message_ids"]["two"], "before": 1, "after": 1},
    )
    assert context["isError"] is False
    assert [item["text"] for item in context_body["messages"]] == ["one", "two", "side"]
    assert context_body["messages"][1]["is_target"] is True
    assert (_fingerprint(store["history"]), _fingerprint(store["contacts"])) == before


def test_missing_stale_and_unindexed_archives_are_explicit(tmp_path: Path) -> None:
    service = _service(tmp_path, tmp_path / "absent")
    (tmp_path / "absent").mkdir()
    missing, missing_body = _call(service, "wechat_archive_status")
    assert missing["isError"] is True
    assert missing_body["code"] == "archive_missing"
    assert "messages" not in missing_body

    store = build_wechat_store(tmp_path / "store")
    service._local_store_root = store["root"]
    unindexed = canonical_contact_id("wxid_bey", authority="native_contact_username")
    refused, refused_body = _call(
        service, "wechat_search_messages", {"contact_id": unindexed}
    )
    assert refused["isError"] is True
    assert refused_body["code"] == "index_incomplete"
    assert "messages" not in refused_body

    cache = contact_analysis_cache_path(store["root"], store["ada_id"])
    connection = sqlite3.connect(cache)
    connection.execute("UPDATE info SET value='stale' WHERE key='fingerprint'")
    connection.commit()
    connection.close()
    stale, stale_body = _call(
        service, "wechat_search_messages", {"contact_id": store["ada_id"]}
    )
    assert stale["isError"] is True
    assert stale_body["code"] == "source_changed"

    connection = sqlite3.connect(cache)
    connection.execute(
        "UPDATE info SET value=? WHERE key='fingerprint'", (_sources(store["root"])[2],)
    )
    connection.commit()
    connection.close()
    (store["history"].parent / "history.sqlite3-wal").write_bytes(b"")
    changed, changed_body = _call(service, "wechat_find_contacts", {"query": "Ada"})
    assert changed["isError"] is True
    assert changed_body["code"] == "source_changed"
    assert "contacts" not in changed_body


def test_paths_symlinks_and_other_local_store_data_stay_closed(tmp_path: Path, monkeypatch) -> None:
    store = build_wechat_store(tmp_path / "store")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.sqlite3"
    secret.write_text("SECRET-ARCHIVE")
    linked = tmp_path / "linked-store"
    (linked / "messaging").mkdir(parents=True)
    (linked / "messaging" / "wechat").symlink_to(store["root"] / "messaging" / "wechat", target_is_directory=True)
    service = _service(tmp_path, linked)
    refused, refused_body = _call(service, "wechat_archive_status")
    assert refused["isError"] is True
    assert refused_body["code"] == "archive_refused"
    assert "SECRET-ARCHIVE" not in json.dumps(refused_body)

    service._local_store_root = store["root"]
    history_link = store["history"].with_name("moved.sqlite3")
    store["history"].rename(history_link)
    store["history"].symlink_to(history_link)
    linked_history, linked_body = _call(service, "wechat_archive_status")
    assert linked_history["isError"] is True
    assert linked_body["code"] == "archive_refused"
    store["history"].unlink()
    history_link.rename(store["history"])

    resource = store["history"].with_name("resource_references.sqlite3")
    resource.symlink_to(secret)
    context, context_body = _call(
        service,
        "wechat_get_message_context",
        {"message_id": store["message_ids"]["one"]},
    )
    assert context["isError"] is True
    assert context_body["code"] == "archive_refused"
    resource.unlink()

    extra, extra_body = _call(
        service,
        "wechat_search_messages",
        {"contact_id": store["ada_id"], "archive_path": str(secret)},
    )
    assert extra["isError"] is True
    assert extra_body["code"] == "invalid_arguments"

    opened = []
    real_connect = sqlite3.connect

    def record_connect(database, *args, **kwargs):
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", record_connect)
    private = store["root"] / "private" / "notes.sqlite3"
    private.parent.mkdir()
    private.write_text("private")
    _call(service, "wechat_archive_status")
    assert opened
    assert all("mode=ro" in uri and "immutable=1" in uri for uri in opened), opened
    assert all("notes.sqlite3" not in uri for uri in opened), opened


def test_controller_still_refuses_local_store_while_archive_tools_read(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("# Rules\n")
    (project / "app.py").write_text('print("hi")\n')
    runtime = project / "local_store"
    (runtime / "private").mkdir(parents=True)
    (runtime / "private" / "secret.txt").write_text("runtime secret")
    store = build_wechat_store(tmp_path / "archive")
    registry = _registry(
        tmp_path / "tunnel-projects.json",
        [{"id": "main", "root": str(project), "writable": True}],
    )
    service = TunnelMcpService(
        lambda: _Settings(str(project)),
        registry=registry,
        runtime_root=tmp_path / "runtime",
        local_store_root=store["root"],
    )
    identity = service.registry.resolve("main", str(project)).identity
    listed, listed_body = _call(
        service,
        "list_files",
        {"project": "main", "project_identity": identity, "path": "local_store"},
    )
    assert listed["isError"] is True
    assert listed_body["code"] == "path_refused"
    read, read_body = _call(
        service,
        "read_files",
        {
            "project": "main",
            "project_identity": identity,
            "files": [{"path": "local_store/private/secret.txt"}],
        },
    )
    assert read["isError"] is True
    assert read_body["files"][0]["code"] == "path_refused"
    written, written_body = _call(
        service,
        "write_file",
        {
            "project": "main",
            "project_identity": identity,
            "path": "local_store/messaging/wechat/history.sqlite3",
            "content": "changed",
            "request_id": "wechat-write-0001",
        },
    )
    assert written["isError"] is True
    assert written_body["code"] == "path_refused"
    assert not (runtime / "messaging" / "wechat" / "history.sqlite3").exists()
    status, status_body = _call(service, "wechat_archive_status")
    assert status["isError"] is False
    assert status_body["message_count"] == 5


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({}, "invalid_arguments"),
        ({"contact_id": "wechat:contact:" + "ab" * 32, "page": 0}, "invalid_arguments"),
        ({"conversation_id": "missing-conversation"}, "not_found"),
    ],
)
def test_search_bounds_and_unknown_conversation(tmp_path: Path, arguments: dict, code: str) -> None:
    store = build_wechat_store(tmp_path / "store")
    service = _service(tmp_path, store["root"])
    if arguments.get("conversation_id") == "missing-conversation":
        arguments = {"conversation_id": "wechat:" + "ab" * 32}
    result, body = _call(service, "wechat_search_messages", arguments)
    assert result["isError"] is True
    assert body["code"] == code
    assert "messages" not in body
