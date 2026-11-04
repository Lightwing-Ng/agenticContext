"""Verify durable recovery protection through the normal ChatGPT store.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.core.chatgpt_downloader import ChatGPTHistoryStore
from app.core.resource_persistence import read_parquet_rows


URL = "https://chatgpt.com/c/synthetic-session"
CAPTURE = "2026-10-22T00:00:00Z"


def payload(**bodies):
    return {"title": "Synthetic title", "mapping": {
        key: {"message": {"author": {"role": "user"}, "content": {"parts": [body]}}}
        for key, body in bodies.items()
    }}


def create_store(tmp_path):
    path = tmp_path / "history.parquet"
    store = ChatGPTHistoryStore(path)
    store.replace_conversation(URL, payload(old="Original body"), CAPTURE, "old-revision")
    store.save()
    return path, store


def enable(path, value=None):
    path.with_name("history-preservation.json").write_text(json.dumps(
        {"version": 1, "mode": "preserve-existing"} if value is None else value,
    ))


def test_guard_retains_missing_rows_and_appends_new_rows_with_stable_indexes(tmp_path):
    path, _ = create_store(tmp_path)
    before = read_parquet_rows(path)
    enable(path)
    store = ChatGPTHistoryStore(path)
    assert store.replace_conversation(URL, payload(new="New body"), CAPTURE, "latest") == (1, False)
    store.save()
    after = read_parquet_rows(path)
    assert after[0] == before[0]
    assert after[1]["message_key"] == "synthetic-session:new"
    assert after[1]["message_index"] == 1
    assert after[1]["provider_revision"] is None


def test_conflicts_survive_repeat_normal_sync_and_title_refresh(tmp_path):
    path, _ = create_store(tmp_path)
    before = read_parquet_rows(path)
    enable(path)
    store = ChatGPTHistoryStore(path)
    assert store.replace_conversation(URL, payload(old="Recovered body"), CAPTURE) == (1, False)
    store.refresh_titles({"synthetic-session": "Changed title"})
    store.save()
    after = read_parquet_rows(path)
    assert after[0] == before[0]
    assert after[1]["content_text"] == "Recovered body"
    assert ":recovered:online:" in after[1]["message_key"]
    store = ChatGPTHistoryStore(path)
    assert store.replace_conversation(URL, payload(old="Recovered body"), CAPTURE) == (0, True)
    store.save()
    assert read_parquet_rows(path) == after


def test_empty_online_mapping_cannot_remove_protected_rows(tmp_path):
    path, _ = create_store(tmp_path)
    before = path.read_bytes()
    enable(path)
    store = ChatGPTHistoryStore(path)
    with pytest.raises(RuntimeError, match="no cacheable"):
        store.replace_conversation(URL, payload(), CAPTURE)
    store.save()
    assert read_parquet_rows(path)[0]["content_text"] == "Original body"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(before).hexdigest()


@pytest.mark.parametrize("value", [[], {"version": True, "mode": "preserve-existing"},
                                  {"version": 2, "mode": "preserve-existing"},
                                  {"version": 1, "mode": "replace"}])
def test_invalid_guard_blocks_store_loading_without_touching_history(tmp_path, value):
    path, _ = create_store(tmp_path)
    before = path.read_bytes()
    enable(path, value)
    with pytest.raises(RuntimeError, match="guard is invalid"):
        ChatGPTHistoryStore(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["save", "refresh_titles", "replace_conversation"])
def test_guard_is_revalidated_for_an_already_loaded_store(tmp_path, operation):
    path, store = create_store(tmp_path)
    before = path.read_bytes()
    path.with_name("history-preservation.json").write_text("invalid json")
    with pytest.raises(RuntimeError, match="guard is unreadable"):
        if operation == "save":
            store.save()
        elif operation == "refresh_titles":
            store.refresh_titles({"synthetic-session": "Changed"})
        else:
            store.replace_conversation(URL, payload(new="Never saved"), CAPTURE)
    assert path.read_bytes() == before
