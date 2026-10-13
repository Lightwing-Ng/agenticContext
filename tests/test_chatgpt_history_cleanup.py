"""Regression coverage for recoverable local ChatGPT trace cleanup."""

# Code version: v1.2.0-claude.0

import hashlib
import json
from pathlib import Path

import pytest

from app.core.chatgpt_downloader import ChatGPTHistoryStore, _extract_chatgpt_conversation_messages
from app.core.chatgpt_history_cleanup import (
    clean_history,
    is_chatgpt_tool_trace,
    normalize_history_markdown,
    plan_history_cleanup,
)
from app.core.job_lock import CacheTaskLock
from app.core.resource_persistence import CHATGPT_HISTORY_SCHEMA, read_parquet_rows, write_parquet_rows_atomic


def fixture_rows():
    rows = []
    for index, (role, text) in enumerate([
        ("user", 'search("Keep the user prompt")'),
        ("assistant", 'search("query")'),
        ("assistant", "slow|query"),
        ("assistant", "I will check the sources."),
        ("assistant", "Final answer."),
        ("user", "Return JSON"),
        ("assistant", '{"search_query":[{"q":"requested JSON answer"}]}'),
    ]):
        rows.append({"conversation_id": "fixture", "message_key": f"fixture:{index}",
                     "message_index": index, "schema_version": 2, "role": role, "content_text": text})
    return rows


def test_plan_preserves_prompts_final_json_and_unreviewed_prose():
    rows = fixture_rows()
    kept, removed = plan_history_cleanup(rows, set())
    assert [row["message_key"] for row in kept] == ["fixture:0", "fixture:3", "fixture:4", "fixture:5", "fixture:6"]
    assert len(removed) == 2
    assert [row["message_index"] for row in kept] == list(range(5))
    assert rows[4]["message_index"] == 4
    reviewed = "chat-" + hashlib.sha256(b"chatgpt:fixture:3").hexdigest()[:24]
    kept, removed = plan_history_cleanup(rows, {reviewed})
    assert len(removed) == 3
    assert "Final answer." in [row["content_text"] for row in kept]


@pytest.mark.parametrize("text", ['search("x")', "slow|query", '{"path":"/connector_demo/search","args":{"query":"x"}}'])
def test_future_cache_excludes_legacy_trace_but_preserves_explicit_final(text):
    message = {"author": {"role": "assistant"}, "content": {"parts": [text]}}
    payload = {"mapping": {"node": {"message": message}}}
    assert is_chatgpt_tool_trace(text)
    assert _extract_chatgpt_conversation_messages(payload, "https://chatgpt.com/c/test", "now") == []
    message["channel"] = "final"
    assert len(_extract_chatgpt_conversation_messages(payload, "https://chatgpt.com/c/test", "now")) == 1


@pytest.mark.parametrize("text", ['search("x") is sample code.', "slow|query\nExplanation", '{"result":"ordinary JSON"}', 'search(open("secret"))'])
def test_trace_detection_does_not_execute_or_match_embedded_examples(text):
    assert not is_chatgpt_tool_trace(text)


def test_cleanup_is_locked_backed_up_atomic_and_idempotent(tmp_path: Path):
    path = tmp_path / "llm/chatgpt/history.parquet"
    store = ChatGPTHistoryStore(path)
    store.replace_conversation("https://chatgpt.com/c/fixture", {"mapping": {
        "user": {"message": {"author": {"role": "user"}, "content": {"parts": ["Prompt"]}}},
        "trace": {"message": {"author": {"role": "assistant"}, "channel": "final", "content": {"parts": ['search("query")']}}},
        "answer": {"message": {"author": {"role": "assistant"}, "content": {"parts": ["Final answer"]}}},
    }}, "now")
    store.save()
    rows = read_parquet_rows(path)
    for row in rows:
        row["schema_version"] = 2
    write_parquet_rows_atomic(path, rows, CHATGPT_HISTORY_SCHEMA)
    original = path.read_bytes()
    # Either the shared gate or one task's own resource lock marks an active cache writer.
    for lock_name in (".cache_task.lock", ".cache_task.task-chatgpt-text.lock"):
        lock = CacheTaskLock(tmp_path / lock_name)
        assert lock.acquire("test-writer")
        try:
            with pytest.raises(RuntimeError, match="running"):
                clean_history(tmp_path, set(), True)
            assert path.read_bytes() == original
        finally:
            lock.release()
    assert clean_history(tmp_path, set())["removed"] == 1
    assert path.read_bytes() == original
    result = clean_history(tmp_path, set(), True)
    assert result["after"] == 2
    assert Path(result["backup"]).read_bytes() == original
    assert [row["content_text"] for row in read_parquet_rows(path)] == ["Prompt", "Final answer"]
    assert clean_history(tmp_path, set(), True)["removed"] == 0


