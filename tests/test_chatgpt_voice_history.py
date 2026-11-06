"""Protect completed outbound voice source nodes without admitting internal text.

Code version: v1.0.0-codex.0
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.core.chatgpt_downloader import (
    _chatgpt_message_is_cacheable,
    _extract_chatgpt_conversation_messages,
)


def voice_message():
    return {
        "author": {"role": "assistant"},
        "recipient": "all",
        "channel": None,
        "end_turn": False,
        "status": "finished_successfully",
        "metadata": {"bidi_voice_fem_message": True, "bidi_voice_mode_message": True},
        "content": {"content_type": "multimodal_text", "parts": [{
            "content_type": "audio_transcription", "direction": "out", "text": "Hello",
        }]},
    }


def test_completed_outbound_voice_retains_its_source_identity_before_final_reply():
    first = voice_message()
    final = deepcopy(first)
    final.update(channel="final", end_turn=True)
    final["content"]["parts"][0]["text"] = "Hello again"
    payload = {"title": "Voice stages", "mapping": {
        "voice-stage": {"parent": None, "message": first},
        "voice-final": {"parent": "voice-stage", "message": final},
    }}
    rows = _extract_chatgpt_conversation_messages(
        payload, "https://chatgpt.com/c/synthetic-voice", "2026-10-22T00:00:00Z",
    )
    assert [row["message_key"] for row in rows] == [
        "synthetic-voice:voice-stage", "synthetic-voice:voice-final",
    ]
    assert [row["content_text"] for row in rows] == ["Hello", "Hello again"]


@pytest.mark.parametrize("field,value", [
    ("recipient", "python"), ("channel", "analysis"), ("channel", "commentary"),
    ("status", "in_progress"), ("status", None),
])
def test_voice_exception_respects_provider_visibility_and_completion(field, value):
    message = voice_message()
    message[field] = value
    assert not _chatgpt_message_is_cacheable(message, "assistant")


@pytest.mark.parametrize("field,value", [
    ("bidi_voice_fem_message", False), ("bidi_voice_fem_message", 1),
    ("bidi_voice_mode_message", None), ("bidi_voice_mode_message", "true"),
    ("is_visually_hidden_from_conversation", True), ("channel", "analysis"),
])
def test_voice_exception_requires_exact_metadata(field, value):
    message = voice_message()
    message["metadata"][field] = value
    assert not _chatgpt_message_is_cacheable(message, "assistant")


@pytest.mark.parametrize("field,value", [
    ("content_type", "text"), ("direction", "in"), ("direction", "outgoing"),
    ("text", ""), ("text", 1),
])
def test_voice_exception_requires_typed_nonempty_outbound_parts(field, value):
    message = voice_message()
    message["content"]["parts"][0][field] = value
    assert not _chatgpt_message_is_cacheable(message, "assistant")


@pytest.mark.parametrize("parts", [[], ["Hello"], [None]])
def test_voice_exception_rejects_untyped_or_absent_parts(parts):
    message = voice_message()
    message["content"]["parts"] = parts
    assert not _chatgpt_message_is_cacheable(message, "assistant")


def test_ordinary_unfinished_text_and_nonassistant_remain_excluded():
    message = voice_message()
    message["content"] = {"content_type": "text", "parts": ["Ordinary reply"]}
    assert not _chatgpt_message_is_cacheable(message, "assistant")
    message["end_turn"] = True
    assert _chatgpt_message_is_cacheable(message, "assistant")
    assert not _chatgpt_message_is_cacheable(voice_message(), "tool")
