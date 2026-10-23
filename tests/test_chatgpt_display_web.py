"""Verify provider-scoped display fallback without changing stored text or exports.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

import hashlib

import pytest

from app.core.chat_history_browser import query_chat_history
from app.core.resource_persistence import CHATGPT_HISTORY_SCHEMA, write_parquet_rows_atomic
from app.web.app import create_app
from app.web.presentation import render_cached_message


COMPONENT = (
    '<box padding={3}><row><title>Example indicator</title><text>units</text></row>'
    '<row><title>2.8</title><text>Reference: 0–3.4</text></row>'
    '<box><svg><text>0</text><text>4.0</text></svg></box>'
    '<caption>Illustration only.</caption></box>'
)
BODY = f'Before.\n\n{COMPONENT}\n\nAfter.<Cite ref="turn390054search0"/>'


def test_display_adapter_is_scoped_to_chatgpt_and_keeps_html_inert():
    rendered = str(render_cached_message(BODY, provider="chatgpt"))
    assert "<table>" in rendered
    assert "Example indicator" in rendered and "Reference: 0–3.4" in rendered
    assert "Illustration only." in rendered and "After." in rendered
    assert "turn390054search0" not in rendered
    assert "&lt;box" not in rendered
    assert "<svg" not in rendered
    assert "&lt;box" in str(render_cached_message(BODY, provider="grok"))
    assert "turn390054search0" in str(render_cached_message(BODY))
    malicious = '<box><script>unsafe()</script><text>Safe words</text></box>'
    assert "<script>" not in str(render_cached_message(malicious, provider="chatgpt"))


@pytest.mark.parametrize("session_view", [True, False])
def test_local_resources_formats_assistant_only_and_export_preserves_original(tmp_path, session_view):
    rows = []
    for index, role in enumerate(("user", "assistant")):
        rows.append({
            "schema_version": 3, "provider_revision": None, "platform": "chatgpt",
            "conversation_id": "synthetic-component", "conversation_url": "https://chatgpt.com/c/synthetic-component",
            "conversation_title": "Synthetic component", "message_key": f"synthetic-component:{role}",
            "turn_index": 0, "message_index": index, "role": role,
            "author_label": "You" if role == "user" else "ChatGPT",
            "content_text": BODY, "content_html": "",
            "content_sha256": hashlib.sha256(BODY.encode()).hexdigest(), "source_links": [],
            "model_label": "", "first_seen_at": "2026-10-22T00:00:00Z", "last_seen_at": "2026-10-22T00:00:00Z",
        })
    path = tmp_path / "llm/chatgpt/history.parquet"
    write_parquet_rows_atomic(path, rows, CHATGPT_HISTORY_SCHEMA)
    before = path.read_bytes()
    page = query_chat_history(tmp_path, source="chatgpt", session_view=True)
    session = page.sessions[0].stable_id
    client = create_app(tmp_path).test_client()
    query = {"view": "text", "source": "chatgpt", "session_view": int(session_view)}
    if session_view:
        query["session"] = session
    response = client.get("/browser", query_string=query)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count("<table>") == 1
    assert html.count("turn390054search0") == 1
    assert "&lt;box" in html
    exported = client.get(f"/browser/session/{session}/export?source=chatgpt")
    assert exported.status_code == 200
    assert exported.get_data(as_text=True).count(BODY) == 2
    assert path.read_bytes() == before