def test_markdown_repair_is_locked_backed_up_structure_preserving_and_idempotent(tmp_path: Path):
    path = tmp_path / "llm/chatgpt/history.parquet"
    store = ChatGPTHistoryStore(path)
    store.replace_conversation("https://chatgpt.com/c/fixture", {"mapping": {
        "user": {"message": {"author": {"role": "user"}, "content": {"parts": ["Prompt"]}}},
        "answer": {"message": {"author": {"role": "assistant"}, "content": {"parts": ["Answer"]}}},
    }}, "now")
    store.save()
    rows = read_parquet_rows(path)
    # Legacy rows were written before the converter, so their stored text still carries provider tokens.
    answer = next(row for row in rows if row["content_text"] == "Answer")
    answer["content_text"] = "Answer \ue200cite\ue202turn0search1\ue201 with a source."
    marker = {**answer, "message_key": "fixture:marker", "message_index": 2,
              "content_text": "\ue200cite\ue202turn0search2\ue201"}
    rows.append(marker)
    write_parquet_rows_atomic(path, rows, CHATGPT_HISTORY_SCHEMA)
    original = path.read_bytes()
    lock = CacheTaskLock(tmp_path / ".cache_task.lock")
    assert lock.acquire("test-writer")
    try:
        with pytest.raises(RuntimeError, match="running"):
            normalize_history_markdown(tmp_path, True)
    finally:
        lock.release()
    assert path.read_bytes() == original
    dry_run = normalize_history_markdown(tmp_path)
    assert dry_run == {"before": 3, "after": 3, "changed": 2, "applied": False}
    assert path.read_bytes() == original
    result = normalize_history_markdown(tmp_path, True)
    assert result["applied"] is True
    assert Path(result["backup"]).read_bytes() == original
    manifest = json.loads((Path(result["backup"]).parent / "normalization.json").read_text())
    assert manifest["applied"] is True
    stored = {row["message_key"]: row for row in read_parquet_rows(path)}
    # Rows are never removed, so message and turn positions and the stored revision still match.
    assert sorted(stored) == sorted(["fixture:marker", answer["message_key"], rows[0]["message_key"]])
    assert stored["fixture:marker"]["content_text"] == ""
    assert stored["fixture:marker"]["message_index"] == 2
    answer_row = stored[answer["message_key"]]
    assert answer_row["content_text"] == "Answer with a source."
    assert answer_row["content_sha256"] == hashlib.sha256(b"Answer with a source.").hexdigest()
    assert answer_row["message_index"] == answer["message_index"]
    assert normalize_history_markdown(tmp_path, True) == {"before": 3, "after": 3, "changed": 0, "applied": False}
    assert len(list((tmp_path / "recovery").iterdir())) == 1


@pytest.mark.parametrize("text", [
    'fast|"Example" employee plan\nfast|"Example" registration\nlength|medium',
    'open|turn937497search13\nopen|turn937497search0\nlength|medium',
    'open|turn646752view2|1\nopen|turn646752view1|1\nlength|short',
    'open|turn985071search3\nfind|turn985071search3|China\nopen|turn985071search3|1\nlength|medium',
    'slow|first query\nfast|second query\nlength|long',
    'open|https://example.com/page\nfind|https://example.com/page|Evidence',
])
def test_compact_web_protocol_is_filtered_for_future_and_legacy_history(text):
    rows = fixture_rows()
    rows[1]['content_text'] = text
    kept, removed = plan_history_cleanup(rows, set())
    assert 'fixture:1' not in {row['message_key'] for row in kept}
    assert len(removed) == 2
    test_future_cache_excludes_legacy_trace_but_preserves_explicit_final(text)


@pytest.mark.parametrize("text", [
    'fast|query\nThis is an explanation.',
    'open|the door',
    'find|a normal phrase',
    'length|medium',
    'open|turn123search0\nlength|medium\nAn actual answer follows.',
    'The syntax is fast|query.',
    '```\nfast|query\n```',
])
def test_compact_web_protocol_does_not_match_normal_answers(text):
    assert not is_chatgpt_tool_trace(text)
